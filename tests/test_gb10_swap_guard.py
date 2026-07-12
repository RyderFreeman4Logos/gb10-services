from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gb10-swap-guard.sh"
UNIT = ROOT / "systemd" / "gb10-swap-guard.service"


class SwapGuardSourceContractTests(unittest.TestCase):
    def run_one_detection_cycle(
        self, *, mem_available_kib: int, swap_used_kib: int = 0
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            calls = tmp / "calls.log"
            for command in ("systemctl", "docker", "free", "swapon", "vmstat"):
                executable = fake_bin / command
                executable.write_text(
                    "#!/usr/bin/env bash\n"
                    f'printf \'%s\\n\' "{command} $*" >> {calls}\n'
                )
                executable.chmod(0o755)

            swap_total_kib = 16 * 1024 * 1024
            meminfo = tmp / "meminfo"
            meminfo.write_text(
                "MemTotal:       134217728 kB\n"
                f"MemAvailable:  {mem_available_kib:>12} kB\n"
                f"SwapTotal:     {swap_total_kib:>12} kB\n"
                f"SwapFree:      {swap_total_kib - swap_used_kib:>12} kB\n"
            )
            env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "HOME": raw_tmp,
                "GB10_SWAP_GUARD_MEMINFO_PATH": str(meminfo),
                "GB10_SWAP_GUARD_ONESHOT": "1",
                "GB10_SWAP_GUARD_SKIP_DIAGNOSTICS": "1",
                "GB10_SWAP_GUARD_LOG_DIR": raw_tmp,
            }
            try:
                result = subprocess.run(
                    ["bash", str(SCRIPT)],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail("guard ignored GB10_SWAP_GUARD_ONESHOT")
            return result, calls.read_text() if calls.exists() else ""

    def test_guard_polls_once_per_second(self) -> None:
        unit = UNIT.read_text()
        match = re.search(r"^Environment=GB10_SWAP_GUARD_INTERVAL=(.+)$", unit, re.MULTILINE)
        if match is None:
            self.fail("GB10_SWAP_GUARD_INTERVAL is missing")
        self.assertEqual(match.group(1), "1")

    def test_guard_has_memavailable_stop_policy(self) -> None:
        script = SCRIPT.read_text()
        unit = UNIT.read_text()
        self.assertIn("GB10_MEM_AVAIL_STOP_GIB", script)
        self.assertIn("GB10_MEM_AVAIL_STOP_GIB=1", unit)
        self.assertRegex(script, r"mem_avail\s*<\s*MEM_AVAIL_STOP_BYTES")

    def test_guard_sheds_active_querit_reranker(self) -> None:
        script = SCRIPT.read_text()
        self.assertIn("systemctl --user stop querit-4b-reranker.service", script)
        self.assertIn("docker stop querit-4b-reranker", script)

    def test_meminfo_detection_hot_path_avoids_python(self) -> None:
        script = SCRIPT.read_text()
        match = re.search(r"read_mem_bytes\(\) \{(?P<body>.*?)\n\}", script, re.DOTALL)
        if match is None:
            self.fail("read_mem_bytes function is missing")
        self.assertNotIn("python3", match.group("body"))

    def test_low_memavailable_stops_active_querit_in_one_detection_cycle(self) -> None:
        result, recorded = self.run_one_detection_cycle(mem_available_kib=1023 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("avail=1.0GiB", result.stdout)
        self.assertIn("systemctl --user stop querit-4b-reranker.service", recorded)
        self.assertIn("docker stop querit-4b-reranker", recorded)

    def test_exact_memavailable_threshold_does_not_stop(self) -> None:
        result, recorded = self.run_one_detection_cycle(mem_available_kib=1024 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("STOP_RERANKER", result.stdout)
        self.assertEqual(recorded, "")

    def test_healthy_memory_and_swap_do_not_stop(self) -> None:
        result, recorded = self.run_one_detection_cycle(mem_available_kib=8 * 1024 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("STOP_RERANKER", result.stdout)
        self.assertEqual(recorded, "")

    def test_high_swap_still_stops_reranker(self) -> None:
        result, recorded = self.run_one_detection_cycle(
            mem_available_kib=8 * 1024 * 1024,
            swap_used_kib=13 * 1024 * 1024,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reason=swap>=12GiB", result.stdout)
        self.assertIn("systemctl --user stop querit-4b-reranker.service", recorded)


if __name__ == "__main__":
    unittest.main()
