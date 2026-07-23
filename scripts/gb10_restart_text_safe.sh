#!/usr/bin/env bash
# Safe restart of the AEON 27B text service without crashing embedding.
#
# Problem: on GB10's unified memory architecture, the 27B text service's
# torch.compile / cudagraph capture peak can exceed available headroom when
# both embedding and reranker are resident, causing the text container to be
# OOM-killed (exit 137) mid-start.  This script orchestrates the restart in
# the only order proven to work:
#
#   1. Stop text (if running)
#   2. Stop reranker (temporarily; embedding NEVER touched)
#   3. Start text and wait for /v1/models to respond
#   4. Start reranker back
#
# Embedding (:18012) is the reliability-critical service and is never stopped.
#
# Usage:
#   gb10_restart_text_safe.sh                # restart text + cycle rr
#   gb10_restart_text_safe.sh --start-only   # start text only (rr already down)
#   gb10_restart_text_safe.sh --rr-only      # start reranker only
#
# Exit codes:
#   0  success
#   1  text failed to start
#   2  reranker failed to start
#   3  text service not found / misconfigured
set -Eeuo pipefail

TEXT_UNIT="vllm-aeon-27b-dflash"
RR_UNIT="vllm-querit-4b-reranker"
EMB_UNIT="vllm-embedding"
TEXT_URL="http://100.105.4.92:18010/v1/models"
RR_URL="http://100.105.4.92:18013/v1/models"
TEXT_DEADLINE="${TEXT_START_DEADLINE:-2400}"
RR_DEADLINE="${RR_START_DEADLINE:-600}"
POLL_INTERVAL="${POLL_INTERVAL:-10}"
LIFECYCLE="${GB10_LIFECYCLE_BIN:-/home/obj/.local/bin/gb10_lifecycle.sh}"
LIFECYCLE_ACTOR="${GB10_LIFECYCLE_ACTOR:-gb10_restart_text_safe}"
LIFECYCLE_REASON="${GB10_LIFECYCLE_REASON:-authorized-text-maintenance}"

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

wait_for_url() {
  local url="$1" deadline="$2" name="$3"
  local elapsed=0
  while (( elapsed < deadline )); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
      log "$name ready (after ${elapsed}s)"
      return 0
    fi
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
  done
  log "TIMEOUT: $name not ready after ${deadline}s ($url)"
  return 1
}

stop_unit() {
  local unit="$1"
  if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
    log "Stopping $unit ..."
    "$LIFECYCLE" stop --unit "${unit}.service" \
      --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"
    # Kill any orphaned readiness scripts
    pkill -f "${unit}.*ready" 2>/dev/null || true
  else
    log "$unit already stopped"
  fi
}

start_unit() {
  local unit="$1"
  log "Starting $unit ..."
  "$LIFECYCLE" start --unit "${unit}.service" \
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
  # Step 2: stop reranker to free UMA peak headroom
  stop_unit "$RR_UNIT"
  sleep 3
fi

# Step 3: start text and wait
start_unit "$TEXT_UNIT"
if ! wait_for_url "$TEXT_URL" "$TEXT_DEADLINE" "text"; then
  log "FAILED: text did not become ready"
  exit 1
fi

if [[ "$MODE" == "full" ]]; then
  # Step 4: bring reranker back
  sleep 5
  start_unit "$RR_UNIT"
  if ! wait_for_url "$RR_URL" "$RR_DEADLINE" "reranker"; then
    log "WARNING: reranker did not become ready — text is up, reranker needs manual fix"
    exit 2
  fi
fi

log "Done. Final state:"
for s in "$EMB_UNIT" "$RR_UNIT" "$TEXT_UNIT"; do
  printf "  %s: " "$s"
  systemctl --user is-active "$s" 2>/dev/null || echo "inactive"
done
awk '/MemAvailable/{printf "  MemAvailable=%.1f GiB\n", $2/1048576}' /proc/meminfo
