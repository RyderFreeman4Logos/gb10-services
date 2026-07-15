#!/usr/bin/env python3
"""Fixed production and explicit direct-test configuration for embedding activation."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from gb10_embedding_activation_checks import RuntimeConfig, UNIT
from gb10_embedding_verifier_runtime import json_file


class ActivationConfigError(RuntimeError):
    pass


def production_config(engine_path: Path) -> RuntimeConfig:
    repository = engine_path.resolve().parents[1]
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    unit_dir = home / ".config/systemd/user"
    return RuntimeConfig(
        source_unit=repository / "systemd" / UNIT,
        installed_unit=unit_dir / UNIT,
        unit_dir=unit_dir,
        state_root=home / ".local/state/gb10-embedding-activation",
        systemctl="/usr/bin/systemctl",
        curl="/usr/bin/curl",
        verifier=str(repository / "scripts/gb10_verify_embedding_profile.py"),
        ready_seconds=92,
        command_seconds=95,
        deadline_seconds=300,
        rollback_seconds=180,
        verifier_authority=(
            repository / "scripts/gb10_verify_embedding_profile.py",
            repository / "scripts/gb10_embedding_profile_contract.py",
            repository / "scripts/gb10_embedding_verifier_runtime.py",
        ),
    )


def test_config(path: Path) -> RuntimeConfig:
    payload = json_file(path, owner_only=True)
    keys = {
        "source_unit",
        "installed_unit",
        "unit_dir",
        "state_root",
        "systemctl",
        "curl",
        "verifier",
        "ready_seconds",
        "command_seconds",
        "deadline_seconds",
        "rollback_seconds",
        "verifier_authority",
        "fail_at",
        "pause_at",
        "marker",
        "release",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ActivationConfigError("test-only activation config fields are invalid")
    for field in (
        "ready_seconds",
        "command_seconds",
        "deadline_seconds",
        "rollback_seconds",
    ):
        value = payload[field]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 30:
            raise ActivationConfigError(f"test-only bound is invalid: {field}")
    for field in ("fail_at", "pause_at"):
        if not isinstance(payload[field], str):
            raise ActivationConfigError(f"test-only hook is invalid: {field}")
    verifier_authority = payload["verifier_authority"]
    if (
        not isinstance(verifier_authority, list)
        or not verifier_authority
        or any(
            not isinstance(value, str) or not Path(value).is_absolute()
            for value in verifier_authority
        )
        or len(set(verifier_authority)) != len(verifier_authority)
    ):
        raise ActivationConfigError("test-only verifier authority is invalid")
    for field in (
        "source_unit",
        "installed_unit",
        "unit_dir",
        "state_root",
        "systemctl",
        "curl",
        "verifier",
        "marker",
        "release",
    ):
        if not isinstance(payload[field], str):
            raise ActivationConfigError(f"test-only string is invalid: {field}")
    return RuntimeConfig(
        source_unit=Path(payload["source_unit"]),
        installed_unit=Path(payload["installed_unit"]),
        unit_dir=Path(payload["unit_dir"]),
        state_root=Path(payload["state_root"]),
        systemctl=payload["systemctl"],
        curl=payload["curl"],
        verifier=payload["verifier"],
        ready_seconds=payload["ready_seconds"],
        command_seconds=payload["command_seconds"],
        deadline_seconds=payload["deadline_seconds"],
        rollback_seconds=payload["rollback_seconds"],
        verifier_authority=tuple(Path(value) for value in verifier_authority),
        test_only=True,
        fail_at=payload["fail_at"],
        pause_at=payload["pause_at"],
        marker=Path(payload["marker"]) if payload["marker"] else None,
        release=Path(payload["release"]) if payload["release"] else None,
    )
