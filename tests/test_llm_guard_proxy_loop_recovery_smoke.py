from __future__ import annotations

import errno
import importlib.util
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


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
            attempt(2, "positive", budget=0, salvage=True, private=True),
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
        },
        "fixture_cleanup": {
            "server_stopped": True,
            "handlers_quiesced": True,
        },
        "port_cleanup": {"guard_rebindable": True, "fake_rebindable": True},
        "sensitive_marker_leak_count_all_fixture_files": 0,
        "scan_errors": 0,
    }


class LoopRecoveryFinalizationTests(unittest.TestCase):
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

    def test_stop_reports_unexpected_exit_and_forced_kill(self) -> None:
        exited = Mock()
        exited.poll.return_value = 17
        self.assertTrue(smoke.stop(exited)["unexpected_exit"])

        stubborn = Mock()
        stubborn.pid = 1234
        stubborn.poll.return_value = None
        stubborn.wait.side_effect = [
            subprocess.TimeoutExpired("proxy", 8),
            -signal.SIGKILL,
        ]
        with patch.object(smoke.os, "killpg") as killpg:
            cleanup = smoke.stop(stubborn)
        self.assertEqual(
            killpg.call_args_list,
            [call(stubborn.pid, signal.SIGTERM), call(stubborn.pid, signal.SIGKILL)],
        )
        self.assertTrue(cleanup["forced_kill"])
        self.assertFalse(cleanup["graceful_stop"])

        crashing = Mock()
        crashing.pid = 1235
        crashing.poll.return_value = None
        crashing.wait.return_value = 70
        with patch.object(smoke.os, "killpg"):
            cleanup = smoke.stop(crashing)
        self.assertTrue(cleanup["proxy_exited"])
        self.assertFalse(cleanup["graceful_stop"])

    def test_marker_scan_fails_closed_on_walk_read_and_symlink_errors(self) -> None:
        marker = (b"private-marker",)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence.sqlite3"
            evidence.write_bytes(b"safe metadata")
            with patch.object(
                Path,
                "read_bytes",
                side_effect=PermissionError(errno.EACCES, "denied"),
            ):
                with self.assertRaises(PermissionError):
                    smoke.sensitive_marker_count(root, marker)

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


if __name__ == "__main__":
    unittest.main()
