from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gb10_lifecycle.sh"
UNIT = "vllm-aeon-27b-dflash.service"


class LifecycleAuditScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.systemctl_log = self.root / "systemctl.log"
        self.systemctl = self.root / "systemctl"
        self.systemctl.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$GB10_LIFECYCLE_TEST_SYSTEMCTL_LOG"\n'
        )
        self.systemctl.chmod(self.systemctl.stat().st_mode | stat.S_IXUSR)
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "GB10_LIFECYCLE_STATE_DIR": str(self.state),
                "GB10_LIFECYCLE_SYSTEMCTL": str(self.systemctl),
                "GB10_LIFECYCLE_TEST_SYSTEMCTL_LOG": str(self.systemctl_log),
            }
        )

    def execute(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/bash", str(SCRIPT), *arguments],
            cwd=ROOT,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def arguments(self, action: str, *, reason: str = "approved-maintenance") -> tuple[str, ...]:
        return (
            action,
            "--unit",
            UNIT,
            "--actor",
            "test-operator",
            "--reason",
            reason,
        )

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
            "actor=test-operator outcome=success",
            audit,
        )

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
        self.assertIn(
            "event=investigation-end actor=benchmark-forensics "
            "reason=forensics-complete outcome=closed",
            audit,
        )

    def test_restart_is_rejected_so_restarts_are_explicit_stop_start_operations(self) -> None:
        result = self.execute(*self.arguments("restart"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restart is forbidden", result.stderr)
        self.assertFalse(self.systemctl_log.exists())


if __name__ == "__main__":
    unittest.main()
