#!/usr/bin/env bash
# Keep last-good Docker inspect evidence. Failed, hung, or mismatched
# inspects must not replace a previous generation or block cleanup.
set -euo pipefail
umask 077

RETAIN_DEADLINE_SEC=10
RETAIN_KILL_AFTER_SEC=2
MAX_INSPECT_BYTES=262144

if [[ "${GB10_RETAIN_UNDER_TIMEOUT:-}" != "1" ]]; then
  export GB10_RETAIN_UNDER_TIMEOUT=1
  /usr/bin/timeout --signal=TERM --kill-after="$RETAIN_KILL_AFTER_SEC" \
    "$RETAIN_DEADLINE_SEC" /usr/bin/bash --noprofile --norc "$0" "$@"
  rc=$?
  if [[ $rc -eq 124 || $rc -eq 137 ]]; then
    echo "gb10_retain_container_inspect: skip: retain timed out; keeping last inspect" >&2
    exit 0
  fi
  exit "$rc"
fi

usage() {
  echo "usage: gb10_retain_container_inspect.sh --container NAME --cidfile PATH --output PATH" >&2
  exit 2
}

skip() {
  echo "gb10_retain_container_inspect: skip: $*" >&2
  exit 0
}

container=""
cidfile=""
output=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)
      [[ $# -ge 2 ]] || usage
      container="$2"
      shift 2
      ;;
    --cidfile)
      [[ $# -ge 2 ]] || usage
      cidfile="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || usage
      output="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done
[[ -n "$container" && -n "$cidfile" && -n "$output" ]] || usage
[[ "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || skip "container name is unsafe"
[[ "$cidfile" == /* && "$output" == /* ]] || skip "cidfile and output must be absolute"
[[ "$cidfile" != *..* && "$output" != *..* ]] || skip "refusing path escape"

parent="${output%/*}"
[[ -d "$parent" ]] || skip "inspect output directory is missing or unsafe"

if ! cid="$(
  /usr/bin/python3 - "$cidfile" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
try:
    fd = os.open(path, flags)
except OSError:
    raise SystemExit(1)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(1)
    raw = os.read(fd, 66)
finally:
    os.close(fd)
if len(raw) != 65 or raw[-1:] != b"\n" or b"\0" in raw:
    raise SystemExit(1)
cid = raw[:-1]
if len(cid) != 64 or any(byte not in b"0123456789abcdef" for byte in cid):
    raise SystemExit(1)
sys.stdout.buffer.write(cid)
PY
)"; then
  skip "cidfile is missing or malformed; keeping last inspect"
fi

tmp="$(mktemp "${output}.tmp.XXXXXX")" || skip "mktemp failed; keeping last inspect"
cleanup() { rm -f -- "$tmp"; }
trap cleanup EXIT
chmod 0600 "$tmp" || skip "chmod failed; keeping last inspect"

# Bound Docker stdout. The helper already has a 10s outer deadline; this
# inner bound stops an inspect hang before Python starts.
limit=$((MAX_INSPECT_BYTES + 1))
set +e
set +o pipefail
/usr/bin/timeout --signal=TERM --kill-after=2 8 \
  docker inspect --type container "$cid" | /usr/bin/head -c "$limit" >"$tmp"
pipe_status=("${PIPESTATUS[@]}")
inspect_status="${pipe_status[0]}"
head_status="${pipe_status[1]}"
set -e
set -o pipefail
if [[ "$inspect_status" -ne 0 || "$head_status" -ne 0 ]]; then
  skip "inspect failed or timed out for cid=$cid; keeping last inspect"
fi
[[ -f "$tmp" && ! -L "$tmp" ]] || skip "inspect tempfile is not a regular file; keeping last inspect"

/usr/bin/python3 - "$cid" "$container" "$tmp" "$MAX_INSPECT_BYTES" <<'PY' || skip "inspect payload is empty, failed, oversized, or a different generation"
import json
import os
import stat
import sys

cid, name, path, max_text = sys.argv[1:]
max_bytes = int(max_text)
flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
try:
    fd = os.open(path, flags)
except OSError:
    raise SystemExit(1)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(1)
    raw = os.read(fd, max_bytes + 1)
finally:
    os.close(fd)
if len(raw) > max_bytes:
    raise SystemExit(1)
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit(1)
if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit(1)
if payload[0].get("Id") != cid:
    raise SystemExit(1)
if payload[0].get("Name") != "/" + name:
    raise SystemExit(1)
state = payload[0].get("State")
if not isinstance(state, dict):
    raise SystemExit(1)
if state.get("Status") != "exited":
    raise SystemExit(1)
if state.get("Running") is not False:
    raise SystemExit(1)
if type(state.get("ExitCode")) is not int:
    raise SystemExit(1)
if type(state.get("OOMKilled")) is not bool:
    raise SystemExit(1)
PY

chmod 0600 "$tmp" || skip "chmod failed; keeping last inspect"
if [[ -L "$output" || -e "$output" ]]; then
  [[ -f "$output" && ! -L "$output" ]] || skip "output leaf is not a replaceable regular file"
  [[ "$(stat -c '%u' -- "$output")" == "$(id -u)" ]] || skip "output owner mismatch"
fi
mv -Tf -- "$tmp" "$output" || skip "publish failed; keeping last inspect"
trap - EXIT
echo "gb10_retain_container_inspect: retained cid=$cid container=$container"
