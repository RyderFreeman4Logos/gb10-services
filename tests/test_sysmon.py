#!/usr/bin/env python3
"""Deterministic contracts for sysmon CSV v5 telemetry."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sysmon.sh"
UNIT = ROOT / "systemd" / "sysmon.service"

V4_COLUMNS = (
    "timestamp,load_1m,load_5m,load_15m,mem_used_mb,mem_total_mb,"
    "swap_used_mb,swap_total_mb,tz0,tz1,tz2,tz3,tz4,tz5,tz6,"
    "nvme_c,nvme_s1,nvme_s2,gpu_temp_c,gpu_power_w,gpu_util_pct,"
    "gpu_clock_mhz,top1_proc_id,top1_rss_mb,top2_proc_id,top2_rss_mb,"
    "top3_proc_id,top3_rss_mb,top4_proc_id,top4_rss_mb,top5_proc_id,"
    "top5_rss_mb,disk_read_mb_s,disk_write_mb_s,disk_io_ms_s,"
    "swap_in_mb_s,swap_out_mb_s,top1_swap_pid,top1_swap_proc_id,"
    "top1_swap_mb,top2_swap_pid,top2_swap_proc_id,top2_swap_mb,"
    "top3_swap_pid,top3_swap_proc_id,top3_swap_mb,top4_swap_pid,"
    "top4_swap_proc_id,top4_swap_mb,top5_swap_pid,top5_swap_proc_id,"
    "top5_swap_mb"
).split(",")


class SysmonSchemaContractTests(unittest.TestCase):
    def test_v5_appends_fields_without_reordering_v4_columns(self) -> None:
        source = SCRIPT.read_text()
        match = re.search(r'^HEADER="([^"]+)"$', source, re.MULTILINE)
        if match is None:
            self.fail("sysmon HEADER assignment is missing")
        columns = match.group(1).split(",")
        self.assertEqual(columns[: len(V4_COLUMNS)], V4_COLUMNS)
        self.assertEqual(
            columns[len(V4_COLUMNS) :],
            [
                "mem_available_mb",
                "sample_cadence_ms",
                "sample_elapsed_ms",
                "sample_lag_ms",
            ],
        )
        self.assertIn("v5", source)

    def test_source_and_unit_remain_observer_only(self) -> None:
        source = SCRIPT.read_text()
        unit = UNIT.read_text()
        for forbidden in (
            "systemctl",
            "docker ",
            "cgroup.kill",
            "/sys/fs/cgroup",
            "OnFailure=",
            "ExecStop=",
        ):
            self.assertNotIn(forbidden, source)
            self.assertNotIn(forbidden, unit)
        self.assertNotRegex(source, r"MemAvailable[^\n]*(?:<|>|-lt|-gt|-le|-ge)")
        self.assertIn("observer-only", source.lower())
        self.assertIn("observer-only", unit.lower())
    def test_docs_describe_target_interval_without_claiming_guaranteed_one_hz(self) -> None:
        readme = (ROOT / "README.md").read_text()
        unit = UNIT.read_text()
        for forbidden in ("Logs 1Hz Stats", "executing at 1Hz", "every 1s"):
            self.assertNotIn(forbidden, readme)
            self.assertNotIn(forbidden, unit)
        self.assertIn("observed cadence", readme)
        self.assertIn("target interval", unit)


class SysmonFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.log_dir = self.home / "log"
        self.proc = self.root / "proc"
        self.fake_bin = self.root / "bin"
        self.home.mkdir()
        self.log_dir.mkdir()
        self.proc.mkdir()
        self.fake_bin.mkdir()

        (self.proc / "meminfo").write_text(
            textwrap.dedent(
                """\
                MemTotal:       1048576 kB
                MemFree:          10240 kB
                MemAvailable:    204800 kB
                SwapTotal:       131072 kB
                SwapFree:         32768 kB
                """
            )
        )
        (self.proc / "loadavg").write_text("1.25 2.50 3.75 1/100 123\n")
        (self.proc / "vmstat").write_text("pswpin 10\npswpout 20\n")
        (self.proc / "diskstats").write_text(
            "259 0 nvme0n1 1 0 100 0 1 0 200 0 0 300 0 0 0 0 0 0\n"
        )
        (self.root / "clock").write_text(
            "1700000000000000\n1700000002500000\n"
            "1700000002500000\n1700000003000000\n"
        )
        fake_free = self.fake_bin / "free"
        fake_free.write_text(
            "#!/bin/sh\n"
            "printf '              total        used        free\\n'\n"
            "printf 'Mem:           1024         800         224\\n'\n"
        )
        fake_free.chmod(0o755)
        fake_ps = self.fake_bin / "ps"
        fake_ps.write_text("#!/bin/sh\nexit 0\n")
        fake_ps.chmod(0o755)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self) -> Path:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "TZ": "UTC",
                "SYSMON_LOG_DIR": str(self.log_dir),
                "SYSMON_PROC_ROOT": str(self.proc),
                "SYSMON_CLOCK_FILE": str(self.root / "clock"),
                "SYSMON_MAX_SAMPLES": "2",
                "SYSMON_TEST_MODE": "1",
                "SYSMON_GPU_LINE": "40, 120.5, 75, 1800",
            }
        )
        subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=5,
            text=True,
            capture_output=True,
        )
        return self.log_dir / "sysmon_2023-11-14.csv"

    def test_fixture_records_exact_memavailable_and_actual_overrun(self) -> None:
        logfile = self._run()
        with logfile.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["mem_available_mb"], "200")
        self.assertEqual(rows[0]["sample_cadence_ms"], "0")
        self.assertEqual(rows[0]["sample_elapsed_ms"], "2500")
        self.assertEqual(rows[0]["sample_lag_ms"], "1500")
        self.assertEqual(rows[1]["sample_cadence_ms"], "2500")
        self.assertEqual(rows[1]["sample_elapsed_ms"], "500")
        self.assertEqual(rows[1]["sample_lag_ms"], "0")
        self.assertEqual(rows[0]["mem_used_mb"], "800")
        self.assertEqual(rows[0]["mem_total_mb"], "1024")

    def test_old_schema_rotates_before_v5_rows_are_written(self) -> None:
        old = self.log_dir / "sysmon_2023-11-14.csv"
        old.write_text(",".join(V4_COLUMNS) + "\nlegacy-row\n")
        logfile = self._run()
        backups = list(self.log_dir.glob("sysmon_2023-11-14.pre-v5.*.csv"))
        self.assertEqual(len(backups), 1)
        self.assertIn("legacy-row", backups[0].read_text())
        self.assertEqual(logfile.read_text().splitlines()[0].split(",")[: len(V4_COLUMNS)], V4_COLUMNS)


if __name__ == "__main__":
    unittest.main()
