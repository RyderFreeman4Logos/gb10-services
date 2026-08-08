#!/usr/bin/env python3
"""Shared bounded process execution for GB10 transactional helpers."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from ctypes import CDLL, get_errno
from pathlib import Path
from typing import NoReturn

__all__ = ["command", "remaining"]

_MAX_COMMAND_OUTPUT = 4 * 1024 * 1024
_SUBREAPER_ENABLED = False


def fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def remaining(deadline: float, maximum: float = 20.0) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        fail("verification deadline exhausted")
    return max(0.05, min(maximum, value))


def _enable_child_subreaper() -> None:
    """Make timed-out grandchildren reapable instead of leaking to init."""

    global _SUBREAPER_ENABLED
    if _SUBREAPER_ENABLED:
        return
    if sys.platform != "linux":
        fail("bounded process groups require Linux child-subreaper support")
    libc = CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        fail(f"could not enable child subreaper (errno {get_errno()})")
    _SUBREAPER_ENABLED = True


def _reap_process_group(process_group: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        reaped = False
        while True:
            try:
                pid, _status = os.waitpid(-process_group, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                break
            reaped = True
        if not reaped:
            time.sleep(0.01)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    while True:
        try:
            pid, _status = os.waitpid(-process_group, 0)
        except ChildProcessError:
            return
        if pid <= 0:
            return


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    _reap_process_group(process.pid)


def command(
    argv: list[str],
    timeout: float = 20,
    input_text: str | None = None,
    *,
    deadline: float | None = None,
    cwd: str | bytes | os.PathLike[str] | os.PathLike[bytes] | None = None,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> str:
    """Run one process group with deadline and in-flight output bounds."""

    effective_timeout = timeout
    if deadline is not None:
        effective_timeout = min(timeout, remaining(deadline, timeout))
    _enable_child_subreaper()
    input_payload = input_text.encode() if input_text is not None else b""
    if len(input_payload) > _MAX_COMMAND_OUTPUT:
        fail(f"command input exceeded bound: {Path(argv[0]).name}")
    process: subprocess.Popen[bytes] = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
        cwd=cwd,
        env=env,
        pass_fds=pass_fds,
    )
    if process.stdout is None or process.stderr is None:
        _terminate_process_group(process)
        fail("bounded command pipes were not created")
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    deadline_monotonic = time.monotonic() + effective_timeout
    try:
        for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream.fileno(), selectors.EVENT_READ, (label, stream))
        input_offset = 0
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(
                process.stdin.fileno(), selectors.EVENT_WRITE, ("stdin", process.stdin)
            )
        while selector.get_map():
            wait = deadline_monotonic - time.monotonic()
            if wait <= 0:
                raise subprocess.TimeoutExpired(argv, effective_timeout)
            for key, _events in selector.select(min(0.1, wait)):
                label, stream = key.data
                if label == "stdin":
                    try:
                        written = os.write(
                            stream.fileno(), input_payload[input_offset : input_offset + 65536]
                        )
                    except BrokenPipeError:
                        written = len(input_payload) - input_offset
                    input_offset += written
                    if input_offset >= len(input_payload):
                        selector.unregister(key.fileobj)
                        stream.close()
                    continue
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    stream.close()
                    continue
                target = stdout if label == "stdout" else stderr
                target.extend(chunk)
                if len(target) > _MAX_COMMAND_OUTPUT:
                    raise BufferError
        wait = deadline_monotonic - time.monotonic()
        if wait <= 0:
            raise subprocess.TimeoutExpired(argv, effective_timeout)
        process.wait(timeout=wait)
        # A successful or failed direct client must not leave descendants behind.
        # The process may already be reaped; its process group remains the stable
        # authority for terminating and reaping any subreaper-adopted children.
        _terminate_process_group(process)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise RuntimeError(
            f"bounded command timed out: {Path(argv[0]).name}"
        ) from error
    except BufferError as error:
        _terminate_process_group(process)
        raise RuntimeError(
            f"command output exceeded bound: {Path(argv[0]).name}"
        ) from error
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        selector.close()
    if process.returncode != 0:
        excerpt = stderr[-500:].decode("utf-8", errors="replace").replace("\n", " ")
        fail(
            f"command failed ({process.returncode}): {Path(argv[0]).name}: {excerpt}"
        )
    try:
        return stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(
            f"command output was not UTF-8: {Path(argv[0]).name}"
        ) from error
