"""Contracts for the unified GB10 systemd readiness probe."""

import os
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


def _run_probe(main_pid: int, container_state: str) -> subprocess.CompletedProcess[str]:
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
            "curl": "#!/bin/sh\nexit 1\n",
        }
        for name, source in commands.items():
            path = fake_bin / name
            path.write_text(source)
            path.chmod(0o755)
        env = os.environ | {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_MAINPID": str(main_pid),
            "FAKE_CONTAINER_STATE": container_state,
        }
        try:
            return subprocess.run(
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
                capture_output=True,
                text=True,
                timeout=1,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError("readiness waited after its MainPID disappeared") from error


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

    def test_vllm_text_units_retain_inspect_evidence_before_cleanup(self) -> None:
        for unit in RETAINED_TEXT_UNITS:
            with self.subTest(unit=unit.name):
                text = unit.read_text()
                self.assertNotIn("docker run --rm", text)
                post = [
                    line for line in text.splitlines() if line.startswith("ExecStopPost=")
                ]
                inspect_at = next(
                    index for index, line in enumerate(post) if "docker inspect" in line
                )
                cleanup_at = next(
                    index for index, line in enumerate(post) if "--cleanup" in line
                )
                self.assertLess(inspect_at, cleanup_at)


if __name__ == "__main__":
    unittest.main()
