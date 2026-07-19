#!/usr/bin/env python3
"""Source-controlled install and rollback owner for the Querit canary.

The lifecycle module owns candidate service transitions.  This module owns the
surrounding deployed bytes, runtime masks, and artifact publication so an
operator never has to unmask or hand-start a canary unit outside a receipt.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

import querit_canary_runtime as runtime
import querit_vllm_artifact as artifact

__all__ = [
    "Deployment",
    "DeploymentError",
    "ProfileError",
    "candidate_budget_kib_for_utilization",
    "main",
    "validate_admission",
    "validate_backend_unit",
]


BUNDLE_SCHEMA = "gb10-querit-canary-bundle-v1"
PLAN_SCHEMA = "gb10-querit-canary-plan-v1"
STATE_SCHEMA = "gb10-querit-canary-deployment-v1"
DEFAULT_STATE = Path("/home/obj/.local/state/gb10-querit-canary-deployment/state.json")
DEFAULT_ARTIFACT = Path("/home/obj/models/querit-4b-vllm")
DEFAULT_LIFECYCLE_STATE = Path("/home/obj/.local/state/gb10-querit-canary/state.json")
LIB_ROOT = Path("/home/obj/.local/lib/gb10")
BIN_ROOT = Path("/home/obj/.local/bin")
UNIT_ROOT = Path("/home/obj/.config/systemd/user")
RUNTIME_UNIT_ROOT = Path("/run/user/1001/systemd/user")
CANDIDATE_UNITS = (runtime.BACKEND_UNIT, runtime.ADAPTER_UNIT)
KIB_PER_GIB = 1024 * 1024
GPU_MEMORY_ENVELOPE_KIB = 128 * KIB_PER_GIB
GPU_MEMORY_UTILIZATION_NUMERATOR = 17
GPU_MEMORY_UTILIZATION_DENOMINATOR = 100
GPU_MEMORY_UTILIZATION = "0.17"
CONVERTER_IMAGE = (
    "ghcr.io/aeon-7/aeon-vllm-ultimate@"
    "sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d"
)
OBSERVED_MEMAVAILABLE_MINIMUM_KIB = 57_246_636
RESERVE_KIB = 20 * KIB_PER_GIB
UNCERTAINTY_MARGIN_KIB = 2 * KIB_PER_GIB
MAX_CANDIDATE_BUDGET_KIB = 34_177_964
MODEL_DIR = "/home/obj/models/querit-4b-vllm"
SERVED_MODEL_NAMES = (
    "qwen3-reranker-8b",
    "Qwen/Qwen3-Reranker-8B",
    "Querit/Querit-4B",
)
RUNNER = "pooling"
DTYPE = "bfloat16"
MAX_MODEL_LEN = 32_768
MAX_NUM_BATCHED_TOKENS = 1_024
MAX_NUM_SEQS = 1


class DeploymentError(RuntimeError):
    """The source-controlled deployment contract was not satisfied."""


class ProfileError(ValueError):
    """The source-controlled canary profile is incomplete or inconsistent."""


def candidate_budget_kib_for_utilization(numerator: int, denominator: int) -> int:
    """Return the conservative whole-KiB vLLM GPU-allocation budget."""

    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or numerator <= 0
        or denominator <= 0
        or numerator > denominator
    ):
        raise ProfileError("GPU memory utilization ratio is invalid")
    return (
        GPU_MEMORY_ENVELOPE_KIB * numerator + denominator - 1
    ) // denominator


CANDIDATE_STARTUP_BUDGET_KIB = candidate_budget_kib_for_utilization(
    GPU_MEMORY_UTILIZATION_NUMERATOR,
    GPU_MEMORY_UTILIZATION_DENOMINATOR,
)
REQUIRED_ADMISSION_KIB = (
    CANDIDATE_STARTUP_BUDGET_KIB + RESERVE_KIB + UNCERTAINTY_MARGIN_KIB
)

_VLLM_OPTION_ARITIES = {
    "--host": 1,
    "--port": 1,
    "--served-model-name": 3,
    "--runner": 1,
    "--dtype": 1,
    "--max-model-len": 1,
    "--gpu-memory-utilization": 1,
    "--kv-cache-memory-bytes": 1,
    "--kv-cache-dtype": 1,
    "--tensor-parallel-size": 1,
    "--pipeline-parallel-size": 1,
    "--swap-space": 1,
    "--cpu-offload-gb": 1,
    "--max-num-batched-tokens": 1,
    "--max-num-seqs": 1,
    "--enable-chunked-prefill": 0,
    "--max-num-partial-prefills": 1,
    "--max-long-partial-prefills": 1,
    "--long-prefill-token-threshold": 1,
    "--enforce-eager": 0,
    "--chat-template": 1,
}
_VLLM_OPTION_VALUES = {
    "--host": ("0.0.0.0",),
    "--port": ("8000",),
    "--served-model-name": SERVED_MODEL_NAMES,
    "--runner": (RUNNER,),
    "--dtype": (DTYPE,),
    "--max-model-len": (str(MAX_MODEL_LEN),),
    "--gpu-memory-utilization": (GPU_MEMORY_UTILIZATION,),
    "--kv-cache-memory-bytes": ("4800M",),
    "--kv-cache-dtype": ("auto",),
    "--tensor-parallel-size": ("1",),
    "--pipeline-parallel-size": ("1",),
    "--swap-space": ("0",),
    "--cpu-offload-gb": ("0",),
    "--max-num-batched-tokens": (str(MAX_NUM_BATCHED_TOKENS),),
    "--max-num-seqs": (str(MAX_NUM_SEQS),),
    "--enable-chunked-prefill": (),
    "--max-num-partial-prefills": ("1",),
    "--max-long-partial-prefills": ("1",),
    "--long-prefill-token-threshold": ("8192",),
    "--enforce-eager": (),
    "--chat-template": (f"{MODEL_DIR}/querit-rerank.jinja",),
}


def _logical_exec_start(unit: str) -> list[str]:
    commands: list[list[str]] = []
    pending: list[str] = []
    for raw_line in unit.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if pending:
            value = line
        elif line.startswith("ExecStart="):
            value = line.removeprefix("ExecStart=")
        else:
            continue
        continued = value.endswith("\\")
        if continued:
            value = value[:-1].rstrip()
        pending.append(value)
        if not continued:
            commands.append(shlex.split(" ".join(pending), posix=True))
            pending = []
    if pending or len(commands) != 1:
        raise ProfileError("canary backend must have exactly one complete ExecStart")
    return commands[0]


def _backend_options(unit: str) -> dict[str, tuple[str, ...]]:
    argv = _logical_exec_start(unit)
    try:
        vllm_index = argv.index("/usr/local/bin/vllm")
    except ValueError as exc:
        raise ProfileError("canary backend is missing the vLLM executable") from exc
    if argv[vllm_index : vllm_index + 3] != [
        "/usr/local/bin/vllm",
        "serve",
        MODEL_DIR,
    ]:
        raise ProfileError("canary backend vLLM command or model path is invalid")
    options: dict[str, tuple[str, ...]] = {}
    index = vllm_index + 3
    while index < len(argv):
        option = argv[index]
        arity = _VLLM_OPTION_ARITIES.get(option)
        if arity is None:
            raise ProfileError(f"unknown vLLM option: {option}")
        if option in options:
            raise ProfileError(f"duplicate vLLM option: {option}")
        values = tuple(argv[index + 1 : index + 1 + arity])
        if len(values) != arity or any(value.startswith("--") for value in values):
            raise ProfileError(f"vLLM option has invalid arity: {option}")
        options[option] = values
        index += 1 + arity
    return options


def validate_backend_unit(unit: str) -> None:
    """Reject any unit whose explicit vLLM memory profile differs from this one."""

    if _backend_options(unit) != _VLLM_OPTION_VALUES:
        raise ProfileError("vLLM option set does not match profile authority")
    if CANDIDATE_STARTUP_BUDGET_KIB > MAX_CANDIDATE_BUDGET_KIB:
        raise ProfileError("candidate startup budget exceeds the measured envelope")
    if REQUIRED_ADMISSION_KIB > OBSERVED_MEMAVAILABLE_MINIMUM_KIB:
        raise ProfileError("candidate admission exceeds the observed memory minimum")


def validate_admission(admission: Mapping[str, object]) -> None:
    """Require the exact profile-derived admission floor before any mutation."""

    available = admission.get("mem_available_kib")
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise ProfileError("candidate admission MemAvailable is invalid")
    if available < REQUIRED_ADMISSION_KIB:
        raise ProfileError(
            "candidate admission is below profile threshold: "
            f"{available} KiB < {REQUIRED_ADMISSION_KIB} KiB"
        )
    for key in ("pressure_some_avg10", "pressure_full_avg10"):
        value = admission.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ProfileError(f"candidate admission {key} is invalid")
        if value != 0:
            raise ProfileError(f"candidate admission {key} is nonzero")
    pswpout = admission.get("pswpout")
    if isinstance(pswpout, bool) or not isinstance(pswpout, int) or pswpout < 0:
        raise ProfileError("candidate admission pswpout is invalid")
    swap_topology = admission.get("swap_topology_sha256")
    if (
        not isinstance(swap_topology, str)
        or len(swap_topology) != 64
        or any(character not in "0123456789abcdef" for character in swap_topology)
    ):
        raise ProfileError("candidate admission swap topology is invalid")


def _pressure_avg10(pressure: str) -> dict[str, float]:
    rows: dict[str, float] = {}
    for line in pressure.splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            values = dict(field.split("=", 1) for field in fields[1:])
            rows[fields[0]] = float(values["avg10"])
        except (KeyError, ValueError) as exc:
            raise ValueError("memory pressure data is malformed") from exc
    if set(rows) != {"some", "full"}:
        raise ValueError("memory pressure data is incomplete")
    return rows


def _pswpout(vmstat: str) -> int:
    values = [line.split() for line in vmstat.splitlines() if line.startswith("pswpout ")]
    if len(values) != 1 or len(values[0]) != 2:
        raise ValueError("vmstat pswpout data is malformed")
    try:
        value = int(values[0][1])
    except ValueError as exc:
        raise ValueError("vmstat pswpout data is malformed") from exc
    if value < 0:
        raise ValueError("vmstat pswpout data is invalid")
    return value


def _swap_topology_sha256(swaps: str) -> str:
    lines = [line.split() for line in swaps.splitlines()]
    if not lines or lines[0] != ["Filename", "Type", "Size", "Used", "Priority"]:
        raise ValueError("swap data header is malformed")
    topology: list[tuple[str, str, int, int]] = []
    for fields in lines[1:]:
        if len(fields) != 5:
            raise ValueError("swap data row is malformed")
        try:
            size = int(fields[2])
            used = int(fields[3])
            priority = int(fields[4])
        except ValueError as exc:
            raise ValueError("swap data row is malformed") from exc
        if size < 0 or used < 0 or used > size:
            raise ValueError("swap data row is invalid")
        topology.append((fields[0], fields[1], size, priority))
    return _sha256(_canonical(topology))


class Host(Protocol):
    def require_cgroup_v2(self) -> None: ...

    def unit_info(self, unit: str) -> dict[str, str]: ...

    def service_state(self, unit: str) -> runtime.ServiceState: ...

    def runtime_mask(self, unit: str) -> None: ...

    def runtime_unmask(self, unit: str) -> None: ...

    def runtime_mask_attestation(self, unit: str) -> dict[str, object] | None: ...

    def daemon_reload(self) -> None: ...

    def listeners(self) -> tuple[str, ...]: ...

    def container(self, name: str) -> dict[str, str] | None: ...

    def admission(self) -> dict[str, object]: ...

    def convert(self, converter: Path, snapshot: Path, template: Path) -> None: ...

    def lifecycle(self, action: str, *, pause_text: bool) -> None: ...

    def lifecycle_state_exists(self) -> bool: ...


class SystemHost:
    """The only live-operation adapter used by the owner transaction."""

    def __init__(
        self,
        *,
        model_root: Path = DEFAULT_ARTIFACT,
        lifecycle_state: Path = DEFAULT_LIFECYCLE_STATE,
    ) -> None:
        self._runtime = runtime.SystemHost(model_root)
        self.lifecycle_state = lifecycle_state

    def require_cgroup_v2(self) -> None:
        self._runtime.require_cgroup_v2()

    @staticmethod
    def _run(arguments: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeploymentError("command failed: " + " ".join(arguments)) from exc

    def unit_info(self, unit: str) -> dict[str, str]:
        completed = self._run(
            [
                "/usr/bin/systemctl",
                "--user",
                "show",
                unit,
                "--property=FragmentPath",
                "--property=DropInPaths",
                "--property=UnitFileState",
                "--property=LoadState",
            ]
        )
        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        expected = {"FragmentPath", "DropInPaths", "UnitFileState", "LoadState"}
        if set(values) != expected:
            raise DeploymentError(f"incomplete unit metadata for {unit}")
        return values

    def service_state(self, unit: str) -> runtime.ServiceState:
        return self._runtime.service_state(unit)

    def runtime_mask(self, unit: str) -> None:
        self._run(["/usr/bin/systemctl", "--user", "mask", "--runtime", unit])

    def runtime_unmask(self, unit: str) -> None:
        self._run(["/usr/bin/systemctl", "--user", "unmask", "--runtime", unit])

    def runtime_mask_attestation(self, unit: str) -> dict[str, object] | None:
        path = _runtime_mask_path(unit)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DeploymentError(f"cannot lstat runtime mask for {unit}") from exc
        if not stat.S_ISLNK(metadata.st_mode):
            raise DeploymentError(f"runtime mask is not a symlink for {unit}")
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise DeploymentError(f"cannot read runtime mask target for {unit}") from exc
        if target != "/dev/null":
            raise DeploymentError(f"runtime mask target is not /dev/null for {unit}")
        return {
            "scope": "runtime",
            "path": str(path),
            "lstat": {
                "st_dev": metadata.st_dev,
                "st_ino": metadata.st_ino,
                "st_mode": metadata.st_mode,
                "type": "symlink",
            },
            "link_target": target,
        }

    def daemon_reload(self) -> None:
        self._run(["/usr/bin/systemctl", "--user", "daemon-reload"])

    def listeners(self) -> tuple[str, ...]:
        completed = self._run(["/usr/bin/ss", "-H", "-ltnp"])
        return tuple(
            line
            for line in completed.stdout.splitlines()
            if ":18014" in line or ":18015" in line
        )

    def container(self, name: str) -> dict[str, str] | None:
        listed = self._run(
            [
                "/usr/bin/docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"name=^/{name}$",
            ],
            timeout=30,
        )
        ids = listed.stdout.splitlines()
        if not ids:
            return None
        if len(ids) != 1 or len(ids[0]) != 64:
            raise DeploymentError(f"candidate container lookup is ambiguous for {name}")
        inspected = self._run(
            [
                "/usr/bin/docker",
                "container",
                "inspect",
                "--format",
                "{{.Id}}|{{.Image}}|{{.Config.Image}}|{{.State.Pid}}|{{.State.Running}}",
                ids[0],
            ],
            timeout=30,
        ).stdout.strip().split("|")
        if len(inspected) != 5:
            raise DeploymentError(f"candidate container metadata is malformed for {name}")
        return dict(zip(("id", "image", "config_image", "pid", "running"), inspected, strict=True))

    def admission(self) -> dict[str, object]:
        try:
            mem_available_kib = self._runtime.memory_available_kib()
            mem_available = mem_available_kib // KIB_PER_GIB
            swaps = Path("/proc/swaps").read_text()
            pressure = Path("/proc/pressure/memory").read_text()
            pressure_avg10 = _pressure_avg10(pressure)
            swap_out = _pswpout(Path("/proc/vmstat").read_text())
            swap_topology_sha256 = _swap_topology_sha256(swaps)
        except (OSError, UnicodeError, ValueError, runtime.LifecycleError) as exc:
            raise DeploymentError("cannot collect memory/swap/PSI admission facts") from exc
        return {
            "mem_available_kib": mem_available_kib,
            "mem_available_gib": mem_available,
            "pressure_full_avg10": pressure_avg10["full"],
            "pressure_some_avg10": pressure_avg10["some"],
            "pressure_sha256": _sha256(pressure.encode()),
            "pswpout": swap_out,
            "swap_topology_sha256": swap_topology_sha256,
            "swaps_sha256": _sha256(swaps.encode()),
        }

    def convert(self, converter: Path, snapshot: Path, template: Path) -> None:
        paths = (converter.parent, snapshot, template.parent)
        if any(not path.is_absolute() or "," in str(path) for path in paths):
            raise DeploymentError("converter bind paths must be absolute and comma-free")
        self._run(
            [
                "/usr/bin/docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=64",
                "--memory=12g",
                "--memory-swap=12g",
                "--memory-swappiness=0",
                "--oom-score-adj=500",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
                "--mount",
                f"type=bind,src={converter.parent},dst=/owner/scripts,readonly",
                "--mount",
                f"type=bind,src={snapshot},dst=/owner/snapshot",
                "--mount",
                f"type=bind,src={template.parent},dst=/owner/config,readonly",
                "--env=HOME=/tmp",
                "--env=HF_HUB_OFFLINE=1",
                "--env=PYTHONDONTWRITEBYTECODE=1",
                "--env=TRANSFORMERS_OFFLINE=1",
                "--entrypoint=python3",
                CONVERTER_IMAGE,
                f"/owner/scripts/{converter.name}",
                "/owner/snapshot",
                "--template",
                f"/owner/config/{template.name}",
            ],
            timeout=7200,
        )

    def lifecycle(self, action: str, *, pause_text: bool) -> None:
        command = [str(BIN_ROOT / "gb10_querit_canary_lifecycle.py"), action]
        if action == "activate" and pause_text:
            command.append("--pause-text")
        self._run(command, timeout=2400)

    def lifecycle_state_exists(self) -> bool:
        return self.lifecycle_state.exists()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise DeploymentError(f"owner path is not a real directory: {path}")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DeploymentError(f"owner directory permissions are unsafe: {path}")


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    _safe_directory(path.parent, create=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, mode)
    _fsync_directory(path.parent)


def _read_json(path: Path, schema: str) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError(f"owner state is missing or unsafe: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > 4 * 1024 * 1024
        ):
            raise DeploymentError("owner state is not a bounded private regular file")
        payload = os.read(descriptor, 4 * 1024 * 1024 + 1)
    finally:
        os.close(descriptor)
    try:
        parsed = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("owner state is malformed") from exc
    if not isinstance(parsed, dict) or parsed.get("schema") != schema:
        raise DeploymentError("owner state schema is invalid")
    return parsed


def _write_state(path: Path, state: Mapping[str, object]) -> None:
    _atomic_write(path, _canonical(state) + b"\n")


def _hash_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError(f"cannot open regular file: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise DeploymentError(f"file is not a standalone regular file: {path}")
        digest = hashlib.sha256()
        observed = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            observed += len(chunk)
            digest.update(chunk)
        if observed != metadata.st_size:
            raise DeploymentError(f"file changed while hashed: {path}")
        return observed, digest.hexdigest()
    finally:
        os.close(descriptor)


def _copy_file(source: Path, destination: Path, *, mode: int) -> None:
    size, source_hash = _hash_file(source)
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    with source.open("rb") as reader, open(temporary, "xb", buffering=0) as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)
    installed_size, installed_hash = _hash_file(destination)
    if (installed_size, installed_hash) != (size, source_hash):
        raise DeploymentError(f"installed bytes changed for {destination}")


def _snapshot_file(path: Path, *, backup: Path | None = None) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeploymentError(f"deployment target is not a regular file: {path}")
    size, digest = _hash_file(path)
    snapshot: dict[str, object] = {
        "exists": True,
        "mode": stat.S_IMODE(metadata.st_mode),
        "sha256": digest,
        "size": size,
    }
    if backup is not None:
        _safe_directory(backup.parent, create=True)
        _copy_file(path, backup, mode=0o600)
        snapshot["backup"] = str(backup)
    return snapshot


def _same_snapshot(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    return {key: value for key, value in left.items() if key != "backup"} == {
        key: value for key, value in right.items() if key != "backup"
    }


def _mapping(source: str, target: Path, mode: int) -> dict[str, object]:
    return {"source": source, "target": str(target), "mode": mode}


TARGETS = (
    _mapping("scripts/querit_deepinfra_adapter.py", LIB_ROOT / "querit_deepinfra_adapter.py", 0o644),
    _mapping("scripts/querit_canary_lifecycle.py", LIB_ROOT / "querit_canary_lifecycle.py", 0o644),
    _mapping("scripts/querit_canary_runtime.py", LIB_ROOT / "querit_canary_runtime.py", 0o644),
    _mapping("scripts/querit_canary_transaction.py", LIB_ROOT / "querit_canary_transaction.py", 0o644),
    _mapping("scripts/querit_vllm_artifact.py", LIB_ROOT / "querit_vllm_artifact.py", 0o644),
    _mapping("scripts/querit_replay_trust.py", LIB_ROOT / "querit_replay_trust.py", 0o644),
    _mapping("scripts/reranker_equivalence_wire.py", LIB_ROOT / "reranker_equivalence_wire.py", 0o644),
    _mapping("scripts/querit_checkpoint_convert.py", LIB_ROOT / "querit_checkpoint_convert.py", 0o644),
    _mapping("config/querit/querit-rerank.jinja", LIB_ROOT / "querit-rerank.jinja", 0o644),
    _mapping("scripts/querit_canary_deployment.py", LIB_ROOT / "querit_canary_deployment.py", 0o644),
    _mapping("scripts/gb10_querit_canary_lifecycle.py", BIN_ROOT / "gb10_querit_canary_lifecycle.py", 0o755),
    _mapping("scripts/gb10_querit_canary_preflight.py", BIN_ROOT / "gb10_querit_canary_preflight.py", 0o755),
    _mapping("scripts/gb10_querit_canary_deploy.py", BIN_ROOT / "gb10_querit_canary_deploy.py", 0o755),
    _mapping("scripts/gb10_service_ready.sh", BIN_ROOT / "gb10_service_ready.sh", 0o755),
    _mapping(
        "scripts/gb10_verify_vllm_no_swap_core.py",
        BIN_ROOT / "gb10_verify_vllm_no_swap_core.py",
        0o644,
    ),
    _mapping("scripts/gb10_verify_vllm_no_swap.sh", BIN_ROOT / "gb10_verify_vllm_no_swap.sh", 0o755),
    _mapping("systemd/vllm-querit-4b-canary.service", UNIT_ROOT / runtime.ADAPTER_UNIT, 0o644),
    _mapping("systemd/vllm-querit-4b-canary-backend.service", UNIT_ROOT / runtime.BACKEND_UNIT, 0o644),
)


def _git(root: Path, *arguments: str, text: bool = True) -> str | bytes:
    try:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=text,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError("cannot attest source repository") from exc
    return completed.stdout


def _attest_source(root: Path) -> str:
    root = root.resolve(strict=True)
    top = str(_git(root, "rev-parse", "--show-toplevel")).strip()
    if Path(top).resolve() != root:
        raise DeploymentError("source root is not the repository root")
    status = str(_git(root, "status", "--porcelain=v1", "--untracked-files=all"))
    if status:
        raise DeploymentError("source worktree/index must be clean before deployment")
    head = str(_git(root, "rev-parse", "HEAD")).strip()
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise DeploymentError("source HEAD is not a full SHA-1")
    return head


def _bundle_manifest(entries: list[dict[str, object]], head: str) -> dict[str, object]:
    unsigned = {"files": entries, "head": head, "schema": BUNDLE_SCHEMA}
    return {**unsigned, "bundle_sha256": _sha256(_canonical(unsigned))}


def build_bundle(source_root: Path, bundle_root: Path) -> Path:
    """Materialize a private exact-HEAD bundle without reading worktree bytes."""

    source_root = source_root.expanduser().resolve(strict=True)
    head = _attest_source(source_root)
    entries: list[dict[str, object]] = []
    raw_by_source: dict[str, bytes] = {}
    for mapped in TARGETS:
        source = str(mapped["source"])
        raw = _git(source_root, "show", f"{head}:{source}", text=False)
        if not isinstance(raw, bytes):
            raise AssertionError("binary git output unexpectedly decoded")
        raw_by_source[source] = raw
        entries.append(
            {
                "mode": mapped["mode"],
                "path": source,
                "sha256": _sha256(raw),
                "size": len(raw),
                "target": mapped["target"],
            }
        )
    manifest = _bundle_manifest(entries, head)
    bundle_root = bundle_root.expanduser().resolve(strict=False)
    _safe_directory(bundle_root, create=True)
    final = bundle_root / str(manifest["bundle_sha256"])
    if final.exists():
        verify_bundle(final, source_root)
        return final
    temporary = Path(tempfile.mkdtemp(prefix=".bundle.", dir=bundle_root))
    os.chmod(temporary, 0o700)
    try:
        payload_root = temporary / "payload"
        for entry in entries:
            destination = payload_root / str(entry["path"])
            _atomic_write(destination, raw_by_source[str(entry["path"])])
        _atomic_write(temporary / "manifest.json", _canonical(manifest) + b"\n")
        os.replace(temporary, final)
        _fsync_directory(bundle_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    verify_bundle(final, source_root)
    return final


def verify_bundle(bundle: Path, source_root: Path) -> dict[str, object]:
    """Re-attest an existing bundle to the clean source HEAD and target map."""

    manifest = _verify_bundle_contents(bundle)
    expected_head = _attest_source(source_root.expanduser().resolve(strict=True))
    if manifest.get("head") != expected_head:
        raise DeploymentError("bundle source HEAD does not match the clean repository")
    return manifest


def _verify_bundle_contents(bundle: Path) -> dict[str, object]:
    """Verify owner-held bytes during recovery without consulting the worktree."""

    bundle = bundle.expanduser().resolve(strict=True)
    _safe_directory(bundle)
    manifest = _read_json(bundle / "manifest.json", BUNDLE_SCHEMA)
    head = manifest.get("head")
    if (
        not isinstance(head, str)
        or len(head) != 40
        or any(character not in "0123456789abcdef" for character in head)
    ):
        raise DeploymentError("bundle source HEAD is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(TARGETS):
        raise DeploymentError("bundle file mapping is incomplete")
    expected_entries: list[dict[str, object]] = []
    for mapped, entry in zip(TARGETS, files, strict=True):
        if not isinstance(entry, dict):
            raise DeploymentError("bundle mapping entry is malformed")
        required = {"mode", "path", "sha256", "size", "target"}
        if set(entry) != required:
            raise DeploymentError("bundle mapping fields are not exact")
        if (
            entry["path"] != mapped["source"]
            or entry["target"] != mapped["target"]
            or entry["mode"] != mapped["mode"]
            or not isinstance(entry["size"], int)
            or entry["size"] < 0
            or not isinstance(entry["sha256"], str)
            or len(entry["sha256"]) != 64
        ):
            raise DeploymentError("bundle mapping differs from the committed deployment plan")
        payload = bundle / "payload" / str(entry["path"])
        size, digest = _hash_file(payload)
        if (size, digest) != (entry["size"], entry["sha256"]):
            raise DeploymentError(f"bundle payload drifted: {entry['path']}")
        expected_entries.append(dict(entry))
    backend_unit = bundle / "payload" / "systemd" / runtime.BACKEND_UNIT
    try:
        validate_backend_unit(backend_unit.read_text())
    except (OSError, UnicodeError, ProfileError) as exc:
        raise DeploymentError("candidate unit disagrees with profile authority") from exc
    expected = _bundle_manifest(expected_entries, head)
    if manifest != expected or bundle.name != expected["bundle_sha256"]:
        raise DeploymentError("bundle manifest identity is invalid")
    return manifest


def _unit_target(unit: str) -> Path:
    for mapped in TARGETS:
        if Path(str(mapped["target"])).name == unit:
            return Path(str(mapped["target"]))
    raise AssertionError(f"missing target mapping for {unit}")


def _quiescent_unmasked_unit_info(info: Mapping[str, str], unit: str) -> bool:
    if (
        info.get("UnitFileState") not in {"disabled", "static"}
        or info.get("DropInPaths")
    ):
        return False
    fragment = info.get("FragmentPath")
    load_state = info.get("LoadState")
    return (fragment == str(_unit_target(unit)) and load_state == "loaded") or (
        not fragment and load_state == "not-found"
    )


def _installed_unit_info(info: Mapping[str, str], unit: str) -> bool:
    return (
        info.get("FragmentPath") == str(_unit_target(unit))
        and not info.get("DropInPaths")
        and info.get("UnitFileState") in {"disabled", "static"}
        and info.get("LoadState") == "loaded"
    )


def _runtime_mask_path(unit: str) -> Path:
    if unit not in CANDIDATE_UNITS:
        raise DeploymentError(f"runtime mask is not an expected candidate unit: {unit}")
    return RUNTIME_UNIT_ROOT / unit


def _runtime_mask_evidence(value: object, unit: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "scope",
        "path",
        "lstat",
        "link_target",
    }:
        raise DeploymentError(f"runtime mask attestation is malformed for {unit}")
    lstat = value.get("lstat")
    if (
        value.get("scope") != "runtime"
        or value.get("path") != str(_runtime_mask_path(unit))
        or value.get("link_target") != "/dev/null"
        or not isinstance(lstat, dict)
        or set(lstat) != {"st_dev", "st_ino", "st_mode", "type"}
        or not isinstance(lstat.get("st_dev"), int)
        or not isinstance(lstat.get("st_ino"), int)
        or not isinstance(lstat.get("st_mode"), int)
        or lstat.get("type") != "symlink"
        or not stat.S_ISLNK(int(lstat["st_mode"]))
    ):
        raise DeploymentError(f"runtime mask attestation is not exact for {unit}")
    return value


def _same_runtime_mask_layout(left: object, right: object, unit: str) -> bool:
    expected = _runtime_mask_evidence(left, unit)
    observed = _runtime_mask_evidence(right, unit)
    expected_lstat = expected["lstat"]
    observed_lstat = observed["lstat"]
    assert isinstance(expected_lstat, dict)
    assert isinstance(observed_lstat, dict)
    return (
        expected["scope"] == observed["scope"]
        and expected["path"] == observed["path"]
        and expected["link_target"] == observed["link_target"]
        and expected_lstat["st_mode"] == observed_lstat["st_mode"]
        and expected_lstat["type"] == observed_lstat["type"]
    )


def _quiescent(state: runtime.ServiceState) -> bool:
    return (
        not state.active
        and state.main_pid == 0
        and not state.unit_pids
        and not state.container_id
        and state.container_pid == 0
        and not state.container_cgroup
        and not state.container_pids
    )


def _candidate_container(host: Host, unit: str) -> dict[str, str] | None:
    name = runtime.CONTAINER_NAMES.get(unit)
    return host.container(name) if name is not None else None


def _prestate_metadata(metadata: os.stat_result, node_type: str) -> dict[str, object]:
    return {
        "device": metadata.st_dev,
        "gid": metadata.st_gid,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "nlink": metadata.st_nlink,
        "type": node_type,
        "uid": metadata.st_uid,
    }


def _prestate_relative(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise DeploymentError("artifact prestate path escaped its root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise DeploymentError("artifact prestate path traversal is unsafe")
    return relative.as_posix()


def _hash_prestate_file(path: Path, expected: os.stat_result) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError(f"cannot safely read artifact prestate file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        stable_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
            before.st_size,
        )
        stable_expected = (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
            expected.st_nlink,
            expected.st_uid,
            expected.st_gid,
            expected.st_size,
        )
        if (
            stable_before != stable_expected
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > artifact.MAX_FILE_BYTES
        ):
            raise DeploymentError(f"artifact prestate file is unsafe: {path}")
        digest = hashlib.sha256()
        observed = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            observed += len(chunk)
            if observed > artifact.MAX_FILE_BYTES:
                raise DeploymentError(f"artifact prestate file grew while hashing: {path}")
            digest.update(chunk)
        after = os.fstat(descriptor)
        stable_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
            after.st_size,
        )
        if stable_before != stable_after or observed != before.st_size:
            raise DeploymentError(f"artifact prestate file changed while hashing: {path}")
        return observed, digest.hexdigest()
    finally:
        os.close(descriptor)


def _unsealed_artifact_prestate(path: Path) -> dict[str, object]:
    try:
        root_metadata = path.lstat()
    except OSError as exc:
        raise DeploymentError("cannot inspect unsealed artifact root") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise DeploymentError("candidate artifact path is not a real directory")
    if root_metadata.st_uid != os.geteuid() or root_metadata.st_gid != os.getegid():
        raise DeploymentError("artifact prestate root ownership is unsupported")

    root_identity = _prestate_metadata(root_metadata, "directory")
    entries: list[dict[str, object]] = []
    try:
        for directory, dirnames, filenames in os.walk(path, followlinks=False):
            directory_path = Path(directory)
            directory_metadata = directory_path.lstat()
            if (
                stat.S_ISLNK(directory_metadata.st_mode)
                or not stat.S_ISDIR(directory_metadata.st_mode)
                or directory_metadata.st_uid != root_metadata.st_uid
                or directory_metadata.st_gid != root_metadata.st_gid
            ):
                raise DeploymentError(f"artifact prestate directory is unsafe: {directory_path}")
            if directory_path != path:
                entries.append(
                    {
                        "path": _prestate_relative(path, directory_path),
                        **_prestate_metadata(directory_metadata, "directory"),
                    }
                )
                if len(entries) > artifact.MAX_FILES:
                    raise DeploymentError("artifact prestate contains too many entries")
            dirnames.sort()
            filenames.sort()
            for dirname in dirnames:
                child = directory_path / dirname
                metadata = child.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != root_metadata.st_uid
                    or metadata.st_gid != root_metadata.st_gid
                ):
                    raise DeploymentError(f"artifact prestate directory is unsafe: {child}")
            for filename in filenames:
                child = directory_path / filename
                metadata = child.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != root_metadata.st_uid
                    or metadata.st_gid != root_metadata.st_gid
                ):
                    raise DeploymentError(f"artifact prestate file is unsafe: {child}")
                size, digest = _hash_prestate_file(child, metadata)
                entries.append(
                    {
                        "path": _prestate_relative(path, child),
                        "sha256": digest,
                        "size": size,
                        **_prestate_metadata(metadata, "regular"),
                    }
                )
                if len(entries) > artifact.MAX_FILES:
                    raise DeploymentError("artifact prestate contains too many entries")
    except OSError as exc:
        raise DeploymentError("cannot safely inventory unsealed artifact prestate") from exc
    if _prestate_metadata(path.lstat(), "directory") != root_identity:
        raise DeploymentError("artifact prestate root changed during inventory")
    entries.sort(key=lambda entry: str(entry["path"]))
    return {
        "accepted_unsealed_prestate": True,
        "exists": True,
        "inventory": entries,
        "kind": "unsealed-directory",
        "root": root_identity,
    }


def _artifact_snapshot(
    path: Path, *, accept_unsealed_artifact_prestate: bool = False
) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {
            "accept_unsealed_artifact_prestate": accept_unsealed_artifact_prestate,
            "exists": False,
        }
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DeploymentError("candidate artifact path is not a real directory")
    manifest_path = path / artifact.MANIFEST_NAME
    if accept_unsealed_artifact_prestate and not os.path.lexists(manifest_path):
        return _unsealed_artifact_prestate(path)
    return {
        "accept_unsealed_artifact_prestate": accept_unsealed_artifact_prestate,
        "exists": True,
        "manifest_sha256": artifact.manifest_sha256(path),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _assert_artifact_prestate(path: Path, expected: Mapping[str, object]) -> None:
    accepted = expected.get("accepted_unsealed_prestate")
    if accepted is True:
        observed = _unsealed_artifact_prestate(path)
    elif accepted is None:
        configured = expected.get("accept_unsealed_artifact_prestate")
        if not isinstance(configured, bool):
            raise DeploymentError("artifact prestate acceptance is invalid")
        observed = _artifact_snapshot(
            path, accept_unsealed_artifact_prestate=configured
        )
    else:
        raise DeploymentError("artifact prestate acceptance is invalid")
    if observed != expected:
        raise DeploymentError("candidate artifact prestate drifted before installation")


def _artifact_prestate_matches(path: Path, expected: Mapping[str, object]) -> bool:
    try:
        _assert_artifact_prestate(path, expected)
    except (DeploymentError, artifact.ArtifactError):
        return False
    return True


def _reserve_artifact_rollback_directory(parent: Path, bundle_sha256: str) -> Path:
    try:
        reserved = Path(
            tempfile.mkdtemp(
                prefix=f".gb10-querit-previous-{bundle_sha256[:16]}-",
                dir=parent,
            )
        )
        metadata = reserved.lstat()
    except OSError as exc:
        raise DeploymentError("cannot reserve artifact rollback directory") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or any(reserved.iterdir())
    ):
        raise DeploymentError("artifact rollback backup collision")
    _fsync_directory(parent)
    return reserved


def _same_artifact_filesystem(path: Path, parent: Path) -> None:
    try:
        if path.lstat().st_dev != parent.stat().st_dev:
            raise DeploymentError("cross-device artifact publication is forbidden")
    except OSError as exc:
        raise DeploymentError("cannot verify artifact publication filesystem") from exc


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _copy_private_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        raise DeploymentError("private artifact work path already exists")
    shutil.copytree(source, destination, copy_function=shutil.copyfile)
    for directory, _dirs, files in os.walk(destination):
        directory_path = Path(directory)
        os.chmod(directory_path, 0o700)
        for filename in files:
            os.chmod(directory_path / filename, 0o600)
    _safe_directory(destination)


class Deployment:
    def __init__(
        self,
        host: Host,
        state_path: Path,
        *,
        source_root: Path,
        artifact_path: Path,
        lifecycle_state: Path = DEFAULT_LIFECYCLE_STATE,
        accept_runtime_mask_prestate: bool = False,
        accept_unsealed_artifact_prestate: bool = False,
    ) -> None:
        self.host = host
        self.state_path = state_path.expanduser().resolve(strict=False)
        self.source_root = source_root.expanduser().resolve(strict=True)
        self.artifact_path = artifact_path.expanduser().absolute()
        self.lifecycle_state = lifecycle_state.expanduser().resolve(strict=False)
        self.accept_runtime_mask_prestate = accept_runtime_mask_prestate
        self.accept_unsealed_artifact_prestate = accept_unsealed_artifact_prestate

    def _write(self, record: dict[str, object]) -> None:
        _write_state(self.state_path, record)

    def _read(self) -> dict[str, object]:
        return _read_json(self.state_path, STATE_SCHEMA)

    def _capture_prestate(self, manifest: Mapping[str, object]) -> dict[str, object]:
        files: dict[str, object] = {}
        backup_root = self.state_path.parent / "backups" / "files"
        for index, mapped in enumerate(TARGETS):
            target = Path(str(mapped["target"]))
            backup = backup_root / str(index) if target.exists() else None
            files[str(target)] = _snapshot_file(target, backup=backup)
        units = {unit: self.host.unit_info(unit) for unit in CANDIDATE_UNITS}
        protected_units = {
            unit: self.host.unit_info(unit)
            for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT)
        }
        states = {
            unit: self.host.service_state(unit).record()
            for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT, *CANDIDATE_UNITS)
        }
        containers = {
            unit: _candidate_container(self.host, unit)
            for unit in CANDIDATE_UNITS
        }
        return {
            "admission": self.host.admission(),
            "artifact": _artifact_snapshot(
                self.artifact_path,
                accept_unsealed_artifact_prestate=self.accept_unsealed_artifact_prestate,
            ),
            "bundle_sha256": manifest["bundle_sha256"],
            "candidate_containers": containers,
            "candidate_runtime_masks": {
                unit: self.host.runtime_mask_attestation(unit)
                for unit in CANDIDATE_UNITS
            },
            "candidate_units": units,
            "files": files,
            "listeners": list(self.host.listeners()),
            "protected_units": protected_units,
            "service_states": states,
        }

    def _validate_prestate(self, prestate: Mapping[str, object], manifest: Mapping[str, object]) -> None:
        if prestate.get("listeners"):
            raise DeploymentError("candidate listener 18014 or 18015 is already occupied")
        containers = prestate.get("candidate_containers")
        if not isinstance(containers, dict) or any(containers.values()):
            raise DeploymentError("candidate container already exists")
        states = prestate.get("service_states")
        if not isinstance(states, dict):
            raise DeploymentError("candidate service prestate is invalid")
        for unit in CANDIDATE_UNITS:
            service_state = runtime.ServiceState.from_record(states.get(unit), unit)
            if not _quiescent(service_state):
                raise DeploymentError("candidate unit must be fully inactive before deployment")
        units = prestate.get("candidate_units")
        if not isinstance(units, dict):
            raise DeploymentError("candidate unit metadata is invalid")
        runtime_masks = prestate.get("candidate_runtime_masks")
        if not isinstance(runtime_masks, dict) or set(runtime_masks) != set(CANDIDATE_UNITS):
            raise DeploymentError("candidate runtime-mask evidence is invalid")
        states_by_unit: dict[str, str] = {}
        for unit in CANDIDATE_UNITS:
            info = units.get(unit)
            if not isinstance(info, dict):
                raise DeploymentError("candidate unit metadata is malformed")
            unit_file_state = info.get("UnitFileState")
            if unit_file_state == "masked":
                raise DeploymentError(f"persistent candidate mask blocks deployment: {unit}")
            if unit_file_state not in {"disabled", "masked-runtime"}:
                raise DeploymentError(f"candidate unit must remain disabled: {unit}")
            states_by_unit[unit] = unit_file_state
            if info.get("DropInPaths"):
                raise DeploymentError(f"candidate unit has unexpected drop-ins: {unit}")
            target = _unit_target(unit)
            snapshot = prestate["files"].get(str(target))  # type: ignore[index]
            if not isinstance(snapshot, dict):
                raise DeploymentError("candidate unit file snapshot is missing")
            expected_fragment = str(target)
            if unit_file_state == "masked-runtime":
                evidence = _runtime_mask_evidence(runtime_masks.get(unit), unit)
                expected_fragment = str(evidence["path"])
                if info.get("LoadState") != "masked":
                    raise DeploymentError(f"candidate runtime mask load state is invalid: {unit}")
            elif runtime_masks.get(unit) is not None:
                raise DeploymentError(f"unexpected runtime candidate mask blocks deployment: {unit}")
            if snapshot.get("exists"):
                expected = next(
                    entry for entry in manifest["files"]  # type: ignore[index]
                    if entry["target"] == str(target)
                )
                if (
                    snapshot.get("sha256") != expected["sha256"]
                    or snapshot.get("mode") != expected["mode"]
                    or info.get("FragmentPath") != expected_fragment
                ):
                    raise DeploymentError(
                        f"candidate unit bytes or FragmentPath drifted: {unit}"
                    )
            elif info.get("FragmentPath") != (
                expected_fragment if unit_file_state == "masked-runtime" else ""
            ):
                raise DeploymentError(f"candidate unit loaded from an unexpected path: {unit}")
        runtime_masked = [
            unit for unit, state in states_by_unit.items() if state == "masked-runtime"
        ]
        if runtime_masked:
            if set(runtime_masked) != set(CANDIDATE_UNITS):
                raise DeploymentError("candidate runtime-mask prestate is mixed or incomplete")
            if not self.accept_runtime_mask_prestate:
                raise DeploymentError(
                    f"foreign runtime candidate mask blocks deployment: {runtime_masked[0]}"
                )
        protected_units = prestate.get("protected_units")
        if not isinstance(protected_units, dict):
            raise DeploymentError("protected unit metadata is invalid")
        text_info = protected_units.get(runtime.TEXT_UNIT)
        if not isinstance(text_info, dict):
            raise DeploymentError("text unit metadata is invalid")
        if text_info.get("UnitFileState") in {"masked", "masked-runtime"}:
            raise DeploymentError("text service is masked and cannot be transactionally restored")
        if prestate.get("bundle_sha256") != manifest.get("bundle_sha256"):
            raise DeploymentError("prestate bundle identity is invalid")
        admission = prestate.get("admission")
        if not isinstance(admission, dict):
            raise DeploymentError("candidate admission prestate is invalid")
        try:
            validate_admission(admission)
        except ProfileError as exc:
            raise DeploymentError(str(exc)) from exc

    def _reattest_preinstall(self, record: Mapping[str, object], manifest: Mapping[str, object]) -> None:
        prestate = record.get("prestate")
        if not isinstance(prestate, dict):
            raise DeploymentError("deployment prestate is missing")
        for mapped in TARGETS:
            target = Path(str(mapped["target"]))
            expected = prestate["files"].get(str(target))  # type: ignore[index]
            if not isinstance(expected, dict) or not _same_snapshot(
                expected, _snapshot_file(target)
            ):
                raise DeploymentError(f"deployed target drifted before installation: {target}")
        expected_artifact = prestate.get("artifact")
        if not isinstance(expected_artifact, dict):
            raise DeploymentError("candidate artifact prestate is invalid")
        _assert_artifact_prestate(self.artifact_path, expected_artifact)
        if prestate.get("listeners") != list(self.host.listeners()):
            raise DeploymentError("candidate listener prestate drifted before installation")
        candidate_units = prestate.get("candidate_units")
        runtime_masks = prestate.get("candidate_runtime_masks")
        if not isinstance(candidate_units, dict) or not isinstance(runtime_masks, dict):
            raise DeploymentError("candidate unit prestate is malformed")
        accepted_runtime_masks = all(
            isinstance(candidate_units.get(unit), dict)
            and candidate_units[unit].get("UnitFileState") == "masked-runtime"
            for unit in CANDIDATE_UNITS
        )
        for unit in CANDIDATE_UNITS:
            info = self.host.unit_info(unit)
            previous = candidate_units.get(unit)
            observed_mask = self.host.runtime_mask_attestation(unit)
            if not isinstance(previous, dict) or observed_mask is None:
                raise DeploymentError(f"candidate unit metadata drifted before installation: {unit}")
            _runtime_mask_evidence(observed_mask, unit)
            if accepted_runtime_masks:
                expected_mask = runtime_masks.get(unit)
                if info != previous or observed_mask != expected_mask:
                    raise DeploymentError(f"candidate unit metadata drifted before installation: {unit}")
            elif (
                info.get("DropInPaths") != previous.get("DropInPaths")
                or info.get("UnitFileState") != "masked-runtime"
            ):
                raise DeploymentError(f"candidate unit metadata drifted before installation: {unit}")
        protected = prestate.get("protected_units")
        if not isinstance(protected, dict):
            raise DeploymentError("protected unit metadata is invalid")
        for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT):
            if protected.get(unit) != self.host.unit_info(unit):
                raise DeploymentError(f"protected unit metadata drifted before installation: {unit}")
        self._assert_service_identities(prestate)
        if prestate.get("candidate_containers") != {
            unit: _candidate_container(self.host, unit) for unit in CANDIDATE_UNITS
        }:
            raise DeploymentError("candidate container prestate drifted before installation")
        previous_admission = prestate.get("admission")
        if not isinstance(previous_admission, dict):
            raise DeploymentError("candidate admission prestate is invalid")
        current_admission = self.host.admission()
        try:
            validate_admission(current_admission)
        except ProfileError as exc:
            raise DeploymentError(str(exc)) from exc
        if current_admission.get("swap_topology_sha256") != previous_admission.get(
            "swap_topology_sha256"
        ):
            raise DeploymentError("swap configuration drifted before installation")
        if current_admission.get("pswpout") != previous_admission.get("pswpout"):
            raise DeploymentError("swap-out activity drifted before installation")

    def plan(self, bundle: Path) -> dict[str, object]:
        """Return the non-secret mask ownership plan without mutating the host."""

        manifest = verify_bundle(bundle, self.source_root)
        if self.state_path.exists():
            raise DeploymentError("existing deployment receipt must be recovered before plan")
        prestate = self._capture_prestate(manifest)
        self._validate_prestate(prestate, manifest)
        candidate_units = prestate["candidate_units"]
        assert isinstance(candidate_units, dict)
        accepted_runtime_masks = all(
            isinstance(candidate_units.get(unit), dict)
            and candidate_units[unit].get("UnitFileState") == "masked-runtime"
            for unit in CANDIDATE_UNITS
        )
        return {
            "artifact_prestate": prestate["artifact"],
            "bundle_sha256": manifest["bundle_sha256"],
            "candidate_runtime_mask_ownership": {
                "accepted_prestate": accepted_runtime_masks,
                "owned_units": list(CANDIDATE_UNITS),
                "remove_before_activation": list(CANDIDATE_UNITS),
                "restore_on_rollback": list(CANDIDATE_UNITS),
            },
            "schema": PLAN_SCHEMA,
        }

    def _assert_service_identities(self, prestate: Mapping[str, object]) -> None:
        expected = prestate.get("service_states")
        if not isinstance(expected, dict):
            raise DeploymentError("service identity prestate is invalid")
        for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT, *CANDIDATE_UNITS):
            expected_state = runtime.ServiceState.from_record(expected.get(unit), unit)
            observed = self.host.service_state(unit)
            if observed != expected_state:
                raise DeploymentError(f"service identity drifted before deployment: {unit}")

    def prepare(self, bundle: Path) -> None:
        self.host.require_cgroup_v2()
        manifest = verify_bundle(bundle, self.source_root)
        if self.state_path.exists():
            raise DeploymentError("existing deployment receipt must be recovered before prepare")
        prestate = self._capture_prestate(manifest)
        self._validate_prestate(prestate, manifest)
        candidate_units = prestate["candidate_units"]
        assert isinstance(candidate_units, dict)
        accepted_runtime_masks = all(
            isinstance(candidate_units.get(unit), dict)
            and candidate_units[unit].get("UnitFileState") == "masked-runtime"
            for unit in CANDIDATE_UNITS
        )
        record: dict[str, object] = {
            "bundle": str(bundle.expanduser().resolve(strict=True)),
            "lifecycle_deactivation_intent": False,
            "lifecycle_deactivated": False,
            "runtime_mask_ownership": {
                "accepted_prestate": accepted_runtime_masks,
                "owned_units": list(CANDIDATE_UNITS) if accepted_runtime_masks else [],
                "removed_units": [],
                "restored_units": [],
            },
            "phase": "preparing",
            "prestate": prestate,
            "schema": STATE_SCHEMA,
        }
        self._write(record)
        if not accepted_runtime_masks:
            ownership = record["runtime_mask_ownership"]
            assert isinstance(ownership, dict)
            owned = ownership["owned_units"]
            assert isinstance(owned, list)
            for unit in CANDIDATE_UNITS:
                owned.append(unit)
                self._write(record)
                self.host.runtime_mask(unit)
                if (
                    self.host.unit_info(unit).get("UnitFileState") != "masked-runtime"
                    or self.host.runtime_mask_attestation(unit) is None
                ):
                    raise DeploymentError(f"owner runtime mask was not created: {unit}")
                self._write(record)
        record["phase"] = "prepared"
        self._write(record)

    def _publish_artifact(
        self, record: dict[str, object], manifest: Mapping[str, object], source_snapshot: Path
    ) -> None:
        source_snapshot = source_snapshot.expanduser().resolve(strict=True)
        if source_snapshot == self.artifact_path.resolve(strict=False):
            raise DeploymentError("source snapshot must not be the candidate artifact path")
        try:
            artifact.attest_source_snapshot(source_snapshot)
        except artifact.ArtifactError as exc:
            raise DeploymentError("source snapshot does not match the pinned ledger") from exc
        self.artifact_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        work_root = Path(tempfile.mkdtemp(prefix=".gb10-querit-owner-", dir=self.artifact_path.parent))
        os.chmod(work_root, 0o700)
        stage = work_root / "converted"
        _copy_private_tree(source_snapshot, stage)
        record["artifact_work"] = str(work_root)
        record["artifact_stage"] = str(stage)
        record["conversion_intent"] = str(stage)
        self._write(record)
        bundle_root = Path(str(record["bundle"]))
        self.host.convert(
            bundle_root / "payload" / "scripts" / "querit_checkpoint_convert.py",
            stage,
            bundle_root / "payload" / "config" / "querit" / "querit-rerank.jinja",
        )
        record["conversion_complete"] = True
        record.pop("conversion_intent", None)
        self._write(record)
        try:
            new_manifest = artifact.manifest_sha256(stage)
        except artifact.ArtifactError as exc:
            raise DeploymentError("converted owner artifact failed validation") from exc
        artifact_record = record["prestate"]  # type: ignore[index]
        assert isinstance(artifact_record, dict)
        previous = artifact_record["artifact"]
        assert isinstance(previous, dict)
        previous_exists = previous.get("exists") is True
        if previous.get("exists") not in {True, False}:
            raise DeploymentError("candidate artifact prestate is invalid")
        backup = (
            _reserve_artifact_rollback_directory(
                self.artifact_path.parent, str(manifest["bundle_sha256"])
            )
            if previous_exists
            else None
        )
        record["artifact_publication"] = {
            "new_manifest_sha256": new_manifest,
            "previous_backup": str(backup) if backup is not None else None,
            "rename_started": False,
            "state": "previous-move-intent",
        }
        self._write(record)
        _assert_artifact_prestate(self.artifact_path, previous)
        if previous_exists:
            assert backup is not None
            _same_artifact_filesystem(self.artifact_path, self.artifact_path.parent)
            _same_artifact_filesystem(stage, self.artifact_path.parent)
        record["artifact_publication"]["rename_started"] = True  # type: ignore[index]
        self._write(record)
        if previous_exists:
            assert backup is not None
            os.replace(self.artifact_path, backup)
            _fsync_directory(self.artifact_path.parent)
        record["artifact_publication"]["state"] = "previous-moved"  # type: ignore[index]
        self._write(record)
        record["artifact_publication"]["state"] = "publish-intent"  # type: ignore[index]
        self._write(record)
        os.replace(stage, self.artifact_path)
        _fsync_directory(self.artifact_path.parent)
        record["artifact_publication"]["state"] = "published"  # type: ignore[index]
        self._write(record)
        if artifact.manifest_sha256(self.artifact_path) != new_manifest:
            raise DeploymentError("published artifact hash changed")

    def _install_files(self, record: dict[str, object], manifest: Mapping[str, object]) -> None:
        bundle = Path(str(record["bundle"]))
        record["files_install_started"] = True
        record["file_installation"] = {"installed_targets": [], "pending_target": None}
        self._write(record)
        installation = record["file_installation"]
        assert isinstance(installation, dict)
        installed_targets = installation["installed_targets"]
        assert isinstance(installed_targets, list)
        for entry in manifest["files"]:  # type: ignore[index]
            assert isinstance(entry, dict)
            source = bundle / "payload" / str(entry["path"])
            target = Path(str(entry["target"]))
            installation["pending_target"] = str(target)
            self._write(record)
            _copy_file(source, target, mode=int(entry["mode"]))
            installed_targets.append(str(target))
            installation["pending_target"] = None
            self._write(record)
        record["files_installed"] = True
        self._write(record)
        record["daemon_reload_intent"] = "after-file-install"
        self._write(record)
        self.host.daemon_reload()
        record.pop("daemon_reload_intent", None)
        self._write(record)
        self._remove_owned_runtime_masks(record)
        record["daemon_reload_intent"] = "after-runtime-mask-removal"
        self._write(record)
        self.host.daemon_reload()
        record.pop("daemon_reload_intent", None)
        self._write(record)
        self._verify_installed(manifest)

    def _verify_installed(self, manifest: Mapping[str, object]) -> None:
        for entry in manifest["files"]:  # type: ignore[index]
            assert isinstance(entry, dict)
            target = Path(str(entry["target"]))
            size, digest = _hash_file(target)
            metadata = target.stat()
            if (
                size != entry["size"]
                or digest != entry["sha256"]
                or stat.S_IMODE(metadata.st_mode) != entry["mode"]
            ):
                raise DeploymentError(f"installed file drifted: {target}")
        for unit in CANDIDATE_UNITS:
            info = self.host.unit_info(unit)
            if not _installed_unit_info(info, unit):
                raise DeploymentError(f"loaded candidate unit is not the installed disabled unit: {unit}")

    def install(self, source_snapshot: Path) -> None:
        self.host.require_cgroup_v2()
        record = self._read()
        if record.get("phase") != "prepared":
            raise DeploymentError("deployment must be prepared before installation")
        bundle = Path(str(record.get("bundle", "")))
        manifest = verify_bundle(bundle, self.source_root)
        self._reattest_preinstall(record, manifest)
        record["phase"] = "installing"
        self._write(record)
        try:
            self._publish_artifact(record, manifest, source_snapshot)
            self._install_files(record, manifest)
        except BaseException:
            publication = record.get("artifact_publication")
            if isinstance(publication, dict) and publication.get("rename_started") is True:
                self.rollback()
            raise
        record["phase"] = "installed"
        self._write(record)

    def activate(self, *, pause_text: bool) -> None:
        self.host.require_cgroup_v2()
        record = self._read()
        if record.get("phase") != "installed":
            raise DeploymentError("deployment must be installed before activation")
        try:
            manifest = verify_bundle(Path(str(record["bundle"])), self.source_root)
            self._verify_installed(manifest)
            prestate = record.get("prestate")
            if not isinstance(prestate, dict):
                raise DeploymentError("deployment prestate is missing")
            self._assert_service_identities(prestate)
            if self.host.listeners() or any(
                _candidate_container(self.host, unit) for unit in CANDIDATE_UNITS
            ):
                raise DeploymentError("candidate listener or container appeared before activation")
            if prestate.get("admission") != self.host.admission():
                raise DeploymentError("memory/swap/PSI admission facts drifted before activation")
            record["lifecycle_started"] = True
            record["lifecycle_activation_intent"] = True
            record["pause_text"] = pause_text
            self._write(record)
            self.host.lifecycle("activate", pause_text=pause_text)
            record["lifecycle_active"] = True
            record["lifecycle_activation_intent"] = False
            record["phase"] = "active"
            self._write(record)
            _atomic_write(
                self.state_path.parent / "active-receipt.json", _canonical(record) + b"\n"
            )
        except BaseException:
            try:
                if self.state_path.exists():
                    self.rollback()
            except BaseException as rollback_exc:
                raise DeploymentError("activation failed and rollback was incomplete") from rollback_exc
            raise

    def _restore_artifact(self, record: dict[str, object]) -> None:
        publication = record.get("artifact_publication")
        if not isinstance(publication, dict):
            return
        state = publication.get("state")
        if state not in {
            "rename-intent",
            "previous-move-intent",
            "previous-moved",
            "publish-intent",
            "published",
            "rollback-old-restoring",
        }:
            raise DeploymentError("artifact publication state is invalid")
        prestate = record.get("prestate")
        if not isinstance(prestate, dict):
            raise DeploymentError("deployment prestate is missing")
        previous = prestate.get("artifact")
        if not isinstance(previous, dict) or previous.get("exists") not in {True, False}:
            raise DeploymentError("artifact prestate is invalid")
        previous_exists = previous["exists"] is True
        raw_backup = publication.get("previous_backup")
        if previous_exists:
            if not isinstance(raw_backup, str):
                raise DeploymentError("artifact rollback backup is missing")
            backup = Path(raw_backup)
            if (
                backup.parent != self.artifact_path.parent
                or not backup.name.startswith(".gb10-querit-previous-")
            ):
                raise DeploymentError("artifact rollback backup path is unsafe")
        elif raw_backup is None:
            backup = None
        else:
            raise DeploymentError("artifact rollback backup is unexpected")

        def backup_matches_prestate() -> bool:
            return backup is not None and _path_lexists(backup) and _artifact_prestate_matches(
                backup, previous
            )

        def backup_is_empty_reservation() -> bool:
            if backup is None or not _path_lexists(backup):
                return False
            metadata = backup.lstat()
            return (
                not stat.S_ISLNK(metadata.st_mode)
                and stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and not any(backup.iterdir())
            )

        def displaced_publication(*, require_exists: bool) -> Path | None:
            raw_displaced = publication.get("rollback_displaced")
            if raw_displaced is None:
                return None
            if not isinstance(raw_displaced, str):
                raise DeploymentError("artifact rollback displaced path is invalid")
            displaced = Path(raw_displaced)
            if (
                displaced.parent != self.artifact_path.parent
                or not displaced.name.startswith(".gb10-querit-discard-")
            ):
                raise DeploymentError("artifact rollback displaced path is unsafe")
            if not _path_lexists(displaced):
                if require_exists:
                    raise DeploymentError("artifact rollback displaced tree is missing")
                return displaced
            metadata = displaced.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise DeploymentError("artifact rollback displaced tree is unsafe")
            return displaced

        def validate_displaced_publication() -> None:
            displaced = displaced_publication(require_exists=True)
            if displaced is None:
                return
            expected = publication.get("new_manifest_sha256")
            if (
                not isinstance(expected, str)
                or artifact.manifest_sha256(displaced) != expected
            ):
                raise DeploymentError("artifact rollback displaced identity changed")

        def cleanup_displaced_publication() -> None:
            displaced = displaced_publication(require_exists=False)
            if displaced is None:
                return
            if _path_lexists(displaced):
                shutil.rmtree(displaced)
                _fsync_directory(displaced.parent)
            publication.pop("rollback_displaced", None)
            self._write(record)

        def mark_restored() -> None:
            if not _artifact_prestate_matches(self.artifact_path, previous):
                raise DeploymentError("restored artifact does not match its prestate")
            if backup is not None and _path_lexists(backup):
                raise DeploymentError("artifact rollback backup remains after restoration")
            if not previous_exists:
                validate_displaced_publication()
            record["artifact_restored"] = True
            self._write(record)
            cleanup_displaced_publication()

        def restore_backup() -> None:
            if not backup_matches_prestate():
                raise DeploymentError("artifact rollback backup no longer matches prestate")
            if _path_lexists(self.artifact_path):
                raise DeploymentError("artifact rollback target is unexpectedly occupied")
            assert backup is not None
            _same_artifact_filesystem(backup, self.artifact_path.parent)
            publication["state"] = "rollback-old-restoring"
            self._write(record)
            os.replace(backup, self.artifact_path)
            _fsync_directory(self.artifact_path.parent)
            mark_restored()

        def target_matches_publication() -> bool:
            expected = publication.get("new_manifest_sha256")
            return (
                _path_lexists(self.artifact_path)
                and isinstance(expected, str)
                and artifact.manifest_sha256(self.artifact_path) == expected
            )

        if record.get("artifact_restored"):
            if not _artifact_prestate_matches(self.artifact_path, previous) or (
                backup is not None and _path_lexists(backup)
            ):
                raise DeploymentError("restored artifact no longer matches its prestate")
            cleanup_displaced_publication()
            return
        target_matches_prestate = _artifact_prestate_matches(self.artifact_path, previous)
        target_exists = _path_lexists(self.artifact_path)
        backup_exists = backup is not None and _path_lexists(backup)
        if state in {"rename-intent", "previous-move-intent"}:
            if not previous_exists:
                if target_matches_prestate and not backup_exists:
                    mark_restored()
                    return
                raise DeploymentError("artifact rename intent recovery is ambiguous")
            if target_matches_prestate and backup_is_empty_reservation():
                assert backup is not None
                backup.rmdir()
                _fsync_directory(backup.parent)
                mark_restored()
                return
            if not target_exists and backup_matches_prestate():
                restore_backup()
                return
            raise DeploymentError("artifact rename intent recovery is ambiguous")
        if state == "rollback-old-restoring":
            if target_matches_prestate and not backup_exists:
                mark_restored()
                return
            if not target_exists and backup_matches_prestate():
                restore_backup()
                return
            raise DeploymentError("artifact rollback recovery is ambiguous")
        if state in {"previous-moved", "publish-intent"}:
            if target_matches_prestate and not backup_exists:
                mark_restored()
                return
            if not target_exists:
                if previous_exists and backup_matches_prestate():
                    restore_backup()
                    return
                if not previous_exists:
                    mark_restored()
                    return
            if target_matches_publication() and (
                backup_matches_prestate() if previous_exists else not backup_exists
            ):
                publication["state"] = "published"
                self._write(record)
                state = "published"
            else:
                raise DeploymentError("artifact publication recovery is ambiguous")
        if state != "published":
            raise DeploymentError("artifact publication state is invalid")
        if target_matches_prestate and not backup_exists:
            mark_restored()
            return
        if not previous_exists:
            if not target_exists:
                raise DeploymentError("artifact publication rollback evidence is missing")
            expected = publication.get("new_manifest_sha256")
            if (
                not isinstance(expected, str)
                or artifact.manifest_sha256(self.artifact_path) != expected
            ):
                raise DeploymentError("refusing to remove artifact whose identity changed")
            displaced = Path(
                tempfile.mkdtemp(
                    prefix=".gb10-querit-discard-", dir=self.artifact_path.parent
                )
            )
            publication["rollback_displaced"] = str(displaced)
            self._write(record)
            os.replace(self.artifact_path, displaced)
            _fsync_directory(self.artifact_path.parent)
            publication["state"] = "rollback-old-restoring"
            self._write(record)
            mark_restored()
            return
        if not backup_matches_prestate():
            raise DeploymentError("artifact publication rollback evidence is missing")
        if target_exists:
            expected = publication.get("new_manifest_sha256")
            if (
                not isinstance(expected, str)
                or artifact.manifest_sha256(self.artifact_path) != expected
            ):
                raise DeploymentError("refusing to remove artifact whose identity changed")
            displaced = Path(
                tempfile.mkdtemp(
                    prefix=".gb10-querit-discard-", dir=self.artifact_path.parent
                )
            )
            publication["rollback_displaced"] = str(displaced)
            self._write(record)
            os.replace(self.artifact_path, displaced)
            _fsync_directory(self.artifact_path.parent)
            publication["state"] = "rollback-old-restoring"
            self._write(record)
        restore_backup()

    def _remove_private_artifact_work(self, record: Mapping[str, object]) -> None:
        raw_work = record.get("artifact_work")
        if not isinstance(raw_work, str):
            return
        work = Path(raw_work)
        if not work.exists():
            return
        if work.parent != self.artifact_path.parent or not work.name.startswith(
            ".gb10-querit-owner-"
        ):
            raise DeploymentError("artifact work path is outside the deployment owner")
        if work.is_symlink() or not work.is_dir():
            raise DeploymentError("artifact work path is unsafe")
        shutil.rmtree(work)
        _fsync_directory(work.parent)

    def _restore_files(self, record: dict[str, object], manifest: Mapping[str, object]) -> None:
        prestate = record.get("prestate")
        assert isinstance(prestate, dict)
        file_prestate = prestate.get("files")
        assert isinstance(file_prestate, dict)
        expected_by_target = {str(entry["target"]): entry for entry in manifest["files"]}  # type: ignore[index]
        for target_name, snapshot in file_prestate.items():
            if not isinstance(snapshot, dict):
                raise DeploymentError("file prestate is malformed")
            target = Path(target_name)
            if snapshot.get("exists"):
                backup = snapshot.get("backup")
                if not isinstance(backup, str):
                    raise DeploymentError("file backup is missing")
                record["file_restore_pending"] = target_name
                self._write(record)
                _copy_file(Path(backup), target, mode=int(snapshot["mode"]))
            elif target.exists():
                size, digest = _hash_file(target)
                expected = expected_by_target.get(target_name)
                if not isinstance(expected, dict) or (
                    size,
                    digest,
                    stat.S_IMODE(target.stat().st_mode),
                ) != (expected["size"], expected["sha256"], expected["mode"]):
                    raise DeploymentError(f"refusing to remove changed deployed file: {target}")
                record["file_remove_pending"] = target_name
                self._write(record)
                target.unlink()
        for target_name, snapshot in file_prestate.items():
            if not isinstance(snapshot, dict) or not _same_snapshot(
                snapshot, _snapshot_file(Path(target_name))
            ):
                raise DeploymentError(f"file rollback did not restore {target_name}")
        record.pop("file_restore_pending", None)
        record.pop("file_remove_pending", None)
        self._write(record)

    def _assert_candidate_absent(self) -> None:
        if self.host.listeners():
            raise DeploymentError("candidate listener remains after lifecycle rollback")
        for unit in CANDIDATE_UNITS:
            if not _quiescent(self.host.service_state(unit)):
                raise DeploymentError(f"candidate unit/PIDs remain after lifecycle rollback: {unit}")
            if _candidate_container(self.host, unit) is not None:
                raise DeploymentError(f"candidate container remains after lifecycle rollback: {unit}")

    def _runtime_mask_ownership(self, record: Mapping[str, object]) -> dict[str, object]:
        prestate = record.get("prestate")
        if not isinstance(prestate, dict):
            raise DeploymentError("deployment prestate is missing")
        units = prestate.get("candidate_units")
        runtime_masks = prestate.get("candidate_runtime_masks")
        ownership = record.get("runtime_mask_ownership")
        if (
            not isinstance(units, dict)
            or not isinstance(runtime_masks, dict)
            or set(runtime_masks) != set(CANDIDATE_UNITS)
            or not isinstance(ownership, dict)
            or set(ownership)
            != {"accepted_prestate", "owned_units", "removed_units", "restored_units"}
            or not isinstance(ownership.get("accepted_prestate"), bool)
        ):
            raise DeploymentError("candidate unit prestate is malformed")
        accepted = ownership["accepted_prestate"]
        expected_accepted = all(
            isinstance(units.get(unit), dict)
            and units[unit].get("UnitFileState") == "masked-runtime"
            for unit in CANDIDATE_UNITS
        )
        if accepted != expected_accepted:
            raise DeploymentError("candidate runtime-mask ownership does not match prestate")
        owned = ownership["owned_units"]
        removed = ownership["removed_units"]
        restored = ownership["restored_units"]
        if (
            not isinstance(owned, list)
            or not isinstance(removed, list)
            or not isinstance(restored, list)
            or any(not isinstance(unit, str) for unit in (*owned, *removed, *restored))
            or len(set(owned)) != len(owned)
            or len(set(removed)) != len(removed)
            or len(set(restored)) != len(restored)
            or not set(owned).issubset(CANDIDATE_UNITS)
            or not set(removed).issubset(owned)
            or not set(restored).issubset(owned)
            or (accepted and set(owned) != set(CANDIDATE_UNITS))
        ):
            raise DeploymentError("owned runtime-mask record is invalid")
        return ownership

    def _assert_runtime_mask_removed(self, unit: str) -> None:
        if (
            not _quiescent_unmasked_unit_info(self.host.unit_info(unit), unit)
            or self.host.runtime_mask_attestation(unit) is not None
        ):
            raise DeploymentError(f"candidate runtime mask did not remove: {unit}")
        if not _quiescent(self.host.service_state(unit)):
            raise DeploymentError(f"candidate unit became active after runtime mask removal: {unit}")
        if self.host.listeners() or _candidate_container(self.host, unit) is not None:
            raise DeploymentError(f"candidate ownership appeared after runtime mask removal: {unit}")

    def _remove_owned_runtime_masks(self, record: dict[str, object]) -> None:
        ownership = self._runtime_mask_ownership(record)
        prestate = record["prestate"]
        assert isinstance(prestate, dict)
        units = prestate["candidate_units"]
        runtime_masks = prestate["candidate_runtime_masks"]
        assert isinstance(units, dict)
        assert isinstance(runtime_masks, dict)
        accepted = ownership["accepted_prestate"]
        owned = ownership["owned_units"]
        removed = ownership["removed_units"]
        assert isinstance(accepted, bool)
        assert isinstance(owned, list)
        assert isinstance(removed, list)
        for unit in owned:
            before = units.get(unit)
            if not isinstance(before, dict):
                raise DeploymentError("candidate unit prestate is malformed")
            if unit in removed:
                self._assert_runtime_mask_removed(unit)
                continue
            observed_mask = self.host.runtime_mask_attestation(unit)
            observed_info = self.host.unit_info(unit)
            masked_view = (
                observed_info.get("UnitFileState") == "masked-runtime"
                and observed_info.get("FragmentPath") == str(_runtime_mask_path(unit))
                and observed_info.get("LoadState") == "masked"
                and observed_info.get("DropInPaths") == before.get("DropInPaths")
            )
            if observed_mask is None or not (
                masked_view or _installed_unit_info(observed_info, unit)
            ):
                raise DeploymentError(
                    f"candidate mask prestate is invalid: owned runtime mask is absent: {unit}"
                )
            _runtime_mask_evidence(observed_mask, unit)
            if accepted and observed_mask != runtime_masks.get(unit):
                raise DeploymentError(f"accepted runtime mask identity drifted: {unit}")
            removed.append(unit)
            self._write(record)
            self.host.runtime_unmask(unit)
            self._assert_runtime_mask_removed(unit)
            self._write(record)

    def _restore_owned_runtime_masks(self, record: dict[str, object]) -> bool:
        ownership = self._runtime_mask_ownership(record)
        prestate = record["prestate"]
        assert isinstance(prestate, dict)
        units = prestate["candidate_units"]
        runtime_masks = prestate["candidate_runtime_masks"]
        assert isinstance(units, dict)
        assert isinstance(runtime_masks, dict)
        accepted = ownership["accepted_prestate"]
        owned = ownership["owned_units"]
        restored = ownership["restored_units"]
        assert isinstance(accepted, bool)
        assert isinstance(owned, list)
        assert isinstance(restored, list)
        changed = False
        for unit in owned:
            before = units.get(unit)
            if not isinstance(before, dict):
                raise DeploymentError("candidate unit prestate is malformed")
            observed_info = self.host.unit_info(unit)
            observed_mask = self.host.runtime_mask_attestation(unit)
            if accepted:
                expected_mask = runtime_masks.get(unit)
                _runtime_mask_evidence(expected_mask, unit)
                if observed_mask == expected_mask:
                    continue
                if (
                    unit in restored
                    and observed_mask is not None
                    and _same_runtime_mask_layout(expected_mask, observed_mask, unit)
                ):
                    continue
                if observed_mask is not None or not _quiescent_unmasked_unit_info(
                    observed_info, unit
                ):
                    raise DeploymentError(f"candidate mask prestate drifted after owner restoration: {unit}")
                if unit not in restored:
                    restored.append(unit)
                    self._write(record)
                self.host.runtime_mask(unit)
                restored_mask = self.host.runtime_mask_attestation(unit)
                if (
                    restored_mask is None
                    or not _same_runtime_mask_layout(expected_mask, restored_mask, unit)
                ):
                    raise DeploymentError(f"candidate runtime mask did not restore: {unit}")
            else:
                if observed_mask is None and _quiescent_unmasked_unit_info(observed_info, unit):
                    continue
                if observed_mask is None:
                    raise DeploymentError(
                        f"candidate mask prestate is invalid: owned runtime mask is absent: {unit}"
                    )
                _runtime_mask_evidence(observed_mask, unit)
                if unit not in restored:
                    restored.append(unit)
                    self._write(record)
                self.host.runtime_unmask(unit)
                self._assert_runtime_mask_removed(unit)
            changed = True
            if unit not in restored:
                restored.append(unit)
            self._write(record)
        return changed

    def _verify_restored_unit_prestate(self, record: Mapping[str, object]) -> None:
        prestate = record.get("prestate")
        if not isinstance(prestate, dict):
            raise DeploymentError("deployment prestate is missing")
        expected = prestate.get("candidate_units")
        runtime_masks = prestate.get("candidate_runtime_masks")
        ownership = self._runtime_mask_ownership(record)
        if not isinstance(expected, dict) or not isinstance(runtime_masks, dict):
            raise DeploymentError("candidate unit prestate is malformed")
        restored = ownership["restored_units"]
        assert isinstance(restored, list)
        for unit in CANDIDATE_UNITS:
            before = expected.get(unit)
            if not isinstance(before, dict) or self.host.unit_info(unit) != before:
                raise DeploymentError(
                    f"candidate unit metadata did not return to its prestate: {unit}"
                )
            expected_mask = runtime_masks.get(unit)
            observed_mask = self.host.runtime_mask_attestation(unit)
            if expected_mask is None:
                if observed_mask is not None:
                    raise DeploymentError(
                        f"candidate runtime mask did not return to its prestate: {unit}"
                    )
            elif observed_mask is None or (
                not _same_runtime_mask_layout(expected_mask, observed_mask, unit)
                if unit in restored
                else observed_mask != expected_mask
            ):
                raise DeploymentError(
                    f"candidate runtime mask did not return to its prestate: {unit}"
                )

    def _deactivate_lifecycle(self, record: dict[str, object]) -> None:
        """Deactivate the candidate lifecycle with a durable pre-effect intent."""

        if self.host.lifecycle_state_exists():
            if record.get("lifecycle_deactivated"):
                raise DeploymentError("lifecycle receipt reappeared after deactivation")
            record["lifecycle_deactivation_intent"] = True
            self._write(record)
            self.host.lifecycle("deactivate", pause_text=False)
        if record.get("lifecycle_deactivation_intent"):
            if self.host.lifecycle_state_exists():
                return
            record["lifecycle_deactivated"] = True
            record["lifecycle_deactivation_intent"] = False
            self._write(record)

    def rollback(self) -> None:
        self.host.require_cgroup_v2()
        record = self._read()
        was_active = record.get("phase") == "active"
        record["phase"] = "restoring"
        self._write(record)
        errors: list[str] = []
        try:
            self._deactivate_lifecycle(record)
            if (
                not self.host.lifecycle_state_exists()
                and not record.get("lifecycle_deactivated")
                and (record.get("lifecycle_active") or was_active)
            ):
                raise DeploymentError("active lifecycle receipt is missing")
            self._assert_candidate_absent()
            self._restore_artifact(record)
            manifest = _verify_bundle_contents(Path(str(record["bundle"])))
            self._restore_files(record, manifest)
            self._remove_private_artifact_work(record)
            self._restore_owned_runtime_masks(record)
            if record.get("files_install_started"):
                record["daemon_reload_intent"] = "after-file-rollback"
                self._write(record)
                self.host.daemon_reload()
                record.pop("daemon_reload_intent", None)
                self._write(record)
            self._assert_candidate_absent()
            self._verify_restored_unit_prestate(record)
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        if errors:
            record["phase"] = "rollback-failed"
            record["rollback_errors"] = errors
            self._write(record)
            raise DeploymentError("deployment rollback was incomplete: " + "; ".join(errors))
        receipt = dict(record)
        receipt["phase"] = "restored"
        _atomic_write(self.state_path.parent / "restore-receipt.json", _canonical(receipt) + b"\n")
        self.state_path.unlink(missing_ok=True)
        _fsync_directory(self.state_path.parent)

    def deploy(self, bundle: Path, source_snapshot: Path, *, pause_text: bool) -> None:
        self.host.require_cgroup_v2()
        if self.state_path.exists():
            existing = self._read()
            if existing.get("phase") == "active":
                raise DeploymentError("active deployment must be deactivated explicitly")
            self.rollback()
        try:
            self.prepare(bundle)
            self.install(source_snapshot)
            self.activate(pause_text=pause_text)
        except BaseException:
            if self.state_path.exists():
                self.rollback()
            raise


def _lock(path: Path) -> int:
    _safe_directory(path.parent, create=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise DeploymentError("another canary deployment owner holds the lock") from exc
    return descriptor


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("plan", "prepare", "install", "activate", "deactivate", "rollback", "deploy"),
    )
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--source-snapshot", type=Path)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--pause-text", action="store_true")
    parser.add_argument("--accept-runtime-mask-prestate", action="store_true")
    parser.add_argument("--accept-unsealed-artifact-prestate", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.accept_runtime_mask_prestate and args.action not in {"plan", "prepare", "deploy"}:
        raise DeploymentError(
            "--accept-runtime-mask-prestate is valid only with plan, prepare, or deploy"
        )
    if args.accept_unsealed_artifact_prestate and args.action not in {
        "plan",
        "prepare",
        "deploy",
    }:
        raise DeploymentError(
            "--accept-unsealed-artifact-prestate is valid only with plan, prepare, or deploy"
        )
    state = args.state.expanduser().resolve(strict=False)
    descriptor = _lock(state.with_suffix(".lock"))
    try:
        source_root = args.source_root.expanduser().resolve(strict=True)
        owner = Deployment(
            SystemHost(model_root=args.artifact),
            state,
            source_root=source_root,
            artifact_path=args.artifact,
            accept_runtime_mask_prestate=args.accept_runtime_mask_prestate,
            accept_unsealed_artifact_prestate=args.accept_unsealed_artifact_prestate,
        )
        if args.action in {"prepare", "deploy"} and state.exists():
            existing = owner._read()
            if existing.get("phase") == "active":
                raise DeploymentError("active deployment must be deactivated explicitly")
            owner.rollback()
        bundle_root = args.bundle_root or state.parent / "bundles"
        bundle: Path | None = None
        if args.action not in {"deactivate", "rollback"}:
            bundle = args.bundle or build_bundle(source_root, bundle_root)
        if args.action == "plan":
            assert bundle is not None
            sys.stdout.write(_canonical(owner.plan(bundle)).decode() + "\n")
        elif args.action == "prepare":
            assert bundle is not None
            owner.prepare(bundle)
        elif args.action == "install":
            if args.source_snapshot is None:
                raise DeploymentError("install requires --source-snapshot")
            owner.install(args.source_snapshot)
        elif args.action == "activate":
            owner.activate(pause_text=args.pause_text)
        elif args.action in {"deactivate", "rollback"}:
            if args.pause_text:
                raise DeploymentError("--pause-text is valid only with activate or deploy")
            owner.rollback()
        else:
            if args.source_snapshot is None:
                raise DeploymentError("deploy requires --source-snapshot")
            assert bundle is not None
            owner.deploy(bundle, args.source_snapshot, pause_text=args.pause_text)
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
