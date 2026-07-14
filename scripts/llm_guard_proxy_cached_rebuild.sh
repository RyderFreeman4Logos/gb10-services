#!/usr/bin/env bash
# Rebuild/update llm-guard-proxy from reviewed main while reusing a persistent
# Cargo target cache on GB10. Run on the GB10 host as obj.
set -Eeuo pipefail

SOURCE_REPO="${SOURCE_REPO:-https://github.com/RyderFreeman4Logos/llm-guard-proxy}"
SOURCE_BRANCH="${SOURCE_BRANCH:-main}"
SOURCE_DIR="${SOURCE_DIR:-$HOME/.cache/source/llm-guard-proxy-main}"
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

mkdir -p "$CACHE_ROOT" "$LOG_DIR" "$(dirname "$SOURCE_DIR")"
chmod 700 "$CACHE_ROOT"

{
  log "cached llm-guard-proxy workspace rebuild starting"
  log "SOURCE_REPO=$SOURCE_REPO"
  log "SOURCE_BRANCH=$SOURCE_BRANCH"
  log "SOURCE_DIR=$SOURCE_DIR"
  log "CARGO_TARGET_DIR=$CARGO_TARGET_DIR"
  log "CARGO_BUILD_JOBS=$CARGO_BUILD_JOBS"
  run cargo --version
  if [ ! -d "$SOURCE_DIR/.git" ]; then
    run git clone --filter=blob:none "$SOURCE_REPO" "$SOURCE_DIR"
  fi
  run git -C "$SOURCE_DIR" fetch --prune origin "$SOURCE_BRANCH"
  run git -C "$SOURCE_DIR" checkout --detach "origin/$SOURCE_BRANCH"
  SOURCE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
  log "source_commit=$SOURCE_COMMIT"
  run nice -n 10 ionice -c3 cargo build --release -p llm-guard-proxy --features guard --manifest-path "$SOURCE_DIR/Cargo.toml"
  BUILD_BIN="$CARGO_TARGET_DIR/release/llm-guard-proxy"
  run test -x "$BUILD_BIN"
  ln -sfn "$BUILD_BIN" "${SERVICE_BIN}.tmp"
  mv -Tf "${SERVICE_BIN}.tmp" "$SERVICE_BIN"
  log "build_bin=$BUILD_BIN"
  log "service_bin_resolved=$(readlink -f "$SERVICE_BIN")"
  file "$BUILD_BIN"
  sha256sum "$BUILD_BIN" "$SERVICE_BIN"
  du -sh "$CACHE_ROOT" 2>/dev/null || true

  if systemctl --user is-active --quiet llm-guard-proxy.service; then
    MAIN_PID="$(systemctl --user show -p MainPID --value llm-guard-proxy.service)"
    RUNNING_EXE="$(readlink "/proc/$MAIN_PID/exe" 2>/dev/null || true)"
    log "running_guard_pid=$MAIN_PID"
    log "running_guard_exe=$RUNNING_EXE"
    if printf '%s\n' "$RUNNING_EXE" | grep -q ' (deleted)$'; then
      log "running guard is still on an unlinked inode; restarting llm-guard-proxy.service only"
      run systemctl --user restart llm-guard-proxy.service
      sleep 2
      run systemctl --user is-active llm-guard-proxy.service
      curl -fsS -m 10 http://100.105.4.92:18009/health >/dev/null
      NEW_PID="$(systemctl --user show -p MainPID --value llm-guard-proxy.service)"
      log "restarted_guard_pid=$NEW_PID"
      log "restarted_guard_exe=$(readlink "/proc/$NEW_PID/exe")"
    fi
  fi

  log "cached llm-guard-proxy workspace rebuild complete"
} 2>&1 | tee "$LOG_FILE"

printf 'log=%s\n' "$LOG_FILE"
