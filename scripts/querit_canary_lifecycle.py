#!/usr/bin/env python3
"""Transactional activation and rollback for the temporary Querit canary."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

import querit_canary_runtime as runtime
import querit_canary_transaction as transaction
from querit_canary_runtime import (
    BACKEND_UNIT,
    DEFAULT_MODEL,
    LifecycleError,
    MINIMUM_HEADROOM_GIB,
    ServiceState,
    SystemHost,
    TEXT_UNIT,
)
from querit_canary_transaction import (
    Host,
    _assert_neighbors,
    _read_state,
    _state_bool,
    activate,
    deactivate,
)


DEFAULT_STATE = Path("/home/obj/.local/state/gb10-querit-canary/state.json")
ADAPTER_UNIT = runtime.ADAPTER_UNIT
IMMUTABLE_NEIGHBORS = runtime.IMMUTABLE_NEIGHBORS
EMBEDDING_UNIT = runtime.EMBEDDING_UNIT
PRODUCTION_RERANKER_UNIT = runtime.PRODUCTION_RERANKER_UNIT
LEGACY_RERANKER_UNIT = runtime.LEGACY_RERANKER_UNIT
GUARD_UNIT = runtime.GUARD_UNIT


class LifecycleCancelled(LifecycleError):
    """A termination signal interrupted activation."""




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
    backend_active = record.get("backend_active")
    if backend_active is None and host.memory_available_gib() < MINIMUM_HEADROOM_GIB:
        raise LifecycleError("canary unit preflight has less than 20 GiB headroom")
    neighbors = record.get("immutable_neighbors")
    if not isinstance(neighbors, dict):
        raise LifecycleError("activation neighbor snapshot is invalid")
    _assert_neighbors(host, neighbors, context="unit preflight")
    text_before = ServiceState.from_record(record.get("text_before"), "text")
    text_paused = _state_bool(record, "text_paused")
    text_observed = host.service_state(TEXT_UNIT)
    if text_paused:
        if (
            text_observed.active
            or text_observed.main_pid
            or text_observed.unit_pids
            or text_observed.container_id
            or text_observed.container_pid
            or text_observed.container_pids
        ):
            raise LifecycleError("paused text service retains active processes")
    elif text_observed != text_before:
        raise LifecycleError("text service identity changed during unit preflight")
    if backend_active is not None and host.service_state(BACKEND_UNIT) != (
        ServiceState.from_record(backend_active, "active backend")
    ):
        raise LifecycleError("backend identity changed before adapter start")


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
    if transaction.restoring_original():
        return
    raise LifecycleCancelled(signal.Signals(signum).name)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("activate", "deactivate", "preflight"))
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODEL)
    parser.add_argument(
        "--pause-text",
        action="store_true",
        help="transactionally pause an active text unit before canary activation",
    )
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
            activate(host, state_path, pause_text=args.pause_text)
        else:
            if args.pause_text:
                raise LifecycleError("--pause-text is valid only with activate")
            deactivate(host, state_path)
    finally:
        for selected, handler in previous_handlers.items():
            signal.signal(selected, handler)
        os.close(lock_descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
