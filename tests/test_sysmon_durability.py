from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sysmon_csv_writer", ROOT / "scripts" / "sysmon_csv_writer.py"
)
assert SPEC is not None and SPEC.loader is not None
writer_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(writer_module)

HEADER = "timestamp,value"
ROW = "2023-11-14T22:13:20+0000,ok\n"


class SysmonCsvDurabilityTests(unittest.TestCase):
    def test_writer_ignores_term_and_int_until_stdin_is_drained(self) -> None:
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(shutdown_signal=shutdown_signal), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                process = subprocess.Popen(
                    ["/usr/bin/python3", "-I", str(ROOT / "scripts" / "sysmon_csv_writer.py"), str(root), HEADER],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline(), b"READY\n")
                os.kill(process.pid, shutdown_signal)
                assert process.stdin is not None
                process.stdin.write(ROW.encode("ascii"))
                process.stdin.close()
                process.stdin = None
                _, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(
                    (root / "sysmon_2023-11-14.csv").read_text(), HEADER + "\n" + ROW
                )

    def test_new_and_rotated_files_sync_complete_csv_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events: list[tuple[str, Path, str]] = []
            real_fdatasync = os.fdatasync
            real_fsync = os.fsync

            def record_file(fd: int) -> None:
                path = Path(os.readlink(f"/proc/self/fd/{fd}"))
                events.append(("file", path, path.read_text()))
                real_fdatasync(fd)

            def record_directory(fd: int) -> None:
                events.append(("directory", Path(os.readlink(f"/proc/self/fd/{fd}")), events[-1][2]))
                real_fsync(fd)

            with patch.object(writer_module.os, "fdatasync", record_file), patch.object(
                writer_module.os, "fsync", record_directory
            ):
                writer = writer_module.CsvWriter(root, HEADER, monotonic=lambda: 10.0)
                writer.write(ROW)
                first = root / "sysmon_2023-11-14.csv"
                self.assertEqual(first.read_text(), HEADER + "\n" + ROW)
                self.assertEqual(events[0], ("file", first, HEADER + "\n" + ROW))
                self.assertEqual(events[1], ("directory", root, HEADER + "\n" + ROW))

                writer.write("2023-11-15T00:00:00+0000,next\n")
                rotated = root / "sysmon_2023-11-15.csv"
                self.assertEqual(rotated.read_text(), HEADER + "\n2023-11-15T00:00:00+0000,next\n")
                rotated_content = HEADER + "\n2023-11-15T00:00:00+0000,next\n"
                self.assertEqual(events[-2], ("file", rotated, rotated_content))
                self.assertEqual(events[-1], ("directory", root, rotated_content))

    def test_date_change_syncs_unsynced_tail_before_closing_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events: list[tuple[str, str]] = []
            real_fdatasync = os.fdatasync
            real_close = os.close
            old_path = root / "sysmon_2023-11-14.csv"
            old_fd: int | None = None

            def record_sync(fd: int) -> None:
                if fd == old_fd:
                    events.append(("sync", Path(os.readlink(f"/proc/self/fd/{fd}")).name))
                real_fdatasync(fd)

            def record_close(fd: int) -> None:
                if fd == old_fd:
                    events.append(("close", Path(os.readlink(f"/proc/self/fd/{fd}")).name))
                real_close(fd)

            with patch.object(writer_module.os, "fdatasync", record_sync), patch.object(
                writer_module.os, "close", record_close
            ):
                writer = writer_module.CsvWriter(root, HEADER, monotonic=lambda: 0.0)
                writer.write(ROW)
                old_fd = writer._file_fd
                writer.write("2023-11-14T22:13:21+0000,unsynced-tail\n")
                events.clear()
                writer.write("2023-11-15T00:00:00+0000,next-day\n")
                self.assertEqual(
                    old_path.read_text(),
                    HEADER + "\n" + ROW + "2023-11-14T22:13:21+0000,unsynced-tail\n",
                )
                self.assertLess(events.index(("sync", old_path.name)), events.index(("close", old_path.name)))
                writer.close()

    def test_sync_failure_is_reported_not_acknowledged(self) -> None:
        for syscall in ("fdatasync", "fsync"):
            with self.subTest(syscall=syscall), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with patch.object(writer_module.os, syscall, side_effect=OSError("disk error")):
                    writer = writer_module.CsvWriter(root, HEADER, monotonic=lambda: 10.0)
                    with self.assertRaises(OSError):
                        writer.write(ROW)
                    writer.close()

    def test_periodic_checkpoint_is_throttled_by_monotonic_attempt_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clock = [0.0]
            attempts: list[float] = []
            real_fdatasync = os.fdatasync

            def checkpoint(fd: int) -> None:
                attempts.append(clock[0])
                real_fdatasync(fd)

            with patch.object(writer_module.os, "fdatasync", checkpoint), patch.object(
                writer_module.time, "time", side_effect=AssertionError("wall clock used")
            ):
                writer = writer_module.CsvWriter(root, HEADER, monotonic=lambda: clock[0])
                writer.write(ROW)  # Initial durable creation.
                attempts.clear()
                for now in (1.0, 20.0, 59.9, 60.0, 60.1, 119.9, 120.0):
                    clock[0] = now
                    writer.write(f"2023-11-14T22:13:{int(now) % 60:02d}+0000,{now}\n")
                self.assertEqual(attempts, [60.0, 120.0])

    def test_failed_periodic_attempt_is_throttled_until_monotonic_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clock = [0.0]
            attempts: list[float] = []
            writer = writer_module.CsvWriter(root, HEADER, monotonic=lambda: clock[0])
            writer.write(ROW)

            def fail_checkpoint(_fd: int) -> None:
                attempts.append(clock[0])
                raise OSError("disk error")

            with patch.object(
                writer_module.os,
                "fdatasync",
                side_effect=fail_checkpoint,
            ):
                for now in (60.0, 60.1, 119.9, 120.0):
                    clock[0] = now
                    if now in (60.0, 120.0):
                        with self.assertRaises(OSError):
                            writer.write(f"2023-11-14T22:13:{int(now) % 60:02d}+0000,{now}\n")
                    else:
                        writer.write(f"2023-11-14T22:13:{int(now) % 60:02d}+0000,{now}\n")
            self.assertEqual(attempts, [60.0, 120.0])

    def test_csv_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "outside.csv"
            target.write_text("private\n")
            (root / "sysmon_2023-11-14.csv").symlink_to(target)
            writer = writer_module.CsvWriter(root, HEADER, monotonic=lambda: 10.0)
            with self.assertRaises(OSError):
                writer.write(ROW)

    def test_existing_file_restart_waits_for_periodic_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = writer_module.CsvWriter(root, HEADER, monotonic=lambda: 0.0)
            first.write(ROW)
            attempts: list[float] = []
            real_fdatasync = os.fdatasync

            def checkpoint(fd: int) -> None:
                attempts.append(100.0)
                real_fdatasync(fd)

            with patch.object(writer_module.os, "fdatasync", checkpoint):
                restarted = writer_module.CsvWriter(root, HEADER, monotonic=lambda: 100.0)
                restarted.write(ROW)
                self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()