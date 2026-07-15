from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gb10-swap-guard.sh"
UNIT = ROOT / "systemd" / "gb10-swap-guard.service"


class SwapGuardObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.meminfo = self.root / "meminfo"
        self.log = self.root / "swap-guard.log"
        self.events = self.root / "events.log"
        self.commands = self.root / "commands.log"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self._write_fake_commands()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_fake_commands(self) -> None:
        for command in ("free", "swapon", "vmstat", "docker"):
            path = self.bin / command
            path.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s %s\\n' '{command}' \"$*\" >>\"$COMMAND_LOG\"\n"
            )
            path.chmod(0o755)
        timeout = self.bin / "timeout"
        timeout.write_text(
            "#!/usr/bin/env bash\n"
            "while [[ $# -gt 0 && $1 == -* ]]; do shift; done\n"
            "[[ $# -gt 0 && $1 =~ ^[0-9]+$ ]] && shift\n"
            '"$@"\n'
        )
        timeout.chmod(0o755)

    def _run(self, *, available_kib: int, swap_total_kib: int, swap_free_kib: int) -> subprocess.CompletedProcess[str]:
        self.meminfo.write_text(
            f"MemTotal: 1048576 kB\nMemAvailable: {available_kib} kB\n"
            f"SwapTotal: {swap_total_kib} kB\nSwapFree: {swap_free_kib} kB\n"
        )
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "COMMAND_LOG": str(self.commands),
                "GB10_MEMINFO_PATH": str(self.meminfo),
                "GB10_SWAP_GUARD_LOG": str(self.log),
                "GB10_SWAP_GUARD_TEST_EVENT_LOG": str(self.events),
                "GB10_SWAP_GUARD_ONESHOT": "1",
                "GB10_SWAP_WARN_GIB": "7.5",
                "GB10_SWAP_ALERT_GIB": "12",
                "GB10_MEM_AVAIL_ALERT_GIB": "1",
                "GB10_SWAP_ATTRIBUTION_INTERVAL": "1",
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_low_memory_alerts_and_collects_read_only_evidence(self) -> None:
        result = self._run(available_kib=1_048_575, swap_total_kib=0, swap_free_kib=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events.read_text()
        self.assertIn("ALERT_MEMORY_PRESSURE", events)
        self.assertIn("observer_only=1", events)
        commands = self.commands.read_text()
        self.assertIn("free -h", commands)
        self.assertIn("swapon --show", commands)
        self.assertIn("vmstat", commands)
        self.assertIn("docker ps", commands)
        for mutation in (" stop", " kill", " restart", " rm", "systemctl"):
            self.assertNotIn(mutation, commands)

    def test_exact_threshold_is_not_an_alert(self) -> None:
        result = self._run(available_kib=1_048_576, swap_total_kib=0, swap_free_kib=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ALERT_MEMORY_PRESSURE", self.events.read_text())

    def test_high_swap_alerts_without_a_mutating_fallback(self) -> None:
        result = self._run(
            available_kib=4 * 1_048_576,
            swap_total_kib=16 * 1_048_576,
            swap_free_kib=4 * 1_048_576 - 1,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reason=swap", self.events.read_text())
        commands = self.commands.read_text()
        self.assertNotIn("docker stop", commands)
        self.assertNotIn("systemctl", commands)

    def test_log_sink_failure_does_not_disable_observation(self) -> None:
        self.log.mkdir()
        result = self._run(available_kib=1, swap_total_kib=0, swap_free_kib=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALERT_MEMORY_PRESSURE", self.events.read_text())

    def test_source_and_unit_are_observer_only(self) -> None:
        script = SCRIPT.read_text()
        unit = UNIT.read_text()
        self.assertIn("OBSERVER_ONLY", script)
        for forbidden in (
            "systemctl --user",
            "docker stop",
            "docker kill",
            "docker rm",
            "STOP_RERANKER",
            "stop_reranker",
        ):
            self.assertNotIn(forbidden, script)
        self.assertIn("observer-only", unit)
        for obsolete in (
            "GB10_SWAP_GUARD_STOP_RETRY_INTERVAL",
            "GB10_SWAP_GUARD_DOCKER_STOP_TIMEOUT",
            "GB10_SWAP_GUARD_SYSTEMCTL_STOP_TIMEOUT",
        ):
            self.assertNotIn(obsolete, unit)


if __name__ == "__main__":
    unittest.main()
