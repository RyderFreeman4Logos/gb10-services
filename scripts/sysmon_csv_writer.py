#!/usr/bin/env python3
"""Append bounded sysmon CSV lines and checkpoint them infrequently."""

from __future__ import annotations

import os
from pathlib import Path
import re
import select
import signal
import stat
import sys
import time
from collections.abc import Callable

MAX_LINE_BYTES = 4096
CHECKPOINT_SECONDS = 60.0
_DATE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{4},")


class CsvWriter:
    """Own one verified directory descriptor and checkpoint complete CSV lines."""

    def __init__(
        self,
        directory: Path,
        header: str,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not header or "\n" in header or len(header.encode("ascii")) > MAX_LINE_BYTES:
            raise ValueError("invalid sysmon CSV header")
        resolved = directory.resolve(strict=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        self._dir_fd = os.open(resolved, flags)
        directory_stat = os.fstat(self._dir_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or directory_stat.st_mode & 0o022
        ):
            os.close(self._dir_fd)
            raise PermissionError("unsafe sysmon log directory")
        self._header = (header + "\n").encode("ascii")
        self._monotonic = monotonic
        self._last_attempt = monotonic()
        self._filename: str | None = None
        self._file_fd: int | None = None
        self._dirty = False

    def close(self) -> None:
        if self._file_fd is not None:
            os.close(self._file_fd)
            self._file_fd = None
        os.close(self._dir_fd)

    def _write_all(self, data: bytes) -> None:
        assert self._file_fd is not None
        view = memoryview(data)
        while view:
            written = os.write(self._file_fd, view)
            if written <= 0:
                raise OSError("short sysmon CSV write")
            view = view[written:]

    @staticmethod
    def _validate_csv_stat(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
        ):
            raise PermissionError("unsafe sysmon CSV file")

    def _open_date(self, date: str, hhmmss: str) -> bool:
        filename = f"sysmon_{date}.csv"
        if self._filename == filename:
            return False
        if self._file_fd is not None:
            if self._dirty:
                self._checkpoint()
            os.close(self._file_fd)
            self._file_fd = None

        created = False
        try:
            existing_fd = os.open(
                filename,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self._dir_fd,
            )
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                info = os.fstat(existing_fd)
                self._validate_csv_stat(info)
                current_header = os.read(existing_fd, MAX_LINE_BYTES + 1).split(b"\n", 1)[0]
                if current_header != self._header[:-1]:
                    backup = f"sysmon_{date}.pre-v6.{hhmmss}.csv"
                    try:
                        os.stat(backup, dir_fd=self._dir_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileExistsError("sysmon CSV rotation target already exists")
                    os.rename(filename, backup, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)
                    created = True
            finally:
                os.close(existing_fd)

        flags = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        if created or existing_fd is None:
            flags |= os.O_CREAT | os.O_EXCL
            self._file_fd = os.open(filename, flags, 0o644, dir_fd=self._dir_fd)
            self._write_all(self._header)
            created = True
        else:
            self._file_fd = os.open(filename, flags, dir_fd=self._dir_fd)
            info = os.fstat(self._file_fd)
            try:
                self._validate_csv_stat(info)
            except PermissionError:
                os.close(self._file_fd)
                self._file_fd = None
                raise
        self._filename = filename
        return created

    def write(self, row: str) -> None:
        data = row.encode("ascii")
        if not data.endswith(b"\n") or len(data) > MAX_LINE_BYTES or b"\r" in data:
            raise ValueError("invalid or oversized sysmon CSV line")
        match = _DATE.match(row)
        if match is None:
            raise ValueError("invalid sysmon CSV timestamp")
        date = match.group(1)
        hhmmss = row[11:13] + row[14:16] + row[17:19]
        new_file = self._open_date(date, hhmmss)
        self._write_all(data)
        self._dirty = True
        now = self._monotonic()
        if new_file or now - self._last_attempt >= CHECKPOINT_SECONDS:
            self._checkpoint()

    def flush(self) -> None:
        if self._dirty:
            self._checkpoint()

    def _checkpoint(self) -> None:
        assert self._file_fd is not None
        self._last_attempt = self._monotonic()
        os.fdatasync(self._file_fd)
        os.fsync(self._dir_fd)
        self._dirty = False


def _process_starttime(pid: int) -> int:
    payload = Path(f"/proc/{pid}/stat").read_text()
    return int(payload[payload.rfind(")") + 2:].split()[19])


def _send_row(row: str) -> int:
    """One atomic nonblocking pipe write; reject overload instead of hanging."""
    data = (row + "\n").encode("ascii")
    if len(data) > min(MAX_LINE_BYTES, os.fpathconf(1, "PC_PIPE_BUF")):
        raise ValueError("oversized CSV row")
    os.set_blocking(1, False)
    if os.write(1, data) != len(data):
        raise OSError("incomplete CSV enqueue")
    return 0


def _stop_writer(pid: int, starttime: int) -> int:
    """0: drained, 1: killed and exited, 2: unconfirmed; never wait unbounded."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return 0
    try:
        if _process_starttime(pid) != starttime:
            return 2
        if select.select([fd], [], [], 2.0)[0]:
            return 0
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        return 1 if select.select([fd], [], [], 1.0)[0] else 2
    finally:
        os.close(fd)


def main() -> int:
    writer: CsvWriter | None = None
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--identity":
            print(_process_starttime(int(sys.argv[2])))
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "--send":
            return _send_row(sys.argv[2])
        if len(sys.argv) == 4 and sys.argv[1] == "--stop":
            return _stop_writer(int(sys.argv[2]), int(sys.argv[3]))
        if len(sys.argv) != 3:
            return 64
        # The owner closes the pipe for graceful shutdown; it alone enforces
        # the drain deadline and kills a wedged disk writer using a pidfd.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        writer = CsvWriter(Path(sys.argv[1]), sys.argv[2])
        print("READY", flush=True)
        while True:
            line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
            if not line:
                writer.flush()
                return 0
            if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                raise ValueError("invalid or oversized sysmon CSV input")
            writer.write(line.decode("ascii"))
    except (OSError, UnicodeError, ValueError) as error:
        # No row, pathname, process arguments, or exception payload in receipts.
        print(f"sysmon CSV failure: {type(error).__name__} errno={getattr(error, 'errno', None)}", file=sys.stderr)
        return 2
    finally:
        if writer is not None:
            writer.close()


if __name__ == "__main__":
    raise SystemExit(main())
