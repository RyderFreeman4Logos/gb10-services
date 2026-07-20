"""Hermetic process-level fixture for durable embedding activation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UNIT = "vllm-embedding.service"
ACTIVATOR = ROOT / "scripts" / "gb10_activate_embedding_profile.sh"
ACTIVATION_ENGINE = ROOT / "scripts" / "gb10_embedding_activation.py"
CANONICAL_UNIT = ROOT / "systemd" / UNIT
MODELS = ("qwen3-embedding-8b", "Qwen/Qwen3-Embedding-8B")
NEIGHBORS = (
    "vllm-aeon-27b-dflash.service",
    "vllm-querit-4b-reranker.service",
    "vllm-qwen3-reranker-8b.service",
)
CONTAINER_ID = "a" * 64


def _tool_source() -> str:
    return r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

state_path = Path(os.environ["ACTIVATION_FIXTURE_STATE"])
log_path = Path(os.environ["ACTIVATION_COMMAND_LOG"])
state = json.loads(state_path.read_text())
args = [arg for arg in sys.argv[1:] if arg != "--user"]
with log_path.open("a") as sink:
    sink.write("tool " + " ".join(args) + "\n")

def save():
    state_path.write_text(json.dumps(state, sort_keys=True))

if args == ["info", "--format", "{{.CgroupVersion}}"]:
    if state.get("docker_info_fail"):
        raise SystemExit(73)
    print(state.get("cgroup_version", "2"))
    raise SystemExit(0)

if args == ["inspect", "--type", "container", "vllm-embedding"]:
    print(json.dumps([state["container"]]))
    raise SystemExit(0)

scope_unit = f"docker-{state.get('container', {}).get('Id', '')}.scope"
if args == ["show", "-p", "ControlGroup", "--value", scope_unit]:
    print(state["docker_scope"])
    raise SystemExit(0)

if args and args[0] in {"show", "daemon-reload", "restart", "stop"}:
    action = args[0]
    if state.get("hang_once") == action:
        state["hang_once"] = ""
        save()
        child = subprocess.Popen(["/usr/bin/sleep", "30"])
        Path(state["child_pid_path"]).write_text(str(child.pid))
        time.sleep(30)
    if action == "daemon-reload":
        installed = Path(state["installed_unit"])
        if installed.exists():
            metadata = installed.stat()
            snapshot = {
                "inode": metadata.st_ino,
                "mode": metadata.st_mode & 0o777,
                "payload": installed.read_bytes().hex(),
            }
        else:
            snapshot = {"inode": None, "mode": None, "payload": None}
        state.setdefault("daemon_reload_units", []).append(snapshot)
        save()
        raise SystemExit(0)
    if action == "restart":
        if len(args) != 2 or args[1] != "vllm-embedding.service":
            raise SystemExit(91)
        count = int(state.get("restart_count", 0))
        state["restart_count"] = count + 1
        if state.get("fail_rollback_restart") and count >= 1:
            save()
            raise SystemExit(44)
        if count >= 1:
            state["ready_timeout"] = False
            state["same_generation"] = bool(state.get("rollback_same_generation"))
        if not state.get("same_generation"):
            state["generation"] = int(state.get("generation", 0)) + 1
        state["active"] = True
        if state.get("mutate_neighbor"):
            state["neighbors"]["vllm-aeon-27b-dflash.service"]["NRestarts"] = "8"
        save()
        raise SystemExit(0)
    if action == "stop":
        if len(args) != 2 or args[1] != "vllm-embedding.service":
            raise SystemExit(92)
        state["active"] = False
        state["generation"] = int(state.get("generation", 0)) + 1
        save()
        raise SystemExit(0)
    unit = args[1]
    properties = [arg.split("=", 1)[1] for arg in args if arg.startswith("--property=")]
    if unit == "vllm-embedding.service":
        generation = int(state.get("generation", 0))
        active = bool(state.get("active", True))
        timeout = generation > 0 and state.get("ready_timeout")
        running = active and not timeout
        fields = {
            "LoadState": "loaded",
            "ActiveState": "active" if running else ("activating" if timeout else "inactive"),
            "SubState": "running" if running else ("start-pre" if timeout else "dead"),
            "FragmentPath": state["installed_unit"],
            "MainPID": str(101 + 101 * generation) if running else "0",
            "ControlGroup": "/app.slice/vllm-embedding.service" if running else "",
            "InvocationID": f"{generation + 1:032x}" if running else "",
            "ExecMainStartTimestampMonotonic": str(100 + 100 * generation) if running else "0",
        }
    else:
        fields = state["neighbors"].get(unit)
        if fields is None:
            raise SystemExit(93)
    for key in properties:
        if key not in fields:
            raise SystemExit(94)
        print(f"{key}={fields[key]}")
    raise SystemExit(0)

url = args[-1] if args else ""
if url.endswith("/v1/models"):
    if (
        state.get("drift_after_models")
        and int(state.get("restart_count", 0)) == 1
    ):
        state["generation"] = int(state.get("generation", 0)) + 1
        save()
    print(json.dumps({"data": [{"id": "qwen3-embedding-8b"}, {"id": "Qwen/Qwen3-Embedding-8B"}]}))
    raise SystemExit(0)
if url.endswith("/v1/embeddings"):
    request = json.load(sys.stdin)
    rows = []
    for index in range(3):
        vector = [0.0] * 4096
        if index == 0:
            vector[0] = 1.0
        elif index == 1:
            vector[0] = 0.9
            vector[1] = 0.1
        else:
            vector[2] = 1.0
        rows.append({"index": index, "embedding": vector})
    print(json.dumps({"model": request["model"], "data": rows}))
    raise SystemExit(0)
raise SystemExit(95)
'''


def _verifier_source() -> str:
    return r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
with open(os.environ["ACTIVATION_COMMAND_LOG"], "a") as sink:
    sink.write("verifier argc=" + str(len(sys.argv) - 1) + " " + " ".join(sys.argv[1:]) + "\n")
state = json.loads(Path(os.environ["ACTIVATION_FIXTURE_STATE"]).read_text())
if state.get("verify_status"):
    raise SystemExit(int(state["verify_status"]))
evidence = Path(sys.argv[1])
receipt = evidence / "verification.receipt.json"
receipt.write_text(json.dumps({"verification": "passed", "profile": "test-only"}, sort_keys=True) + "\n")
receipt.chmod(0o600)
'''


def _no_swap_core_source() -> str:
    return r'''from __future__ import annotations

import json
import os
import sys
from pathlib import Path
state_path = Path(os.environ["ACTIVATION_FIXTURE_STATE"])
log_path = Path(os.environ["ACTIVATION_COMMAND_LOG"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with log_path.open("a") as sink:
    sink.write("no-swap " + " ".join(args) + " core=" + str(Path(__file__).resolve()) + "\n")
if not args or args[0] != "--test-only":
    raise SystemExit(81)
args = args[1:]
if args.count("--unit") != 1 or args.count("--container") != 1:
    raise SystemExit(82)
if args[args.index("--container") + 1] != "vllm-embedding":
    raise SystemExit(83)
count = int(state.get("no_swap_count", 0)) + 1
state["no_swap_count"] = count
state_path.write_text(json.dumps(state, sort_keys=True))
if int(state.get("fail_no_swap_at", 0)) == count:
    raise SystemExit(84)
'''


def _no_swap_helper_source(core: bytes) -> str:
    digest = hashlib.sha256(core).hexdigest()
    return f'''#!/usr/bin/env bash
set -euo pipefail
CORE_BASENAME=gb10_verify_vllm_no_swap_core.py
EXPECTED_CORE_SHA256={digest}
/usr/bin/python3 -I - "${{BASH_SOURCE[0]}}" "$EXPECTED_CORE_SHA256" "$@" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path
wrapper = Path(sys.argv[1]).resolve(strict=True)
core = wrapper.parent / "gb10_verify_vllm_no_swap_core.py"
descriptor = os.open(core, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
try:
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) not in (0o600, 0o644)):
        raise SystemExit(85)
    payload = b""
    while chunk := os.read(descriptor, 65536):
        payload += chunk
finally:
    os.close(descriptor)
if hashlib.sha256(payload).hexdigest() != sys.argv[2]:
    raise SystemExit(86)
sys.argv = [str(core), *sys.argv[3:]]
exec(compile(payload, str(core), "exec"), {{"__name__": "__main__", "__file__": str(core)}})
PY
'''


class ActivationFixture:
    def __init__(
        self,
        *,
        prior_present: bool = True,
        prior_helper_present: bool = True,
        prior_core_present: bool | None = None,
    ) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_unit = self.root / "source/systemd" / UNIT
        self.source_helper = self.root / "source/scripts/gb10_verify_vllm_no_swap.sh"
        self.source_core = self.root / "source/scripts/gb10_verify_vllm_no_swap_core.py"
        self.unit_dir = self.root / "home/.config/systemd/user"
        self.installed_unit = self.unit_dir / UNIT
        self.installed_helper = self.root / "home/.local/bin/gb10_verify_vllm_no_swap.sh"
        self.installed_core = self.root / "home/.local/bin/gb10_verify_vllm_no_swap_core.py"
        self.state_root = self.root / "home/.local/state/gb10-embedding-activation"
        self.bin_dir = self.root / "bin"
        self.config = self.root / "activation-test.json"
        self.state_path = self.root / "fixture-state.json"
        self.command_log = self.root / "commands.log"
        self.marker = self.root / "pause.marker"
        self.release = self.root / "pause.release"
        self.child_pid_path = self.root / "child.pid"
        self.cgroup_root = self.root / "cgroup"
        for directory, mode in (
            (self.source_unit.parent, 0o755),
            (self.source_helper.parent, 0o755),
            (self.unit_dir, 0o755),
            (self.installed_helper.parent, 0o755),
            (self.state_root.parent, 0o700),
            (self.bin_dir, 0o755),
            (self.cgroup_root, 0o755),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(mode)
        self.source_unit.write_bytes(CANONICAL_UNIT.read_bytes())
        self.source_unit.chmod(0o644)
        source_core = _no_swap_core_source().encode()
        self.source_core.write_bytes(source_core)
        self.source_core.chmod(0o644)
        self.source_helper.write_text(_no_swap_helper_source(source_core))
        self.source_helper.chmod(0o755)
        self.prior_helper_bytes = b"#!/usr/bin/python3\nraise SystemExit(77)\n"
        self.prior_helper_mode = 0o700
        if prior_helper_present:
            self.installed_helper.write_bytes(self.prior_helper_bytes)
            self.installed_helper.chmod(self.prior_helper_mode)
        self.prior_core_bytes = b"# prior verifier core fixture\n"
        self.prior_core_mode = 0o640
        if prior_core_present is None:
            prior_core_present = prior_helper_present
        if prior_core_present:
            self.installed_core.write_bytes(self.prior_core_bytes)
            self.installed_core.chmod(self.prior_core_mode)
        self.prior_bytes = CANONICAL_UNIT.read_bytes() + b"\n# prior fixture generation\n"
        self.prior_mode = 0o640
        if prior_present:
            self.installed_unit.write_bytes(self.prior_bytes)
            self.installed_unit.chmod(self.prior_mode)
        self.tool = self.bin_dir / "tool"
        self.tool.write_text(_tool_source())
        self.tool.chmod(0o755)
        self.verifier = self.bin_dir / "verify"
        self.verifier.write_text(_verifier_source())
        self.verifier.chmod(0o755)
        docker_scope = f"/app.slice/docker-{CONTAINER_ID}.scope"
        docker_cgroup = self.cgroup_root / docker_scope.removeprefix("/")
        docker_cgroup.mkdir(parents=True)
        (docker_cgroup / "memory.swap.max").write_text("0\n")
        (docker_cgroup / "memory.swap.current").write_text("0\n")
        neighbor_states = {}
        for index, unit in enumerate(NEIGHBORS):
            running = index != 1
            neighbor_states[unit] = {
                "LoadState": "loaded",
                "ActiveState": "active" if running else "inactive",
                "SubState": "running" if running else "dead",
                "MainPID": str(500 + index) if running else "0",
                "NRestarts": str(index),
                "InvocationID": str(index + 4) * 32 if running else "",
                "ExecMainStartTimestampMonotonic": str(1000 + index) if running else "0",
            }
        self.state: dict[str, Any] = {
            "active": True,
            "child_pid_path": str(self.child_pid_path),
            "daemon_reload_units": [],
            "cgroup_version": "2",
            "docker_info_fail": False,
            "drift_after_models": False,
            "fail_rollback_restart": False,
            "generation": 0,
            "fail_no_swap_at": 0,
            "hang_once": "",
            "installed_unit": str(self.installed_unit),
            "installed_no_swap_helper": str(self.installed_helper),
            "installed_no_swap_core": str(self.installed_core),
            "docker_scope": docker_scope,
            "container": {
                "Id": CONTAINER_ID,
                "Name": "/vllm-embedding",
                "State": {"Pid": 303, "Running": True},
                "HostConfig": {
                    "Memory": 128 * 1024**3,
                    "MemorySwap": 128 * 1024**3,
                },
                "Config": {
                    "Cmd": [
                        "/usr/local/bin/vllm",
                        "serve",
                        "Qwen/Qwen3-Embedding-8B",
                    ]
                },
            },
            "mutate_neighbor": False,
            "no_swap_count": 0,
            "neighbors": neighbor_states,
            "ready_timeout": False,
            "restart_count": 0,
            "rollback_same_generation": False,
            "same_generation": False,
            "verify_status": 0,
        }
        self.hooks = {"fail_at": "", "pause_at": ""}
        self.save()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, sort_keys=True))
        payload = {
            "command_seconds": 2,
            "curl": str(self.tool),
            "docker": str(self.tool),
            "deadline_seconds": 12,
            "fail_at": self.hooks["fail_at"],
            "installed_unit": str(self.installed_unit),
            "installed_no_swap_helper": str(self.installed_helper),
            "installed_no_swap_core": str(self.installed_core),
            "marker": str(self.marker),
            "pause_at": self.hooks["pause_at"],
            "ready_seconds": 2,
            "release": str(self.release),
            "rollback_seconds": 8,
            "source_unit": str(self.source_unit),
            "source_no_swap_helper": str(self.source_helper),
            "source_no_swap_core": str(self.source_core),
            "state_root": str(self.state_root),
            "systemctl": str(self.tool),
            "unit_dir": str(self.unit_dir),
            "verifier": str(self.verifier),
            "verifier_authority": [str(self.verifier)],
        }
        self.config.write_text(json.dumps(payload, sort_keys=True))
        self.config.chmod(0o600)

    def env(self, *, optimized: bool = False) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "ACTIVATION_COMMAND_LOG": str(self.command_log),
                "ACTIVATION_FIXTURE_STATE": str(self.state_path),
                "DOCKER_HOST": f"unix:///run/user/{os.getuid()}/docker.sock",
                "GB10_VLLM_NO_SWAP_CGROUP_ROOT": str(self.cgroup_root),
                "GB10_VLLM_NO_SWAP_DOCKER_BIN": str(self.tool),
                "GB10_VLLM_NO_SWAP_SYSTEMCTL_BIN": str(self.tool),
                "GB10_VLLM_NO_SWAP_WAIT_SECONDS": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if optimized:
            env["PYTHONOPTIMIZE"] = "1"
        else:
            env.pop("PYTHONOPTIMIZE", None)
        return env

    def run(self, *, optimized: bool = False, timeout: float = 20) -> subprocess.CompletedProcess[str]:
        self.save()
        argv = ["/usr/bin/python3", "-I", "-B", "-S"]
        if optimized:
            argv.append("-O")
        argv.extend([str(ACTIVATION_ENGINE), "--test-only", str(self.config)])
        return subprocess.run(
            argv,
            env=self.env(optimized=optimized),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def spawn(self, *, optimized: bool = False) -> subprocess.Popen[str]:
        self.save()
        argv = ["/usr/bin/python3", "-I", "-B", "-S"]
        if optimized:
            argv.append("-O")
        argv.extend([str(ACTIVATION_ENGINE), "--test-only", str(self.config)])
        return subprocess.Popen(
            argv,
            env=self.env(optimized=optimized),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def wait_for_marker(self, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.marker.exists():
            time.sleep(0.02)
        if not self.marker.exists():
            raise AssertionError("activation did not reach the test-only pause boundary")

    def transaction(self) -> Path:
        return self.state_root / "transaction.v1"

    def log(self) -> str:
        return self.command_log.read_text() if self.command_log.exists() else ""

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.temporary.cleanup()

    def __enter__(self) -> "ActivationFixture":
        return self

    def __exit__(self, *_args: object) -> None:
        self.cleanup()
