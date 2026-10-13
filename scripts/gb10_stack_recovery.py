#!/usr/bin/env python3
"""Opt-in Guardian-owned recovery of the fixed GB10 model stack."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from gb10_bounded_process import command

TEXT = "vllm-aeon-ultimate-uncensored-nvfp4.service"
EMBED = "vllm-embedding.service"
RERANK = "vllm-querit-4b-reranker.service"
STOP_ORDER = (TEXT, EMBED, RERANK)
START_ORDER = (EMBED, RERANK, TEXT)
MODELS = {
    TEXT: ("vllm-aeon-ultimate-uncensored-nvfp4", "chat", "18010", "aeon-ultimate"),
    EMBED: ("vllm-embedding", "embedding", "18012", "qwen3-embedding-8b"),
    RERANK: ("querit-4b-vllm", "rerank", "18013", "Querit/Querit-4B"),
}
STATE = Path("/home/obj/.local/state/gb10-lifecycle")
RESERVE = 240
FIELDS = {"version", "receipt_id", "unit_name", "boot_id", "config_revision",
          "target_device", "target_inode", "container_id", "control_group",
          "threshold_bytes", "deadline_boottime_ms"}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def private_directory(path: Path) -> None:
    check(path.is_absolute() and ".." not in path.parts, "unsafe directory path")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        check(stat.S_ISDIR(info.st_mode), "directory contains symlink or non-directory")
    check(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
          "directory must be owner-only 0700")


def read_private(path: Path) -> bytes:
    with contextlib.ExitStack() as stack:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        check(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
              and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
              "unsafe private file")
        data = os.read(fd, 65537)
        check(len(data) <= 65536, "private file exceeds bound")
        return data


def parse(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=pairs)
    check(isinstance(value, dict), "expected JSON object")
    return value


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path: Path, value: dict, *, create: bool = False) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class Host:
    """Only this boundary talks to the real user manager and Docker."""
    def __init__(self):
        self.deadline = self.clock() + 3600

    def clock(self):
        return time.clock_gettime(time.CLOCK_BOOTTIME)

    def boot_id(self):
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def sleep(self):
        time.sleep(0.2)

    def budget(self):
        left = self.deadline - self.clock()
        check(left > 0, "recovery command deadline exhausted")
        return left

    def call(self, args, timeout=15):
        return command(args, timeout=min(timeout, self.budget()),
                       env={"HOME": "/home/obj", "PATH": "/usr/bin:/bin", "LC_ALL": "C",
                            "DOCKER_HOST": "unix:///run/user/1001/docker.sock",
                            "XDG_RUNTIME_DIR": f"/run/user/{os.geteuid()}"},
                       progress=self.budget)

    def properties(self, unit):
        names = ("ActiveState", "SubState", "InvocationID", "MainPID", "Job", "NRestarts")
        raw = self.call(["/usr/bin/systemctl", "--user", "show", unit,
                         *("--property=" + name for name in names)])
        rows = [row.split("=", 1) for row in raw.splitlines()]
        check(all(len(row) == 2 for row in rows), "malformed unit properties")
        result = dict(rows)
        check(len(rows) == len(names) and set(result) == set(names), "incomplete unit properties")
        return result

    def snapshot(self, unit):
        props = self.properties(unit)
        name = MODELS[unit][0]
        ids = self.call(["/usr/bin/docker", "container", "ls", "-a", "--no-trunc",
                         "--filter", "name=^/" + name + "$", "--format", "{{.ID}}"]).split()
        check(len(ids) <= 1 and all(re.fullmatch("[0-9a-f]{64}", x) for x in ids),
              "ambiguous Docker generation")
        result = dict(invocation=props["InvocationID"], cid=None, running=False,
                      cgroup=None, device=None, inode=None, pid=props["MainPID"],
                      restarts=int(props["NRestarts"]),
                      settled=props["ActiveState"] in {"inactive", "failed"} and props["Job"] in {"", "0"})
        if not ids:
            return result
        payload = json.loads(self.call(["/usr/bin/docker", "inspect", ids[0]]))
        check(isinstance(payload, list) and len(payload) == 1, "invalid Docker inspect")
        item = payload[0]
        check(item["Id"] == ids[0] and item["Name"] == "/" + name, "Docker identity mismatch")
        result.update(cid=ids[0], started=item["State"]["StartedAt"])
        if item["State"]["Running"]:
            pid = item["State"]["Pid"]
            check(type(pid) is int and pid > 0, "invalid Docker PID")
            control = f"/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/app.slice/docker-{ids[0]}.scope"
            check(Path(f"/proc/{pid}/cgroup").read_text().strip() == "0::" + control,
                  "Docker process cgroup mismatch")
            group = Path("/sys/fs/cgroup" + control)
            metadata = group.stat()
            result.update(cgroup=control, device=metadata.st_dev, inode=metadata.st_ino,
                          running=props["ActiveState"] == "active" and props["SubState"] == "running",
                          container_running=True)
        check(self.properties(unit) == props, "unit generation changed during snapshot")
        return result

    def target(self, manifest):
        snapshot = self.snapshot(TEXT)
        check(snapshot["cid"] == manifest["container_id"]
              and snapshot["cgroup"] == manifest["control_group"]
              and snapshot["device"] == manifest["target_device"]
              and snapshot["inode"] == manifest["target_inode"], "old Guardian target changed")
        return snapshot

    def stop(self, unit):
        self.call(["/usr/bin/systemctl", "--user", "stop", "--no-block", unit])

    def startable(self, unit):
        props = self.properties(unit)
        check(props["ActiveState"] in {"inactive", "failed"} and props["Job"] in {"", "0"},
              "start requires an inactive unit without a pending job")

    def start(self, unit):
        self.startable(unit)
        self.call(["/usr/bin/systemctl", "--user", "reset-failed", unit])
        self.startable(unit)
        raw = self.call(["/usr/bin/busctl", "--user", "call", "org.freedesktop.systemd1",
                         "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                         "StartUnit", "ss", unit, "fail"])
        match = re.fullmatch(r'o "(/org/freedesktop/systemd1/job/([1-9][0-9]{0,9}))"\n?', raw)
        check(match is not None and int(match[2]) < 2**32, "invalid submitted manager job")
        return match[1]

    def acquire(self, unit, job):
        """First ownership requires the submitted job to bracket one invocation."""
        match = re.fullmatch(r"/org/freedesktop/systemd1/job/([1-9][0-9]{0,9})", job)
        check(match is not None and int(match[1]) < 2**32, "invalid acquisition job")
        before = self.properties(unit)
        check(before["Job"] == match[1], "submitted job is no longer attached to unit")
        invocation = before["InvocationID"]
        if not invocation:
            return None  # The owned job is queued but has not created an invocation.
        check(re.fullmatch(r"[0-9a-f]{32}", invocation) is not None, "invalid acquired invocation")
        generation = self.snapshot(unit)
        after = self.properties(unit)
        check(after["Job"] == match[1] and after["InvocationID"] == invocation
              and generation["invocation"] == invocation, "submitted job invocation changed during acquisition")
        return generation

    def gone(self, generation):
        cid = generation["cid"]
        if cid:
            ids = self.call(["/usr/bin/docker", "container", "ls", "-a", "--no-trunc",
                             "--filter", "id=" + cid, "--format", "{{.ID}}"]).split()
            if ids:
                return False
        group = generation.get("cgroup")
        if group and Path("/sys/fs/cgroup" + group).exists():
            return False
        return True

    def available(self):
        return int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                        if line.startswith("MemAvailable:"))) * 1024

    def ready(self, unit, deadline):
        container, kind, port, model = MODELS[unit]
        seconds = max(1, int(deadline - self.clock()))
        self.call(["/usr/bin/bash", "--noprofile", "--norc",
                   "/home/obj/.local/bin/gb10_service_ready.sh", kind,
                   "http://100.105.4.92:" + port, model, "--unit", unit,
                   "--container", container, "--deadline", str(seconds)], timeout=seconds)


class Recovery:
    def __init__(self, directory, host, state=STATE, invocation=None, startup_min=None):
        self.directory, self.host, self.state = directory, host, state
        self.startup_min = startup_min
        self.invocation = invocation if invocation is not None else os.environ.get("INVOCATION_ID", "")
        self.marker = state / "recovery-owner.json"

    def load(self):
        private_directory(self.directory)
        raw = read_private(self.directory / "manifest.json")
        self.manifest = parse(raw)
        m = self.manifest
        check(set(m) == FIELDS and type(m["version"]) is int and m["version"] == 1, "invalid manifest fields")
        check(isinstance(m["receipt_id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", m["receipt_id"])
              and m["receipt_id"] == self.directory.name
              and m["unit_name"] == "gb10-stack-recovery@" + m["receipt_id"] + ".service", "invalid receipt identity")
        check(m["boot_id"] == self.host.boot_id(), "receipt belongs to another boot")
        check(re.fullmatch(r"[0-9a-f]{32}", self.invocation) is not None, "invalid invocation identity")
        for key in ("config_revision", "target_device", "target_inode", "threshold_bytes", "deadline_boottime_ms"):
            check(type(m[key]) is int and 0 <= m[key] < 2**64, "invalid manifest integer")
        check(m["threshold_bytes"] > 0 and re.fullmatch(r"[0-9a-f]{64}", m["container_id"])
              and isinstance(m["control_group"], str) and m["control_group"].startswith("/")
              and ".." not in Path(m["control_group"]).parts, "invalid target identity")
        self.deadline = m["deadline_boottime_ms"] / 1000
        self.identity = dict(version=1, receipt_id=m["receipt_id"],
                             manifest_sha256=hashlib.sha256(raw).hexdigest(), invocation_id=self.invocation)

    @contextlib.contextmanager
    def lock(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_directory(self.state)
        fd = os.open(self.state / "lifecycle.mutex", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            check(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                  and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, "unsafe lifecycle mutex")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("lifecycle operation already active") from error
            yield
        finally:
            os.close(fd)

    def wait(self, predicate, deadline):
        while True:
            check(self.host.clock() < deadline, "recovery deadline exhausted")
            if predicate():
                return
            self.host.sleep()

    def save(self):
        write_private(self.directory / "journal.json", self.journal)

    def permit(self):
        ready = read_private(self.directory / "ready.json")
        permit = read_private(self.directory / "permit.json")
        check(ready == permit and parse(ready) == self.identity, "permit does not match owner readiness")

    def release_marker(self):
        check(parse(read_private(self.marker)) == self.identity, "recovery owner changed")
        self.marker.unlink()
        sync_directory(self.state)

    def result(self, status):
        check(self.host.clock() < self.deadline, "cannot publish terminal result after operation deadline")
        write_private(self.directory / "result.json", {**self.identity, "status": status})

    def settled(self, unit):
        state = self.host.snapshot(unit)
        return state.get("settled", not state["running"])

    def run(self):
        self.load()
        check(type(self.startup_min) is int and self.manifest["threshold_bytes"] < self.startup_min < 2**64,
              "measured startup headroom must exceed emergency threshold")
        limit = self.deadline - RESERVE
        check(self.host.clock() < limit and self.deadline - self.host.clock() <= 3600, "invalid operation deadline")
        self.host.deadline = limit
        with self.lock():
            check(not os.path.lexists(self.state / "investigation.lock"), "investigation lock active")
            check(not os.path.lexists(self.marker), "unfinished recovery owner exists")
            self.host.target(self.manifest)
            write_private(self.directory / "ready.json", self.identity, create=True)
            self.wait(lambda: os.path.lexists(self.directory / "permit.json"), limit)
            self.permit()
            target = self.host.target(self.manifest)
            old = {unit: target if unit == TEXT else self.host.snapshot(unit) for unit in STOP_ORDER}
            self.journal = {"identity": self.identity, "old": old,
                            "new": {}, "intent": None, "successful": False,
                            "startup_min_available_bytes": self.startup_min,
                            "available_before": self.host.available()}
            self.save()
            write_private(self.marker, self.identity, create=True)
            for unit in STOP_ORDER:
                self.journal["intent"] = ["stop", unit]
                self.save()
                check(self.host.snapshot(unit) == self.journal["old"][unit], "old generation replaced before stop")
                self.host.stop(unit)
            self.wait(lambda: all(self.settled(unit)
                                  and self.host.gone(self.journal["old"][unit]) for unit in STOP_ORDER), limit)
            self.wait(lambda: self.host.available() >= self.startup_min
                      and self.host.available() > self.journal["available_before"], limit)
            self.journal["available_released"] = self.host.available()
            self.save()
            for unit in START_ORDER:
                self.journal["intent"] = ["start", unit]
                self.save()
                job = self.host.start(unit)
                def capture():
                    generation = (self.host.snapshot(unit) if unit in self.journal["new"]
                                  else self.host.acquire(unit, job))
                    if generation is None:
                        return False
                    if generation["invocation"] and generation["invocation"] != self.journal["old"][unit]["invocation"]:
                        check(generation["restarts"] == 0, "automatic restart cannot become owned startup")
                        if unit not in self.journal["new"]:
                            self.journal["new"][unit] = generation
                        check(generation["invocation"] == self.journal["new"][unit]["invocation"], "new unit replaced during startup")
                        for field in ("cid", "cgroup", "device", "inode", "started"):
                            expected = self.journal["new"][unit].get(field)
                            check(expected is None or generation.get(field) == expected,
                                  "new container replaced during startup")
                        # Retain each validated observation, including the first CID
                        # materialized while the service is still activating.
                        self.journal["new"][unit] = generation
                        self.save()
                        if generation["running"]:
                            return True
                    return False
                self.wait(capture, limit)
                self.host.ready(unit, limit)
                check(self.host.snapshot(unit) == self.journal["new"][unit], "generation changed during readiness")
            check(all(self.host.snapshot(unit) == self.journal["new"][unit] for unit in START_ORDER), "stack changed before success")
            self.journal["successful"] = True
            self.journal["intent"] = None
            self.save()
            self.result("succeeded")
            self.release_marker()

    def cleanup(self):
        self.load()
        self.host.deadline = self.host.clock() + RESERVE
        with self.lock():
            if not os.path.lexists(self.marker):
                return  # No admitted transaction ever mutated the model stack.
            check(parse(read_private(self.marker)) == self.identity, "cleanup does not own recovery marker")
            self.permit()
            self.journal = parse(read_private(self.directory / "journal.json"))
            check(self.journal["identity"] == self.identity, "cleanup journal belongs to another invocation")
            if self.journal["successful"]:
                for unit in START_ORDER:
                    check(self.host.snapshot(unit) == self.journal["new"][unit], "successful generation changed")
                    self.host.ready(unit, self.host.deadline)
                    check(self.host.snapshot(unit) == self.journal["new"][unit], "successful generation changed during probe")
                check(all(self.host.snapshot(unit) == self.journal["new"][unit] for unit in START_ORDER),
                      "successful stack changed before result")
                self.result("succeeded")
                self.release_marker()
                return
            # Retain every validated observation: a fresh snapshot is not authority.
            owned = {}
            for unit in STOP_ORDER:
                current = self.host.snapshot(unit)
                expected = self.journal["new"].get(unit, self.journal["old"][unit])
                # An intent cannot prove submission or ownership of a new invocation.
                absent = current.get("settled", False) and current["cid"] is None and self.host.gone(expected)
                check(current["invocation"] == expected["invocation"] or absent,
                      "outside-protocol unit generation replacement")
                if not absent:
                    for field in ("cid", "cgroup", "device", "inode", "started"):
                        check(expected.get(field) is None or current.get(field) == expected[field],
                              "outside-protocol container generation replacement")
                owned[unit] = current
            for unit in STOP_ORDER:
                check(self.host.snapshot(unit) == owned[unit], "cleanup generation changed before stop")
                self.host.stop(unit)
            self.wait(lambda: all(self.settled(unit)
                                  and self.host.gone(owned[unit]) and self.host.gone(self.journal["old"][unit])
                                  for unit in STOP_ORDER), self.host.deadline)
            self.result("stopped")
            self.release_marker()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "cleanup"))
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--startup-min-available-bytes", type=int)
    args = parser.parse_args()
    expected = Path(f"/run/user/{os.geteuid()}/gb10-memory-guardian/recovery")
    check(args.run_directory.parent == expected, "run directory is outside Guardian runtime")
    recovery = Recovery(args.run_directory, Host(), startup_min=args.startup_min_available_bytes)
    getattr(recovery, args.action)()


if __name__ == "__main__":
    main()
