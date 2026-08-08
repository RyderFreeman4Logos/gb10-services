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
GUARD_CONFIG="${LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG:-$HOME/.config/llm-guard-proxy/config.toml}"
GUARD_UNIT="${LLM_GUARD_PROXY_REBUILD_GUARD_UNIT:-$HOME/.config/systemd/user/llm-guard-proxy.service}"
PROC_ROOT="${LLM_GUARD_PROXY_REBUILD_PROC_ROOT:-/proc}"

export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"
export CARGO_TARGET_DIR="$CACHE_ROOT"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}"

require_tool() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'ERROR: required tool unavailable: %s\n' "$1" >&2
    exit 1
  }
}
for tool in cargo chmod date dirname git ionice ln mkdir mv nice readelf readlink rustc sha256sum stat systemctl tee; do
  require_tool "$tool"
done

TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_FILE:-$LOG_DIR/llm_guard_proxy_cached_rebuild_${TS}.log}"

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
run() { log "+ $*"; "$@"; }
die() { log "ERROR: $*"; exit 1; }
same() { [[ "$1" == "$2" ]] || die "$3"; }

sha256_of() {
  local line digest
  line="$(sha256sum -- "$1" 2>/dev/null)" || return 1
  digest="${line%% *}"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$digest"
}

sha256_text() {
  local line digest
  line="$(printf '%s' "$1" | sha256sum)" || return 1
  digest="${line%% *}"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$digest"
}

elf_build_id() {
  local notes line build_id='' count=0
  notes="$(readelf -n -- "$1" 2>/dev/null)" || return 1
  while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*Build[[:space:]]ID:[[:space:]]*([[:xdigit:]]+)[[:space:]]*$ ]]; then
      build_id="${BASH_REMATCH[1],,}"
      ((count += 1))
    fi
  done <<<"$notes"
  ((count == 1)) || return 1
  printf '%s\n' "$build_id"
}

valid_git_oid() { [[ "$1" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; }
device_inode() { stat -Lc '%d:%i' -- "$1" 2>/dev/null; }
main_pid() {
  local pid
  pid="$(systemctl --user show -p MainPID --value llm-guard-proxy.service 2>/dev/null)" || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s\n' "$pid"
}

mkdir -p "$CACHE_ROOT" "$LOG_DIR" "$(dirname "$SOURCE_DIR")"
chmod 700 "$CACHE_ROOT"

{
  log "cached llm-guard-proxy workspace rebuild starting"
  log "SOURCE_REPO=$SOURCE_REPO"
  log "SOURCE_BRANCH=$SOURCE_BRANCH"
  log "SOURCE_DIR=$SOURCE_DIR"
  log "CARGO_TARGET_DIR=$CARGO_TARGET_DIR"
  log "CARGO_BUILD_JOBS=$CARGO_BUILD_JOBS"

  CARGO_IDENTITY_INITIAL="$(cargo --version --verbose 2>/dev/null)" || die "cannot read Cargo toolchain identity"
  RUSTC_IDENTITY_INITIAL="$(rustc -vV 2>/dev/null)" || die "cannot read rustc toolchain identity"
  CARGO_IDENTITY_SHA_INITIAL="$(sha256_text "$CARGO_IDENTITY_INITIAL")" || die "cannot hash Cargo toolchain identity"
  RUSTC_IDENTITY_SHA_INITIAL="$(sha256_text "$RUSTC_IDENTITY_INITIAL")" || die "cannot hash rustc toolchain identity"
  GUARD_CONFIG_SHA_INITIAL="$(sha256_of "$GUARD_CONFIG")" || die "cannot hash Guard config"
  GUARD_UNIT_SHA_INITIAL="$(sha256_of "$GUARD_UNIT")" || die "cannot hash Guard systemd unit"

  if [[ ! -d "$SOURCE_DIR/.git" ]]; then
    run git clone --filter=blob:none "$SOURCE_REPO" "$SOURCE_DIR"
  fi
  run git -C "$SOURCE_DIR" fetch --prune origin "$SOURCE_BRANCH"
  run git -C "$SOURCE_DIR" checkout --detach "origin/$SOURCE_BRANCH"
  SOURCE_STATUS="$(git -C "$SOURCE_DIR" status --porcelain=v1 --untracked-files=all)" || die "cannot inspect source checkout"
  [[ -z "$SOURCE_STATUS" ]] || die "source checkout is not clean after checkout"
  SOURCE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)" || die "cannot read source commit"
  SOURCE_TREE="$(git -C "$SOURCE_DIR" rev-parse 'HEAD^{tree}')" || die "cannot read source tree"
  valid_git_oid "$SOURCE_COMMIT" || die "source commit identity is malformed"
  valid_git_oid "$SOURCE_TREE" || die "source tree identity is malformed"

  run nice -n 10 ionice -c3 cargo build --release -p llm-guard-proxy --features guard --manifest-path "$SOURCE_DIR/Cargo.toml"
  BUILD_BIN="$CARGO_TARGET_DIR/release/llm-guard-proxy"
  [[ -x "$BUILD_BIN" ]] || die "built Guard binary is missing or not executable"
  BUILT_BINARY_SHA_INITIAL="$(sha256_of "$BUILD_BIN")" || die "cannot hash built Guard binary"
  ELF_BUILD_ID_INITIAL="$(elf_build_id "$BUILD_BIN")" || die "ELF build ID unavailable or malformed for built Guard binary"

  ln -sfn "$BUILD_BIN" "${SERVICE_BIN}.tmp"
  mv -Tf "${SERVICE_BIN}.tmp" "$SERVICE_BIN"

  systemctl --user is-active --quiet llm-guard-proxy.service || die "llm-guard-proxy.service is not active"
  MAIN_PID_INITIAL="$(main_pid)" || die "llm-guard-proxy.service MainPID is unavailable or malformed"
  RUNNING_EXE_INITIAL="$(readlink -- "$PROC_ROOT/$MAIN_PID_INITIAL/exe" 2>/dev/null)" || die "running Guard executable link is unavailable"
  if [[ "$RUNNING_EXE_INITIAL" == *' (deleted)' ]]; then
    log "running guard is still on an unlinked inode; restarting llm-guard-proxy.service only"
    require_tool curl
    require_tool sleep
    run systemctl --user restart llm-guard-proxy.service
    sleep 2
    run systemctl --user is-active llm-guard-proxy.service
    log "+ curl -fsS -m 10 http://100.105.4.92:18009/health >/dev/null"
    curl -fsS -m 10 http://100.105.4.92:18009/health >/dev/null
  fi

  SOURCE_STATUS_FINAL="$(git -C "$SOURCE_DIR" status --porcelain=v1 --untracked-files=all)" || die "cannot re-inspect source checkout"
  [[ -z "$SOURCE_STATUS_FINAL" ]] || die "source checkout changed during rebuild"
  SOURCE_COMMIT_FINAL="$(git -C "$SOURCE_DIR" rev-parse HEAD)" || die "cannot re-read source commit"
  SOURCE_TREE_FINAL="$(git -C "$SOURCE_DIR" rev-parse 'HEAD^{tree}')" || die "cannot re-read source tree"
  valid_git_oid "$SOURCE_COMMIT_FINAL" || die "final source commit identity is malformed"
  valid_git_oid "$SOURCE_TREE_FINAL" || die "final source tree identity is malformed"

  CARGO_IDENTITY_FINAL="$(cargo --version --verbose 2>/dev/null)" || die "cannot re-read Cargo toolchain identity"
  RUSTC_IDENTITY_FINAL="$(rustc -vV 2>/dev/null)" || die "cannot re-read rustc toolchain identity"
  CARGO_IDENTITY_SHA_FINAL="$(sha256_text "$CARGO_IDENTITY_FINAL")" || die "cannot re-hash Cargo toolchain identity"
  RUSTC_IDENTITY_SHA_FINAL="$(sha256_text "$RUSTC_IDENTITY_FINAL")" || die "cannot re-hash rustc toolchain identity"
  GUARD_CONFIG_SHA_FINAL="$(sha256_of "$GUARD_CONFIG")" || die "cannot re-hash Guard config"
  GUARD_UNIT_SHA_FINAL="$(sha256_of "$GUARD_UNIT")" || die "cannot re-hash Guard systemd unit"

  [[ -L "$SERVICE_BIN" ]] || die "service binary is not a symlink"
  SERVICE_SYMLINK_TARGET="$(readlink -- "$SERVICE_BIN")" || die "service symlink target is unavailable"
  same "$SERVICE_SYMLINK_TARGET" "$BUILD_BIN" "service symlink target does not match built binary"
  BUILD_BIN_RESOLVED="$(readlink -f -- "$BUILD_BIN")" || die "built binary canonical path is unavailable"
  SERVICE_BIN_RESOLVED="$(readlink -f -- "$SERVICE_BIN")" || die "service binary canonical path is unavailable"
  same "$SERVICE_BIN_RESOLVED" "$BUILD_BIN_RESOLVED" "service symlink does not resolve to built binary"

  BUILT_BINARY_SHA_FINAL="$(sha256_of "$BUILD_BIN")" || die "cannot re-hash built Guard binary"
  SERVICE_BINARY_SHA="$(sha256_of "$SERVICE_BIN")" || die "cannot hash service Guard binary"
  ELF_BUILD_ID_FINAL="$(elf_build_id "$BUILD_BIN")" || die "final ELF build ID unavailable or malformed"
  BUILT_BINARY_DEVICE_INODE="$(device_inode "$BUILD_BIN")" || die "cannot stat built Guard binary"
  SERVICE_BINARY_DEVICE_INODE="$(device_inode "$SERVICE_BIN")" || die "cannot stat service Guard binary"

  MAIN_PID="$(main_pid)" || die "final llm-guard-proxy.service MainPID is unavailable or malformed"
  RUNNING_EXE="$(readlink -- "$PROC_ROOT/$MAIN_PID/exe" 2>/dev/null)" || die "final running Guard executable link is unavailable"
  [[ "$RUNNING_EXE" != *' (deleted)' ]] || die "running Guard executable is a deleted inode"
  RUNNING_BINARY_SHA="$(sha256_of "$PROC_ROOT/$MAIN_PID/exe")" || die "cannot hash running Guard executable"
  RUNNING_BINARY_DEVICE_INODE="$(device_inode "$PROC_ROOT/$MAIN_PID/exe")" || die "cannot stat running Guard executable"

  same "$SOURCE_COMMIT" "$SOURCE_COMMIT_FINAL" "source commit changed during rebuild"
  same "$SOURCE_TREE" "$SOURCE_TREE_FINAL" "source tree changed during rebuild"
  same "$CARGO_IDENTITY_SHA_INITIAL" "$CARGO_IDENTITY_SHA_FINAL" "Cargo toolchain identity changed during rebuild"
  same "$RUSTC_IDENTITY_SHA_INITIAL" "$RUSTC_IDENTITY_SHA_FINAL" "rustc toolchain identity changed during rebuild"
  same "$GUARD_CONFIG_SHA_INITIAL" "$GUARD_CONFIG_SHA_FINAL" "Guard config hash changed during rebuild"
  same "$GUARD_UNIT_SHA_INITIAL" "$GUARD_UNIT_SHA_FINAL" "Guard systemd unit hash changed during rebuild"
  same "$BUILT_BINARY_SHA_INITIAL" "$BUILT_BINARY_SHA_FINAL" "built binary hash changed after build"
  same "$BUILT_BINARY_SHA_FINAL" "$SERVICE_BINARY_SHA" "service binary SHA-256 does not match built binary"
  same "$BUILT_BINARY_SHA_FINAL" "$RUNNING_BINARY_SHA" "running executable SHA-256 does not match built binary"
  same "$ELF_BUILD_ID_INITIAL" "$ELF_BUILD_ID_FINAL" "built binary ELF build ID changed after build"
  same "$BUILT_BINARY_DEVICE_INODE" "$SERVICE_BINARY_DEVICE_INODE" "service binary inode does not match built binary"
  same "$BUILT_BINARY_DEVICE_INODE" "$RUNNING_BINARY_DEVICE_INODE" "running executable inode does not match built binary"

  MAIN_PID_CONFIRM="$(main_pid)" || die "cannot confirm llm-guard-proxy.service MainPID"
  same "$MAIN_PID" "$MAIN_PID_CONFIRM" "llm-guard-proxy.service MainPID changed during attestation"
  RUNNING_EXE_CONFIRM="$(readlink -- "$PROC_ROOT/$MAIN_PID_CONFIRM/exe" 2>/dev/null)" || die "cannot confirm running Guard executable link"
  same "$RUNNING_EXE" "$RUNNING_EXE_CONFIRM" "running Guard executable changed during attestation"
  SERVICE_SYMLINK_TARGET_CONFIRM="$(readlink -- "$SERVICE_BIN")" || die "cannot confirm service symlink target"
  same "$SERVICE_SYMLINK_TARGET" "$SERVICE_SYMLINK_TARGET_CONFIRM" "service symlink target changed during attestation"

  log "source_commit=$SOURCE_COMMIT_FINAL"
  log "source_tree=$SOURCE_TREE_FINAL"
  log "cargo_identity_sha256=$CARGO_IDENTITY_SHA_FINAL"
  log "rustc_identity_sha256=$RUSTC_IDENTITY_SHA_FINAL"
  log "elf_build_id=$ELF_BUILD_ID_FINAL"
  log "guard_config_sha256=$GUARD_CONFIG_SHA_FINAL"
  log "guard_unit_sha256=$GUARD_UNIT_SHA_FINAL"
  log "built_binary_sha256=$BUILT_BINARY_SHA_FINAL"
  log "service_binary_sha256=$SERVICE_BINARY_SHA"
  log "running_binary_sha256=$RUNNING_BINARY_SHA"
  log "built_binary_device_inode=$BUILT_BINARY_DEVICE_INODE"
  log "service_binary_device_inode=$SERVICE_BINARY_DEVICE_INODE"
  log "running_binary_device_inode=$RUNNING_BINARY_DEVICE_INODE"
  log "build_bin=$BUILD_BIN"
  log "service_symlink_target=$SERVICE_SYMLINK_TARGET"
  log "service_bin_resolved=$SERVICE_BIN_RESOLVED"
  log "running_guard_pid=$MAIN_PID"
  log "running_guard_exe=$RUNNING_EXE"
  du -sh "$CACHE_ROOT" 2>/dev/null || true
  log "cached llm-guard-proxy workspace rebuild complete"
} 2>&1 | tee "$LOG_FILE"

printf 'log=%s\n' "$LOG_FILE"
