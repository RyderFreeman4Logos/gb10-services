"""Hermetic fake systemd/Docker/cgroup/API fixture for embedding verification."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "systemd" / "vllm-embedding.service"
IMAGE = (
    "ghcr.io/aeon-7/aeon-vllm-ultimate@"
    "sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d"
)
MODELS = ("qwen3-embedding-8b", "Qwen/Qwen3-Embedding-8B")
CONTAINER_ID = "a" * 64
CURRENT_INVOCATION = "2" * 32
PREVIOUS_INVOCATION = "1" * 32


def _unit_argv() -> list[str]:
    import shlex

    lines: list[str] = []
    pending: list[str] = []
    for raw in UNIT.read_text().splitlines():
        line = raw.strip()
        if pending:
            value = line
        elif line.startswith("ExecStart="):
            value = line.removeprefix("ExecStart=")
        else:
            continue
        continued = value.endswith("\\")
        pending.append(value[:-1].rstrip() if continued else value)
        if not continued:
            lines = shlex.split(" ".join(pending))
            break
    if not lines:
        raise AssertionError("fixture could not parse canonical unit")
    return lines


def _vector(index: int) -> list[float]:
    result = [0.0] * 4096
    if index == 0:
        result[0] = 1.0
    elif index == 1:
        result[0] = 0.9
        result[1] = 0.1
    else:
        result[2] = 1.0
    return result


def embedding_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "data": [
            {"index": index, "embedding": _vector(index)} for index in range(3)
        ],
    }


class VerifierFixture:
    """Owner of one isolated verifier runtime with mutable hostile inputs."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source" / "systemd" / "vllm-embedding.service"
        self.installed = self.root / "home" / ".config/systemd/user/vllm-embedding.service"
        self.evidence = self.root / "evidence"
        self.proc = self.root / "proc"
        self.cgroup = self.root / "cgroup"
        self.fragment = self.installed
        self.state_path = self.root / "fixture.json"
        self.counter_path = self.root / "counters.json"
        self.tool = self.root / "fake-tool"
        for directory in (
            self.source.parent,
            self.installed.parent,
            self.evidence,
            self.proc,
            self.cgroup,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.evidence.chmod(0o700)
        canonical = UNIT.read_bytes()
        self.source.write_bytes(canonical)
        self.installed.write_bytes(canonical)
        self.source.chmod(0o644)
        self.installed.chmod(0o644)
        self._write_runtime_files()
        self.state = self._default_state()
        self.save()
        self._write_tool()
        self._write_baselines()

    def cleanup(self) -> None:
        self.temporary.cleanup()

    def __enter__(self) -> VerifierFixture:
        return self

    def __exit__(self, *_args: object) -> None:
        self.cleanup()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, sort_keys=True))
        self.counter_path.write_text("{}")

    def env(self) -> dict[str, str]:
        result = os.environ.copy()
        result["FIXTURE_STATE"] = str(self.state_path)
        result["FIXTURE_COUNTERS"] = str(self.counter_path)
        result["PYTHONDONTWRITEBYTECODE"] = "1"
        return result

    def config(self) -> Path:
        config = self.root / "test-config.json"
        config.write_text(
            json.dumps(
                {
                    "source_unit": str(self.source),
                    "installed_unit": str(self.installed),
                    "expected_fragment": str(self.fragment),
                    "proc_root": str(self.proc),
                    "cgroup_root": str(self.cgroup),
                    "systemctl": str(self.tool),
                    "docker": str(self.tool),
                    "journalctl": str(self.tool),
                    "curl": str(self.tool),
                    "deadline_seconds": 8,
                },
                sort_keys=True,
            )
        )
        return config

    def _write_runtime_files(self) -> None:
        service_cgroup = self.cgroup / "app.slice" / "vllm-embedding.service"
        docker_cgroup = self.cgroup / "app.slice" / f"docker-{CONTAINER_ID}.scope"
        for path in (service_cgroup, docker_cgroup):
            path.mkdir(parents=True)
            (path / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        (self.proc / "202").mkdir(parents=True)
        (self.proc / "202" / "cgroup").write_text(
            "0::/app.slice/vllm-embedding.service\n"
        )
        (self.proc / "303").mkdir(parents=True)
        (self.proc / "303" / "cgroup").write_text(
            f"0::/app.slice/docker-{CONTAINER_ID}.scope\n"
        )
        (docker_cgroup / "memory.max").write_text(str(128 * 1024**3) + "\n")
        (docker_cgroup / "memory.swap.max").write_text("0\n")
        (docker_cgroup / "memory.swap.current").write_text("0\n")
        (docker_cgroup / "memory.events").write_text(
            "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"
        )

    def _systemd_show(self) -> str:
        argv = _unit_argv()
        image_index = argv.index(IMAGE)
        effective = " ".join(argv)
        pre = "/usr/bin/docker rm -f vllm-embedding"
        post = (
            "/home/obj/.local/bin/gb10_service_ready.sh embedding "
            "http://100.105.4.92:18012 qwen3-embedding-8b --deadline 300"
        )
        stop = "/usr/bin/docker stop --time 20 vllm-embedding"
        rows = {
            "Id": "vllm-embedding.service",
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "FragmentPath": str(self.fragment),
            "DropInPaths": "",
            "MainPID": "202",
            "ControlGroup": "/app.slice/vllm-embedding.service",
            "InvocationID": CURRENT_INVOCATION,
            "ExecMainStartTimestampMonotonic": "2000000",
            "ExecStart": (
                f"{{ path={argv[0]} ; argv[]={effective} ; ignore_errors=no ; }}"
            ),
            "ExecStartPre": (
                f"{{ path=/usr/bin/docker ; argv[]={pre} ; ignore_errors=yes ; }}"
            ),
            "ExecStartPost": (
                f"{{ path=/home/obj/.local/bin/gb10_service_ready.sh ; "
                f"argv[]={post} ; ignore_errors=no ; }}"
            ),
            "ExecStop": (
                f"{{ path=/usr/bin/docker ; argv[]={stop} ; ignore_errors=no ; }}"
            ),
        }
        if image_index <= 2:
            raise AssertionError("fixture has no Docker host/image boundary")
        return "".join(f"{key}={value}\n" for key, value in rows.items())

    def _default_state(self) -> dict[str, Any]:
        argv = _unit_argv()
        image_index = argv.index(IMAGE)
        container_argv = argv[image_index + 1 :]
        systemd = self._systemd_show()
        container = {
            "Id": CONTAINER_ID,
            "Name": "/vllm-embedding",
            "State": {
                "Running": True,
                "Pid": 303,
                "StartedAt": "2026-07-14T12:00:00.000000000Z",
            },
            "Config": {
                "Image": IMAGE,
                "Entrypoint": ["python3"],
                "Cmd": container_argv,
            },
            "HostConfig": {
                "Memory": 128 * 1024**3,
                "MemorySwap": 128 * 1024**3,
                "MemorySwappiness": 0,
                "OomScoreAdj": 0,
                "AutoRemove": True,
                "CgroupParent": "",
                "PortBindings": {"8000/tcp": [{"HostIp": "100.105.4.92", "HostPort": "18012"}]},
            },
        }
        journal = {
            "_SYSTEMD_INVOCATION_ID": CURRENT_INVOCATION,
            "_PID": "404",
            "__MONOTONIC_TIMESTAMP": "2000100",
            "MESSAGE": "(EngineCore_DP0 pid=404) GPU KV cache size: 34,124 tokens",
        }
        return {
            "systemd_outputs": [systemd, systemd],
            "docker_outputs": [[container], [container]],
            "journal": [journal],
            "models": {"data": [{"id": model} for model in MODELS]},
            "embeddings": {model: embedding_payload(model) for model in MODELS},
            "command_log": str(self.root / "commands.log"),
        }

    def _write_baselines(self) -> None:
        (self.evidence / "systemd.before.json").write_text(
            json.dumps(
                {
                    "InvocationID": PREVIOUS_INVOCATION,
                    "MainPID": 101,
                    "ExecMainStartTimestampMonotonic": 1000000,
                },
                sort_keys=True,
            )
        )
        (self.evidence / "baselines.json").write_text(
            json.dumps(
                {
                    "aliases": {
                        model: embedding_payload(model) for model in MODELS
                    }
                },
                sort_keys=True,
            )
        )
        for path in self.evidence.iterdir():
            path.chmod(0o600)

    def _write_tool(self) -> None:
        self.tool.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "state = json.loads(Path(os.environ['FIXTURE_STATE']).read_text())\n"
            "counter_path = Path(os.environ['FIXTURE_COUNTERS'])\n"
            "counters = json.loads(counter_path.read_text())\n"
            "args = sys.argv[1:]\n"
            "with open(state['command_log'], 'a') as sink:\n"
            "    sink.write(' '.join(args) + '\\n')\n"
            "def selected(name, outputs):\n"
            "    index = counters.get(name, 0)\n"
            "    counters[name] = index + 1\n"
            "    counter_path.write_text(json.dumps(counters))\n"
            "    return outputs[min(index, len(outputs) - 1)]\n"
            "if args and args[0] == 'inspect':\n"
            "    print(json.dumps(selected('docker', state['docker_outputs'])))\n"
            "elif 'show' in args:\n"
            "    sys.stdout.write(selected('systemd', state['systemd_outputs']))\n"
            "elif '-o' in args and 'json' in args:\n"
            "    for row in state['journal']:\n"
            "        print(json.dumps(row))\n"
            "elif args and args[-1].endswith('/v1/models'):\n"
            "    print(json.dumps(state['models']))\n"
            "elif args and args[-1].endswith('/v1/embeddings'):\n"
            "    request = json.load(sys.stdin)\n"
            "    print(json.dumps(state['embeddings'][request['model']]))\n"
            "else:\n"
            "    print('unexpected fake command: ' + ' '.join(args), file=sys.stderr)\n"
            "    raise SystemExit(17)\n"
        )
        self.tool.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
