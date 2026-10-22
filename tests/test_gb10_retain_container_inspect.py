"""Retain last-good Docker inspect evidence across empty or failed queries."""

from __future__ import annotations

import json
import multiprocessing
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
        "/home/obj/.local/state/gb10-vllm-cids/vllm-aeon-ultimate-uncensored-nvfp4.cid",
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
            "StartedAt": "2026-09-17T17:00:00.000Z",
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

    def test_live_snapshot_survives_destroyed_scope_as_last_known_not_terminal(self) -> None:
        # Execute the source archive boundary; remap only kernel paths into an
        # owned filesystem fixture, never remove or populate a real cgroup.
        import shutil
        import sys
        from unittest.mock import patch

        source = HELPER.read_text()
        block = source.split('<<\'PY\' || skip "inspect validation or durable archive failed; keeping last inspect"\n', 1)[1].split('\nPY\n', 1)[0]
        root = Path(self.temporary.name)
        scope = f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/docker-{CID}.scope"
        owned_scope = root / "kernel" / scope.lstrip("/")
        owned_scope.mkdir(parents=True)
        raw = "low 0\nhigh 0\nmax 0\noom 2\noom_kill 1\noom_group_kill 0\n"
        (owned_scope / "memory.events").write_text(raw)
        proc = root / "kernel/proc/1234"
        proc.mkdir(parents=True)
        (proc / "stat").write_text("1234 (fixture) S " + "0 " * 18 + "42\n")
        (proc / "cgroup").write_text("0::" + scope.removeprefix("/sys/fs/cgroup") + "\n")
        body = json.loads(_exited_payload())[0]
        body["State"].update(Status="running", Running=True, Pid=1234)
        payload = root / "live.inspect"
        payload.write_text(json.dumps([body]))
        _fake_docker(self.bin_dir, f"{CID} /{CONTAINER} true 1234 {body['State']['StartedAt']}\n", 0)
        original_open = os.open
        denied = False
        def kernel_open(path, *args, **kwargs):
            text = os.fspath(path)
            if denied and text == "memory.events":
                raise PermissionError("fixture access denied")
            if isinstance(text, str) and (text.startswith(scope) or text.startswith("/proc/1234/")):
                path = root / "kernel" / text.lstrip("/")
            return original_open(path, *args, **kwargs)
        def execute(live):
            argv = ["archive", CID, CONTAINER, str(payload), str(MAX_INSPECT_BYTES)]
            environment = dict(self.env, GB10_RETAIN_SNAPSHOT_LIVE=str(int(live)))
            with patch.dict(os.environ, environment), patch.object(sys, "argv", argv), patch.object(os, "open", kernel_open):
                try:
                    exec(compile(block, str(HELPER), "exec"), {"__name__": "__main__"})
                except SystemExit as error:
                    self.assertEqual(error.code, 1)

        store = root / ".local/state/gb10-vllm-cids"
        original_fsync = os.fsync
        def execute_interrupted(live):
            def crash_after_durable_publish(fd):
                original_fsync(fd)
                suffix = "*.memory.json" if live else "*.exited.json"
                if stat.S_ISDIR(os.fstat(fd).st_mode) and list(store.glob(suffix)):
                    os._exit(77)  # Real child interruption before publication cleanup.
            def capture():
                with patch.object(os, "fsync", crash_after_durable_publish):
                    execute(live)
            child = multiprocessing.get_context("fork").Process(target=capture)
            try:
                child.start()
                child.join(8)
                self.assertFalse(child.is_alive(), "publication fault barrier timed out")
                self.assertEqual(child.exitcode, 77)
            finally:
                if child.is_alive():
                    child.kill()
                child.join(3)
        execute_interrupted(True)
        snapshot = next(store.glob("*.memory.json"), None)
        self.assertIsNotNone(snapshot, "live same-generation counters were not durably captured")
        assert snapshot is not None
        saved = json.loads(snapshot.read_text())
        self.assertEqual(saved["memory_events"]["raw"], raw)
        self.assertEqual(saved["owner"]["start_ticks"], 42)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        body = json.loads(_exited_payload())[0]
        payload.write_text(json.dumps([body]))
        _fake_docker(self.bin_dir, f"{CID} /{CONTAINER} false 0 {body['State']['StartedAt']}\n", 0)
        # A surviving scope is terminal evidence only with the saved inode and
        # a second same-StartedAt exited inspection around the counter read.
        execute_interrupted(False)
        archive = next(store.glob("*.exited.json"))
        self.assertEqual(snapshot.stat().st_nlink, 1)
        self.assertEqual(archive.stat().st_nlink, 1)
        terminal = json.loads(archive.read_text())["memory_events"]
        self.assertTrue(terminal["generation_fenced"])
        self.assertEqual(terminal["terminal_status"], "captured")
        archive.unlink()
        shutil.rmtree(owned_scope)
        execute_interrupted(False)
        archive = next(store.glob("*.exited.json"))
        receipt = json.loads(archive.read_text())
        self.assertEqual(receipt["memory_events"]["status"], "last-known")
        self.assertEqual(receipt["memory_events"]["raw"], raw)
        self.assertEqual(receipt["memory_events"]["counts"]["oom_kill"], 1)
        self.assertEqual(receipt["memory_events"]["terminal_status"], "unavailable")
        self.assertEqual(receipt["kill_initiator"], "UNKNOWN")
        self.assertEqual(receipt["state"]["StartedAt"], saved["started_at"])
        archive.unlink()
        original = snapshot.read_bytes()
        # Neither a foreign alias nor a symlink is our publication protocol.
        foreign = root / "foreign-alias"
        os.link(snapshot, foreign)
        execute(False)
        self.assertEqual(json.loads(archive.read_text())["memory_events"]["status"], "unavailable")
        archive.unlink()
        foreign.unlink()
        snapshot.rename(foreign)
        snapshot.symlink_to(foreign)
        execute(False)
        self.assertEqual(json.loads(archive.read_text())["memory_events"]["status"], "unavailable")
        archive.unlink()
        snapshot.unlink()
        foreign.rename(snapshot)
        for field, value in (("container_id", "a" * 64),
                             ("started_at", "2026-09-18T17:00:00.000Z"),
                             ("boot_id", "0" * 36), ("owner", {"pid": 1234, "start_ticks": 0}),
                             ("memory_events", {"status": "captured", "raw": "oom bad\n"})):
            with self.subTest(stale=field):
                candidate = dict(saved, **{field: value})
                snapshot.write_text(json.dumps(candidate))
                execute(False)
                self.assertEqual(json.loads(archive.read_text())["memory_events"]["status"], "unavailable")
                archive.unlink()
        for invalid in (b"[]", b"{", b"x" * 16385):
            snapshot.write_bytes(invalid)
            execute(False)
            self.assertEqual(json.loads(archive.read_text())["memory_events"]["status"], "unavailable")
            archive.unlink()
        snapshot.write_bytes(original)
        snapshot.chmod(0o644)
        execute(False)
        self.assertEqual(json.loads(archive.read_text())["memory_events"]["status"], "unavailable")
        archive.unlink()
        snapshot.chmod(0o600)
        # PID reuse and malformed/unreadable kernel counters must never publish
        # a replacement snapshot. Everything here is fixture-owned.
        owned_scope.mkdir()
        event = owned_scope / "memory.events"
        body["State"].update(Status="running", Running=True, Pid=1234)
        payload.write_text(json.dumps([body]))
        _fake_docker(self.bin_dir, f"{CID} /{CONTAINER} true 1234 {body['State']['StartedAt']}\n", 0)
        (proc / "stat").write_text("1234 (fixture) S " + "0 " * 18 + "43\n")
        event.write_text(raw)
        execute(True)
        self.assertEqual(snapshot.read_bytes(), original)
        snapshot.unlink()
        (proc / "stat").write_text("1234 (fixture) S " + "0 " * 18 + "42\n")
        denied = True
        execute(True)
        self.assertFalse(snapshot.exists())
        denied = False
        for invalid in ("oom 1\noom_kill bad\n", "oom 1\noom 2\noom_kill 0\n", "oom 1\noom_kill 0"):
            event.write_text(invalid)
            execute(True)
            self.assertFalse(snapshot.exists())
        event.unlink()
        event.symlink_to(root / "missing")
        execute(True)
        self.assertFalse(snapshot.exists())

    def test_text_start_hooks_capture_once_before_readiness(self) -> None:
        for unit, container, cidfile, output in UNITS:
            text = unit.read_text()
            hooks = [line for line in text.splitlines() if line.startswith("ExecStartPost=")]
            live = [line for line in hooks if "--snapshot-live" in line]
            self.assertEqual(len(live), 1, unit.name)
            self.assertIn(HELPER_DEST, live[0])
            self.assertIn(f"--container {container}", live[0])
            self.assertIn(f"--cidfile {cidfile}", live[0])
            self.assertIn(f"--output {output}", live[0])
            self.assertLess(hooks.index(live[0]), next(i for i, line in enumerate(hooks) if "gb10_service_ready.sh" in line))

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

    def test_durable_generation_receipt_survives_runtime_directory_loss(self) -> None:
        payload = json.loads(_exited_payload())
        payload[0]["Config"] = {"Env": ["SECRET=must-not-be-archived"]}
        _fake_docker(self.bin_dir, json.dumps(payload), 0)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archive = next((Path(self.env["HOME"]) / ".local/state/gb10-vllm-cids").glob(f"{CID}.*.exited.json"), None)
        self.assertIsNotNone(archive, result.stdout + result.stderr)
        assert archive is not None
        receipt = json.loads(archive.read_text())
        self.assertEqual(receipt["container_id"], CID)
        self.assertEqual(receipt["state"]["ExitCode"], 137)
        self.assertIs(receipt["state"]["OOMKilled"], False)
        self.assertEqual(receipt["memory_events"]["status"], "unavailable")
        self.assertGreater(receipt["caller"]["pid"], 0)
        self.assertGreater(receipt["caller"]["start_ticks"], 0)
        self.assertRegex(receipt["boot_id"], r"^[0-9a-f-]{36}$")
        self.assertNotIn("must-not-be-archived", archive.read_text())
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.cidfile.unlink()
        self.output.unlink()
        self.identity.rmdir()
        self.assertEqual(json.loads(archive.read_text()), receipt)

    def test_durable_archive_keeps_first_episode_and_rejects_unsafe_store(self) -> None:
        _fake_docker(self.bin_dir, _exited_payload(), 0)
        first = _run(self.env, self.cidfile, self.output)
        self.assertIn("retained", first.stdout)
        store = Path(self.env["HOME"]) / ".local/state/gb10-vllm-cids"
        archive = next(store.glob(f"{CID}.*.exited.json"))
        initial = archive.read_bytes()
        second = _run(self.env, self.cidfile, self.output)
        self.assertIn("retained", second.stdout)
        self.assertEqual(archive.read_bytes(), initial)
        # Simultaneous duplicate hooks with different evidence cannot replace
        # the first generation receipt, even after lock contention settles.
        from concurrent.futures import ThreadPoolExecutor
        changed = json.loads(_exited_payload())
        changed[0]["State"]["ExitCode"] = 0
        _fake_docker(self.bin_dir, json.dumps(changed), 0)
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: _run(self.env, self.cidfile, self.output), range(4)))
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertEqual(archive.read_bytes(), initial)
        self.assertEqual(archive.stat().st_nlink, 1)
        self.assertEqual(list(store.glob(".inspect-*")), [])
        store.chmod(0o777)
        self.output.write_text(LAST_GOOD + "\n")
        refused = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, refused, self.output)
        self.assertEqual(archive.read_bytes(), initial)

    def test_archive_distinguishes_started_at_and_preserves_capacity(self) -> None:
        _fake_docker(self.bin_dir, _exited_payload(), 0)
        self.assertIn("retained", _run(self.env, self.cidfile, self.output).stdout)
        store = Path(self.env["HOME"]) / ".local/state/gb10-vllm-cids"
        payload = json.loads(_exited_payload())
        payload[0]["State"]["StartedAt"] = "2026-09-18T17:00:00.000Z"
        _fake_docker(self.bin_dir, json.dumps(payload), 0)
        self.assertIn("retained", _run(self.env, self.cidfile, self.output).stdout)
        self.assertEqual(len(list(store.glob("*.exited.json"))), 2)
        for index in range(62):
            (store / f"retained-{index}.exited.json").write_text("{}")
        payload[0]["State"]["StartedAt"] = "2026-09-19T17:00:00.000Z"
        _fake_docker(self.bin_dir, json.dumps(payload), 0)
        self.output.write_text(LAST_GOOD + "\n")
        refused = _run(self.env, self.cidfile, self.output)
        _assert_kept_last_good(self, refused, self.output)
        self.assertEqual(len(list(store.glob("*.exited.json"))), 64)

    def test_concurrent_archive_admission_preserves_capacity(self) -> None:
        # Execute the unchanged archive block in owned processes, pausing only
        # the first count snapshot while the second writer attempts admission.
        source = HELPER.read_text()
        block = source.split('<<\'PY\' || skip "inspect validation or durable archive failed; keeping last inspect"\n', 1)[1].split('\nPY\n', 1)[0]
        context = multiprocessing.get_context("fork")
        snapshot, release = context.Event(), context.Event()
        results = context.Queue()
        store = Path(self.env["HOME"]) / ".local/state/gb10-vllm-cids"
        store.mkdir(parents=True, mode=0o700)
        for index in range(63):
            (store / f"retained-{index}.exited.json").write_text("{}")

        def worker(cid: str, pause: bool) -> None:
            import sys
            os.environ["HOME"] = self.env["HOME"]
            payload = Path(self.temporary.name) / f"{cid}.inspect"
            body = json.loads(_exited_payload())
            body[0]["Id"] = cid
            payload.write_text(json.dumps(body))
            sys.argv = ["archive", cid, CONTAINER, str(payload), str(MAX_INSPECT_BYTES)]
            original = os.listdir
            def count(directory: int) -> list[str]:
                entries = original(directory)
                if pause:
                    snapshot.set()
                    if not release.wait(6):
                        raise TimeoutError("admission fixture release")
                return entries
            os.listdir = count
            try:
                exec(compile(block, str(HELPER), "exec"), {"__name__": "__main__"})
                results.put("success")
            except (OSError, SystemExit) as error:
                results.put(type(error).__name__)

        first = context.Process(target=worker, args=("a" * 64, True))
        second = context.Process(target=worker, args=("b" * 64, False))
        try:
            first.start()
            self.assertTrue(snapshot.wait(3))
            second.start()
            second.join(3)
            self.assertFalse(second.is_alive())
            release.set()
            first.join(3)
            self.assertFalse(first.is_alive())
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)
            self.assertLessEqual(len(list(store.glob("*.exited.json"))), 64)
            self.assertEqual(sorted(results.get(timeout=1) for _ in range(2)), ["BlockingIOError", "success"])
        finally:
            release.set()
            for process in (first, second):
                if process.pid is not None:
                    if process.is_alive():
                        process.kill()
                    process.join(3)
            results.close()
            results.join_thread()

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
        self.assertEqual(leftover, [])
        self.assertGreaterEqual(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC - 1)
        self.assertLess(elapsed, RETAIN_DEADLINE_SEC + RETAIN_KILL_AFTER_SEC + 3)

    def test_nested_children_settle_after_timeout_and_normal_exit(self) -> None:
        # Run each real process topology in an isolated subreaper so even RED
        # owns and reaps its exact fixture children before deleting their files.
        probe = r'''import ctypes, json, os, signal, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import test_gb10_retain_container_inspect as m
mode = sys.argv[2]
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
case = m.RetainContainerInspectTests(); case.setUp()
known = []; caller = None
try:
    marker = case.identity / "nested-child.json"
    docker = case.bin_dir / "docker"
    identity = (
        'raw=Path("/proc/self/stat").read_text(); f=raw[raw.rfind(")")+2:].split()\n'
        'record={"pid":os.getpid(),"start_ticks":f[19],"pgid":os.getpgrp(),"sid":os.getsid(0)}\n'
        f'part=Path({str(marker)!r}+".partial"); part.write_text(json.dumps(record)); os.replace(part,{str(marker)!r})\n'
    )
    prefix = "#!/usr/bin/python3 -IBS\nimport json,os,signal,time\nfrom pathlib import Path\n"
    if mode == "delayed-inspect":
        delay = case.identity / "chmod-start"
        chmod = case.bin_dir / "chmod"
        chmod.write_text(f'#!/bin/sh\nif [ ! -f "{delay}" ]; then : > "{delay}"; /bin/sleep 4; fi\nexec /bin/chmod "$@"\n')
        chmod.chmod(0o700)
        docker.write_text(prefix + 'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n' + identity + 'time.sleep(30)\n')
        command = ["/usr/bin/timeout", "--signal=TERM", "--kill-after=2", "10"]
        limit = 16
    else:
        docker.write_text(prefix + 'child=os.fork()\nif child:\n'
                          f'    deadline=time.monotonic()+3\n    while not Path({str(marker)!r}).exists() and time.monotonic()<deadline: time.sleep(.01)\n'
                          f'    assert Path({str(marker)!r}).exists()\n    print({m._exited_payload()!r},flush=True)\n    os._exit(0)\n'
                          'os.setsid()\nnull=os.open(os.devnull,os.O_RDWR)\n'
                          'for fd in (0,1,2): os.dup2(null,fd)\nos.close(null)\n'
                          'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n' + identity + 'time.sleep(30)\n')
        command = []
        limit = 5
    docker.chmod(0o700)
    started = time.monotonic()
    caller = subprocess.Popen(command + ["bash", str(m.HELPER), "--container", m.CONTAINER,
                              "--cidfile", str(case.cidfile), "--output", str(case.output)],
                              env=case.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    until = time.monotonic() + (6 if command else 3)
    while time.monotonic() < until and not marker.exists(): time.sleep(.01)
    assert marker.exists(), "owned nested process did not start"
    recorded = json.loads(marker.read_text())
    for pid in sorted({recorded["pid"], recorded["pgid"]}):
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue  # A repaired helper may already have settled this child.
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(); f = raw[raw.rfind(")")+2:].split()
        except FileNotFoundError:
            os.close(fd); continue
        if pid == recorded["pid"] and f[19] != recorded["start_ticks"]:
            os.close(fd); continue
        known.append((pid, f[19], fd))
    rc = caller.wait(timeout=limit)
    alive = []
    for pid, ticks, fd in known:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(); f = raw[raw.rfind(")")+2:].split()
        except FileNotFoundError:
            continue
        if f[19] == ticks: alive.append({"pid":pid,"start_ticks":ticks,"state":f[0]})
    print(json.dumps({"mode":mode,"recorded":recorded,"returncode":rc,
                      "elapsed":time.monotonic()-started,"surviving":alive}),flush=True)
    assert rc == (124 if command else 0), rc
    assert list(case.identity.glob(case.output.name + ".tmp.*")) == []
    if command:
        assert case.output.read_text() == m.LAST_GOOD + "\n"
    else:
        assert json.loads(case.output.read_text()) == json.loads(m._exited_payload())
    assert not alive, "owned nested child survived helper return"
finally:
    if caller is not None and caller.poll() is None:
        caller.kill(); caller.wait(timeout=2)
    settled=False
    try:
        deadline=time.monotonic()+2
        while True:
            try: child,status=os.waitpid(-1,os.WNOHANG)
            except ChildProcessError: break
            if child:
                print(json.dumps({"fixture_reaped":child,"status":status}),flush=True)
                continue
            if time.monotonic()>=deadline:
                raise RuntimeError("fixture cleanup deadline")
            with open(f"/proc/self/task/{os.getpid()}/children","rb") as stream:
                raw=stream.read(65537)
            if len(raw)>65536: raise RuntimeError("fixture child list exceeded bound")
            for pid in raw.split():
                if time.monotonic()>=deadline: raise RuntimeError("fixture cleanup deadline")
                try: fd=os.pidfd_open(int(pid))
                except ProcessLookupError: continue
                try:
                    if os.waitid(os.P_PIDFD,fd,os.WEXITED|os.WNOHANG|os.WNOWAIT) is None:
                        signal.pidfd_send_signal(fd,signal.SIGKILL)
                except (ChildProcessError,ProcessLookupError): pass
                finally: os.close(fd)
            time.sleep(.01)
        settled=True
    finally:
        for _,_,fd in known: os.close(fd)
        if caller is not None: caller.stdout.close(); caller.stderr.close()
        if settled: case.doCleanups()
        else:
            case.temporary._finalizer.detach()
            print("fixture cleanup uncertain; preserving " + case.temporary.name,flush=True)

'''
        for mode in ("delayed-inspect", "normal-exit"):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    ["/usr/bin/python3", "-IBS", "-c", probe, str(ROOT / "tests"), mode],
                    text=True, capture_output=True, timeout=18, check=False,
                )
                print(result.stdout, end="", flush=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('"surviving": []', result.stdout)

    def test_adopted_child_authority_bounds_and_descriptor_cleanup(self) -> None:
        import ast
        import io
        import signal
        from unittest.mock import patch

        source = HELPER.read_text().split("<<'PY_SUPERVISOR'\n", 1)[1].split("\nPY_SUPERVISOR", 1)[0]
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "kill_owned_children")
        namespace = {"os": os, "time": time, "signal": signal, "reap_deadline": time.monotonic() + 2}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(HELPER), "exec"), namespace)
        settle = namespace["kill_owned_children"]
        opened = []
        original = os.pidfd_open

        def capture(pid: int) -> int:
            fd = original(pid)
            opened.append(fd)
            return fd

        # Our own PID is live but is not our child. Real P_PIDFD waitid must
        # reject it, and the held descriptor must close without any signal.
        with patch("builtins.open", return_value=io.BytesIO(str(os.getpid()).encode())), \
             patch("os.pidfd_open", side_effect=capture), \
             patch("signal.pidfd_send_signal") as send:
            settle()
            send.assert_not_called()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        with patch("builtins.open", return_value=io.BytesIO(str(os.getpid()).encode())), \
             patch("os.pidfd_open", side_effect=capture), \
             patch("os.waitid", side_effect=OSError("child authority unavailable")), \
             patch("signal.pidfd_send_signal") as send:
            with self.assertRaises(OSError):
                settle()
            send.assert_not_called()
        with self.assertRaises(OSError):
            os.fstat(opened[-1])
        with patch("builtins.open", return_value=io.BytesIO(b"99999999")), \
             patch("os.pidfd_open", side_effect=ProcessLookupError), \
             patch("signal.pidfd_send_signal") as send:
            settle()
            send.assert_not_called()
        with patch("builtins.open", return_value=io.BytesIO(b"1" * 65537)), \
             patch("os.pidfd_open") as acquire:
            with self.assertRaises(ValueError):
                settle()
            acquire.assert_not_called()
        namespace["reap_deadline"] = time.monotonic() - 1
        with patch("builtins.open", return_value=io.BytesIO(b"1")), \
             patch("os.pidfd_open") as acquire:
            with self.assertRaises(TimeoutError):
                settle()
            acquire.assert_not_called()

    def test_reused_worker_pid_preserves_recorded_status_and_cleanup(self) -> None:
        # Model a distinct adopted child reusing the already-reaped worker PID.
        source = HELPER.read_text().split("<<'PY_SUPERVISOR'\n", 1)[1].split("\nPY_SUPERVISOR", 1)[0]
        probe = r'''import os, socket, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
source, output = sys.argv[1:]
child = SimpleNamespace(pid=99999999, returncode=7)
child.wait = lambda **kwargs: child.returncode
channel, worker_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
worker_channel.sendall(os.fsencode(output))
held_peer = worker_channel.dup()
statuses = iter([(child.pid, 0), (child.pid + 1, 0), ChildProcessError()])
def reap(pid, flags):
    assert (pid, flags) == (-1, os.WNOHANG)
    status = next(statuses)
    if isinstance(status, BaseException):
        raise status
    return status
sys.argv = ["supervisor", "fixture", "10", "2"]
try:
    with patch("socket.socketpair", return_value=(channel, worker_channel)), \
         patch("subprocess.Popen", return_value=child), \
         patch("os.pidfd_open", side_effect=lambda _pid: os.open(os.devnull, os.O_RDONLY)), \
         patch("os.killpg") as kill, patch("os.waitpid", side_effect=reap) as wait:
        try:
            exec(compile(Path(source).read_text(), "supervisor", "exec"), {"__name__": "__main__"})
        except SystemExit as exit:
            assert exit.code == 7, exit.code
        else:
            raise AssertionError("supervisor did not preserve the worker exit")
        assert child.returncode == 7
        assert wait.call_count == 3, wait.call_count
        kill.assert_not_called()
finally:
    held_peer.close()
assert list(Path(output).parent.glob(Path(output).name + ".tmp.*")) == []
print("reused-PID-reaped-to-ECHILD; worker-status-preserved; tempfile-cleaned")
'''
        script = Path(self.temporary.name) / "supervisor.source"
        script.write_text(source)
        result = subprocess.run(
            ["/usr/bin/python3", "-IS", "-c", probe, str(script), str(self.output)],
            text=True, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reused-PID-reaped-to-ECHILD", result.stdout)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")
        self.assertEqual(list(self.identity.glob(f"{self.output.name}.tmp.*")), [])

    def test_term_after_worker_reap_preserves_exit_status_and_cleanup(self) -> None:
        source = HELPER.read_text().split("<<'PY_SUPERVISOR'\n", 1)[1].split("\nPY_SUPERVISOR", 1)[0]
        root = Path(self.temporary.name)
        script = root / "supervisor.source"
        script.write_text(source)
        worker = root / "abnormal-worker.sh"
        worker.write_text("""exec /usr/bin/python3 -IS - "$1" <<'PY'
import os, signal, socket, sys
with socket.socket(fileno=int(os.environ["GB10_RETAIN_SOCKET_FD"])) as channel:
    channel.settimeout(2)
    channel.sendall(os.fsencode(sys.argv[1]))
    assert channel.recv(65536)
os.kill(os.getpid(), signal.SIGKILL)
PY
""")
        # Keep real Popen/waitpid; inject TERM only after the kernel has reaped
        # the abnormal worker, before Popen stores its returncode.
        probe = r'''import json, os, signal, subprocess, sys
from pathlib import Path
from unittest.mock import patch
source, worker, output = sys.argv[1:]
original = subprocess.Popen._handle_exitstatus
events = []
def record_status(child, status):
    assert child.returncode is None
    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    try:
        os.waitpid(child.pid, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        raise AssertionError("worker was not already reaped")
    assert len(list(Path(output).parent.glob(Path(output).name + ".tmp.*"))) == 1
    print("TERM-after-real-waitpid", flush=True)
    os.kill(os.getpid(), signal.SIGTERM)
    original(child, status)
    events.append({"pid": child.pid, "returncode": child.returncode})
sys.argv = ["supervisor", worker, "10", "2", output]
try:
    with patch.object(subprocess.Popen, "_handle_exitstatus", record_status):
        exec(compile(Path(source).read_text(), "supervisor", "exec"), {"__name__": "__main__"})
except SystemExit as exit:
    assert exit.code == 0, exit.code
assert len(events) == 1 and events[0]["returncode"] == -signal.SIGKILL, events
assert list(Path(output).parent.glob(Path(output).name + ".tmp.*")) == []
print(json.dumps(events))
'''
        result = subprocess.run(
            ["/usr/bin/python3", "-IS", "-c", probe, str(script), str(worker), str(self.output)],
            env=self.env, text=True, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TERM-after-real-waitpid", result.stdout)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")
        self.assertEqual(list(self.identity.glob(f"{self.output.name}.tmp.*")), [])

    def test_term_during_supervisor_settlement_does_not_abort_cleanup(self) -> None:
        # Isolate real signal delivery from unittest's own process. Replace only
        # process syscalls so the TERM lands deterministically inside settlement.
        source = HELPER.read_text().split("<<'PY_SUPERVISOR'\n", 1)[1].split("\nPY_SUPERVISOR", 1)[0]
        probe = r'''import json, os, select, signal, socket, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
source, output = sys.argv[1:]
child = SimpleNamespace(pid=99999999, returncode=None)
child.wait = lambda **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("owned worker", 10))
channel, worker_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
worker_channel.sendall(os.fsencode(output))
held_peer = worker_channel.dup()  # Model the live worker retaining its endpoint.
events = []
statuses = iter([(child.pid, signal.SIGKILL), ChildProcessError()])
original_select = select.select
def reap(*_args):
    status = next(statuses)
    if isinstance(status, BaseException):
        raise status
    events.append("reap")
    return status
def settle(readers, writers, errors, timeout):
    if events != ["SIGTERM"]:
        return original_select(readers, writers, errors, timeout)
    events.append("TERM-during-settlement")
    os.kill(os.getpid(), signal.SIGTERM)
    return ([], [], [])
def kill(_pid, sig):
    events.append(signal.Signals(sig).name)
sys.argv = ["supervisor", "fixture", "10", "2"]
try:
    with patch("socket.socketpair", return_value=(channel, worker_channel)), \
         patch("subprocess.Popen", return_value=child), \
         patch("os.pidfd_open", side_effect=lambda _pid: os.open(os.devnull, os.O_RDONLY)), \
         patch("os.killpg", side_effect=kill), patch("os.waitpid", side_effect=reap), \
         patch("select.select", side_effect=settle):
        exec(compile(Path(source).read_text(), "supervisor", "exec"), {"__name__": "__main__"})
except SystemExit as exit:
    assert exit.code == 0, exit.code
assert events == ["SIGTERM", "TERM-during-settlement", "SIGKILL", "reap"], events
assert list(Path(output).parent.glob(Path(output).name + ".tmp.*")) == []
print(json.dumps(events))
'''
        script = Path(self.temporary.name) / "supervisor.source"
        script.write_text(source)
        result = subprocess.run(
            ["/usr/bin/python3", "-IS", "-c", probe, str(script), str(self.output)],
            text=True, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TERM-during-settlement", result.stdout)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")
        self.assertEqual(list(self.identity.glob(f"{self.output.name}.tmp.*")), [])

    def test_supervisor_cleanup_preserves_replacement_symlink(self) -> None:
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        foreign = self.identity / "foreign"
        foreign.write_text("not owned by the retainer\n")
        mv = self.bin_dir / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            '/bin/rm -- "$3"\n'
            f'/bin/ln -s {json.dumps(str(foreign))} "$3"\n'
            "exec /bin/sleep 30\n"
        )
        mv.chmod(mv.stat().st_mode | stat.S_IXUSR)
        bash_env = Path(self.temporary.name) / "replacement-ignore-term.bash"
        bash_env.write_text("trap '' TERM\n")
        env = dict(self.env, BASH_ENV=str(bash_env))
        result = _run(env, self.cidfile, self.output, timeout=18)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")
        leftover = list(self.identity.glob(f"{self.output.name}.tmp.*"))
        self.assertEqual(len(leftover), 1)
        self.assertTrue(leftover[0].is_symlink())
        self.assertEqual(leftover[0].readlink(), foreign)
        self.assertEqual(foreign.read_text(), "not owned by the retainer\n")

    def test_timed_out_writer_does_not_clean_concurrent_publication(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        marker = self.identity / "paused"
        mv = self.bin_dir / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            'if [ "${RETAIN_TEST_PAUSE:-}" = 1 ]; then\n'
            f'  : > {json.dumps(str(marker))}\n'
            "  trap '' TERM\n"
            "  exec /bin/sleep 30\n"
            "fi\n"
            'exec /bin/mv "$@"\n'
        )
        mv.chmod(mv.stat().st_mode | stat.S_IXUSR)
        foreign = self.identity / f"{self.output.name}.tmp.foreign"
        foreign.write_text("another invocation owns this\n")
        paused_env = dict(self.env, RETAIN_TEST_PAUSE="1")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(_run, paused_env, self.cidfile, self.output, timeout=16)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "writer did not reach publication")
            published = _run(self.env, self.cidfile, self.output)
            self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
            self.assertIn("retained", published.stdout)
            expected = self.output.read_bytes()
            result = pending.result(timeout=16)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("retained", result.stdout)
        self.assertEqual(self.output.read_bytes(), expected)
        self.assertEqual(list(self.identity.glob(f"{self.output.name}.tmp.*")), [foreign])
        self.assertEqual(foreign.read_text(), "another invocation owns this\n")

    def test_callers_outer_term_reaps_worker_before_cleanup(self) -> None:
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        marker = self.identity / "worker-identity"
        mv = self.bin_dir / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            f"printf '%s ' \"$$\" > {json.dumps(str(marker))}\n"
            f"/usr/bin/awk '{{print $22}}' /proc/$$/stat >> {json.dumps(str(marker))}\n"
            "trap '' TERM\nexec /bin/sleep 30\n"
        )
        mv.chmod(mv.stat().st_mode | stat.S_IXUSR)
        result = subprocess.run(
            ["/usr/bin/timeout", "--signal=TERM", "--kill-after=2", "10",
             "bash", str(HELPER), "--container", CONTAINER,
             "--cidfile", str(self.cidfile), "--output", str(self.output)],
            env=self.env, text=True, capture_output=True, timeout=16, check=False,
        )
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertTrue(marker.exists(), "worker never reached publication")
        pid, start_ticks = marker.read_text().split()
        try:
            current = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            pass
        else:
            fields = current[current.rfind(")") + 2:].split()
            self.assertNotEqual(fields[19], start_ticks, "owned writer survived outer TERM")
        self.assertEqual(self.output.read_text(), LAST_GOOD + "\n")
        self.assertNotIn("retained", result.stdout)
        self.assertEqual(list(self.identity.glob(f"{self.output.name}.tmp.*")), [])

    def test_existing_maximum_temp_prefix_still_publishes(self) -> None:
        _fake_docker(self.bin_dir, _exited_payload() + "\n", 0)
        output = self.identity / ("x" * 244)
        output.write_text(LAST_GOOD + "\n")
        result = _run(self.env, self.cidfile, output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("retained", result.stdout, result.stderr)
        self.assertEqual(json.loads(output.read_text()), json.loads(_exited_payload()))
        self.assertEqual(list(self.identity.glob(f"{output.name}.tmp.*")), [])

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

    def test_docker_cidfile_without_newline_retains_exited_generation(self) -> None:
        # Docker writes exactly 64 hex bytes, without a trailing newline.
        self.cidfile.write_bytes(CID.encode())
        updated = _exited_payload()
        _fake_docker(self.bin_dir, updated + "\n", 0)
        result = _run(self.env, self.cidfile, self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), updated + "\n")
        self.assertIn(f"retained cid={CID}", result.stdout)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(list(self.identity.glob(f"{self.output.name}.tmp.*")), [])

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
        self.assertIn("--enable-prefix-caching", text)
        self.assertNotIn("--no-enable-prefix-caching", text)
        self.assertLess(
            text.find("first verify every live setting is retained"),
            text.find(ULTIMATE_UNIT_SOURCE),
        )
        self.assertNotIn("Then install the unit that will own", text)

    def test_runbook_unit_hook_stage_requires_reload_readback_and_apc_gate(self) -> None:
        text = RUNBOOK.read_text()
        live_gate_at = text.find("first verify every live setting is retained")
        self.assertNotEqual(live_gate_at, -1)
        for name, source in UNIT_SOURCES.items():
            with self.subTest(unit=name):
                source_at = text.find(source)
                self.assertNotEqual(source_at, -1, source)
                self.assertGreater(source_at, live_gate_at, source)
                self.assertNotIn(f"install -m 0644 {source}", text)
        self.assertIn("daemon-reload", text)
        self.assertIn("systemctl --user show", text)
        self.assertIn("ExecStop", text)
        self.assertIn("ExecStopPost", text)
        self.assertIn(
            "Do not copy this block until every live setting is verified",
            text,
        )


if __name__ == "__main__":
    unittest.main()
