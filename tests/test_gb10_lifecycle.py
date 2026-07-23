from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gb10_lifecycle.sh"
GUARD_HELPER = ROOT / "scripts" / "aeon_text_stop_start.sh"
RESTART_HELPER = ROOT / "scripts" / "gb10_restart_text_safe.sh"
RUNBOOK = ROOT / "docs" / "deployment" / "AGENTS.md"
PRODUCTION_STATE = "/home/obj/.local/state/gb10-lifecycle"
UNIT = "vllm-aeon-27b-dflash.service"


class LifecycleAuditScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"

        source = SCRIPT.read_text()
        state_assignment = f'readonly STATE_DIR="{PRODUCTION_STATE}"'
        self.assertEqual(source.count(state_assignment), 1)
        self.script = self.root / "gb10_lifecycle.sh"
        self.script.write_text(
            source.replace(state_assignment, f'readonly STATE_DIR="{self.state}"')
        )

        self.systemctl_log = self.root / "systemctl.log"
        self.systemctl = self.root / "systemctl"
        self.systemctl.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$GB10_LIFECYCLE_TEST_SYSTEMCTL_LOG"\n'
            'if [ -n "${GB10_LIFECYCLE_TEST_SYSTEMCTL_ENTERED:-}" ]; then\n'
            '    : > "$GB10_LIFECYCLE_TEST_SYSTEMCTL_ENTERED"\n'
            '    while [ ! -e "$GB10_LIFECYCLE_TEST_SYSTEMCTL_RELEASE" ]; do\n'
            "        /usr/bin/sleep 0.01\n"
            "    done\n"
            "fi\n"
            'exit "${GB10_LIFECYCLE_TEST_SYSTEMCTL_STATUS:-0}"\n'
        )
        self.systemctl.chmod(self.systemctl.stat().st_mode | stat.S_IXUSR)

        self.environment = os.environ.copy()
        self.environment.update(
            {
                "GB10_LIFECYCLE_SYSTEMCTL": str(self.systemctl),
                "GB10_LIFECYCLE_TEST_SYSTEMCTL_LOG": str(self.systemctl_log),
                "GB10_LIFECYCLE_STATE_DIR": str(self.root / "ignored-state"),
                "XDG_STATE_HOME": str(self.root / "ignored-xdg-state"),
                "HOME": str(self.root / "ignored-home"),
            }
        )

    def execute(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/bash", str(self.script), *arguments],
            cwd=ROOT,
            env=self.environment if environment is None else environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def spawn(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["/usr/bin/bash", str(self.script), *arguments],
            cwd=ROOT,
            env=self.environment if environment is None else environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def arguments(
        self,
        action: str,
        *,
        reason: str = "approved-maintenance",
    ) -> tuple[str, ...]:
        return (
            action,
            "--unit",
            UNIT,
            "--actor",
            "test-operator",
            "--reason",
            reason,
        )

    def test_production_script_uses_only_the_canonical_state_directory(self) -> None:
        source = SCRIPT.read_text()

        self.assertIn(f'readonly STATE_DIR="{PRODUCTION_STATE}"', source)
        self.assertNotIn("GB10_LIFECYCLE_STATE_DIR", source)
        self.assertNotIn("XDG_STATE_HOME", source)
        self.assertNotIn("${HOME", source)

    def test_stop_is_audited_before_calling_systemctl(self) -> None:
        result = self.execute(*self.arguments("stop"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.systemctl_log.read_text(), f"--user stop {UNIT}\n")
        audit_log = self.state / "lifecycle-audit.log"
        self.assertEqual(stat.S_IMODE(audit_log.stat().st_mode), 0o600)
        audit = audit_log.read_text()
        self.assertIn(
            "event=request action=stop unit=vllm-aeon-27b-dflash.service "
            "actor=test-operator reason=approved-maintenance outcome=accepted",
            audit,
        )
        self.assertIn(
            "event=result action=stop unit=vllm-aeon-27b-dflash.service "
            "actor=test-operator reason=approved-maintenance outcome=success",
            audit,
        )
        self.assertFalse((self.root / "ignored-state").exists())
        self.assertFalse((self.root / "ignored-xdg-state").exists())
        self.assertFalse((self.root / "ignored-home").exists())

    def test_investigation_lock_blocks_model_lifecycle_until_it_is_closed(self) -> None:
        begin = self.execute(
            "investigation-begin",
            "--actor",
            "benchmark-forensics",
            "--reason",
            "incident-26",
        )
        blocked = self.execute(*self.arguments("stop", reason="do-not-run"))
        end = self.execute(
            "investigation-end",
            "--actor",
            "benchmark-forensics",
            "--reason",
            "forensics-complete",
        )
        started = self.execute(*self.arguments("start"))

        self.assertEqual(begin.returncode, 0, begin.stdout + begin.stderr)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("active investigation lock", blocked.stderr)
        self.assertEqual(end.returncode, 0, end.stdout + end.stderr)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(self.systemctl_log.read_text(), f"--user start {UNIT}\n")
        audit = (self.state / "lifecycle-audit.log").read_text()
        self.assertIn(
            "event=investigation-begin actor=benchmark-forensics "
            "reason=incident-26 outcome=created",
            audit,
        )
        self.assertIn(
            "event=request action=stop unit=vllm-aeon-27b-dflash.service "
            "actor=test-operator reason=do-not-run outcome=blocked investigation=active",
            audit,
        )
        requested = (
            "event=investigation-end actor=benchmark-forensics "
            "reason=forensics-complete outcome=requested"
        )
        closed = (
            "event=investigation-end actor=benchmark-forensics "
            "reason=forensics-complete outcome=closed"
        )
        self.assertIn(requested, audit)
        self.assertIn(closed, audit)
        self.assertLess(audit.index(requested), audit.index(closed))

    def test_investigation_end_keeps_marker_when_request_audit_fails(self) -> None:
        begin = self.execute(
            "investigation-begin",
            "--actor",
            "benchmark-forensics",
            "--reason",
            "incident-26",
        )
        self.assertEqual(begin.returncode, 0, begin.stdout + begin.stderr)
        marker = self.state / "investigation.lock"
        self.assertTrue(marker.is_file())

        audit_log = self.state / "lifecycle-audit.log"
        audit_log.unlink()
        self.addCleanup(
            lambda: audit_log.chmod(0o700) if audit_log.is_dir() else None
        )
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        fake_touch = fake_bin / "touch"
        fake_touch.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "    *lifecycle-audit.log) /usr/bin/mkdir -p -- \"$1\" ;;\n"
            '    *) /usr/bin/touch "$@" ;;\n'
            "esac\n"
        )
        fake_touch.chmod(fake_touch.stat().st_mode | stat.S_IXUSR)
        environment = self.environment.copy()
        environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"

        end = self.execute(
            "investigation-end",
            "--actor",
            "benchmark-forensics",
            "--reason",
            "forensics-complete",
            environment=environment,
        )

        self.assertNotEqual(end.returncode, 0)
        self.assertTrue(marker.is_file())

    def test_investigation_end_records_unlink_failure_outcome(self) -> None:
        begin = self.execute(
            "investigation-begin",
            "--actor",
            "benchmark-forensics",
            "--reason",
            "incident-26",
        )
        self.assertEqual(begin.returncode, 0, begin.stdout + begin.stderr)
        marker = self.state / "investigation.lock"

        fake_bin = self.root / "fake-rm-bin"
        fake_bin.mkdir()
        fake_rm = fake_bin / "rm"
        fake_rm.write_text("#!/bin/sh\nexit 23\n")
        fake_rm.chmod(fake_rm.stat().st_mode | stat.S_IXUSR)
        environment = self.environment.copy()
        environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"

        end = self.execute(
            "investigation-end",
            "--actor",
            "benchmark-forensics",
            "--reason",
            "forensics-complete",
            environment=environment,
        )

        self.assertEqual(end.returncode, 23, end.stdout + end.stderr)
        self.assertTrue(marker.is_file())
        audit = (self.state / "lifecycle-audit.log").read_text()
        self.assertIn(
            "event=investigation-end actor=benchmark-forensics "
            "reason=forensics-complete outcome=failure exit_status=23",
            audit,
        )

    def test_lifecycle_mutex_is_held_until_systemctl_finishes(self) -> None:
        entered = self.root / "systemctl-entered"
        release = self.root / "systemctl-release"
        environment = self.environment.copy()
        environment.update(
            {
                "GB10_LIFECYCLE_TEST_SYSTEMCTL_ENTERED": str(entered),
                "GB10_LIFECYCLE_TEST_SYSTEMCTL_RELEASE": str(release),
            }
        )
        owner = self.spawn(*self.arguments("stop"), environment=environment)
        try:
            deadline = time.monotonic() + 5
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(entered.exists(), "fake systemctl was not entered")

            contender = self.execute(*self.arguments("start"))
            self.assertNotEqual(contender.returncode, 0)
            self.assertIn(
                "another lifecycle operation holds the mutex",
                contender.stderr,
            )
        finally:
            release.touch()
            stdout, stderr = owner.communicate(timeout=5)

        self.assertEqual(owner.returncode, 0, stdout + stderr)
        self.assertEqual(self.systemctl_log.read_text(), f"--user stop {UNIT}\n")

    def test_failed_result_keeps_reason_and_exit_status(self) -> None:
        environment = self.environment.copy()
        environment["GB10_LIFECYCLE_TEST_SYSTEMCTL_STATUS"] = "7"

        result = self.execute(*self.arguments("stop"), environment=environment)

        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        audit = (self.state / "lifecycle-audit.log").read_text()
        self.assertIn(
            "event=result action=stop unit=vllm-aeon-27b-dflash.service "
            "actor=test-operator reason=approved-maintenance "
            "outcome=failure exit_status=7",
            audit,
        )

    def test_restart_is_rejected_so_restarts_are_explicit_stop_start_operations(self) -> None:
        result = self.execute(*self.arguments("restart"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restart is forbidden", result.stderr)
        self.assertFalse(self.systemctl_log.exists())


class LifecycleIntegrationContractTests(unittest.TestCase):
    def make_executable(self, path: Path, content: str) -> None:
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def test_runbook_installs_guard_helper_and_documents_the_event_schema(self) -> None:
        runbook = " ".join(RUNBOOK.read_text().split())

        self.assertIn(
            "install -m 0755 scripts/aeon_text_stop_start.sh "
            "/home/obj/scripts/aeon_text_stop_start.sh",
            runbook,
        )
        self.assertIn(
            f"fixed production path `{PRODUCTION_STATE}/lifecycle-audit.log`",
            runbook,
        )
        self.assertIn(
            "Every record contains UTC and monotonic timestamps, UID, PID, "
            "event, actor, reason, and outcome",
            runbook,
        )
        self.assertIn(
            "failed results and failed investigation closes include "
            "`exit_status`, and blocked requests identify the active "
            "investigation",
            runbook,
        )

    def test_restart_helper_has_no_pre_lifecycle_model_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.log"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            lifecycle = root / "lifecycle"
            fake_systemctl = root / "systemctl"
            self.make_executable(
                lifecycle,
                "#!/bin/sh\n"
                'printf "lifecycle %s\\n" "$*" >> "$GB10_HELPER_EVENT_LOG"\n',
            )
            self.make_executable(
                fake_systemctl,
                "#!/bin/sh\n"
                'printf "systemctl %s\\n" "$*" >> "$GB10_HELPER_EVENT_LOG"\n',
            )
            self.make_executable(
                fake_bin / "curl",
                "#!/bin/sh\n"
                'printf "curl %s\\n" "$*" >> "$GB10_HELPER_EVENT_LOG"\n',
            )
            helper = root / "gb10_restart_text_safe.sh"
            helper.write_text(
                RESTART_HELPER.read_text().replace("systemctl", str(fake_systemctl))
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "GB10_HELPER_EVENT_LOG": str(events),
                    "GB10_LIFECYCLE_BIN": str(lifecycle),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )

            result = subprocess.run(
                ["/usr/bin/bash", str(helper), "--rr-only"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            event_lines = events.read_text().splitlines()
            self.assertTrue(event_lines[0].startswith("lifecycle start "))
            self.assertFalse(any("reset-failed" in line for line in event_lines))
            self.assertFalse(
                any(line.startswith("systemctl ") for line in event_lines)
            )

    def test_guard_helper_calls_lifecycle_without_an_outer_timeout(self) -> None:
        source = GUARD_HELPER.read_text()
        self.assertNotIn("/usr/bin/timeout", source)
        self.assertNotIn("STOP_TIMEOUT_SECS", source)
        self.assertNotIn("START_TIMEOUT_SECS", source)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.log"
            lifecycle = root / "lifecycle"
            systemctl = root / "systemctl"
            sleep = root / "sleep"
            self.make_executable(
                lifecycle,
                "#!/bin/sh\n"
                'printf "lifecycle %s\\n" "$*" >> "$GB10_HELPER_EVENT_LOG"\n',
            )
            self.make_executable(
                systemctl,
                "#!/bin/sh\n"
                'printf "systemctl %s\\n" "$*" >> "$GB10_HELPER_EVENT_LOG"\n',
            )
            self.make_executable(sleep, "#!/bin/sh\nexit 0\n")
            helper = root / "aeon_text_stop_start.sh"
            helper.write_text(
                source.replace("/usr/bin/systemctl", str(systemctl)).replace(
                    "/usr/bin/sleep",
                    str(sleep),
                )
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "GB10_HELPER_EVENT_LOG": str(events),
                    "GB10_LIFECYCLE_BIN": str(lifecycle),
                }
            )

            result = subprocess.run(
                ["/usr/bin/bash", str(helper)],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            event_lines = events.read_text().splitlines()
            self.assertEqual(len(event_lines), 3)
            self.assertTrue(event_lines[0].startswith("lifecycle stop "))
            self.assertTrue(event_lines[1].startswith("lifecycle start "))
            self.assertEqual(
                event_lines[2],
                "systemctl --user is-active --quiet "
                "vllm-aeon-27b-dflash.service",
            )


if __name__ == "__main__":
    unittest.main()
