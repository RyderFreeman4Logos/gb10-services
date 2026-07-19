#!/usr/bin/python3
"""Safely verify and publish immutable pre-push review receipts."""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import sys
from pathlib import Path


__all__ = ["StoreError", "main", "run"]


MAX_RECEIPT_BYTES = 4 * 1024 * 1024
RECEIPT_SUFFIX = ".receipt"
LOCK_NAME = ".lock"


class StoreError(RuntimeError):
    pass


def same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def validate_directory(fd: int, path: Path) -> None:
    opened = os.fstat(fd)
    try:
        named = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise StoreError("pre-push receipt directory path is unavailable") from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or not same_identity(opened, named)
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        raise StoreError("pre-push receipt directory identity or mode is unsafe")
    # Directory link counts vary by filesystem and are not an identity boundary.


def open_directory(path: Path) -> int:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise StoreError("pre-push receipt directory cannot be created") from error
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise StoreError("pre-push receipt directory is unsafe") from error
    try:
        validate_directory(fd, path)
    except Exception:
        os.close(fd)
        raise
    return fd


def validate_regular_entry(
    directory_fd: int,
    name: str,
    opened: os.stat_result,
    *,
    label: str,
) -> None:
    try:
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise StoreError(f"{label} path is unavailable") from error
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or not same_identity(opened, named)
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink != 1
        or named.st_nlink != 1
    ):
        raise StoreError(f"{label} metadata is unsafe")


def open_entry(
    directory_fd: int,
    name: str,
    flags: int,
    *,
    label: str,
    create: bool = False,
) -> int:
    open_flags = flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if create:
        open_flags |= os.O_CREAT
    try:
        fd = os.open(name, open_flags, 0o600, dir_fd=directory_fd)
    except OSError as error:
        raise StoreError(f"{label} is missing or unsafe") from error
    try:
        validate_regular_entry(directory_fd, name, os.fstat(fd), label=label)
    except Exception:
        os.close(fd)
        raise
    return fd


def read_bounded(fd: int, *, label: str) -> bytes:
    before = os.fstat(fd)
    if before.st_size < 0 or before.st_size > MAX_RECEIPT_BYTES:
        raise StoreError(f"{label} exceeds the bounded receipt size")
    chunks: list[bytes] = []
    remaining = MAX_RECEIPT_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    after = os.fstat(fd)
    if (
        len(data) > MAX_RECEIPT_BYTES
        or len(data) != before.st_size
        or after.st_size != before.st_size
        or not same_identity(before, after)
        or after.st_nlink != 1
    ):
        raise StoreError(f"{label} changed or exceeded its bounded read")
    return data


def read_candidate(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise StoreError("candidate pre-push receipt is missing or unsafe") from error
    try:
        opened = os.fstat(fd)
        try:
            named = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise StoreError("candidate pre-push receipt path is unavailable") from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or not same_identity(opened, named)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or named.st_nlink != 1
        ):
            raise StoreError("candidate pre-push receipt metadata is unsafe")
        data = read_bounded(fd, label="candidate pre-push receipt")
        current = os.stat(path, follow_symlinks=False)
        if not same_identity(opened, current):
            raise StoreError("candidate pre-push receipt path changed")
        return data
    finally:
        os.close(fd)


def lock_store(directory_fd: int) -> int:
    lock_fd = open_entry(
        directory_fd,
        LOCK_NAME,
        os.O_RDWR,
        label="pre-push receipt lock",
        create=True,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        validate_regular_entry(
            directory_fd,
            LOCK_NAME,
            os.fstat(lock_fd),
            label="pre-push receipt lock",
        )
    except Exception:
        os.close(lock_fd)
        raise
    return lock_fd


def read_existing(directory_fd: int, receipt_name: str) -> bytes | None:
    try:
        receipt_fd = open_entry(
            directory_fd,
            receipt_name,
            os.O_RDONLY,
            label="existing pre-push receipt",
        )
    except StoreError as error:
        try:
            os.stat(receipt_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as stat_error:
            raise StoreError("existing pre-push receipt state is unsafe") from stat_error
        raise error
    try:
        data = read_bounded(receipt_fd, label="existing pre-push receipt")
        validate_regular_entry(
            directory_fd,
            receipt_name,
            os.fstat(receipt_fd),
            label="existing pre-push receipt",
        )
        return data
    finally:
        os.close(receipt_fd)


def verify_existing(directory_fd: int, receipt_name: str, candidate: bytes) -> None:
    existing = read_existing(directory_fd, receipt_name)
    if existing is not None and existing != candidate:
        raise StoreError("existing pre-push receipt is stale or tampered")


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise StoreError("temporary pre-push receipt write failed")
        view = view[written:]


def unlink_created_entry(directory_fd: int, name: str, identity: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise StoreError("temporary pre-push receipt state is unsafe") from error
    if not same_identity(identity, current):
        raise StoreError("temporary pre-push receipt path changed")
    os.unlink(name, dir_fd=directory_fd)


def publish(directory_fd: int, receipt_name: str, candidate: bytes) -> None:
    existing = read_existing(directory_fd, receipt_name)
    if existing is not None:
        if existing != candidate:
            raise StoreError("existing pre-push receipt is stale or tampered")
        return

    temporary_name = f".receipt-{os.getpid()}-{secrets.token_hex(12)}"
    temporary_fd = open_entry(
        directory_fd,
        temporary_name,
        os.O_WRONLY | os.O_EXCL,
        label="temporary pre-push receipt",
        create=True,
    )
    temporary_identity = os.fstat(temporary_fd)
    try:
        try:
            write_all(temporary_fd, candidate)
            os.fsync(temporary_fd)
            after_write = os.fstat(temporary_fd)
            if after_write.st_size != len(candidate) or not same_identity(
                temporary_identity, after_write
            ):
                raise StoreError("temporary pre-push receipt changed while writing")
            validate_regular_entry(
                directory_fd,
                temporary_name,
                after_write,
                label="temporary pre-push receipt",
            )
        finally:
            os.close(temporary_fd)
        try:
            os.link(
                temporary_name,
                receipt_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            verify_existing(directory_fd, receipt_name, candidate)
        unlink_created_entry(directory_fd, temporary_name, temporary_identity)
        os.fsync(directory_fd)
        verify_existing(directory_fd, receipt_name, candidate)
    except Exception:
        unlink_created_entry(directory_fd, temporary_name, temporary_identity)
        raise


def run(command: str, directory: Path, candidate_path: Path, expected_sha: str) -> None:
    if command not in {"verify", "publish"}:
        raise StoreError("receipt-store command is invalid")
    if len(expected_sha) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha
    ):
        raise StoreError("candidate pre-push receipt digest is malformed")
    candidate = read_candidate(candidate_path)
    if hashlib.sha256(candidate).hexdigest() != expected_sha:
        raise StoreError("candidate pre-push receipt digest differs from its contents")
    receipt_name = expected_sha + RECEIPT_SUFFIX

    directory_fd = open_directory(directory)
    try:
        lock_fd = lock_store(directory_fd)
        try:
            validate_directory(directory_fd, directory)
            if command == "verify":
                verify_existing(directory_fd, receipt_name, candidate)
            else:
                publish(directory_fd, receipt_name, candidate)
            validate_directory(directory_fd, directory)
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory_fd)


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "ERROR: receipt-store requires a command, directory, candidate, and digest",
            file=sys.stderr,
        )
        return 1
    try:
        run(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    except (OSError, StoreError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
