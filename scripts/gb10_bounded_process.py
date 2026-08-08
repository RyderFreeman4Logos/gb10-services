from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["command", "remaining"]

_MAX_STREAM_BYTES = 4 * 1024 * 1024
_READ_BYTES = 64 * 1024
_POLL_SECONDS = 0.02


class BoundedProcessError(RuntimeError):
    """A command failed or its complete process tree could not be contained."""


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

    def signal(self, number: int) -> None:
        table = self.scan()
        leader_row = table.get(self.leader.pid)
        if (
            self.leader.returncode is None
            and leader_row is not None
            and leader_row[1] == self.root_starttime
        ):
            try:
                os.killpg(self.leader.pid, number)
            except ProcessLookupError:
                pass
        for owned in self.descendants(table):
            if owned.pidfd is None or not hasattr(signal, "pidfd_send_signal"):
                continue
            try:
                signal.pidfd_send_signal(owned.pidfd, number, None, 0)
            except ProcessLookupError:
                pass

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
    total: int = 0
    truncated: bool = False

    def append(self, payload: bytes) -> None:
        self.total += len(payload)
        available = max(0, _MAX_STREAM_BYTES - len(self.retained))
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


def _bounded_reap(
    process: subprocess.Popen[bytes],
    tree: _ProcessTree,
    selector: selectors.BaseSelector,
    streams: dict[int, tuple[str, object]],
    captures: dict[str, _Capture],
    hard_deadline: float,
) -> None:
    now = time.monotonic()
    term_deadline = min(hard_deadline, now + min(2.0, max(0.0, (hard_deadline - now) / 2)))
    tree.signal(signal.SIGTERM)
    while time.monotonic() < term_deadline:
        _drain_once(selector, streams, captures, min(_POLL_SECONDS, term_deadline - time.monotonic()))
        process.poll()
        tree.reap_adopted()
        if not tree.survivors() and not streams:
            break
    if tree.survivors():
        tree.signal(signal.SIGKILL)
    while time.monotonic() < hard_deadline:
        _drain_once(selector, streams, captures, min(_POLL_SECONDS, hard_deadline - time.monotonic()))
        process.poll()
        tree.reap_adopted()
        if not tree.survivors() and not streams:
            break
    for descriptor in list(streams):
        _close_stream(selector, streams, descriptor)
    if process.returncode is None:
        try:
            process.wait(timeout=max(0.001, hard_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    tree.reap_adopted()
    survivors = tree.survivors()
    if survivors:
        raise BoundedProcessError(
            "subprocess cleanup deadline exhausted; survivors="
            + ",".join(str(pid) for pid in survivors)
        )


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
    return stdout
