"""Contracts for the unified GB10 systemd readiness probe."""

import os
import signal
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
READY = ROOT / "scripts" / "gb10_service_ready.sh"
READINESS_UNITS = {
    ROOT / "profile/aeon-ultimate-uncensored-nvfp4/vllm-aeon-ultimate-uncensored-nvfp4.service": "vllm-aeon-ultimate-uncensored-nvfp4",
    ROOT / "profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service": "vllm-aeon-27b-dflash",
    ROOT / "profile/qwen3.8-27b-nvfp4-sglang/sglang-qwen38-27b.service": "qwen3.8-27b-sglang",
    ROOT / "profile/qwen3.8-27b-nvfp4-vllm/vllm-aeon-qwen38-dflash.service": "vllm-aeon-qwen38-dflash",
}
RETAINED_TEXT_UNITS = (
    ROOT / "profile/aeon-ultimate-uncensored-nvfp4/vllm-aeon-ultimate-uncensored-nvfp4.service",
    ROOT / "profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service",
    ROOT / "profile/qwen3.8-27b-nvfp4-vllm/vllm-aeon-qwen38-dflash.service",
)


def _run_probe(
    main_pid: int, container_state: str, blocked_command: str | None = None
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as raw_tmp:
        fake_bin = Path(raw_tmp)
        commands = {
            "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$FAKE_MAINPID\"\n",
            "docker": (
                "#!/bin/sh\n"
                "if [ \"$FAKE_CONTAINER_STATE\" = missing ]; then\n"
                "  echo 'No such object: test-container' >&2\n"
                "  exit 1\n"
                "fi\n"
                "printf '%s\\n' \"running=$FAKE_CONTAINER_STATE pid=0 "
                "oom_killed=false exit_code=137 error=none started_at=earlier "
                "finished_at=now\"\n"
            ),
            "curl": "#!/bin/sh\nprintf '%s\\n' '{\"choices\":[{\"message\":{\"content\":\"OK\"}}]}'\n",
        }
        if blocked_command:
            commands[blocked_command] = "#!/bin/sh\nexec /usr/bin/sleep 60\n"
        for name, source in commands.items():
            path = fake_bin / name
            path.write_text(source)
            path.chmod(0o755)
        env = os.environ | {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_MAINPID": str(main_pid),
            "FAKE_CONTAINER_STATE": container_state,
        }
        process = subprocess.Popen(
            [
                "bash",
                str(READY),
                "chat",
                "http://127.0.0.1:9",
                "test-model",
                "--deadline",
                "60",
                "--unit",
                "test.service",
                "--container",
                "test-container",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=8 if blocked_command else 1)
            return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as error:
            raise AssertionError("readiness blocked on owner inspection or waited after owner exit") from error
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


class TestGb10ServiceReadyChatProbe(unittest.TestCase):
    def test_chat_probe_accepts_reasoning_without_thinking(self) -> None:
        text = READY.read_text(encoding="utf-8")
        self.assertIn("enable_thinking", text)
        self.assertIn("reasoning_content", text)
        self.assertIn("msg.get('content')", text)

    def test_disappeared_main_pid_fails_fast_with_container_evidence(self) -> None:
        result = _run_probe(0, "false")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("MainPID=0", result.stdout)
        self.assertIn("exit_code=137", result.stdout)

    def test_stopped_or_missing_owned_container_fails_fast(self) -> None:
        for state, evidence in (
            ("false", "running=false"),
            ("missing", "inspect_failed=No such object: test-container"),
        ):
            with self.subTest(state=state):
                result = _run_probe(os.getpid(), state)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(evidence, result.stdout)

    def test_hung_owner_commands_fail_fast(self) -> None:
        for command in ("systemctl", "docker"):
            with self.subTest(command=command):
                result = _run_probe(os.getpid(), "true", blocked_command=command)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("OWNER EXITED", result.stdout)
                self.assertNotIn("SERVICE_READY", result.stdout)

    def test_live_owner_and_successful_probe_are_ready(self) -> None:
        result = _run_probe(os.getpid(), "true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SERVICE_READY", result.stdout)

    def test_every_readiness_caller_identifies_its_systemd_and_container_owner(self) -> None:
        for unit, container in READINESS_UNITS.items():
            with self.subTest(unit=unit.name):
                readiness = [
                    line
                    for line in unit.read_text().splitlines()
                    if line.startswith("ExecStartPost=")
                    and "gb10_service_ready.sh" in line
                ]
                self.assertEqual(len(readiness), 1)
                argv = shlex.split(readiness[0].split("=", 1)[1])
                helper_at = next(
                    index
                    for index, token in enumerate(argv)
                    if token.endswith("/gb10_service_ready.sh")
                )
                self.assertIn(
                    ["--unit", "%n", "--container", container],
                    [argv[index : index + 4] for index in range(helper_at + 1, len(argv))],
                )

    def test_vllm_text_units_retain_inside_generation_bound_cleanup(self) -> None:
        helper = "/home/obj/.local/bin/gb10_retain_container_inspect.sh"
        for unit in RETAINED_TEXT_UNITS:
            with self.subTest(unit=unit.name):
                text = unit.read_text()
                self.assertNotIn("docker run --rm", text)
                self.assertNotIn("docker inspect ", text)
                lines = text.splitlines()
                live = [shlex.split(line.split("=", 1)[1]) for line in lines
                        if line.startswith("ExecStartPost=") and helper in line]
                self.assertEqual(len(live), 1)
                self.assertIn("--snapshot-live", live[0])
                output = live[0][live[0].index("--output") + 1]
                cidfile = live[0][live[0].index("--cidfile") + 1]
                for directive in ("ExecStartPre=", "ExecStop=", "ExecStopPost="):
                    commands = [shlex.split(line.split("=", 1)[1]) for line in lines
                                if line.startswith(directive)]
                    self.assertFalse(any(helper in argv for argv in commands))
                    cleanup = [argv for argv in commands if "--cleanup" in argv]
                    self.assertEqual(len(cleanup), 1)
                    argv = cleanup[0]
                    self.assertEqual(argv.count("--retain-inspect-output"), 1)
                    self.assertEqual(argv[argv.index("--retain-inspect-output") + 1], output)
                    self.assertEqual(argv[argv.index("--cidfile") + 1], cidfile)
                    self.assertIn("/home/obj/.local/bin/gb10_verify_vllm_no_swap.sh", argv)


if __name__ == "__main__":
    unittest.main()
