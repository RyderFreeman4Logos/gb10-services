from __future__ import annotations

import importlib.util
import json
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from embedding_activation_fixtures import (  # noqa: E402
    ACTIVATION_ENGINE,
    ACTIVATOR,
    CANONICAL_UNIT,
    UNIT,
    ActivationFixture,
)


class EmbeddingActivationTransactionTests(unittest.TestCase):
    def assert_restored(self, fixture: ActivationFixture, *, present: bool = True) -> None:
        if present:
            self.assertEqual(fixture.installed_unit.read_bytes(), fixture.prior_bytes)
            self.assertEqual(fixture.installed_unit.stat().st_mode & 0o777, fixture.prior_mode)
        else:
            self.assertFalse(fixture.installed_unit.exists())
        self.assertFalse(fixture.transaction().exists())
        receipt = fixture.state_root / "rollback.receipt.json"
        self.assertTrue(receipt.is_file())
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(receipt.read_text())["rollback"], "passed")

    def terminate_at_pause(
        self, fixture: ActivationFixture, signum: int
    ) -> subprocess.CompletedProcess[str]:
        process = fixture.spawn()
        try:
            fixture.wait_for_marker()
            os.killpg(process.pid, signum)
            stdout, stderr = process.communicate(timeout=12)
            return subprocess.CompletedProcess(
                process.args, process.returncode, stdout, stderr
            )
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_success_commits_private_receipt_after_strict_verifier(self) -> None:
        for optimized in (False, True):
            with self.subTest(optimized=optimized), ActivationFixture() as fixture:
                result = fixture.run(optimized=optimized)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(fixture.installed_unit.read_bytes(), CANONICAL_UNIT.read_bytes())
                self.assertEqual(fixture.installed_unit.stat().st_mode & 0o777, 0o644)
                transaction = fixture.transaction()
                self.assertEqual((transaction / "phase").read_text(), "committed\n")
                receipt_path = transaction / "activation.receipt.json"
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt["commit_requires_phase"], "committed")
                self.assertEqual(receipt["verification"], "passed")
                self.assertNotIn("transaction", receipt)
                self.assertNotIn("rollback_available", receipt)
                self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
                serialized = receipt_path.read_text()
                self.assertNotIn("InvocationID", serialized)
                self.assertNotIn("MainPID", serialized)
                self.assertNotIn("1" * 32, serialized)
                log = fixture.log()
                self.assertIn("verifier argc=1", log)
                self.assertEqual(log.count(f"restart {UNIT}"), 1)
                for neighbor in (
                    "vllm-aeon-27b-dflash.service",
                    "querit-4b-reranker.service",
                    "vllm-qwen3-reranker-8b.service",
                ):
                    self.assertNotIn(f"restart {neighbor}", log)
                    self.assertNotIn(f"stop {neighbor}", log)

    def test_verification_generation_and_receipt_failures_all_roll_back(self) -> None:
        cases = (
            ("verify_status", 9, ""),
            ("same_generation", True, ""),
            ("ready_timeout", True, ""),
            ("drift_after_models", True, ""),
            ("fail_at", "", "before_receipt"),
            ("fail_at", "", "after_receipt"),
        )
        for field, value, fail_at in cases:
            with self.subTest(field=field, fail_at=fail_at), ActivationFixture() as fixture:
                if field == "fail_at":
                    fixture.hooks["fail_at"] = fail_at
                else:
                    fixture.state[field] = value
                result = fixture.run(optimized=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_restored(fixture)
                self.assertFalse((fixture.state_root / "activation.receipt.json").exists())
                self.assertGreaterEqual(fixture.log().count(f"restart {UNIT}"), 2)

    def test_rollback_does_not_rewrite_unit_after_daemon_reload(self) -> None:
        with ActivationFixture() as fixture:
            fixture.state["verify_status"] = 9
            result = fixture.run()
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_restored(fixture)
            state = json.loads(fixture.state_path.read_text())
            snapshots = state["daemon_reload_units"]
            self.assertEqual(len(snapshots), 2)
            rollback_snapshot = snapshots[-1]
            self.assertEqual(rollback_snapshot["payload"], fixture.prior_bytes.hex())
            self.assertEqual(rollback_snapshot["mode"], fixture.prior_mode)
            self.assertEqual(
                rollback_snapshot["inode"], fixture.installed_unit.stat().st_ino
            )

    def test_hup_int_and_term_after_install_restore_exact_prior_unit(self) -> None:
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum), ActivationFixture() as fixture:
                fixture.hooks["pause_at"] = "after_install"
                fixture.save()
                result = self.terminate_at_pause(fixture, signum)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_restored(fixture)

    def test_signal_after_receipt_is_precommit_and_rolls_back(self) -> None:
        with ActivationFixture() as fixture:
            fixture.hooks["pause_at"] = "after_receipt"
            fixture.save()
            result = self.terminate_at_pause(fixture, signal.SIGTERM)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_restored(fixture)
            self.assertFalse((fixture.state_root / "activation.receipt.json").exists())

    def test_committed_phase_is_authoritative_during_cleanup_interruption(self) -> None:
        with ActivationFixture() as fixture:
            fixture.hooks["pause_at"] = "after_commit"
            fixture.save()
            result = self.terminate_at_pause(fixture, signal.SIGTERM)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((fixture.transaction() / "phase").read_text(), "committed\n")
            self.assertEqual(fixture.installed_unit.read_bytes(), CANONICAL_UNIT.read_bytes())
            self.assertNotIn("rollback", fixture.log())

    def test_lock_contender_cannot_touch_owners_transaction(self) -> None:
        with ActivationFixture() as fixture:
            fixture.hooks["pause_at"] = "after_install"
            fixture.save()
            owner = fixture.spawn()
            try:
                fixture.wait_for_marker()
                events_before = fixture.log()
                contender = fixture.run()
                self.assertNotEqual(contender.returncode, 0, contender.stdout + contender.stderr)
                self.assertIn("holds the lock", contender.stderr)
                self.assertEqual(fixture.log(), events_before)
                fixture.release.write_text("release\n")
                stdout, stderr = owner.communicate(timeout=15)
                self.assertEqual(owner.returncode, 0, stdout + stderr)
            finally:
                if owner.poll() is None:
                    os.killpg(owner.pid, signal.SIGKILL)
                    owner.communicate(timeout=5)

    def test_sigkill_stale_transaction_is_recovered_before_new_activation(self) -> None:
        with ActivationFixture() as fixture:
            fixture.hooks["pause_at"] = "after_install"
            fixture.save()
            process = fixture.spawn()
            try:
                fixture.wait_for_marker()
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
            self.assertTrue(fixture.transaction().is_dir())
            fixture.source_unit.unlink()
            fixture.hooks["pause_at"] = ""
            recovered = fixture.run()
            self.assertNotEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertIn("rerun activation", recovered.stderr)
            self.assert_restored(fixture)
            fixture.source_unit.write_bytes(CANONICAL_UNIT.read_bytes())
            fixture.source_unit.chmod(0o644)
            activated = fixture.run()
            self.assertEqual(activated.returncode, 0, activated.stdout + activated.stderr)

    def test_sigkill_after_receipt_cannot_leave_a_committed_claim(self) -> None:
        with ActivationFixture() as fixture:
            fixture.hooks["pause_at"] = "after_receipt"
            process = fixture.spawn()
            try:
                fixture.wait_for_marker()
                public_receipt = fixture.state_root / "activation.receipt.json"
                payload = json.loads(public_receipt.read_text())
                self.assertEqual(payload["commit_requires_phase"], "committed")
                self.assertNotIn("transaction", payload)
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
            fixture.hooks["pause_at"] = ""
            recovered = fixture.run()
            self.assertNotEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assert_restored(fixture)
            self.assertFalse((fixture.state_root / "activation.receipt.json").exists())

    def test_sigkill_during_rollback_resumes_rolling_back_phase(self) -> None:
        with ActivationFixture() as fixture:
            fixture.state["verify_status"] = 9
            fixture.hooks["pause_at"] = "rollback_started"
            fixture.save()
            process = fixture.spawn()
            try:
                fixture.wait_for_marker()
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
            self.assertEqual((fixture.transaction() / "phase").read_text(), "rolling_back\n")
            fixture.hooks["pause_at"] = ""
            fixture.state["verify_status"] = 0
            recovered = fixture.run()
            self.assertNotEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assert_restored(fixture)

    def test_term_during_rollback_is_deferred_until_rollback_finishes(self) -> None:
        with ActivationFixture() as fixture:
            fixture.state["verify_status"] = 9
            fixture.hooks["pause_at"] = "rollback_started"
            process = fixture.spawn()
            try:
                fixture.wait_for_marker()
                os.killpg(process.pid, signal.SIGTERM)
                time.sleep(0.1)
                self.assertIsNone(process.poll(), "rollback was interrupted by SIGTERM")
                self.assertEqual(
                    (fixture.transaction() / "phase").read_text(), "rolling_back\n"
                )
                fixture.release.write_text("release\n")
                stdout, stderr = process.communicate(timeout=15)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assert_restored(fixture)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)

    def test_failed_rollback_preserves_private_state_and_is_recoverable(self) -> None:
        for failure in ("fail_rollback_restart", "rollback_same_generation"):
            with self.subTest(failure=failure), ActivationFixture() as fixture:
                fixture.state.update({"verify_status": 9, failure: True})
                failed = fixture.run(timeout=25)
                self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
                self.assertTrue(fixture.transaction().is_dir())
                self.assertEqual(
                    (fixture.transaction() / "phase").read_text(), "rollback_failed\n"
                )
                self.assertEqual(fixture.transaction().stat().st_mode & 0o777, 0o700)
                fixture.state.update({"verify_status": 0, failure: False})
                recovered = fixture.run()
                self.assertNotEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                self.assert_restored(fixture)

    def test_explicit_prior_absence_is_restored_on_failure(self) -> None:
        with ActivationFixture(prior_present=False) as fixture:
            fixture.state["verify_status"] = 9
            result = fixture.run()
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_restored(fixture, present=False)
            receipt = json.loads((fixture.state_root / "rollback.receipt.json").read_text())
            self.assertFalse(receipt["restored_presence"])

    def test_source_mutation_after_prepare_is_rejected_and_rolled_back(self) -> None:
        with ActivationFixture() as fixture:
            fixture.hooks["pause_at"] = "prepared"
            fixture.save()
            process = fixture.spawn()
            try:
                fixture.wait_for_marker()
                fixture.source_unit.write_bytes(fixture.source_unit.read_bytes() + b"# drift\n")
                fixture.release.write_text("release\n")
                stdout, stderr = process.communicate(timeout=15)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assert_restored(fixture)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
        with ActivationFixture() as fixture:
            fixture.hooks["pause_at"] = "after_restart"
            process = fixture.spawn()
            try:
                fixture.wait_for_marker()
                fixture.installed_unit.write_bytes(b"hostile installed unit drift\n")
                fixture.installed_unit.chmod(0o644)
                fixture.release.write_text("release\n")
                stdout, stderr = process.communicate(timeout=15)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assert_restored(fixture)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)

    def test_verifier_authority_mutation_before_execution_is_detected(self) -> None:
        with ActivationFixture() as fixture:
            fixture.hooks["pause_at"] = "after_restart"
            process = fixture.spawn()
            try:
                fixture.wait_for_marker()
                fixture.verifier.write_text(
                    fixture.verifier.read_text() + "\n# hostile drift\n"
                )
                fixture.verifier.chmod(0o700)
                fixture.release.write_text("release\n")
                stdout, stderr = process.communicate(timeout=15)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assert_restored(fixture)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)

    def test_timed_out_command_reaps_descendants_and_still_rolls_back(self) -> None:
        with ActivationFixture() as fixture:
            fixture.state["hang_once"] = "daemon-reload"
            result = fixture.run(timeout=20)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_restored(fixture)
            self.assertTrue(fixture.child_pid_path.exists())
            child_pid = int(fixture.child_pid_path.read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_production_wrapper_has_no_test_or_environment_override_channel(self) -> None:
        source = ACTIVATOR.read_text()
        self.assertNotIn("--test-only", source)
        self.assertNotIn("GB10_EMBEDDING_ACTIVATE_", source)
        self.assertTrue(source.startswith("#!/usr/bin/bash\n"))
        self.assertIn("/usr/bin/python3 -I -B -S", source)
        rejected = subprocess.run(
            ["/usr/bin/bash", str(ACTIVATOR), "--test-only", "/tmp/not-used"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        production = (ACTIVATOR.parent / "gb10_embedding_activation.py").read_text()
        self.assertNotIn("os.environ.get", production)
        self.assertNotIn("os.getenv", production)
        self.assertNotIn("assert ", production)

    def test_wrapper_rejects_engine_substitution_before_python_executes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary)
            wrapper = copied / ACTIVATOR.name
            engine = copied / ACTIVATION_ENGINE.name
            wrapper.write_bytes(ACTIVATOR.read_bytes())
            wrapper.chmod(0o755)
            marker = copied / "substituted-engine-executed"
            engine_source = ACTIVATION_ENGINE.read_text().replace(
                "from __future__ import annotations\n",
                "from __future__ import annotations\n"
                f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')\n"
                "raise SystemExit(99)\n",
                1,
            )
            engine.write_text(engine_source)
            result = subprocess.run(
                ["/usr/bin/bash", str(wrapper)],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 99, result.stdout + result.stderr)
            self.assertFalse(marker.exists())

    def test_dependency_substitution_is_rejected_before_import_side_effects(self) -> None:
        production_files = (
            ACTIVATION_ENGINE,
            ACTIVATION_ENGINE.parent / "gb10_embedding_activation_checks.py",
            ACTIVATION_ENGINE.parent / "gb10_embedding_activation_config.py",
            ACTIVATION_ENGINE.parent / "gb10_embedding_activation_storage.py",
            ACTIVATION_ENGINE.parent / "gb10_embedding_profile_contract.py",
            ACTIVATION_ENGINE.parent / "gb10_embedding_verifier_runtime.py",
            ACTIVATION_ENGINE.parent / "gb10_verify_embedding_profile.py",
        )
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary)
            for source in production_files:
                (copied / source.name).write_bytes(source.read_bytes())
            marker = copied / "substituted-dependency-imported"
            runtime = copied / "gb10_embedding_verifier_runtime.py"
            runtime.write_text(
                runtime.read_text().replace(
                    "import json\n",
                    "import json\n"
                    f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')\n",
                    1,
                )
            )
            result = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "-S",
                    str(copied / ACTIVATION_ENGINE.name),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists())

    def test_insecure_state_parent_is_rejected_before_transaction_creation(self) -> None:
        with ActivationFixture() as fixture:
            fixture.state_root.parent.chmod(0o777)
            result = fixture.run()
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(fixture.state_root.exists())

    def test_production_verifier_authority_hashes_match_fixed_sources(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "embedding_activation_authority_under_test", ACTIVATION_ENGINE
        )
        if spec is None or spec.loader is None:
            self.fail("could not load activation engine")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "HOME": temporary,
                "XDG_CONFIG_HOME": str(Path(temporary) / "hostile-config"),
                "XDG_STATE_HOME": str(Path(temporary) / "hostile-state"),
                "PYTHONPATH": temporary,
            },
        ):
            config = module._production_config(ACTIVATION_ENGINE)
        account_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        self.assertEqual(config.unit_dir, account_home / ".config/systemd/user")
        self.assertEqual(
            config.state_root,
            account_home / ".local/state/gb10-embedding-activation",
        )
        snapshot = module._verifier_authority_snapshot(config)
        self.assertEqual(set(snapshot), {str(path) for path in config.verifier_authority})


if __name__ == "__main__":
    unittest.main()
