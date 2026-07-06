#!/usr/bin/env bash
# Health check for AEON DFlash vLLM service.
# Detects CUDA kernel hang: running > 0 + 0 tok/s + GPU power < 20W.
# Intended to run as a systemd timer every 2 minutes.

set -euo pipefail

SERVICE="vllm-aeon-27b-dflash.service"
# Scrape the raw AEON vLLM backend. The guard proxy exposes its own metrics at
# 18009 and does not forward vLLM's vllm:* counters.
METRICS_URL="http://100.105.4.92:18010/metrics"
LOCKFILE="/tmp/aeon-healthcheck.lock"
COOLDOWN_FILE="/tmp/aeon-healthcheck-last-restart"
COOLDOWN_SECONDS=600  # don't restart more than once per 10 minutes
SAMPLE_INTERVAL=600    # seconds between two token counter samples

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

exec 200>"$LOCKFILE"
flock -n 200 || { log "SKIP: another healthcheck is running"; exit 0; }

if ! systemctl --user is-active --quiet "$SERVICE"; then
    log "SKIP: service not active"
    exit 0
fi

if [[ -f "$COOLDOWN_FILE" ]]; then
    last_restart=$(cat "$COOLDOWN_FILE")
    now=$(date +%s)
    elapsed=$(( now - last_restart ))
    if (( elapsed < COOLDOWN_SECONDS )); then
        log "SKIP: cooldown (${elapsed}s since last restart, need ${COOLDOWN_SECONDS}s)"
        exit 0
    fi
fi

metrics1=$(curl -sf --max-time 5 "$METRICS_URL" 2>/dev/null) || {
    log "WARN: metrics endpoint unreachable (may be starting up)"
    exit 0
}

running=$(awk '/^vllm:num_requests_running\{/ {print int($2); exit}' <<<"$metrics1")
tokens1=$(awk '/^vllm:generation_tokens_total\{/ {print int($2); exit}' <<<"$metrics1")

if [[ -z "$running" || -z "$tokens1" ]]; then
    log "WARN: could not parse metrics"
    exit 0
fi

if (( running == 0 )); then
    log "OK: idle (0 running requests)"
    exit 0
fi

sleep "$SAMPLE_INTERVAL"

tokens2=$(curl -sf --max-time 5 "$METRICS_URL" 2>/dev/null \
    | awk '/^vllm:generation_tokens_total\{/ {print int($2); exit}') || {
    log "WARN: second metrics fetch failed"
    exit 0
}

delta=$(( tokens2 - tokens1 ))

if (( delta > 0 )); then
    log "OK: generating tokens (${delta} in ${SAMPLE_INTERVAL}s, ${running} running)"
    exit 0
fi

# 0 tokens generated with running requests — check GPU power to confirm hang
gpu_power=$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>/dev/null | head -1 | cut -d. -f1)

if [[ -z "$gpu_power" ]]; then
    log "WARN: cannot read GPU power"
    exit 0
fi

if (( gpu_power > 1 )); then
    # High power = likely doing prefill on a very long context, not a hang
    log "OK: 0 tokens but GPU power ${gpu_power}W (likely prefill, ${running} running)"
    exit 0
fi

# Confirmed hang: running > 0, 0 tok/s, low power
log "HANG DETECTED: ${running} running, 0 tok/s over ${SAMPLE_INTERVAL}s, GPU ${gpu_power}W"
log "Restarting ${SERVICE}..."

systemctl --user restart "$SERVICE"
date +%s > "$COOLDOWN_FILE"

log "Service restarted. Cooldown ${COOLDOWN_SECONDS}s before next possible restart."
