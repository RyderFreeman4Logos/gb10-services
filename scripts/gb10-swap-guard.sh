#!/usr/bin/env bash
# OBSERVER_ONLY: report GB10 memory pressure and collect bounded read-only evidence.
# The Rust memory guardian is the sole automatic recovery actor.
set -uo pipefail

meminfo_path="${GB10_MEMINFO_PATH:-/proc/meminfo}"
log_path="${GB10_SWAP_GUARD_LOG:-$HOME/log/gb10-swap-guard.log}"
event_log="${GB10_SWAP_GUARD_TEST_EVENT_LOG:-}"
interval="${GB10_SWAP_GUARD_INTERVAL:-1}"
sample_log_interval="${GB10_SWAP_GUARD_SAMPLE_LOG_INTERVAL:-20}"
attribution_interval="${GB10_SWAP_ATTRIBUTION_INTERVAL:-300}"
alert_repeat_interval="${GB10_SWAP_GUARD_ALERT_REPEAT_INTERVAL:-60}"
oneshot="${GB10_SWAP_GUARD_ONESHOT:-0}"

positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

gib_to_kib() {
  local raw="$1" whole fraction scale
  [[ "$raw" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1
  whole="${raw%%.*}"
  if [[ "$raw" == *.* ]]; then
    fraction="${raw#*.}"
    scale=1
    for ((i = 0; i < ${#fraction}; i++)); do
      scale=$((scale * 10))
    done
    printf '%s\n' "$((whole * 1048576 + 10#$fraction * 1048576 / scale))"
  else
    printf '%s\n' "$((whole * 1048576))"
  fi
}

for value in "$interval" "$sample_log_interval" "$attribution_interval" "$alert_repeat_interval"; do
  if ! positive_integer "$value"; then
    echo "observer interval must be a positive integer: $value" >&2
    exit 2
  fi
done

warn_swap_kib="$(gib_to_kib "${GB10_SWAP_WARN_GIB:-7.5}")" || {
  echo "invalid GB10_SWAP_WARN_GIB" >&2
  exit 2
}
alert_swap_kib="$(gib_to_kib "${GB10_SWAP_ALERT_GIB:-12}")" || {
  echo "invalid GB10_SWAP_ALERT_GIB" >&2
  exit 2
}
alert_mem_kib="$(gib_to_kib "${GB10_MEM_AVAIL_ALERT_GIB:-1}")" || {
  echo "invalid GB10_MEM_AVAIL_ALERT_GIB" >&2
  exit 2
}

mkdir -p -- "${log_path%/*}" 2>/dev/null || true

emit_event() {
  [[ -n "$event_log" ]] || return 0
  printf '%s\n' "$*" >>"$event_log" 2>/dev/null || true
}

log_message() {
  local timestamp line
  printf -v timestamp '%(%Y-%m-%dT%H:%M:%S%z)T' -1
  line="$timestamp $*"
  printf '%s\n' "$line" >>"$log_path" 2>/dev/null || printf '%s\n' "$line" >&2
  emit_event "$*"
}

read_memory() {
  local key value _ mem_total= mem_available= swap_total= swap_free=
  while read -r key value _; do
    case "$key" in
      MemTotal:) mem_total="$value" ;;
      MemAvailable:) mem_available="$value" ;;
      SwapTotal:) swap_total="$value" ;;
      SwapFree:) swap_free="$value" ;;
    esac
  done <"$meminfo_path" || return 1
  [[ "$mem_total" =~ ^[0-9]+$ && "$mem_available" =~ ^[0-9]+$ ]] || return 1
  [[ "$swap_total" =~ ^[0-9]+$ && "$swap_free" =~ ^[0-9]+$ ]] || return 1
  MEM_TOTAL_KIB="$mem_total"
  MEM_AVAILABLE_KIB="$mem_available"
  SWAP_TOTAL_KIB="$swap_total"
  SWAP_FREE_KIB="$swap_free"
  SWAP_USED_KIB=$((swap_total - swap_free))
}

collect_evidence() {
  {
    printf '%s\n' '--- observer evidence begin ---'
    timeout --signal=TERM --kill-after=2 5 free -h || true
    timeout --signal=TERM --kill-after=2 5 swapon --show || true
    timeout --signal=TERM --kill-after=2 5 vmstat 1 2 || true
    timeout --signal=TERM --kill-after=2 5 docker ps --no-trunc || true
    printf '%s\n' '--- observer evidence end ---'
  } >>"$log_path" 2>&1 || true
}

diagnostic_pid=""
start_evidence_if_idle() {
  if [[ -n "$diagnostic_pid" ]] && kill -0 "$diagnostic_pid" 2>/dev/null; then
    return 0
  fi
  collect_evidence &
  diagnostic_pid=$!
}

last_sample=0
last_attribution=0
last_alert=0
alert_latched=0
log_message "OBSERVER_ONLY started warn_swap_kib=$warn_swap_kib alert_swap_kib=$alert_swap_kib alert_mem_kib=$alert_mem_kib"

while true; do
  now=$SECONDS
  if ! read_memory; then
    log_message "OBSERVER_READ_ERROR path=$meminfo_path"
  else
    reason=""
    if ((MEM_AVAILABLE_KIB < alert_mem_kib)); then
      reason="mem_available"
    fi
    if ((SWAP_USED_KIB > alert_swap_kib)); then
      reason="${reason:+${reason}+}swap"
    fi

    if [[ -n "$reason" ]]; then
      if ((alert_latched == 0 || now - last_alert >= alert_repeat_interval)); then
        log_message "ALERT_MEMORY_PRESSURE observer_only=1 reason=$reason mem_available_kib=$MEM_AVAILABLE_KIB swap_used_kib=$SWAP_USED_KIB"
        last_alert=$now
        start_evidence_if_idle
      fi
      alert_latched=1
    else
      if ((alert_latched == 1)); then
        log_message "MEMORY_PRESSURE_CLEARED observer_only=1 mem_available_kib=$MEM_AVAILABLE_KIB swap_used_kib=$SWAP_USED_KIB"
      fi
      alert_latched=0
    fi

    if ((SWAP_USED_KIB >= warn_swap_kib && now - last_sample >= sample_log_interval)); then
      log_message "SWAP_WARNING observer_only=1 swap_used_kib=$SWAP_USED_KIB"
    fi
    if ((now - last_sample >= sample_log_interval)); then
      log_message "SAMPLE mem_available_kib=$MEM_AVAILABLE_KIB swap_used_kib=$SWAP_USED_KIB swap_total_kib=$SWAP_TOTAL_KIB"
      last_sample=$now
    fi
    if ((now - last_attribution >= attribution_interval)); then
      start_evidence_if_idle
      last_attribution=$now
    fi
  fi

  if [[ "$oneshot" == "1" ]]; then
    if [[ -n "$diagnostic_pid" ]]; then
      wait "$diagnostic_pid" 2>/dev/null || true
    fi
    break
  fi
  sleep "$interval"
done
