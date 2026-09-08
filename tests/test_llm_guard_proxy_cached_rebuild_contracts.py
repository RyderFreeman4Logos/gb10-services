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
BOUNDED_PROCESS = ROOT / "scripts" / "gb10_bounded_process.py"
SCOPED_WORKER = ROOT / "scripts" / "llm_guard_proxy_scoped_worker.py"
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
        engine = REBUILD_ENGINE.read_text()
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
        helper_match = re.search(
            r'EXPECTED_BOUNDED_PROCESS_SHA256\s*=\s*\(\s*"([0-9a-f]{64})"\s*\)',
            engine,
        )
        worker_match = re.search(
            r'"scoped_worker": ToolSpec\(.*?\n\s*"([0-9a-f]{64})",\n\s*\)',
            engine,
            re.DOTALL,
        )
        self.assertIsNotNone(helper_match, "bounded helper authority is missing")
        self.assertIsNotNone(worker_match, "scoped worker authority is missing")
        assert helper_match is not None and worker_match is not None
        self.assertEqual(
            hashlib.sha256(BOUNDED_PROCESS.read_bytes()).hexdigest(),
            helper_match.group(1),
        )
        self.assertEqual(
            hashlib.sha256(SCOPED_WORKER.read_bytes()).hexdigest(),
            worker_match.group(1),
        )
        self.assertIn("class RebuildError", engine)

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
    def _write_stub_launcher(root: Path, marker: Path) -> Path:
        engine = root / REBUILD_ENGINE.name
        engine.write_text(
            "import json\n"
            "import os\n"
            "import sys\n"
            f"with open({str(marker)!r}, \"w\") as stream:\n"
            "    json.dump({\"argv\": sys.argv[1:], \"env\": dict(os.environ)}, stream)\n"
        )
        engine.chmod(0o644)
        engine_sha256 = hashlib.sha256(engine.read_bytes()).hexdigest()
        launcher = root / REBUILD_SCRIPT.name
        launcher.write_text(
            re.sub(
                r'^expected_engine_sha256=\"[0-9a-f]{64}\"$',
                f'expected_engine_sha256=\"{engine_sha256}\"',
                REBUILD_SCRIPT.read_text(),
                flags=re.MULTILINE,
            )
        )
        launcher.chmod(0o755)
        return launcher

    def test_direct_production_entry_reaches_engine_with_fixed_environment(self) -> None:
        ambient_environments = (
            {},
            {
                "HOME": "/caller-home",
                "PATH": "/caller-path",
                "LC_ALL": "en_US.UTF-8",
                "LANG": "fr_FR.UTF-8",
            },
        )
        for ambient in ambient_environments:
            with self.subTest(ambient=ambient), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                marker = root / "engine-environment.json"
                launcher = self._write_stub_launcher(root, marker)
                result = subprocess.run(
                    [str(launcher)],
                    cwd=root,
                    env=ambient,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, self.output(result))
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
                observed = json.loads(marker.read_text())
                self.assertEqual(observed["argv"], [])
                self.assertEqual(
                    {
                        name: observed["env"][name]
                        for name in ("HOME", "PATH", "LC_ALL", "LANG")
                    },
                    {
                        "HOME": "/home/obj",
                        "PATH": "/usr/bin:/bin",
                        "LC_ALL": "C",
                        "LANG": "C",
                    },
                )

    def test_production_rejects_source_cache_service_and_test_authority_overrides(
        self,
    ) -> None:
        authorities = (
            "SOURCE_REPO",
            "SOURCE_BRANCH",
            "SOURCE_DIR",
            "CACHE_ROOT",
            "SERVICE_BIN",
            "LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG",
            "LLM_GUARD_PROXY_REBUILD_GUARD_UNIT",
            "LLM_GUARD_PROXY_REBUILD_PROC_ROOT",
            "LLM_GUARD_PROXY_REBUILD_RECEIPT_DIR",
            "LLM_GUARD_REBUILD_TEST_CONFIG",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "engine-ran"
            launcher = self._write_stub_launcher(root, marker)
            for name in authorities:
                with self.subTest(name=name):
                    marker.unlink(missing_ok=True)
                    result = subprocess.run(
                        [str(launcher)],
                        cwd=root,
                        env={
                            "HOME": "/caller-home",
                            "PATH": "/caller-path",
                            "LC_ALL": "en_US.UTF-8",
                            "LANG": "fr_FR.UTF-8",
                            name: "/caller-controlled-authority",
                        },
                        text=True,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 64, self.output(result))
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(
                        result.stderr,
                        f"production rebuild override {name} requires --test-only\n",
                    )
                    self.assertFalse(marker.exists())

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
                    "ca_cert",
                    "cargo",
                    "cc",
                    "curl",
                    "git",
                    "git_remote_https",
                    "ionice",
                    "ld",
                    "hosts",
                    "nice",
                    "nsswitch",
                    "prlimit",
                    "python",
                    "readelf",
                    "resolv_conf",
                    "rustc",
                    "scoped_worker",
                    "systemd_run",
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
            self.assertIn("--size 6442450944 --tmpfs /target", metadata_line)
            self.assertIn("--size 6442450944 --tmpfs /target", build_line)
            self.assertNotIn("--bind", build_line)
            self.assertIn("--chdir /", build_line)
            self.assertRegex(
                build_line,
                r"--ro-bind /proc/self/fd/[0-9]+ /toolchain/bin/cargo",
            )
            self.assertRegex(
                build_line,
                r"--ro-bind /proc/self/fd/[0-9]+ /toolchain/bin/rustc",
            )
            self.assertIn("/worker.py build", build_line)
            scope_lines = [
                line for line in calls.splitlines() if line.startswith("systemd_run ")
            ]
            self.assertEqual(len(scope_lines), 3)
            for line in scope_lines:
                self.assertIn("--scope", line)
                self.assertIn("--property=MemorySwapMax=0", line)
                self.assertIn("--property=KillMode=control-group", line)
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

    def test_proc_mount_follows_pid_isolation_and_preserves_network_policy(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commands = {
                operation: shlex.split(
                    next(
                        line
                        for line in fixture.calls().splitlines()
                        if line.startswith("bwrap ")
                        and f" {operation} " in f" {line} "
                    )
                )
                for operation in ("fetch", "metadata", "build")
            }
            for operation, arguments in commands.items():
                self.assertIn("--unshare-user", arguments)
                self.assertIn("--unshare-all", arguments)
                proc = arguments.index("--proc")
                self.assertLess(arguments.index("--unshare-all"), proc)
                self.assertEqual(arguments[proc + 1], "/proc")
                for weaker in (
                    "--unshare-cgroup",
                    "--unshare-ipc",
                    "--unshare-net",
                    "--unshare-pid",
                    "--unshare-uts",
                ):
                    self.assertNotIn(weaker, arguments)
                self.assertEqual("--share-net" in arguments, operation == "fetch")


class GuardRebuildFixtureScopeAuthorityTests(unittest.TestCase):
    @staticmethod
    def _write_scope(fixture: RebuildFixture, unit: str, worker_pid: int) -> Path:
        scope = fixture.cgroup_root / "fixture.slice" / unit
        scope.mkdir(parents=True)
        (scope / "cgroup.procs").write_text(f"{worker_pid}\n")
        (scope / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        return scope

    def _register_scope(
        self, fixture: RebuildFixture, unit: str, worker_pid: int
    ) -> Path:
        scope = self._write_scope(fixture, unit, worker_pid)
        proc = fixture.proc_root / str(worker_pid)
        proc.mkdir(parents=True)
        (proc / "cgroup").write_text(f"0::/fixture.slice/{unit}\n")
        fixture.set_state(
            scope_unit=unit,
            scope_worker=worker_pid,
            scope_registration={
                "cgroup_path": str(scope),
                "unit": unit,
                "worker_pid": worker_pid,
            },
        )
        return scope

    @staticmethod
    def _scope_bytes(scope: Path) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in scope.iterdir()}

    @staticmethod
    def _systemctl(
        fixture: RebuildFixture, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(fixture.fake_bin / "systemctl"), *arguments],
            cwd=fixture.root,
            env=fixture.env,
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )

    def test_fake_systemctl_rejects_malformed_or_unregistered_scope_kills(self) -> None:
        cases = ("foreign", "signal", "kill", "argv")
        for case in cases:
            with self.subTest(case=case), RebuildFixture() as fixture:
                worker = subprocess.Popen(["/usr/bin/sleep", "30"])
                try:
                    owned = "llm-guard-rebuild-build-" + "a" * 32 + ".scope"
                    owned_scope = self._register_scope(fixture, owned, worker.pid)
                    foreign = "llm-guard-rebuild-build-" + "b" * 32 + ".scope"
                    foreign_scope = self._write_scope(fixture, foreign, 999999)
                    before = {
                        owned: self._scope_bytes(owned_scope),
                        foreign: self._scope_bytes(foreign_scope),
                    }
                    if case == "foreign":
                        arguments = (
                            "--user",
                            "kill",
                            "--kill-whom=all",
                            "--signal=SIGTERM",
                            foreign,
                        )
                    elif case == "signal":
                        arguments = (
                            "--user",
                            "kill",
                            "--kill-whom=all",
                            "--signal=SIGBOGUS",
                            owned,
                        )
                    elif case == "kill":
                        arguments = (
                            "--user",
                            "kill",
                            "--kill-whom=all",
                            "--signal=SIGKILL",
                            owned,
                        )
                    else:
                        arguments = (
                            "--user",
                            "kill",
                            "--kill-whom=all",
                            "--signal=SIGTERM",
                            "--",
                            owned,
                        )
                    result = self._systemctl(fixture, *arguments)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIsNone(worker.poll())
                    self.assertEqual(self._scope_bytes(owned_scope), before[owned])
                    self.assertEqual(self._scope_bytes(foreign_scope), before[foreign])
                finally:
                    if worker.poll() is None:
                        worker.kill()
                    worker.wait(timeout=2)

    def test_fake_systemctl_signals_only_the_exact_registered_owned_scope(self) -> None:
        with RebuildFixture() as fixture:
            worker = subprocess.Popen(["/usr/bin/sleep", "30"])
            unit = "llm-guard-rebuild-build-" + "a" * 32 + ".scope"
            scope = self._register_scope(fixture, unit, worker.pid)
            try:
                result = self._systemctl(
                    fixture,
                    "--user",
                    "kill",
                    "--kill-whom=all",
                    "--signal=SIGTERM",
                    unit,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(worker.wait(timeout=2), -15)
                self.assertFalse(scope.exists())
                state = fixture.reload_state()
                self.assertEqual(state["scope_registration"], {})
                self.assertEqual(state["scope_unit"], "")
                self.assertEqual(state["scope_worker"], 0)
            finally:
                if worker.poll() is None:
                    worker.kill()
                worker.wait(timeout=2)


class GuardRebuildHardContainmentContractTests(unittest.TestCase):
    def assert_contained_failure(
        self, fixture: RebuildFixture, *, payload_released: bool = False
    ) -> str:
        result = fixture.run()
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertNotIn(PRODUCTION_COMPLETE, output)
        self.assertNotIn(TEST_COMPLETE, output)
        if payload_released:
            self.assertIn("payload ", fixture.calls())
        else:
            self.assertNotIn("payload ", fixture.calls())
        fixture.assert_prior_restored(self)
        fixture.assert_no_scopes_or_scratch(self)
        return output

    def test_rebuild_payload_has_one_fail_closed_hard_containment_boundary(self) -> None:
        engine = REBUILD_ENGINE.read_text()
        bounded = BOUNDED_PROCESS.read_text()
        self.assertTrue(SCOPED_WORKER.is_file())
        for required in (
            "def scoped_command(",
            "--scope",
            "MemoryHigh",
            "MemoryMax",
            "MemorySwapMax",
            "TasksMax",
            "CPUQuota",
            "--json-status-fd",
            "--block-fd",
            "cgroup.procs",
            "memory.events",
            "pids.events",
        ):
            self.assertIn(required, bounded)
        for required in (
            "FETCH_TMPFS_BYTES",
            "BUILD_TARGET_TMPFS_BYTES",
            "HOST_WRITE_BUDGET_BYTES",
            "HOST_FREE_FLOOR_BYTES",
            "GB10ART1",
            "scoped_worker",
        ):
            self.assertIn(required, engine)
        self.assertNotRegex(engine, r'"--bind",\s*target\.exec_path')

    def test_scope_setup_failures_never_release_payload(self) -> None:
        for mode in (
            "manager",
            "property",
            "status",
            "worker-wrapper",
            "cgroup",
            "controller",
            "property-readback",
        ):
            with self.subTest(mode=mode), RebuildFixture() as fixture:
                fixture.set_state(scope_failure=mode)
                self.assert_contained_failure(fixture)
        for mode, diagnostic in (
            ("array", "scope status"),
            ("duplicate-key", "scope status"),
            ("duplicate-record", "scope status"),
            ("unknown-member", "scope status"),
            ("unknown-record", "scope status"),
            ("boolean", "scope status"),
            ("zero", "scope status"),
            ("negative", "scope status"),
            ("oversized", "scope status"),
            ("trailing-bytes", "scope status"),
            ("exit-array", "scope status"),
            ("exit-duplicate-key", "scope status"),
            ("exit-unknown-member", "scope exit status"),
            ("exit-boolean", "scope exit status"),
        ):
            with self.subTest(status_mode=mode), RebuildFixture() as fixture:
                fixture.set_state(scope_status_mode=mode)
                output = self.assert_contained_failure(
                    fixture, payload_released=mode.startswith("exit-")
                )
                self.assertIn(diagnostic, output)
                self.assertEqual(fixture.receipt_paths(), [])

    def test_frames_and_resource_limit_events_fail_without_artifacts(self) -> None:
        cases = (
            ("scope_frame_mode", "partial"),
            ("scope_frame_mode", "trailing"),
            ("scope_frame_mode", "oversized"),
            ("scope_frame_mode", "malformed"),
            ("scope_resource_event", "memory"),
            ("scope_resource_event", "pids"),
            ("scope_failure", "payload"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value), RebuildFixture() as fixture:
                fixture.set_state(**{key: value})
                output = self.assert_contained_failure(fixture, payload_released=True)
                if key == "scope_resource_event":
                    self.assertIn("scope resource limit was reached", output)
                self.assertEqual(fixture.receipt_paths(), [])

    def test_immediate_scope_collection_still_requires_worker_exit(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(scope_worker_live_after_collect=True)
            self.assert_contained_failure(fixture, payload_released=True)

    def test_memory_and_free_space_admission_happen_before_payload(self) -> None:
        with RebuildFixture() as fixture:
            (fixture.proc_root / "meminfo").write_text(
                "MemTotal: 134217728 kB\nMemAvailable: 1024 kB\n"
            )
            self.assert_contained_failure(fixture)
        with RebuildFixture() as fixture:
            fixture.test_free_bytes = 8 * 1024 * 1024 * 1024 - 1
            self.assert_contained_failure(fixture)

    def test_success_leaves_no_scope_name_in_wal_or_receipt(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(scope_status_mode="canonical")
            result = fixture.run()
            self.assertEqual(
                result.returncode,
                0,
                result.stdout + result.stderr + "\nCALLS\n" + fixture.calls(),
            )
            serialized = fixture.receipt_paths()[0].read_text()
            self.assertNotIn("llm-guard-rebuild-", serialized)
            state = fixture.reload_state()
            self.assertEqual(fixture.calls().count("payload "), 3)
            self.assertEqual(state["scope_registration"], {})
            self.assertEqual(state["scope_unit"], "")
            self.assertEqual(state["scope_worker"], 0)
            fixture.assert_transaction_clean(self)
            fixture.assert_no_scopes_or_scratch(self)


if __name__ == "__main__":
    unittest.main()
