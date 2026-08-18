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

    def test_recovery_rejects_replaced_snapshot_root_without_touching_target(
        self,
    ) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            snapshot = Path(json.loads(state.read_text())["snapshot_root"])
            owned = snapshot.with_name(snapshot.name + ".owned")
            snapshot.rename(owned)
            victim = fixture.root / "external-snapshot-target"
            victim.mkdir()
            sentinel = victim / "keep.txt"
            sentinel.write_text("keep\n")
            snapshot.symlink_to(victim, target_is_directory=True)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assertTrue(sentinel.exists(), output)
            self.assertTrue(snapshot.is_symlink(), output)
            self.assertTrue(state.exists(), output)
            self.assert_no_completion(output)

    def test_recovery_rejects_snapshot_directory_and_leaf_replacements(self) -> None:
        for replacement in ("directory", "leaf"):
            with self.subTest(replacement=replacement), RebuildFixture() as fixture:
                state = fixture.write_wal("mutated", mutate=True)
                snapshot = Path(json.loads(state.read_text())["snapshot_root"])
                snapshot.rename(snapshot.with_name(snapshot.name + ".owned"))
                if replacement == "directory":
                    snapshot.mkdir()
                    sentinel = snapshot / "keep.txt"
                    sentinel.write_text("keep\n")
                else:
                    snapshot.write_text("keep\n")
                    sentinel = snapshot

                result = fixture.run(timeout=15)
                output = self.output(result)

                self.assertNotEqual(result.returncode, 0, output)
                self.assertEqual(sentinel.read_text(), "keep\n")
                self.assertTrue(state.exists(), output)
                self.assert_no_completion(output)

    def test_recovery_tombstones_transaction_with_orphan_state_temp(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            orphan = state.parent / ".state.tmp.1234.deadbeef"
            orphan.write_bytes(state.read_bytes())
            orphan.chmod(0o600)

            first = fixture.run(timeout=15)
            first_output = self.output(first)

            self.assertEqual(first.returncode, 75, first_output)
            self.assertIn(RECOVERED, first_output)
            self.assertFalse(state.parent.exists(), first_output)
            self.assertFalse(orphan.exists(), first_output)
            fixture.assert_prior_restored(self)

            second = fixture.run(timeout=20)
            second_output = self.output(second)
            self.assertEqual(second.returncode, 0, second_output)
            self.assertIn(TEST_COMPLETE, second_output)
            self.assertFalse(self.transaction_state(fixture).exists())

    def test_recovery_finishes_exact_snapshot_cleanup_tombstone(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            snapshot = Path(json.loads(state.read_text())["snapshot_root"])
            nested = snapshot / "read-only" / "input.txt"
            nested.parent.mkdir()
            nested.write_text("owned\n")
            nested.parent.chmod(0o500)
            tombstone = snapshot.with_name(
                f".{snapshot.name}.cleanup.1234.deadbeef"
            )
            snapshot.rename(tombstone)

            first = fixture.run(timeout=15)
            first_output = self.output(first)

            self.assertEqual(first.returncode, 75, first_output)
            self.assertFalse(tombstone.exists(), first_output)
            self.assertFalse(state.exists(), first_output)
            fixture.assert_prior_restored(self)

            second = fixture.run(timeout=20)
            self.assertEqual(second.returncode, 0, self.output(second))

    def test_exact_snapshot_cleanup_does_not_follow_nested_symlink(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            snapshot = Path(json.loads(state.read_text())["snapshot_root"])
            victim = fixture.root / "external-cleanup-target"
            victim.mkdir()
            sentinel = victim / "keep.txt"
            sentinel.write_text("keep\n")
            (snapshot / "escape").symlink_to(victim, target_is_directory=True)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            self.assertEqual(sentinel.read_text(), "keep\n")
            self.assertFalse(state.exists(), output)
            fixture.assert_prior_restored(self)

    def test_complete_initial_publication_temp_is_promoted_and_recovered(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            temporary = state.parent.with_name(
                ".transaction.v1.tmp.1234.deadbeef"
            )
            state.parent.rename(temporary)
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            self.assertIn(RECOVERED, output)
            self.assertFalse(temporary.exists(), output)
            self.assertFalse(self.transaction_state(fixture).exists(), output)
            self.assertNotIn("cargo build", fixture.calls())
            fixture.assert_prior_restored(self)

    def test_incomplete_initial_publication_temp_is_retained_and_refused(self) -> None:
        with RebuildFixture() as fixture:
            temporary = fixture.receipt_dir / ".transaction.v1.tmp.1234.deadbeef"
            temporary.mkdir(mode=0o700, parents=True)
            fixture.receipt_dir.chmod(0o700)
            partial = temporary / ".state.tmp.1234.deadbeef"
            partial.write_text('{"schema":1')
            partial.chmod(0o600)
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=10)
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("prepublication", output)
            self.assertTrue(temporary.exists(), output)
            self.assertTrue(partial.exists(), output)
            self.assertEqual(fixture.calls(), "")
            self.assert_no_completion(output)

    def test_empty_initial_publication_temp_is_retained_and_classified(self) -> None:
        with RebuildFixture() as fixture:
            temporary = fixture.receipt_dir / ".transaction.v1.tmp.1234.deadbeef"
            temporary.mkdir(mode=0o700, parents=True)
            fixture.receipt_dir.chmod(0o700)
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=10)
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("empty prepublication", output)
            self.assertTrue(temporary.exists(), output)
            self.assertEqual(fixture.calls(), "")
            self.assert_no_completion(output)

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

    def test_unsafe_snapshot_name_blocks_before_tools_or_recovery_mutation(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated")
            payload = json.loads(state.read_text())
            snapshot = Path(payload["snapshot_root"])
            unsafe = snapshot.with_name(".rebuild-input-unsafe")
            snapshot.rename(unsafe)
            payload["snapshot_root"] = str(unsafe)
            state.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )
            fixture.tool_log.unlink(missing_ok=True)
            before_link = os.readlink(fixture.service_bin)

            result = fixture.run(timeout=10)
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("transaction snapshot name is unsafe", output)
            self.assertEqual(fixture.calls(), "")
            self.assertEqual(os.readlink(fixture.service_bin), before_link)
            self.assertTrue(state.exists())

    def test_committed_executable_one_field_mismatch_matrix_is_rejected(self) -> None:
        mutations: dict[str, object] = {
            "device": 1,
            "inode": 1,
            "size": 1,
            "mtime_ns": 1,
            "ctime_ns": 1,
            "sha256": "f" * 64,
            "build_id": "abcdef12",
            "mode": 0o700,
            "uid": os.geteuid() + 1,
            "nlink": 2,
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field), RebuildFixture() as fixture:
                state = fixture.write_wal("committed", mutate=True)
                payload = json.loads(state.read_text())
                current = payload["committed"]["executable"][field]
                if isinstance(replacement, int) and field not in {"mode", "uid", "nlink"}:
                    replacement = current + replacement
                payload["committed"]["executable"][field] = replacement
                state.write_text(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
                )
                fixture.tool_log.unlink(missing_ok=True)

                result = fixture.run(timeout=10)
                output = self.output(result)

                self.assertNotEqual(result.returncode, 0, output)
                self.assertTrue(state.exists(), output)
                self.assertEqual(fixture.receipt_paths(), [])

    def test_exact_committed_executable_evidence_archives(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("committed", mutate=True)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            self.assertFalse(state.exists(), output)
            receipts = fixture.receipt_paths()
            self.assertEqual(len(receipts), 1, output)
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(
                receipt["committed"]["executable"],
                receipt["candidate"]["identity"],
            )

    def test_committed_archive_discards_recognized_state_temp(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("committed", mutate=True)
            orphan = state.parent / ".state.tmp.1234.deadbeef"
            orphan.write_bytes(state.read_bytes())
            orphan.chmod(0o600)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            receipts = fixture.receipt_paths()
            self.assertEqual(len(receipts), 1, output)
            self.assertEqual(
                sorted(path.name for path in receipts[0].parent.iterdir()),
                ["state.json"],
            )

    def test_prior_absence_requires_stable_original_path_before_build(self) -> None:
        with RebuildFixture() as fixture:
            fixture.service_bin.unlink()
            original = fixture.prior.with_name("prior-held-object")
            fixture.prior.rename(original)
            fixture.prior.symlink_to(original)
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=10)
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assertFalse(fixture.service_bin.exists())
            self.assertFalse(self.transaction_state(fixture).exists())
            self.assertNotIn("cargo build", fixture.calls())
            self.assert_no_completion(output)

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
                os.readlink(fixture.service_bin), blocked["prior"]["running_link"]
            )
            self.assert_no_completion(output)

    def test_prior_absence_rollback_restarts_exact_stable_original_path(self) -> None:
        with RebuildFixture() as fixture:
            fixture.service_bin.unlink()

            result = fixture.run(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE": "write",
                    "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "5",
                },
                timeout=20,
            )
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("ROLLBACK_RESTORED=1", output)
            self.assertFalse(fixture.service_bin.exists())
            self.assertFalse(fixture.service_bin.is_symlink())
            self.assertEqual(
                fixture.reload_state()["running_target"], str(fixture.prior)
            )
            self.assertTrue(fixture.prior.exists())
            self.assertFalse(self.transaction_state(fixture).exists(), output)

    def test_forward_accepts_restart_completed_before_first_job_observation(
        self,
    ) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(job_behavior="complete-before-observation")

            result = fixture.run(timeout=20)
            output = self.output(result)

            self.assertEqual(result.returncode, 0, output)
            self.assertIn(TEST_COMPLETE, output)
            self.assertEqual(
                fixture.reload_state()["running_target"], str(fixture.candidate)
            )
            self.assertNotIn("systemctl --user cancel", fixture.calls())

    def test_recovery_accepts_restart_completed_before_first_job_observation(
        self,
    ) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            fixture.set_state(job_behavior="complete-before-observation")
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            self.assertIn(RECOVERED, output)
            self.assertFalse(state.exists(), output)
            fixture.assert_prior_restored(self)
            self.assertNotIn("systemctl --user cancel", fixture.calls())

    def test_forward_accepts_same_restart_job_waiting_to_running(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(job_behavior="waiting-to-running", job_state="waiting")

            result = fixture.run(timeout=20)
            output = self.output(result)

            self.assertEqual(result.returncode, 0, output)
            self.assertIn(TEST_COMPLETE, output)
            self.assertEqual(
                fixture.reload_state()["running_target"], str(fixture.candidate)
            )

    def test_recovery_accepts_same_restart_job_waiting_to_running(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            fixture.set_state(job_behavior="waiting-to-running", job_state="waiting")
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            self.assertIn(RECOVERED, output)
            self.assertFalse(state.exists(), output)
            fixture.assert_prior_restored(self)

    def test_disappeared_restart_without_fresh_runtime_fails_closed(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(job_behavior="disappear-without-activation")

            result = fixture.run(timeout=20)
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assert_no_completion(output)
            fixture.assert_prior_restored(self)

    def test_foreign_unit_with_owned_job_id_is_never_cancelled(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("committed", mutate=True)
            payload = json.loads(state.read_text())
            payload["manager_job_ids"] = [99]
            payload["restart_intent"] = True
            state.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )
            fixture.set_state(
                jobs={"99": str(fixture.candidate)},
                job_behavior="nonterminal",
                job_unit="foreign.service",
            )
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            self.assertNotIn("systemctl --user cancel 99", fixture.calls())
            self.assertEqual(
                fixture.reload_state()["jobs"], {"99": str(fixture.candidate)}
            )
            self.assertFalse(state.exists(), output)

    def test_multiple_dispatched_jobs_are_never_adopted_or_cancelled(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(job_behavior="multiple-on-dispatch")

            result = fixture.run(
                extra_env={"LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "2"},
                timeout=20,
            )
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assert_no_completion(output)
            self.assertNotIn("systemctl --user cancel 41", fixture.calls())
            self.assertNotIn("systemctl --user cancel 42", fixture.calls())
            self.assertTrue(self.transaction_state(fixture).exists(), output)

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

    def test_foreign_current_manager_job_is_waited_not_cancelled(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            fixture.set_state(
                jobs={"99": str(fixture.candidate)},
                job_behavior="normal",
                job_polls_remaining=1,
                systemctl_call_limit=12,
                systemctl_calls=0,
            )
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(
                extra_env={"LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "4"},
                timeout=15,
            )
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            self.assertNotIn("systemctl --user cancel 99", fixture.calls())
            self.assertNotIn("bounded command failed", output)
            self.assertFalse(state.exists(), output)
            fixture.assert_prior_restored(self)

    def test_owned_exact_manager_job_is_cancelled(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            payload = json.loads(state.read_text())
            payload["manager_job_ids"] = [99]
            payload["restart_intent"] = True
            state.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )
            fixture.set_state(
                jobs={"99": str(fixture.candidate)},
                job_behavior="cancel-owned-then-normal",
            )
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=15)
            output = self.output(result)

            self.assertEqual(result.returncode, 75, output)
            self.assertIn("systemctl --user cancel 99", fixture.calls())
            self.assertFalse(state.exists(), output)
            fixture.assert_prior_restored(self)

    def test_manager_generation_drift_never_cancels_or_recovers(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            payload = json.loads(state.read_text())
            payload["manager_job_ids"] = [99]
            payload["restart_intent"] = True
            state.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )
            fixture.set_state(
                manager_invocation="e" * 32,
                jobs={"99": str(fixture.candidate)},
                job_behavior="nonterminal",
            )
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(timeout=10)
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("user manager generation differs", output)
            self.assertNotIn("systemctl --user cancel 99", fixture.calls())
            self.assertTrue(state.exists(), output)

    def test_reused_job_id_with_foreign_type_is_never_cancelled(self) -> None:
        with RebuildFixture() as fixture:
            state = fixture.write_wal("mutated", mutate=True)
            payload = json.loads(state.read_text())
            payload["manager_job_ids"] = [99]
            payload["restart_intent"] = True
            state.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )
            fixture.set_state(
                jobs={"99": str(fixture.candidate)},
                job_behavior="nonterminal",
                job_type="start",
                systemctl_call_limit=12,
                systemctl_calls=0,
            )
            fixture.tool_log.unlink(missing_ok=True)

            result = fixture.run(
                extra_env={"LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "1"},
                timeout=10,
            )
            output = self.output(result)

            self.assertNotEqual(result.returncode, 0, output)
            self.assertNotIn("systemctl --user cancel 99", fixture.calls())
            self.assertTrue(state.exists(), output)
            self.assertIn("deadline", output)
            self.assertNotIn("bounded command failed", output)

    def test_dispatch_to_record_crash_gap_waits_unknown_job(self) -> None:
        with RebuildFixture() as fixture:
            marker = fixture.root / "crash-forward-job-dispatched"
            process = fixture.popen(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_CRASH_POINT": "forward-job-dispatched",
                    "LLM_GUARD_REBUILD_TEST_CRASH_MARKER": str(marker),
                }
            )
            try:
                self.assertTrue(self.wait_for_path(marker, process, 15))
            finally:
                self.kill_fixture_process(process)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            state = self.transaction_state(fixture)
            payload = json.loads(state.read_text())
            self.assertTrue(payload["restart_intent"])
            self.assertEqual(payload["manager_job_ids"], [])
            fixture.tool_log.unlink(missing_ok=True)

            recovered = fixture.run(timeout=15)
            output = self.output(recovered)

            self.assertEqual(recovered.returncode, 75, output)
            self.assertNotIn("systemctl --user cancel 41", fixture.calls())
            self.assertFalse(state.exists(), output)
            fixture.assert_prior_restored(self)

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

    def test_direct_recovery_stages_share_one_absolute_deadline(self) -> None:
        for stage in ("filesystem-read", "snapshot-cleanup", "rollback", "diagnostic"):
            with self.subTest(stage=stage), RebuildFixture() as fixture:
                state = fixture.write_wal("mutated", mutate=True)
                started = time.monotonic()

                result = fixture.run(
                    extra_env={
                        "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS": "1",
                        "LLM_GUARD_REBUILD_TEST_DELAY_STAGE": stage,
                        "LLM_GUARD_REBUILD_TEST_DELAY_SECONDS": "3",
                    },
                    timeout=3,
                )
                elapsed = time.monotonic() - started
                output = self.output(result)

                self.assertNotEqual(result.returncode, 0, output)
                self.assertLess(elapsed, 1.8, (stage, elapsed, output))
                self.assertIn("deadline", output)
                self.assertTrue(state.exists(), output)
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
            "LLM_GUARD_REBUILD_TEST_DELAY_STAGE": "filesystem-read",
            "LLM_GUARD_REBUILD_TEST_DELAY_SECONDS": "1",
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

    def _exercise_child_scan_order(self, *, overflow: bool) -> None:
        bounded = self._load_bounded()
        events: list[tuple[str, int | None]] = []
        real_killpg = os.killpg
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import pathlib,signal,sys,time;"
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                    "pathlib.Path(sys.argv[1]).touch();time.sleep(30)",
                    str(ready),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            selector = bounded.selectors.DefaultSelector()
            streams: dict[int, tuple[str, object]] = {}
            tree = None
            try:
                ready_deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < ready_deadline:
                    self.assertIsNone(process.poll(), "fixture exited before readiness")
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "fixture did not become ready")
                tree = bounded._ProcessTree(process, set())
                captures = {
                    "stdout": bounded._Capture(bytearray()),
                    "stderr": bounded._Capture(bytearray()),
                }
                assert process.stdout is not None and process.stderr is not None
                for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                    descriptor = stream.fileno()
                    os.set_blocking(descriptor, False)
                    selector.register(descriptor, bounded.selectors.EVENT_READ)
                    streams[descriptor] = (name, stream)

                def child_pids(_pid: int) -> list[int]:
                    events.append(("scan", None))
                    if overflow:
                        raise bounded.BoundedProcessError(
                            "subprocess child list exceeded its bound"
                        )
                    return []

                def killpg(pid: int, number: int) -> None:
                    events.append(("group", number))
                    real_killpg(pid, number)

                with (
                    patch.object(bounded, "_child_pids", side_effect=child_pids),
                    patch.object(bounded.os, "killpg", side_effect=killpg),
                ):
                    if overflow:
                        with self.assertRaisesRegex(
                            bounded.BoundedProcessError,
                            "subprocess child list exceeded its bound",
                        ):
                            bounded._bounded_reap(
                                process,
                                tree,
                                selector,
                                streams,
                                captures,
                                time.monotonic() + 1,
                            )
                    else:
                        bounded._bounded_reap(
                            process,
                            tree,
                            selector,
                            streams,
                            captures,
                            time.monotonic() + 1,
                        )

                self.assertIn(("group", signal.SIGTERM), events)
                self.assertIn(("group", signal.SIGKILL), events)
                self.assertEqual(events[0], ("group", signal.SIGTERM))
                term = events.index(("group", signal.SIGTERM))
                kill = events.index(("group", signal.SIGKILL))
                self.assertLess(term, kill)
                self.assertEqual(events[term + 1], ("scan", None))
                self.assertEqual(events[kill + 1], ("scan", None))
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assertFalse(Path(f"/proc/{process.pid}").exists())
            finally:
                if process.poll() is None:
                    try:
                        real_killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=3)
                for descriptor in list(streams):
                    bounded._close_stream(selector, streams, descriptor)
                selector.close()
                if tree is not None:
                    tree.close()

    def test_child_scan_overflow_is_delayed_until_group_term_kill_and_reap(self) -> None:
        self._exercise_child_scan_order(overflow=True)

    def test_normal_child_scan_follows_group_term_and_kill_before_reap(self) -> None:
        self._exercise_child_scan_order(overflow=False)

    def test_scope_cleanup_orders_group_then_scope_then_descendant_scan(self) -> None:
        bounded = self._load_bounded()
        events: list[tuple[str, int]] = []

        class Tree:
            def signal_group(self, number: int) -> None:
                events.append(("group", number))

            def signal_descendants(self, number: int) -> None:
                events.append(("scan", number))

        bounded._signal_cleanup(
            Tree(), lambda number: events.append(("scope", number)), signal.SIGTERM
        )
        self.assertEqual(
            events,
            [
                ("group", signal.SIGTERM),
                ("scope", signal.SIGTERM),
            ],
        )

        class CompleteTree(Tree):
            def reap_adopted(self) -> None:
                pass

            def survivors(self) -> list[int]:
                return []

        class CompleteProcess:
            pid = 424242
            returncode = 0

            def poll(self) -> int:
                return 0

            def wait(self, timeout: float) -> int:
                del timeout
                return 0

        completed_scope: list[int] = []
        completed_selector = bounded.selectors.DefaultSelector()
        try:
            bounded._bounded_reap(
                CompleteProcess(),
                CompleteTree(),
                completed_selector,
                {},
                {},
                time.monotonic() + 1,
                completed_scope.append,
            )
        finally:
            completed_selector.close()
        self.assertEqual(completed_scope, [signal.SIGTERM, signal.SIGKILL])

        baseline = bounded._direct_child_identities(os.getpid())
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "os.write(1,b'R');"
                "time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdout is not None
        self.assertEqual(process.stdout.read(1), b"R")
        process.stdout.close()
        tree = bounded._ProcessTree(process, baseline)
        selector = bounded.selectors.DefaultSelector()
        scope_events: list[int] = []
        real_signal_group = tree.signal_group
        interrupted = False

        def signal_group(number: int) -> None:
            nonlocal interrupted
            real_signal_group(number)
            if number == signal.SIGTERM and not interrupted:
                interrupted = True
                raise KeyboardInterrupt

        tree.signal_group = signal_group
        try:
            with self.assertRaises(KeyboardInterrupt):
                bounded._bounded_reap(
                    process,
                    tree,
                    selector,
                    {},
                    {},
                    time.monotonic() + 2,
                    scope_events.append,
                )
            self.assertIn(signal.SIGKILL, scope_events)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertFalse(Path(f"/proc/{process.pid}").exists())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            selector.close()
            tree.close()

        class InjectedRebuildError(RuntimeError):
            pass

        cleanup_events: list[tuple[str, int | None]] = []

        class InterruptedProcess:
            pid = 424243
            returncode: int | None = None

            def poll(self) -> int | None:
                cleanup_events.append(("poll", None))
                if any(
                    event == ("group", signal.SIGKILL) for event in cleanup_events
                ):
                    self.returncode = -signal.SIGKILL
                return self.returncode

            def wait(self, timeout: float) -> int:
                del timeout
                cleanup_events.append(("wait", None))
                self.returncode = -signal.SIGKILL
                return self.returncode

        interrupted_process = InterruptedProcess()

        class InterruptedTree(Tree):
            def signal_group(self, number: int) -> None:
                cleanup_events.append(("group", number))

            def reap_adopted(self) -> None:
                cleanup_events.append(("reap", None))

            def survivors(self) -> list[int]:
                return [] if interrupted_process.returncode is not None else [interrupted_process.pid]

        real_monotonic = bounded.time.monotonic
        clock_calls = 0

        def interrupted_clock() -> float:
            nonlocal clock_calls
            clock_calls += 1
            cleanup_events.append(("clock", None))
            if clock_calls == 2:
                raise InjectedRebuildError("TERM grace interrupted")
            return real_monotonic()

        interrupted_selector = bounded.selectors.DefaultSelector()
        interrupted_deadline = time.monotonic() + 1
        try:
            with (
                patch.object(bounded.time, "monotonic", side_effect=interrupted_clock),
                self.assertRaises(InjectedRebuildError),
            ):
                bounded._bounded_reap(
                    interrupted_process,
                    InterruptedTree(),
                    interrupted_selector,
                    {},
                    {},
                    interrupted_deadline,
                    lambda number: cleanup_events.append(("scope", number)),
                )
        finally:
            interrupted_selector.close()
        self.assertIn(("scope", signal.SIGKILL), cleanup_events)
        self.assertIn(("group", signal.SIGKILL), cleanup_events)
        self.assertIn(("reap", None), cleanup_events)
        self.assertEqual(interrupted_process.returncode, -signal.SIGKILL)
        self.assertEqual(InterruptedTree().survivors(), [])

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
        modeled = bounded._OwnedProcess(424242, 11, None)
        modeled_tree = object.__new__(bounded._ProcessTree)
        modeled_tree.scan = lambda: {modeled.pid: (os.getpid(), modeled.starttime)}
        modeled_tree.descendants = lambda _table: [modeled]
        raw_pid_signals: list[tuple[int, int]] = []
        with patch.object(
            bounded.os,
            "kill",
            side_effect=lambda pid, number: raw_pid_signals.append((pid, number)),
        ):
            with self.assertRaisesRegex(
                bounded.BoundedProcessError, "descendant pidfd"
            ):
                modeled_tree.signal_descendants(signal.SIGKILL)
        self.assertEqual(raw_pid_signals, [])

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
                    self.assertRaisesRegex(RuntimeError, "pidfd"),
                ):
                    bounded.command([sys.executable, str(hostile), str(pid_path)], timeout=1)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not pid_path.exists():
                    time.sleep(0.01)
                self.assertTrue(pid_path.exists(), "escaped child did not publish PID")
                escaped_pid = int(pid_path.read_text())
                os.kill(escaped_pid, 0)
                self.assertIsNone(unrelated.poll(), "unrelated process was disturbed")
            finally:
                if escaped_pid:
                    try:
                        os.kill(escaped_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        os.waitpid(escaped_pid, 0)
                    except (ChildProcessError, ProcessLookupError):
                        pass
                if unrelated.poll() is None:
                    unrelated.kill()
                unrelated.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
