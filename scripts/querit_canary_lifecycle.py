#!/usr/bin/env python3
"""Transactional activation and rollback for the temporary Querit canary."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import stat
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol

import querit_vllm_artifact
from reranker_equivalence_wire import (
    DEEPINFRA_MODEL_VERSION,
    ENDPOINT_PATH,
    canonical_payload,
    validate_response,
)


TEXT_UNIT = "vllm-aeon-27b-dflash.service"
BACKEND_UNIT = "vllm-querit-4b-canary-backend.service"
ADAPTER_UNIT = "vllm-querit-4b-canary.service"
EMBEDDING_UNIT = "vllm-embedding.service"
PRODUCTION_RERANKER_UNIT = "querit-4b-reranker.service"
LEGACY_RERANKER_UNIT = "vllm-qwen3-reranker-8b.service"
GUARD_UNIT = "llm-guard-proxy.service"
IMMUTABLE_NEIGHBORS = (
    EMBEDDING_UNIT,
    PRODUCTION_RERANKER_UNIT,
    LEGACY_RERANKER_UNIT,
    GUARD_UNIT,
)
MINIMUM_HEADROOM_GIB = 20
DEFAULT_STATE = Path("/home/obj/.local/state/gb10-querit-canary/state.json")
DEFAULT_MODEL = Path("/home/obj/models/querit-4b-vllm")
PUBLIC_URL = (
    "http://100.105.4.92:18014" + ENDPOINT_PATH + "?version=" + DEEPINFRA_MODEL_VERSION
)


class LifecycleError(RuntimeError):
    """The requested lifecycle transition failed closed."""


class LifecycleCancelled(LifecycleError):
    """A termination signal interrupted activation."""


@dataclass(frozen=True)
class ServiceState:
    active: bool
    invocation_id: str

    def record(self) -> dict[str, object]:
        return {"active": self.active, "invocation_id": self.invocation_id}


class Host(Protocol):
    def verify_artifact(self) -> str: ...

    def memory_available_gib(self) -> int: ...

    def service_state(self, unit: str) -> ServiceState: ...

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
    if not isinstance(record, dict) or record.get("schema") != "querit-canary-state-v1":
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
) -> dict[str, object]:
    return {
        "adapter_started": False,
        "artifact_manifest_sha256": artifact_sha256,
        "backend_started": False,
        "immutable_neighbors": neighbors,
        "phase": "activating",
        "schema": "querit-canary-state-v1",
        "text_before": text.record(),
        "text_paused": False,
    }


def _rollback_activation(
    host: Host,
    state_path: Path,
    record: dict[str, object],
    original_error: BaseException,
) -> None:
    rollback_errors: list[str] = []
    if record.get("adapter_started") and host.service_state(ADAPTER_UNIT).active:
        try:
            host.stop(ADAPTER_UNIT)
        except BaseException as exc:
            rollback_errors.append(f"adapter stop: {exc}")
    if record.get("backend_started") and host.service_state(BACKEND_UNIT).active:
        try:
            host.stop(BACKEND_UNIT)
        except BaseException as exc:
            rollback_errors.append(f"backend stop: {exc}")
    if record.get("text_paused") and not host.service_state(TEXT_UNIT).active:
        try:
            host.start(TEXT_UNIT)
        except BaseException as exc:
            rollback_errors.append(f"text restore: {exc}")
    try:
        _assert_neighbors(
            host,
            record["immutable_neighbors"],
            context="activation rollback",
        )
    except BaseException as exc:
        rollback_errors.append(str(exc))
    if not rollback_errors:
        _remove_state(state_path)
        return
    record["phase"] = "rollback-failed"
    record["rollback_errors"] = rollback_errors
    record["original_error"] = f"{type(original_error).__name__}: {original_error}"
    _write_state(state_path, record)
    raise LifecycleError(
        "canary activation failed and rollback was incomplete: "
        + "; ".join(rollback_errors)
    ) from original_error


def activate(host: Host, state_path: Path) -> None:
    if state_path.exists():
        raise LifecycleError(
            "an unfinished or active canary transaction already exists"
        )
    artifact_sha256 = host.verify_artifact()
    neighbors = _snapshot_neighbors(host)
    text_before = host.service_state(TEXT_UNIT)
    if (
        host.service_state(BACKEND_UNIT).active
        or host.service_state(ADAPTER_UNIT).active
    ):
        raise LifecycleError("canary services must be inactive before activation")
    record = _new_record(
        artifact_sha256=artifact_sha256,
        neighbors=neighbors,
        text=text_before,
    )
    _write_state(state_path, record)
    try:
        available = host.memory_available_gib()
        if available < MINIMUM_HEADROOM_GIB and text_before.active:
            record["text_paused"] = True
            _write_state(state_path, record)
            host.stop(TEXT_UNIT)
            available = host.memory_available_gib()
        if available < MINIMUM_HEADROOM_GIB:
            raise LifecycleError(
                f"insufficient MemAvailable: {available} GiB < "
                f"{MINIMUM_HEADROOM_GIB} GiB"
            )
        _assert_neighbors(host, neighbors, context="activation preflight")
        record["backend_started"] = True
        _write_state(state_path, record)
        host.start(BACKEND_UNIT)
        record["adapter_started"] = True
        _write_state(state_path, record)
        host.start(ADAPTER_UNIT)
        host.warm()
        _assert_neighbors(host, neighbors, context="activation")
        record["phase"] = "active"
        _write_state(state_path, record)
    except BaseException as exc:
        _rollback_activation(host, state_path, record, exc)
        raise


def _state_bool(record: Mapping[str, object], key: str) -> bool:
    value = record.get(key)
    if not isinstance(value, bool):
        raise LifecycleError(f"lifecycle state {key} is invalid")
    return value


def deactivate(host: Host, state_path: Path) -> None:
    record = _read_state(state_path)
    if record.get("phase") != "active":
        raise LifecycleError("only an active canary transaction can be deactivated")
    neighbors = record.get("immutable_neighbors")
    if not isinstance(neighbors, dict):
        raise LifecycleError("lifecycle neighbor snapshot is invalid")
    text_paused = _state_bool(record, "text_paused")
    backend_started = _state_bool(record, "backend_started")
    adapter_started = _state_bool(record, "adapter_started")
    _assert_neighbors(host, neighbors, context="deactivation preflight")
    record["phase"] = "deactivating"
    _write_state(state_path, record)
    try:
        if adapter_started:
            host.stop(ADAPTER_UNIT)
        if backend_started:
            host.stop(BACKEND_UNIT)
        if text_paused:
            host.start(TEXT_UNIT)
        _assert_neighbors(host, neighbors, context="deactivation")
        _remove_state(state_path)
    except BaseException as exc:
        rollback_errors: list[str] = []
        try:
            if text_paused and host.service_state(TEXT_UNIT).active:
                host.stop(TEXT_UNIT)
            if backend_started and not host.service_state(BACKEND_UNIT).active:
                host.start(BACKEND_UNIT)
            if adapter_started and not host.service_state(ADAPTER_UNIT).active:
                host.start(ADAPTER_UNIT)
            if backend_started or adapter_started:
                host.warm()
            _assert_neighbors(host, neighbors, context="deactivation rollback")
        except BaseException as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        record["phase"] = "active" if not rollback_errors else "rollback-failed"
        if rollback_errors:
            record["rollback_errors"] = rollback_errors
        _write_state(state_path, record)
        if rollback_errors:
            raise LifecycleError(
                "canary deactivation and rollback both failed: "
                + "; ".join(rollback_errors)
            ) from exc
        raise


class SystemHost:
    def __init__(self, model_root: Path = DEFAULT_MODEL) -> None:
        self.model_root = model_root

    def verify_artifact(self) -> str:
        return querit_vllm_artifact.manifest_sha256(self.model_root)

    def memory_available_gib(self) -> int:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) == 3 and parts[2] == "kB":
                        return int(parts[1]) // (1024 * 1024)
        except (OSError, UnicodeError, ValueError) as exc:
            raise LifecycleError("cannot read MemAvailable") from exc
        raise LifecycleError("MemAvailable is missing from /proc/meminfo")

    def service_state(self, unit: str) -> ServiceState:
        completed = self._systemctl(
            "show", unit, "--property=ActiveState", "--property=InvocationID"
        )
        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        if set(values) != {"ActiveState", "InvocationID"}:
            raise LifecycleError(f"systemd state fields are incomplete for {unit}")
        return ServiceState(
            values["ActiveState"] in {"active", "activating"},
            values["InvocationID"],
        )

    def start(self, unit: str) -> None:
        if unit not in {TEXT_UNIT, BACKEND_UNIT, ADAPTER_UNIT}:
            raise LifecycleError(f"refusing to start out-of-scope unit: {unit}")
        self._systemctl("start", unit, timeout=1900)

    def stop(self, unit: str) -> None:
        if unit not in {TEXT_UNIT, BACKEND_UNIT, ADAPTER_UNIT}:
            raise LifecycleError(f"refusing to stop out-of-scope unit: {unit}")
        self._systemctl("stop", unit, timeout=120)

    def warm(self) -> None:
        body = canonical_payload(["headroom probe"], ["headroom probe"])
        request = urllib.request.Request(
            PUBLIC_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                response_body = response.read(1024 * 1024 + 1)
                if response.status != 200 or len(response_body) > 1024 * 1024:
                    raise LifecycleError("canary warmup returned invalid HTTP evidence")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LifecycleError("canary warmup transport failed") from exc
        validate_response(response_body, 1)

    @staticmethod
    def _systemctl(
        *arguments: str, timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["/usr/bin/systemctl", "--user", *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LifecycleError("systemctl failed: " + " ".join(arguments)) from exc


def preflight(host: Host, state_path: Path) -> None:
    record = _read_state(state_path)
    if record.get("phase") != "activating":
        raise LifecycleError(
            "unit start is not authorized by an activation transaction"
        )
    expected_sha = record.get("artifact_manifest_sha256")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise LifecycleError("activation artifact identity is invalid")
    if host.verify_artifact() != expected_sha:
        raise LifecycleError("converted artifact changed after activation began")
    if (
        not host.service_state(BACKEND_UNIT).active
        and host.memory_available_gib() < MINIMUM_HEADROOM_GIB
    ):
        raise LifecycleError("canary unit preflight has less than 20 GiB headroom")
    neighbors = record.get("immutable_neighbors")
    if not isinstance(neighbors, dict):
        raise LifecycleError("activation neighbor snapshot is invalid")
    _assert_neighbors(host, neighbors, context="unit preflight")


def _lock(path: Path) -> int:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise LifecycleError(
            "another canary lifecycle operation holds the lock"
        ) from exc
    return descriptor


def _signal_handler(signum: int, _frame: FrameType | None) -> None:
    raise LifecycleCancelled(signal.Signals(signum).name)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("activate", "deactivate", "preflight"))
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODEL)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    state_path = args.state.expanduser().resolve(strict=False)
    host = SystemHost(args.model_root.expanduser().resolve(strict=True))
    if args.action == "preflight":
        preflight(host, state_path)
        return 0
    lock_descriptor = _lock(state_path.with_suffix(".lock"))
    previous_handlers: dict[signal.Signals, object] = {}
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[selected] = signal.signal(selected, _signal_handler)
    try:
        if args.action == "activate":
            activate(host, state_path)
        else:
            deactivate(host, state_path)
    finally:
        for selected, handler in previous_handlers.items():
            signal.signal(selected, handler)
        os.close(lock_descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
