from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "gb10_deploy_memory_guardian.sh"


class DeploymentTransactionTests(unittest.TestCase):
    ARTIFACTS = (
        ("config/gb10-memory-guardian/config.toml", ".config/gb10-memory-guardian/config.toml"),
        ("target/release/gb10-memory-guardian", ".local/bin/gb10-memory-guardian"),
        (
            "scripts/gb10_enforce_docker_cgroup_limits.sh",
            ".local/bin/gb10_enforce_docker_cgroup_limits.sh",
        ),
        (
            "scripts/gb10_memory_guardian_canary.sh",
            ".local/bin/gb10_memory_guardian_canary.sh",
        ),
        ("systemd/gb10-memory-guardian.service", ".config/systemd/user/gb10-memory-guardian.service"),
        (
            "systemd/gb10-memory-guardian-canary.service",
            ".config/systemd/user/gb10-memory-guardian-canary.service",
        ),
        ("systemd/vllm-aeon-27b-dflash.service", ".config/systemd/user/vllm-aeon-27b-dflash.service"),
        ("systemd/querit-4b-reranker.service", ".config/systemd/user/querit-4b-reranker.service"),
        (
            "systemd/vllm-qwen3-reranker-8b.service",
            ".config/systemd/user/vllm-qwen3-reranker-8b.service",
        ),
    )

    def _fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, dict[str, str], dict[Path, bytes | None]]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        home = root / "home"
        source = root / "source"
        runtime = root / "runtime"
        state = root / "state"
        bin_dir = root / "bin"
        for directory in (home, source, runtime, state, bin_dir):
            directory.mkdir(parents=True)

        sources = {
            "config/gb10-memory-guardian/config.toml": (
                'schema_version = 1\n[target]\nlabel = "aeon-text"\n'
                'registration_file = "text-cgroup.v1"\n'
            ),
            "target/release/gb10-memory-guardian": "#!/usr/bin/env bash\nexit 0\n",
            "scripts/gb10_enforce_docker_cgroup_limits.sh": "#!/usr/bin/env bash\nexit 0\n",
            "scripts/gb10_memory_guardian_canary.sh": (
                "#!/usr/bin/env bash\n"
                "printf 'canary %s\\n' \"$1\" >>\"$COMMAND_LOG\"\n"
            ),
            "systemd/gb10-memory-guardian.service": (
                "Environment=GB10_MEMORY_GUARDIAN_EXPECTED_LABEL=aeon-text\n"
                "Environment=GB10_MEMORY_GUARDIAN_EXPECTED_REGISTRATION_FILE=text-cgroup.v1\n"
            ),
            "systemd/gb10-memory-guardian-canary.service": "[Service]\nType=oneshot\n",
            "systemd/vllm-aeon-27b-dflash.service": (
                "[Unit]\n[Service]\n"
                "Environment=GB10_CGROUP_REGISTRATION_PATH=%t/gb10-memory-guardian/text-cgroup.v1\n"
                "Restart=on-failure\nExecStart=/usr/bin/docker run --cgroup-parent app.slice image\n"
            ),
            "systemd/querit-4b-reranker.service": "[Unit]\n# lifecycle-independent\n[Service]\n",
            "systemd/vllm-qwen3-reranker-8b.service": "[Unit]\n# lifecycle-independent\n[Service]\n",
        }
        for relative, contents in sources.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
            if relative.startswith(("scripts/", "target/")):
                path.chmod(0o755)

        commands = root / "commands.log"
        systemctl = bin_dir / "systemctl"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
            "case \"$*\" in\n"
            "  '--user is-enabled gb10-memory-guardian.service')\n"
            "    printf 'disabled\\n'; exit 1 ;;\n"
            "  '--user show gb10-memory-guardian.service --property=LoadState --property=ActiveState --property=SubState')\n"
            "    printf 'LoadState=loaded\\nActiveState=inactive\\nSubState=dead\\n' ;;\n"
            "esac\n"
        )
        systemctl.chmod(0o755)
        baseline: dict[Path, bytes | None] = {}
        for index, (_, destination) in enumerate(self.ARTIFACTS):
            path = home / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            if index % 2 == 0:
                old = f"old-artifact-{index}\n".encode()
                path.write_bytes(old)
                path.chmod(0o640)
                baseline[path] = old
            else:
                baseline[path] = None

        registration = runtime / "gb10-memory-guardian" / "text-cgroup.v1"
        registration.parent.mkdir()
        registration.write_text("present\n")
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_STATE_HOME": str(state),
                "XDG_RUNTIME_DIR": str(runtime),
                "GB10_BENCHMARK_EXCLUDED": "YES",
                "GB10_MEMORY_GUARDIAN_DEPLOY_SOURCE_ROOT": str(source),
                "GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN": str(systemctl),
                "COMMAND_LOG": str(commands),
            }
        )
        return temporary, commands, env, baseline

    def _run(self, env: dict[str, str], mode: str, fail_at: str | None = None) -> subprocess.CompletedProcess[str]:
        run_env = env.copy()
        if fail_at is not None:
            run_env["GB10_MEMORY_GUARDIAN_DEPLOY_FAIL_AT"] = fail_at
        return subprocess.run(
            ["bash", str(DEPLOY), mode],
            env=run_env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def _assert_baseline(self, baseline: dict[Path, bytes | None]) -> None:
        for path, expected in baseline.items():
            if expected is None:
                self.assertFalse(path.exists(), path)
            else:
                self.assertEqual(path.read_bytes(), expected, path)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640, path)

    def test_every_install_boundary_restores_prior_bytes_and_absence(self) -> None:
        boundaries = (
            "install-config",
            "install-binary",
            "install-helper",
            "install-canary",
            "install-guardian-unit",
            "install-canary-unit",
            "install-text-unit",
            "install-querit-unit",
            "install-vllm-reranker-unit",
            "install-daemon-reload",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                temporary, commands, env, baseline = self._fixture()
                try:
                    result = self._run(env, "install", boundary)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self._assert_baseline(baseline)
                    self.assertIn("daemon-reload", commands.read_text())
                finally:
                    temporary.cleanup()

    def test_failed_activation_restores_generation_from_before_install(self) -> None:
        for boundary in ("activate-disposable", "activate-enable", "activate-configured"):
            with self.subTest(boundary=boundary):
                temporary, commands, env, baseline = self._fixture()
                try:
                    installed = self._run(env, "install")
                    self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                    failed = self._run(env, "activate", boundary)
                    self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
                    self._assert_baseline(baseline)
                    self.assertGreaterEqual(commands.read_text().count("daemon-reload"), 2)
                finally:
                    temporary.cleanup()

    def test_activate_rejects_source_or_installed_drift_and_restores(self) -> None:
        for drift in ("source-bytes", "installed-bytes", "installed-mode"):
            with self.subTest(drift=drift):
                temporary, _, env, baseline = self._fixture()
                try:
                    installed = self._run(env, "install")
                    self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                    source = Path(env["GB10_MEMORY_GUARDIAN_DEPLOY_SOURCE_ROOT"])
                    deployed = Path(env["HOME"]) / ".local/bin/gb10-memory-guardian"
                    if drift == "source-bytes":
                        (source / "target/release/gb10-memory-guardian").write_text("changed\n")
                    elif drift == "installed-bytes":
                        deployed.write_text("changed\n")
                    else:
                        deployed.chmod(0o777)
                    activated = self._run(env, "activate")
                    self.assertNotEqual(activated.returncode, 0, activated.stdout + activated.stderr)
                    self._assert_baseline(baseline)
                finally:
                    temporary.cleanup()

    def test_source_preflight_failure_recovers_pending_install(self) -> None:
        temporary, _, env, baseline = self._fixture()
        try:
            installed = self._run(env, "install")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            source = Path(env["GB10_MEMORY_GUARDIAN_DEPLOY_SOURCE_ROOT"])
            (source / "config/gb10-memory-guardian/config.toml").unlink()
            activated = self._run(env, "activate")
            self.assertNotEqual(activated.returncode, 0, activated.stdout + activated.stderr)
            self._assert_baseline(baseline)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            self.assertFalse(transaction.exists())
        finally:
            temporary.cleanup()

    def test_term_during_install_recovers_prepared_transaction(self) -> None:
        temporary, _, env, baseline = self._fixture()
        process: subprocess.Popen[str] | None = None
        try:
            root = Path(env["HOME"]).parent
            marker = root / "signal-marker"
            systemctl = Path(env["GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN"])
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
                "if [[ \"$*\" == *'disable --now'* && ! -e \"$SIGNAL_MARKER\" ]]; then\n"
                "  : >\"$SIGNAL_MARKER\"\n"
                "  sleep 2\n"
                "fi\n"
            )
            systemctl.chmod(0o755)
            run_env = env.copy()
            run_env["SIGNAL_MARKER"] = str(marker)
            run_env["GB10_MEMORY_GUARDIAN_DEPLOY_TIMEOUT_SECONDS"] = "30"
            process = subprocess.Popen(
                ["bash", str(DEPLOY), "install"],
                env=run_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "install never reached the signal barrier")
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            self._assert_baseline(baseline)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            self.assertFalse(transaction.exists())
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
            temporary.cleanup()

    def test_kill_recovery_restores_stale_prepared_transaction(self) -> None:
        temporary, commands, env, baseline = self._fixture()
        process: subprocess.Popen[str] | None = None
        try:
            root = Path(env["HOME"]).parent
            marker = root / "kill-marker"
            systemctl = Path(env["GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN"])
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
                "if [[ \"$*\" == *'disable --now'* ]]; then\n"
                "  : >\"$KILL_MARKER\"\n"
                "  sleep 2\n"
                "fi\n"
            )
            systemctl.chmod(0o755)
            run_env = env.copy()
            run_env["KILL_MARKER"] = str(marker)
            run_env["GB10_MEMORY_GUARDIAN_DEPLOY_TIMEOUT_SECONDS"] = "30"
            process = subprocess.Popen(
                ["bash", str(DEPLOY), "install"],
                env=run_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "install never reached the kill barrier")
            process.kill()
            process.communicate(timeout=5)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            self.assertEqual((transaction / "phase").read_text(), "prepared\n")

            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
            )
            systemctl.chmod(0o755)
            recovered = self._run(env, "activate")
            self.assertNotEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self._assert_baseline(baseline)
            self.assertFalse(transaction.exists())
            self.assertIn("daemon-reload", commands.read_text())
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
            temporary.cleanup()

    def test_pending_transaction_is_private_complete_and_phase_bound(self) -> None:
        temporary, _, env, _ = self._fixture()
        try:
            installed = self._run(env, "install")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            self.assertTrue(transaction.is_dir())
            self.assertEqual(transaction.stat().st_mode & 0o777, 0o700)
            self.assertEqual((transaction / "phase").read_text(), "installed\n")
            self.assertEqual((transaction / "manifest.json").stat().st_mode & 0o777, 0o600)
            manifest = (transaction / "manifest.json").read_text()
            for contract in ("source_sha256", "installed_sha256", "prior", "parents"):
                self.assertIn(contract, manifest)
        finally:
            temporary.cleanup()

    def test_corrupt_pending_manifest_fails_closed_and_is_not_activatable(self) -> None:
        temporary, commands, env, _ = self._fixture()
        try:
            installed = self._run(env, "install")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            manifest = transaction / "manifest.json"
            manifest.write_text(manifest.read_text() + "{}\n")
            commands.write_text("")
            activated = self._run(env, "activate")
            self.assertNotEqual(activated.returncode, 0, activated.stdout + activated.stderr)
            self.assertNotIn("activation passed", activated.stdout)
            self.assertTrue(transaction.exists(), "corrupt private recovery state must be retained")
            self.assertEqual((transaction / "phase").read_text(), "rollback_failed\n")
            self.assertNotIn("disable --now", commands.read_text())
        finally:
            temporary.cleanup()

    def test_rollback_restores_parent_modes_and_explicit_absence(self) -> None:
        temporary, _, env, baseline = self._fixture()
        try:
            home = Path(env["HOME"])
            config = home / ".config/gb10-memory-guardian/config.toml"
            config.unlink()
            baseline[config] = None
            config.parent.rmdir()
            unit_parent = home / ".config/systemd/user"
            unit_parent.chmod(0o711)
            result = self._run(env, "install", "install-daemon-reload")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self._assert_baseline(baseline)
            self.assertFalse(config.parent.exists())
            self.assertEqual(unit_parent.stat().st_mode & 0o777, 0o711)
        finally:
            temporary.cleanup()

    def test_activation_starts_unenabled_then_verifies_then_enables(self) -> None:
        temporary, commands, env, _ = self._fixture()
        try:
            installed = self._run(env, "install")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            commands.write_text("")
            activated = self._run(env, "activate")
            self.assertEqual(activated.returncode, 0, activated.stdout + activated.stderr)
            events = commands.read_text().splitlines()
            disposable = events.index("canary disposable")
            started = events.index("systemctl --user start gb10-memory-guardian.service")
            configured = events.index("canary configured-target")
            enabled = events.index("systemctl --user enable gb10-memory-guardian.service")
            self.assertLess(disposable, started)
            self.assertLess(started, configured)
            self.assertLess(configured, enabled)
            self.assertFalse(any("enable --now" in event for event in events))
        finally:
            temporary.cleanup()

    def test_rollback_failed_is_never_activatable_and_recovery_is_idempotent(self) -> None:
        temporary, _, env, baseline = self._fixture()
        try:
            installed = self._run(env, "install")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            failed = self._run(
                env,
                "activate",
                "activate-disposable,rollback-artifact-0",
            )
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            self.assertEqual((transaction / "phase").read_text(), "rollback_failed\n")

            recovered = self._run(env, "activate")
            self.assertNotEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertNotIn("activation passed", recovered.stdout)
            self._assert_baseline(baseline)
            self.assertFalse(transaction.exists())
        finally:
            temporary.cleanup()

    def test_systemctl_mutations_target_only_guardian(self) -> None:
        temporary, commands, env, _ = self._fixture()
        try:
            self.assertEqual(self._run(env, "install").returncode, 0)
            self.assertEqual(self._run(env, "activate").returncode, 0)
            mutating = (" start ", " stop ", " enable ", " disable ", " restart ")
            for line in commands.read_text().splitlines():
                if line.startswith("systemctl") and any(token in f" {line} " for token in mutating):
                    self.assertIn("gb10-memory-guardian.service", line)
                    for forbidden in ("vllm-aeon", "embedding", "reranker", "llm-guard-proxy"):
                        self.assertNotIn(forbidden, line)
        finally:
            temporary.cleanup()

    def test_lock_contender_cannot_touch_owners_transaction(self) -> None:
        temporary, commands, env, _ = self._fixture()
        owner: subprocess.Popen[str] | None = None
        try:
            root = Path(env["HOME"]).parent
            marker = root / "owner-marker"
            release = root / "owner-release"
            systemctl = Path(env["GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN"])
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
                "if [[ \"$*\" == *'disable --now'* && ! -e \"$OWNER_MARKER\" ]]; then\n"
                "  : >\"$OWNER_MARKER\"\n"
                "  while [[ ! -e \"$OWNER_RELEASE\" ]]; do sleep 0.02; done\n"
                "fi\n"
                "case \"$*\" in\n"
                "  '--user is-enabled gb10-memory-guardian.service') printf 'disabled\\n'; exit 1 ;;\n"
                "  '--user show gb10-memory-guardian.service --property=LoadState --property=ActiveState --property=SubState')\n"
                "    printf 'LoadState=loaded\\nActiveState=inactive\\nSubState=dead\\n' ;;\n"
                "esac\n"
            )
            systemctl.chmod(0o755)
            run_env = env.copy()
            run_env.update({"OWNER_MARKER": str(marker), "OWNER_RELEASE": str(release)})
            owner = subprocess.Popen(
                ["bash", str(DEPLOY), "install"],
                env=run_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "owner never acquired its transaction lock")
            contender = self._run(run_env, "install")
            self.assertNotEqual(contender.returncode, 0, contender.stdout + contender.stderr)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            self.assertEqual((transaction / "phase").read_text(), "prepared\n")
            self.assertEqual(commands.read_text().count("disable --now"), 1)
            release.touch()
            stdout, stderr = owner.communicate(timeout=15)
            self.assertEqual(owner.returncode, 0, stdout + stderr)
            self.assertEqual((transaction / "phase").read_text(), "installed\n")
        finally:
            if owner is not None and owner.poll() is None:
                owner.kill()
                owner.communicate(timeout=5)
            temporary.cleanup()

    def test_kill_during_rollback_resumes_nonactivatable_phase(self) -> None:
        temporary, _, env, baseline = self._fixture()
        process: subprocess.Popen[str] | None = None
        try:
            self.assertEqual(self._run(env, "install").returncode, 0)
            root = Path(env["HOME"]).parent
            marker = root / "rollback-marker"
            systemctl = Path(env["GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN"])
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
                "case \"$*\" in\n"
                "  '--user is-enabled gb10-memory-guardian.service') printf 'disabled\\n'; exit 1 ;;\n"
                "  '--user show gb10-memory-guardian.service --property=LoadState --property=ActiveState --property=SubState')\n"
                "    printf 'LoadState=loaded\\nActiveState=inactive\\nSubState=dead\\n' ;;\n"
                "  *'disable --now'*) : >\"$ROLLBACK_MARKER\"; sleep 2 ;;\n"
                "esac\n"
            )
            systemctl.chmod(0o755)
            run_env = env.copy()
            run_env.update(
                {
                    "ROLLBACK_MARKER": str(marker),
                    "GB10_MEMORY_GUARDIAN_DEPLOY_FAIL_AT": "activate-disposable",
                }
            )
            process = subprocess.Popen(
                ["bash", str(DEPLOY), "activate"],
                env=run_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "rollback never reached its kill barrier")
            process.kill()
            process.communicate(timeout=5)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            self.assertEqual((transaction / "phase").read_text(), "rolling_back\n")
            time.sleep(2.2)

            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
            )
            systemctl.chmod(0o755)
            recovered = self._run(env, "activate")
            self.assertNotEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self._assert_baseline(baseline)
            self.assertFalse(transaction.exists())
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            temporary.cleanup()

    def test_durable_commit_is_authoritative_before_shell_cleanup(self) -> None:
        temporary, commands, env, baseline = self._fixture()
        try:
            self.assertEqual(self._run(env, "install").returncode, 0)
            commands.write_text("")
            result = self._run(env, "activate", "activate-committed")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            transaction = Path(env["XDG_STATE_HOME"]) / "gb10-memory-guardian/deploy-transaction.v2"
            self.assertEqual((transaction / "phase").read_text(), "committed\n")
            self.assertFalse(any("disable --now" in line for line in commands.read_text().splitlines()))
            self.assertTrue(
                any(
                    expected is None and path.exists()
                    or expected is not None and path.read_bytes() != expected
                    for path, expected in baseline.items()
                ),
                "durably committed installation was incorrectly restored",
            )
            (transaction / "manifest.json").write_text("{}\n")
            commands.write_text("")
            retried = self._run(env, "activate")
            self.assertNotEqual(retried.returncode, 0, retried.stdout + retried.stderr)
            self.assertEqual((transaction / "phase").read_text(), "committed\n")
            self.assertNotIn("disable --now", commands.read_text())
        finally:
            temporary.cleanup()

    def test_activation_rejects_guardian_that_is_still_enabled(self) -> None:
        temporary, commands, env, baseline = self._fixture()
        try:
            self.assertEqual(self._run(env, "install").returncode, 0)
            commands.write_text("")
            systemctl = Path(env["GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN"])
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'systemctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
                "if [[ \"$*\" == *'is-enabled'* ]]; then printf 'enabled\\n'; exit 0; fi\n"
            )
            systemctl.chmod(0o755)
            result = self._run(env, "activate")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("canary", commands.read_text())
            self._assert_baseline(baseline)
        finally:
            temporary.cleanup()


class DeploymentOrderingContractTests(unittest.TestCase):
    def test_deployment_installs_complete_reviewed_bundle_before_activation(self) -> None:
        deploy = DEPLOY.read_text()
        required_sources = (
            "config/gb10-memory-guardian/config.toml",
            "target/release/gb10-memory-guardian",
            "scripts/gb10_enforce_docker_cgroup_limits.sh",
            "scripts/gb10_memory_guardian_canary.sh",
            "systemd/gb10-memory-guardian.service",
            "systemd/gb10-memory-guardian-canary.service",
            "systemd/vllm-aeon-27b-dflash.service",
            "systemd/querit-4b-reranker.service",
            "systemd/vllm-qwen3-reranker-8b.service",
        )
        activation = deploy.index('run_systemctl enable "$guardian_unit"')
        for source in required_sources:
            self.assertIn(source, deploy)
            self.assertLess(deploy.index(source), activation, source)
        self.assertIn("install_modes=(0600", deploy)
        self.assertNotIn("systemd/*", deploy)
        self.assertIn("configured-target", deploy)
        self.assertIn("trap fail_closed_activation EXIT", deploy)
        self.assertIn('run_systemctl start "$guardian_unit"', deploy)
        self.assertLess(
            deploy.index('run_systemctl start "$guardian_unit"'),
            deploy.index("run_canary configured-target"),
        )
        self.assertLess(
            deploy.index("run_canary configured-target"),
            activation,
        )

    def test_runbook_uses_fail_closed_deployer_not_loose_manual_enable(self) -> None:
        for path in (
            ROOT / "README.md",
            ROOT / "docs" / "deployment" / "AGENTS.md",
        ):
            with self.subTest(path=path.name):
                runbook = path.read_text()
                self.assertIn("gb10_deploy_memory_guardian.sh install", runbook)
                self.assertIn("gb10_deploy_memory_guardian.sh activate", runbook)
                self.assertIn("aeon-text", runbook)
                self.assertIn("text-cgroup.v1", runbook)
                self.assertIn("owner-only", runbook)
                self.assertIn("read-only configured-target identity check", runbook)
                self.assertNotIn("cp systemd/*", runbook)
                self.assertNotIn(
                    "systemctl --user enable --now gb10-memory-guardian.service", runbook
                )
                transaction = runbook.split("gb10_deploy_memory_guardian.sh install", 1)[1].split(
                    "gb10_deploy_memory_guardian.sh activate", 1
                )[0]
                for forbidden in (
                    "systemctl --user start",
                    "systemctl --user stop",
                    "systemctl --user restart",
                    "rm -f",
                    "daemon-reload",
                    "gb10_memory_guardian_canary.sh",
                ):
                    self.assertNotIn(forbidden, transaction)

    def test_deployer_never_swallows_failure_to_stop_stale_guardian(self) -> None:
        source = DEPLOY.read_text()
        for line in source.splitlines():
            if 'disable --now "$guardian_unit"' in line:
                self.assertNotIn("|| true", line)


if __name__ == "__main__":
    unittest.main()
