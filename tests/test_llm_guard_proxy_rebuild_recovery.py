from __future__ import annotations

import fcntl
import errno
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from guard_rebuild_fixtures import ROOT, RebuildFixture

ENGINE = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.py"
BOUNDED = ROOT / "scripts" / "gb10_bounded_process.py"
PRODUCTION_COMPLETE = "LLM_GUARD_PROXY_REBUILD_COMPLETE"
TEST_COMPLETE = "LLM_GUARD_PROXY_REBUILD_TEST_ONLY_COMPLETE"
RECOVERED = "LLM_GUARD_PROXY_REBUILD_RECOVERED"


class GuardRebuildRecoveryTests(unittest.TestCase):
    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_no_completion(self, output: str) -> None:
        self.assertNotIn(PRODUCTION_COMPLETE, output)
        self.assertNotIn(TEST_COMPLETE, output)
        self.assertNotIn("LLM_GUARD_REBUILD_PRODUCTION_COMPLETE", output)
        self.assertNotIn("LLM_GUARD_REBUILD_TEST_ONLY_COMPLETE", output)

    @staticmethod
    def wait_for_path(
        path: Path, process: subprocess.Popen[str], timeout: float
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            if process.poll() is not None:
                return False
            time.sleep(0.01)
        return path.exists()

    @staticmethod
    def kill_fixture_process(process: subprocess.Popen[str]) -> tuple[str, str]:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return process.communicate(timeout=5)

    @staticmethod
    def transaction_state(fixture: RebuildFixture) -> Path:
        return fixture.receipt_dir / "transaction.v1" / "state.json"

    def test_lock_contention_fails_closed_before_tools_or_link(self) -> None:
        with RebuildFixture() as fixture:
            descriptor, lock = fixture.hold_rebuild_lock()
            before = os.readlink(fixture.service_bin)
            try:
                result = fixture.run(timeout=10)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            output = self.output(result)
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("lock", output.lower())
            self.assertEqual(fixture.calls(), "")
            self.assertEqual(os.readlink(fixture.service_bin), before)
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            self.assert_no_completion(output)

    def test_stale_mutated_wal_rolls_back_before_build_and_exits_75(self) -> None:
        with RebuildFixture() as fixture:
            wal = fixture.write_wal("mutated", mutate=True)
            fixture.tool_log.unlink(missing_ok=True)
            result = fixture.run(timeout=15)
            output = self.output(result)
            self.assertEqual(result.returncode, 75, output)
            self.assertIn(RECOVERED, output)
            fixture.assert_prior_restored(self)
            self.assertNotIn("cargo build", fixture.calls())
            self.assertFalse(wal.exists())
            self.assert_no_completion(output)
            fixture.assert_no_backend_lifecycle(self)

    def test_stale_python_runtime_mismatch_blocks_before_tools_or_mutation(
        self,
    ) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            payload = json.loads(state.read_text())
            payload["authorities"]["python_runtime_authority_sha256"] = "f" * 64
            state.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )
            fixture.tool_log.unlink(missing_ok=True)
            before_link = os.readlink(fixture.service_bin)
            result = fixture.run(timeout=10)
            output = self.output(result)
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("Python runtime authority differs", output)
            self.assertEqual(fixture.calls(), "")
            self.assertEqual(os.readlink(fixture.service_bin), before_link)
            self.assertTrue(state.exists())

    def test_exact_python_runtime_authority_allows_stale_recovery(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            fixture.tool_log.unlink(missing_ok=True)
            result = fixture.run(timeout=15)
            output = self.output(result)
            self.assertEqual(result.returncode, 75, output)
            self.assertFalse(state.exists())
            fixture.assert_prior_restored(self)

    def test_malformed_and_unsafe_wal_block_before_tools(self) -> None:
        cases = {
            "duplicate": b'{"schema":1,"schema":1}\n',
            "unsafe-path": json.dumps(
                {
                    "schema": 1,
                    "phase": "mutated",
                    "txid": "a" * 32,
                    "service_bin": "relative/path",
                }
            ).encode()
            + b"\n",
        }
        for name, payload in cases.items():
            with self.subTest(name=name), RebuildFixture() as fixture:
                transaction = fixture.receipt_dir / "transaction.v1"
                transaction.mkdir(mode=0o700, parents=True)
                state = transaction / "state.json"
                state.write_bytes(payload)
                state.chmod(0o600)
                result = fixture.run(timeout=10)
                output = self.output(result)
                self.assertNotEqual(result.returncode, 0, output)
                self.assertTrue(state.exists())
                self.assertEqual(fixture.calls(), "")
                fixture.assert_prior_restored(self)
                self.assert_no_completion(output)

        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated")
            payload = json.loads(state.read_text())
            payload["prior"]["backup"]["identity"]["sha256"] = "f" * 64
            state.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )
            fixture.tool_log.unlink(missing_ok=True)
            result = fixture.run(timeout=10)
            output = self.output(result)
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("backup identity differs", output)
            self.assertTrue(state.exists())
            self.assertEqual(fixture.calls(), "")
            fixture.assert_prior_restored(self)
            self.assert_no_completion(output)

    def test_prior_absence_noop_restart_retains_mutated_wal(self) -> None:
        with RebuildFixture() as fixture:
            fixture.use_prior_bytes_for_candidate()
            fixture.service_bin.unlink()
            fixture.set_state(restart_noop_after=1)
            result = fixture.run(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE": "write",
                    "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "3",
                },
                timeout=15,
            )
            output = self.output(result)
            self.assertNotEqual(result.returncode, 0, output)
            self.assertNotIn("ROLLBACK_RESTORED=1", output)
            state_path = self.transaction_state(fixture)
            self.assertTrue(state_path.exists(), output)
            self.assertEqual(json.loads(state_path.read_text())["phase"], "mutated")
            self.assertEqual(
                fixture.reload_state()["running_target"], str(fixture.candidate)
            )
            blocked = json.loads(state_path.read_text())
            self.assertEqual(
                os.readlink(fixture.service_bin), blocked["prior"]["backup"]["path"]
            )
            self.assert_no_completion(output)

    def test_nonterminal_manager_job_is_cancelled_by_exact_id_then_rolls_back(
        self,
    ) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(job_behavior="nonterminal")
            result = fixture.run(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS": "5",
                    "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "6",
                },
                timeout=35,
            )
            output = self.output(result)
            self.assertNotEqual(result.returncode, 0, output)
            calls = fixture.calls()
            self.assertIn("systemctl --user cancel 41", calls)
            self.assertNotIn("cancel *", calls)
            self.assertEqual(fixture.reload_state()["jobs"], {})
            fixture.assert_prior_restored(self)
            self.assertFalse(self.transaction_state(fixture).exists())
            self.assert_no_completion(output)

    def test_cancelled_job_still_present_blocks_and_retains_wal(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(job_behavior="cancel-still-present")
            result = fixture.run(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS": "5",
                    "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "3",
                },
                timeout=35,
            )
            output = self.output(result)
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("systemctl --user cancel 41", fixture.calls())
            state_path = self.transaction_state(fixture)
            self.assertTrue(state_path.exists(), output)
            self.assertEqual(json.loads(state_path.read_text())["phase"], "mutated")
            self.assert_no_completion(output)

    def test_forward_exhaustion_keeps_independent_recovery_budget_and_reaps_tree(
        self,
    ) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(hang_restart_calls=[1])
            try:
                result = fixture.run(
                    extra_env={
                        "LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS": "5",
                        "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "7",
                    },
                    timeout=35,
                )
            except subprocess.TimeoutExpired as error:
                if fixture.descendant_pids.exists():
                    for line in fixture.descendant_pids.read_text().splitlines():
                        if line.isdigit():
                            try:
                                os.kill(int(line), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                self.fail(f"actual rebuild exceeded its internal bound: {error}")
            output = self.output(result)
            self.assertNotEqual(result.returncode, 0, output)
            fixture.assert_prior_restored(self)
            self.assertFalse(self.transaction_state(fixture).exists())
            pids = {
                int(line)
                for line in fixture.descendant_pids.read_text().splitlines()
                if line.isdigit()
            }
            self.assertTrue(pids)
            for pid in pids:
                self.assertFalse(Path(f"/proc/{pid}").exists(), f"survivor pid={pid}")
            self.assert_no_completion(output)

    def test_sigkill_boundary_matrix_next_holder_recovers_without_build(self) -> None:
        points = (
            "prestate-fsynced",
            "mutated-fsynced",
            "link-renamed",
            "forward-job",
            "candidate-attested",
            "committed-fsynced",
        )
        for point in points:
            with self.subTest(point=point), RebuildFixture() as fixture:
                marker = fixture.root / f"crash-{point}"
                process = fixture.popen(
                    extra_env={
                        "LLM_GUARD_REBUILD_TEST_CRASH_POINT": point,
                        "LLM_GUARD_REBUILD_TEST_CRASH_MARKER": str(marker),
                    }
                )
                try:
                    self.assertTrue(
                        self.wait_for_path(marker, process, 15),
                        f"boundary not reached: {point}",
                    )
                finally:
                    crashed_stdout, crashed_stderr = self.kill_fixture_process(process)
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assert_no_completion(crashed_stdout + crashed_stderr)
                fixture.tool_log.unlink(missing_ok=True)
                recovered = fixture.run(timeout=15)
                output = self.output(recovered)
                self.assertEqual(recovered.returncode, 75, output)
                self.assertIn(RECOVERED, output)
                self.assertNotIn("cargo build", fixture.calls())
                if point == "committed-fsynced":
                    self.assertEqual(
                        os.readlink(fixture.service_bin), str(fixture.candidate)
                    )
                    self.assertEqual(
                        fixture.reload_state()["running_target"], str(fixture.candidate)
                    )
                else:
                    fixture.assert_prior_restored(self)
                self.assert_no_completion(output)
                fixture.assert_no_backend_lifecycle(self)

    def test_committed_stdout_failure_never_rolls_back(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE": "completion-sink"
                },
                timeout=20,
            )
            output = self.output(result)
            self.assertNotEqual(result.returncode, 0, output)
            self.assertEqual(os.readlink(fixture.service_bin), str(fixture.candidate))
            self.assertEqual(
                fixture.reload_state()["running_target"], str(fixture.candidate)
            )
            receipts = list((fixture.receipt_dir / "receipts").glob("*/state.json"))
            self.assertEqual(len(receipts), 1, output)
            self.assertEqual(json.loads(receipts[0].read_text())["phase"], "committed")
            self.assertFalse(self.transaction_state(fixture).exists())
            self.assert_no_completion(output)

    def test_transaction_engine_has_no_unbounded_subprocess_api(self) -> None:
        source = ENGINE.read_text()
        for forbidden in (
            "subprocess.run(",
            "subprocess.Popen(",
            "subprocess.check_call(",
            "subprocess.check_output(",
            ".communicate(",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("run_bounded(", source)
        self.assertIn("--no-block", source)
        self.assertIn("--job-mode=fail", source)
        self.assertIn("list-jobs", source)

    def test_production_engine_rejects_every_test_only_seam(self) -> None:
        names = {
            "LLM_GUARD_REBUILD_TEST_CONFIG": "x",
            "LLM_GUARD_REBUILD_TEST_MISSING_TOOL": "curl",
            "LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE": "write",
            "LLM_GUARD_REBUILD_TEST_REPLACE_EXE_DURING_HASH": "cargo",
            "LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS": "1",
            "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "1",
            "LLM_GUARD_REBUILD_TEST_CRASH_POINT": "after-lock",
            "LLM_GUARD_REBUILD_TEST_CRASH_MARKER": "/tmp/never-created",
        }
        for name, value in names.items():
            with self.subTest(name=name):
                result = subprocess.run(
                    ["/usr/bin/python3", str(ENGINE)],
                    cwd=ROOT,
                    env={name: value},
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)


class SharedBoundedProcessRecoveryTests(unittest.TestCase):
    @staticmethod
    def _load_bounded():
        spec = importlib.util.spec_from_file_location("bounded_recovery_test", BOUNDED)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_escaped_term_ignoring_flooders_are_killed_and_reaped(self) -> None:
        bounded = self._load_bounded()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pids_path = root / "pids"
            script = root / "hostile-tree.py"
            script.write_text(
                "import os,signal,sys,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "child=os.fork()\n"
                "if child==0:\n"
                " os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                " with open(sys.argv[1],'a') as f: f.write(str(os.getpid())+'\\n')\n"
                " chunk=b'x'*65536\n"
                " try:\n"
                "  while True: os.write(1,chunk); os.write(2,chunk)\n"
                " except OSError:\n"
                "  while True: time.sleep(1)\n"
                "with open(sys.argv[1],'a') as f: f.write(str(os.getpid())+'\\n')\n"
                "chunk=b'y'*65536\n"
                "while True: os.write(1,chunk); os.write(2,chunk)\n"
            )
            pids: set[int] = set()
            try:
                with self.assertRaises(RuntimeError):
                    bounded.command(
                        [sys.executable, str(script), str(pids_path)], timeout=1
                    )
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not pids_path.exists():
                    time.sleep(0.01)
                self.assertTrue(pids_path.exists())
                pids = {
                    int(line)
                    for line in pids_path.read_text().splitlines()
                    if line.isdigit()
                }
                self.assertGreaterEqual(len(pids), 2)
                survivors = {pid for pid in pids if Path(f"/proc/{pid}").exists()}
                self.assertEqual(survivors, set())
            finally:
                if pids_path.exists():
                    pids.update(
                        int(line)
                        for line in pids_path.read_text().splitlines()
                        if line.isdigit()
                    )
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_pidfd_failure_still_contains_escaped_descendant_only(self) -> None:
        bounded = self._load_bounded()
        unrelated = subprocess.Popen(["/usr/bin/sleep", "30"])
        escaped_pid = 0
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "escaped.pid"
            hostile = Path(temporary) / "escaped.py"
            hostile.write_text(
                "import os,signal,sys,time\n"
                "child=os.fork()\n"
                "if child==0:\n"
                " os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                " open(sys.argv[1],'w').write(str(os.getpid()))\n"
                " while True: time.sleep(1)\n"
                "while True: time.sleep(1)\n"
            )
            try:
                with (
                    patch.object(
                        bounded.os,
                        "pidfd_open",
                        side_effect=OSError(errno.EMFILE, "forced pidfd exhaustion"),
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    bounded.command(
                        [sys.executable, str(hostile), str(pid_path)], timeout=1
                    )
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not pid_path.exists():
                    time.sleep(0.01)
                self.assertTrue(pid_path.exists(), "escaped child did not publish PID")
                escaped_pid = int(pid_path.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(escaped_pid, 0)
                self.assertIsNone(unrelated.poll(), "unrelated process was disturbed")
            finally:
                for pid in (escaped_pid, unrelated.pid):
                    if pid:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            os.waitpid(pid, 0)
                        except (ChildProcessError, ProcessLookupError):
                            pass


if __name__ == "__main__":
    unittest.main()
