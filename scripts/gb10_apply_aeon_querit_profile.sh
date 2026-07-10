#!/usr/bin/env bash
# Apply or resume the committed AEON + Querit memory profile on GB10.
set -Eeuo pipefail

export DOCKER_HOST=${DOCKER_HOST:-unix:///run/user/1001/docker.sock}

AEON_UNIT=vllm-aeon-27b-dflash.service
RERANK_UNIT=querit-4b-reranker.service
AEON_CONTAINER=vllm-aeon-27b-dflash-n12
AEON_URL=http://100.105.4.92:18010
RERANK_URL=http://100.105.4.92:18013
EXPECTED_KV_GIB=${GB10_EXPECTED_AEON_KV_GIB:-36}
EXPECTED_KV_MIB=$((EXPECTED_KV_GIB * 1024))
EXPECTED_MEMORY_GIB=${GB10_EXPECTED_AEON_MEMORY_GIB:-69}
MAX_USED_GIB=${GB10_MAX_USED_GIB:-115}
MIN_AVAILABLE_GIB=${GB10_MIN_AVAILABLE_GIB:-4}
BASELINE_MAX_USED_GIB=${GB10_BASELINE_MAX_USED_GIB:-104}
AEON_READY_ATTEMPTS=${GB10_AEON_READY_ATTEMPTS:-360}
RERANK_READY_ATTEMPTS=${GB10_RERANK_READY_ATTEMPTS:-180}
RESUME=0

if [[ ${1:-} == "--resume" ]]; then
  RESUME=1
elif [[ $# -ne 0 ]]; then
  echo "usage: $0 [--resume]" >&2
  exit 2
fi

PEAK_USED_KIB=0
MIN_AVAIL_KIB=999999999
SWAPOUT_START=$(awk '/pswpout / {print $2}' /proc/vmstat)
RERANK_STARTED=0

memory_kib() {
  awk '
    /MemTotal:/ {total=$2}
    /MemAvailable:/ {available=$2}
    END {printf "%d %d\n", total-available, available}
  ' /proc/meminfo
}

sample_memory() {
  local used_kib avail_kib
  read -r used_kib avail_kib < <(memory_kib)
  (( used_kib > PEAK_USED_KIB )) && PEAK_USED_KIB=$used_kib
  (( avail_kib < MIN_AVAIL_KIB )) && MIN_AVAIL_KIB=$avail_kib
  printf 'MEM_SAMPLE ts=%s used_gib=%.2f avail_gib=%.2f\n' \
    "$(date --iso-8601=seconds)" \
    "$(awk -v x="$used_kib" 'BEGIN {print x/1048576}')" \
    "$(awk -v x="$avail_kib" 'BEGIN {print x/1048576}')"
  if (( used_kib >= MAX_USED_GIB * 1048576 || avail_kib < MIN_AVAILABLE_GIB * 1048576 )); then
    echo "MEMORY_LIMIT_EXCEEDED max_used_gib=$MAX_USED_GIB min_available_gib=$MIN_AVAILABLE_GIB"
    return 42
  fi
}

cleanup_on_error() {
  local rc=$?
  trap - ERR
  if (( RERANK_STARTED == 1 )); then
    systemctl --user stop "$RERANK_UNIT" || true
  fi
  echo "DEPLOY_FAILED rc=$rc"
  systemctl --user status "$AEON_UNIT" "$RERANK_UNIT" --no-pager -l || true
  exit "$rc"
}
trap cleanup_on_error ERR

wait_for_aeon() {
  local attempt
  for ((attempt=1; attempt<=AEON_READY_ATTEMPTS; attempt++)); do
    if curl -fsS --max-time 3 "$AEON_URL/v1/models" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  echo "AEON_READY_TIMEOUT attempts=$AEON_READY_ATTEMPTS"
  return 43
}

verify_aeon_profile() {
  local command_json memory_limit expected_memory_limit logs
  command_json=$(docker inspect "$AEON_CONTAINER" --format '{{json .Config.Cmd}}')
  [[ $command_json == *"--kv-cache-memory-bytes\",\"${EXPECTED_KV_MIB}M"* ]]

  memory_limit=$(docker inspect "$AEON_CONTAINER" --format '{{.HostConfig.Memory}}')
  expected_memory_limit=$((EXPECTED_MEMORY_GIB * 1024 * 1024 * 1024))
  (( memory_limit == expected_memory_limit ))

  logs=$(docker logs --since 45m "$AEON_CONTAINER" 2>&1)
  if [[ $logs != *"reserved ${EXPECTED_KV_GIB}.0 GiB memory for KV Cache"* ]]; then
    echo "AEON_KV_RESERVATION_NOT_FOUND expected_gib=$EXPECTED_KV_GIB"
    return 44
  fi
}

echo "PHASE stop_reranker"
systemctl --user stop "$RERANK_UNIT" || true
systemctl --user daemon-reload

if (( RESUME == 0 )); then
  echo "PHASE restart_aeon"
  systemctl --user restart "$AEON_UNIT"
else
  echo "PHASE resume_existing_aeon"
  systemctl --user is-active --quiet "$AEON_UNIT"
fi

wait_for_aeon
verify_aeon_profile
echo "PHASE aeon_ready"
sample_memory
read -r BASE_USED_KIB _ < <(memory_kib)
if (( BASE_USED_KIB >= BASELINE_MAX_USED_GIB * 1048576 )); then
  echo "BASELINE_TOO_HIGH max_gib=$BASELINE_MAX_USED_GIB"
  exit 45
fi

echo "PHASE start_reranker"
systemctl --user start "$RERANK_UNIT"
RERANK_STARTED=1
READY=0
for ((attempt=1; attempt<=RERANK_READY_ATTEMPTS; attempt++)); do
  sample_memory
  if curl -fsS --max-time 2 "$RERANK_URL/v1/models" >/dev/null 2>&1; then
    READY=1
    break
  fi
  if ! systemctl --user is-active --quiet "$RERANK_UNIT"; then
    echo "RERANKER_EXITED_DURING_STARTUP"
    exit 46
  fi
  sleep 2
done
if (( READY == 0 )); then
  echo "RERANKER_READY_TIMEOUT attempts=$RERANK_READY_ATTEMPTS"
  exit 47
fi

for _ in {1..30}; do
  sample_memory
  sleep 2
done

RAW=$(curl -fsS --max-time 30 -H 'content-type: application/json' \
  -d '{"model":"qwen3-reranker-8b","query":"capital of France","documents":["Paris is the capital of France.","Bananas are yellow."],"top_n":2}' \
  "$RERANK_URL/v1/rerank")
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["results"][0]["index"] == 0; print("RAW_RERANK_OK", d["results"])' "$RAW"

GUARD=$(curl -fsS --max-time 30 -H 'content-type: application/json' \
  -d '{"model":"qwen3-reranker-8b","text_1":"capital of France","text_2":"Paris is the capital of France."}' \
  http://100.105.4.92:18003/v1/score)
python3 -c 'import json,math,sys; d=json.loads(sys.argv[1]); s=float(d["data"][0]["score"]); assert math.isfinite(s); print("GUARD_SCORE_OK", s)' "$GUARD"

for service in vllm-embedding.service "$AEON_UNIT" "$RERANK_UNIT" llm-guard-proxy.service; do
  systemctl --user is-active --quiet "$service"
done

SWAPOUT_END=$(awk '/pswpout / {print $2}' /proc/vmstat)
printf 'PEAK used_gib=%.2f min_avail_gib=%.2f swapout_pages_delta=%d\n' \
  "$(awk -v x="$PEAK_USED_KIB" 'BEGIN {print x/1048576}')" \
  "$(awk -v x="$MIN_AVAIL_KIB" 'BEGIN {print x/1048576}')" \
  "$((SWAPOUT_END-SWAPOUT_START))"
free -h
systemctl --user is-active vllm-embedding.service "$AEON_UNIT" "$RERANK_UNIT" llm-guard-proxy.service
trap - ERR
echo "DEPLOY_SUCCESS"
