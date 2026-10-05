"""Adversarial observer persistence checks; only fixture processes are signalled."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time
import unittest

import test_sysmon
from test_sysmon import ROOT, SCRIPT


class SysmonWriterLifecycleTests(unittest.TestCase):
    home: Path
    fake_bin: Path
    log_dir: Path
    proc: Path
    root: Path
    setUp = test_sysmon.SysmonFixtureTests.setUp
    tearDown = test_sysmon.SysmonFixtureTests.tearDown

    def run_observer(self, script: Path = SCRIPT, samples: int = 2) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(HOME=str(self.home), PATH=f"{self.fake_bin}:/usr/bin:/bin", TZ="UTC",
                   SYSMON_LOG_DIR=str(self.log_dir), SYSMON_PROC_ROOT=str(self.proc),
                   SYSMON_CLOCK_FILE=str(self.root / "clock"), SYSMON_MAX_SAMPLES=str(samples))
        with (self.root / "stdout").open("w+") as out, (self.root / "stderr").open("w+") as err:
            process = subprocess.Popen(["bash", str(script), "--test-only"], env=env,
                                       stdout=out, stderr=err, start_new_session=True)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.fail("observer blocked on CSV output or writer cleanup for 8 seconds")
            finally:
                # The process group is owned by this exact unreaped fixture root.
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(process.args, process.returncode, out.read(), err.read())

    def test_csv_fifo_is_rejected_without_blocking_observer(self) -> None:
        os.mkfifo(self.log_dir / "sysmon_2023-11-14.csv")
        result = self.run_observer()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sysmon CSV", result.stderr)

    def _writer_fault(self, statement: str) -> Path:
        source = self.root / "source"
        source.mkdir()
        script = source / "sysmon.sh"
        script.write_bytes(SCRIPT.read_bytes())
        helper = ROOT / "scripts" / "sysmon_csv_writer.py"
        self.assertTrue(helper.exists(), "observer has no isolated durable CSV writer")
        content = helper.read_text()
        anchor = '        print("READY", flush=True)\n'
        self.assertEqual(content.count(anchor), 1)
        content = content.replace(anchor, anchor + f"        {statement}\n")
        (source / helper.name).write_text(content)
        return script

    def test_full_pipe_stopped_writer_fails_bounded_and_reaps_writer(self) -> None:
        marker = self.root / "writer.pid"
        script = self._writer_fault(
            f"import fcntl; fcntl.fcntl(0, fcntl.F_SETPIPE_SZ, 4096); Path({str(marker)!r}).write_text(str(os.getpid())); os.kill(os.getpid(), signal.SIGSTOP)"
        )
        (self.root / "clock").write_text("".join(
            f"{1700000000000000 + i * 2000000}\n" for i in range(400)
        ))
        started = time.monotonic()
        result = self.run_observer(script, samples=200)
        self.assertLess(time.monotonic() - started, 8)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CSV", result.stderr)
        self.assertTrue(marker.exists())
        self.assertFalse(Path(f"/proc/{int(marker.read_text())}").exists(), "writer survived cleanup")

    def _assert_signal_drains(self, number: int) -> None:
        script = self._writer_fault("time.sleep(0.3)")
        source = script.read_text()
        anchor = '    sample_count=$((sample_count + 1))\n'
        self.assertEqual(source.count(anchor), 1)
        script.write_text(source.replace(anchor, anchor + f"    kill -{number} $$\n"))
        result = self.run_observer(script)
        self.assertEqual(result.returncode, 128 + number, result.stderr)
        rows = test_sysmon.SysmonFixtureTests._rows(self.log_dir / "sysmon_2023-11-14.csv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["boot_id"], "12345678-1234-4abc-8def-1234567890ab")
        self.assertEqual(rows[0]["memory_some_avg10"], "1.23")

    def test_term_drains_complete_queued_boot_pressure_row(self) -> None:
        self._assert_signal_drains(signal.SIGTERM)

    def test_int_drains_complete_queued_boot_pressure_row(self) -> None:
        self._assert_signal_drains(signal.SIGINT)

    def test_shutdown_before_ready_reaps_stopped_writer(self) -> None:
        marker = self.root / "writer.pid"
        script = self._writer_fault("pass")
        helper = script.with_name("sysmon_csv_writer.py")
        source = helper.read_text()
        source = source.replace('        print("READY", flush=True)\n',
            f"        Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "        os.kill(os.getppid(), signal.SIGTERM)\n"
            "        os.kill(os.getpid(), signal.SIGSTOP)\n"
            '        print("READY", flush=True)\n')
        helper.write_text(source)
        result = self.run_observer(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(marker.exists())
        self.assertFalse(Path(f"/proc/{int(marker.read_text())}").exists())

    def test_dead_writer_fails_without_sigpipe_or_false_success(self) -> None:
        script = self._writer_fault("os._exit(42)")
        result = self.run_observer(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.returncode, -signal.SIGPIPE)
        self.assertIn("CSV", result.stderr)


if __name__ == "__main__":
    unittest.main()
