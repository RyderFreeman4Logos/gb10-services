#!/usr/bin/env python3
"""Durable state transitions for the temporary Querit canary."""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Protocol

from querit_canary_runtime import (
    ADAPTER_UNIT,
    BACKEND_UNIT,
    IMMUTABLE_NEIGHBORS,
    LifecycleError,
    MINIMUM_HEADROOM_GIB,
    ServiceState,
    TEXT_UNIT,
)


class Host(Protocol):
    def require_cgroup_v2(self) -> None: ...

    def verify_no_swap(self, unit: str, container: str | None = None) -> None: ...

    def verify_artifact(self) -> str: ...

    def memory_available_gib(self) -> int: ...

    def service_state(self, unit: str) -> ServiceState: ...

    def unit_file_state(self, unit: str) -> str: ...

    def start(self, unit: str) -> None: ...

    def stop(self, unit: str) -> None: ...

    def warm(self) -> None: ...


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_state(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise LifecycleError("lifecycle state parent must be a real directory")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    payload = _json_bytes(record)
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
    _fsync_directory(path.parent)


def _read_state(path: Path) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LifecycleError("lifecycle state is missing or unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > 256 * 1024
        ):
            raise LifecycleError("lifecycle state is not a bounded regular file")
        raw = os.read(descriptor, 256 * 1024 + 1)
    finally:
        os.close(descriptor)
    try:
        record = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("lifecycle state is malformed") from exc
    if not isinstance(record, dict) or record.get("schema") != "querit-canary-state-v2":
        raise LifecycleError("lifecycle state schema is invalid")
    return record


def _snapshot_neighbors(host: Host) -> dict[str, dict[str, object]]:
    return {unit: host.service_state(unit).record() for unit in IMMUTABLE_NEIGHBORS}


def _assert_neighbors(
    host: Host, expected: Mapping[str, object], *, context: str
) -> None:
    observed = _snapshot_neighbors(host)
    if observed != expected:
        raise LifecycleError(f"protected service state changed during {context}")


def _remove_state(path: Path) -> None:
    path.unlink(missing_ok=True)
    if path.parent.exists():
        _fsync_directory(path.parent)


def _new_record(
    *,
    artifact_sha256: str,
    neighbors: Mapping[str, object],
    text: ServiceState,
    backend: ServiceState,
    adapter: ServiceState,
) -> dict[str, object]:
    return {
        "adapter_before": adapter.record(),
        "artifact_manifest_sha256": artifact_sha256,
        "backend_before": backend.record(),
        "immutable_neighbors": neighbors,
        "phase": "activating",
        "schema": "querit-canary-state-v2",
        "text_before": text.record(),
        "text_paused": False,
        "text_pause_requested": False,
    }


_RESTORING_ORIGINAL = False


def _record_neighbors(record: Mapping[str, object]) -> dict[str, object]:
    neighbors = record.get("immutable_neighbors")
    if not isinstance(neighbors, dict):
        raise LifecycleError("lifecycle neighbor snapshot is invalid")
    return neighbors


def _retry_restore(
    label: str, action: Callable[[], None], failures: list[BaseException]
) -> str | None:
    last_error: BaseException | None = None
    for _attempt in range(2):
        try:
            action()
            return None
        except BaseException as exc:
            if not failures:
                failures.append(exc)
            last_error = exc
    return f"{label}: {type(last_error).__name__}: {last_error}"


def _quiescent(state: ServiceState) -> bool:
    return (
        not state.active
        and state.main_pid == 0
        and not state.unit_pids
        and not state.container_id
        and state.container_pid == 0
        and not state.container_cgroup
        and not state.container_pids
    )


def _require_unmasked_text(host: Host) -> None:
    state = host.unit_file_state(TEXT_UNIT)
    if state in {"masked", "masked-runtime"}:
        raise LifecycleError("text service is masked and cannot be transactionally restored")


def _restore_original(
    host: Host, state_path: Path, record: dict[str, object]
) -> BaseException | None:
    global _RESTORING_ORIGINAL

    host.require_cgroup_v2()
    text_before = ServiceState.from_record(record.get("text_before"), "text")
    backend_before = ServiceState.from_record(
        record.get("backend_before"), "backend"
    )
    adapter_before = ServiceState.from_record(
        record.get("adapter_before"), "adapter"
    )
    if backend_before.active or adapter_before.active:
        raise LifecycleError("canary pre-activation snapshot must be inactive")
    neighbors = _record_neighbors(record)
    text_paused = _state_bool(record, "text_paused")
    failures: list[BaseException] = []
    unresolved: list[str] = []
    record["phase"] = "restoring-original"
    _RESTORING_ORIGINAL = True
    try:
        _write_state(state_path, record)

        def stop_to_snapshot(unit: str, expected: ServiceState) -> None:
            if expected.active:
                raise LifecycleError(f"{unit} was unexpectedly active before activation")
            if not _quiescent(host.service_state(unit)):
                host.stop(unit)
            stopped = host.service_state(unit)
            if not _quiescent(stopped):
                raise LifecycleError(f"{unit} retains unit/container/PID residue")
            record[f"{unit}_restored"] = stopped.record()

        error = _retry_restore(
            "adapter restore",
            lambda: stop_to_snapshot(ADAPTER_UNIT, adapter_before),
            failures,
        )
        if error:
            unresolved.append(error)
        error = _retry_restore(
            "backend restore",
            lambda: stop_to_snapshot(BACKEND_UNIT, backend_before),
            failures,
        )
        if error:
            unresolved.append(error)

        def restore_text() -> None:
            observed = host.service_state(TEXT_UNIT)
            if text_paused:
                if not text_before.active:
                    raise LifecycleError("inactive text service cannot be pause-owned")
                if not observed.active:
                    host.start(TEXT_UNIT)
                restored = host.service_state(TEXT_UNIT)
                if not restored.active:
                    raise LifecycleError("text service did not return to active state")
                record["text_restored"] = restored.record()
            else:
                if observed != text_before:
                    raise LifecycleError("untouched text service identity changed")
                record["text_restored"] = observed.record()

        error = _retry_restore("text restore", restore_text, failures)
        if error:
            unresolved.append(error)
        try:
            _assert_neighbors(host, neighbors, context="original-state restoration")
        except BaseException as exc:
            unresolved.append(f"protected neighbors: {type(exc).__name__}: {exc}")
        error = _retry_restore(
            "rollback no-swap verification",
            lambda: host.verify_no_swap(BACKEND_UNIT),
            failures,
        )
        if error:
            unresolved.append(error)
        if unresolved:
            record["phase"] = "rollback-failed"
            record["rollback_errors"] = unresolved
            _write_state(state_path, record)
            raise LifecycleError(
                "canary transaction could not restore its original state: "
                + "; ".join(unresolved)
            )
        _remove_state(state_path)
        return failures[0] if failures else None
    finally:
        _RESTORING_ORIGINAL = False


def _recover_stale_transaction(host: Host, state_path: Path) -> None:
    record = _read_state(state_path)
    if record.get("phase") == "active":
        raise LifecycleError("an active canary transaction already exists")
    restore_error = _restore_original(host, state_path, record)
    if restore_error is not None:
        raise restore_error


def activate(host: Host, state_path: Path, *, pause_text: bool = False) -> None:
    host.require_cgroup_v2()
    if state_path.exists():
        _recover_stale_transaction(host, state_path)
    host.verify_no_swap(BACKEND_UNIT)
    artifact_sha256 = host.verify_artifact()
    neighbors = _snapshot_neighbors(host)
    text_before = host.service_state(TEXT_UNIT)
    backend_before = host.service_state(BACKEND_UNIT)
    adapter_before = host.service_state(ADAPTER_UNIT)
    if backend_before.active or adapter_before.active or backend_before.container_id:
        raise LifecycleError(
            "canary services and their containers must be absent before activation"
        )
    record = _new_record(
        artifact_sha256=artifact_sha256,
        neighbors=neighbors,
        text=text_before,
        backend=backend_before,
        adapter=adapter_before,
    )
    try:
        if pause_text:
            _require_unmasked_text(host)
            record["text_pause_requested"] = True
        _write_state(state_path, record)
        available = host.memory_available_gib()
        should_pause_text = text_before.active and (
            pause_text or available < MINIMUM_HEADROOM_GIB
        )
        if should_pause_text:
            if not pause_text:
                _require_unmasked_text(host)
            record["text_paused"] = True
            _write_state(state_path, record)
            host.stop(TEXT_UNIT)
            if not _quiescent(host.service_state(TEXT_UNIT)):
                raise LifecycleError("text service did not fully quiesce for canary")
            available = host.memory_available_gib()
        if available < MINIMUM_HEADROOM_GIB:
            raise LifecycleError(
                f"insufficient MemAvailable: {available} GiB < "
                f"{MINIMUM_HEADROOM_GIB} GiB"
            )
        _assert_neighbors(host, neighbors, context="activation preflight")
        host.start(BACKEND_UNIT)
        backend_active = host.service_state(BACKEND_UNIT)
        if not backend_active.active or backend_active == backend_before:
            raise LifecycleError("canary backend did not enter a new active identity")
        host.verify_no_swap(BACKEND_UNIT, "vllm-querit-4b-canary")
        record["backend_active"] = backend_active.record()
        record["no_swap_verified"] = True
        _write_state(state_path, record)
        host.start(ADAPTER_UNIT)
        adapter_active = host.service_state(ADAPTER_UNIT)
        if not adapter_active.active or adapter_active == adapter_before:
            raise LifecycleError("canary adapter did not enter a new active identity")
        record["adapter_active"] = adapter_active.record()
        _write_state(state_path, record)
        host.warm()
        if host.service_state(BACKEND_UNIT) != backend_active:
            raise LifecycleError("canary backend identity changed during readiness")
        if host.service_state(ADAPTER_UNIT) != adapter_active:
            raise LifecycleError("canary adapter identity changed during readiness")
        _assert_neighbors(host, neighbors, context="activation")
        record["phase"] = "active"
        _write_state(state_path, record)
    except BaseException as exc:
        try:
            _restore_original(host, state_path, record)
        except BaseException as restore_exc:
            raise LifecycleError(
                "canary activation failed and original-state restoration was incomplete; "
                f"original failure was {type(exc).__name__}: {exc}"
            ) from restore_exc
        raise


def _state_bool(record: Mapping[str, object], key: str) -> bool:
    value = record.get(key)
    if not isinstance(value, bool):
        raise LifecycleError(f"lifecycle state {key} is invalid")
    return value


def deactivate(host: Host, state_path: Path) -> None:
    host.require_cgroup_v2()
    record = _read_state(state_path)
    phase = record.get("phase")
    preflight_error: BaseException | None = None
    if phase == "active":
        try:
            expected_sha = record.get("artifact_manifest_sha256")
            if host.verify_artifact() != expected_sha:
                raise LifecycleError("converted artifact changed while canary was active")
            _assert_neighbors(
                host, _record_neighbors(record), context="deactivation preflight"
            )
            expected_backend = ServiceState.from_record(
                record.get("backend_active"), "active backend"
            )
            expected_adapter = ServiceState.from_record(
                record.get("adapter_active"), "active adapter"
            )
            if host.service_state(BACKEND_UNIT) != expected_backend:
                raise LifecycleError("canary backend identity changed before deactivation")
            if host.service_state(ADAPTER_UNIT) != expected_adapter:
                raise LifecycleError("canary adapter identity changed before deactivation")
        except BaseException as exc:
            preflight_error = exc
        record["phase"] = "deactivating"
        try:
            _write_state(state_path, record)
        except BaseException:
            restore_error = _restore_original(host, state_path, record)
            if restore_error is not None:
                raise restore_error
            raise
    elif phase not in {
        "activating",
        "deactivating",
        "restoring-original",
        "rollback-failed",
    }:
        raise LifecycleError("lifecycle transaction phase is invalid")

    restore_error = _restore_original(host, state_path, record)
    if preflight_error is not None:
        raise preflight_error
    if restore_error is not None:
        raise restore_error


def restoring_original() -> bool:
    return _RESTORING_ORIGINAL
