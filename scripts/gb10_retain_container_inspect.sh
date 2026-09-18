#!/usr/bin/env bash
# Keep last-good Docker inspect evidence. Empty, failed, or mismatched
# inspects must not replace a previous generation.
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
[[ -d "$parent" && ! -L "$parent" ]] || skip "inspect output directory is missing or unsafe"
[[ -f "$cidfile" && ! -L "$cidfile" ]] || skip "cidfile is missing; keeping last inspect"

mapfile -t cid_lines <"$cidfile" || skip "cidfile is unreadable"
[[ "${#cid_lines[@]}" == "1" && "${cid_lines[0]}" =~ ^[0-9a-f]{64}$ ]] || skip "cidfile is malformed"

cid="${cid_lines[0]}"
tmp="$(mktemp "${output}.tmp.XXXXXX")"
trap 'rm -f -- "$tmp"' EXIT
chmod 0600 "$tmp"

if ! docker inspect --type container "$cid" >"$tmp"; then
  skip "inspect failed for cid=$cid; keeping last inspect"
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
if not isinstance(identity, str) or not identity.startswith(cid):
    raise SystemExit(1)
reported = payload[0].get("Name")
if isinstance(reported, str) and reported.lstrip("/") != name:
    raise SystemExit(1)
state = payload[0].get("State")
if not isinstance(state, dict) or "ExitCode" not in state or "OOMKilled" not in state:
    raise SystemExit(1)
PY

chmod 0600 "$tmp"
mv -f -- "$tmp" "$output"
trap - EXIT
echo "gb10_retain_container_inspect: retained cid=$cid container=$container"
