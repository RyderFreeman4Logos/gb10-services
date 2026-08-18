#!/usr/bin/bash -p
# Hash-pin the reviewed rebuild engine; production starts from an empty environment.
set -euo pipefail
umask 077
case "$#:${1-}" in
  0:|1:--test-only) ;;
  *) printf 'usage: llm_guard_proxy_cached_rebuild.sh [--test-only]\n' >&2; exit 64 ;;
esac
script_dir="$(cd -P -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"; engine="$script_dir/llm_guard_proxy_cached_rebuild.py"
expected_engine_sha256="8dbdfefb304ffe80634bd4204a8f091d6902a9e55d85cb6533b4c3b7bf8ddf13"
if [[ -L "$engine" || ! -f "$engine" ]]; then
  printf 'Guard rebuild engine authority is unsafe\n' >&2
  exit 1
fi
exec {engine_fd}<"$engine"; read -r owner mode links size device inode < <(
  /usr/bin/stat -Lc '%u %a %h %s %d %i' -- "/proc/$$/fd/$engine_fd"
)
path_identity="$(/usr/bin/stat -Lc '%d %i %s' -- "$engine")"; if [[ "$owner" != "$EUID" || "$mode" != 644 || "$links" != 1 ||
      "$size" -gt 1048576 || "$path_identity" != "$device $inode $size" ]]; then
  printf 'Guard rebuild engine authority metadata differs\n' >&2
  exit 1
fi
engine_sha256="$(/usr/bin/sha256sum -- "/proc/$$/fd/$engine_fd")"; if [[ "${engine_sha256%% *}" != "$expected_engine_sha256" || -L "$engine" ||
      "$(/usr/bin/stat -Lc '%d %i %s' -- "$engine")" != "$path_identity" ]]; then
  printf 'Guard rebuild engine authority differs\n' >&2
  exit 1
fi
engine_fd_path="/proc/self/fd/$engine_fd"; if (( $# == 1 )); then
  exec /usr/bin/python3 -I -B -S "$engine_fd_path" --test-only
fi
if [[ -v LLM_GUARD_REBUILD_TEST_ONLY ]]; then
  printf 'LLM_GUARD_REBUILD_TEST_ONLY requires --test-only\n' >&2
  exit 64
fi
for name in CACHE_ROOT SOURCE_DIR SOURCE_REPO SOURCE_BRANCH SERVICE_BIN LOG_DIR LOG_FILE PATH \
  LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG LLM_GUARD_PROXY_REBUILD_GUARD_UNIT \
  LLM_GUARD_PROXY_REBUILD_PROC_ROOT LLM_GUARD_PROXY_REBUILD_RECEIPT_DIR \
  LLM_GUARD_REBUILD_TEST_CONFIG LLM_GUARD_REBUILD_TEST_MISSING_TOOL LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE LLM_GUARD_REBUILD_TEST_REPLACE_EXE_DURING_HASH LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS LLM_GUARD_REBUILD_TEST_CRASH_POINT LLM_GUARD_REBUILD_TEST_CRASH_MARKER XDG_RUNTIME_DIR HOME BASH_ENV ENV PYTHONPATH \
  PYTHONHOME PYTHONSTARTUP PYTHONINSPECT; do
  if [[ -v "$name" ]]; then
    printf 'production rebuild override %s requires --test-only\n' "$name" >&2
    exit 64
  fi
done
python_logical=/usr/bin/python3; python_resolved=/usr/bin/python3.11; python_sha256=6d972cf21be56fe3c947ab6ba257ff8d08c342dd2714442986791bd9a6dfabfe
if [[ "$(/usr/bin/readlink -e -- "$python_logical")" != "$python_resolved" ]]; then
  printf 'reviewed Python resolved path differs\n' >&2
  exit 1
fi
exec {python_fd}<"$python_resolved"
read -r python_owner python_mode python_links < <(
  /usr/bin/stat -Lc '%u %a %h' -- "/proc/$$/fd/$python_fd"
)
held_python_sha="$(/usr/bin/sha256sum -- "/proc/$$/fd/$python_fd")"
python_path_identity="$(/usr/bin/stat -Lc '%d %i %s' -- "$python_resolved")"
python_fd_identity="$(/usr/bin/stat -Lc '%d %i %s' -- "/proc/$$/fd/$python_fd")"
if [[ "$python_owner" != 0 || "$python_mode" != 755 || "$python_links" != 1 ||
      "${held_python_sha%% *}" != "$python_sha256" ||
      "$python_path_identity" != "$python_fd_identity" ]]; then
  printf 'reviewed Python object authority differs\n' >&2
  exit 1
fi
exec /usr/bin/env -i HOME=/home/obj PATH=/usr/bin:/bin LC_ALL=C LANG=C \
  "/proc/self/fd/$python_fd" -I -B -S "$engine_fd_path"
