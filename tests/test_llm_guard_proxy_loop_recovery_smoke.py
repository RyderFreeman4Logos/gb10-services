from __future__ import annotations

import ctypes
import errno
import io
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
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


def enable_test_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "test_subreaper_setup_failed")


def fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


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
            ),
            attempt(3, "fresh", budget=smoke.THINKING_BUDGET),
        ],
        "fixture_errors": [],
        "positive_pass": True,
        "fresh_negative_pass": True,
        "execution_error": None,
        "subreaper_enabled": True,
        "exclusive_supervisor": True,
        "process_cleanup": {
            "exclusive_supervisor": True,
            "supervisor_exited": True,
            "supervisor_reaped": True,
            "candidate_ownership_quiesced": True,
            "supervisor_error": None,
            "proxy_exited": True,
            "unexpected_exit": False,
            "graceful_stop": True,
            "forced_kill": False,
            "spawn_identity_captured": True,
            "pidfd_available": True,
            "ownership_quiesced": True,
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
        "scan_limits_enforced": True,
    }


class LoopRecoveryFinalizationTests(unittest.TestCase):
    def test_final_acceptance_checks_every_attempt_privacy_and_budget(self) -> None:
        self.assertFalse(smoke.acceptance_errors(passing_summary()))

        legal_shadow = passing_summary()
        legal_shadow["attempts"].append(
            attempt(4, "positive", budget=1024)
        )
        self.assertFalse(smoke.acceptance_errors(legal_shadow))

        for field in ("private_prefix_present", "loop_tail_present"):
            private_shadow = passing_summary()
            private_shadow["attempts"].append(
                attempt(4, "positive", budget=smoke.THINKING_BUDGET)
            )
            private_shadow["attempts"][-1][field] = True
            with self.subTest(shadow_field=field):
                self.assertIn(
                    "non_salvage_replayed_private_material:4",
                    smoke.acceptance_errors(private_shadow),
                )

        for field, value, code in (
            ("phase", "fresh", "salvage_phase:2"),
            ("thinking_budget", 1, "salvage_thinking_budget:2"),
            ("private_prefix_present", False, "salvage_private_prefix:2"),
            ("loop_tail_present", True, "salvage_loop_tail:2"),
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
                budget=smoke.THINKING_BUDGET,
                private=True,
                loop=True,
            )
        )
        self.assertIn(
            "fresh_request_replayed_private_material:4",
            smoke.acceptance_errors(late_replay),
        )

        wrong_fresh_budget = passing_summary()
        wrong_fresh_budget["attempts"].append(
            attempt(4, "fresh", budget=0)
        )
        self.assertIn(
            "fresh_thinking_budget:4",
            smoke.acceptance_errors(wrong_fresh_budget),
        )

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

    def test_exclusive_supervisor_receipts_are_required(self) -> None:
        for scope, field, code in (
            (None, "exclusive_supervisor", "exclusive_supervisor_missing"),
            ("process_cleanup", "exclusive_supervisor", "cleanup_supervisor_not_exclusive"),
            ("process_cleanup", "supervisor_exited", "supervisor_not_exited"),
            ("process_cleanup", "supervisor_reaped", "supervisor_not_reaped"),
            (
                "process_cleanup",
                "candidate_ownership_quiesced",
                "candidate_ownership_not_quiesced",
            ),
        ):
            summary = passing_summary()
            target = summary if scope is None else summary[scope]
            target[field] = False
            with self.subTest(field=field):
                self.assertIn(code, smoke.acceptance_errors(summary))

        summary = passing_summary()
        summary["process_cleanup"]["supervisor_error"] = {
            "class": "TimeoutError",
            "code": "supervisor_control_timeout",
        }
        self.assertIn("supervisor_cleanup_failed", smoke.acceptance_errors(summary))

    def test_direct_stop_never_claims_unrelated_post_baseline_child(self) -> None:
        child = r"""
import os, signal, sys, time
ready = sys.argv[1]
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""
        unrelated_child = r"""
import os, signal, sys, time
ready, term = sys.argv[1:]
def terminated(*_):
    with open(term, "w", encoding="ascii") as output:
        output.write("term")
    os._exit(73)
signal.signal(signal.SIGTERM, terminated)
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_ready = root / "candidate.ready"
            unrelated_ready = root / "unrelated.ready"
            unrelated_term = root / "unrelated.term"
            baseline = smoke._capture_direct_children()
            proc = subprocess.Popen(
                [sys.executable, "-c", child, str(candidate_ready)],
                start_new_session=True,
            )
            identity = smoke.capture_spawn_identity(proc.pid)
            unrelated = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    unrelated_child,
                    str(unrelated_ready),
                    str(unrelated_term),
                ],
                start_new_session=True,
            )
            unrelated_identity = smoke.capture_spawn_identity(unrelated.pid)
            try:
                deadline = time.monotonic() + 2
                while (
                    not candidate_ready.exists() or not unrelated_ready.exists()
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(candidate_ready.exists())
                self.assertTrue(unrelated_ready.exists())

                cleanup = smoke.stop(proc, identity, baseline)
                current = smoke._read_process_identity(unrelated.pid)

                self.assertTrue(smoke._same_process(current, unrelated_identity))
                self.assertIsNone(unrelated.poll())
                self.assertFalse(unrelated_term.exists())
                self.assertTrue(cleanup["ownership_quiesced"])
                self.assertEqual(cleanup["residual_producer_count_final"], 0)
            finally:
                if proc.returncode is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except ChildProcessError:
                        pass
                if unrelated.returncode is None:
                    try:
                        os.killpg(unrelated.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        unrelated.wait(timeout=2)
                    except ChildProcessError:
                        pass
                    smoke._close_identity_pidfd(unrelated_identity)

    def test_supervisor_control_failures_cleanup_candidate_bounded(self) -> None:
        candidate = r"""
import os, signal, sys, time
ready = sys.argv[1]
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""
        for mode, expected in (
            ("eof", "supervisor_parent_eof"),
            ("timeout", "supervisor_control_timeout"),
            ("invalid", "supervisor_invalid_control"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ready = root / "candidate.ready"
                timeout = 0.5 if mode == "timeout" else smoke.SUPERVISOR_CONTROL_TIMEOUT
                with patch.object(smoke, "SUPERVISOR_CONTROL_TIMEOUT", timeout):
                    handle = smoke.start_candidate_supervisor(
                        [sys.executable, "-c", candidate, str(ready)],
                        root,
                        os.environ.copy(),
                        root / "candidate.log",
                    )
                    deadline = time.monotonic() + 2
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    started = time.monotonic()
                    if mode == "eof":
                        handle.control.shutdown(socket.SHUT_WR)
                        cleanup = smoke._finish_candidate_supervisor(handle, None)
                    elif mode == "timeout":
                        cleanup = smoke._finish_candidate_supervisor(handle, None)
                    else:
                        cleanup = smoke._finish_candidate_supervisor(handle, b"invalid")
                    elapsed = time.monotonic() - started

                self.assertLess(elapsed, 2.0)
                self.assertEqual(cleanup["supervisor_error"]["code"], expected)
                self.assertTrue(cleanup["proxy_exited"])
                self.assertTrue(cleanup["ownership_quiesced"])
                self.assertTrue(cleanup["candidate_ownership_quiesced"])
                self.assertEqual(cleanup["residual_producer_count_final"], 0)
                self.assertTrue(cleanup["supervisor_exited"])
                self.assertTrue(cleanup["supervisor_reaped"])

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
                    self.assertTrue(cleanup["spawn_identity_captured"])
                    self.assertTrue(cleanup["pidfd_available"])
                    self.assertTrue(cleanup["ownership_quiesced"])
                    self.assertEqual(
                        cleanup["residual_producer_count_before_kill"], 0
                    )
                    self.assertEqual(cleanup["residual_producer_count_final"], 0)
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

    def test_stop_uses_linux_pidfd_syscalls_when_python_omits_wrappers(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        native_pidfd_open = smoke.os.pidfd_open
        native_pidfd_send_signal = smoke.signal.pidfd_send_signal
        identity = None
        try:
            with (
                patch.object(smoke.os, "pidfd_open", None),
                patch.object(smoke.signal, "pidfd_send_signal", None),
            ):
                identity = smoke.capture_spawn_identity(proc.pid)
                cleanup = smoke.stop(proc, identity)
            self.assertTrue(cleanup["spawn_identity_captured"])
            self.assertTrue(cleanup["pidfd_available"])
            self.assertTrue(cleanup["ownership_quiesced"])
            self.assertEqual(cleanup["residual_producer_count_before_kill"], 0)
            self.assertEqual(cleanup["residual_producer_count_final"], 0)
            self.assertFalse(cleanup["graceful_stop"])
            self.assertFalse(cleanup["forced_kill"])
            self.assertEqual(cleanup["returncode"], -signal.SIGTERM)
            self.assertTrue(cleanup["group_quiesced"])
            self.assertTrue(cleanup["session_quiesced"])
        finally:
            if proc.returncode is None:
                pidfd = native_pidfd_open(proc.pid)
                try:
                    native_pidfd_send_signal(pidfd, signal.SIGKILL)
                finally:
                    os.close(pidfd)
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
receipt_path = ready + ".tmp"
with open(receipt_path, "w", encoding="ascii") as receipt:
    receipt.write(str(child))
    receipt.flush()
    os.fsync(receipt.fileno())
os.replace(receipt_path, ready)
while True:
    time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = root / "ready"
            supervisor = smoke.start_candidate_supervisor(
                [sys.executable, "-c", child_ready, str(ready)],
                root,
                os.environ.copy(),
                root / "candidate.log",
            )
            try:
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                child_pid = int(ready.read_text())

                cleanup = smoke.stop_candidate_supervisor(supervisor)
                self.assertGreaterEqual(
                    cleanup["residual_producer_count_before_kill"], 1
                )
                self.assertTrue(cleanup["forced_kill"])
                self.assertFalse(cleanup["graceful_stop"])
                self.assertTrue(cleanup["group_quiesced"])
                self.assertTrue(cleanup["session_quiesced"])
                self.assertTrue(cleanup["ownership_quiesced"])
                self.assertEqual(cleanup["residual_producer_count_final"], 0)
                self.assertFalse(smoke.process_identity_is_live(child_pid))

                summary = passing_summary()
                summary["process_cleanup"] = cleanup
                self.assertIn("proxy_forced_kill", smoke.acceptance_errors(summary))
            finally:
                if smoke.process_identity_is_live(supervisor.pid):
                    try:
                        smoke.stop_candidate_supervisor(supervisor)
                    except OSError:
                        self.fail("exclusive supervisor did not terminate")

    def test_subreaper_setup_failure_prevents_candidate_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            marker = base / "spawned"
            binary = base / "candidate"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('spawned')\n"
            )
            binary.chmod(0o700)
            output = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "smoke",
                        "--candidate-config",
                        str(ROOT / "config" / "llm-guard-proxy" / "config.toml"),
                        "--binary",
                        str(binary),
                        "--root",
                        str(base / "run"),
                    ],
                ),
                patch.object(
                    smoke,
                    "_enable_child_subreaper",
                    side_effect=PermissionError(errno.EPERM, "denied"),
                ),
                redirect_stdout(output),
            ):
                result = smoke.main()

            self.assertEqual(result, 1)
            self.assertFalse(marker.exists())
            summary = json.loads(output.getvalue())
            self.assertFalse(summary["subreaper_enabled"])
            self.assertEqual(summary["execution_error"]["code"], "EPERM")

    def test_exclusive_supervisor_owns_escaped_candidate_not_unrelated_child(self) -> None:
        child = r"""
import os, signal, sys, time
mode, ready, root_ready, trigger, late = sys.argv[1:]

def producer():
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    receipt = ready + ".tmp"
    with open(receipt, "w", encoding="ascii") as output:
        output.write(str(os.getpid()))
        output.flush()
        os.fsync(output.fileno())
    os.replace(receipt, ready)
    while not os.path.exists(trigger):
        time.sleep(0.01)
    with open(late, "wb") as output:
        output.write(b"post-scan-private-marker")
        output.flush()
        os.fsync(output.fileno())
    while True:
        time.sleep(1)

if mode == "setsid":
    spawned = os.fork()
    if spawned == 0:
        producer()
else:
    middle = os.fork()
    if middle == 0:
        spawned = os.fork()
        if spawned == 0:
            producer()
        os._exit(0)
    os.waitpid(middle, 0)

while not os.path.exists(ready):
    time.sleep(0.01)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(root_ready, "w", encoding="ascii") as output:
    output.write("ready")
    output.flush()
    os.fsync(output.fileno())
while True:
    time.sleep(1)
"""
        unrelated_child = r"""
import os, signal, sys, time
ready, term = sys.argv[1:]
def terminated(*_):
    with open(term, "w", encoding="ascii") as output:
        output.write("term")
    os._exit(73)
signal.signal(signal.SIGTERM, terminated)
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""
        for mode, trial in (
            (mode, trial)
            for mode in ("setsid", "double-fork")
            for trial in range(3)
        ):
            with self.subTest(mode=mode, trial=trial), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ready = root / "producer.pid"
                root_ready = root / "root.ready"
                trigger = root / "post-scan.trigger"
                late = root / "late-evidence"
                unrelated_ready = root / "unrelated.ready"
                unrelated_term = root / "unrelated.term"
                baseline = smoke._capture_direct_children()
                supervisor = smoke.start_candidate_supervisor(
                    [
                        sys.executable,
                        "-c",
                        child,
                        mode,
                        str(ready),
                        str(root_ready),
                        str(trigger),
                        str(late),
                    ],
                    root,
                    os.environ.copy(),
                    root / "candidate.log",
                )
                unrelated = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        unrelated_child,
                        str(unrelated_ready),
                        str(unrelated_term),
                    ],
                    start_new_session=True,
                )
                unrelated_identity = smoke.capture_spawn_identity(unrelated.pid)
                producer_pid = None
                try:
                    deadline = time.monotonic() + 2
                    while (
                        not root_ready.exists() or not unrelated_ready.exists()
                    ) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(root_ready.exists())
                    self.assertTrue(unrelated_ready.exists())
                    self.assertNotIn(unrelated.pid, baseline)
                    producer_pid = int(ready.read_text())

                    cleanup = smoke.stop_candidate_supervisor(supervisor)
                    unrelated_current = smoke._read_process_identity(unrelated.pid)
                    scan_before = smoke.sensitive_marker_count(
                        root, (b"post-scan-private-marker",)
                    )
                    trigger.write_bytes(b"continue")
                    late_deadline = time.monotonic() + 0.3
                    while not late.exists() and time.monotonic() < late_deadline:
                        time.sleep(0.01)
                    late_write = late.exists()
                    producer_live = smoke.process_identity_is_live(producer_pid)

                    self.assertEqual(scan_before, 0)
                    self.assertFalse(late_write)
                    self.assertFalse(producer_live)
                    self.assertTrue(cleanup["exclusive_supervisor"])
                    self.assertTrue(cleanup["supervisor_exited"])
                    self.assertTrue(cleanup["supervisor_reaped"])
                    self.assertTrue(cleanup["candidate_ownership_quiesced"])
                    self.assertTrue(cleanup["group_quiesced"])
                    self.assertTrue(cleanup["session_quiesced"])
                    self.assertTrue(
                        smoke._same_process(unrelated_current, unrelated_identity)
                    )
                    self.assertIsNone(unrelated.poll())
                    self.assertFalse(unrelated_term.exists())
                    self.assertTrue(cleanup["forced_kill"])
                    self.assertGreaterEqual(
                        cleanup["residual_producer_count_before_kill"], 1
                    )
                    self.assertTrue(cleanup["ownership_quiesced"])
                    self.assertEqual(cleanup["residual_producer_count_final"], 0)
                    summary = passing_summary()
                    summary["process_cleanup"] = cleanup
                    self.assertIn(
                        "proxy_forced_kill", smoke.acceptance_errors(summary)
                    )
                finally:
                    if smoke.process_identity_is_live(supervisor.pid):
                        try:
                            smoke.stop_candidate_supervisor(supervisor)
                        except OSError:
                            self.fail("exclusive supervisor did not terminate")
                    if unrelated.returncode is None:
                        try:
                            os.killpg(unrelated.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            unrelated.wait(timeout=2)
                        except ChildProcessError:
                            pass
                    smoke._close_identity_pidfd(unrelated_identity)
                    if producer_pid is not None:
                        try:
                            os.kill(producer_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            os.waitpid(producer_pid, 0)
                        except ChildProcessError:
                            pass

    def test_pidfd_capture_failures_still_reap_owned_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            binary = base / "candidate"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                "time.sleep(60)\n"
            )
            binary.chmod(0o700)
            for code in (errno.EMFILE, errno.EPERM):
                with self.subTest(code=errno.errorcode[code]):
                    output = io.StringIO()
                    root = base / f"run-{errno.errorcode[code]}"
                    argv = [
                        "smoke",
                        "--candidate-config",
                        str(ROOT / "config" / "llm-guard-proxy" / "config.toml"),
                        "--binary",
                        str(binary),
                        "--root",
                        str(root),
                    ]
                    before_fds = fd_count()
                    with (
                        patch.object(sys, "argv", argv),
                        patch.object(
                            smoke,
                            "_pidfd_open",
                            side_effect=OSError(code, os.strerror(code)),
                        ),
                        redirect_stdout(output),
                    ):
                        result = smoke.main()

                    summary = json.loads(output.getvalue())
                    cleanup = summary["process_cleanup"]
                    self.assertEqual(result, 1)
                    spawned_pid = cleanup["spawn_identity"]["pid"]
                    self.assertFalse(smoke.process_identity_is_live(spawned_pid))
                    self.assertEqual(summary["result"], "FAIL")
                    self.assertEqual(
                        summary["execution_error"]["code"], errno.errorcode[code]
                    )
                    self.assertTrue(cleanup["proxy_exited"])
                    self.assertTrue(cleanup["spawn_identity_captured"])
                    self.assertFalse(cleanup["pidfd_available"])
                    self.assertTrue(cleanup["ownership_quiesced"])
                    self.assertEqual(
                        cleanup["residual_producer_count_before_kill"], 0
                    )
                    self.assertEqual(cleanup["residual_producer_count_final"], 0)
                    self.assertEqual(
                        cleanup["stop_error"]["code"], errno.errorcode[code]
                    )
                    self.assertTrue(all(summary["port_cleanup"].values()))
                    self.assertEqual(fd_count(), before_fds)

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
            stats: dict[str, int | float | bool] = {}
            self.assertEqual(smoke.sensitive_marker_count(root, marker, stats), 1)
            self.assertTrue(stats["completed"])
            self.assertLessEqual(stats["entry_count"], smoke.SCAN_MAX_ENTRIES)
            self.assertLessEqual(stats["logical_bytes"], smoke.SCAN_MAX_TOTAL_BYTES)
            self.assertLessEqual(stats["largest_file_bytes"], smoke.SCAN_MAX_FILE_BYTES)
            self.assertLess(stats["elapsed_milliseconds"], smoke.SCAN_TIMEOUT * 1_000)

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

    def test_marker_scan_rejects_sparse_oversize_before_reading(self) -> None:
        marker = (b"private-marker",)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sparse = root / "sparse-evidence"
            with sparse.open("wb") as output:
                output.truncate(1 << 40)
            before_fds = fd_count()
            started = time.monotonic()
            with (
                patch.object(
                    smoke.os,
                    "read",
                    side_effect=AssertionError("oversized file was read"),
                ),
                self.assertRaises(OSError) as raised,
            ):
                smoke.sensitive_marker_count(root, marker)
            elapsed = time.monotonic() - started

            self.assertEqual(raised.exception.errno, errno.EFBIG)
            self.assertLess(elapsed, 0.5)
            self.assertEqual(fd_count(), before_fds)
            self.assertNotIn(sparse.name, str(raised.exception))
            self.assertNotIn(marker[0].decode(), str(raised.exception))

    def test_marker_scan_rejects_total_logical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").write_bytes(b"a" * 40)
            (root / "second").write_bytes(b"b" * 40)
            before_fds = fd_count()
            with (
                patch.object(smoke, "SCAN_MAX_FILE_BYTES", 64),
                patch.object(smoke, "SCAN_MAX_TOTAL_BYTES", 64),
                self.assertRaises(OSError) as raised,
            ):
                smoke.sensitive_marker_count(root, (b"private-marker",))

            self.assertEqual(raised.exception.errno, errno.EFBIG)
            self.assertEqual(fd_count(), before_fds)
            self.assertNotIn("first", str(raised.exception))
            self.assertNotIn("second", str(raised.exception))

    def test_marker_scan_rejects_excess_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(smoke.SCAN_MAX_ENTRIES + 1):
                (root / f"entry-{number}").touch()
            before_fds = fd_count()
            with self.assertRaises(OSError) as raised:
                smoke.sensitive_marker_count(root, (b"private-marker",))

            self.assertEqual(raised.exception.errno, errno.E2BIG)
            self.assertEqual(fd_count(), before_fds)
            self.assertNotIn("entry-", str(raised.exception))

    def test_marker_scan_enforces_absolute_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "evidence").write_bytes(b"safe")
            before_fds = fd_count()
            ticks = iter((10.0, 10.0 + smoke.SCAN_TIMEOUT + 1.0))
            with (
                patch.object(smoke.time, "monotonic", side_effect=lambda: next(ticks)),
                self.assertRaises(OSError) as raised,
            ):
                smoke.sensitive_marker_count(root, (b"private-marker",))

            self.assertEqual(raised.exception.errno, errno.ETIMEDOUT)
            self.assertEqual(fd_count(), before_fds)

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
