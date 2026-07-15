"""Secure durable-file primitives for embedding activation transactions."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


class ActivationStorageError(RuntimeError):
    """A transaction path or filesystem operation was not safe."""


def fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def secure_directory(path: Path, mode: int) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise ActivationStorageError(f"unsafe activation directory: {path}")


def secure_regular(path: Path, mode: int | None = None) -> os.stat_result:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise ActivationStorageError(f"unsafe activation file: {path}")
    return metadata


def atomic_write(
    path: Path, payload: bytes, mode: int, *, replace: bool = True
) -> None:
    if path.exists() or path.is_symlink():
        if not replace:
            raise ActivationStorageError(
                f"activation file already exists: {path.name}"
            )
        secure_regular(path)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fchmod(descriptor, mode)
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
    fsync_directory(path.parent)


def atomic_json(
    path: Path, payload: dict[str, Any], *, replace: bool = True
) -> None:
    data = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()
    atomic_write(path, data, 0o600, replace=replace)
