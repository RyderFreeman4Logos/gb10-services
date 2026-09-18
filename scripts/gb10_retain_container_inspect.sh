#!/usr/bin/env bash
# Keep last-good Docker inspect evidence. Failed, hung, or mismatched
# inspects must not replace a previous generation or block cleanup.
set -euo pipefail
umask 077

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
[[ -f "$cidfile" && ! -L "$cidfile" ]] || skip "cidfile is missing; keeping last inspect"

mapfile -t cid_lines <"$cidfile" || skip "cidfile is unreadable"
[[ "${#cid_lines[@]}" == "1" && "${cid_lines[0]}" =~ ^[0-9a-f]{64}$ ]] || skip "cidfile is malformed"

cid="${cid_lines[0]}"
tmp="$(mktemp "${output}.tmp.XXXXXX")" || skip "mktemp failed; keeping last inspect"
trap 'rm -f -- "$tmp"' EXIT
chmod 0600 "$tmp" || skip "chmod failed; keeping last inspect"

# Hard bound well below TimeoutStopSec=60 so a hung inspect cannot skip cleanup.
if ! /usr/bin/timeout --signal=TERM --kill-after=2 8 \
  docker inspect --type container "$cid" >"$tmp"; then
  skip "inspect failed or timed out for cid=$cid; keeping last inspect"
fi

/usr/bin/python3 - "$cid" "$container" "$tmp" <<'PY' || skip "inspect payload is empty, failed, or a different generation"
import json
import sys

cid, name, path = sys.argv[1:]
try:
    with open(path, "rb") as handle:
        raw = handle.read()
    payload = json.loads(raw)
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit(1)
identity = payload[0].get("Id")
if identity != cid:
    raise SystemExit(1)
if payload[0].get("Name") != "/" + name:
    raise SystemExit(1)
state = payload[0].get("State")
if not isinstance(state, dict):
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
