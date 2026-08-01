from __future__ import annotations

import errno
import importlib.util
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "llm_guard_proxy_loop_recovery_smoke",
    ROOT / "scripts" / "llm_guard_proxy_loop_recovery_smoke.py",
)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def attempt(
    number: int,
    phase: str,
    *,
    budget: int,
    salvage: bool = False,
    private: bool = False,
    loop: bool = False,
) -> dict:
    return {
        "number": number,
        "phase": phase,
        "thinking_budget": budget,
        "salvage_material_present": salvage,
        "private_prefix_present": private,
        "loop_tail_present": loop,
    }


def passing_summary() -> dict:
    return {
        "attempts": [
            attempt(1, "positive", budget=smoke.THINKING_BUDGET),
            attempt(
                2,
                "positive",
                budget=0,
                salvage=True,
                private=True,
                loop=True,
            ),
            attempt(3, "fresh", budget=smoke.THINKING_BUDGET),
        ],
        "fixture_errors": [],
        "positive_pass": True,
        "fresh_negative_pass": True,
        "execution_error": None,
        "process_cleanup": {
            "proxy_exited": True,
            "unexpected_exit": False,
            "graceful_stop": True,
            "forced_kill": False,
            "spawn_identity_captured": True,
            "group_quiesced": True,
            "session_quiesced": True,
            "residual_producer_count_before_kill": 0,
            "residual_producer_count_final": 0,
        },
        "fixture_cleanup": {
            "server_stopped": True,
            "shutdown_stopped": True,
            "handlers_quiesced": True,
        },
        "port_cleanup": {"guard_rebindable": True, "fake_rebindable": True},
        "sensitive_marker_leak_count_all_fixture_files": 0,
        "scan_errors": 0,
    }


class LoopRecoveryFinalizationTests(unittest.TestCase):
    def test_final_acceptance_checks_every_attempt_privacy_and_budget(self) -> None:
        legal_shadow = passing_summary()
        legal_shadow["attempts"].append(
            attempt(4, "positive", budget=smoke.THINKING_BUDGET)
        )
        self.assertFalse(smoke.acceptance_errors(legal_shadow))

        private_shadow = passing_summary()
        private_shadow["attempts"].append(
            attempt(
                4,
                "positive",
                budget=smoke.THINKING_BUDGET,
                private=True,
                loop=True,
            )
        )
        self.assertIn(
            "non_salvage_replayed_private_material:4",
            smoke.acceptance_errors(private_shadow),
        )

        wrong_shadow_budget = passing_summary()
        wrong_shadow_budget["attempts"].append(
            attempt(4, "positive", budget=0)
        )
        self.assertIn(
            "non_salvage_thinking_budget:4",
            smoke.acceptance_errors(wrong_shadow_budget),
        )

        for field, value, code in (
            ("phase", "fresh", "salvage_phase:2"),
            ("thinking_budget", 1, "salvage_thinking_budget:2"),
            ("private_prefix_present", False, "salvage_private_prefix:2"),
            ("loop_tail_present", False, "salvage_loop_tail:2"),
        ):
            invalid_salvage = passing_summary()
            invalid_salvage["attempts"][1][field] = value
            with self.subTest(field=field):
                self.assertIn(code, smoke.acceptance_errors(invalid_salvage))

    def test_final_acceptance_checks_late_errors_and_every_fresh_attempt(self) -> None:
        late_error = passing_summary()
        late_error["fixture_errors"].append("late_fixture_error")
        self.assertIn("late_fixture_error", smoke.acceptance_errors(late_error))

        late_replay = passing_summary()
        late_replay["attempts"].append(
            attempt(
                4,
                "fresh",
                budget=0,
                private=True,
                loop=True,
            )
        )
        self.assertIn(
            "fresh_request_replayed_private_material:4",
            smoke.acceptance_errors(late_replay),
        )
        self.assertIn("fresh_thinking_budget:4", smoke.acceptance_errors(late_replay))

        duplicate_salvage = passing_summary()
        duplicate_salvage["attempts"].append(
            attempt(4, "positive", budget=0, salvage=True, private=True)
        )
        self.assertIn("salvage_count", smoke.acceptance_errors(duplicate_salvage))

    def test_final_snapshot_waits_for_handler_quiescence(self) -> None:
        fixture = smoke.Fixture("reason", "private", "fresh", "positive", "fresh-output")
        fixture.handler_started()
        release = threading.Event()
        completed = threading.Event()
        receipt: list[tuple[dict, list[str]]] = []

        def late_handler() -> None:
            release.wait()
            fixture.record_error("late_fixture_error")
            fixture.handler_finished()

        def finalize() -> None:
            cleanup = smoke.stop_fixture_server(None, None, fixture)
            _, errors = fixture.snapshot()
            receipt.append((cleanup, errors))
            completed.set()

        handler = threading.Thread(target=late_handler)
        finalizer = threading.Thread(target=finalize)
        handler.start()
        finalizer.start()
        self.assertFalse(completed.wait(timeout=0.05))
        release.set()
        handler.join(timeout=1)
        finalizer.join(timeout=1)
        self.assertFalse(handler.is_alive())
        self.assertFalse(finalizer.is_alive())
        self.assertTrue(receipt[0][0]["handlers_quiesced"])
        self.assertIn("late_fixture_error", receipt[0][1])

    def test_unexpected_exit_and_forced_kill_cannot_pass(self) -> None:
        for field in ("unexpected_exit", "forced_kill"):
            summary = passing_summary()
            summary["process_cleanup"][field] = True
            with self.subTest(field=field):
                self.assertTrue(smoke.acceptance_errors(summary))

    def test_stop_reports_unexpected_exit_and_graceful_stop(self) -> None:
        child = r"""
import os, signal, sys, time
mode, ready = sys.argv[1:]
if mode == "graceful":
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(ready, "w", encoding="ascii") as receipt:
    receipt.write("ready")
    receipt.flush()
    os.fsync(receipt.fileno())
if mode == "unexpected":
    os._exit(17)
while True:
    time.sleep(1)
"""
        for mode in ("unexpected", "graceful"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                ready = Path(tmp) / "ready"
                proc = subprocess.Popen(
                    [sys.executable, "-c", child, mode, str(ready)],
                    start_new_session=True,
                )
                identity = smoke.capture_spawn_identity(proc.pid)
                try:
                    deadline = time.monotonic() + 2
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    if mode == "unexpected":
                        while (
                            smoke.process_identity_is_live(proc.pid)
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)

                    cleanup = smoke.stop(proc, identity)
                    self.assertTrue(cleanup["proxy_exited"])
                    self.assertTrue(cleanup["group_quiesced"])
                    self.assertTrue(cleanup["session_quiesced"])
                    self.assertFalse(cleanup["forced_kill"])
                    if mode == "unexpected":
                        self.assertTrue(cleanup["unexpected_exit"])
                        self.assertEqual(cleanup["returncode"], 17)
                        self.assertFalse(cleanup["graceful_stop"])
                    else:
                        self.assertFalse(cleanup["unexpected_exit"])
                        self.assertEqual(cleanup["returncode"], 0)
                        self.assertTrue(cleanup["graceful_stop"])
                finally:
                    if proc.returncode is None:
                        smoke._signal_group(identity, signal.SIGKILL)
                        proc.wait(timeout=2)

    def test_stop_kills_same_session_residual_before_reaping_leader(self) -> None:
        child_ready = r"""
import os, signal, sys, time
ready = sys.argv[1]
reader, writer = os.pipe()
child = os.fork()
if child == 0:
    os.close(reader)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(writer, b"1")
    os.close(writer)
    while True:
        time.sleep(1)
os.close(writer)
os.read(reader, 1)
os.close(reader)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(ready, "w", encoding="ascii") as receipt:
    receipt.write(str(child))
    receipt.flush()
    os.fsync(receipt.fileno())
while True:
    time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready"
            proc = subprocess.Popen(
                [sys.executable, "-c", child_ready, str(ready)],
                start_new_session=True,
            )
            identity = smoke.capture_spawn_identity(proc.pid)
            try:
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                child_pid = int(ready.read_text())

                cleanup = smoke.stop(proc, identity)
                self.assertGreaterEqual(
                    cleanup["residual_producer_count_before_kill"], 1
                )
                self.assertTrue(cleanup["forced_kill"])
                self.assertFalse(cleanup["graceful_stop"])
                self.assertTrue(cleanup["group_quiesced"])
                self.assertTrue(cleanup["session_quiesced"])
                self.assertEqual(cleanup["residual_producer_count_final"], 0)
                self.assertFalse(smoke.process_identity_is_live(child_pid))

                summary = passing_summary()
                summary["process_cleanup"] = cleanup
                self.assertIn("proxy_forced_kill", smoke.acceptance_errors(summary))
            finally:
                if proc.returncode is None:
                    try:
                        smoke._signal_group(identity, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.fail("owned process group did not terminate")

    def test_marker_scan_fails_closed_on_walk_read_and_symlink_errors(self) -> None:
        marker = (b"private-marker",)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence.sqlite3"
            evidence.write_bytes(b"safe metadata")
            with patch.object(
                smoke.os,
                "read",
                side_effect=PermissionError(errno.EACCES, "denied"),
            ):
                with self.assertRaises(PermissionError):
                    smoke.sensitive_marker_count(root, marker)

            evidence.write_bytes(
                b"x" * (smoke.SCAN_CHUNK_SIZE - 3) + marker[0] + b"safe"
            )
            self.assertEqual(smoke.sensitive_marker_count(root, marker), 1)

            not_directory = root / "not-a-directory"
            not_directory.write_bytes(b"safe metadata")
            with self.assertRaises(OSError):
                smoke.sensitive_marker_count(not_directory, marker)

            symlink = root / "evidence-link"
            symlink.symlink_to(evidence)
            with self.assertRaises(OSError):
                smoke.sensitive_marker_count(root, marker)

        summary = passing_summary()
        summary["scan_errors"] = 1
        self.assertIn("privacy_scan_failed", smoke.acceptance_errors(summary))

    def test_marker_scan_fails_closed_on_replacement_races(self) -> None:
        marker = (b"private-marker",)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            root.mkdir()
            (root / "safe").write_bytes(b"safe")
            moved_root = base / "root.saved"
            outside = base / "outside"
            outside.mkdir()
            (outside / "evidence").write_bytes(marker[0])
            real_scandir = smoke.os.scandir
            changed = False

            def replace_root(path):
                nonlocal changed
                if not changed:
                    changed = True
                    root.rename(moved_root)
                    root.symlink_to(outside, target_is_directory=True)
                return real_scandir(path)

            failed_closed = False
            try:
                with patch.object(smoke.os, "scandir", side_effect=replace_root):
                    smoke.sensitive_marker_count(root, marker)
            except OSError:
                failed_closed = True
            finally:
                if changed:
                    root.unlink(missing_ok=True)
                    moved_root.rename(root)
            self.assertTrue(failed_closed)

        class RacingEntry:
            def __init__(self, entry, mutate) -> None:
                self._entry = entry
                self._mutate = mutate

            def stat(self, *args, **kwargs):
                result = self._entry.stat(*args, **kwargs)
                self._mutate(self._entry.name)
                return result

            def __getattr__(self, name):
                return getattr(self._entry, name)

        class RacingEntries:
            def __init__(self, entries, mutate, restore) -> None:
                self._entries = entries
                self._mutate = mutate
                self._restore = restore

            def __enter__(self):
                self._entries.__enter__()
                return self

            def __exit__(self, *args):
                self._restore()
                return self._entries.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                try:
                    entry = next(self._entries)
                except StopIteration:
                    self._restore()
                    raise
                return RacingEntry(entry, self._mutate)

        def run_race(*, replacement: str) -> tuple[bool, int | None, bool]:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root = base / "root"
                root.mkdir()
                target = root / "evidence"
                backup = base / "evidence.saved"
                outside = base / "outside"
                if replacement == "symlink":
                    target.write_bytes(b"safe")
                    outside.write_bytes(marker[0])
                else:
                    target.write_bytes(marker[0])
                    outside.write_bytes(b"safe")
                changed = False

                def mutate(name: str) -> None:
                    nonlocal changed
                    if changed or name != target.name:
                        return
                    changed = True
                    target.rename(backup)
                    if replacement == "symlink":
                        target.symlink_to(outside)
                    else:
                        target.write_bytes(b"safe")

                def restore() -> None:
                    if not changed or not backup.exists():
                        return
                    target.unlink(missing_ok=True)
                    backup.rename(target)

                real_scandir = smoke.os.scandir

                def racing_scandir(path):
                    return RacingEntries(real_scandir(path), mutate, restore)

                failed_closed = False
                count = None
                try:
                    with patch.object(smoke.os, "scandir", side_effect=racing_scandir):
                        count = smoke.sensitive_marker_count(root, marker)
                except OSError:
                    failed_closed = True
                finally:
                    restore()
                return failed_closed, count, marker[0] in target.read_bytes()

        followed_symlink, _, _ = run_race(replacement="symlink")
        self.assertTrue(followed_symlink)

        failed_closed, count, marker_restored = run_race(replacement="regular")
        self.assertTrue(marker_restored)
        self.assertTrue(failed_closed or count == 1)

    def test_fixture_close_is_bounded_for_partial_request_body(self) -> None:
        fixture = smoke.Fixture(
            "reason", "private", "fresh", "positive", "fresh-output"
        )
        server = smoke.FixtureHTTPServer(
            ("127.0.0.1", 0), fixture.handler(), fixture
        )
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        server_thread.start()
        for length in ("1e3", str(smoke.FIXTURE_BODY_LIMIT + 1)):
            with socket.create_connection(server.server_address, timeout=1) as rejected:
                rejected.sendall(
                    (
                        "POST /v1/chat/completions HTTP/1.1\r\n"
                        "Host: fixture\r\n"
                        f"Content-Length: {length}\r\n\r\n"
                    ).encode("ascii")
                )
        error_deadline = time.monotonic() + 1
        while time.monotonic() < error_deadline:
            _, errors = fixture.snapshot()
            if errors.count("fixture_invalid_content_length") == 2:
                break
            time.sleep(0.01)

        client_sock = socket.create_connection(server.server_address, timeout=1)
        client_sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: fixture\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 100\r\n\r\n{"
        )
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with fixture.lock:
                if fixture.active_handlers == 1:
                    break
            time.sleep(0.01)
        with fixture.lock:
            self.assertEqual(fixture.active_handlers, 1)

        receipt: list[dict] = []
        done = threading.Event()

        def close_fixture() -> None:
            receipt.append(smoke.stop_fixture_server(server, server_thread, fixture))
            done.set()

        stopper = threading.Thread(target=close_fixture, daemon=True)
        started = time.monotonic()
        stopper.start()
        bounded = done.wait(timeout=1.5)
        elapsed = time.monotonic() - started
        client_sock.close()
        done.wait(timeout=2)
        stopper.join(timeout=2)

        self.assertTrue(bounded)
        self.assertLess(elapsed, 1.5)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(server_thread.is_alive())
        self.assertTrue(receipt[0]["shutdown_stopped"])
        attempts, errors = fixture.snapshot()
        self.assertFalse(attempts)
        self.assertEqual(errors.count("fixture_invalid_content_length"), 2)
        self.assertIn("fixture_body_timeout", errors)
        self.assertTrue(receipt[0]["handlers_quiesced"])

        summary = passing_summary()
        summary["fixture_cleanup"] = receipt[0]
        summary["fixture_errors"] = errors
        self.assertTrue(smoke.acceptance_errors(summary))


if __name__ == "__main__":
    unittest.main()
