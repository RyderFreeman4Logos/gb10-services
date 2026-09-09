from __future__ import annotations

import errno
import os
import json
import selectors
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ScopePolicy",
    "command",
    "remaining",
    "scoped_command",
    "scoped_direct_command",
]

_MAX_STREAM_BYTES = 4 * 1024 * 1024
_READ_BYTES = 64 * 1024
_POLL_SECONDS = 0.02
_SCOPE_STATUS_BYTES = 64 * 1024
_SCOPE_UNIT_TOKEN = "@GB10_SCOPE_UNIT@"
_LINUX_PID_LIMIT = 1 << 22
_LINUX_NAMESPACE_INODE_LIMIT = 1 << 64


class BoundedProcessError(RuntimeError):
    """A command failed or its complete process tree could not be contained."""

    def __init__(self, message: str, *, error_number: int | None = None) -> None:
        super().__init__(message)
        self.errno = error_number


def remaining(deadline: float, cap: float | None = None) -> float:
    value = deadline - time.monotonic()
    if cap is not None:
        value = min(value, cap)
    if value <= 0:
        raise BoundedProcessError("operation deadline exhausted")
    return value


def _enable_subreaper() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    except (ImportError, OSError) as error:
        raise BoundedProcessError("cannot establish subprocess subreaper authority") from error


def _proc_row(pid: int) -> tuple[int, int] | None:
    try:
        payload = Path(f"/proc/{pid}/stat").read_bytes().decode(
            "utf-8", errors="replace"
        )
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    closing = payload.rfind(")")
    if closing <= 1:
        return None
    fields = payload[closing + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[1]), int(fields[19])
    except ValueError:
        return None


def _child_pids(pid: int) -> list[int]:
    try:
        payload = Path(f"/proc/{pid}/task/{pid}/children").read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return []
    if len(payload) > 1024 * 1024:
        raise BoundedProcessError("subprocess child list exceeded its bound")
    fields = payload.split()
    if any(not field.isdigit() for field in fields):
        raise BoundedProcessError("subprocess child list is malformed")
    return [int(field) for field in fields]


def _direct_child_identities(pid: int) -> set[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for child in _child_pids(pid):
        row = _proc_row(child)
        if row is not None and row[0] == pid:
            identities.add((child, row[1]))
    return identities


@dataclass
class _OwnedProcess:
    pid: int
    starttime: int
    pidfd: int | None

    def close(self) -> None:
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None


class _ProcessTree:
    def __init__(
        self,
        leader: subprocess.Popen[bytes],
        baseline_children: set[tuple[int, int]],
    ) -> None:
        self.leader = leader
        self.owner_pid = os.getpid()
        self.baseline_children = baseline_children
        row = _proc_row(leader.pid)
        starttime = row[1] if row is not None else 0
        self.root_starttime = starttime
        self.owned: dict[int, _OwnedProcess] = {}
        self._add(leader.pid, starttime)

    def _add(self, pid: int, starttime: int) -> None:
        if pid in self.owned:
            return
        pidfd: int | None = None
        try:
            pidfd = os.pidfd_open(pid, 0)
        except (AttributeError, OSError):
            pass
        self.owned[pid] = _OwnedProcess(pid, starttime, pidfd)

    def scan(self) -> dict[int, tuple[int, int]]:
        table: dict[int, tuple[int, int]] = {}
        queue = [self.owner_pid, *self.owned]
        visited: set[int] = set()
        while queue:
            parent = queue.pop()
            if parent in visited:
                continue
            visited.add(parent)
            for pid in _child_pids(parent):
                row = _proc_row(pid)
                if row is None or row[0] != parent:
                    continue
                table[pid] = row
                if pid not in self.owned:
                    parent_owned = self.owned.get(parent)
                    from_owned_parent = parent_owned is not None
                    newly_adopted = (
                        parent == self.owner_pid
                        and (pid, row[1]) not in self.baseline_children
                        and row[1] >= self.root_starttime
                    )
                    if from_owned_parent or newly_adopted:
                        self._add(pid, row[1])
                if pid in self.owned:
                    queue.append(pid)
        for pid, owned in self.owned.items():
            row = _proc_row(pid)
            if row is not None and row[1] == owned.starttime:
                table[pid] = row
        for pid, owned in list(self.owned.items()):
            row = table.get(pid)
            if (row is None or row[1] != owned.starttime) and (
                pid != self.leader.pid or self.leader.returncode is not None
            ):
                owned.close()
                self.owned.pop(pid, None)
        return table

    def descendants(self, table: dict[int, tuple[int, int]]) -> list[_OwnedProcess]:
        return [
            owned
            for pid, owned in self.owned.items()
            if pid != self.leader.pid
            and table.get(pid, (0, -1))[1] == owned.starttime
        ]

    def _signal_group(self, number: int) -> None:
        leader = self.owned.get(self.leader.pid)
        if (
            self.leader.returncode is None
            and leader is not None
            and leader.starttime == self.root_starttime
        ):
            if leader.pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                try:
                    signal.pidfd_send_signal(leader.pidfd, 0, None, 0)
                except ProcessLookupError:
                    return
                except OSError:
                    pass
            leader_row = _proc_row(self.leader.pid)
            if leader_row is None or leader_row[1] != self.root_starttime:
                return
            try:
                os.killpg(self.leader.pid, number)
            except ProcessLookupError:
                pass

    def signal_group(self, number: int) -> None:
        self._signal_group(number)

    def signal_descendants(self, number: int) -> None:
        table = self.scan()
        first_error: BoundedProcessError | None = None
        for owned in self.descendants(table):
            if owned.pidfd is None or not hasattr(signal, "pidfd_send_signal"):
                first_error = first_error or BoundedProcessError(
                    "subprocess descendant pidfd signal is unavailable"
                )
                continue
            try:
                signal.pidfd_send_signal(owned.pidfd, number, None, 0)
            except ProcessLookupError:
                continue
            except OSError:
                first_error = first_error or BoundedProcessError(
                    "subprocess descendant pidfd signal failed"
                )
        if first_error is not None:
            raise first_error

    def signal(self, number: int) -> None:
        self.signal_group(number)
        self.signal_descendants(number)

    def reap_adopted(self) -> None:
        table = self.scan()
        for owned in list(self.descendants(table)):
            row = table.get(owned.pid)
            if row is None or row[0] != self.owner_pid:
                continue
            try:
                waited, _ = os.waitpid(owned.pid, os.WNOHANG)
            except ChildProcessError:
                waited = owned.pid if _proc_row(owned.pid) is None else 0
            if waited == owned.pid:
                owned.close()
                self.owned.pop(owned.pid, None)

    def survivors(self) -> list[int]:
        table = self.scan()
        return sorted(
            owned.pid
            for owned in self.owned.values()
            if table.get(owned.pid, (0, -1))[1] == owned.starttime
            and (owned.pid != self.leader.pid or self.leader.returncode is None)
        )

    def close(self) -> None:
        for owned in self.owned.values():
            owned.close()
        self.owned.clear()


@dataclass
class _Capture:
    retained: bytearray
    limit: int = _MAX_STREAM_BYTES
    total: int = 0
    truncated: bool = False

    def append(self, payload: bytes) -> None:
        self.total += len(payload)
        available = max(0, self.limit - len(self.retained))
        if available:
            self.retained.extend(payload[:available])
        if len(payload) > available:
            self.truncated = True


def _close_stream(
    selector: selectors.BaseSelector,
    streams: dict[int, tuple[str, object]],
    descriptor: int,
) -> None:
    try:
        selector.unregister(descriptor)
    except (KeyError, ValueError):
        pass
    entry = streams.pop(descriptor, None)
    if entry is not None:
        entry[1].close()  # type: ignore[union-attr]


def _drain_once(
    selector: selectors.BaseSelector,
    streams: dict[int, tuple[str, object]],
    captures: dict[str, _Capture],
    wait_seconds: float,
) -> None:
    for key, _ in selector.select(max(0.0, wait_seconds)):
        descriptor = key.fd
        try:
            payload = os.read(descriptor, _READ_BYTES)
        except BlockingIOError:
            continue
        if not payload:
            _close_stream(selector, streams, descriptor)
            continue
        name = streams[descriptor][0]
        captures[name].append(payload)


def _render(capture: _Capture) -> str:
    text = bytes(capture.retained).decode("utf-8", errors="replace")
    if capture.truncated:
        text += f"\n[{capture.total - len(capture.retained)} bytes omitted]"
    return text


def _bounded_error_summary(error: BaseException) -> str:
    if isinstance(error, BoundedProcessError):
        summary = str(error).splitlines()[0].strip()
    else:
        summary = type(error).__name__
    return (summary or "cleanup failure")[:256]


def _signal_cleanup(
    tree: _ProcessTree,
    scope_signal: Callable[[int], None] | None,
    number: int,
) -> None:
    first_error: BaseException | None = None
    actions: list[Callable[[], None]] = []
    if scope_signal is not None and number == signal.SIGKILL:
        actions.extend((lambda: scope_signal(number), lambda: tree.signal_group(number)))
    else:
        actions.append(lambda: tree.signal_group(number))
        if scope_signal is not None:
            actions.append(lambda: scope_signal(number))
    if scope_signal is None:
        actions.append(lambda: tree.signal_descendants(number))
    for action in actions:
        try:
            action()
        except BaseException as error:
            first_error = first_error or error
    if first_error is not None:
        raise first_error


def _bounded_reap(
    process: subprocess.Popen[bytes],
    tree: _ProcessTree,
    selector: selectors.BaseSelector,
    streams: dict[int, tuple[str, object]],
    captures: dict[str, _Capture],
    hard_deadline: float,
    scope_signal: Callable[[int], None] | None = None,
) -> None:
    first_error: BaseException | None = None
    cleanup_complete = False

    def remember(error: BaseException) -> None:
        nonlocal first_error
        first_error = first_error or error

    try:
        now = time.monotonic()
        term_deadline = min(
            hard_deadline,
            now + min(2.0, max(0.0, (hard_deadline - now) / 2)),
        )
        try:
            _signal_cleanup(tree, scope_signal, signal.SIGTERM)
        except BaseException as error:
            remember(error)
        while time.monotonic() < term_deadline:
            try:
                _drain_once(
                    selector,
                    streams,
                    captures,
                    min(_POLL_SECONDS, term_deadline - time.monotonic()),
                )
                process.poll()
            except BaseException as error:
                remember(error)
                break
            try:
                tree.reap_adopted()
                cleanup_complete = not tree.survivors() and not streams
            except BaseException as error:
                remember(error)
                cleanup_complete = False
            if cleanup_complete:
                break
    except BaseException as error:
        remember(error)
    finally:
        if scope_signal is not None or not cleanup_complete:
            try:
                _signal_cleanup(tree, scope_signal, signal.SIGKILL)
            except BaseException as error:
                remember(error)
    try:
        while time.monotonic() < hard_deadline:
            try:
                _drain_once(
                    selector,
                    streams,
                    captures,
                    min(_POLL_SECONDS, hard_deadline - time.monotonic()),
                )
                process.poll()
            except BaseException as error:
                remember(error)
                break
            try:
                tree.reap_adopted()
                cleanup_complete = not tree.survivors() and not streams
            except BaseException as error:
                remember(error)
                cleanup_complete = False
            if cleanup_complete:
                break
    except BaseException as error:
        remember(error)
    for descriptor in list(streams):
        try:
            _close_stream(selector, streams, descriptor)
        except BaseException as error:
            remember(error)
    if process.returncode is None:
        try:
            process.wait(timeout=max(0.001, hard_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        except BaseException as error:
            remember(error)
    try:
        tree.reap_adopted()
    except BaseException as error:
        remember(error)
    try:
        survivors = tree.survivors()
    except BaseException as error:
        remember(error)
        survivors = []
    if process.returncode is None and process.pid not in survivors:
        survivors.append(process.pid)
        survivors.sort()
    if survivors:
        diagnostic = "" if first_error is None else f"; cleanup={_bounded_error_summary(first_error)}"
        raise BoundedProcessError(
            f"subprocess cleanup deadline exhausted; survivor_count={len(survivors)}"
            + diagnostic
        )
    if first_error is not None:
        raise first_error


@dataclass(frozen=True)
class ScopePolicy:
    phase: str
    memory_high: int
    memory_max: int
    tasks_max: int
    cpu_percent: int
    fsize_bytes: int
    min_mem_available: int
    runtime_seconds: int
    proc_root: Path = Path("/proc")
    cgroup_root: Path = Path("/sys/fs/cgroup")

    def __post_init__(self) -> None:
        if (
            not self.phase
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in self.phase)
            or self.memory_high <= 0
            or self.memory_max < self.memory_high
            or self.tasks_max <= 0
            or self.cpu_percent not in {100, 200}
            or self.fsize_bytes <= 0
            or self.min_mem_available <= 0
            or self.runtime_seconds <= 0
            or not self.proc_root.is_absolute()
            or not self.cgroup_root.is_absolute()
        ):
            raise BoundedProcessError("invalid scope policy")

    def contract(self) -> dict[str, int | str]:
        return {
            "cpu_percent": self.cpu_percent,
            "fsize_bytes": self.fsize_bytes,
            "memory_high": self.memory_high,
            "memory_max": self.memory_max,
            "min_mem_available": self.min_mem_available,
            "phase": self.phase,
            "runtime_seconds": self.runtime_seconds,
            "tasks_max": self.tasks_max,
        }


@dataclass
class _ScopeCgroup:
    relative_path: str
    descriptors: dict[str, int]

    def read(self, name: str, maximum: int = _SCOPE_STATUS_BYTES) -> bytes:
        descriptor = self.descriptors[name]
        try:
            if os.fstat(self.descriptors["directory"]).st_nlink == 0:
                raise OSError(errno.ENODEV, "held scope was removed")
            os.lseek(descriptor, 0, os.SEEK_SET)
            payload = os.read(descriptor, maximum + 1)
        except OSError as error:
            raise BoundedProcessError(
                "scope controller read failed", error_number=error.errno
            ) from error
        if len(payload) > maximum:
            raise BoundedProcessError("scope controller value exceeded its bound")
        return payload

    def close(self) -> None:
        for descriptor in self.descriptors.values():
            os.close(descriptor)
        self.descriptors.clear()


def _read_small_file(path: Path, maximum: int = _SCOPE_STATUS_BYTES) -> bytes:
    try:
        with path.open("rb", buffering=0) as stream:
            payload = stream.read(maximum + 1)
    except OSError as error:
        raise BoundedProcessError("scope authority read failed") from error
    if len(payload) > maximum:
        raise BoundedProcessError("scope authority value exceeded its bound")
    return payload


def _proc_row_under(root: Path, pid: int) -> tuple[int, int] | None:
    try:
        payload = _read_small_file(root / str(pid) / "stat").decode(
            "utf-8", errors="strict"
        )
    except (BoundedProcessError, UnicodeDecodeError):
        return None
    closing = payload.rfind(")")
    if closing <= 1:
        return None
    fields = payload[closing + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[1]), int(fields[19])
    except ValueError:
        return None


def _memory_available(policy: ScopePolicy) -> int:
    payload = _read_small_file(policy.proc_root / "meminfo").decode(
        "ascii", errors="strict"
    )
    values = [line.split() for line in payload.splitlines() if line.startswith("MemAvailable:")]
    if len(values) != 1 or len(values[0]) != 3 or values[0][2] != "kB":
        raise BoundedProcessError("MemAvailable authority is malformed")
    try:
        available = int(values[0][1]) * 1024
    except ValueError as error:
        raise BoundedProcessError("MemAvailable authority is malformed") from error
    if available < policy.min_mem_available:
        raise BoundedProcessError("scope memory admission failed")
    return available


def _json_object(payload: bytes) -> dict[str, object]:
    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate key")
        return dict(pairs)

    try:
        value = json.loads(payload, object_pairs_hook=strict_object)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise BoundedProcessError("scope status is malformed") from error
    if type(value) is not dict:
        raise BoundedProcessError("scope status is malformed")
    return value


def _status_rows(capture: _Capture, *, complete: bool) -> list[dict[str, object]]:
    payload = bytes(capture.retained)
    if not complete and not payload.endswith(b"\n"):
        payload = payload[: payload.rfind(b"\n") + 1]
    if complete and payload and not payload.endswith(b"\n"):
        raise BoundedProcessError("scope status is incomplete")
    rows = [_json_object(line) for line in payload[:-1].split(b"\n")] if payload else []
    if len(rows) > 2:
        raise BoundedProcessError("scope status has unexpected records")
    return rows


def _ready_status(
    capture: _Capture, namespace_keys: set[str]
) -> tuple[int, dict[str, object]] | None:
    payload = bytes(capture.retained)
    if capture.truncated:
        raise BoundedProcessError("scope status exceeded its bound")
    if b"\n" not in payload:
        return None
    if not payload.endswith(b"\n"):
        raise BoundedProcessError("scope status has trailing bytes")
    rows = _status_rows(capture, complete=False)
    expected_keys = {"child-pid", *namespace_keys}
    if len(rows) != 1 or set(rows[0]) != expected_keys:
        raise BoundedProcessError("scope status ready record is invalid")
    row = rows[0]
    value = row["child-pid"]
    if type(value) is not int or not 1 < value < _LINUX_PID_LIMIT:
        raise BoundedProcessError("scope status worker identity is invalid")
    for key in namespace_keys:
        namespace_inode = row[key]
        if (
            type(namespace_inode) is not int
            or not 0 < namespace_inode < _LINUX_NAMESPACE_INODE_LIMIT
        ):
            raise BoundedProcessError("scope status namespace identity is invalid")
    return value, row


def _open_scope_cgroup(policy: ScopePolicy, relative_path: str) -> _ScopeCgroup:
    parts = relative_path.lstrip("/").split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BoundedProcessError("scope cgroup path is malformed")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    current = -1
    descriptors: dict[str, int] = {}
    try:
        current = os.open(policy.cgroup_root, flags)
        for part in parts:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        descriptors = {"directory": current}
        for name in (
            "cgroup.events",
            "cgroup.procs",
            "cpu.max",
            "memory.events",
            "memory.high",
            "memory.max",
            "memory.oom.group",
            "memory.swap.max",
            "pids.events",
            "pids.max",
        ):
            descriptors[name] = os.open(
                name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=current
            )
        descriptors["cgroup.kill"] = os.open(
            "cgroup.kill", os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=current
        )
    except OSError as error:
        for descriptor in descriptors.values():
            os.close(descriptor)
        if current >= 0 and current not in descriptors.values():
            os.close(current)
        raise BoundedProcessError("scope controllers are incomplete") from error
    return _ScopeCgroup(relative_path, descriptors)


def _integer_map(payload: bytes) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in payload.decode("ascii", errors="strict").splitlines():
            key, raw = line.split()
            if key in result:
                raise ValueError("duplicate")
            result[key] = int(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise BoundedProcessError("scope event counters are malformed") from error
    if any(value < 0 for value in result.values()):
        raise BoundedProcessError("scope event counters are malformed")
    return result


def _scope_worker_exact(
    policy: ScopePolicy,
    unit: str,
    worker_pid: int,
    worker_starttime: int,
    cgroup: _ScopeCgroup,
) -> bool:
    row = _proc_row_under(policy.proc_root, worker_pid)
    if row is None or row[1] != worker_starttime:
        return False
    try:
        membership = _read_small_file(
            policy.proc_root / str(worker_pid) / "cgroup"
        ).decode("ascii", errors="strict")
        pinned = os.fstat(cgroup.descriptors["directory"])
        current = os.stat(
            policy.cgroup_root / cgroup.relative_path.lstrip("/"),
            follow_symlinks=False,
        )
        procs = cgroup.read("cgroup.procs").split()
    except (BoundedProcessError, UnicodeDecodeError, OSError):
        return False
    return (
        membership.splitlines() == [f"0::{cgroup.relative_path}"]
        and Path(cgroup.relative_path).name == unit
        and stat.S_ISDIR(pinned.st_mode)
        and stat.S_ISDIR(current.st_mode)
        and (pinned.st_dev, pinned.st_ino) == (current.st_dev, current.st_ino)
        and str(worker_pid).encode("ascii") in procs
    )


def _verify_scope(
    policy: ScopePolicy,
    unit: str,
    wrapper_pid: int,
    worker_pid: int,
) -> tuple[int, int, _ScopeCgroup, dict[str, int], dict[str, int]]:
    if worker_pid <= 1 or worker_pid == wrapper_pid:
        raise BoundedProcessError("scope worker identity is invalid")
    row = _proc_row_under(policy.proc_root, worker_pid)
    if row is None or row[1] <= 0:
        raise BoundedProcessError("scope worker identity is unavailable")
    try:
        cgroup_payload = _read_small_file(
            policy.proc_root / str(worker_pid) / "cgroup"
        ).decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise BoundedProcessError("scope cgroup membership is malformed") from error
    lines = cgroup_payload.splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise BoundedProcessError("scope cgroup membership is malformed")
    relative_path = lines[0][3:]
    if Path(relative_path).name != unit:
        raise BoundedProcessError("scope cgroup membership is not exact")
    cgroup = _open_scope_cgroup(policy, relative_path)
    worker_pidfd = -1
    try:
        expected = {
            "memory.high": str(policy.memory_high),
            "memory.max": str(policy.memory_max),
            "memory.oom.group": "1",
            "memory.swap.max": "0",
            "pids.max": str(policy.tasks_max),
            "cpu.max": f"{policy.cpu_percent * 1000} 100000",
        }
        for name, value in expected.items():
            if cgroup.read(name).decode("ascii", errors="strict").strip() != value:
                raise BoundedProcessError("scope controller value is not exact")
        procs = cgroup.read("cgroup.procs").split()
        if str(worker_pid).encode("ascii") not in procs:
            raise BoundedProcessError("scope worker is outside exact cgroup")
        events = _integer_map(cgroup.read("cgroup.events"))
        if events.get("populated") != 1:
            raise BoundedProcessError("scope cgroup is not populated")
        memory_events = _integer_map(cgroup.read("memory.events"))
        pids_events = _integer_map(cgroup.read("pids.events"))
        for key in ("oom", "oom_kill", "max"):
            if key not in memory_events:
                raise BoundedProcessError("scope memory events are incomplete")
        if "max" not in pids_events:
            raise BoundedProcessError("scope PID events are incomplete")
        if (
            any(memory_events[key] != 0 for key in ("oom", "oom_kill", "max"))
            or pids_events["max"] != 0
        ):
            raise BoundedProcessError("scope pre-GO resource event is nonzero")
        if not _scope_worker_exact(policy, unit, worker_pid, row[1], cgroup):
            raise BoundedProcessError("scope worker identity changed before GO")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise BoundedProcessError("scope worker pidfd authority is unavailable")
        try:
            worker_pidfd = os.pidfd_open(worker_pid, 0)
            signal.pidfd_send_signal(worker_pidfd, 0, None, 0)
        except (AttributeError, ProcessLookupError, OSError) as error:
            raise BoundedProcessError(
                "scope worker pidfd authority is unavailable"
            ) from error
        if not _scope_worker_exact(policy, unit, worker_pid, row[1], cgroup):
            raise BoundedProcessError("scope worker identity changed before GO")
    except BaseException:
        if worker_pidfd >= 0:
            os.close(worker_pidfd)
        cgroup.close()
        raise
    return row[1], worker_pidfd, cgroup, memory_events, pids_events


def _scope_manager_command(
    systemctl: str,
    pass_fds: Sequence[int],
    arguments: Sequence[str],
    label: str,
    *,
    capture: bool = False,
) -> bytes:
    try:
        process = subprocess.Popen(
            [systemctl, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=tuple(pass_fds),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BoundedProcessError(f"scope manager {label} failed") from error
    try:
        output, _ = process.communicate(timeout=2)
    except BaseException as error:
        cleanup_error: BaseException | None = None
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
        except BaseException as failure:
            cleanup_error = failure
        try:
            process.wait(timeout=1)
        except BaseException as failure:
            cleanup_error = cleanup_error or failure
        if process.returncode is None:
            containment = BoundedProcessError(
                f"scope manager {label} cancellation cleanup failed"
            )
            if cleanup_error is not None:
                containment.add_note(f"cleanup diagnostic: {cleanup_error}")
            raise containment from error
        if cleanup_error is not None:
            error.add_note(f"scope manager cleanup diagnostic: {cleanup_error}")
        if isinstance(error, subprocess.TimeoutExpired):
            raise BoundedProcessError(f"scope manager {label} timed out") from error
        if isinstance(error, (OSError, subprocess.SubprocessError)):
            raise BoundedProcessError(f"scope manager {label} failed") from error
        raise
    if process.returncode != 0 or len(output) > _SCOPE_STATUS_BYTES:
        raise BoundedProcessError(f"scope manager {label} failed")
    return output


def _scope_signal(
    policy: ScopePolicy,
    unit: str,
    worker_pid: int,
    worker_starttime: int,
    cgroup: _ScopeCgroup,
    worker_pidfd: int,
    number: int,
    deadline: float,
) -> None:
    del unit
    if number not in {signal.SIGTERM, signal.SIGKILL}:
        raise BoundedProcessError("scope cleanup signal is invalid")
    errors: list[str] = []
    first_error: BaseException | None = None
    try:
        if _scope_quiescent(
            policy, worker_pid, worker_starttime, worker_pidfd, cgroup
        ):
            return
    except BaseException as error:
        first_error = error
        errors.append("scope quiescence check failed")
    if number == signal.SIGKILL:
        try:
            if os.write(cgroup.descriptors["cgroup.kill"], b"1\n") != 2:
                raise OSError("short cgroup.kill write")
        except BaseException as error:
            first_error = first_error or error
            errors.append("scope cgroup KILL failed")
    try:
        signal.pidfd_send_signal(worker_pidfd, number, None, 0)
    except ProcessLookupError:
        pass
    except BaseException as error:
        first_error = first_error or error
        errors.append(
            "scope worker pidfd KILL failed"
            if number == signal.SIGKILL
            else "scope worker pidfd TERM failed"
        )
    if number != signal.SIGKILL:
        if errors:
            raise BoundedProcessError("; ".join(dict.fromkeys(errors))) from first_error
        return
    poll_deadline = min(deadline, time.monotonic() + 1.0)
    quiescent = False
    while time.monotonic() < poll_deadline:
        try:
            quiescent = _scope_quiescent(
                policy, worker_pid, worker_starttime, worker_pidfd, cgroup
            )
        except BaseException as error:
            first_error = first_error or error
            errors.append("scope quiescence check failed")
            break
        if quiescent:
            break
        try:
            time.sleep(min(_POLL_SECONDS, max(0.0, poll_deadline - time.monotonic())))
        except BaseException as error:
            first_error = first_error or error
            errors.append("scope quiescence wait failed")
            break
    if not quiescent:
        try:
            quiescent = _scope_quiescent(
                policy, worker_pid, worker_starttime, worker_pidfd, cgroup
            )
        except BaseException as error:
            first_error = first_error or error
            errors.append("scope quiescence check failed")
    if not quiescent:
        errors.append("scope cgroup KILL did not quiesce")
    if errors:
        raise BoundedProcessError("; ".join(dict.fromkeys(errors))) from first_error


def _scope_resource_events(
    cgroup: _ScopeCgroup,
    memory_before: Mapping[str, int],
    pids_before: Mapping[str, int],
) -> tuple[str, ...]:
    memory_after = _integer_map(cgroup.read("memory.events"))
    pids_after = _integer_map(cgroup.read("pids.events"))
    events = [
        f"memory.{key}"
        for key in ("oom", "oom_kill", "max")
        if memory_after.get(key, 0) > memory_before.get(key, 0)
    ]
    if pids_after.get("max", 0) > pids_before.get("max", 0):
        events.append("pids.max")
    return tuple(events)


def _scope_quiescent(
    policy: ScopePolicy,
    worker_pid: int,
    worker_starttime: int,
    worker_pidfd: int,
    cgroup: _ScopeCgroup,
) -> bool:
    row = _proc_row_under(policy.proc_root, worker_pid)
    if row is not None and row[1] == worker_starttime:
        return False
    try:
        signal.pidfd_send_signal(worker_pidfd, 0, None, 0)
    except ProcessLookupError:
        pass
    except (AttributeError, OSError):
        return False
    else:
        return False
    try:
        events = _integer_map(cgroup.read("cgroup.events"))
        procs = cgroup.read("cgroup.procs").split()
    except BoundedProcessError as error:
        return error.errno == errno.ENODEV
    return events.get("populated") == 0 and not procs


def scoped_command(
    sandbox_arguments: Sequence[str],
    policy: ScopePolicy,
    *,
    systemd_run: str,
    systemctl: str,
    nice: str,
    ionice: str,
    prlimit: str,
    timeout: float,
    deadline: float,
    env: Mapping[str, str],
    pass_fds: Sequence[int],
    max_output_bytes: int,
    cleanup_reserve: float = 4.0,
    progress: Callable[[], None] | None = None,
    _direct: bool = False,
) -> bytes:
    """Run one payload only after its exact user scope is proven."""

    if (
        not sandbox_arguments
        or (_direct and len(sandbox_arguments) < 3)
        or timeout <= 0
        or cleanup_reserve <= 0
        or max_output_bytes <= 0
        or max_output_bytes > 192 * 1024 * 1024
    ):
        raise BoundedProcessError("invalid scoped command")
    if _direct:
        unshare_all = proc_mount = -1
    else:
        try:
            unshare_all = sandbox_arguments.index("--unshare-all")
            proc_mount = sandbox_arguments.index("--proc")
        except ValueError as error:
            raise BoundedProcessError("invalid scoped namespace policy") from error
    weaker_unshares = {
        "--unshare-cgroup",
        "--unshare-ipc",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-uts",
    }
    resource_fences = [
        index
        for index in range(len(sandbox_arguments) - 2)
        if list(sandbox_arguments[index : index + 3])
        == ["--setenv", "GB10_RESOURCE_FENCE", "1"]
    ]
    if not _direct and (
        sandbox_arguments.count("--unshare-user") != 1
        or sandbox_arguments.count("--unshare-all") != 1
        or sandbox_arguments.count("--proc") != 1
        or len(resource_fences) != 1
        or sandbox_arguments.count("GB10_RESOURCE_FENCE") != 1
        or proc_mount + 1 >= len(sandbox_arguments)
        or sandbox_arguments[proc_mount + 1] != "/proc"
        or unshare_all > proc_mount
        or any(option in sandbox_arguments for option in weaker_unshares)
        or sandbox_arguments.count("--share-net") != (policy.phase == "fetch")
        or (
            "--share-net" in sandbox_arguments
            and unshare_all > sandbox_arguments.index("--share-net")
        )
    ):
        raise BoundedProcessError("invalid scoped namespace policy")
    _memory_available(policy)
    started = time.monotonic()
    hard_deadline = min(deadline, started + timeout)
    reserve = min(cleanup_reserve, max(0.0, (hard_deadline - started) / 2))
    work_deadline = hard_deadline - reserve
    if work_deadline <= started:
        raise BoundedProcessError("insufficient scoped command budget")

    namespace_keys = set() if _direct else {
        "cgroup-namespace",
        "ipc-namespace",
        "mnt-namespace",
        "net-namespace",
        "pid-namespace",
        "uts-namespace",
    }
    if policy.phase == "fetch":
        namespace_keys.discard("net-namespace")

    unit = f"llm-guard-rebuild-{policy.phase}-{secrets.token_hex(16)}.scope"
    sandbox = [unit if value == _SCOPE_UNIT_TOKEN else value for value in sandbox_arguments]
    status_read, status_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    block_read, block_write = os.pipe2(os.O_CLOEXEC)
    fence_parent, fence_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    fence_parent.setblocking(False)
    sandbox = (
        [
            sandbox[0],
            sandbox[1],
            "--direct-scope-child",
            str(status_write),
            str(block_read),
            unit,
            "--",
            *sandbox[2:],
        ]
        if _direct
        else [
            sandbox[0],
            "--json-status-fd",
            str(status_write),
            "--block-fd",
            str(block_read),
            *sandbox[1:],
        ]
    )
    arguments = [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        *([] if _direct else ["--collect"]),
        f"--unit={unit}",
        f"--property=MemoryHigh={policy.memory_high}",
        f"--property=MemoryMax={policy.memory_max}",
        "--property=MemorySwapMax=0",
        f"--property=TasksMax={policy.tasks_max}",
        f"--property=CPUQuota={policy.cpu_percent}%",
        "--property=CPUQuotaPeriodSec=100ms",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=OOMPolicy=kill",
        f"--property=RuntimeMaxSec={policy.runtime_seconds}",
        *([f"--property=LimitFSIZE={policy.fsize_bytes}"] if _direct else []),
        "--",
        *(
            []
            if _direct
            else [
                nice,
                "-n",
                "10",
                ionice,
                "-c",
                "3",
                prlimit,
                f"--fsize={policy.fsize_bytes}:{policy.fsize_bytes}",
                "--",
            ]
        ),
        *sandbox,
    ]
    inherited = tuple(sorted(set((*pass_fds, status_write, block_read))))
    _enable_subreaper()
    baseline = _direct_child_identities(os.getpid())
    process: subprocess.Popen[bytes] | None = None
    tree: _ProcessTree | None = None
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[str, object]] = {}
    captures = {
        "stdout": _Capture(bytearray(), max_output_bytes),
        "stderr": _Capture(bytearray(), _SCOPE_STATUS_BYTES),
        "status": _Capture(bytearray(), _SCOPE_STATUS_BYTES),
        "fence": _Capture(bytearray(), 1),
    }
    cgroup: _ScopeCgroup | None = None
    worker_pid = 0
    ready_row: dict[str, object] = {}
    worker_starttime = 0
    worker_pidfd = -1
    memory_before: dict[str, int] = {}
    pids_before: dict[str, int] = {}
    resource_events: tuple[str, ...] = ()
    resource_snapshot = False
    fence_released = False
    failure: str | None = None
    identity_lost_at: float | None = None
    try:
        try:
            process = subprocess.Popen(
                arguments,
                env=dict(env),
                stdin=fence_child.fileno(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                pass_fds=inherited,
            )
        except (OSError, ValueError) as error:
            raise BoundedProcessError("scoped command spawn failed") from error
        finally:
            fence_child.close()
            os.close(status_write)
            os.close(block_read)
        tree = _ProcessTree(process, baseline)
        assert process.stdout is not None and process.stderr is not None
        status_stream = os.fdopen(status_read, "rb", buffering=0)
        status_read = -1
        for name, stream in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
            ("status", status_stream),
            ("fence", fence_parent),
        ):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
            streams[descriptor] = (name, stream)

        setup_deadline = min(work_deadline, time.monotonic() + 8.0)
        while time.monotonic() < setup_deadline:
            tree.scan()
            _drain_once(selector, streams, captures, _POLL_SECONDS)
            ready = _ready_status(captures["status"], namespace_keys)
            if ready is not None:
                _drain_once(selector, streams, captures, 0.0)
                ready = _ready_status(captures["status"], namespace_keys)
                if ready is None:
                    raise BoundedProcessError("scope status ready record disappeared")
                worker_pid, ready_row = ready
                break
            if process.poll() is not None or captures["status"].truncated:
                break
        if worker_pid == 0:
            raise BoundedProcessError("scope did not report a worker")
        (
            worker_starttime,
            worker_pidfd,
            cgroup,
            memory_before,
            pids_before,
        ) = _verify_scope(policy, unit, process.pid, worker_pid)
        if not _scope_worker_exact(
            policy, unit, worker_pid, worker_starttime, cgroup
        ):
            raise BoundedProcessError("scope worker identity changed before GO")
        os.write(block_write, b"G")
        os.close(block_write)
        block_write = -1

        while True:
            tree.scan()
            if progress is not None:
                progress()
            _drain_once(
                selector,
                streams,
                captures,
                min(_POLL_SECONDS, max(0.0, work_deadline - time.monotonic())),
            )
            if not resource_snapshot:
                try:
                    observed_events = _scope_resource_events(
                        cgroup, memory_before, pids_before
                    )
                except BoundedProcessError as error:
                    if error.errno == errno.ENODEV and resource_events:
                        resource_snapshot = True
                    elif error.errno != errno.ENODEV:
                        raise
                else:
                    resource_events = tuple(
                        dict.fromkeys((*resource_events, *observed_events))
                    )
            fence_payload = bytes(captures["fence"].retained)
            if captures["fence"].truncated or fence_payload not in {b"", b"R"}:
                failure = "scope resource snapshot fence is malformed"
                break
            if fence_payload == b"R" and not fence_released:
                try:
                    observed_events = _scope_resource_events(
                        cgroup, memory_before, pids_before
                    )
                except BoundedProcessError as error:
                    if error.errno == errno.ENODEV:
                        raise BoundedProcessError(
                            "scope resource receipt unavailable before collection"
                        ) from error
                    raise
                resource_events = tuple(
                    dict.fromkeys((*resource_events, *observed_events))
                )
                resource_snapshot = True
                if fence_parent.send(b"G") != 1:
                    raise BoundedProcessError("scope resource snapshot fence release failed")
                fence_parent.shutdown(socket.SHUT_WR)
                fence_released = True
            status = process.poll()
            tree.reap_adopted()
            descendants = tree.descendants(tree.scan())
            if any(capture.truncated for capture in captures.values()):
                failure = "scoped output exceeded bound"
                break
            terminal_status = len(
                _status_rows(captures["status"], complete=False)
            ) == 2
            worker_exact = _scope_worker_exact(
                policy, unit, worker_pid, worker_starttime, cgroup
            )
            if worker_exact or terminal_status:
                identity_lost_at = None
            elif identity_lost_at is None:
                identity_lost_at = time.monotonic()
            elif time.monotonic() - identity_lost_at >= 0.1:
                failure = "scope worker identity changed during execution"
                break
            if status is not None and not streams:
                if descendants:
                    failure = "scoped command left descendants"
                break
            if time.monotonic() >= work_deadline:
                failure = "scoped command deadline exhausted"
                break
        if not resource_snapshot:
            try:
                observed_events = _scope_resource_events(
                    cgroup, memory_before, pids_before
                )
            except BoundedProcessError as error:
                if error.errno != errno.ENODEV:
                    raise
                if resource_events:
                    resource_snapshot = True
                elif failure is None:
                    raise BoundedProcessError(
                        "scope resource receipt unavailable after collection"
                    ) from error
                else:
                    failure += "; scope resource receipt unavailable after collection"
            else:
                resource_events = tuple(
                    dict.fromkeys((*resource_events, *observed_events))
                )
                resource_snapshot = True
        if failure is None and process.returncode not in {None, 0}:
            failure = "scoped payload failed"
        if resource_events:
            resource_failure = (
                "scope resource limit was reached: " + ",".join(resource_events)
            )
            if failure is not None:
                raise BoundedProcessError(f"{failure}; {resource_failure}")
            raise BoundedProcessError(resource_failure)
        if failure is not None:
            raise BoundedProcessError(failure)
        if process.returncode != 0:
            raise BoundedProcessError("scoped payload failed")
        rows = _status_rows(captures["status"], complete=True)
        if len(rows) != (1 if _direct else 2):
            raise BoundedProcessError("scope exit status is invalid")
        exit_code = 0 if _direct else rows[1].get("exit-code")
        if rows[0] != ready_row or (
            not _direct
            and (
                set(rows[1]) != {"exit-code"}
                or type(exit_code) is not int
                or exit_code != 0
            )
        ):
            raise BoundedProcessError("scope exit status is invalid")
        if not _scope_quiescent(
            policy, worker_pid, worker_starttime, worker_pidfd, cgroup
        ):
            raise BoundedProcessError("scope worker survived collection")
        return bytes(captures["stdout"].retained)
    except BaseException as error:
        resource_diagnostics: list[str] = []
        if cgroup is not None and not resource_snapshot:
            try:
                observed_events = _scope_resource_events(
                    cgroup, memory_before, pids_before
                )
            except BoundedProcessError as snapshot_error:
                if snapshot_error.errno == errno.ENODEV and resource_events:
                    resource_snapshot = True
                else:
                    resource_diagnostics.append(
                        "scope resource receipt unavailable before cleanup"
                        if snapshot_error.errno == errno.ENODEV
                        else _bounded_error_summary(snapshot_error)
                    )
            else:
                resource_events = tuple(
                    dict.fromkeys((*resource_events, *observed_events))
                )
                resource_snapshot = True
        if resource_events:
            resource_diagnostics.append(
                "scope resource limit was reached: " + ",".join(resource_events)
            )
        if process is not None and tree is not None:
            scope_signal = (
                None
                if cgroup is None
                else lambda number: _scope_signal(
                    policy,
                    unit,
                    worker_pid,
                    worker_starttime,
                    cgroup,
                    worker_pidfd,
                    number,
                    hard_deadline,
                )
            )
            cleanup_error: BaseException | None = None
            try:
                _bounded_reap(
                    process,
                    tree,
                    selector,
                    streams,
                    captures,
                    hard_deadline,
                    scope_signal,
                )
            except BaseException as cleanup_failure:
                cleanup_error = cleanup_failure
            cleanup_diagnostics: list[str] = []
            scope_quiescent = False
            if cgroup is not None:
                try:
                    scope_quiescent = _scope_quiescent(
                        policy,
                        worker_pid,
                        worker_starttime,
                        worker_pidfd,
                        cgroup,
                    )
                except BaseException as diagnostic_failure:
                    cleanup_error = cleanup_error or diagnostic_failure
            if cgroup is not None and not scope_quiescent:
                cleanup_diagnostics.append("hard-contained scope survived cleanup")
            if process.returncode is None:
                cleanup_diagnostics.append("scope wrapper survived cleanup")
            if cleanup_error is not None:
                cleanup_diagnostics.append(_bounded_error_summary(cleanup_error))
            if cleanup_diagnostics or resource_diagnostics:
                diagnostics = [
                    _bounded_error_summary(error),
                    *resource_diagnostics,
                ]
                if cleanup_diagnostics:
                    diagnostics.append(
                        "cleanup=" + "; ".join(dict.fromkeys(cleanup_diagnostics))
                    )
                raise BoundedProcessError(
                    "; ".join(dict.fromkeys(diagnostics))
                ) from error
        elif resource_diagnostics:
            raise BoundedProcessError(
                "; ".join(
                    dict.fromkeys(
                        (_bounded_error_summary(error), *resource_diagnostics)
                    )
                )
            ) from error
        raise
    finally:
        if block_write >= 0:
            os.close(block_write)
        if status_read >= 0:
            os.close(status_read)
        for descriptor in list(streams):
            _close_stream(selector, streams, descriptor)
        fence_parent.close()
        selector.close()
        if tree is not None:
            tree.close()
        if worker_pidfd >= 0:
            os.close(worker_pidfd)
        if cgroup is not None:
            cgroup.close()


def scoped_direct_command(
    arguments: Sequence[str],
    policy: ScopePolicy,
    *,
    python: str,
    module_fd: int,
    systemd_run: str,
    systemctl: str,
    nice: str,
    ionice: str,
    prlimit: str,
    timeout: float,
    deadline: float,
    env: Mapping[str, str],
    pass_fds: Sequence[int],
    max_output_bytes: int,
    cleanup_reserve: float = 4.0,
    progress: Callable[[], None] | None = None,
) -> bytes:
    """Run an exact command directly after cgroup authority is proven."""

    if module_fd < 0:
        raise BoundedProcessError("invalid bounded-process module authority")
    return scoped_command(
        [python, f"/proc/self/fd/{module_fd}", *arguments],
        policy,
        systemd_run=systemd_run,
        systemctl=systemctl,
        nice=nice,
        ionice=ionice,
        prlimit=prlimit,
        timeout=timeout,
        deadline=deadline,
        env=env,
        pass_fds=tuple((*pass_fds, module_fd)),
        max_output_bytes=max_output_bytes,
        cleanup_reserve=cleanup_reserve,
        progress=progress,
        _direct=True,
    )


def _direct_scope_child(arguments: Sequence[str]) -> None:
    if len(arguments) < 5 or arguments[3] != "--":
        raise BoundedProcessError("invalid direct scope child")
    try:
        status_fd, block_fd = (int(arguments[0]), int(arguments[1]))
    except ValueError as error:
        raise BoundedProcessError("invalid direct scope child descriptors") from error
    unit = arguments[2]
    command_arguments = list(arguments[4:])
    if (
        status_fd <= 2
        or block_fd <= 2
        or status_fd == block_fd
        or not command_arguments
        or not unit.startswith("llm-guard-rebuild-")
        or not unit.endswith(".scope")
        or Path(unit).name != unit
    ):
        raise BoundedProcessError("invalid direct scope child authority")
    payload = json.dumps(
        {"child-pid": os.getpid()},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii") + b"\n"
    if os.write(status_fd, payload) != len(payload):
        raise BoundedProcessError("direct scope child status write failed")
    os.close(status_fd)
    if os.read(block_fd, 2) != b"G":
        raise BoundedProcessError("direct scope child release failed")
    os.close(block_fd)
    os.execvpe(command_arguments[0], command_arguments, os.environ)


def command(
    arguments: Sequence[str],
    timeout: float = 20,
    input_text: str | None = None,
    *,
    deadline: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    cleanup_reserve: float = 2.0,
    progress: Callable[[], None] | None = None,
) -> str:
    """Run one complete process tree with capped concurrent output and hard cleanup."""

    if not arguments or timeout <= 0 or cleanup_reserve < 0:
        raise BoundedProcessError("invalid bounded command")
    input_payload = input_text.encode() if input_text is not None else b""
    if len(input_payload) > _MAX_STREAM_BYTES:
        raise BoundedProcessError(f"command input exceeded bound: {arguments[0]}")
    started = time.monotonic()
    hard_deadline = started + timeout
    if deadline is not None:
        hard_deadline = min(hard_deadline, deadline)
    total = hard_deadline - started
    if total <= 0:
        raise BoundedProcessError("operation deadline exhausted")
    reserve = min(cleanup_reserve, max(0.0, total / 2))
    work_deadline = hard_deadline - reserve
    if work_deadline <= time.monotonic():
        raise BoundedProcessError("insufficient command budget after cleanup reserve")

    _enable_subreaper()
    owner_pid = os.getpid()
    baseline = _direct_child_identities(owner_pid)
    try:
        process = subprocess.Popen(
            list(arguments),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            pass_fds=tuple(pass_fds),
        )
    except (OSError, ValueError) as error:
        raise BoundedProcessError(f"command spawn failed: {arguments[0]}") from error

    tree = _ProcessTree(process, baseline)
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[str, object]] = {}
    captures = {"stdout": _Capture(bytearray()), "stderr": _Capture(bytearray())}
    failure: str | None = None
    input_offset = 0
    try:
        assert process.stdout is not None and process.stderr is not None
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
            streams[descriptor] = (name, stream)

        while True:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    input_offset += os.write(
                        process.stdin.fileno(),
                        input_payload[input_offset : input_offset + _READ_BYTES],
                    )
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    process.stdin.close()
                if input_offset >= len(input_payload) and not process.stdin.closed:
                    process.stdin.close()
            if progress is not None:
                progress()
            tree.scan()
            _drain_once(
                selector,
                streams,
                captures,
                min(_POLL_SECONDS, max(0.0, work_deadline - time.monotonic())),
            )
            status = process.poll()
            tree.reap_adopted()
            descendants = tree.descendants(tree.scan())
            if captures["stdout"].truncated or captures["stderr"].truncated:
                failure = "command output exceeded bound"
                break
            if status is not None and not streams:
                if descendants:
                    failure = (
                        f"command failed ({status}) and left surviving descendants"
                        if status
                        else "command left surviving descendants"
                    )
                break
            if time.monotonic() >= work_deadline:
                failure = "command deadline exhausted"
                break
        if failure is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            _bounded_reap(
                process,
                tree,
                selector,
                streams,
                captures,
                hard_deadline,
            )
        elif process.returncode is None:
            process.wait(timeout=max(0.001, hard_deadline - time.monotonic()))
    except BaseException as error:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            _bounded_reap(
                process,
                tree,
                selector,
                streams,
                captures,
                hard_deadline,
            )
        except BaseException as cleanup_error:
            raise cleanup_error from error
        raise
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        for descriptor in list(streams):
            _close_stream(selector, streams, descriptor)
        selector.close()
        tree.close()

    stdout = _render(captures["stdout"])
    stderr = _render(captures["stderr"])
    if failure is not None:
        raise BoundedProcessError(
            f"{failure}: {arguments[0]}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    if process.returncode != 0:
        raise BoundedProcessError(
            f"command failed ({process.returncode}): {arguments[0]}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    try:
        return bytes(captures["stdout"].retained).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise BoundedProcessError(
            f"command stdout is not valid UTF-8: {arguments[0]}"
        ) from error


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "--direct-scope-child":
        raise SystemExit(64)
    _direct_scope_child(sys.argv[2:])
