from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import tomllib
from guard_rebuild_fixtures import RebuildFixture

ROOT = Path(__file__).resolve().parents[1]
REBUILD_SCRIPT = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.sh"
REBUILD_ENGINE = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.py"
GUARD_CONFIG = ROOT / "config" / "llm-guard-proxy" / "config.toml"
README = ROOT / "README.md"
DEPLOYMENT_GUIDE = ROOT / "docs" / "deployment" / "AGENTS.md"
PRODUCTION_COMPLETE = "LLM_GUARD_PROXY_REBUILD_COMPLETE"
TEST_COMPLETE = "LLM_GUARD_PROXY_REBUILD_TEST_ONLY_COMPLETE"


class GuardProductionFeatureContractTests(unittest.TestCase):
    def test_production_config_bounds_workflow_executions(self) -> None:
        # Active [guard_workflows] is temporarily commented: the installed
        # binary fail-closes on unknown sections. Keep the intended bound in
        # comments so deploy does not forget the value when re-enabled.
        text = GUARD_CONFIG.read_text()
        config = tomllib.loads(text)
        self.assertNotIn("guard_workflows", config)
        self.assertRegex(text, r"(?m)^# \[guard_workflows\]\s*$")
        self.assertRegex(text, r"(?m)^# max_in_flight_executions = 4\s*$")

    def test_docs_match_production_and_test_only_rebuild_boundaries(self) -> None:
        readme = README.read_text()
        deployment = DEPLOYMENT_GUIDE.read_text()
        for text in (readme, deployment):
            normalized = " ".join(text.split())
            for required in (
                "Production invocation accepts no override environment variables",
                "`--test-only`",
                "immutable Git archive",
                "`MainPID`",
                "`InvocationID`",
                "`/proc/<pid>/stat` starttime",
                "held `/proc/<pid>/exe` file descriptor",
                "atomic rename",
                "directory `fsync`",
                PRODUCTION_COMPLETE,
                "never restarts a vLLM backend",
            ):
                self.assertIn(required, normalized)


class GuardRebuildProvenanceTests(unittest.TestCase):
    def test_launcher_hash_pins_real_module_without_embedded_python(self) -> None:
        launcher = REBUILD_SCRIPT.read_text()
        self.assertTrue(REBUILD_ENGINE.is_file(), "real rebuild engine is missing")
        self.assertTrue(launcher.startswith("#!/usr/bin/bash -p\n"))
        self.assertLessEqual(len(launcher.splitlines()), 64)
        self.assertNotIn("<<'PY'", launcher)
        self.assertIn("/usr/bin/env -i", launcher)
        self.assertIn("/usr/bin/python3 -I -B -S", launcher)
        match = re.search(
            r'^expected_engine_sha256="([0-9a-f]{64})"$', launcher, re.MULTILINE
        )
        self.assertIsNotNone(match, "launcher engine authority is missing")
        assert match is not None
        self.assertEqual(
            hashlib.sha256(REBUILD_ENGINE.read_bytes()).hexdigest(), match.group(1)
        )
        self.assertIn("class RebuildError", REBUILD_ENGINE.read_text())

    def test_launcher_rejects_invalid_argv_before_engine_execution(self) -> None:
        usage = "usage: llm_guard_proxy_cached_rebuild.sh [--test-only]\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            launcher = root / REBUILD_SCRIPT.name
            engine = root / REBUILD_ENGINE.name
            marker = root / "engine-argv.json"
            engine.write_text(
                "import json\nimport sys\nfrom pathlib import Path\n"
                f"Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            )
            engine.chmod(0o644)
            engine_sha256 = hashlib.sha256(engine.read_bytes()).hexdigest()
            launcher_text = re.sub(
                r'^expected_engine_sha256="[0-9a-f]{64}"$',
                f'expected_engine_sha256="{engine_sha256}"',
                REBUILD_SCRIPT.read_text(),
                flags=re.MULTILINE,
            )
            launcher.write_text(
                launcher_text.replace(
                    '"$python_owner" != 0',
                    f'"$python_owner" != {os.stat("/usr/bin/python3.11").st_uid}',
                )
            )
            launcher.chmod(0o755)
            cases: tuple[tuple[tuple[str, ...], int, list[str] | None], ...] = (
                ((), 0, []),
                (("--test-only",), 0, ["--test-only"]),
                (("unexpected",), 64, None),
                (("--test-only", "extra"), 64, None),
                (("unexpected", "extra"), 64, None),
                (("--test-only", "extra", "more"), 64, None),
            )
            for arguments, expected_returncode, expected_engine_argv in cases:
                with self.subTest(arguments=arguments):
                    marker.unlink(missing_ok=True)
                    result = subprocess.run(
                        [
                            "/usr/bin/bash",
                            "-p",
                            "-c",
                            'launcher=$1; shift; unset PATH; source "$launcher" "$@"',
                            "_",
                            str(launcher),
                            *arguments,
                        ],
                        cwd=root,
                        env={},
                        text=True,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, expected_returncode, self.output(result)
                    )
                    if expected_engine_argv is None:
                        self.assertEqual(result.stdout, "")
                        self.assertEqual(result.stderr, usage)
                        self.assertFalse(marker.exists())
                    else:
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(
                            json.loads(marker.read_text()), expected_engine_argv
                        )

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_failed_without_completion(
        self, result: subprocess.CompletedProcess[str]
    ) -> str:
        output = self.output(result)
        self.assertNotEqual(result.returncode, 0, output)
        self.assertNotIn(PRODUCTION_COMPLETE, output)
        self.assertNotIn(TEST_COMPLETE, output)
        return output

    def test_production_rejects_overrides_before_build_or_link(self) -> None:
        with RebuildFixture() as fixture:
            bash_marker = fixture.root / "bash-env-ran"
            python_marker = fixture.root / "python-site-ran"
            bash_env = fixture.root / "bash-env"
            bash_env.write_text(f"printf x > {shlex.quote(str(bash_marker))}\n")
            python_path = fixture.root / "python-path"
            python_path.mkdir()
            (python_path / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(python_marker)!r}).write_text('x')\n"
            )
            result = fixture.run(
                test_only=False,
                extra_env={
                    "BASH_ENV": str(bash_env),
                    "LLM_GUARD_REBUILD_TEST_ONLY": "1",
                    "PYTHONPATH": str(python_path),
                },
            )
            output = self.assert_failed_without_completion(result)
            self.assertIn("--test-only", output)
            self.assertIn("LLM_GUARD_REBUILD_TEST_ONLY", output)
            self.assertEqual(fixture.calls(), "")
            self.assertFalse(bash_marker.exists())
            self.assertFalse(python_marker.exists())
            fixture.assert_prior_restored(self)

    def test_exact_candidate_uses_snapshot_and_durable_test_only_receipt(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run()
            output = self.output(result)
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("LLM_GUARD_REBUILD_TEST_ONLY=1", output)
            self.assertIn(TEST_COMPLETE, output)
            self.assertNotIn(PRODUCTION_COMPLETE, output)
            self.assertTrue(fixture.service_bin.is_symlink())
            self.assertEqual(os.readlink(fixture.service_bin), str(fixture.candidate))
            state = fixture.reload_state()
            self.assertEqual(state["running_target"], str(fixture.candidate))
            receipts = fixture.receipt_paths()
            self.assertEqual(len(receipts), 1)
            self.assertEqual(stat.S_IMODE(receipts[0].stat().st_mode), 0o600)
            completion = re.search(
                rf"{TEST_COMPLETE} receipt_sha256=([0-9a-f]{{64}})",
                output,
            )
            self.assertIsNotNone(completion)
            assert completion is not None
            self.assertEqual(
                completion.group(1),
                hashlib.sha256(receipts[0].read_bytes()).hexdigest(),
            )
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(receipt["schema"], 1)
            self.assertEqual(receipt["phase"], "committed")
            self.assertEqual(receipt["mode"], "test-only")
            authorities = receipt["authorities"]
            self.assertEqual(authorities["source_commit"], fixture.source_commit)
            self.assertEqual(authorities["source_tree"], fixture.source_tree)
            self.assertRegex(authorities["source_archive_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(authorities["snapshot_content_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(authorities["metadata_closure_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(authorities["sandbox_contract_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(authorities["build_inputs_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                set(authorities["tool_authorities"]),
                {
                    "ar",
                    "as",
                    "bwrap",
                    "cargo",
                    "cc",
                    "curl",
                    "git",
                    "git_remote_https",
                    "ionice",
                    "ld",
                    "nice",
                    "readelf",
                    "rustc",
                    "systemctl",
                },
            )
            self.assertEqual(
                receipt["candidate"]["identity"]["sha256"], fixture.binary_sha256
            )
            serialized = receipts[0].read_text()
            for forbidden in (
                "private_config_payload",
                "private-unit-payload",
                "credential",
                "prompt",
                "response",
                "header",
                "payload",
            ):
                self.assertNotIn(forbidden, serialized)
            calls = fixture.calls()
            metadata_line = next(
                line
                for line in calls.splitlines()
                if line.startswith("bwrap ") and " metadata " in f" {line} "
            )
            build_line = next(
                line
                for line in calls.splitlines()
                if line.startswith("bwrap ") and " build " in f" {line} "
            )
            metadata_arguments = shlex.split(metadata_line)
            build_arguments = shlex.split(build_line)
            metadata_target = metadata_arguments[
                metadata_arguments.index("/target") - 1
            ]
            build_target = build_arguments[build_arguments.index("/target") - 1]
            self.assertEqual(metadata_target, build_target)
            self.assertIn("--chdir /src", build_line)
            self.assertRegex(
                build_line,
                r"--ro-bind /proc/self/fd/[0-9]+ /toolchain/bin/cargo",
            )
            self.assertRegex(
                build_line,
                r"--ro-bind /proc/self/fd/[0-9]+ /toolchain/bin/rustc",
            )
            self.assertIn("-- /toolchain/bin/cargo build", build_line)
            self.assertIn("--release --frozen --locked --offline", build_line)
            self.assertIn("-p llm-guard-proxy --features guard", build_line)
            self.assertNotIn(str(fixture.source_dir), build_line)
            self.assertFalse(fixture.source_dir.exists())
            fixture.assert_no_backend_lifecycle(self)

    def test_wrong_hash_and_same_hash_other_inode_roll_back(self) -> None:
        for mode in ("wrong-hash", "same-hash-different-inode"):
            with self.subTest(mode=mode), RebuildFixture() as fixture:
                fixture.set_state(candidate_restart_mode=mode)
                output = self.assert_failed_without_completion(fixture.run())
                self.assertIn("identity", output)
                fixture.assert_prior_restored(self)
                fixture.assert_no_backend_lifecycle(self)

    def test_missing_conditional_tool_rolls_back_without_restart(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(
                extra_env={"LLM_GUARD_REBUILD_TEST_MISSING_TOOL": "curl"}
            )
            output = self.assert_failed_without_completion(result)
            self.assertIn("required tool unavailable: curl", output)
            fixture.assert_prior_restored(self)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)
            fixture.assert_no_backend_lifecycle(self)

    def test_inactive_and_unsupported_service_prestates_fail_before_mutation(
        self,
    ) -> None:
        with self.subTest(prestate="inactive"), RebuildFixture() as fixture:
            fixture.set_state(active=False, sub="dead")
            self.assert_failed_without_completion(fixture.run())
            self.assertTrue(fixture.service_bin.is_symlink())
            self.assertEqual(os.readlink(fixture.service_bin), str(fixture.prior))
            self.assertFalse(fixture.reload_state()["active"])
            self.assertNotIn("cargo build ", fixture.calls())
        with self.subTest(prestate="regular-service-file"), RebuildFixture() as fixture:
            fixture.service_bin.unlink()
            fixture.service_bin.write_text("unsupported\n")
            original = fixture.service_bin.read_bytes()
            self.assert_failed_without_completion(fixture.run())
            self.assertFalse(fixture.service_bin.is_symlink())
            self.assertEqual(fixture.service_bin.read_bytes(), original)
            self.assertNotIn("cargo build ", fixture.calls())

    def test_restart_failure_and_health_failure_restore_prior_runtime(self) -> None:
        for updates in ({"restart_failures": 1}, {"health_failures": 1}):
            with self.subTest(updates=updates), RebuildFixture() as fixture:
                fixture.set_state(**updates)
                self.assert_failed_without_completion(fixture.run())
                fixture.assert_prior_restored(self)
                restart_calls = fixture.reload_state()["restart_calls"]
                if not isinstance(restart_calls, int):
                    self.fail("fixture restart count is not an integer")
                self.assertGreaterEqual(restart_calls, 1)
                fixture.assert_no_backend_lifecycle(self)

    def test_every_receipt_failure_rolls_back_and_emits_no_completion(self) -> None:
        for stage in (
            "create",
            "write",
            "fsync",
            "rename",
            "dir-fsync",
            "enospc",
        ):
            with self.subTest(stage=stage), RebuildFixture() as fixture:
                result = fixture.run(
                    extra_env={"LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE": stage}
                )
                self.assert_failed_without_completion(result)
                fixture.assert_prior_restored(self)
                self.assertEqual(fixture.receipt_paths(), [])
                fixture.assert_transaction_clean(self)
                fixture.assert_no_backend_lifecycle(self)

    def test_source_mutation_and_restore_during_cargo_is_rejected_pre_cutover(
        self,
    ) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(mutate_source_during_cargo=True)
            output = self.assert_failed_without_completion(fixture.run())
            self.assertIn(
                "directory authority ledger changed: canonical source", output
            )
            fixture.assert_prior_restored(self)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)
            self.assertFalse(fixture.source_dir.exists())

    def test_generation_and_proc_drift_after_attestation_roll_back(self) -> None:
        for kind in ("pid", "invocation", "starttime", "pid-reuse"):
            with self.subTest(kind=kind), RebuildFixture() as fixture:
                fixture.set_state(drift_after="candidate-attestation", drift_kind=kind)
                output = self.assert_failed_without_completion(fixture.run())
                self.assertIn("generation changed", output)
                self.assertTrue(fixture.reload_state()["drifted"])
                fixture.assert_call_order(
                    self,
                    "systemctl --user restart --no-block --job-mode=fail -- llm-guard-proxy.service",
                    "curl -fsS ",
                    "systemctl --user show ",
                    "systemctl --user show ",
                )
                fixture.assert_prior_restored(self)
                fixture.assert_no_backend_lifecycle(self)

    def test_generation_drift_at_durable_publication_removes_receipt(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(
                drift_after="first-publication-bracket", drift_kind="invocation"
            )
            output = self.assert_failed_without_completion(fixture.run())
            self.assertIn("generation changed", output)
            self.assertTrue(fixture.reload_state()["drifted"])
            fixture.assert_call_order(
                self,
                "curl -fsS ",
                "systemctl --user show ",
                "cargo --version --verbose",
                "systemctl --user list-jobs --output=json",
                "cargo --version --verbose",
                "systemctl --user show ",
            )
            self.assertEqual(fixture.receipt_paths(), [])
            fixture.assert_prior_restored(self)
            fixture.assert_no_backend_lifecycle(self)

    def test_committed_drift_blocks_completion_without_rollback(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(
                drift_after="second-publication-bracket", drift_kind="invocation"
            )
            output = self.assert_failed_without_completion(fixture.run())
            self.assertIn("committed candidate generation cannot be proven", output)
            self.assertEqual(os.readlink(fixture.service_bin), str(fixture.candidate))
            state = fixture.reload_state()
            self.assertTrue(state["drifted"])
            self.assertEqual(state["restart_calls"], 1)
            receipts = fixture.receipt_paths()
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())["phase"], "committed")
            fixture.assert_transaction_clean(self)
            fixture.assert_no_backend_lifecycle(self)

    def test_executable_replacement_during_held_fd_hash_rolls_back(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_REPLACE_EXE_DURING_HASH": str(
                        fixture.same_hash_other_inode
                    )
                }
            )
            output = self.assert_failed_without_completion(result)
            self.assertIn("current proc executable no longer names held inode", output)
            fixture.assert_prior_restored(self)
            fixture.assert_no_backend_lifecycle(self)

    def test_health_is_bound_to_the_attested_generation(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(health_generation_drift=True)
            output = self.assert_failed_without_completion(fixture.run())
            self.assertIn("changed during health check", output)
            fixture.assert_prior_restored(self)
            fixture.assert_no_backend_lifecycle(self)

    def test_prior_absence_is_restored_on_receipt_failure_without_runtime_restart(
        self,
    ) -> None:
        with RebuildFixture() as fixture:
            fixture.set_prior_absent_with_candidate_runtime()
            result = fixture.run(
                extra_env={"LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE": "write"}
            )
            self.assert_failed_without_completion(result)
            self.assertFalse(fixture.service_bin.exists())
            self.assertFalse(fixture.service_bin.is_symlink())
            state = fixture.reload_state()
            self.assertEqual(state["running_target"], str(fixture.candidate))
            self.assertEqual(state["restart_calls"], 0)
            fixture.assert_no_backend_lifecycle(self)


if __name__ == "__main__":
    unittest.main()
