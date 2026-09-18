"""Retain last-good Docker inspect evidence across empty or failed queries."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import time
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


INSTALLER = ROOT / "scripts" / "gb10_install_retain_container_inspect.sh"
INSTALL_COMMAND = "bash scripts/gb10_install_retain_container_inspect.sh"
HELPER_DEST = "/home/obj/.local/bin/gb10_retain_container_inspect.sh"
CONTAINER = "vllm-aeon-ultimate-uncensored-nvfp4"
RETAIN_DEADLINE_SEC = 10
RETAIN_KILL_AFTER_SEC = 2
MAX_INSPECT_BYTES = 262144
RUNBOOK = ROOT / "docs" / "deployment" / "text-inspect-retention.md"
README = ROOT / "README.md"
NESTED_AGENTS = ROOT / "docs" / "deployment" / "AGENTS.md"
ULTIMATE_UNIT_SOURCE = (
    "profile/aeon-ultimate-uncensored-nvfp4/"
    "vllm-aeon-ultimate-uncensored-nvfp4.service"
)
UNIT_SOURCES = {
    "vllm-aeon-ultimate-uncensored-nvfp4.service": ULTIMATE_UNIT_SOURCE,
    "vllm-aeon-27b-dflash.service": (
        "profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service"
    ),
    "vllm-aeon-qwen38-dflash.service": (
        "profile/qwen3.8-27b-nvfp4-vllm/vllm-aeon-qwen38-dflash.service"
    ),
}
FENCED_BASH = re.compile(r"```(?:bash)?\n(.*?)```", re.S)


def _exited_payload(**overrides: object) -> str:
    body: dict[str, object] = {
        "Id": CID,
        "Name": f"/{CONTAINER}",
        "State": {
            "Status": "exited",
            "Running": False,
            "ExitCode": 137,
            "OOMKilled": False,
            "FinishedAt": "2026-09-17T18:59:23.000Z",
        },
    }
    body.update(overrides)
    return json.dumps([body])


def _padded_exited_payload(size: int) -> str:
    body = json.loads(_exited_payload())[0]
    body["Config"] = {"Labels": {"pad": ""}}
    pad = size - len(json.dumps([body]))
    if pad < 0:
        raise ValueError(f"payload already larger than {size}")
    body["Config"]["Labels"]["pad"] = "x" * pad
    raw = json.dumps([body])
    if len(raw) != size:
        raise AssertionError(f"padded payload is {len(raw)} bytes, not {size}")
    return raw


def _fenced_bash(text: str) -> list[str]:
    return FENCED_BASH.findall(text)


def _run(
    env: dict[str, str],
    cidfile: Path,
    output: Path,
    container: str = CONTAINER,
    timeout: int = 5,
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
        timeout=timeout,
        check=False,
    )


def _assert_kept_last_good(test: unittest.TestCase, result: subprocess.CompletedProcess[str], output: Path) -> None:
    test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    test.assertEqual(output.read_text(), LAST_GOOD + "\n")
    test.assertNotIn("retained", result.stdout.lower())
    leftover = list(output.parent.glob(f"{output.name}.tmp.*"))
    test.assertEqual(leftover, [])


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
        updated = _exited_payload()
        _fake_docker(self.bin_dir, updated + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), updated + "\n")
        leftover = list(self.identity.glob(f"{self.output.name}.tmp.*"))
        self.assertEqual(leftover, [])

    def test_inspects_full_cid_not_container_name(self) -> None:
        seen = self.bin_dir / "seen.args"
        docker = self.bin_dir / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" > {json.dumps(str(seen))}\n"
            f"printf '%s\\n' {json.dumps(_exited_payload())}\n"
            "exit 0\n"
        )
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = seen.read_text()
        self.assertIn(CID, args)
        self.assertNotIn("vllm-aeon-ultimate-uncensored-nvfp4", args)
        self.assertIn("--type container", args)

    def test_text_units_ignore_retain_failures_before_cleanup(self) -> None:
        helper = HELPER_DEST
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
                    if line.startswith(("ExecStop=", "ExecStop-=", "ExecStopPost=", "ExecStopPost-="))
                ]
                retain = [line for line in stop if helper in line]
                cleanup = [line for line in stop if "--cleanup" in line]
                self.assertEqual(len(retain), 2)
                self.assertEqual(len(cleanup), 2)
                for line in retain:
                    self.assertTrue(
                        line.startswith("ExecStop=-") or line.startswith("ExecStopPost=-"),
                        line,
                    )
                    self.assertIn(f"--container {container}", line)
                    self.assertIn(f"--cidfile {cidfile}", line)
                    self.assertIn(f"--output {output}", line)
                    self.assertIn(
                        f"/usr/bin/timeout --signal=TERM --kill-after={RETAIN_KILL_AFTER_SEC} {RETAIN_DEADLINE_SEC} ",
                        line,
                    )
                    self.assertLess(RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC, 60)
                retain_at = [index for index, line in enumerate(stop) if helper in line]
                cleanup_at = [index for index, line in enumerate(stop) if "--cleanup" in line]
                self.assertLess(retain_at[0], cleanup_at[0])
                self.assertLess(retain_at[1], cleanup_at[1])

    def test_running_inspect_does_not_overwrite_last_good_inspect(self) -> None:
        payload = json.loads(_exited_payload())
        payload[0]["State"]["Running"] = True
        payload[0]["State"]["Status"] = "running"
        _fake_docker(self.bin_dir, json.dumps(payload) + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, result, self.output)

    def test_missing_name_does_not_overwrite_last_good_inspect(self) -> None:
        payload = json.loads(_exited_payload())
        del payload[0]["Name"]
        _fake_docker(self.bin_dir, json.dumps(payload) + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, result, self.output)

    def test_id_prefix_and_wrong_types_do_not_overwrite_last_good_inspect(self) -> None:
        payload = json.loads(_exited_payload())
        payload[0]["Id"] = CID + "00"
        payload[0]["State"]["ExitCode"] = None
        payload[0]["State"]["OOMKilled"] = "false"
        _fake_docker(self.bin_dir, json.dumps(payload) + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, result, self.output)

    def test_directory_symlink_output_does_not_claim_success(self) -> None:
        target = self.identity / "inspect-dir"
        target.mkdir()
        self.output.unlink()
        self.output.symlink_to(target)
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(list(target.iterdir()), [])
        leftover = list(self.identity.glob(f"{self.output.name}.tmp.*"))
        self.assertEqual(leftover, [])
        self.assertNotIn("retained", result.stdout.lower())

    def test_inspect_timeout_keeps_last_good_and_exits_zero(self) -> None:
        docker = self.bin_dir / "docker"
        docker.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        try:
            result = _run(self.env, self.cidfile, self.output, timeout=14)
        except subprocess.TimeoutExpired:
            self.fail("inspect hung past the helper deadline")
        _assert_kept_last_good(self, result, self.output)
        self.assertIn("skip", result.stderr.lower())

    def test_outer_publication_stall_maps_term_timeout_to_skip(self) -> None:
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        mv = self.bin_dir / "mv"
        mv.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
        mv.chmod(mv.stat().st_mode | stat.S_IXUSR)
        started = time.monotonic()
        try:
            result = _run(self.env, self.cidfile, self.output, timeout=16)
        except subprocess.TimeoutExpired:
            self.fail("outer retain deadline did not terminate")
        elapsed = time.monotonic() - started
        _assert_kept_last_good(self, result, self.output)
        self.assertIn("timed out", result.stderr.lower())
        self.assertGreaterEqual(elapsed, RETAIN_DEADLINE_SEC - 1)
        self.assertLess(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC + 2)

    def test_outer_timeout_kill_escalation_maps_to_skip(self) -> None:
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        mv = self.bin_dir / "mv"
        mv.write_text("#!/bin/sh\ntrap '' TERM\nexec /bin/sleep 30\n")
        mv.chmod(mv.stat().st_mode | stat.S_IXUSR)
        bash_env = Path(self.temporary.name) / "ignore-term.bash"
        bash_env.write_text("trap '' TERM\n")
        self.env["BASH_ENV"] = str(bash_env)
        started = time.monotonic()
        try:
            result = _run(self.env, self.cidfile, self.output, timeout=18)
        except subprocess.TimeoutExpired:
            self.fail("outer retain kill-after did not terminate")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")
        self.assertNotIn("retained", result.stdout.lower())
        self.assertIn("timed out", result.stderr.lower())
        leftover = list(self.identity.glob(f"{self.output.name}.tmp.*"))
        self.assertEqual(len(leftover), 1)
        self.assertGreaterEqual(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC - 1)
        self.assertLess(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC + 3)

    def test_chmod_failure_keeps_last_good_and_exits_zero(self) -> None:
        chmod = self.bin_dir / "chmod"
        chmod.write_text("#!/bin/sh\nexit 1\n")
        chmod.chmod(chmod.stat().st_mode | stat.S_IXUSR)
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, result, self.output)

    def test_nul_cidfile_does_not_overwrite_last_good_inspect(self) -> None:
        self.cidfile.write_bytes(f"{CID}\0\n".encode())
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, result, self.output)

    def test_cidfile_without_newline_does_not_overwrite_last_good_inspect(self) -> None:
        self.cidfile.write_bytes(CID.encode())
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, result, self.output)

    def test_cidfile_extra_byte_and_second_line_do_not_overwrite_last_good_inspect(
        self,
    ) -> None:
        cases = (f"{CID}x\n".encode(), f"{CID}\nextra\n".encode(), (b"a" * 4096))
        for raw in cases:
            with self.subTest(raw=raw[:16]):
                self.cidfile.write_bytes(raw)
                _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
                result = _run(self.env, self.cidfile, self.output)
                _assert_kept_last_good(self, result, self.output)

    def test_cidfile_fifo_does_not_hang_or_overwrite_last_good_inspect(self) -> None:
        self.cidfile.unlink()
        os.mkfifo(self.cidfile, 0o600)
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        started = time.monotonic()
        result = _run(self.env, self.cidfile, self.output, timeout=14)
        elapsed = time.monotonic() - started
        _assert_kept_last_good(self, result, self.output)
        self.assertLess(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC + 2)

    def test_created_and_dead_status_do_not_overwrite_last_good_inspect(self) -> None:
        for status in ("created", "dead", "restarting"):
            with self.subTest(status=status):
                payload = json.loads(_exited_payload())
                payload[0]["State"]["Status"] = status
                payload[0]["State"]["Running"] = False
                _fake_docker(self.bin_dir, json.dumps(payload) + "\n", 0)
                result = _run(self.env, self.cidfile, self.output)
                _assert_kept_last_good(self, result, self.output)

    def test_non_string_status_does_not_overwrite_last_good_inspect(self) -> None:
        payload = json.loads(_exited_payload())
        payload[0]["State"]["Status"] = None
        _fake_docker(self.bin_dir, json.dumps(payload) + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, result, self.output)

    def test_inspect_json_at_cap_is_published(self) -> None:
        raw = _padded_exited_payload(MAX_INSPECT_BYTES)
        _fake_docker(self.bin_dir, raw, 0)
        result = _run(self.env, self.cidfile, self.output, timeout=14)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), raw)
        self.assertIn("retained", result.stdout.lower())

    def test_inspect_json_over_cap_keeps_last_good_within_deadline(self) -> None:
        raw = _padded_exited_payload(MAX_INSPECT_BYTES + 1)
        _fake_docker(self.bin_dir, raw, 0)
        started = time.monotonic()
        result = _run(self.env, self.cidfile, self.output, timeout=14)
        elapsed = time.monotonic() - started
        _assert_kept_last_good(self, result, self.output)
        self.assertLess(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC + 2)

    def test_endless_inspect_producer_keeps_last_good_within_deadline(self) -> None:
        docker = self.bin_dir / "docker"
        docker.write_text("#!/bin/sh\nwhile true; do printf 'x'; done\n")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        started = time.monotonic()
        result = _run(self.env, self.cidfile, self.output, timeout=14)
        elapsed = time.monotonic() - started
        _assert_kept_last_good(self, result, self.output)
        leftover = list(self.identity.glob(f"{self.output.name}.tmp.*"))
        self.assertEqual(leftover, [])
        self.assertLess(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC + 2)

    def test_post_inspect_fifo_keeps_last_good_within_deadline(self) -> None:
        docker = self.bin_dir / "docker"
        pattern = str(self.identity / f"{self.output.name}.tmp.*")
        docker.write_text(
            "#!/bin/sh\n"
            f"printf '%s' {json.dumps(_exited_payload())}\n"
            f"for f in {pattern}; do\n"
            "  [ -e \"$f\" ] || continue\n"
            "  /bin/rm -f -- \"$f\"\n"
            "  /usr/bin/mkfifo -- \"$f\"\n"
            "done\n"
            "exit 0\n"
        )
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        started = time.monotonic()
        result = _run(self.env, self.cidfile, self.output, timeout=14)
        elapsed = time.monotonic() - started
        _assert_kept_last_good(self, result, self.output)
        leftover = list(self.identity.glob(f"{self.output.name}.tmp.*"))
        self.assertEqual(leftover, [])
        self.assertLess(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC + 2)

    def test_deploy_guides_install_helper_before_every_text_unit(self) -> None:
        self.assertTrue(INSTALLER.is_file())
        installer = INSTALLER.read_text()
        self.assertIn("install -m 0755", installer)
        self.assertIn("scripts/gb10_retain_container_inspect.sh", installer)
        self.assertIn(HELPER_DEST, installer)
        agents = NESTED_AGENTS.read_text()
        self.assertNotIn("gb10_retain_container_inspect.sh", agents)
        readme = README.read_text()
        self.assertIn(INSTALL_COMMAND, readme)
        for source in UNIT_SOURCES.values():
            self.assertIn(source, readme, f"README missing {source}")
        helper_at = readme.find(INSTALL_COMMAND)
        for source in UNIT_SOURCES.values():
            self.assertLess(helper_at, readme.find(source), source)

    def test_runbook_helper_only_stage_has_no_executable_unit_write(self) -> None:
        text = RUNBOOK.read_text()
        blocks = _fenced_bash(text)
        self.assertGreaterEqual(len(blocks), 1)
        helper_blocks = [block for block in blocks if INSTALL_COMMAND in block]
        self.assertEqual(len(helper_blocks), 1)
        self.assertEqual(helper_blocks[0].strip(), INSTALL_COMMAND)
        for block in blocks:
            self.assertNotIn("install -m 0644", block)
            self.assertNotIn("daemon-reload", block)
            self.assertNotIn("systemctl", block)
        self.assertIn("unit_hooks_effective=false", text)
        self.assertIn("--no-enable-prefix-caching", text)
        self.assertLess(
            text.find("--no-enable-prefix-caching"),
            text.find(ULTIMATE_UNIT_SOURCE),
        )
        self.assertNotIn("Then install the unit that will own", text)

    def test_runbook_unit_hook_stage_requires_reload_readback_and_apc_gate(self) -> None:
        text = RUNBOOK.read_text()
        apc_at = text.find("--no-enable-prefix-caching")
        self.assertNotEqual(apc_at, -1)
        for name, source in UNIT_SOURCES.items():
            with self.subTest(unit=name):
                source_at = text.find(source)
                self.assertNotEqual(source_at, -1, source)
                self.assertGreater(source_at, apc_at, source)
                self.assertNotIn(f"install -m 0644 {source}", text)
        self.assertIn("daemon-reload", text)
        self.assertIn("systemctl --user show", text)
        self.assertIn("ExecStop", text)
        self.assertIn("ExecStopPost", text)
        self.assertIn("Do not copy this block until a reviewed APC-align commit", text)


if __name__ == "__main__":
    unittest.main()
