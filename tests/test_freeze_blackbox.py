#!/usr/bin/env python3
"""Contracts for the user-level freeze black box."""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "freeze-blackbox.py"
UNIT = ROOT / "profile" / "freeze-blackbox" / "freeze-blackbox.service"


class FreezeBlackboxContractTests(unittest.TestCase):
    def test_unit_does_not_wait_for_default_target(self) -> None:
        active = [
            line.strip()
            for line in UNIT.read_text().splitlines()
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
        self.assertIn("basic.target", after)
        self.assertNotIn("systemctl", SCRIPT.read_text())

    def test_slow_sampler_timeout_does_not_block_heartbeat(self) -> None:
        tmp_root = Path.home() / "tmp"
        tmp_root.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
            home = Path(tmp)
            log = home / "log" / "freeze-blackbox"
            hang = home / "bin" / "nvidia-smi"
            hang.parent.mkdir()
            hang.write_text("#!/bin/sh\nexec sleep 30\n")
            hang.chmod(0o755)
            env = os.environ.copy()
            env["HOME"] = str(home)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--test-only",
                    str(log),
                    "0",
                    "0.4",
                    str(home / "bin" / "nvidia-smi"),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 4
                rows = 0
                while time.monotonic() < deadline:
                    csvs = list(log.glob("blackbox_*.csv"))
                    if csvs:
                        rows = len(csvs[0].read_text().splitlines())
                        if rows >= 3:
                            break
                    time.sleep(0.2)
                self.assertGreaterEqual(rows, 3)
                children = subprocess.run(
                    ["ps", "-o", "pid=", "--ppid", str(proc.pid)],
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout.split()
                self.assertLessEqual(len(children), 1)
            finally:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                if proc.stderr is not None:
                    proc.stderr.close()

    def test_parser_keeps_blank_workload_blank(self) -> None:
        text = (
            "# HELP vllm:num_requests_running\n"
            "vllm:num_requests_running{engine=\"0\"} 2\n"
            "vllm:num_requests_waiting{engine=\"0\"} 3\n"
            "vllm:kv_cache_usage_perc{engine=\"0\"} 0.5\n"
            "llm_guard_proxy_generation_active 4\n"
            "llm_guard_proxy_generation_queued 5\n"
        )
        spec = importlib.util.spec_from_file_location("freeze_blackbox", SCRIPT)
        if spec is None or spec.loader is None:
            self.fail("freeze-blackbox module missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module._num(text, "vllm:num_requests_running"), "2")
        self.assertEqual(module._num(text, "vllm:num_requests_waiting"), "3")
        self.assertEqual(module._num(text, "vllm:prompt_tokens_total"), "")
        self.assertEqual(
            module._max_metric(text, ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")),
            "0.5",
        )
        self.assertEqual(module._num(text, "llm_guard_proxy_generation_active"), "4")
        self.assertEqual(module._num(text, "llm_guard_proxy_generation_queued"), "5")
        self.assertEqual(module._num("not prometheus", "vllm:num_requests_running"), "")


if __name__ == "__main__":
    unittest.main()
