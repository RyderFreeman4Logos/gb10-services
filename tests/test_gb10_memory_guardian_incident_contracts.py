from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "scripts" / "gb10_memory_guardian_canary.sh"
DEPLOY = ROOT / "scripts" / "gb10_deploy_memory_guardian.sh"
TEXT_UNIT = "vllm-aeon-27b-dflash.service"
GUARDIAN_UNIT = "gb10-memory-guardian.service"


class CanaryHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.runtime = self.root / "runtime"
        self.bin = self.root / "bin"
        self.states = self.root / "states"
        self.commands = self.root / "commands.log"
        self.guardian_invocations = self.root / "guardian-invocations.log"
        self.journal = self.root / "journal.log"
        for directory in (self.home, self.runtime, self.bin, self.states):
            directory.mkdir(parents=True)

        self.guardian = self.bin / "gb10-memory-guardian"
        self.guardian.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >>\"$GUARDIAN_INVOCATIONS\"\n"
        )
        self.guardian.chmod(0o755)
        self._write_fake_systemctl()
        self._write_fake_systemd_run()
        self._write_fake_journalctl()
        self._write_config("aeon-text", "text-cgroup.v1")
        self._write_registration("text-cgroup.v1")
        self._write_standard_states()
        self.journal.write_text("gb10-memory-guardian: armed target aeon-text\n")
        stamp = self.runtime / "gb10-memory-guardian" / "disposable-canary.passed"
        stamp.write_text(
            f"binary_sha256={hashlib.sha256(self.guardian.read_bytes()).hexdigest()}\n"
            f"passed_epoch={int(time.time())}\n"
        )
        stamp.chmod(0o600)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_fake_systemctl(self) -> None:
        script = self.bin / "systemctl"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "root = Path(os.environ['STATE_ROOT'])\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as sink:\n"
            "    sink.write('systemctl ' + ' '.join(args) + '\\n')\n"
            "while args and args[0].startswith('--'):\n"
            "    args.pop(0)\n"
            "if not args:\n"
            "    raise SystemExit(2)\n"
            "action = args.pop(0)\n"
            "unit = next((arg for arg in args if arg.endswith('.service')), '')\n"
            "if action == 'show':\n"
            "    if unit == 'gb10-memory-guardian-disposable-canary.service':\n"
            "        suffix = 'after' if (root / 'driver-started').exists() else 'before'\n"
            "        path = root / f'{unit}.{suffix}.show'\n"
            "    else:\n"
            "        path = root / f'{unit}.show'\n"
            "    if not path.exists():\n"
            "        raise SystemExit(4)\n"
            "    sys.stdout.write(path.read_text())\n"
            "elif action == 'is-active':\n"
            "    if unit == 'gb10-memory-guardian-disposable-canary.service':\n"
            "        active = (root / 'canary-created').exists() and not (root / 'driver-started').exists()\n"
            "    else:\n"
            "        active = 'ActiveState=active\\n' in (root / f'{unit}.show').read_text()\n"
            "    print('active' if active else 'inactive')\n"
            "    raise SystemExit(0 if active else 3)\n"
            "elif action == 'start' and unit == 'gb10-memory-guardian-canary.service':\n"
            "    (root / 'driver-started').write_text('1')\n"
            "elif action == 'stop' and unit == 'gb10-memory-guardian-disposable-canary.service':\n"
            "    (root / 'canary-created').unlink(missing_ok=True)\n"
            "elif action in {'reset-failed', 'revert'}:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(5)\n"
        )
        script.chmod(0o755)

    def _write_fake_systemd_run(self) -> None:
        script = self.bin / "systemd-run"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'systemd-run %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
            "printf 1 >\"$STATE_ROOT/canary-created\"\n"
        )
        script.chmod(0o755)

    def _write_fake_journalctl(self) -> None:
        script = self.bin / "journalctl"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'journalctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
            "cat \"$JOURNAL_FIXTURE\"\n"
        )
        script.chmod(0o755)

    def _write_config(self, label: str, registration_file: str) -> None:
        config = self.home / ".config" / "gb10-memory-guardian" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            f'schema_version = 1\n[target]\nlabel = "{label}"\n'
            f'registration_file = "{registration_file}"\n'
        )
        config.chmod(0o600)

    def _write_registration(self, name: str) -> None:
        directory = self.runtime / "gb10-memory-guardian"
        directory.mkdir(parents=True, exist_ok=True)
        registration = directory / name
        registration.write_text(
            "version=1\n"
            f"container_id={'a' * 64}\n"
            f"scope=docker-{'a' * 64}.scope\n"
            f"control_group=/user.slice/user-{os.geteuid()}.slice/"
            f"user@{os.geteuid()}.service/"
            f"app.slice/docker-{'a' * 64}.scope\n"
        )
        registration.chmod(0o600)

    @staticmethod
    def _state(
        *,
        load: str = "loaded",
        active: str,
        sub: str,
        pid: int,
        result: str = "success",
        exit_status: int = 0,
        restarts: int = 0,
        restart: str = "no",
        environment: str = "",
    ) -> str:
        return (
            f"LoadState={load}\n"
            f"ActiveState={active}\n"
            f"SubState={sub}\n"
            f"MainPID={pid}\n"
            f"Result={result}\n"
            "ExecMainCode=0\n"
            f"ExecMainStatus={exit_status}\n"
            f"NRestarts={restarts}\n"
            f"Restart={restart}\n"
            f"Environment={environment}\n"
        )

    def _write_standard_states(self) -> None:
        registration = self.runtime / "gb10-memory-guardian" / "text-cgroup.v1"
        self._write_state(
            TEXT_UNIT,
            self._state(
                active="active",
                sub="running",
                pid=101,
                restarts=2,
                restart="on-failure",
                environment=f"GB10_CGROUP_REGISTRATION_PATH={registration}",
            ),
        )
        self._write_state(
            GUARDIAN_UNIT,
            self._state(
                active="active", sub="running", pid=102, restarts=1, restart="always"
            ),
        )
        protected = {
            "vllm-embedding.service": (201, "active", "running"),
            "querit-4b-reranker.service": (202, "active", "running"),
            "vllm-qwen3-reranker-8b.service": (0, "inactive", "dead"),
            "llm-guard-proxy.service": (203, "active", "running"),
        }
        for unit, (pid, active, sub) in protected.items():
            self._write_state(unit, self._state(active=active, sub=sub, pid=pid))
        self._write_state(
            "gb10-memory-guardian-disposable-canary.service.before",
            self._state(active="active", sub="running", pid=301),
            literal=True,
        )
        self._write_state(
            "gb10-memory-guardian-disposable-canary.service.after",
            self._state(
                active="failed",
                sub="failed",
                pid=0,
                result="signal",
                exit_status=9,
            ).replace("ExecMainCode=0\n", "ExecMainCode=2\n"),
            literal=True,
        )
        self._write_state(
            "gb10-memory-guardian-canary.service",
            self._state(active="inactive", sub="dead", pid=0).replace(
                "ExecMainCode=0\n", "ExecMainCode=1\n"
            ),
        )

    def _write_state(self, unit: str, contents: str, *, literal: bool = False) -> None:
        suffix = unit if literal else f"{unit}.show"
        if literal:
            suffix += ".show"
        (self.states / suffix).write_text(contents)

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "PATH": f"{self.bin}:{env['PATH']}",
                "STATE_ROOT": str(self.states),
                "COMMAND_LOG": str(self.commands),
                "GUARDIAN_INVOCATIONS": str(self.guardian_invocations),
                "JOURNAL_FIXTURE": str(self.journal),
                "GB10_BENCHMARK_EXCLUDED": "YES",
                "GB10_MEMORY_GUARDIAN_BIN": str(self.guardian),
                "GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN": str(self.bin / "systemctl"),
                "GB10_MEMORY_GUARDIAN_SYSTEMD_RUN_BIN": str(self.bin / "systemd-run"),
                "GB10_MEMORY_GUARDIAN_JOURNALCTL_BIN": str(self.bin / "journalctl"),
                "GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT": TEXT_UNIT,
                "GB10_MEMORY_GUARDIAN_JOURNAL_SINCE": "2026-07-14T10:09:15-07:00",
            }
        )
        return env

    def _run(self, mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(CANARY), mode],
            env=self._environment(),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )


class StrictCanaryStateTests(CanaryHarness):
    def test_disposable_canary_rejects_missing_protected_unit(self) -> None:
        self._write_state(
            "vllm-embedding.service",
            self._state(load="not-found", active="inactive", sub="dead", pid=0),
        )
        stamp = self.runtime / "gb10-memory-guardian" / "disposable-canary.passed"
        stamp.unlink()
        result = self._run("disposable")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(stamp.exists())

    def test_configured_target_check_is_read_only_and_proves_text_identity(self) -> None:
        result = self._run("configured-target")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("read-only", result.stdout)
        self.assertFalse(self.guardian_invocations.exists())
        commands = self.commands.read_text()
        for mutation in (
            f"systemctl --user stop {TEXT_UNIT}",
            f"systemctl --user restart {TEXT_UNIT}",
            f"systemctl --user start {TEXT_UNIT}",
        ):
            self.assertNotIn(mutation, commands)

    def test_configured_target_rejects_hostile_systemd_output_mutations(self) -> None:
        original = (self.states / f"{GUARDIAN_UNIT}.show").read_text()
        mutations = {
            "missing LoadState": original.replace("LoadState=loaded\n", ""),
            "duplicate ActiveState": original + "ActiveState=active\n",
            "substring Result": original.replace("Result=success\n", "Result=successfully\n"),
            "non-numeric MainPID": original.replace("MainPID=102\n", "MainPID=102oops\n"),
            "unknown field": original + "ActiveStateHint=active\n",
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                (self.states / f"{GUARDIAN_UNIT}.show").write_text(mutation)
                result = self._run("configured-target")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.guardian_invocations.exists())
                (self.states / f"{GUARDIAN_UNIT}.show").write_text(original)

    def test_configured_target_rejects_stale_target_surfaces(self) -> None:
        scenarios = []

        self._write_config("querit", "querit-cgroup.v1")
        scenarios.append(("wrong config", self._run("configured-target")))
        self._write_config("aeon-text", "text-cgroup.v1")

        text_registration = self.runtime / "gb10-memory-guardian" / "text-cgroup.v1"
        text_registration.unlink()
        self._write_registration("querit-cgroup.v1")
        scenarios.append(("only querit registration", self._run("configured-target")))
        self._write_registration("text-cgroup.v1")
        scenarios.append(("stale querit coexists with text", self._run("configured-target")))

        text_state = self.states / f"{TEXT_UNIT}.show"
        original_text = text_state.read_text()
        text_state.write_text(original_text.replace("text-cgroup.v1", "querit-cgroup.v1"))
        scenarios.append(("text unit publishes wrong registration", self._run("configured-target")))
        text_state.write_text(original_text)

        self.journal.write_text("gb10-memory-guardian: armed target querit\n")
        scenarios.append(("journal proves wrong target", self._run("configured-target")))

        for label, result in scenarios:
            with self.subTest(label=label):
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.guardian_invocations.exists())

    def test_disposable_canary_uses_same_strict_bounded_state_parser(self) -> None:
        result = self._run("disposable")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.commands.read_text()
        self.assertIn("systemd-run", commands)
        self.assertIn("--property=LoadState", commands)
        self.assertIn("--property=ExecMainStatus", commands)

    def test_disposable_canary_rejects_mutated_driver_result(self) -> None:
        driver = self.states / "gb10-memory-guardian-canary.service.show"
        driver.write_text(driver.read_text().replace("Result=success\n", "Result=successfully\n"))
        result = self._run("disposable")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_disposable_canary_rejects_transitional_target_state(self) -> None:
        self._write_state(
            "gb10-memory-guardian-disposable-canary.service.after",
            self._state(
                active="deactivating",
                sub="stop-sigterm",
                pid=0,
                result="signal",
                exit_status=15,
            ),
            literal=True,
        )
        result = self._run("disposable")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


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
        activation = deploy.index('enable --now "$guardian_unit"')
        for source in required_sources:
            self.assertIn(source, deploy)
            self.assertLess(deploy.index(source), activation, source)
        self.assertIn("install -m 0600", deploy)
        self.assertNotIn("systemd/*", deploy)
        self.assertIn("configured-target", deploy)
        self.assertIn("trap fail_closed_activation EXIT", deploy)

    def test_runbook_uses_fail_closed_deployer_not_loose_manual_enable(self) -> None:
        readme = (ROOT / "README.md").read_text()
        self.assertIn("gb10_deploy_memory_guardian.sh", readme)
        self.assertIn("aeon-text", readme)
        self.assertIn("text-cgroup.v1", readme)
        self.assertIn("owner-only", readme)
        self.assertIn("read-only configured-target identity check", readme)
        self.assertNotIn("cp systemd/*", readme)

    def test_deployer_never_swallows_failure_to_stop_stale_guardian(self) -> None:
        source = DEPLOY.read_text()
        for line in source.splitlines():
            if 'disable --now "$guardian_unit"' in line:
                self.assertNotIn("|| true", line)


if __name__ == "__main__":
    unittest.main()
