#!/usr/bin/env python3
"""Deterministic contracts for sysmon CSV v6 telemetry."""

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

V5_COLUMNS = V4_COLUMNS + [
    "mem_available_mb",
    "sample_cadence_ms",
    "sample_elapsed_ms",
    "sample_lag_ms",
]


class SysmonSchemaContractTests(unittest.TestCase):
    def test_v6_appends_boot_pressure_without_reordering_v5_columns(self) -> None:
        source = SCRIPT.read_text()
        match = re.search(r'^HEADER="([^"]+)"$', source, re.MULTILINE)
        if match is None:
            self.fail("sysmon HEADER assignment is missing")
        columns = match.group(1).split(",")
        self.assertEqual(columns[: len(V5_COLUMNS)], V5_COLUMNS)
        self.assertEqual(
            columns[len(V5_COLUMNS) :],
            [
                "boot_id",
                "memory_some_avg10",
                "memory_full_avg10",
                "io_full_avg10",
                "cpu_some_avg10",
            ],
        )
        self.assertIn("v6", source)

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

    def test_unit_starts_without_default_target_ordering(self) -> None:
        def assert_start_contract(unit: str) -> None:
            active = [
                line.strip()
                for line in unit.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            wanted = [
                target
                for line in active
                if line.startswith("WantedBy=")
                for target in line.partition("=")[2].split()
            ]
            after = [
                target
                for line in active
                if line.startswith("After=")
                for target in line.partition("=")[2].split()
            ]
            self.assertIn("default.target", wanted)
            self.assertNotIn("default.target", after)

        unit = UNIT.read_text()
        assert_start_contract(unit)
        self.assertIn(
            "UnsetEnvironment=SYSMON_LOG_DIR SYSMON_INTERVAL_SECONDS "
            "SYSMON_PROC_ROOT SYSMON_CLOCK_FILE SYSMON_MAX_SAMPLES "
            "SYSMON_TEST_MODE SYSMON_GPU_LINE",
            unit,
        )
        hostile = unit.replace("[Unit]\n", "[Unit]\nAfter=default.target\n", 1)
        self.assertNotEqual(hostile, unit)
        with self.assertRaises(AssertionError):
            assert_start_contract(hostile)

    def test_docs_describe_target_interval_without_claiming_guaranteed_one_hz(self) -> None:
        readme = (ROOT / "README.md").read_text()
        deployment = (ROOT / "docs" / "deployment" / "AGENTS.md").read_text()
        source = SCRIPT.read_text()
        unit = UNIT.read_text()
        for forbidden in ("Logs 1Hz Stats", "executing at 1Hz", "every 1s"):
            self.assertNotIn(forbidden, readme)
            self.assertNotIn(forbidden, unit)
        self.assertNotIn("1Hz monitor", deployment)
        self.assertNotIn("1 Hz CSV", source)
        self.assertNotIn("1 Hz log", source)
        self.assertIn("observed cadence", readme)
        self.assertIn("target interval", unit)
        self.assertIn("CSV v6", readme)
        self.assertIn("`--test-only`", readme)
        self.assertIn("rejects inherited `SYSMON_*`", readme)
        self.assertIn("`UnsetEnvironment=`", deployment)
        for field in (
            "boot_id",
            "memory_some_avg10",
            "memory_full_avg10",
            "io_full_avg10",
            "cpu_some_avg10",
        ):
            self.assertIn(field, readme)


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
        boot_id = self.proc / "sys" / "kernel" / "random" / "boot_id"
        boot_id.parent.mkdir(parents=True)
        boot_id.write_text("12345678-1234-4abc-8def-1234567890ab\n")
        pressure = self.proc / "pressure"
        pressure.mkdir()
        (pressure / "memory").write_text(
            "some avg10=1.23 avg60=2.34 avg300=3.45 total=999999\n"
            "full avg10=0.04 avg60=0.05 avg300=0.06 total=888888\n"
        )
        (pressure / "io").write_text(
            "some avg10=91.00 avg60=1.00 avg300=2.00 total=777777\n"
            "full avg10=5.67 avg60=6.78 avg300=7.89 total=666666\n"
        )
        (pressure / "cpu").write_text(
            "some avg10=9.87 avg60=8.76 avg300=7.65 total=555555\n"
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
            ["bash", str(SCRIPT), "--test-only"],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=5,
            text=True,
            capture_output=True,
        )
        return self.log_dir / "sysmon_2023-11-14.csv"

    @staticmethod
    def _rows(logfile: Path) -> list[dict[str, str]]:
        with logfile.open(newline="") as handle:
            return list(csv.DictReader(handle))

    def _assert_pressure_unavailable(self, row: dict[str, str]) -> None:
        self.assertEqual(
            [
                row.get("boot_id"),
                row.get("memory_some_avg10"),
                row.get("memory_full_avg10"),
                row.get("io_full_avg10"),
                row.get("cpu_some_avg10"),
            ],
            ["N/A"] * 5,
        )

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
        self.assertEqual(
            rows[0].get("boot_id"), "12345678-1234-4abc-8def-1234567890ab"
        )
        self.assertEqual(rows[0].get("memory_some_avg10"), "1.23")
        self.assertEqual(rows[0].get("memory_full_avg10"), "0.04")
        self.assertEqual(rows[0].get("io_full_avg10"), "5.67")
        self.assertEqual(rows[0].get("cpu_some_avg10"), "9.87")
        self.assertEqual(rows[0]["mem_used_mb"], "800")
        self.assertEqual(rows[0]["mem_total_mb"], "1024")

    def test_malformed_or_missing_boot_pressure_keeps_explicit_aligned_values(self) -> None:
        (self.proc / "sys" / "kernel" / "random" / "boot_id").write_text(
            "malformed,boot,content\n"
        )
        (self.proc / "pressure" / "memory").write_text(
            "some avg60=1.23 total=123\n"
            "full avg10=101.00 total=456\n"
        )
        (self.proc / "pressure" / "io").unlink()
        (self.proc / "pressure" / "cpu").write_text(
            "some avg10_total=9.87 total=987\n"
        )

        logfile = self._run()
        with logfile.open(newline="") as handle:
            csv_rows = list(csv.reader(handle))
        self.assertTrue(
            all(len(row) == len(csv_rows[0]) for row in csv_rows[1:]),
            "unavailable boot/PSI values must not shift CSV columns",
        )
        with logfile.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn(None, row)
            self._assert_pressure_unavailable(row)

    def test_production_rejects_test_overrides_before_sampling(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "TZ": "UTC",
                "SYSMON_LOG_DIR": str(self.log_dir),
                "SYSMON_PROC_ROOT": str(self.proc),
                "SYSMON_CLOCK_FILE": str(self.root / "clock"),
                "SYSMON_MAX_SAMPLES": "1",
                "SYSMON_TEST_MODE": "1",
                "SYSMON_GPU_LINE": "40, 120.5, 75, 1800",
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=ROOT,
            env=env,
            check=False,
            timeout=2,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--test-only", result.stderr)
        self.assertEqual(list(self.log_dir.iterdir()), [])

    def test_boot_id_fifo_is_rejected_without_blocking(self) -> None:
        boot_id = self.proc / "sys" / "kernel" / "random" / "boot_id"
        boot_id.unlink()
        os.mkfifo(boot_id)
        rows = self._rows(self._run())
        self.assertEqual(rows[0]["boot_id"], "N/A")
        self.assertEqual(rows[0]["memory_some_avg10"], "1.23")

    def test_psi_fifo_is_rejected_without_blocking(self) -> None:
        memory = self.proc / "pressure" / "memory"
        memory.unlink()
        os.mkfifo(memory)
        rows = self._rows(self._run())
        self.assertEqual(rows[0]["memory_some_avg10"], "N/A")
        self.assertEqual(rows[0]["memory_full_avg10"], "N/A")
        self.assertEqual(rows[0]["boot_id"], "12345678-1234-4abc-8def-1234567890ab")

    def test_symlink_device_and_non_regular_inputs_degrade_to_na(self) -> None:
        boot_id = self.proc / "sys" / "kernel" / "random" / "boot_id"
        boot_id.unlink()
        boot_id.symlink_to("/dev/null")
        memory = self.proc / "pressure" / "memory"
        memory.unlink()
        memory.symlink_to("/dev/null")
        io_pressure = self.proc / "pressure" / "io"
        io_pressure.unlink()
        io_pressure.mkdir()
        cpu = self.proc / "pressure" / "cpu"
        cpu.unlink()
        cpu.symlink_to("/dev/null")
        self._assert_pressure_unavailable(self._rows(self._run())[0])

    def test_oversize_boot_and_psi_inputs_degrade_to_na(self) -> None:
        (self.proc / "sys" / "kernel" / "random" / "boot_id").write_text(
            "a" * 1024 + "\n"
        )
        (self.proc / "pressure" / "memory").write_text(
            "x" * 1024
            + "\nsome avg10=1.23 total=1\nfull avg10=0.04 total=2\n"
        )
        rows = self._rows(self._run())
        self.assertEqual(rows[0]["boot_id"], "N/A")
        self.assertEqual(rows[0]["memory_some_avg10"], "N/A")
        self.assertEqual(rows[0]["memory_full_avg10"], "N/A")

    def test_missing_final_newlines_degrade_to_na(self) -> None:
        (self.proc / "sys" / "kernel" / "random" / "boot_id").write_text(
            "12345678-1234-4abc-8def-1234567890ab"
        )
        (self.proc / "pressure" / "memory").write_text(
            "some avg10=1.23 total=1\nfull avg10=0.04 total=2"
        )
        (self.proc / "pressure" / "io").write_text("full avg10=5.67 total=3")
        (self.proc / "pressure" / "cpu").write_text("some avg10=9.87 total=4")
        self._assert_pressure_unavailable(self._rows(self._run())[0])

    def test_duplicate_class_and_avg10_degrade_to_na(self) -> None:
        (self.proc / "pressure" / "memory").write_text(
            "some avg10=1.23 total=1\nsome avg10=2.34 total=2\n"
            "full avg10=0.04 total=3\n"
        )
        (self.proc / "pressure" / "io").write_text(
            "full avg10=5.67 avg10=6.78 total=4\n"
        )
        rows = self._rows(self._run())
        self.assertEqual(rows[0]["memory_some_avg10"], "N/A")
        self.assertEqual(rows[0]["memory_full_avg10"], "0.04")
        self.assertEqual(rows[0]["io_full_avg10"], "N/A")

    def test_all_boot_pressure_inputs_missing_degrade_to_na(self) -> None:
        (self.proc / "sys" / "kernel" / "random" / "boot_id").unlink()
        for resource in ("memory", "io", "cpu"):
            (self.proc / "pressure" / resource).unlink()
        self._assert_pressure_unavailable(self._rows(self._run())[0])

    def test_old_v5_schema_rotates_before_v6_rows_are_written(self) -> None:
        old = self.log_dir / "sysmon_2023-11-14.csv"
        old.write_text(",".join(V5_COLUMNS) + "\nlegacy-row\n")
        logfile = self._run()
        backups = list(self.log_dir.glob("sysmon_2023-11-14.pre-v6.*.csv"))
        self.assertEqual(len(backups), 1)
        self.assertIn("legacy-row", backups[0].read_text())
        self.assertEqual(
            logfile.read_text().splitlines()[0].split(",")[: len(V4_COLUMNS)],
            V4_COLUMNS,
        )


if __name__ == "__main__":
    unittest.main()
