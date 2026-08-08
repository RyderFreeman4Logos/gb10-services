#!/usr/bin/bash -p
# Hash-pin the reviewed rebuild engine; production starts from an empty environment.
set -euo pipefail
umask 077

if (( $# > 1 )) || (( $# == 1 )) && [[ "$1" != "--test-only" ]]; then
  printf 'usage: llm_guard_proxy_cached_rebuild.sh [--test-only]\n' >&2
  exit 64
fi
script_dir="$(cd -P -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
engine="$script_dir/llm_guard_proxy_cached_rebuild.py"
expected_engine_sha256="c31aa8e0e50e2114858b82e34560388cb17e83dd7c16cb7e78b6b6cd8fd9deb2"
if [[ -L "$engine" || ! -f "$engine" ]]; then
  printf 'Guard rebuild engine authority is unsafe\n' >&2
  exit 1
fi
exec {engine_fd}<"$engine"
read -r owner mode links size device inode < <(
  /usr/bin/stat -Lc '%u %a %h %s %d %i' -- "/proc/$$/fd/$engine_fd"
)
path_identity="$(/usr/bin/stat -Lc '%d %i %s' -- "$engine")"
if [[ "$owner" != "$EUID" || "$mode" != 644 || "$links" != 1 ||
      "$size" -gt 1048576 || "$path_identity" != "$device $inode $size" ]]; then
  printf 'Guard rebuild engine authority metadata differs\n' >&2
  exit 1
fi
engine_sha256="$(/usr/bin/sha256sum -- "/proc/$$/fd/$engine_fd")"
if [[ "${engine_sha256%% *}" != "$expected_engine_sha256" || -L "$engine" ||
      "$(/usr/bin/stat -Lc '%d %i %s' -- "$engine")" != "$path_identity" ]]; then
  printf 'Guard rebuild engine authority differs\n' >&2
  exit 1
fi
engine_fd_path="/proc/self/fd/$engine_fd"
if (( $# == 1 )); then
  exec /usr/bin/python3 -I -B -S "$engine_fd_path" --test-only
fi
if [[ -v LLM_GUARD_REBUILD_TEST_ONLY ]]; then
  printf 'LLM_GUARD_REBUILD_TEST_ONLY requires --test-only\n' >&2
  exit 64
fi
for name in CACHE_ROOT SOURCE_DIR SOURCE_REPO SOURCE_BRANCH SERVICE_BIN LOG_DIR LOG_FILE PATH \
  LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG LLM_GUARD_PROXY_REBUILD_GUARD_UNIT \
  LLM_GUARD_PROXY_REBUILD_PROC_ROOT LLM_GUARD_PROXY_REBUILD_RECEIPT_DIR \
  LLM_GUARD_REBUILD_TEST_CONFIG XDG_RUNTIME_DIR HOME BASH_ENV ENV PYTHONPATH \
  PYTHONHOME PYTHONSTARTUP PYTHONINSPECT; do
  if [[ -v "$name" ]]; then
    printf 'production rebuild override %s requires --test-only\n' "$name" >&2
    exit 64
  fi
done
exec /usr/bin/env -i HOME=/home/obj PATH=/usr/bin:/bin LC_ALL=C LANG=C \
  /usr/bin/python3 -I -B -S "$engine_fd_path"
