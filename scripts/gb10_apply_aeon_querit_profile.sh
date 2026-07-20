#!/usr/bin/env bash
# Apply the committed production AEON + Querit profile on GB10.
set -euo pipefail

export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1001/docker.sock}"

AEON_UNIT=vllm-aeon-27b-dflash.service
RERANK_UNIT=vllm-querit-4b-reranker.service
FALLBACK_UNIT=vllm-qwen3-reranker-8b.service
GUARD_UNIT=llm-guard-proxy.service
EMBEDDING_UNIT=vllm-embedding.service
AEON_CONTAINER=vllm-aeon-27b-dflash-n12
EMBEDDING_CONTAINER=vllm-embedding
RERANK_CONTAINER=querit-4b-vllm
FALLBACK_CONTAINER=vllm-qwen3-reranker-8b
NO_SWAP_HELPER=/home/obj/.local/bin/gb10_verify_vllm_no_swap.sh
NO_SWAP_TEST_ARGS=()
if [[ "${GB10_QUERIT_PROFILE_TEST_ONLY:-0}" == 1 ]]; then
    NO_SWAP_HELPER="${GB10_NO_SWAP_HELPER_TEST_PATH:?test helper path required}"
    [[ "$NO_SWAP_HELPER" == /* && -x "$NO_SWAP_HELPER" ]] || {
        echo "test no-swap helper must be an executable absolute path" >&2
        exit 2
    }
    NO_SWAP_TEST_ARGS=(--test-only)
elif [[ -n "${GB10_NO_SWAP_HELPER_TEST_PATH:-}" ]]; then
    echo "test no-swap helper selector requires explicit test-only mode" >&2
    exit 2
fi
AEON_URL="${GB10_AEON_URL:-http://100.105.4.92:18010}"
RERANK_URL="${GB10_RERANK_URL:-http://100.105.4.92:18013}"
GUARD_SCORE_URL="${GB10_GUARD_SCORE_URL:-http://100.105.4.92:18003/v1/score}"
# Keep this owner aligned with the committed AEON unit: AUTO KV sizing must
# remain enabled so the patched UMA headroom guard can size the pool.
EXPECTED_AEON_GPU_MEMORY_UTILIZATION=0.355
EXPECTED_AEON_MEMORY_BYTES=$((128 * 1024 * 1024 * 1024))
MIN_AVAILABLE_GIB=${GB10_MIN_AVAILABLE_GIB:-4}
AEON_READY_ATTEMPTS=${GB10_AEON_READY_ATTEMPTS:-120}
RERANK_READY_ATTEMPTS=${GB10_RERANK_READY_ATTEMPTS:-180}
SYSTEMCTL_TIMEOUT_SECONDS=${GB10_SYSTEMCTL_TIMEOUT_SECONDS:-120}
SYSTEMCTL_START_TIMEOUT_SECONDS=${GB10_SYSTEMCTL_START_TIMEOUT_SECONDS:-1900}
DOCKER_TIMEOUT_SECONDS=${GB10_DOCKER_TIMEOUT_SECONDS:-15}
for value in \
    "$MIN_AVAILABLE_GIB" \
    "$AEON_READY_ATTEMPTS" \
    "$RERANK_READY_ATTEMPTS" \
    "$SYSTEMCTL_TIMEOUT_SECONDS" \
    "$SYSTEMCTL_START_TIMEOUT_SECONDS" \
    "$DOCKER_TIMEOUT_SECONDS"; do
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
        echo "headroom and timeout values must be positive integers: $value" >&2
        exit 2
    fi
done

if [[ $# -ne 0 ]]; then
    echo "usage: $0" >&2
    exit 2
fi

run_systemctl() {
    /usr/bin/timeout --signal=TERM --kill-after=5 "$SYSTEMCTL_TIMEOUT_SECONDS" \
        systemctl --user "$@"
}

run_systemctl_start() {
    /usr/bin/timeout --signal=TERM --kill-after=5 \
        "$SYSTEMCTL_START_TIMEOUT_SECONDS" systemctl --user "$@"
}

run_docker() {
    /usr/bin/timeout --signal=TERM --kill-after=5 "$DOCKER_TIMEOUT_SECONDS" \
        docker "$@"
}

verify_no_swap() {
    local unit="$1" container="${2:-}"
    local -a arguments=(
        "${NO_SWAP_TEST_ARGS[@]}"
        --unit "/home/obj/.config/systemd/user/$unit"
    )
    if [[ -n "$container" ]]; then
        arguments+=(--container "$container")
    fi
    if (( ${#NO_SWAP_TEST_ARGS[@]} == 1 )); then
        "$NO_SWAP_HELPER" "${arguments[@]}"
    else
        /usr/bin/env -i \
            HOME=/home/obj \
            PATH=/usr/bin:/bin \
            LC_ALL=C \
            DOCKER_HOST=unix:///run/user/1001/docker.sock \
            /usr/bin/bash --noprofile --norc \
            "$NO_SWAP_HELPER" "${arguments[@]}"
    fi
}

require_cgroup_v2() {
    local version
    if ! version="$(run_docker info --format '{{.CgroupVersion}}')"; then
        printf 'Docker cgroup-version preflight failed\n' >&2
        return 1
    fi
    if [[ "$version" != "2" ]]; then
        printf 'Docker must report cgroup version exactly 2 (got %q)\n' "$version" >&2
        return 1
    fi
}

unit_enabled_state() {
    local state rc=0
    state="$(run_systemctl is-enabled "$1" 2>/dev/null)" || rc=$?
    if [[ -z "$state" || ( $rc -ne 0 && $rc -ne 1 ) ]]; then
        echo "cannot determine enablement state for $1 rc=$rc" >&2
        return 45
    fi
    printf '%s' "$state"
}

unit_active_state() {
    local state rc=0
    state="$(run_systemctl is-active "$1" 2>/dev/null)" || rc=$?
    if [[ -z "$state" || ( $rc -ne 0 && $rc -ne 3 ) ]]; then
        echo "cannot determine active state for $1 rc=$rc" >&2
        return 46
    fi
    printf '%s' "$state"
}

restore_unit_enablement() {
    local unit="$1" state="$2"
    case "$state" in
        enabled) run_systemctl enable "$unit" ;;
        enabled-runtime) run_systemctl enable --runtime "$unit" ;;
        disabled) run_systemctl disable "$unit" ;;
        masked) run_systemctl mask "$unit" ;;
        masked-runtime) run_systemctl mask --runtime "$unit" ;;
        static|indirect|generated|transient) return 0 ;;
        *)
            echo "unsupported enablement state for $unit: $state" >&2
            return 47
            ;;
    esac
}

PREV_RERANK_ENABLED=""
PREV_RERANK_ACTIVE=""
PREV_FALLBACK_ENABLED=""
PREV_FALLBACK_ACTIVE=""
MIGRATION_STARTED=0
DEPLOY_SUCCESS=0
CLEANUP_STARTED=0

rollback_runtime_state() {
    if ! require_cgroup_v2; then
        printf 'Rollback refused service mutation without Docker cgroup v2\n' >&2
        return 1
    fi
    local rollback_failed=0
    run_systemctl stop "$RERANK_UNIT" || rollback_failed=1
    run_systemctl stop "$FALLBACK_UNIT" || rollback_failed=1
    restore_unit_enablement "$RERANK_UNIT" "$PREV_RERANK_ENABLED" || rollback_failed=1
    restore_unit_enablement "$FALLBACK_UNIT" "$PREV_FALLBACK_ENABLED" || rollback_failed=1
    if [[ "$PREV_FALLBACK_ACTIVE" == "active" ]]; then
        run_systemctl_start start "$FALLBACK_UNIT" || rollback_failed=1
    elif [[ "$PREV_RERANK_ACTIVE" == "active" ]]; then
        run_systemctl_start start "$RERANK_UNIT" || rollback_failed=1
    fi
    verify_no_swap "$AEON_UNIT" "$AEON_CONTAINER" || rollback_failed=1
    verify_no_swap "$EMBEDDING_UNIT" "$EMBEDDING_CONTAINER" || rollback_failed=1
    if [[ "$PREV_FALLBACK_ACTIVE" == "active" ]]; then
        verify_no_swap "$FALLBACK_UNIT" "$FALLBACK_CONTAINER" || rollback_failed=1
    elif [[ "$PREV_RERANK_ACTIVE" == "active" ]]; then
        verify_no_swap "$RERANK_UNIT" "$RERANK_CONTAINER" || rollback_failed=1
    fi
    return "$rollback_failed"
}

cleanup_failure() {
    local rc="$1" reason="$2" final_rc="$1"
    if (( final_rc == 0 )); then
        final_rc=49
    fi
    if (( CLEANUP_STARTED == 1 )); then
        return
    fi
    CLEANUP_STARTED=1
    trap - ERR INT TERM EXIT
    echo "DEPLOY_FAILED rc=$rc reason=$reason" >&2
    if (( MIGRATION_STARTED == 1 )); then
        if ! rollback_runtime_state; then
            echo "ROLLBACK_FAILED: inspect and restore the previous committed units" >&2
            final_rc=70
        fi
    fi
    run_systemctl status \
        "$AEON_UNIT" "$RERANK_UNIT" "$FALLBACK_UNIT" "$GUARD_UNIT" \
        --no-pager -l || true
    exit "$final_rc"
}

cleanup_on_error() {
    local rc=$?
    cleanup_failure "$rc" ERR
}

cleanup_on_signal() {
    cleanup_failure "$2" "$1"
}

cleanup_on_exit() {
    local rc=$?
    if (( DEPLOY_SUCCESS == 0 && MIGRATION_STARTED == 1 )); then
        cleanup_failure "$rc" EXIT
    fi
}

trap cleanup_on_error ERR
trap 'cleanup_on_signal INT 130' INT
trap 'cleanup_on_signal TERM 143' TERM
trap cleanup_on_exit EXIT

require_cgroup_v2

PREV_RERANK_ENABLED="$(unit_enabled_state "$RERANK_UNIT")"
PREV_RERANK_ACTIVE="$(unit_active_state "$RERANK_UNIT")"
PREV_FALLBACK_ENABLED="$(unit_enabled_state "$FALLBACK_UNIT")"
PREV_FALLBACK_ACTIVE="$(unit_active_state "$FALLBACK_UNIT")"
run_systemctl is-active --quiet "$EMBEDDING_UNIT"
verify_no_swap "$AEON_UNIT" "$AEON_CONTAINER"
verify_no_swap "$EMBEDDING_UNIT" "$EMBEDDING_CONTAINER"
if [[ "$PREV_FALLBACK_ACTIVE" == "active" ]]; then
    verify_no_swap "$FALLBACK_UNIT" "$FALLBACK_CONTAINER"
elif [[ "$PREV_RERANK_ACTIVE" == "active" ]]; then
    verify_no_swap "$RERANK_UNIT" "$RERANK_CONTAINER"
fi

read_mem_available_kib() {
    local key rest value
    while IFS=: read -r key rest; do
        if [[ "$key" == "MemAvailable" ]]; then
            read -r value _ <<< "$rest"
            printf '%s' "${value:-0}"
            return
        fi
    done < "${GB10_MEMINFO_PATH:-/proc/meminfo}"
    printf '0'
}

require_memory_headroom() {
    local available_kib
    available_kib="$(read_mem_available_kib)"
    if (( available_kib < MIN_AVAILABLE_GIB * 1024 * 1024 )); then
        echo "INSUFFICIENT_MEMORY available_kib=$available_kib minimum_gib=$MIN_AVAILABLE_GIB" >&2
        return 42
    fi
}

wait_for_url() {
    local url="$1" attempts="$2" label="$3" attempt
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    echo "${label}_READY_TIMEOUT attempts=$attempts" >&2
    return 43
}

verify_aeon_profile() {
    local command_json memory_limit memory_swap_limit
    if ! command_json="$(run_docker inspect "$AEON_CONTAINER" --format '{{json .Config.Cmd}}')"; then
        echo "AEON_INSPECT_FAILED field=command" >&2
        return 44
    fi
    if ! /usr/bin/python3 - "$command_json" "$EXPECTED_AEON_GPU_MEMORY_UTILIZATION" <<'PY'
import json
import sys


command_json, expected_utilization = sys.argv[1:]
try:
    command = json.loads(command_json)
except json.JSONDecodeError as error:
    print(f"AEON_COMMAND_MALFORMED error={error.msg}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(command, list) or not all(isinstance(token, str) for token in command):
    print("AEON_COMMAND_MALFORMED expected_json_string_array", file=sys.stderr)
    raise SystemExit(1)

if any(
    token == "--kv-cache-memory-bytes"
    or token.startswith("--kv-cache-memory-bytes=")
    for token in command
):
    print("AEON_KV_PINNED expected_auto_sizing", file=sys.stderr)
    raise SystemExit(1)

utilization_indices = [
    index
    for index, token in enumerate(command)
    if token == "--gpu-memory-utilization"
]
if len(utilization_indices) != 1:
    print(
        "AEON_GPU_UTILIZATION_MISMATCH "
        f"expected={expected_utilization} count={len(utilization_indices)}",
        file=sys.stderr,
    )
    raise SystemExit(1)

utilization_index = utilization_indices[0]
if utilization_index + 1 >= len(command):
    print(
        f"AEON_GPU_UTILIZATION_MISMATCH expected={expected_utilization} missing_value",
        file=sys.stderr,
    )
    raise SystemExit(1)
if command[utilization_index + 1] != expected_utilization:
    print(
        "AEON_GPU_UTILIZATION_MISMATCH "
        f"expected={expected_utilization} actual={command[utilization_index + 1]}",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
    then
        return 44
    fi
    if ! memory_limit="$(run_docker inspect "$AEON_CONTAINER" --format '{{.HostConfig.Memory}}')"; then
        echo "AEON_INSPECT_FAILED field=memory" >&2
        return 44
    fi
    if [[ ! "$memory_limit" =~ ^[0-9]+$ ]] \
        || (( 10#$memory_limit != EXPECTED_AEON_MEMORY_BYTES )); then
        echo "AEON_MEMORY_MISMATCH actual=$memory_limit expected=$EXPECTED_AEON_MEMORY_BYTES" >&2
        return 44
    fi
    if ! memory_swap_limit="$(run_docker inspect "$AEON_CONTAINER" --format '{{.HostConfig.MemorySwap}}')"; then
        echo "AEON_INSPECT_FAILED field=memory_swap" >&2
        return 44
    fi
    if [[ ! "$memory_swap_limit" =~ ^[0-9]+$ ]] \
        || (( 10#$memory_swap_limit != EXPECTED_AEON_MEMORY_BYTES )); then
        echo "AEON_MEMORY_SWAP_MISMATCH actual=$memory_swap_limit expected=$EXPECTED_AEON_MEMORY_BYTES" >&2
        return 44
    fi
}

run_systemctl is-active --quiet "$GUARD_UNIT"
require_memory_headroom

echo "PHASE verify_existing_aeon"
run_systemctl is-active --quiet "$AEON_UNIT"
wait_for_url "$AEON_URL/v1/models" "$AEON_READY_ATTEMPTS" AEON
verify_aeon_profile
require_memory_headroom

echo "PHASE switch_reranker"
MIGRATION_STARTED=1
run_systemctl stop "$FALLBACK_UNIT"
run_systemctl disable "$FALLBACK_UNIT"
run_systemctl stop "$RERANK_UNIT"
run_systemctl reset-failed "$FALLBACK_UNIT" || true
run_systemctl reset-failed "$RERANK_UNIT" || true
run_systemctl daemon-reload
run_systemctl_start start "$RERANK_UNIT"
verify_no_swap "$RERANK_UNIT" "$RERANK_CONTAINER"
verify_no_swap "$AEON_UNIT" "$AEON_CONTAINER"
verify_no_swap "$EMBEDDING_UNIT" "$EMBEDDING_CONTAINER"

wait_for_url "$RERANK_URL/v1/models" "$RERANK_READY_ATTEMPTS" RERANK
run_systemctl is-active --quiet "$RERANK_UNIT"
require_memory_headroom
echo "RERANK_READY"

RAW="$(curl -fsS --max-time 30 -H 'content-type: application/json' \
    -d '{"model":"qwen3-reranker-8b","query":"capital of France","documents":["Paris is the capital of France.","Bananas are yellow."],"top_n":2}' \
    "$RERANK_URL/v1/rerank")"
python3 -c 'import json,math,sys; d=json.loads(sys.argv[1]); r=d["results"]; assert r[0]["index"] == 0; assert all(math.isfinite(float(x["relevance_score"])) for x in r); print("RAW_RERANK_OK", r)' "$RAW"

GUARD="$(curl -fsS --max-time 30 -H 'content-type: application/json' \
    -d '{"model":"qwen3-reranker-8b","text_1":"capital of France","text_2":"Paris is the capital of France."}' \
    "$GUARD_SCORE_URL")"
python3 -c 'import json,math,sys; d=json.loads(sys.argv[1]); s=float(d["data"][0]["score"]); assert math.isfinite(s); print("GUARD_SCORE_OK", s)' "$GUARD"

run_systemctl enable "$RERANK_UNIT"
run_systemctl is-enabled --quiet "$RERANK_UNIT"
if run_systemctl is-enabled --quiet "$FALLBACK_UNIT"; then
    echo "fallback reranker unexpectedly enabled" >&2
    exit 48
fi
run_systemctl is-active --quiet \
    vllm-embedding.service "$AEON_UNIT" "$RERANK_UNIT" "$GUARD_UNIT"

DEPLOY_SUCCESS=1
MIGRATION_STARTED=0
trap - ERR INT TERM EXIT
echo "DEPLOY_SUCCESS"
