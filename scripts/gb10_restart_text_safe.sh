#!/usr/bin/env bash
# Safe restart of the AEON 27B text service without crashing embedding.
#
# Problem: on GB10's unified memory architecture, the 27B text service's
# concurrent model startup can exceed available headroom.  This script starts
# the fixed-KV reranker first so text AUTO-sizes from the remaining memory:
#
#   1. Stop/cancel text and verify it is inactive
#   2. Start reranker and wait for /v1/models to respond
#   3. Start text and wait for /v1/models to respond
#
# Embedding (:18012) is the reliability-critical service and is never stopped.
#
# Usage:
#   gb10_restart_text_safe.sh                # ensure rr ready, then restart text
#   gb10_restart_text_safe.sh --start-only   # start text only (rr already ready)
#   gb10_restart_text_safe.sh --rr-only      # start reranker only
#
# Exit codes:
#   0  success
#   1  text failed to start
#   2  reranker failed to start
#   3  text service not found / misconfigured
set -Eeuo pipefail

RR_UNIT="vllm-querit-4b-reranker"
EMB_UNIT="vllm-embedding"
TEXT_URL="http://100.105.4.92:18010/v1/models"
RR_URL="http://100.105.4.92:18013/v1/models"
TEXT_DEADLINE="${TEXT_START_DEADLINE:-2800}"
RR_DEADLINE=1800
POLL_INTERVAL="${POLL_INTERVAL:-10}"
LIFECYCLE="${GB10_LIFECYCLE_BIN:-/home/obj/.local/bin/gb10_lifecycle.sh}"
LIFECYCLE_ACTOR="${GB10_LIFECYCLE_ACTOR:-gb10_restart_text_safe}"
LIFECYCLE_REASON="${GB10_LIFECYCLE_REASON:-authorized-text-maintenance}"
readonly TEXT_UNIT="vllm-aeon-27b-dflash"

MODE="full"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start-only) MODE="text-only" ;;
    --rr-only)    MODE="rr-only" ;;
    --help|-h)
      echo "Usage: $0 [--start-only|--rr-only]"
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 3 ;;
  esac
  shift
done

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

monotonic_ticks() {
  awk '{printf "%.0f\n", $1 * 100}' /proc/uptime
}

wait_for_url() {
  local url="$1" deadline="$2" name="$3"
  local start_ticks deadline_ticks now_ticks remaining_ticks curl_ticks sleep_ticks completed_ticks
  start_ticks="$(monotonic_ticks)"
  deadline_ticks=$((start_ticks + deadline * 100))
  while :; do
    now_ticks="$(monotonic_ticks)"
    remaining_ticks=$((deadline_ticks - now_ticks))
    (( remaining_ticks > 0 )) || break
    curl_ticks=$((remaining_ticks < 500 ? remaining_ticks : 500))
    if curl -fsS --max-time "$(awk -v ticks="$curl_ticks" 'BEGIN {printf "%.2f", ticks / 100}')" "$url" >/dev/null 2>&1; then
      completed_ticks="$(monotonic_ticks)"
      if (( completed_ticks <= deadline_ticks )); then
        log "$name ready (after $(((completed_ticks - start_ticks) / 100))s)"
        return 0
      fi
      break
    fi
    now_ticks="$(monotonic_ticks)"
    remaining_ticks=$((deadline_ticks - now_ticks))
    (( remaining_ticks > 0 )) || break
    sleep_ticks=$((remaining_ticks <= POLL_INTERVAL * 100 ? remaining_ticks - 1 : POLL_INTERVAL * 100))
    (( sleep_ticks > 0 )) && sleep "$(awk -v ticks="$sleep_ticks" 'BEGIN {printf "%.2f", ticks / 100}')"
  done
  log "TIMEOUT: $name not ready after ${deadline}s ($url)"
  return 1
}

stop_unit() {
  local unit="$1" state deadline
  log "Stopping $unit ..."
  "$LIFECYCLE" stop --unit "${unit}.service" \
    --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"
  # Kill any orphaned readiness scripts.
  pkill -f "${unit}.*ready" 2>/dev/null || true
  deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    state="$(systemctl --user show --property=ActiveState --value "$unit" 2>/dev/null || true)"
    case "$state" in
      inactive|failed)
        log "$unit stopped"
        return 0
        ;;
    esac
    sleep "$POLL_INTERVAL"
  done
  log "TIMEOUT: $unit did not stop (ActiveState=${state:-unknown})"
  return 1
}

start_unit() {
  local unit="$1"
  log "Starting $unit ..."
  "$LIFECYCLE" start --reset-failed --unit "${unit}.service" \
    --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"
}

# ------------------------------------------------------------------- rr-only --
if [[ "$MODE" == "rr-only" ]]; then
  start_unit "$RR_UNIT"
  if wait_for_url "$RR_URL" "$RR_DEADLINE" "reranker"; then exit 0; else exit 2; fi
fi

# ---------------------------------------------------------- text-only / full --

# Verify embedding is running; if not, warn but do NOT auto-start (caller's job).
if ! systemctl --user is-active --quiet "$EMB_UNIT" 2>/dev/null; then
  log "WARNING: $EMB_UNIT is not active — it should be started separately first!"
fi

# Step 1: stop text
stop_unit "$TEXT_UNIT"

if [[ "$MODE" == "full" ]]; then
  # Step 2: establish fixed-KV reranker readiness before text AUTO-sizing.
  start_unit "$RR_UNIT"
  if ! wait_for_url "$RR_URL" "$RR_DEADLINE" "reranker"; then
    log "FAILED: reranker did not become ready"
    exit 2
  fi
fi

# Step 3: start text and wait
start_unit "$TEXT_UNIT"
if ! wait_for_url "$TEXT_URL" "$TEXT_DEADLINE" "text"; then
  log "FAILED: text did not become ready"
  exit 1
fi

log "Done. Final state:"
for s in "$EMB_UNIT" "$RR_UNIT" "$TEXT_UNIT"; do
  printf "  %s: " "$s"
  systemctl --user is-active "$s" 2>/dev/null || echo "inactive"
done
awk '/MemAvailable/{printf "  MemAvailable=%.1f GiB\n", $2/1048576}' /proc/meminfo
