#!/usr/bin/env bash
# gb10_service_ready.sh — unified readiness check for GB10 model services.
#
# Polls /v1/models until ready, then sends a real inference request to verify
# the service is actually serving (not just port open). Prints elapsed time
# to journal so `systemctl --user status <service>` shows it.
#
# Usage:
#   gb10_service_ready.sh <kind> <url> <model> [--models-url URL] [--deadline SECS]
#
# kind:   chat | embedding | rerank
# url:    base URL (e.g. http://100.105.4.92:18010)
# model:  model name for the probe request
#
# Exit 0 = ready, exit 1 = timeout/failure.

set -euo pipefail

KIND="${1:?usage: gb10_service_ready.sh <chat|embedding|rerank> <url> <model> [--models-url URL] [--deadline SECS]}"
BASE_URL="${2:?missing url}"
MODEL="${3:?missing model}"; shift 3

MODELS_URL="$BASE_URL/v1/models"
DEADLINE=2200
PROBE_INTERVAL=5
PROBE_TIMEOUT=30

while [ $# -gt 0 ]; do
  case "$1" in
    --models-url)   MODELS_URL="$2"; shift 2;;
    --deadline)     DEADLINE="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; shift;;
  esac
done

START_EPOCH=$(date +%s)

log() {
  # systemd captures stdout into journal; `systemctl status` shows recent lines.
  local elapsed=$(( $(date +%s) - START_EPOCH ))
  echo "[gb10_ready +${elapsed}s] $*"
}

log "waiting for $KIND models at $MODELS_URL (deadline ${DEADLINE}s)..."

# Phase 1: poll /v1/models
MODELS_READY=false
while [ $(( $(date +%s) - START_EPOCH )) -lt "$DEADLINE" ]; do
  if curl -fsS --max-time "$PROBE_TIMEOUT" "$MODELS_URL" >/dev/null 2>&1; then
    MODELS_READY=true
    break
  fi
  sleep "$PROBE_INTERVAL"
done

if [ "$MODELS_READY" = false ]; then
  elapsed=$(( $(date +%s) - START_EPOCH ))
  log "TIMEOUT: $KIND models not ready after ${elapsed}s"
  exit 1
fi

log "$KIND models endpoint up"

# Phase 2: functional inference probe
PROBE_OK=false
while [ $(( $(date +%s) - START_EPOCH )) -lt "$DEADLINE" ]; do
  case "$KIND" in
    chat)
      resp=$(curl -fsS --max-time "$PROBE_TIMEOUT" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"OK\"}],\"max_tokens\":2,\"temperature\":0}" \
        "$BASE_URL/v1/chat/completions" 2>/dev/null || true)
      # Check we got a choices array with content
      if echo "$resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['choices'][0]['message']['content'], 'empty content'
" 2>/dev/null; then
        PROBE_OK=true
        break
      fi
      ;;
    embedding)
      resp=$(curl -fsS --max-time "$PROBE_TIMEOUT" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"$MODEL\",\"input\":\"health check\"}" \
        "$BASE_URL/v1/embeddings" 2>/dev/null || true)
      if echo "$resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert len(d['data'][0]['embedding']) > 0, 'empty embedding'
" 2>/dev/null; then
        PROBE_OK=true
        break
      fi
      ;;
    rerank)
      resp=$(curl -fsS --max-time "$PROBE_TIMEOUT" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"$MODEL\",\"query\":\"health\",\"documents\":[\"ok\",\"not ok\"]}" \
        "$BASE_URL/v1/rerank" 2>/dev/null || true)
      if echo "$resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert len(d['results']) >= 2, 'missing results'
" 2>/dev/null; then
        PROBE_OK=true
        break
      fi
      ;;
    *)
      echo "unknown kind: $KIND" >&2
      exit 1
      ;;
  esac
  sleep "$PROBE_INTERVAL"
done

if [ "$PROBE_OK" = false ]; then
  elapsed=$(( $(date +%s) - START_EPOCH ))
  log "TIMEOUT: $KIND inference probe failed after ${elapsed}s"
  exit 1
fi

TOTAL=$(( $(date +%s) - START_EPOCH ))
log "SERVICE_READY elapsed=${TOTAL}s kind=$KIND model=$MODEL"
exit 0
