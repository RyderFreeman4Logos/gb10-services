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
  if /usr/bin/timeout --signal=TERM --kill-after="$RETAIN_KILL_AFTER_SEC" \
    "$RETAIN_DEADLINE_SEC" /usr/bin/bash --noprofile --norc "$0" "$@"; then
    exit 0
  else
    rc=$?
    if [[ $rc -eq 124 || $rc -eq 137 ]]; then
      echo "gb10_retain_container_inspect: skip: retain timed out; keeping last inspect" >&2
      exit 0
    fi
    exit "$rc"
  fi
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
# Docker's --cidfile is 64 hex bytes; legacy publishers append one newline.
if len(raw) == 65 and raw[-1:] == b"\n":
    raw = raw[:-1]
cid = raw
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

/usr/bin/python3 -IS - "$cid" "$container" "$tmp" "$MAX_INSPECT_BYTES" <<'PY' || skip "inspect validation or durable archive failed; keeping last inspect"
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time

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
for key in ("StartedAt", "FinishedAt"):
    value = state.get(key)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z", value):
        raise SystemExit(1)

# Content-free receipt in the existing durable CID store, before cleanup.
# This caller is the retainer, NOT evidence identifying a SIGKILL initiator.
uid = os.getuid()
store = os.path.join(os.environ["HOME"], ".local", "state", "gb10-vllm-cids")
directory = os.open(os.environ["HOME"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for component in (".local", "state", "gb10-vllm-cids"):
        parent_info = os.fstat(directory)
        if parent_info.st_uid != uid or parent_info.st_mode & 0o022:
            raise SystemExit(1)
        try:
            os.mkdir(component, mode=0o700, dir_fd=directory)
        except FileExistsError:
            pass
        next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.fsync(directory)
        os.close(directory)
        directory = next_fd
    info = os.fstat(directory)
    if info.st_uid != uid or info.st_mode & 0o077:
        raise SystemExit(1)
    # Count and publish share a nonblocking lock; contention never delays cleanup.
    fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
    boot = open("/proc/sys/kernel/random/boot_id", encoding="ascii").read(64).strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot):
        raise SystemExit(1)
    pid = os.getpid()
    proc = open(f"/proc/{pid}/stat", encoding="ascii").read(4096)
    fields = proc[proc.rfind(")") + 2:].split()
    caller = {"pid": pid, "start_ticks": int(fields[19]), "ppid": os.getppid(), "uid": uid}
    scope = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/docker-{cid}.scope"
    events = {"status": "unavailable"}
    try:
        scope_fd = os.open(scope, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            scope_stat = os.fstat(scope_fd)
            event_fd = os.open("memory.events", flags, dir_fd=scope_fd)
            try:
                event_stat = os.fstat(event_fd)
                data = os.read(event_fd, 4097).decode("ascii")
                if not stat.S_ISREG(event_stat.st_mode) or len(data) > 4096:
                    raise ValueError("invalid events")
                counts = {}
                for line in data.splitlines():
                    key, value = line.split()
                    if key in counts or not re.fullmatch(r"[a-z_]+", key) or not value.isdecimal():
                        raise ValueError("invalid events")
                    counts[key] = int(value)
                if not {"oom", "oom_kill"} <= counts.keys():
                    raise ValueError("missing events")
                events = {"status": "captured", "path": scope, "device": scope_stat.st_dev,
                          "inode": scope_stat.st_ino, "counts": counts}
            finally:
                os.close(event_fd)
        finally:
            os.close(scope_fd)
    except (OSError, ValueError, UnicodeError):
        pass  # A vanished exited scope is unknown, never an inferred zero.
    safe_state = {key: state.get(key) for key in
                  ("Status", "Running", "ExitCode", "OOMKilled", "StartedAt", "FinishedAt")}
    receipt = {"schema": 1, "boot_id": boot, "container_id": cid, "container_name": name,
               "state": safe_state, "caller": caller, "memory_events": events,
               "realtime_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
               "inspect_sha256": hashlib.sha256(raw).hexdigest(), "kill_initiator": "UNKNOWN"}
    generation = hashlib.sha256(str(state.get("StartedAt")).encode()).hexdigest()[:16]
    leaf = f"{cid}.{generation}.exited.json"
    # ponytail: 64 receipts maximum; preserve old evidence and fail closed at capacity.
    entries = os.listdir(directory)
    if len([entry for entry in entries if entry.endswith(".exited.json")]) >= 64 and leaf not in entries:
        raise SystemExit(1)
    archive_tmp = ".inspect-" + os.urandom(16).hex()
    archive_fd = os.open(archive_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(archive_fd, "w", encoding="ascii") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Immutable per Docker StartedAt generation; duplicate stop hooks keep first evidence.
        try:
            os.link(archive_tmp, leaf, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            existing = os.open(leaf, flags, dir_fd=directory)
            try:
                existing_stat = os.fstat(existing)
                previous = json.loads(os.read(existing, 16385))
                if (not stat.S_ISREG(existing_stat.st_mode) or existing_stat.st_uid != uid
                        or existing_stat.st_mode & 0o077 or previous.get("container_id") != cid
                        or previous.get("state", {}).get("StartedAt") != state.get("StartedAt")):
                    raise SystemExit(1)
            finally:
                os.close(existing)
        os.fsync(directory)
    finally:
        os.unlink(archive_tmp, dir_fd=directory)
    os.fsync(directory)
    current = os.stat(store, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise SystemExit(1)
finally:
    os.close(directory)
PY

chmod 0600 "$tmp" || skip "chmod failed; keeping last inspect"
if [[ -L "$output" || -e "$output" ]]; then
  [[ -f "$output" && ! -L "$output" ]] || skip "output leaf is not a replaceable regular file"
  [[ "$(stat -c '%u' -- "$output")" == "$(id -u)" ]] || skip "output owner mismatch"
fi
mv -Tf -- "$tmp" "$output" || skip "publish failed; keeping last inspect"
trap - EXIT
echo "gb10_retain_container_inspect: retained cid=$cid container=$container"
