#!/usr/bin/env bash
# Rebuild/update llm-guard-proxy from reviewed main while reusing a persistent
# Cargo target cache on GB10. Run on the GB10 host as obj.
set -Eeuo pipefail

readonly PINNED_SOURCE_REPO="https://github.com/RyderFreeman4Logos/llm-guard-proxy"
MODE="default"
REQUESTED_SOURCE_SHA=""

if (( $# > 0 )); then
  if (( $# != 2 )) || [[ "$1" != "--install-pinned-deferred" ]]; then
    printf 'usage: %s [--install-pinned-deferred FULL_SOURCE_SHA]\n' "$0" >&2
    exit 64
  fi
  [[ "$2" =~ ^[0-9a-fA-F]{40}$ ]] || {
    printf 'FULL_SOURCE_SHA must be exactly 40 hexadecimal characters\n' >&2
    exit 64
  }
  MODE="pinned-deferred"
  REQUESTED_SOURCE_SHA="${2,,}"
fi

SOURCE_REPO="${SOURCE_REPO:-https://github.com/RyderFreeman4Logos/llm-guard-proxy}"
SOURCE_BRANCH="${SOURCE_BRANCH:-main}"
SOURCE_DIR="${SOURCE_DIR:-$HOME/.cache/source/llm-guard-proxy-main}"
SERVICE_BIN="${SERVICE_BIN:-$HOME/.local/bin/llm-guard-proxy}"
if [[ "$MODE" == "pinned-deferred" ]]; then
  SOURCE_REPO="$PINNED_SOURCE_REPO"
  DEFAULT_CACHE_ROOT="$HOME/.cache/cargo-target/llm-guard-proxy-$REQUESTED_SOURCE_SHA"
else
  DEFAULT_CACHE_ROOT="$HOME/.cache/cargo-target/llm-guard-proxy-main"
fi
CACHE_ROOT="${CACHE_ROOT:-$DEFAULT_CACHE_ROOT}"
LOG_DIR="${LOG_DIR:-$HOME/log}"
RECEIPT_DIR="$HOME/.local/state/llm-guard-proxy-rebuild"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_FILE:-$LOG_DIR/llm_guard_proxy_cached_rebuild_${TS}.log}"

export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"
export CARGO_TARGET_DIR="$CACHE_ROOT"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}"

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
run() { log "+ $*"; "$@"; }

mkdir -p "$CACHE_ROOT" "$LOG_DIR" "$(dirname "$SOURCE_DIR")"
chmod 700 "$CACHE_ROOT"
if [[ "$MODE" == "pinned-deferred" ]]; then
  mkdir -p "$RECEIPT_DIR"
  chmod 700 "$RECEIPT_DIR"
fi

{
  log "cached llm-guard-proxy workspace rebuild starting"
  log "mode=$MODE"
  log "SOURCE_REPO=$SOURCE_REPO"
  if [[ "$MODE" == "pinned-deferred" ]]; then
    log "requested_source_sha=$REQUESTED_SOURCE_SHA"
  else
    log "SOURCE_BRANCH=$SOURCE_BRANCH"
  fi
  log "SOURCE_DIR=$SOURCE_DIR"
  log "CARGO_TARGET_DIR=$CARGO_TARGET_DIR"
  log "CARGO_BUILD_JOBS=$CARGO_BUILD_JOBS"
  run cargo --version
  if [ ! -d "$SOURCE_DIR/.git" ]; then
    run git clone --filter=blob:none "$SOURCE_REPO" "$SOURCE_DIR"
  fi
  if [[ "$MODE" == "pinned-deferred" ]]; then
    run git -C "$SOURCE_DIR" fetch --no-tags "$SOURCE_REPO" "$REQUESTED_SOURCE_SHA"
    FETCHED_COMMIT="$(git -C "$SOURCE_DIR" rev-parse --verify 'FETCH_HEAD^{commit}')"
    [[ "$FETCHED_COMMIT" == "$REQUESTED_SOURCE_SHA" ]] || {
      log "fetched source does not match requested SHA"
      exit 65
    }
    run git -C "$SOURCE_DIR" checkout --detach "$FETCHED_COMMIT"
    SOURCE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse --verify 'HEAD^{commit}')"
    [[ "$SOURCE_COMMIT" == "$REQUESTED_SOURCE_SHA" ]] || {
      log "checked-out source does not match requested SHA"
      exit 65
    }
  else
    run git -C "$SOURCE_DIR" fetch --prune origin "$SOURCE_BRANCH"
    run git -C "$SOURCE_DIR" checkout --detach "origin/$SOURCE_BRANCH"
    SOURCE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
  fi
  log "source_commit=$SOURCE_COMMIT"
  run nice -n 10 ionice -c3 cargo build --release -p llm-guard-proxy --features guard --manifest-path "$SOURCE_DIR/Cargo.toml"
  BUILD_BIN="$CARGO_TARGET_DIR/release/llm-guard-proxy"
  run test -x "$BUILD_BIN"
  PREVIOUS_SERVICE_TARGET=""
  PREVIOUS_SERVICE_PRESENT=0
  if [[ "$MODE" == "pinned-deferred" ]]; then
    if [[ -L "$SERVICE_BIN" ]]; then
      PREVIOUS_SERVICE_TARGET="$(readlink "$SERVICE_BIN")"
      PREVIOUS_SERVICE_PRESENT=1
    elif [[ -e "$SERVICE_BIN" ]]; then
      log "deferred publication requires the managed runtime path to be a symlink"
      exit 65
    fi
  fi
  restore_previous_publication() {
    if (( PREVIOUS_SERVICE_PRESENT )); then
      ln -sfn "$PREVIOUS_SERVICE_TARGET" "${SERVICE_BIN}.tmp"
      mv -Tf "${SERVICE_BIN}.tmp" "$SERVICE_BIN"
    else
      rm -f "$SERVICE_BIN"
    fi
  }
  ln -sfn "$BUILD_BIN" "${SERVICE_BIN}.tmp"
  mv -Tf "${SERVICE_BIN}.tmp" "$SERVICE_BIN"
  log "build_bin=$BUILD_BIN"
  RUNTIME_RESOLVED="$(readlink -f "$SERVICE_BIN")"
  log "service_bin_resolved=$RUNTIME_RESOLVED"
  file "$BUILD_BIN"
  sha256sum "$BUILD_BIN" "$SERVICE_BIN"
  du -sh "$CACHE_ROOT" 2>/dev/null || true

  if [[ "$MODE" == "pinned-deferred" ]]; then
    read -r BUILD_SHA256 _ < <(sha256sum "$BUILD_BIN")
    read -r RUNTIME_SHA256 _ < <(sha256sum "$SERVICE_BIN")
    if [[ "$BUILD_SHA256" != "$RUNTIME_SHA256" ]]; then
      log "published runtime artifact does not match the candidate"
      restore_previous_publication
      exit 65
    fi
    RECEIPT_FILE="$RECEIPT_DIR/completion-${TS}-$$-${SOURCE_COMMIT}.json"
    if ! /usr/bin/python3 - "$RECEIPT_FILE" "$SOURCE_REPO" \
      "$REQUESTED_SOURCE_SHA" "$SOURCE_COMMIT" "$BUILD_BIN" "$BUILD_SHA256" \
      "$SERVICE_BIN" "$RUNTIME_RESOLVED" "$RUNTIME_SHA256" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    receipt_name,
    source_repository,
    requested_source_sha,
    verified_source_sha,
    candidate_path,
    candidate_sha256,
    runtime_path,
    runtime_resolved_path,
    runtime_sha256,
) = sys.argv[1:]
receipt = Path(receipt_name)
temporary = receipt.with_name(f"{receipt.name}.tmp.{os.getpid()}")
payload = {
    "schema_version": 1,
    "status": "completed",
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "source_repository": source_repository,
    "requested_source_sha": requested_source_sha,
    "verified_source_sha": verified_source_sha,
    "candidate_artifact": {"path": candidate_path, "sha256": candidate_sha256},
    "runtime_artifact": {
        "path": runtime_path,
        "resolved_path": runtime_resolved_path,
        "sha256": runtime_sha256,
    },
    "activation": "deferred",
    "activation_actions_performed": False,
    "guard_activation": "deferred",
    "vllm_activation": "not_requested",
}
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, receipt)
directory_fd = os.open(receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
    then
      log "completion receipt failed; restoring prior runtime publication"
      restore_previous_publication
      exit 65
    fi
    log "activation=deferred"
    log "completion_receipt=$RECEIPT_FILE"
  elif systemctl --user is-active --quiet llm-guard-proxy.service; then
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
