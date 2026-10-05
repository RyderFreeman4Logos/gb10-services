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
  echo "usage: gb10_retain_container_inspect.sh [--snapshot-live] --container NAME --cidfile PATH --output PATH" >&2
  exit 2
}

skip() {
  echo "gb10_retain_container_inspect: skip: $*" >&2
  exit 0
}

container=""
cidfile=""
output=""
export GB10_RETAIN_SNAPSHOT_LIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot-live)
      export GB10_RETAIN_SNAPSHOT_LIVE=1
      shift
      ;;
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
import subprocess
import tempfile

cid, name, path, max_text = sys.argv[1:]
live = os.environ.get("GB10_RETAIN_SNAPSHOT_LIVE") == "1"
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
if state.get("Status") != ("running" if live else "exited"):
    raise SystemExit(1)
if state.get("Running") is not live:
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
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
        boot = stream.read(64).strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot):
        raise SystemExit(1)
    pid = os.getpid()
    with open(f"/proc/{pid}/stat", encoding="ascii") as stream:
        proc = stream.read(4096)
    fields = proc[proc.rfind(")") + 2:].split()
    caller = {"pid": pid, "start_ticks": int(fields[19]), "ppid": os.getppid(), "uid": uid}
    scope = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/docker-{cid}.scope"
    generation = hashlib.sha256(str(state.get("StartedAt")).encode()).hexdigest()[:16]
    memory_leaf = f"{cid}.{generation}.memory.json"
    def read_file(file, limit):
        fd = os.open(file, flags)
        try:
            meta = os.fstat(fd)
            data = os.read(fd, limit + 1)
            if not stat.S_ISREG(meta.st_mode) or len(data) > limit:
                raise ValueError("invalid file")
            return data.decode("ascii")
        finally:
            os.close(fd)
    def owner_identity():
        owner_pid = state.get("Pid")
        if type(owner_pid) is not int or owner_pid <= 1:
            raise ValueError("invalid owner PID")
        proc = read_file(f"/proc/{owner_pid}/stat", 4096)
        fields = proc[proc.rfind(")") + 2:].split()
        if not proc.startswith(f"{owner_pid} (") or len(fields) < 20 or not fields[19].isdecimal() or int(fields[19]) <= 0:
            raise ValueError("invalid owner stat")
        if read_file(f"/proc/{owner_pid}/cgroup", 4096) != "0::" + scope.removeprefix("/sys/fs/cgroup") + "\n":
            raise ValueError("owner scope mismatch")
        return {"pid": owner_pid, "start_ticks": int(fields[19])}
    def parse_events(data):
        if not data.endswith("\n") or "\r" in data:
            raise ValueError("unterminated events")
        counts = {}
        for line in data.splitlines():
            key, value = line.split()
            if key in counts or not re.fullmatch(r"[a-z_]+", key) or not re.fullmatch(r"[0-9]{1,20}", value):
                raise ValueError("invalid events")
            counts[key] = int(value)
        if not {"oom", "oom_kill"} <= counts.keys():
            raise ValueError("missing events")
        return counts
    def inspect_fence():
        # Fixed projection excludes Docker environment/argv; disk-backed output
        # is bounded on read and covered by the helper's existing outer deadline.
        with tempfile.TemporaryFile() as stream:
            result = subprocess.run(["/usr/bin/timeout", "--signal=TERM", "--kill-after=1", "2",
                                     "docker", "inspect", "--type", "container", "--format",
                                     "{{.Id}} {{.Name}} {{.State.Running}} {{.State.Pid}} {{.State.StartedAt}}", cid],
                                    stdout=stream, stderr=subprocess.DEVNULL, check=False)
            stream.seek(0)
            expected = f"{cid} /{name} {'true' if live else 'false'} {state.get('Pid', 0)} {state.get('StartedAt')}\n"
            return result.returncode == 0 and stream.read(1025) == expected.encode("ascii")
    owner = owner_identity() if live else None
    sampled_ns = time.time_ns()
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
                counts = parse_events(data)
                events = {"status": "captured", "path": scope, "device": scope_stat.st_dev,
                          "inode": scope_stat.st_ino, "counts": counts, "raw": data,
                          "sampled_realtime_ns": sampled_ns, "generation_fenced": False,
                          "terminal_status": "unfenced"}
            finally:
                os.close(event_fd)
        finally:
            os.close(scope_fd)
    except (OSError, ValueError, UnicodeError):
        pass  # A vanished exited scope is unknown, never an inferred zero.
    saved = None
    try:
        fd = os.open(memory_leaf, flags, dir_fd=directory)
        try:
            meta = os.fstat(fd)
            data = os.read(fd, 16385)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != uid or meta.st_mode & 0o077
                    or meta.st_nlink != 1 or len(data) > 16384):
                raise ValueError("unsafe snapshot")
            candidate = json.loads(data)
            prior = candidate.get("memory_events", {})
            prior_owner = candidate.get("owner", {})
            if (candidate.get("schema") != 1 or candidate.get("container_id") != cid
                    or candidate.get("started_at") != state.get("StartedAt")
                    or candidate.get("container_name") != name or candidate.get("boot_id") != boot
                    or prior.get("path") != scope or prior.get("status") != "captured"
                    or prior.get("generation_fenced") is not True or prior.get("terminal_status") != "not-observed"
                    or type(prior.get("device")) is not int or type(prior.get("inode")) is not int
                    or prior["device"] < 0 or prior["inode"] <= 0
                    or type(prior_owner.get("pid")) is not int or prior_owner["pid"] <= 1
                    or type(prior_owner.get("start_ticks")) is not int or prior_owner["start_ticks"] <= 0
                    or type(prior.get("sampled_realtime_ns")) is not int
                    or not 0 < prior["sampled_realtime_ns"] <= sampled_ns
                    or not isinstance(prior.get("raw"), str) or len(prior["raw"]) > 4096
                    or not isinstance(prior.get("counts"), dict)
                    or any(type(value) is not int for value in prior["counts"].values())
                    or parse_events(prior["raw"]) != prior.get("counts")):
                raise ValueError("snapshot generation mismatch")
            saved = candidate
        finally:
            os.close(fd)
    except (OSError, ValueError, UnicodeError, AttributeError, TypeError):
        pass
    if live:
        if events["status"] != "captured" or owner_identity() != owner or not inspect_fence() or owner_identity() != owner:
            raise SystemExit(1)
        current_fd = os.open(scope, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            current = os.fstat(current_fd)
        finally:
            os.close(current_fd)
        if (current.st_dev, current.st_ino) != (events["device"], events["inode"]):
            raise SystemExit(1)
        events["generation_fenced"] = True
        events["terminal_status"] = "not-observed"
        if saved is not None and (saved["owner"] != owner or
                (saved["memory_events"]["device"], saved["memory_events"]["inode"]) != (current.st_dev, current.st_ino)):
            raise SystemExit(1)
    elif saved is not None:
        if events["status"] == "captured":
            prior = saved["memory_events"]
            current_identity = None
            try:
                current_fd = os.open(scope, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    current = os.fstat(current_fd)
                    current_identity = (current.st_dev, current.st_ino)
                finally:
                    os.close(current_fd)
            except OSError:
                pass
            if (events["device"], events["inode"]) == (prior["device"], prior["inode"]) == current_identity and inspect_fence():
                events["generation_fenced"] = True
                events["terminal_status"] = "captured"
            else:
                events = {"status": "unavailable"}
        if events["status"] == "unavailable":
            events = dict(saved["memory_events"], status="last-known", terminal_status="unavailable")
    safe_state = {key: state.get(key) for key in
                  ("Status", "Running", "ExitCode", "OOMKilled", "StartedAt", "FinishedAt")}
    receipt = {"schema": 1, "boot_id": boot, "container_id": cid, "container_name": name,
               "state": safe_state, "caller": caller, "memory_events": events,
               "realtime_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
               "inspect_sha256": hashlib.sha256(raw).hexdigest(), "kill_initiator": "UNKNOWN"}
    if live:
        receipt = {"schema": 1, "boot_id": boot, "container_id": cid, "container_name": name,
                   "started_at": state.get("StartedAt"), "owner": owner, "memory_events": events}
    leaf = memory_leaf if live else f"{cid}.{generation}.exited.json"
    # ponytail: 64 receipts of each kind maximum; no eviction or second poller.
    entries = os.listdir(directory)
    suffix = ".memory.json" if live else ".exited.json"
    if len([entry for entry in entries if entry.endswith(suffix)]) >= 64 and leaf not in entries:
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
                        or (previous.get("started_at") if live else previous.get("state", {}).get("StartedAt")) != state.get("StartedAt")
                        or (live and saved is None)):
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

if [[ "$GB10_RETAIN_SNAPSHOT_LIVE" == 1 ]]; then
  echo "gb10_retain_container_inspect: captured live cid=$cid container=$container"
  exit 0
fi
chmod 0600 "$tmp" || skip "chmod failed; keeping last inspect"
if [[ -L "$output" || -e "$output" ]]; then
  [[ -f "$output" && ! -L "$output" ]] || skip "output leaf is not a replaceable regular file"
  [[ "$(stat -c '%u' -- "$output")" == "$(id -u)" ]] || skip "output owner mismatch"
fi
mv -Tf -- "$tmp" "$output" || skip "publish failed; keeping last inspect"
trap - EXIT
echo "gb10_retain_container_inspect: retained cid=$cid container=$container"
