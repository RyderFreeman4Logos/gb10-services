#!/usr/bin/env python3
"""Bounded process and owner-only evidence primitives for embedding verification."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from ctypes import CDLL, get_errno
from pathlib import Path
from typing import Any, NoReturn

__all__ = [
    "command",
    "fail",
    "json_file",
    "json_text",
    "read_nofollow",
    "remaining",
    "secure_write_json",
    "text_file",
    "validate_evidence_dir",
]

_MAX_COMMAND_OUTPUT = 4 * 1024 * 1024
_MAX_JSON_BYTES = 16 * 1024 * 1024
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


def command(
    argv: list[str],
    timeout: float = 20,
    input_text: str | None = None,
    *,
    deadline: float | None = None,
) -> str:
    """Run one bounded process group and reap it on every timeout path."""

    effective_timeout = timeout
    if deadline is not None:
        effective_timeout = min(timeout, remaining(deadline, timeout))
    _enable_child_subreaper()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=effective_timeout)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        _reap_process_group(process.pid)
        raise RuntimeError(
            f"bounded command timed out: {Path(argv[0]).name}"
        ) from error
    if len(stdout.encode()) > _MAX_COMMAND_OUTPUT or len(stderr.encode()) > _MAX_COMMAND_OUTPUT:
        fail(f"command output exceeded bound: {Path(argv[0]).name}")
    if process.returncode != 0:
        excerpt = stderr[-500:].replace("\n", " ")
        fail(
            f"command failed ({process.returncode}): {Path(argv[0]).name}: {excerpt}"
        )
    return stdout


def read_nofollow(path: Path, maximum: int, *, owner_only: bool = False) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            fail(f"unsafe or oversized file: {path.name}")
        if owner_only and (
            metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 != 0
        ):
            fail(f"file is not owner-only: {path.name}")
        chunks: list[bytes] = []
        budget = maximum + 1
        while budget > 0:
            chunk = os.read(descriptor, min(65536, budget))
            if not chunk:
                break
            chunks.append(chunk)
            budget -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            fail(f"file exceeded read bound: {path.name}")
        return payload
    finally:
        os.close(descriptor)


def text_file(path: Path, maximum: int, *, owner_only: bool = False) -> str:
    try:
        return read_nofollow(path, maximum, owner_only=owner_only).decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"non-UTF-8 file: {path.name}") from error


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def json_text(text: str) -> Any:
    if len(text.encode()) > _MAX_JSON_BYTES:
        fail("JSON exceeds parser bound")
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicates,
        parse_constant=lambda value: fail(f"non-finite JSON number: {value}"),
    )


def json_file(path: Path, *, owner_only: bool = False) -> Any:
    return json_text(text_file(path, _MAX_JSON_BYTES, owner_only=owner_only))


def validate_evidence_dir(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        fail("evidence path must be a real directory")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 != 0:
        fail("evidence directory must be owner-only")


def secure_write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        fail(f"refusing to replace existing evidence target: {path.name}")
    data = (json.dumps(payload, sort_keys=True, allow_nan=False) + "\n").encode()
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
