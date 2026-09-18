"""Retain last-good Docker inspect evidence across empty or failed queries."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "gb10_retain_container_inspect.sh"
CID = "7045598dad834ebdea2525ca9884c4d509754fcfc18c217527abea77d4f30d4e"
LAST_GOOD = json.dumps(
    [
        {
            "Id": CID,
            "State": {
                "Status": "exited",
                "ExitCode": 137,
                "OOMKilled": False,
                "FinishedAt": "2026-09-17T17:52:24.655Z",
            },
            "HostConfig": {"Memory": 77309411328, "AutoRemove": False},
        }
    ]
)
UNITS = (
    (
        ROOT
        / "profile/aeon-ultimate-uncensored-nvfp4/vllm-aeon-ultimate-uncensored-nvfp4.service",
        "vllm-aeon-ultimate-uncensored-nvfp4",
        "%t/gb10-memory-guardian/aeon-text.cid",
        "%t/gb10-memory-guardian/last-aeon-ultimate-uncensored-nvfp4-inspect.json",
    ),
    (
        ROOT / "profile/qwen3.8-27b-nvfp4-vllm/vllm-aeon-qwen38-dflash.service",
        "vllm-aeon-qwen38-dflash",
        "%t/gb10-memory-guardian/aeon-qwen38-text.cid",
        "%t/gb10-memory-guardian/last-text-inspect.json",
    ),
    (
        ROOT / "profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service",
        "vllm-aeon-27b-dflash",
        "%t/gb10-memory-guardian/aeon-text.cid",
        "%t/gb10-memory-guardian/last-aeon-27b-dflash-inspect.json",
    ),
)


def _fake_docker(bin_dir: Path, stdout: str, returncode: int) -> None:
    payload = bin_dir / "docker.stdout"
    payload.write_bytes(stdout.encode())
    path = bin_dir / "docker"
    path.write_text(
        "#!/bin/sh\n"
        f"cat -- {json.dumps(str(payload))}\n"
        f"exit {returncode}\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    env: dict[str, str],
    cidfile: Path,
    output: Path,
    container: str = "vllm-aeon-ultimate-uncensored-nvfp4",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(HELPER),
            "--container",
            container,
            "--cidfile",
            str(cidfile),
            "--output",
            str(output),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


class RetainContainerInspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.identity = root / "gb10-memory-guardian"
        self.identity.mkdir(mode=0o700)
        self.cidfile = self.identity / "aeon-text.cid"
        self.cidfile.write_text(f"{CID}\n")
        os.chmod(self.cidfile, 0o600)
        self.output = self.identity / "last-aeon-ultimate-uncensored-nvfp4-inspect.json"
        self.output.write_text(LAST_GOOD + "\n")
        os.chmod(self.output, 0o600)
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin_dir}:/usr/bin:/bin"
        self.env["HOME"] = str(root)
        self.env["DOCKER_HOST"] = "unix:///run/user/1001/docker.sock"

    def test_empty_array_does_not_overwrite_last_good_inspect(self) -> None:
        _fake_docker(self.bin_dir, "[]\n", 1)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")
        self.assertIn("skip", result.stderr.lower() + result.stdout.lower())

    def test_failed_name_query_does_not_overwrite_last_good_inspect(self) -> None:
        _fake_docker(self.bin_dir, "[]\nError: No such object\n", 1)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")

    def test_mismatched_generation_does_not_overwrite_last_good_inspect(self) -> None:
        other = "c34c55668953ae0269f3bd12e7e74bcd71ba62e0fa52fa17f0fc11adea2744c6"
        payload = json.dumps([{"Id": other, "State": {"ExitCode": 0, "OOMKilled": False}}])
        _fake_docker(self.bin_dir, payload + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")

    def test_missing_cidfile_does_not_overwrite_last_good_inspect(self) -> None:
        self.cidfile.unlink()
        _fake_docker(self.bin_dir, LAST_GOOD + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")

    def test_matching_cid_inspect_replaces_last_good_atomically(self) -> None:
        updated = json.dumps(
            [
                {
                    "Id": CID,
                    "State": {
                        "Status": "exited",
                        "ExitCode": 137,
                        "OOMKilled": False,
                        "FinishedAt": "2026-09-17T18:59:23.000Z",
                    },
                }
            ]
        )
        _fake_docker(self.bin_dir, updated + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), updated + "\n")
        leftover = list(self.identity.glob(".last-aeon-ultimate-uncensored-nvfp4-inspect.json.tmp.*"))
        self.assertEqual(leftover, [])

    def test_inspects_full_cid_not_container_name(self) -> None:
        seen = self.bin_dir / "seen.args"
        docker = self.bin_dir / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" > {json.dumps(str(seen))}\n"
            f"printf '%s\\n' {json.dumps(LAST_GOOD)}\n"
            "exit 0\n"
        )
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = seen.read_text()
        self.assertIn(CID, args)
        self.assertNotIn("vllm-aeon-ultimate-uncensored-nvfp4", args)
        self.assertIn("--type container", args)

    def test_text_units_retain_inspect_before_any_cleanup_and_install_helper(self) -> None:
        helper = "/home/obj/.local/bin/gb10_retain_container_inspect.sh"
        for unit, container, cidfile, output in UNITS:
            with self.subTest(unit=unit.name):
                text = unit.read_text()
                self.assertNotIn("docker run --rm", text)
                self.assertNotIn("|| true", text)
                self.assertNotIn("docker inspect ", text)
                self.assertIn(helper, text)
                stop = [
                    line
                    for line in text.splitlines()
                    if line.startswith("ExecStop=") or line.startswith("ExecStopPost=")
                ]
                retain_at = [
                    index for index, line in enumerate(stop) if helper in line
                ]
                cleanup_at = [
                    index for index, line in enumerate(stop) if "--cleanup" in line
                ]
                self.assertEqual(len(retain_at), 2)
                self.assertEqual(len(cleanup_at), 2)
                self.assertLess(retain_at[0], cleanup_at[0])
                self.assertLess(retain_at[1], cleanup_at[1])
                retain = next(line for line in stop if helper in line)
                self.assertIn(f"--container {container}", retain)
                self.assertIn(f"--cidfile {cidfile}", retain)
                self.assertIn(f"--output {output}", retain)
        guide = (ROOT / "docs" / "deployment" / "AGENTS.md").read_text()
        self.assertIn(
            "install -m 0755 scripts/gb10_retain_container_inspect.sh "
            "/home/obj/.local/bin/gb10_retain_container_inspect.sh",
            guide,
        )


if __name__ == "__main__":
    unittest.main()
