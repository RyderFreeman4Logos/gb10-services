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
import os
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


BUNDLE_SCHEMA = "gb10-querit-canary-bundle-v1"
STATE_SCHEMA = "gb10-querit-canary-deployment-v1"
DEFAULT_STATE = Path("/home/obj/.local/state/gb10-querit-canary-deployment/state.json")
DEFAULT_ARTIFACT = Path("/home/obj/models/querit-4b-vllm")
DEFAULT_LIFECYCLE_STATE = Path("/home/obj/.local/state/gb10-querit-canary/state.json")
LIB_ROOT = Path("/home/obj/.local/lib/gb10")
BIN_ROOT = Path("/home/obj/.local/bin")
UNIT_ROOT = Path("/home/obj/.config/systemd/user")
CANDIDATE_UNITS = (runtime.BACKEND_UNIT, runtime.ADAPTER_UNIT)


class DeploymentError(RuntimeError):
    """The source-controlled deployment contract was not satisfied."""


class Host(Protocol):
    def unit_info(self, unit: str) -> dict[str, str]: ...

    def service_state(self, unit: str) -> runtime.ServiceState: ...

    def runtime_mask(self, unit: str) -> None: ...

    def runtime_unmask(self, unit: str) -> None: ...

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
            mem_available = self._runtime.memory_available_gib()
            swaps = Path("/proc/swaps").read_text()
            pressure = Path("/proc/pressure/memory").read_text()
        except (OSError, UnicodeError, runtime.LifecycleError) as exc:
            raise DeploymentError("cannot collect memory/swap/PSI admission facts") from exc
        return {
            "mem_available_gib": mem_available,
            "pressure_sha256": _sha256(pressure.encode()),
            "swaps_sha256": _sha256(swaps.encode()),
        }

    def convert(self, converter: Path, snapshot: Path, template: Path) -> None:
        self._run(
            ["/usr/bin/python3", str(converter), str(snapshot), "--template", str(template)],
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
    expected = _bundle_manifest(expected_entries, head)
    if manifest != expected or bundle.name != expected["bundle_sha256"]:
        raise DeploymentError("bundle manifest identity is invalid")
    return manifest


def _unit_target(unit: str) -> Path:
    for mapped in TARGETS:
        if Path(str(mapped["target"])).name == unit:
            return Path(str(mapped["target"]))
    raise AssertionError(f"missing target mapping for {unit}")


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


def _artifact_snapshot(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DeploymentError("candidate artifact path is not a real directory")
    return {
        "exists": True,
        "manifest_sha256": artifact.manifest_sha256(path),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


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
    ) -> None:
        self.host = host
        self.state_path = state_path.expanduser().resolve(strict=False)
        self.source_root = source_root.expanduser().resolve(strict=True)
        self.artifact_path = artifact_path.expanduser().resolve(strict=False)
        self.lifecycle_state = lifecycle_state.expanduser().resolve(strict=False)

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
            "artifact": _artifact_snapshot(self.artifact_path),
            "bundle_sha256": manifest["bundle_sha256"],
            "candidate_containers": containers,
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
            state = runtime.ServiceState.from_record(states.get(unit), unit)
            if not _quiescent(state):
                raise DeploymentError("candidate unit must be fully inactive before deployment")
        units = prestate.get("candidate_units")
        if not isinstance(units, dict):
            raise DeploymentError("candidate unit metadata is invalid")
        for unit in CANDIDATE_UNITS:
            info = units.get(unit)
            if not isinstance(info, dict):
                raise DeploymentError("candidate unit metadata is malformed")
            state = info.get("UnitFileState")
            if state == "masked":
                raise DeploymentError(f"persistent candidate mask blocks deployment: {unit}")
            if state == "masked-runtime":
                raise DeploymentError(f"foreign runtime candidate mask blocks deployment: {unit}")
            if state != "disabled":
                raise DeploymentError(f"candidate unit must remain disabled: {unit}")
            if info.get("DropInPaths"):
                raise DeploymentError(f"candidate unit has unexpected drop-ins: {unit}")
            target = _unit_target(unit)
            snapshot = prestate["files"].get(str(target))  # type: ignore[index]
            if not isinstance(snapshot, dict):
                raise DeploymentError("candidate unit file snapshot is missing")
            if snapshot.get("exists"):
                expected = next(
                    entry for entry in manifest["files"]  # type: ignore[index]
                    if entry["target"] == str(target)
                )
                if (
                    snapshot.get("sha256") != expected["sha256"]
                    or snapshot.get("mode") != expected["mode"]
                    or info.get("FragmentPath") != str(target)
                ):
                    raise DeploymentError(
                        f"candidate unit bytes or FragmentPath drifted: {unit}"
                    )
            elif info.get("FragmentPath"):
                raise DeploymentError(f"candidate unit loaded from an unexpected path: {unit}")
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
        if prestate.get("artifact") != _artifact_snapshot(self.artifact_path):
            raise DeploymentError("candidate artifact prestate drifted before installation")
        if prestate.get("listeners") != list(self.host.listeners()):
            raise DeploymentError("candidate listener prestate drifted before installation")
        for unit in CANDIDATE_UNITS:
            info = self.host.unit_info(unit)
            previous = prestate["candidate_units"].get(unit)  # type: ignore[index]
            if not isinstance(previous, dict) or any(
                info.get(field) != previous.get(field)
                for field in ("FragmentPath", "DropInPaths", "LoadState")
            ) or info.get("UnitFileState") != "masked-runtime":
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
        if prestate.get("admission") != self.host.admission():
            raise DeploymentError("memory/swap/PSI admission facts drifted before installation")

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
        manifest = verify_bundle(bundle, self.source_root)
        if self.state_path.exists():
            raise DeploymentError("existing deployment receipt must be recovered before prepare")
        prestate = self._capture_prestate(manifest)
        self._validate_prestate(prestate, manifest)
        record: dict[str, object] = {
            "bundle": str(bundle.expanduser().resolve(strict=True)),
            "lifecycle_deactivated": False,
            "owned_runtime_masks": [],
            "runtime_masks_restored": [],
            "phase": "preparing",
            "prestate": prestate,
            "schema": STATE_SCHEMA,
        }
        self._write(record)
        for unit in CANDIDATE_UNITS:
            self.host.runtime_mask(unit)
            cast_masks = record["owned_runtime_masks"]
            assert isinstance(cast_masks, list)
            cast_masks.append(unit)
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
        self._write(record)
        bundle_root = Path(str(record["bundle"]))
        self.host.convert(
            bundle_root / "payload" / "scripts" / "querit_checkpoint_convert.py",
            stage,
            bundle_root / "payload" / "config" / "querit" / "querit-rerank.jinja",
        )
        try:
            new_manifest = artifact.manifest_sha256(stage)
        except artifact.ArtifactError as exc:
            raise DeploymentError("converted owner artifact failed validation") from exc
        artifact_record = record["prestate"]  # type: ignore[index]
        assert isinstance(artifact_record, dict)
        previous = artifact_record["artifact"]
        assert isinstance(previous, dict)
        backup = self.artifact_path.parent / f".gb10-querit-previous-{manifest['bundle_sha256']}"
        if backup.exists():
            raise DeploymentError("previous candidate artifact backup path already exists")
        record["artifact_publication"] = {
            "new_manifest_sha256": new_manifest,
            "previous_backup": str(backup),
            "state": "staged",
        }
        self._write(record)
        if self.artifact_path.exists():
            os.replace(self.artifact_path, backup)
            _fsync_directory(self.artifact_path.parent)
            record["artifact_publication"]["state"] = "previous-moved"  # type: ignore[index]
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
        self._write(record)
        for entry in manifest["files"]:  # type: ignore[index]
            assert isinstance(entry, dict)
            source = bundle / "payload" / str(entry["path"])
            target = Path(str(entry["target"]))
            _copy_file(source, target, mode=int(entry["mode"]))
        record["files_installed"] = True
        self._write(record)
        self.host.daemon_reload()
        self._restore_owned_runtime_masks(record)
        self.host.daemon_reload()
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
            if (
                info.get("FragmentPath") != str(_unit_target(unit))
                or info.get("DropInPaths")
                or info.get("UnitFileState") != "disabled"
            ):
                raise DeploymentError(f"loaded candidate unit is not the installed disabled unit: {unit}")

    def install(self, source_snapshot: Path) -> None:
        record = self._read()
        if record.get("phase") != "prepared":
            raise DeploymentError("deployment must be prepared before installation")
        bundle = Path(str(record.get("bundle", "")))
        manifest = verify_bundle(bundle, self.source_root)
        self._reattest_preinstall(record, manifest)
        record["phase"] = "installing"
        self._write(record)
        self._publish_artifact(record, manifest, source_snapshot)
        self._install_files(record, manifest)
        record["phase"] = "installed"
        self._write(record)

    def activate(self, *, pause_text: bool) -> None:
        record = self._read()
        if record.get("phase") != "installed":
            raise DeploymentError("deployment must be installed before activation")
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
        record["pause_text"] = pause_text
        self._write(record)
        self.host.lifecycle("activate", pause_text=pause_text)
        record["lifecycle_active"] = True
        record["phase"] = "active"
        self._write(record)
        _atomic_write(self.state_path.parent / "active-receipt.json", _canonical(record) + b"\n")

    def _restore_artifact(self, record: dict[str, object]) -> None:
        publication = record.get("artifact_publication")
        if not isinstance(publication, dict):
            return
        state = publication.get("state")
        if state not in {"staged", "previous-moved", "published"}:
            raise DeploymentError("artifact publication state is invalid")
        backup = Path(str(publication.get("previous_backup", "")))
        prestate = record.get("prestate")
        assert isinstance(prestate, dict)
        previous = prestate.get("artifact")
        assert isinstance(previous, dict)
        restored = _artifact_snapshot(self.artifact_path)
        if record.get("artifact_restored"):
            if restored != previous or backup.exists():
                raise DeploymentError("restored artifact no longer matches its prestate")
            return
        if restored == previous and not backup.exists():
            record["artifact_restored"] = True
            self._write(record)
            return
        if state == "staged":
            raise DeploymentError("staged artifact publication drifted before rollback")
        if self.artifact_path.exists():
            expected = publication.get("new_manifest_sha256")
            if (
                not isinstance(expected, str)
                or artifact.manifest_sha256(self.artifact_path) != expected
            ):
                raise DeploymentError("refusing to remove artifact whose identity changed")
            shutil.rmtree(self.artifact_path)
        if previous.get("exists"):
            if not backup.exists():
                raise DeploymentError("previous artifact backup is missing")
            os.replace(backup, self.artifact_path)
            _fsync_directory(self.artifact_path.parent)
            if _artifact_snapshot(self.artifact_path) != previous:
                raise DeploymentError("restored artifact does not match its prestate")
        elif backup.exists():
            raise DeploymentError("unexpected prior artifact backup exists")
        if _artifact_snapshot(self.artifact_path) != previous:
            raise DeploymentError("restored artifact does not match its prestate")
        record["artifact_restored"] = True
        self._write(record)

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

    def _restore_files(self, record: Mapping[str, object], manifest: Mapping[str, object]) -> None:
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
                target.unlink()
        for target_name, snapshot in file_prestate.items():
            if not isinstance(snapshot, dict) or not _same_snapshot(
                snapshot, _snapshot_file(Path(target_name))
            ):
                raise DeploymentError(f"file rollback did not restore {target_name}")

    def _assert_candidate_absent(self) -> None:
        if self.host.listeners():
            raise DeploymentError("candidate listener remains after lifecycle rollback")
        for unit in CANDIDATE_UNITS:
            if not _quiescent(self.host.service_state(unit)):
                raise DeploymentError(f"candidate unit/PIDs remain after lifecycle rollback: {unit}")
            if _candidate_container(self.host, unit) is not None:
                raise DeploymentError(f"candidate container remains after lifecycle rollback: {unit}")

    def _restore_owned_runtime_masks(self, record: dict[str, object]) -> None:
        prestate = record.get("prestate")
        if not isinstance(prestate, dict):
            raise DeploymentError("deployment prestate is missing")
        units = prestate.get("candidate_units")
        if not isinstance(units, dict):
            raise DeploymentError("candidate unit prestate is malformed")
        owned = record.get("owned_runtime_masks")
        restored = record.get("runtime_masks_restored")
        if (
            not isinstance(owned, list)
            or not isinstance(restored, list)
            or any(not isinstance(unit, str) for unit in (*owned, *restored))
            or len(set(owned)) != len(owned)
            or len(set(restored)) != len(restored)
            or not set(owned).issubset(CANDIDATE_UNITS)
            or not set(restored).issubset(owned)
        ):
            raise DeploymentError("owned runtime-mask record is invalid")
        for unit in owned:
            before = units.get(unit)
            if not isinstance(before, dict):
                raise DeploymentError("candidate unit prestate is malformed")
            expected = before.get("UnitFileState")
            if expected != "disabled":
                raise DeploymentError("candidate mask prestate is invalid")
            observed = self.host.unit_info(unit).get("UnitFileState")
            if unit in restored:
                if observed != expected:
                    raise DeploymentError(
                        f"candidate mask prestate drifted after owner restoration: {unit}"
                    )
                continue
            if observed == expected:
                restored.append(unit)
                self._write(record)
                continue
            if observed != "masked-runtime":
                raise DeploymentError(
                    f"owned candidate runtime mask is no longer present: {unit}"
                )
            self.host.runtime_unmask(unit)
            if self.host.unit_info(unit).get("UnitFileState") != expected:
                raise DeploymentError(f"candidate runtime mask did not restore: {unit}")
            restored.append(unit)
            self._write(record)

    def _verify_restored_unit_prestate(self, record: Mapping[str, object]) -> None:
        prestate = record.get("prestate")
        if not isinstance(prestate, dict):
            raise DeploymentError("deployment prestate is missing")
        expected = prestate.get("candidate_units")
        if not isinstance(expected, dict):
            raise DeploymentError("candidate unit prestate is malformed")
        for unit in CANDIDATE_UNITS:
            before = expected.get(unit)
            if not isinstance(before, dict) or self.host.unit_info(unit) != before:
                raise DeploymentError(
                    f"candidate unit metadata did not return to its prestate: {unit}"
                )

    def rollback(self) -> None:
        record = self._read()
        was_active = record.get("phase") == "active"
        record["phase"] = "restoring"
        self._write(record)
        errors: list[str] = []
        try:
            if self.host.lifecycle_state_exists():
                if record.get("lifecycle_deactivated"):
                    raise DeploymentError("lifecycle receipt reappeared after deactivation")
                self.host.lifecycle("deactivate", pause_text=False)
                record["lifecycle_deactivated"] = True
                self._write(record)
            elif (
                not record.get("lifecycle_deactivated")
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
                self.host.daemon_reload()
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
    parser.add_argument("action", choices=("prepare", "install", "activate", "deactivate", "rollback", "deploy"))
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--source-snapshot", type=Path)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--pause-text", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    state = args.state.expanduser().resolve(strict=False)
    descriptor = _lock(state.with_suffix(".lock"))
    try:
        source_root = args.source_root.expanduser().resolve(strict=True)
        owner = Deployment(
            SystemHost(model_root=args.artifact),
            state,
            source_root=source_root,
            artifact_path=args.artifact,
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
        if args.action == "prepare":
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
