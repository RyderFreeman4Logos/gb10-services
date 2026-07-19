"""Secure durable storage and source-authority transaction helpers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from gb10_embedding_activation_checks import Generation, RuntimeConfig, UNIT
from gb10_embedding_profile_contract import validate_unit_text
from gb10_embedding_verifier_runtime import json_file, json_text, read_nofollow

EXPECTED_VERIFIER_AUTHORITY: dict[str, str] = {
    "gb10_verify_embedding_profile.py": "ec2b81b1653130615f77e463845adced13ad4e9adc75f599e58eadd7297f44f3",
    "gb10_embedding_profile_contract.py": "cbb89060e5f8d5a2391023811b1223200cdc57976d2349374705e8a0e123bce7",
    "gb10_embedding_verifier_runtime.py": "599af1c802e1a0d3e942fb0b16cdfd3a66f9e928ab64eabf3fb455ec007df629",
}
NO_SWAP_KEYS = ("core", "wrapper")
NO_SWAP_PRIVATE_FILES = {
    "core": "gb10_verify_vllm_no_swap_core.py",
    "wrapper": "gb10_verify_vllm_no_swap.sh",
}
NO_SWAP_PRIOR_FILES = {"core": "no_swap_core.before", "wrapper": "no_swap_wrapper.before"}
EXPECTED_NO_SWAP_SHA256 = {
    "core": "da4bf81f75c816a4d0beb24cb8fec8935500337484b320cbc6b43b3effe740ce",
    "wrapper": "03de16bae0d7d3214aa6cff9404a6f1173cae71e33fc181923fc6e1c6fa8e208",
}

class ActivationStorageError(RuntimeError):
    """A transaction path or filesystem operation was not safe."""


def fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def secure_directory(path: Path, mode: int) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise ActivationStorageError(f"unsafe activation directory: {path}")


def secure_regular(path: Path, mode: int | None = None) -> os.stat_result:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise ActivationStorageError(f"unsafe activation file: {path}")
    return metadata


def atomic_write(
    path: Path, payload: bytes, mode: int, *, replace: bool = True
) -> None:
    if path.exists() or path.is_symlink():
        if not replace:
            raise ActivationStorageError(
                f"activation file already exists: {path.name}"
            )
        secure_regular(path)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def atomic_json(
    path: Path, payload: dict[str, Any], *, replace: bool = True
) -> None:
    data = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()
    atomic_write(path, data, 0o600, replace=replace)

_atomic_json = atomic_json
_atomic_write = atomic_write
_fsync_directory = fsync_directory
_secure_directory = secure_directory
_secure_regular = secure_regular


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

def _no_swap_paths(config: RuntimeConfig, key: str) -> tuple[Path, Path, int]:
    if key == "core":
        return config.source_no_swap_core, config.installed_no_swap_core, 0o644
    if key == "wrapper":
        return config.source_no_swap_helper, config.installed_no_swap_helper, 0o755
    raise TransactionError("unknown fixed no-swap artifact key")


def _no_swap_source_snapshots(config: RuntimeConfig) -> dict[str, SourceSnapshot]:
    snapshots: dict[str, SourceSnapshot] = {}
    for key in NO_SWAP_KEYS:
        source, _installed, mode = _no_swap_paths(config, key)
        snapshot = _source_snapshot(source, require_mode=mode)
        if not config.test_only and snapshot.digest != EXPECTED_NO_SWAP_SHA256[key]:
            raise TransactionError(f"source no-swap {key} authority differs")
        snapshots[key] = snapshot
    return snapshots


def _no_swap_source_snapshot(config: RuntimeConfig) -> SourceSnapshot:
    return _no_swap_source_snapshots(config)["wrapper"]


def _verify_installed_no_swap_helper(config: RuntimeConfig) -> SourceSnapshot:
    sources = _no_swap_source_snapshots(config)
    installed: dict[str, SourceSnapshot] = {}
    for key in NO_SWAP_KEYS:
        _source, path, mode = _no_swap_paths(config, key)
        installed[key] = _source_snapshot(path, require_mode=mode)
        if installed[key].data != sources[key].data or installed[key].digest != sources[key].digest:
            raise TransactionError(f"installed no-swap {key} differs from source")
    return installed["wrapper"]


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

def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_identity(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    )


def _valid_mode(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0o7777


def _validate_prior_receipt(receipt: Any, backup: Path, label: str) -> None:
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"present", "mode", "sha256"}
        or not isinstance(receipt["present"], bool)
    ):
        raise TransactionError(f"transaction prior {label} receipt is invalid")
    if receipt["present"]:
        if not _valid_mode(receipt["mode"]) or not _valid_digest(receipt["sha256"]):
            raise TransactionError(f"transaction prior {label} metadata is invalid")
        _secure_regular(backup, 0o600)
        if _sha256(read_nofollow(backup, 1024 * 1024, owner_only=True)) != receipt["sha256"]:
            raise TransactionError(f"transaction prior {label} backup is corrupt")
    elif (
        receipt["mode"] is not None
        or receipt["sha256"] is not None
        or backup.exists()
        or backup.is_symlink()
    ):
        raise TransactionError(f"absent prior {label} receipt has backup data")


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
    required = {
        "schema", "source", "installed", "source_sha256", "source_mode",
        "source_identity", "prior", "no_swap_artifacts", "before",
        "neighbors", "verifier_authority",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise TransactionError("transaction manifest fields are invalid")
    if (
        manifest["schema"] != 2
        or manifest["source"] != str(config.source_unit)
        or manifest["installed"] != str(config.installed_unit)
        or manifest["source_mode"] != 0o644
        or not _valid_identity(manifest["source_identity"])
        or not _valid_digest(manifest["source_sha256"])
    ):
        raise TransactionError("transaction source authority is invalid")
    verifier_authority = manifest["verifier_authority"]
    if not isinstance(verifier_authority, dict) or set(verifier_authority) != {
        str(path) for path in config.verifier_authority
    }:
        raise TransactionError("transaction verifier authority paths are invalid")
    for receipt in verifier_authority.values():
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"identity", "mode", "sha256"}
            or not _valid_identity(receipt["identity"])
            or not _valid_mode(receipt["mode"])
            or not _valid_digest(receipt["sha256"])
        ):
            raise TransactionError("transaction verifier authority receipt is invalid")
    _validate_prior_receipt(
        manifest["prior"], config.transaction / "unit.before", "unit"
    )
    artifacts = manifest["no_swap_artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(NO_SWAP_KEYS):
        raise TransactionError("transaction no-swap artifact keys are invalid")
    for key in NO_SWAP_KEYS:
        receipt = artifacts[key]
        source_path, installed_path, source_mode = _no_swap_paths(config, key)
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {
                "source", "installed", "source_sha256", "source_mode",
                "source_identity", "prior",
            }
            or receipt["source"] != str(source_path)
            or receipt["installed"] != str(installed_path)
            or receipt["source_mode"] != source_mode
            or not _valid_digest(receipt["source_sha256"])
            or not _valid_identity(receipt["source_identity"])
        ):
            raise TransactionError(f"transaction no-swap {key} receipt is invalid")
        private_source = config.transaction / NO_SWAP_PRIVATE_FILES[key]
        _secure_regular(private_source, 0o600)
        if _sha256(read_nofollow(private_source, 1024 * 1024, owner_only=True)) != receipt["source_sha256"]:
            raise TransactionError(f"transaction no-swap {key} source copy is corrupt")
        _validate_prior_receipt(
            receipt["prior"], config.transaction / NO_SWAP_PRIOR_FILES[key], f"no-swap {key}"
        )
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


def _persist_transaction(
    config: RuntimeConfig,
    manifest: dict[str, Any],
    unit_source: SourceSnapshot,
    no_swap_sources: dict[str, SourceSnapshot],
    prior_unit: SourceSnapshot | None,
    prior_artifacts: dict[str, SourceSnapshot | None],
    baselines: dict[str, Any],
    systemd_before: dict[str, Any],
) -> None:
    temporary = config.state_root / f".{config.transaction.name}.tmp.{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise TransactionError("transaction temporary path already exists")
    os.mkdir(temporary, 0o700)
    try:
        if prior_unit is not None:
            _atomic_write(temporary / "unit.before", prior_unit.data, 0o600, replace=False)
        for key in NO_SWAP_KEYS:
            prior = prior_artifacts[key]
            if prior is not None:
                _atomic_write(
                    temporary / NO_SWAP_PRIOR_FILES[key], prior.data, 0o600, replace=False
                )
            _atomic_write(
                temporary / NO_SWAP_PRIVATE_FILES[key],
                no_swap_sources[key].data,
                0o600,
                replace=False,
            )
        _atomic_json(temporary / "baselines.json", baselines, replace=False)
        _atomic_json(temporary / "systemd.before.json", systemd_before, replace=False)
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


def _install_transaction_no_swap_bundle(
    config: RuntimeConfig,
    manifest: dict[str, Any],
    after_core: Callable[[], None] | None = None,
) -> None:
    data = _transaction_no_swap_helper_data(config, manifest)
    for key in NO_SWAP_KEYS:
        _source, installed, mode = _no_swap_paths(config, key)
        _atomic_write(installed, data[key], mode)
        receipt = manifest["no_swap_artifacts"][key]
        if _source_snapshot(installed, require_mode=mode).digest != receipt["source_sha256"]:
            raise TransactionError(f"rollback no-swap {key} installation is not authoritative")
        if key == "core" and after_core is not None:
            after_core()


def _verify_source_authority(config: RuntimeConfig, manifest: dict[str, Any]) -> SourceSnapshot:
    source = _source_snapshot(config.source_unit, require_mode=0o644)
    if source.digest != manifest["source_sha256"] or list(source.identity) != manifest["source_identity"]:
        raise TransactionError("canonical source unit drifted after transaction preparation")
    validate_unit_text(source.data.decode("utf-8"))
    return source

def _verify_no_swap_source_authority(
    config: RuntimeConfig, manifest: dict[str, Any]
) -> dict[str, SourceSnapshot]:
    sources = _no_swap_source_snapshots(config)
    for key in NO_SWAP_KEYS:
        receipt = manifest["no_swap_artifacts"][key]
        source = sources[key]
        if (
            source.digest != receipt["source_sha256"]
            or source.mode != receipt["source_mode"]
            or list(source.identity) != receipt["source_identity"]
        ):
            raise TransactionError(f"canonical no-swap {key} drifted after preparation")
    return sources


def _transaction_no_swap_helper_data(
    config: RuntimeConfig, manifest: dict[str, Any]
) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for key in NO_SWAP_KEYS:
        data = read_nofollow(
            config.transaction / NO_SWAP_PRIVATE_FILES[key],
            1024 * 1024,
            owner_only=True,
        )
        if _sha256(data) != manifest["no_swap_artifacts"][key]["source_sha256"]:
            raise TransactionError(f"transaction no-swap {key} source bytes drifted")
        result[key] = data
    return result


def _install_transaction_sources(
    config: RuntimeConfig,
    manifest: dict[str, Any],
    after_core: Callable[[], None] | None = None,
) -> None:
    sources = _verify_no_swap_source_authority(config, manifest)
    for key in NO_SWAP_KEYS:
        _source, installed, mode = _no_swap_paths(config, key)
        _atomic_write(installed, sources[key].data, mode)
        receipt = manifest["no_swap_artifacts"][key]
        snapshot = _source_snapshot(installed, require_mode=mode)
        if snapshot.digest != receipt["source_sha256"]:
            raise TransactionError(f"atomic no-swap {key} installation differs from source")
        _verify_no_swap_source_authority(config, manifest)
        if key == "core" and after_core is not None:
            after_core()
    _verify_installed_no_swap_helper(config)
    unit_source = _verify_source_authority(config, manifest)
    _atomic_write(config.installed_unit, unit_source.data, 0o644)
    installed_unit = _source_snapshot(config.installed_unit, require_mode=0o644)
    if installed_unit.data != unit_source.data or installed_unit.digest != unit_source.digest:
        raise TransactionError("atomic embedding unit installation differs from source")
    _verify_source_authority(config, manifest)
    _verify_no_swap_source_authority(config, manifest)
    _verify_installed_no_swap_helper(config)


def _verify_prior_unit_restored(config: RuntimeConfig, manifest: dict[str, Any]) -> None:
    prior = manifest["prior"]
    if prior["present"]:
        restored = _source_snapshot(config.installed_unit)
        if restored.digest != prior["sha256"] or restored.mode != prior["mode"]:
            raise TransactionError("rollback did not restore exact prior unit bytes and mode")
    elif config.installed_unit.exists() or config.installed_unit.is_symlink():
        raise TransactionError("rollback did not restore explicit prior unit absence")

def _restore_prior_unit(config: RuntimeConfig, manifest: dict[str, Any]) -> None:
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
    _verify_prior_unit_restored(config, manifest)

def _verify_prior_no_swap_helper_restored(
    config: RuntimeConfig, manifest: dict[str, Any]
) -> None:
    for key in NO_SWAP_KEYS:
        receipt = manifest["no_swap_artifacts"][key]["prior"]
        _source, installed, _mode = _no_swap_paths(config, key)
        if receipt["present"]:
            restored = _source_snapshot(installed)
            if restored.digest != receipt["sha256"] or restored.mode != receipt["mode"]:
                raise TransactionError(f"rollback did not restore exact prior no-swap {key}")
        elif installed.exists() or installed.is_symlink():
            raise TransactionError(f"rollback did not restore prior no-swap {key} absence")


def _restore_prior_no_swap_helper(
    config: RuntimeConfig,
    manifest: dict[str, Any],
    after_core: Callable[[], None] | None = None,
) -> None:
    for key in NO_SWAP_KEYS:
        receipt = manifest["no_swap_artifacts"][key]["prior"]
        _source, installed, _mode = _no_swap_paths(config, key)
        if receipt["present"]:
            backup = read_nofollow(
                config.transaction / NO_SWAP_PRIOR_FILES[key],
                1024 * 1024,
                owner_only=True,
            )
            if _sha256(backup) != receipt["sha256"]:
                raise TransactionError(f"prior no-swap {key} backup changed before rollback")
            _atomic_write(installed, backup, receipt["mode"])
        elif installed.exists() or installed.is_symlink():
            _secure_regular(installed)
            installed.unlink()
            _fsync_directory(installed.parent)
        if key == "core" and after_core is not None:
            after_core()
    _verify_prior_no_swap_helper_restored(config, manifest)


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
    removed_unit = False
    for candidate in config.unit_dir.glob(f".{UNIT}.tmp.*"):
        _secure_regular(candidate, 0o644)
        candidate.unlink()
        removed_unit = True
    if removed_unit:
        _fsync_directory(config.unit_dir)
    helper_parent = config.installed_no_swap_helper.parent
    removed_artifact = False
    for key in NO_SWAP_KEYS:
        _source, installed, mode = _no_swap_paths(config, key)
        for candidate in helper_parent.glob(f".{installed.name}.tmp.*"):
            _secure_regular(candidate, mode)
            candidate.unlink()
            removed_artifact = True
    if removed_artifact:
        _fsync_directory(helper_parent)


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
    helper_parent = config.installed_no_swap_helper.parent
    helper_parent_mode = stat.S_IMODE(helper_parent.lstat().st_mode)
    _secure_directory(helper_parent, helper_parent_mode)
    if helper_parent_mode & 0o022:
        raise TransactionError("no-swap helper directory is group/world writable")
    if (
        config.source_no_swap_core.name != "gb10_verify_vllm_no_swap_core.py"
        or config.source_no_swap_helper.name != "gb10_verify_vllm_no_swap.sh"
        or config.installed_no_swap_core.name != "gb10_verify_vllm_no_swap_core.py"
        or config.installed_no_swap_helper.name != "gb10_verify_vllm_no_swap.sh"
        or config.source_no_swap_core.parent != config.source_no_swap_helper.parent
        or config.installed_no_swap_core.parent != helper_parent
    ):
        raise TransactionError("no-swap core/wrapper layout is not canonical")


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
