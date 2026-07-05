#!/usr/bin/env bash
# Rebuild/update llm-guard-proxy from reviewed main through mise while reusing
# a persistent Cargo target cache on GB10. Run on the GB10 host as obj.
set -Eeuo pipefail

CARGO_SPEC="${CARGO_SPEC:-cargo:https://github.com/RyderFreeman4Logos/llm-guard-proxy@branch:main}"
SERVICE_BIN="${SERVICE_BIN:-$HOME/.local/bin/llm-guard-proxy}"
CACHE_ROOT="${CACHE_ROOT:-$HOME/.cache/cargo-target/llm-guard-proxy-main}"
LOG_DIR="${LOG_DIR:-$HOME/log}"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_FILE:-$LOG_DIR/llm_guard_proxy_cached_rebuild_${TS}.log}"

export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"
export CARGO_TARGET_DIR="$CACHE_ROOT"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}"

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
run() { log "+ $*"; "$@"; }

mkdir -p "$CACHE_ROOT" "$LOG_DIR"
chmod 700 "$CACHE_ROOT"

{
  log "cached llm-guard-proxy mise rebuild starting"
  log "CARGO_SPEC=$CARGO_SPEC"
  log "CARGO_TARGET_DIR=$CARGO_TARGET_DIR"
  log "CARGO_BUILD_JOBS=$CARGO_BUILD_JOBS"
  run mise --version
  # -j1 limits mise-level concurrency; CARGO_BUILD_JOBS limits cargo/rustc jobs.
  run nice -n 10 ionice -c3 mise use -g -f -j1 "$CARGO_SPEC"
  MISE_BIN="$(mise which llm-guard-proxy)"
  case "$MISE_BIN" in
    *mise/installs/cargo-https-github-com-ryder-freeman4-logos-llm-guard-proxy/*/bin/llm-guard-proxy) ;;
    *) echo "unexpected mise binary path: $MISE_BIN" >&2; exit 1 ;;
  esac
  run test -x "$MISE_BIN"
  ln -sfn "$MISE_BIN" "${SERVICE_BIN}.tmp"
  mv -Tf "${SERVICE_BIN}.tmp" "$SERVICE_BIN"
  log "mise_bin=$MISE_BIN"
  log "service_bin_resolved=$(readlink -f "$SERVICE_BIN")"
  file "$MISE_BIN"
  sha256sum "$MISE_BIN" "$SERVICE_BIN"
  du -sh "$CACHE_ROOT" 2>/dev/null || true
  log "cached llm-guard-proxy mise rebuild complete"
} 2>&1 | tee "$LOG_FILE"

printf 'log=%s\n' "$LOG_FILE"
