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
    "gb10_embedding_activation_checks.py": "88a06823da2b976df3eb45cfc4bccb6f2067ff35625752d90b517519cb3bc91d",
    "gb10_embedding_activation_config.py": "1a3b8df549d5ff6aeb061067827d0befc1dada3431760920c428096bbe9fb4eb",
    "gb10_embedding_activation_storage.py": "27e20e181fb244a599802f42c5c60724f9c4bf80d6e2aaaca8648924aa2f9bbe",
    "gb10_embedding_profile_contract.py": "cbb89060e5f8d5a2391023811b1223200cdc57976d2349374705e8a0e123bce7",
    "gb10_embedding_verifier_runtime.py": "599af1c802e1a0d3e942fb0b16cdfd3a66f9e928ab64eabf3fb455ec007df629",
    "gb10_verify_embedding_profile.py": "ec2b81b1653130615f77e463845adced13ad4e9adc75f599e58eadd7297f44f3",
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
    require_docker_cgroup_v2 as _require_docker_cgroup_v2,
    run_systemctl as _run_systemctl,
    wait_new_generation as _wait_new_generation,
)
from gb10_embedding_activation_config import (  # noqa: E402
    production_config as _production_config,
    test_config as _test_config,
)
from gb10_embedding_activation_storage import (  # noqa: E402
    ActivationStorageError,
    NO_SWAP_KEYS,
    SourceSnapshot,
    TransactionError,
    atomic_json as _atomic_json,
    atomic_write as _atomic_write,
    fsync_directory as _fsync_directory,
    secure_regular as _secure_regular,
    _acquire_lock,
    _cleanup_temporary_paths,
    _cleanup_transaction,
    _create_state_root,
    _install_transaction_no_swap_bundle,
    _install_transaction_sources,
    _load_manifest,
    _manifest_generation,
    _no_swap_source_snapshot,
    _no_swap_source_snapshots,
    _persist_transaction,
    _read_phase,
    _restore_prior_no_swap_helper,
    _restore_prior_unit,
    _set_phase,
    _sha256,
    _source_snapshot,
    _verifier_authority_snapshot,
    _verify_installed_no_swap_helper,
    _verify_no_swap_source_authority,
    _verify_prior_no_swap_helper_restored,
    _verify_prior_unit_restored,
    _verify_source_authority,
    _verify_verifier_authority,
    _write_public_receipt,
)
from gb10_embedding_profile_contract import (  # noqa: E402
    EXPECTED_PROFILE,
    validate_unit_text,
)
from gb10_embedding_verifier_runtime import (  # noqa: E402
    command,
    json_file,
    json_text,
    read_nofollow,
)
from gb10_verify_embedding_profile import verify_production  # noqa: E402

class ActivationInterrupted(RuntimeError):
    """Signal converted to a recoverable pre-commit failure."""


















def _verify_live_no_swap(
    config: RuntimeConfig,
    deadline: float,
    unit_path: Path,
    *,
    helper_path: Path,
    expected_artifact_sha256: dict[str, str] | None = None,
) -> None:
    """Attest one unit and its exact live Docker generation with an owned bundle."""

    if helper_path.name != "gb10_verify_vllm_no_swap.sh":
        raise TransactionError("selected no-swap wrapper path is not canonical")
    selected = {
        "core": _source_snapshot(
            helper_path.with_name("gb10_verify_vllm_no_swap_core.py"),
            require_mode=0o644,
        ),
        "wrapper": _source_snapshot(helper_path, require_mode=0o755),
    }
    if expected_artifact_sha256 is None:
        sources = _no_swap_source_snapshots(config)
        for key in NO_SWAP_KEYS:
            if selected[key].data != sources[key].data or selected[key].digest != sources[key].digest:
                raise TransactionError(f"selected no-swap {key} differs from source authority")
    else:
        if set(expected_artifact_sha256) != set(NO_SWAP_KEYS):
            raise TransactionError("selected no-swap artifact authority keys are invalid")
        for key in NO_SWAP_KEYS:
            if selected[key].digest != expected_artifact_sha256[key]:
                raise TransactionError(f"selected no-swap {key} differs from transaction authority")
    if config.test_only:
        arguments = [str(helper_path), "--test-only"]
    else:
        arguments = [
            "/usr/bin/env",
            "-i",
            "HOME=/home/obj",
            "PATH=/usr/bin:/bin",
            "LC_ALL=C",
            "DOCKER_HOST=unix:///run/user/1001/docker.sock",
            "/usr/bin/bash",
            "--noprofile",
            "--norc",
            str(helper_path),
        ]
    arguments.extend(
        [
            "--unit",
            str(unit_path),
            "--container",
            "vllm-embedding",
        ]
    )
    command(arguments, timeout=config.command_seconds, deadline=deadline)



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
































def _prepare_transaction(config: RuntimeConfig, deadline: float) -> dict[str, Any]:
    source = _source_snapshot(config.source_unit, require_mode=0o644)
    no_swap_sources = _no_swap_source_snapshots(config)
    verifier_authority = _verifier_authority_snapshot(config)
    try:
        validate_unit_text(source.data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise TransactionError("canonical source unit is not UTF-8") from error
    prior_snapshot = (
        _source_snapshot(config.installed_unit)
        if config.installed_unit.exists() or config.installed_unit.is_symlink()
        else None
    )
    prior_artifacts: dict[str, SourceSnapshot | None] = {
        "core": (
            _source_snapshot(config.installed_no_swap_core)
            if config.installed_no_swap_core.exists() or config.installed_no_swap_core.is_symlink()
            else None
        ),
        "wrapper": (
            _source_snapshot(config.installed_no_swap_helper)
            if config.installed_no_swap_helper.exists() or config.installed_no_swap_helper.is_symlink()
            else None
        ),
    }
    first_generation = _query_generation(config, deadline)
    if not first_generation.running:
        raise TransactionError("embedding service must be active/running before activation")
    _verify_live_no_swap(
        config,
        deadline,
        config.installed_unit if prior_snapshot is not None else config.source_unit,
        helper_path=config.source_no_swap_helper,
    )
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
    if _no_swap_source_snapshots(config) != no_swap_sources:
        raise TransactionError("canonical no-swap bundle mutated during pre-state capture")
    if _verifier_authority_snapshot(config) != verifier_authority:
        raise TransactionError("strict verifier authority mutated during pre-state capture")
    if prior_snapshot is not None:
        if _source_snapshot(config.installed_unit) != prior_snapshot:
            raise TransactionError("installed unit mutated during pre-state capture")
    elif config.installed_unit.exists() or config.installed_unit.is_symlink():
        raise TransactionError("installed unit appeared during pre-state capture")
    installed_paths = {
        "core": config.installed_no_swap_core,
        "wrapper": config.installed_no_swap_helper,
    }
    for key in NO_SWAP_KEYS:
        prior = prior_artifacts[key]
        path = installed_paths[key]
        if prior is not None:
            if _source_snapshot(path) != prior:
                raise TransactionError(f"installed no-swap {key} mutated during pre-state capture")
        elif path.exists() or path.is_symlink():
            raise TransactionError(f"installed no-swap {key} appeared during pre-state capture")
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
    artifact_paths = {
        "core": (config.source_no_swap_core, config.installed_no_swap_core),
        "wrapper": (config.source_no_swap_helper, config.installed_no_swap_helper),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for key in NO_SWAP_KEYS:
        previous = prior_artifacts[key]
        source_path, installed_path = artifact_paths[key]
        snapshot = no_swap_sources[key]
        artifacts[key] = {
            "source": str(source_path),
            "installed": str(installed_path),
            "source_sha256": snapshot.digest,
            "source_mode": snapshot.mode,
            "source_identity": list(snapshot.identity),
            "prior": {
                "present": previous is not None,
                "mode": previous.mode if previous is not None else None,
                "sha256": previous.digest if previous is not None else None,
            },
        }
    manifest = {
        "schema": 2,
        "source": str(config.source_unit),
        "installed": str(config.installed_unit),
        "source_sha256": source.digest,
        "source_mode": source.mode,
        "source_identity": list(source.identity),
        "prior": prior,
        "no_swap_artifacts": artifacts,
        "before": before,
        "neighbors": first_neighbors,
        "verifier_authority": verifier_authority,
    }
    _persist_transaction(
        config,
        manifest,
        source,
        no_swap_sources,
        prior_snapshot,
        prior_artifacts,
        baselines,
        {
            "InvocationID": first_generation.invocation,
            "MainPID": first_generation.pid,
            "ExecMainStartTimestampMonotonic": first_generation.started,
        },
    )
    return manifest


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
        _restore_prior_unit(config, manifest)
        _install_transaction_no_swap_bundle(config, manifest)
        _run_systemctl(config, deadline, "daemon-reload")
        prior_generation = _manifest_generation(manifest["before"])
        if not prior_generation.running:
            raise TransactionError("transaction prior generation was not active/running")
        _run_systemctl(config, deadline, "restart", UNIT)
        restored_generation = _wait_new_generation(
            config, failed_generation, deadline
        )
        try:
            _verify_live_no_swap(
                config,
                deadline,
                (
                    config.installed_unit
                    if manifest["prior"]["present"]
                    else config.source_unit
                ),
                helper_path=config.installed_no_swap_helper,
                expected_artifact_sha256={
                    key: manifest["no_swap_artifacts"][key]["source_sha256"]
                    for key in NO_SWAP_KEYS
                },
            )
        except BaseException:
            _run_systemctl(config, deadline, "stop", UNIT)
            raise
        if _neighbors(config, deadline) != manifest["neighbors"]:
            raise TransactionError("neighbor generation changed during rollback")
        final_generation = _query_generation(config, deadline)
        if final_generation.stable != restored_generation.stable:
            raise TransactionError("restored embedding generation changed before rollback commit")
        _verify_prior_unit_restored(config, manifest)
        _restore_prior_no_swap_helper(
            config,
            manifest,
            lambda: _test_boundary(config, "after_prior_core_restore", deadline),
        )
        _verify_prior_no_swap_helper_restored(config, manifest)
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
                "no_swap_verified": True,
                "restored_no_swap_core_mode": manifest["no_swap_artifacts"]["core"]["prior"]["mode"],
                "restored_no_swap_core_presence": manifest["no_swap_artifacts"]["core"]["prior"]["present"],
                "restored_no_swap_helper_mode": manifest["no_swap_artifacts"]["wrapper"]["prior"]["mode"],
                "restored_no_swap_helper_presence": manifest["no_swap_artifacts"]["wrapper"]["prior"]["present"],
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








def activate(config: RuntimeConfig) -> int:
    deadline = time.monotonic() + config.deadline_seconds
    _require_docker_cgroup_v2(config, deadline)
    _create_state_root(config)
    try:
        lock_descriptor = _acquire_lock(config)
    except BlockingIOError:
        print("another embedding activation transaction holds the lock", file=sys.stderr)
        return 1
    previous_handlers: dict[int, Any] = {}
    committed_this_attempt = False

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
        _install_transaction_sources(
            config,
            manifest,
            lambda: _test_boundary(config, "after_core_install", deadline),
        )
        _test_boundary(config, "after_install", deadline)
        _set_phase(config, "installed")
        _run_systemctl(config, deadline, "daemon-reload")
        _set_phase(config, "reloaded")
        _verify_installed_no_swap_helper(config)
        before = _manifest_generation(manifest["before"])
        _run_systemctl(config, deadline, "restart", UNIT)
        current = _wait_new_generation(config, before, deadline)
        _verify_live_no_swap(
            config,
            deadline,
            config.installed_unit,
            helper_path=config.installed_no_swap_helper,
        )
        _set_phase(config, "restarted")
        _test_boundary(config, "after_restart", deadline)
        _verify_verifier_authority(config, manifest)
        _verify_installed_no_swap_helper(config)
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
            or verification["profile"] != EXPECTED_PROFILE
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
        _verify_no_swap_source_authority(config, manifest)
        _verify_installed_no_swap_helper(config)
        installed = _source_snapshot(config.installed_unit, require_mode=0o644)
        if installed.digest != manifest["source_sha256"]:
            raise TransactionError("installed embedding unit drifted before commit")
        _set_phase(config, "verified")
        _test_boundary(config, "before_receipt", deadline)
        receipt = {
            "commit_requires_phase": "committed",
            "neighbor_generations_unchanged": True,
            "no_swap_verified": True,
            "no_swap_helper_sha256": manifest["no_swap_artifacts"]["wrapper"]["source_sha256"],
            "profile": EXPECTED_PROFILE,
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
        committed_this_attempt = True
        _test_boundary(config, "after_commit", deadline)
        print(
            f"embedding activation committed; private evidence: {config.transaction}"
        )
        return 0
    except BaseException as error:
        print(f"embedding activation failed: {error}", file=sys.stderr)
        try:
            if (
                committed_this_attempt
                and config.transaction.exists()
                and _read_phase(config) == "committed"
            ):
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
