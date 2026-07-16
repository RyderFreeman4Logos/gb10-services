#!/usr/bin/env python3
"""Durable, fail-closed activation for the single embedding systemd unit."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_IMPORT_AUTHORITY: dict[str, str] = {
    "gb10_embedding_activation_checks.py": "9c4720fff0d7eaf44e19ca6ce018b8a4688bf1ecafbb4ac78de3de6a2f1a84d6",
    "gb10_embedding_activation_config.py": "9a0f87fa66d55c59d755525eb779ed00aa6540375bfbf3855a6e443aef6e093f",
    "gb10_embedding_activation_storage.py": "38f93b566019b8a29b4c84dbe6882b4e55eb9ebcce75288d3d356953a625b339",
    "gb10_embedding_profile_contract.py": "be3a02e1603803826f0ec843cccce27e48ae832d2e8e8b716dce65dc3b69eaf2",
    "gb10_embedding_verifier_runtime.py": "599af1c802e1a0d3e942fb0b16cdfd3a66f9e928ab64eabf3fb455ec007df629",
    "gb10_verify_embedding_profile.py": "104f1d1219861ff463ef2fd2acbabd200aada66a9d901300fb1e17dc25d1fb73",
}


def _verify_import_authority(script_directory: Path) -> None:
    for name, expected in EXPECTED_IMPORT_AUTHORITY.items():
        path = script_directory / name
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o022 != 0
                or metadata.st_size > 1024 * 1024
            ):
                raise RuntimeError(f"unsafe activation import authority: {name}")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 65536):
                digest.update(chunk)
        finally:
            os.close(descriptor)
        if digest.hexdigest() != expected:
            raise RuntimeError(f"activation import authority differs: {name}")


_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
_verify_import_authority(_SCRIPT_DIRECTORY)
sys.path.insert(0, str(_SCRIPT_DIRECTORY))
from gb10_embedding_activation_checks import (  # noqa: E402
    Generation,
    RuntimeConfig,
    UNIT,
    capture_baselines as _capture_baselines,
    neighbors as _neighbors,
    query_generation as _query_generation,
    run_systemctl as _run_systemctl,
    wait_new_generation as _wait_new_generation,
)
from gb10_embedding_activation_config import (  # noqa: E402
    production_config as _production_config,
    test_config as _test_config,
)
from gb10_embedding_activation_storage import (  # noqa: E402
    atomic_json as _atomic_json,
    atomic_write as _atomic_write,
    fsync_directory as _fsync_directory,
    secure_directory as _secure_directory,
    secure_regular as _secure_regular,
)
from gb10_embedding_profile_contract import validate_unit_text  # noqa: E402
from gb10_embedding_verifier_runtime import (  # noqa: E402
    command,
    json_file,
    json_text,
    read_nofollow,
)
from gb10_verify_embedding_profile import verify_production  # noqa: E402

EXPECTED_VERIFIER_AUTHORITY: dict[str, str] = {
    name: EXPECTED_IMPORT_AUTHORITY[name]
    for name in (
        "gb10_verify_embedding_profile.py",
        "gb10_embedding_profile_contract.py",
        "gb10_embedding_verifier_runtime.py",
    )
}
PHASES = {
    "prepared",
    "installed",
    "reloaded",
    "restarted",
    "verified",
    "rolling_back",
    "rollback_failed",
    "committed",
}
TRANSITIONS = {
    "prepared": {"installed", "rolling_back"},
    "installed": {"reloaded", "rolling_back"},
    "reloaded": {"restarted", "rolling_back"},
    "restarted": {"verified", "rolling_back"},
    "verified": {"committed", "rolling_back"},
    "rolling_back": {"rolling_back", "rollback_failed"},
    "rollback_failed": {"rolling_back", "rollback_failed"},
    "committed": set(),
}


class ActivationInterrupted(RuntimeError):
    """Signal converted to a recoverable pre-commit failure."""


@dataclass(frozen=True)
class SourceSnapshot:
    data: bytes
    mode: int
    identity: tuple[int, int, int, int]
    digest: str


class TransactionError(RuntimeError):
    pass


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _source_snapshot(path: Path, *, require_mode: int | None = None) -> SourceSnapshot:
    metadata = _secure_regular(path, require_mode)
    data = read_nofollow(path, 1024 * 1024)
    identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    return SourceSnapshot(data, stat.S_IMODE(metadata.st_mode), identity, _sha256(data))


def _verifier_authority_snapshot(config: RuntimeConfig) -> dict[str, dict[str, Any]]:
    authority: dict[str, dict[str, Any]] = {}
    for path in config.verifier_authority:
        snapshot = _source_snapshot(path)
        if not config.test_only:
            expected = EXPECTED_VERIFIER_AUTHORITY.get(path.name)
            if expected is None or snapshot.digest != expected:
                raise TransactionError(
                    f"production verifier authority differs: {path.name}"
                )
        authority[str(path)] = {
            "identity": list(snapshot.identity),
            "mode": snapshot.mode,
            "sha256": snapshot.digest,
        }
    if len(authority) != len(config.verifier_authority):
        raise TransactionError("duplicate verifier authority path")
    return authority


def _verify_verifier_authority(
    config: RuntimeConfig, manifest: dict[str, Any]
) -> None:
    if _verifier_authority_snapshot(config) != manifest["verifier_authority"]:
        raise TransactionError("strict verifier authority drifted during activation")


def _test_boundary(config: RuntimeConfig, boundary: str, deadline: float) -> None:
    if not config.test_only:
        return
    if config.fail_at == boundary:
        raise TransactionError(f"test-only injected failure at {boundary}")
    if config.pause_at != boundary:
        return
    if config.marker is None or config.release is None:
        raise TransactionError("test-only pause lacks marker/release paths")
    _atomic_write(config.marker, b"reached\n", 0o600, replace=True)
    while not config.release.exists():
        if time.monotonic() >= deadline:
            raise TransactionError(f"test-only pause timed out at {boundary}")
        time.sleep(0.02)


def _read_phase(config: RuntimeConfig) -> str:
    _secure_directory(config.transaction, 0o700)
    _secure_regular(config.transaction / "phase", 0o600)
    payload = read_nofollow(config.transaction / "phase", 64, owner_only=True)
    try:
        phase = payload.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError as error:
        raise TransactionError("transaction phase is not ASCII") from error
    if payload != f"{phase}\n".encode() or phase not in PHASES:
        raise TransactionError("transaction phase is invalid")
    return phase


def _set_phase(config: RuntimeConfig, phase: str) -> None:
    previous = _read_phase(config)
    if phase not in TRANSITIONS[previous]:
        raise TransactionError(f"illegal transaction transition: {previous}->{phase}")
    _atomic_write(config.transaction / "phase", f"{phase}\n".encode(), 0o600)


def _load_manifest(config: RuntimeConfig) -> dict[str, Any]:
    _secure_directory(config.state_root, 0o700)
    _secure_directory(config.transaction, 0o700)
    manifest_path = config.transaction / "manifest.json"
    complete_path = config.transaction / "complete"
    _secure_regular(manifest_path, 0o600)
    _secure_regular(complete_path, 0o600)
    manifest_bytes = read_nofollow(manifest_path, 1024 * 1024, owner_only=True)
    complete = read_nofollow(complete_path, 256, owner_only=True)
    if complete != f"manifest_sha256={_sha256(manifest_bytes)}\n".encode():
        raise TransactionError("transaction completeness receipt differs")
    manifest = json_text(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "source",
        "installed",
        "source_sha256",
        "source_mode",
        "source_identity",
        "prior",
        "before",
        "neighbors",
        "verifier_authority",
    }:
        raise TransactionError("transaction manifest fields are invalid")
    if (
        manifest["schema"] != 1
        or manifest["source"] != str(config.source_unit)
        or manifest["installed"] != str(config.installed_unit)
        or manifest["source_mode"] != 0o644
        or not isinstance(manifest["source_identity"], list)
        or len(manifest["source_identity"]) != 4
        or any(not isinstance(value, int) or isinstance(value, bool) for value in manifest["source_identity"])
        or not isinstance(manifest["source_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest["source_sha256"]) is None
    ):
        raise TransactionError("transaction source authority is invalid")
    verifier_authority = manifest["verifier_authority"]
    if (
        not isinstance(verifier_authority, dict)
        or set(verifier_authority) != {str(path) for path in config.verifier_authority}
    ):
        raise TransactionError("transaction verifier authority paths are invalid")
    for receipt in verifier_authority.values():
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"identity", "mode", "sha256"}
            or not isinstance(receipt["identity"], list)
            or len(receipt["identity"]) != 4
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in receipt["identity"]
            )
            or not isinstance(receipt["mode"], int)
            or isinstance(receipt["mode"], bool)
            or not 0 <= receipt["mode"] <= 0o7777
            or not isinstance(receipt["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]) is None
        ):
            raise TransactionError("transaction verifier authority receipt is invalid")
    prior = manifest["prior"]
    if not isinstance(prior, dict) or set(prior) != {"present", "mode", "sha256"}:
        raise TransactionError("transaction prior-unit receipt is invalid")
    if not isinstance(prior["present"], bool):
        raise TransactionError("transaction prior-unit presence is invalid")
    backup = config.transaction / "unit.before"
    if prior["present"]:
        if (
            not isinstance(prior["mode"], int)
            or isinstance(prior["mode"], bool)
            or not 0 <= prior["mode"] <= 0o7777
            or not isinstance(prior["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", prior["sha256"]) is None
        ):
            raise TransactionError("transaction prior-unit metadata is invalid")
        _secure_regular(backup, 0o600)
        if _sha256(read_nofollow(backup, 1024 * 1024, owner_only=True)) != prior["sha256"]:
            raise TransactionError("transaction prior-unit backup is corrupt")
    elif prior["mode"] is not None or prior["sha256"] is not None or backup.exists() or backup.is_symlink():
        raise TransactionError("absent prior-unit receipt has backup data")
    if not isinstance(manifest["before"], dict) or not isinstance(manifest["neighbors"], dict):
        raise TransactionError("transaction generation snapshots are invalid")
    return manifest


def _manifest_generation(payload: dict[str, Any]) -> Generation:
    keys = {"load", "active", "sub", "fragment", "pid", "cgroup", "invocation", "started"}
    if set(payload) != keys:
        raise TransactionError("manifest generation fields are invalid")
    for field in ("load", "active", "sub", "fragment", "cgroup", "invocation"):
        if not isinstance(payload[field], str):
            raise TransactionError("manifest generation string is invalid")
    for field in ("pid", "started"):
        if not isinstance(payload[field], int) or isinstance(payload[field], bool) or payload[field] < 0:
            raise TransactionError("manifest generation number is invalid")
    return Generation(**payload)


def _prepare_transaction(config: RuntimeConfig, deadline: float) -> dict[str, Any]:
    source = _source_snapshot(config.source_unit, require_mode=0o644)
    verifier_authority = _verifier_authority_snapshot(config)
    try:
        validate_unit_text(source.data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise TransactionError("canonical source unit is not UTF-8") from error
    prior_present = config.installed_unit.exists() or config.installed_unit.is_symlink()
    prior_snapshot: SourceSnapshot | None = None
    if prior_present:
        prior_snapshot = _source_snapshot(config.installed_unit)
    first_generation = _query_generation(config, deadline)
    if not first_generation.running:
        raise TransactionError("embedding service must be active/running before activation")
    first_neighbors = _neighbors(config, deadline)
    baselines = _capture_baselines(config, deadline)
    second_neighbors = _neighbors(config, deadline)
    second_generation = _query_generation(config, deadline)
    if first_generation.stable != second_generation.stable:
        raise TransactionError("embedding generation changed during pre-state capture")
    if first_neighbors != second_neighbors:
        raise TransactionError("neighbor generation changed during pre-state capture")
    if _source_snapshot(config.source_unit, require_mode=0o644) != source:
        raise TransactionError("canonical source unit mutated during pre-state capture")
    if _verifier_authority_snapshot(config) != verifier_authority:
        raise TransactionError("strict verifier authority mutated during pre-state capture")
    if prior_snapshot is not None and _source_snapshot(config.installed_unit) != prior_snapshot:
        raise TransactionError("installed unit mutated during pre-state capture")
    if prior_snapshot is None and (config.installed_unit.exists() or config.installed_unit.is_symlink()):
        raise TransactionError("installed unit appeared during pre-state capture")
    before = {
        "load": first_generation.load,
        "active": first_generation.active,
        "sub": first_generation.sub,
        "fragment": first_generation.fragment,
        "pid": first_generation.pid,
        "cgroup": first_generation.cgroup,
        "invocation": first_generation.invocation,
        "started": first_generation.started,
    }
    prior = {
        "present": prior_snapshot is not None,
        "mode": prior_snapshot.mode if prior_snapshot is not None else None,
        "sha256": prior_snapshot.digest if prior_snapshot is not None else None,
    }
    manifest = {
        "schema": 1,
        "source": str(config.source_unit),
        "installed": str(config.installed_unit),
        "source_sha256": source.digest,
        "source_mode": source.mode,
        "source_identity": list(source.identity),
        "prior": prior,
        "before": before,
        "neighbors": first_neighbors,
        "verifier_authority": verifier_authority,
    }
    temporary = config.state_root / f".{config.transaction.name}.tmp.{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise TransactionError("transaction temporary path already exists")
    os.mkdir(temporary, 0o700)
    try:
        if prior_snapshot is not None:
            _atomic_write(temporary / "unit.before", prior_snapshot.data, 0o600, replace=False)
        _atomic_json(temporary / "baselines.json", baselines, replace=False)
        _atomic_json(
            temporary / "systemd.before.json",
            {
                "InvocationID": first_generation.invocation,
                "MainPID": first_generation.pid,
                "ExecMainStartTimestampMonotonic": first_generation.started,
            },
            replace=False,
        )
        manifest_bytes = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode()
        _atomic_write(temporary / "manifest.json", manifest_bytes, 0o600, replace=False)
        _atomic_write(temporary / "phase", b"prepared\n", 0o600, replace=False)
        _atomic_write(
            temporary / "complete",
            f"manifest_sha256={_sha256(manifest_bytes)}\n".encode(),
            0o600,
            replace=False,
        )
        _fsync_directory(temporary)
        os.replace(temporary, config.transaction)
        _fsync_directory(config.state_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _load_manifest(config)
    return manifest


def _verify_source_authority(config: RuntimeConfig, manifest: dict[str, Any]) -> SourceSnapshot:
    source = _source_snapshot(config.source_unit, require_mode=0o644)
    if source.digest != manifest["source_sha256"] or list(source.identity) != manifest["source_identity"]:
        raise TransactionError("canonical source unit drifted after transaction preparation")
    validate_unit_text(source.data.decode("utf-8"))
    return source


def _install_source(config: RuntimeConfig, manifest: dict[str, Any]) -> None:
    source = _verify_source_authority(config, manifest)
    _atomic_write(config.installed_unit, source.data, 0o644)
    installed = _source_snapshot(config.installed_unit, require_mode=0o644)
    if installed.data != source.data or installed.digest != source.digest:
        raise TransactionError("atomic embedding unit installation differs from source")
    _verify_source_authority(config, manifest)

def _verify_prior_restored(config: RuntimeConfig, manifest: dict[str, Any]) -> None:
    prior = manifest["prior"]
    if prior["present"]:
        restored = _source_snapshot(config.installed_unit)
        if restored.digest != prior["sha256"] or restored.mode != prior["mode"]:
            raise TransactionError("rollback did not restore exact prior unit bytes and mode")
    elif config.installed_unit.exists() or config.installed_unit.is_symlink():
        raise TransactionError("rollback did not restore explicit prior absence")


def _restore_prior(config: RuntimeConfig, manifest: dict[str, Any]) -> None:
    prior = manifest["prior"]
    if prior["present"]:
        backup = read_nofollow(
            config.transaction / "unit.before", 1024 * 1024, owner_only=True
        )
        if _sha256(backup) != prior["sha256"]:
            raise TransactionError("prior embedding backup changed before rollback")
        _atomic_write(config.installed_unit, backup, prior["mode"])
    elif config.installed_unit.exists() or config.installed_unit.is_symlink():
        _secure_regular(config.installed_unit)
        config.installed_unit.unlink()
        _fsync_directory(config.unit_dir)
    _verify_prior_restored(config, manifest)


def _write_public_receipt(config: RuntimeConfig, name: str, payload: dict[str, Any]) -> None:
    _atomic_json(config.state_root / name, payload, replace=True)


def _cleanup_transaction(config: RuntimeConfig) -> None:
    _secure_directory(config.transaction, 0o700)
    tombstone = config.state_root / ".transaction.v1.cleanup"
    if tombstone.exists() or tombstone.is_symlink():
        raise TransactionError("transaction cleanup tombstone already exists")
    os.replace(config.transaction, tombstone)
    _fsync_directory(config.state_root)
    shutil.rmtree(tombstone)
    _fsync_directory(config.state_root)


def _rollback(config: RuntimeConfig, deadline: float) -> bool:
    previous_handlers: dict[int, Any] = {}
    deferred: list[int] = []

    def defer(signum: int, _frame: Any) -> None:
        deferred.append(signum)

    try:
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, defer)
        return _rollback_uninterrupted(config, deadline)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if deferred:
            print(
                "signals deferred until embedding rollback completed: "
                + ",".join(str(signum) for signum in deferred),
                file=sys.stderr,
            )


def _rollback_uninterrupted(config: RuntimeConfig, deadline: float) -> bool:
    try:
        manifest = _load_manifest(config)
        phase = _read_phase(config)
        if phase == "committed":
            return True
        if phase != "rolling_back":
            _set_phase(config, "rolling_back")
        _test_boundary(config, "rollback_started", deadline)
        try:
            failed_generation = _query_generation(config, deadline)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            failed_generation = _manifest_generation(manifest["before"])
        _restore_prior(config, manifest)
        _run_systemctl(config, deadline, "daemon-reload")
        prior_generation = _manifest_generation(manifest["before"])
        if not prior_generation.running:
            raise TransactionError("transaction prior generation was not active/running")
        _run_systemctl(config, deadline, "restart", UNIT)
        restored_generation = _wait_new_generation(
            config, failed_generation, deadline
        )
        if _neighbors(config, deadline) != manifest["neighbors"]:
            raise TransactionError("neighbor generation changed during rollback")
        final_generation = _query_generation(config, deadline)
        if final_generation.stable != restored_generation.stable:
            raise TransactionError("restored embedding generation changed before rollback commit")
        _verify_prior_restored(config, manifest)
        pending_receipt = config.transaction / "activation.receipt.json"
        public_receipt = config.state_root / "activation.receipt.json"
        if pending_receipt.exists() or pending_receipt.is_symlink():
            _secure_regular(pending_receipt, 0o600)
            pending_bytes = read_nofollow(
                pending_receipt, 1024 * 1024, owner_only=True
            )
            if public_receipt.exists() or public_receipt.is_symlink():
                _secure_regular(public_receipt, 0o600)
                if read_nofollow(
                    public_receipt, 1024 * 1024, owner_only=True
                ) != pending_bytes:
                    raise TransactionError(
                        "public activation receipt differs from pending transaction"
                    )
                public_receipt.unlink()
                _fsync_directory(config.state_root)
        _write_public_receipt(
            config,
            "rollback.receipt.json",
            {
                "generation_proved": True,
                "neighbor_generations_unchanged": True,
                "restored_mode": manifest["prior"]["mode"],
                "restored_presence": manifest["prior"]["present"],
                "rollback": "passed",
                "unit": UNIT,
            },
        )
        _cleanup_transaction(config)
        return True
    except BaseException as error:
        print(f"embedding rollback failed: {error}", file=sys.stderr)
        try:
            if config.transaction.exists() and _read_phase(config) != "committed":
                current = _read_phase(config)
                if current != "rollback_failed":
                    if current != "rolling_back":
                        _set_phase(config, "rolling_back")
                    _set_phase(config, "rollback_failed")
        except BaseException:
            pass
        return False


def _cleanup_temporary_paths(config: RuntimeConfig) -> None:
    for candidate in config.state_root.glob(".transaction.v1.tmp.*"):
        _secure_directory(candidate, 0o700)
        shutil.rmtree(candidate)
    tombstone = config.state_root / ".transaction.v1.cleanup"
    if tombstone.exists() or tombstone.is_symlink():
        _secure_directory(tombstone, 0o700)
        shutil.rmtree(tombstone)
    for name in ("activation.receipt.json", "rollback.receipt.json"):
        for candidate in config.state_root.glob(f".{name}.tmp.*"):
            _secure_regular(candidate, 0o600)
            candidate.unlink()
    _fsync_directory(config.state_root)
    removed_unit_temporary = False
    for candidate in config.unit_dir.glob(f".{UNIT}.tmp.*"):
        _secure_regular(candidate, 0o644)
        candidate.unlink()
        removed_unit_temporary = True
    if removed_unit_temporary:
        _fsync_directory(config.unit_dir)


def _create_state_root(config: RuntimeConfig) -> None:
    parent = config.state_root.parent
    parent_mode = stat.S_IMODE(parent.lstat().st_mode)
    _secure_directory(parent, parent_mode)
    if parent_mode & 0o022:
        raise TransactionError("activation state parent is group/world writable")
    if not config.state_root.exists() and not config.state_root.is_symlink():
        os.mkdir(config.state_root, 0o700)
        _fsync_directory(parent)
    _secure_directory(config.state_root, 0o700)
    unit_dir_mode = stat.S_IMODE(config.unit_dir.lstat().st_mode)
    _secure_directory(config.unit_dir, unit_dir_mode)
    if unit_dir_mode & 0o022:
        raise TransactionError("systemd user unit directory is group/world writable")
    if config.installed_unit.parent != config.unit_dir:
        raise TransactionError("installed embedding unit escapes canonical unit directory")


def _acquire_lock(config: RuntimeConfig) -> int:
    lock_path = config.state_root / "activate.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise TransactionError("activation lock authority is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def activate(config: RuntimeConfig) -> int:
    _create_state_root(config)
    try:
        lock_descriptor = _acquire_lock(config)
    except BlockingIOError:
        print("another embedding activation transaction holds the lock", file=sys.stderr)
        return 1
    deadline = time.monotonic() + config.deadline_seconds
    previous_handlers: dict[int, Any] = {}

    def interrupted(signum: int, _frame: Any) -> None:
        raise ActivationInterrupted(f"received signal {signum}")

    try:
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        _cleanup_temporary_paths(config)
        if config.transaction.exists() or config.transaction.is_symlink():
            phase = _read_phase(config)
            if phase == "committed":
                _load_manifest(config)
                _cleanup_transaction(config)
            else:
                print(f"recovering stale embedding transaction in phase {phase}", file=sys.stderr)
                if not _rollback(
                    config, time.monotonic() + config.rollback_seconds
                ):
                    return 1
                print("stale embedding transaction recovered; rerun activation", file=sys.stderr)
                return 1
        manifest = _prepare_transaction(config, deadline)
        _test_boundary(config, "prepared", deadline)
        _install_source(config, manifest)
        _test_boundary(config, "after_install", deadline)
        _set_phase(config, "installed")
        _run_systemctl(config, deadline, "daemon-reload")
        _set_phase(config, "reloaded")
        before = _manifest_generation(manifest["before"])
        _run_systemctl(config, deadline, "restart", UNIT)
        current = _wait_new_generation(config, before, deadline)
        _set_phase(config, "restarted")
        _test_boundary(config, "after_restart", deadline)
        _verify_verifier_authority(config, manifest)
        if config.test_only:
            command(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "-S",
                    config.verifier,
                    str(config.transaction),
                ],
                timeout=config.command_seconds,
                deadline=deadline,
            )
        else:
            verify_production(config.transaction)
        _verify_verifier_authority(config, manifest)
        verification = json_file(
            config.transaction / "verification.receipt.json", owner_only=True
        )
        if not isinstance(verification, dict) or verification.get("verification") != "passed":
            raise TransactionError("strict verifier did not publish a passing receipt")
        if not config.test_only and (
            set(verification)
            != {
                "canary_input_count",
                "cgroup_populated",
                "engine_process_count",
                "generation_changed",
                "generation_stable",
                "kv_capacity_tokens",
                "minimum_stability_cosine",
                "profile",
                "quality_claim",
                "unit_sha256",
                "vector_dimensions",
                "verification",
            }
            or verification["profile"] != "qwen3-embedding-8b-32k-4800M-20GiB"
            or verification["quality_claim"] != "synthetic-baseline-stability-only"
            or verification["unit_sha256"] != manifest["source_sha256"]
            or verification["vector_dimensions"] != 4096
            or verification["canary_input_count"] != 3
            or verification["engine_process_count"] < 1
            or verification["kv_capacity_tokens"] < 32768
            or verification["minimum_stability_cosine"] < 0.99999
            or verification["cgroup_populated"] is not True
            or verification["generation_changed"] is not True
            or verification["generation_stable"] is not True
        ):
            raise TransactionError("strict verifier receipt contract is invalid")
        if _neighbors(config, deadline) != manifest["neighbors"]:
            raise TransactionError("activation changed a text or reranker generation")
        final_generation = _query_generation(config, deadline)
        if final_generation.stable != current.stable:
            raise TransactionError("embedding generation changed after strict verification")
        _verify_source_authority(config, manifest)
        installed = _source_snapshot(config.installed_unit, require_mode=0o644)
        if installed.digest != manifest["source_sha256"]:
            raise TransactionError("installed embedding unit drifted before commit")
        _set_phase(config, "verified")
        _test_boundary(config, "before_receipt", deadline)
        receipt = {
            "commit_requires_phase": "committed",
            "neighbor_generations_unchanged": True,
            "profile": "qwen3-embedding-8b-32k-4800M-20GiB",
            "source_sha256": manifest["source_sha256"],
            "verification": "passed",
            "unit": UNIT,
            "verification_receipt_sha256": _sha256(
                read_nofollow(
                    config.transaction / "verification.receipt.json",
                    1024 * 1024,
                    owner_only=True,
                )
            ),
        }
        _atomic_json(
            config.transaction / "activation.receipt.json", receipt, replace=False
        )
        _write_public_receipt(config, "activation.receipt.json", receipt)
        _test_boundary(config, "after_receipt", deadline)
        _set_phase(config, "committed")
        _test_boundary(config, "after_commit", deadline)
        print(
            f"embedding activation committed; private evidence: {config.transaction}"
        )
        return 0
    except BaseException as error:
        print(f"embedding activation failed: {error}", file=sys.stderr)
        try:
            if config.transaction.exists() and _read_phase(config) == "committed":
                print("durable committed phase is authoritative", file=sys.stderr)
                return 0
        except BaseException:
            pass
        if config.transaction.exists() or config.transaction.is_symlink():
            if not _rollback(
                config, time.monotonic() + config.rollback_seconds
            ):
                return 1
        return 1
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        os.close(lock_descriptor)


def main() -> int:
    if len(sys.argv) == 1:
        config = _production_config(Path(__file__))
    elif len(sys.argv) == 3 and sys.argv[1] == "--test-only":
        config = _test_config(Path(sys.argv[2]))
    else:
        print("usage: gb10_activate_embedding_profile.sh", file=sys.stderr)
        return 2
    return activate(config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"embedding activation failed closed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
