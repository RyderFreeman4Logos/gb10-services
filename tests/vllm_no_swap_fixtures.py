from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "gb10_verify_vllm_no_swap.sh"
VERIFIER_CORE = ROOT / "scripts" / "gb10_verify_vllm_no_swap_core.py"


def _proc_stat(pid: int, starttime: int) -> str:
    fields_three_through_twenty_two = ["S", *(["0"] * 18), str(starttime)]
    return f"{pid} (vllm worker (test)) " + " ".join(fields_three_through_twenty_two) + "\n"


class VllmNoSwapFixture(unittest.TestCase):
    image = "example.invalid/vllm@sha256:" + "d" * 64
    memory = 18 * 1024**3

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.proc_root = self.root / "proc"
        self.cgroup_root = self.root / "cgroup"
        self.proc_root.mkdir()
        self.cgroup_root.mkdir()
        self.unit = self.root / "vllm-test.service"
        self.second_unit = self.root / "vllm-second.service"
        self.command_log = self.root / "commands.log"
        self.inspect_state = self.root / "inspect-state.json"
        self.cleanup_state = self.root / "cleanup-state.json"
        self.docker = self.root / "docker"
        self.systemctl = self.root / "systemctl"
        self._write_docker()
        self._write_systemctl()

        self.identifiers = {"vllm-test": "a" * 64, "vllm-second": "b" * 64}
        self.pids = {"vllm-test": 4242, "vllm-second": 5252}
        self.started = {
            "vllm-test": "2026-07-18T01:02:03.123456789Z",
            "vllm-second": "2026-07-18T01:02:04.123456789Z",
        }
        self.starttimes = {"vllm-test": 111_111, "vllm-second": 222_222}
        self.scopes = {
            name: (
                f"/user.slice/user-1001.slice/user@1001.service/app.slice/"
                f"docker-{identifier}.scope"
            )
            for name, identifier in self.identifiers.items()
        }
        self._write_unit(self.unit, "vllm-test", "/run/user/1001/gb10-vllm-cids/test.cid")
        self._write_unit(
            self.second_unit,
            "vllm-second",
            "/run/user/1001/gb10-vllm-cids/second.cid",
        )
        for name in self.identifiers:
            self._write_generation(name)
        self.containers = {
            name: self._inspect(name) for name in self.identifiers
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_unit(
        self,
        path: Path,
        name: str,
        cidfile: str,
        *,
        application: list[str] | None = None,
    ) -> None:
        command = application or [
            "/usr/local/bin/vllm",
            "serve",
            "model",
        ]
        path.write_text(
            "[Service]\n"
            "ExecStart=/usr/bin/docker run --rm "
            f"--cidfile={cidfile} --name {name} "
            "--memory 18g --memory-swap 18g --memory-swappiness 0 --entrypoint python3 "
            f"{self.image} "
            + " ".join(shlex.quote(token) for token in command)
            + "\n"
        )
        path.chmod(0o644)

    def _write_generation(self, name: str) -> None:
        pid = self.pids[name]
        proc = self.proc_root / str(pid)
        proc.mkdir(parents=True, exist_ok=True)
        (proc / "stat").write_text(_proc_stat(pid, self.starttimes[name]))
        (proc / "cgroup").write_text(f"0::{self.scopes[name]}\n")
        scope = self.cgroup_root / self.scopes[name].removeprefix("/")
        scope.mkdir(parents=True, exist_ok=True)
        (scope / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        (scope / "memory.max").write_text(f"{self.memory}\n")
        (scope / "memory.swap.max").write_text("0\n")
        (scope / "memory.swap.current").write_text("0\n")

    def _inspect(
        self,
        name: str,
        *,
        identifier: str | None = None,
        pid: int | None = None,
        started_at: str | None = None,
        memory: int | None = None,
        memory_swap: int | None = None,
        entrypoint: list[str] | None = None,
        command: list[str] | None = None,
        running: bool = True,
    ) -> dict[str, object]:
        return {
            "Id": identifier or self.identifiers[name],
            "Name": f"/{name}",
            "State": {
                "Pid": self.pids[name] if pid is None else pid,
                "Running": running,
                "StartedAt": self.started[name] if started_at is None else started_at,
            },
            "HostConfig": {
                "Memory": self.memory if memory is None else memory,
                "MemorySwap": self.memory if memory_swap is None else memory_swap,
            },
            "Config": {
                "Image": self.image,
                "Entrypoint": ["python3"] if entrypoint is None else entrypoint,
                "Cmd": command
                if command is not None
                else [
                    "/usr/local/bin/vllm",
                    "serve",
                    "model",
                ],
            },
        }

    def _write_docker(self) -> None:
        self.docker.write_text(
            "#!/usr/bin/python3\n"
            "import json, os, shutil, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['GB10_FAKE_COMMAND_LOG'], 'a') as sink:\n"
            "    sink.write('docker ' + ' '.join(args) + '\\n')\n"
            "if args == ['info', '--format', '{{.CgroupVersion}}']:\n"
            "    if os.environ.get('GB10_FAKE_INFO_FAIL') == '1':\n"
            "        print('fake docker info failure', file=sys.stderr); raise SystemExit(73)\n"
            "    sys.stdout.write(os.environ.get('GB10_FAKE_CGROUP_VERSION', '2') + '\\n')\n"
            "    raise SystemExit(0)\n"
            "mode = os.environ.get('GB10_FAKE_DOCKER_MODE', 'verify')\n"
            "def absent(value):\n"
            "    print('Error: No such object: ' + value, file=sys.stderr); raise SystemExit(1)\n"
            "if mode == 'cleanup':\n"
            "    state_path = Path(os.environ['GB10_FAKE_CLEANUP_STATE'])\n"
            "    state = json.loads(state_path.read_text())\n"
            "    objects = state['objects']; names = state['names']\n"
            "    if args[:3] == ['inspect', '--type', 'container']:\n"
            "        requested = args[-1]\n"
            "        identifier = names.get(requested, requested if requested in objects else '')\n"
            "        if not identifier or identifier not in objects: absent(requested)\n"
            "        json.dump([objects[identifier]], sys.stdout); raise SystemExit(0)\n"
            "    if args[:2] == ['stop', '--time'] and len(args) == 4:\n"
            "        identifier = args[-1]\n"
            "        if identifier not in objects: absent(identifier)\n"
            "        state['stopped'].append(identifier); state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "        print(identifier); raise SystemExit(0)\n"
            "    if args[:2] == ['rm', '-f'] and len(args) == 3:\n"
            "        identifier = args[-1]\n"
            "        item = objects.pop(identifier, None)\n"
            "        if item is None: absent(identifier)\n"
            "        for key, value in list(names.items()):\n"
            "            if value == identifier: del names[key]\n"
            "        state['removed'].append(identifier); state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "        print(identifier); raise SystemExit(0)\n"
            "    raise SystemExit(86)\n"
            "if args[:3] != ['inspect', '--type', 'container']:\n"
            "    raise SystemExit(86)\n"
            "name = args[-1]\n"
            "sequences = json.loads(os.environ['GB10_FAKE_INSPECT_SEQUENCES'])\n"
            "items = sequences.get(name, [None])\n"
            "state_path = Path(os.environ['GB10_FAKE_INSPECT_STATE'])\n"
            "state = json.loads(state_path.read_text()) if state_path.exists() else {}\n"
            "index = int(state.get(name, 0)); state[name] = index + 1\n"
            "state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "if index == 1:\n"
            "    for action in json.loads(os.environ.get('GB10_FAKE_SECOND_INSPECT_ACTIONS', '[]')):\n"
            "        path = Path(action['path'])\n"
            "        if action['op'] == 'write':\n"
            "            path.write_text(action['data'])\n"
            "        elif action['op'] == 'replace_dir':\n"
            "            backup = path.with_name(path.name + '.old')\n"
            "            if backup.exists(): shutil.rmtree(backup)\n"
            "            path.rename(backup); path.mkdir()\n"
            "            for filename, data in action['files'].items(): (path / filename).write_text(data)\n"
            "        else: raise SystemExit(95)\n"
            "item = items[min(index, len(items) - 1)]\n"
            "if item is None: absent(name)\n"
            "json.dump([item], sys.stdout)\n"
        )
        self.docker.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    def _write_systemctl(self) -> None:
        self.systemctl.write_text(
            "#!/usr/bin/python3\n"
            "import json, os, sys\n"
            "args = [arg for arg in sys.argv[1:] if arg != '--user']\n"
            "with open(os.environ['GB10_FAKE_COMMAND_LOG'], 'a') as sink:\n"
            "    sink.write('systemctl ' + ' '.join(args) + '\\n')\n"
            "if len(args) != 5 or args[:4] != ['show', '-p', 'ControlGroup', '--value']:\n"
            "    raise SystemExit(87)\n"
            "unit = args[-1]\n"
            "if not unit.startswith('docker-') or not unit.endswith('.scope'):\n"
            "    raise SystemExit(88)\n"
            "identifier = unit.removeprefix('docker-').removesuffix('.scope')\n"
            "scopes = json.loads(os.environ['GB10_FAKE_SCOPES'])\n"
            "if identifier not in scopes: raise SystemExit(89)\n"
            "sys.stdout.write(scopes[identifier] + '\\n')\n"
        )
        self.systemctl.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    def _test_environment(
        self,
        *,
        cgroup_version: str = "2",
        info_fail: bool = False,
        inspect_sequences: dict[str, list[dict[str, object] | None]] | None = None,
        second_inspect_actions: list[dict[str, object]] | None = None,
        docker_mode: str = "verify",
    ) -> dict[str, str]:
        return {
            "DOCKER_HOST": f"unix:///run/user/{os.getuid()}/docker.sock",
            "GB10_FAKE_CGROUP_VERSION": cgroup_version,
            "GB10_FAKE_COMMAND_LOG": str(self.command_log),
            "GB10_FAKE_CLEANUP_STATE": str(self.cleanup_state),
            "GB10_FAKE_DOCKER_MODE": docker_mode,
            "GB10_FAKE_INFO_FAIL": "1" if info_fail else "0",
            "GB10_FAKE_INSPECT_SEQUENCES": json.dumps(
                inspect_sequences
                if inspect_sequences is not None
                else {name: [self.containers[name]] for name in self.containers}
            ),
            "GB10_FAKE_INSPECT_STATE": str(self.inspect_state),
            "GB10_FAKE_SCOPES": json.dumps(
                {
                    self.identifiers[name]: scope
                    for name, scope in self.scopes.items()
                }
            ),
            "GB10_FAKE_SECOND_INSPECT_ACTIONS": json.dumps(
                second_inspect_actions or []
            ),
            "GB10_VLLM_NO_SWAP_CGROUP_ROOT": str(self.cgroup_root),
            "GB10_VLLM_NO_SWAP_DOCKER_BIN": str(self.docker),
            "GB10_VLLM_NO_SWAP_PROC_ROOT": str(self.proc_root),
            "GB10_VLLM_NO_SWAP_SYSTEMCTL_BIN": str(self.systemctl),
            "GB10_VLLM_NO_SWAP_WAIT_SECONDS": "1",
            "GB10_VLLM_NO_SWAP_COMMAND_TIMEOUT_SECONDS": "2",
        }

    def _run(
        self,
        *,
        wrapper: str | Path = VERIFIER,
        cwd: Path | None = None,
        containers: tuple[str, ...] = ("vllm-test",),
        units: tuple[Path, ...] | None = None,
        cgroup_version: str = "2",
        info_fail: bool = False,
        inspect_sequences: dict[str, list[dict[str, object] | None]] | None = None,
        second_inspect_actions: list[dict[str, object]] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = self._test_environment(
            cgroup_version=cgroup_version,
            info_fail=info_fail,
            inspect_sequences=inspect_sequences,
            second_inspect_actions=second_inspect_actions,
        )
        argv = [
            "/usr/bin/env",
            "-i",
            *[f"{key}={value}" for key, value in environment.items()],
            "/usr/bin/bash",
            "--noprofile",
            "--norc",
            str(wrapper),
            "--test-only",
        ]
        selected_units = units if units is not None else (self.unit,)
        for unit in selected_units:
            argv.extend(["--unit", str(unit)])
        for container in containers:
            argv.extend(["--container", container])
        return subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )

    def assert_rejected(self, **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = self._run(**kwargs)  # type: ignore[arg-type]
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gb10_vllm_no_swap:", result.stderr)
        return result
