from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gb10-swap-guard.sh"
UNIT = ROOT / "systemd" / "gb10-swap-guard.service"


class SwapGuardSourceContractTests(unittest.TestCase):
    def run_guard(
        self, env: dict[str, str], *, timeout: float = 3
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            ["bash", str(SCRIPT)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=3)
            self.fail(
                f"guard exceeded {timeout}s; stdout={stdout!r}; stderr={stderr!r}"
            )
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )

    def run_one_detection_cycle(
        self,
        *,
        mem_available_kib: int,
        swap_used_kib: int = 0,
        skip_diagnostics: bool = True,
        max_cycles: int = 0,
        diagnostic_delay: float = 0,
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
                    + ('sleep "${GB10_TEST_DIAGNOSTIC_DELAY:-0}"\n' if command == "free" else "")
                )
                executable.chmod(0o755)
            tee = fake_bin / "tee"
            tee.write_text(
                "#!/usr/bin/env bash\n"
                'payload="$(/bin/cat)"\n'
                f'printf \'tee %s\\n\' "$payload" >> {calls}\n'
                'printf \'%s\\n\' "$payload"\n'
            )
            tee.chmod(0o755)

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
                "GB10_SWAP_GUARD_ONESHOT": "1" if max_cycles == 0 else "0",
                "GB10_SWAP_GUARD_SKIP_DIAGNOSTICS": "1" if skip_diagnostics else "0",
                "GB10_SWAP_GUARD_WAIT_DIAGNOSTICS": "1" if not skip_diagnostics else "0",
                "GB10_SWAP_GUARD_LOG_DIR": raw_tmp,
                "GB10_SWAP_GUARD_MAX_CYCLES": str(max_cycles),
                "GB10_SWAP_GUARD_TEST_NOW_START": "100",
                "GB10_SWAP_GUARD_TEST_NOW_STEP": "1" if max_cycles else "0",
                "GB10_SWAP_GUARD_INTERVAL": "0" if max_cycles else "1",
                "GB10_TEST_DIAGNOSTIC_DELAY": str(diagnostic_delay),
                "GB10_SWAP_GUARD_TEST_EVENT_LOG": str(calls),
            }
            result = self.run_guard(env)
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
        unit = UNIT.read_text()
        self.assertIn(
            "systemctl --user --no-block stop querit-4b-reranker.service", script
        )
        self.assertIn(
            'timeout --signal=TERM --kill-after="$SYSTEMCTL_KILL_AFTER" "$SYSTEMCTL_STOP_TIMEOUT" systemctl --user --no-block stop querit-4b-reranker.service',
            script,
        )
        self.assertIn(
            'timeout --signal=TERM --kill-after="$DOCKER_KILL_AFTER" "$DOCKER_STOP_TIMEOUT" docker stop --time 2 querit-4b-reranker',
            script,
        )
        self.assertIn("GB10_SWAP_GUARD_SYSTEMCTL_STOP_TIMEOUT=1", unit)
        self.assertIn("GB10_SWAP_GUARD_SYSTEMCTL_KILL_AFTER=1", unit)

    def test_meminfo_detection_hot_path_avoids_python(self) -> None:
        script = SCRIPT.read_text()
        conversion = re.search(
            r"bytes_from_gib\(\) \{(?P<body>.*?)\n\}", script, re.DOTALL
        )
        if conversion is None:
            self.fail("bytes_from_gib function is missing")
        self.assertNotIn("python3", conversion.group("body"))
        match = re.search(r"read_mem_bytes\(\) \{(?P<body>.*?)\n\}", script, re.DOTALL)
        if match is None:
            self.fail("read_mem_bytes function is missing")
        self.assertNotIn("python3", match.group("body"))

    def test_low_memavailable_stops_active_querit_in_one_detection_cycle(self) -> None:
        result, recorded = self.run_one_detection_cycle(mem_available_kib=1023 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("avail=1.0GiB", result.stdout)
        self.assertIn(
            "systemctl --user --no-block stop querit-4b-reranker.service", recorded
        )
        self.assertIn("docker stop --time 2 querit-4b-reranker", recorded)

    def test_exact_memavailable_threshold_does_not_stop(self) -> None:
        result, recorded = self.run_one_detection_cycle(mem_available_kib=1024 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("STOP_RERANKER", result.stdout)
        self.assertNotIn(" stop ", recorded)
        self.assertNotIn("docker stop", recorded)

    def test_healthy_memory_and_swap_do_not_stop(self) -> None:
        result, recorded = self.run_one_detection_cycle(mem_available_kib=8 * 1024 * 1024)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("STOP_RERANKER", result.stdout)
        self.assertNotIn(" stop ", recorded)
        self.assertNotIn("docker stop", recorded)

    def test_logging_write_failures_do_not_exit_guard(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            meminfo = tmp / "meminfo"
            meminfo.write_text(
                "MemTotal: 134217728 kB\nMemAvailable: 8388608 kB\n"
                "SwapTotal: 16777216 kB\nSwapFree: 16777216 kB\n"
            )
            base_env = os.environ | {
                "HOME": raw_tmp,
                "GB10_SWAP_GUARD_MEMINFO_PATH": str(meminfo),
                "GB10_SWAP_GUARD_ONESHOT": "1",
                "GB10_SWAP_GUARD_SKIP_DIAGNOSTICS": "1",
                "GB10_SWAP_GUARD_LOG_DIR": raw_tmp,
            }
            with open("/dev/full", "w") as full:
                stdout_failure = subprocess.run(
                    ["bash", str(SCRIPT)],
                    env=base_env,
                    text=True,
                    stdout=full,
                    stderr=subprocess.PIPE,
                    timeout=3,
                    check=False,
                )
            self.assertEqual(stdout_failure.returncode, 0, stdout_failure.stderr)

            event_failure = subprocess.run(
                ["bash", str(SCRIPT)],
                env=base_env | {"GB10_SWAP_GUARD_TEST_EVENT_LOG": "/dev/full"},
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=3,
                check=False,
            )
            self.assertEqual(event_failure.returncode, 0, event_failure.stderr)

    def test_high_swap_still_stops_reranker(self) -> None:
        result, recorded = self.run_one_detection_cycle(
            mem_available_kib=8 * 1024 * 1024,
            swap_used_kib=13 * 1024 * 1024,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reason=swap>=12GiB", result.stdout)
        self.assertIn(
            "systemctl --user --no-block stop querit-4b-reranker.service", recorded
        )

    def test_simultaneous_memory_and_swap_triggers_are_coalesced(self) -> None:
        result, recorded = self.run_one_detection_cycle(
            mem_available_kib=1023 * 1024,
            swap_used_kib=13 * 1024 * 1024,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mem_avail<1GiB", result.stdout)
        self.assertIn("swap>=12GiB", result.stdout)
        self.assertEqual(
            recorded.count(
                "systemctl --user --no-block stop querit-4b-reranker.service"
            ),
            1,
        )

    def test_emergency_stop_precedes_slow_diagnostics(self) -> None:
        result, recorded = self.run_one_detection_cycle(
            mem_available_kib=1023 * 1024,
            swap_used_kib=8 * 1024 * 1024,
            skip_diagnostics=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = recorded.splitlines()
        querit_systemd = calls.index(
            "systemctl --user --no-block stop querit-4b-reranker.service"
        )
        querit_docker = calls.index("docker stop --time 2 querit-4b-reranker")
        legacy_systemd = calls.index(
            "systemctl --user --no-block stop vllm-qwen3-reranker-8b.service"
        )
        legacy_docker = calls.index("docker stop --time 2 vllm-qwen3-reranker-8b")
        stop_attempt_log = next(
            index
            for index, call in enumerate(calls)
            if "STOP_RERANKER_ATTEMPT" in call
        )
        self.assertLess(querit_systemd, querit_docker)
        self.assertLess(querit_docker, legacy_systemd)
        self.assertLess(legacy_systemd, legacy_docker)
        self.assertLess(legacy_docker, stop_attempt_log)
        for diagnostic in ("free -h", "swapon --show", "vmstat 1 3"):
            self.assertLess(querit_systemd, calls.index(diagnostic))
        attribution_index = next(
            index for index, call in enumerate(calls) if call.startswith("docker ps --format")
        )
        self.assertLess(querit_systemd, attribution_index)
        self.assertLess(
            result.stdout.index("STOP_RERANKER_ATTEMPT"),
            result.stdout.index("sample mem_used="),
        )
        self.assertLess(
            result.stdout.index("STOP_RERANKER_ATTEMPT"),
            result.stdout.index("WARN_SWAP"),
        )

    def test_slow_diagnostics_are_single_instance_across_triggers(self) -> None:
        result, recorded = self.run_one_detection_cycle(
            mem_available_kib=1023 * 1024,
            swap_used_kib=8 * 1024 * 1024,
            skip_diagnostics=False,
            max_cycles=2,
            diagnostic_delay=0.2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(recorded.splitlines().count("free -h"), 1, recorded)

    def test_failed_shedding_retries_at_configured_interval(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            calls = tmp / "calls.log"
            attempts = tmp / "querit-attempts"
            state = tmp / "querit-state"

            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                f'printf \'%s\\n\' "systemctl $*" >> {calls}\n'
                'if [[ "$*" == *" show "* ]]; then\n'
                f'  if [[ -f {state} ]]; then echo "LoadState=loaded"; echo "ActiveState=inactive"; else echo "LoadState=loaded"; echo "ActiveState=active"; fi\n'
                "  exit 0\n"
                "fi\n"
                'if [[ "$*" == *"is-active"* ]]; then\n'
                f'  if [[ -f {state} ]]; then echo inactive; exit 3; fi\n'
                "  echo active; exit 0\n"
                "fi\n"
                'if [[ "$*" == *"stop querit-4b-reranker.service"* ]]; then\n'
                f'  n=0; [[ -f {attempts} ]] && n=$(<{attempts}); n=$((n + 1)); printf \'%s\\n\' "$n" > {attempts}\n'
                f'  if (( n >= 2 )); then : > {state}; exit 0; fi\n'
                "fi\n"
                "exit 1\n"
            )
            systemctl.chmod(0o755)
            for command in ("docker", "free", "swapon", "vmstat"):
                executable = fake_bin / command
                executable.write_text(
                    "#!/usr/bin/env bash\n"
                    f'printf \'%s\\n\' "{command} $*" >> {calls}\n'
                    "exit 1\n"
                )
                executable.chmod(0o755)

            meminfo = tmp / "meminfo"
            meminfo.write_text(
                "MemTotal:       134217728 kB\n"
                "MemAvailable:     1047552 kB\n"
                "SwapTotal:       16777216 kB\n"
                "SwapFree:        16777216 kB\n"
            )
            env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "HOME": raw_tmp,
                "GB10_SWAP_GUARD_MEMINFO_PATH": str(meminfo),
                "GB10_SWAP_GUARD_SKIP_DIAGNOSTICS": "1",
                "GB10_SWAP_GUARD_LOG_DIR": raw_tmp,
                "GB10_SWAP_GUARD_INTERVAL": "0",
                "GB10_SWAP_GUARD_STOP_RETRY_INTERVAL": "5",
                "GB10_SWAP_GUARD_TEST_NOW_START": "100",
                "GB10_SWAP_GUARD_TEST_NOW_STEP": "1",
            }
            before_deadline = self.run_guard(
                env | {"GB10_SWAP_GUARD_MAX_CYCLES": "5"}
            )
            self.assertEqual(before_deadline.returncode, 0, before_deadline.stderr)
            self.assertTrue(attempts.exists(), calls.read_text())
            self.assertEqual(int(attempts.read_text()), 1, calls.read_text())

            attempts.unlink()
            state.unlink(missing_ok=True)
            at_deadline = self.run_guard(
                env | {"GB10_SWAP_GUARD_MAX_CYCLES": "6"}
            )
            self.assertEqual(at_deadline.returncode, 0, at_deadline.stderr)
            self.assertEqual(int(attempts.read_text()), 2, calls.read_text())

    def test_missing_or_unqueryable_querit_unit_does_not_false_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            calls = tmp / "calls.log"
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                f'printf \'%s\\n\' "systemctl $*" >> {calls}\n'
                'if [[ "$*" == *" show "* ]]; then\n'
                '  if [[ "${GB10_TEST_SHOW_MODE:-}" == "failure" ]]; then exit 1; fi\n'
                '  echo "LoadState=not-found"; echo "ActiveState=inactive"; exit 0\n'
                "fi\n"
                'if [[ "$*" == *"is-active"* ]]; then echo inactive; exit 3; fi\n'
                "exit 1\n"
            )
            systemctl.chmod(0o755)
            for command in ("docker", "free", "swapon", "vmstat"):
                executable = fake_bin / command
                executable.write_text("#!/usr/bin/env bash\nexit 1\n")
                executable.chmod(0o755)
            meminfo = tmp / "meminfo"
            meminfo.write_text(
                "MemTotal: 134217728 kB\nMemAvailable: 1047552 kB\n"
                "SwapTotal: 16777216 kB\nSwapFree: 16777216 kB\n"
            )
            env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "HOME": raw_tmp,
                "GB10_SWAP_GUARD_MEMINFO_PATH": str(meminfo),
                "GB10_SWAP_GUARD_SKIP_DIAGNOSTICS": "1",
                "GB10_SWAP_GUARD_LOG_DIR": raw_tmp,
                "GB10_SWAP_GUARD_INTERVAL": "0",
                "GB10_SWAP_GUARD_STOP_RETRY_INTERVAL": "5",
                "GB10_SWAP_GUARD_MAX_CYCLES": "7",
                "GB10_SWAP_GUARD_TEST_NOW_START": "100",
                "GB10_SWAP_GUARD_TEST_NOW_STEP": "1",
            }
            for show_mode in ("not-found", "failure"):
                calls.write_text("")
                result = self.run_guard(env | {"GB10_TEST_SHOW_MODE": show_mode})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    calls.read_text().count(
                        "systemctl --user --no-block stop querit-4b-reranker.service"
                    ),
                    2,
                    show_mode,
                )
                self.assertNotIn("STOP_RERANKER_CONFIRMED", result.stdout)

    def test_term_ignoring_docker_fallback_is_hard_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            docker_pids = tmp / "docker-pids"
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "$*" == *" show "* ]]; then echo "LoadState=loaded"; echo "ActiveState=active"; exit 0; fi\n'
                'if [[ "$*" == *"is-active"* ]]; then echo active; exit 0; fi\n'
                "exit 1\n"
            )
            systemctl.chmod(0o755)
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                'printf \'%s\\n\' "$$" >> "$GB10_TEST_DOCKER_PIDS"\n'
                "trap '' TERM\nwhile true; do :; done\n"
            )
            docker.chmod(0o755)
            for command in ("free", "swapon", "vmstat"):
                executable = fake_bin / command
                executable.write_text("#!/usr/bin/env bash\nexit 0\n")
                executable.chmod(0o755)
            meminfo = tmp / "meminfo"
            meminfo.write_text(
                "MemTotal: 134217728 kB\nMemAvailable: 1047552 kB\n"
                "SwapTotal: 16777216 kB\nSwapFree: 16777216 kB\n"
            )
            env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "HOME": raw_tmp,
                "GB10_SWAP_GUARD_MEMINFO_PATH": str(meminfo),
                "GB10_SWAP_GUARD_ONESHOT": "1",
                "GB10_SWAP_GUARD_SKIP_DIAGNOSTICS": "1",
                "GB10_SWAP_GUARD_LOG_DIR": raw_tmp,
                "GB10_SWAP_GUARD_DOCKER_STOP_TIMEOUT": "0.1",
                "GB10_SWAP_GUARD_DOCKER_KILL_AFTER": "0.1",
                "GB10_TEST_DOCKER_PIDS": str(docker_pids),
            }
            result = self.run_guard(env, timeout=1.5)
            self.assertEqual(result.returncode, 0, result.stderr)
            pids = [int(value) for value in docker_pids.read_text().splitlines()]
            self.assertEqual(len(pids), 2)
            for pid in pids:
                self.assertFalse(
                    Path(f"/proc/{pid}").exists(), f"leaked fake docker pid {pid}"
                )


if __name__ == "__main__":
    unittest.main()
