#!/usr/bin/env python3
"""Strict systemd, neighbor, and API checks for embedding activation."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gb10_embedding_profile_contract import EXPECTED_MODELS
from gb10_embedding_verifier_runtime import command, json_text, remaining
from gb10_verify_embedding_profile import CANARY_INPUTS, vectors

__all__ = [
    "ActivationCheckError",
    "GENERATION_FIELDS",
    "Generation",
    "NEIGHBORS",
    "NEIGHBOR_FIELDS",
    "RuntimeConfig",
    "UNIT",
    "capture_baselines",
    "generation_is_new",
    "neighbors",
    "query_generation",
    "require_docker_cgroup_v2",
    "run_systemctl",
    "verify_models",
    "wait_new_generation",
]

UNIT = "vllm-embedding.service"
NEIGHBORS = (
    "vllm-aeon-27b-dflash.service",
    "vllm-querit-4b-reranker.service",
    "vllm-qwen3-reranker-8b.service",
)
GENERATION_FIELDS = (
    "LoadState",
    "ActiveState",
    "SubState",
    "FragmentPath",
    "MainPID",
    "ControlGroup",
    "InvocationID",
    "ExecMainStartTimestampMonotonic",
)
NEIGHBOR_FIELDS = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "NRestarts",
    "InvocationID",
    "ExecMainStartTimestampMonotonic",
)


class ActivationCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    source_unit: Path
    installed_unit: Path
    source_no_swap_core: Path
    installed_no_swap_core: Path
    source_no_swap_helper: Path
    installed_no_swap_helper: Path
    unit_dir: Path
    state_root: Path
    systemctl: str
    docker: str
    curl: str
    verifier: str
    ready_seconds: int
    command_seconds: int
    deadline_seconds: int
    rollback_seconds: int
    verifier_authority: tuple[Path, ...]
    test_only: bool = False
    fail_at: str = ""
    pause_at: str = ""
    marker: Path | None = None
    release: Path | None = None

    @property
    def transaction(self) -> Path:
        return self.state_root / "transaction.v1"


@dataclass(frozen=True)
class Generation:
    load: str
    active: str
    sub: str
    fragment: str
    pid: int
    cgroup: str
    invocation: str
    started: int

    @property
    def stable(self) -> tuple[str, ...]:
        return (
            self.load,
            self.active,
            self.sub,
            self.fragment,
            str(self.pid),
            self.cgroup,
            self.invocation,
            str(self.started),
        )

    @property
    def running(self) -> bool:
        return self.load == "loaded" and self.active == "active" and self.sub == "running"


def require_docker_cgroup_v2(config: RuntimeConfig, deadline: float) -> None:
    """Reject every mutation path unless rootless Docker reports cgroup v2."""

    output = command(
        [config.docker, "info", "--format", "{{.CgroupVersion}}"],
        timeout=config.command_seconds,
        deadline=deadline,
    )
    if output not in {"2", "2\n"}:
        raise ActivationCheckError(
            "Docker must report cgroup version exactly 2 before activation"
        )


def _parse_exact_fields(text: str, expected: tuple[str, ...]) -> dict[str, str]:
    if len(text.encode()) > 128 * 1024 or "\x00" in text:
        raise ActivationCheckError("systemd output exceeded strict bound")
    result: dict[str, str] = {}
    for row in text.splitlines():
        key, separator, value = row.partition("=")
        if separator != "=" or key not in expected or key in result:
            raise ActivationCheckError(
                "systemd output has malformed, duplicate, or extra field"
            )
        result[key] = value
    if set(result) != set(expected):
        raise ActivationCheckError("systemd output is missing a required field")
    return result


def run_systemctl(config: RuntimeConfig, deadline: float, *arguments: str) -> str:
    return command(
        [config.systemctl, "--user", *arguments],
        timeout=config.command_seconds,
        deadline=deadline,
    )


def query_generation(config: RuntimeConfig, deadline: float) -> Generation:
    arguments = ["show", UNIT, "--no-pager"]
    arguments.extend(f"--property={field}" for field in GENERATION_FIELDS)
    fields = _parse_exact_fields(
        run_systemctl(config, deadline, *arguments), GENERATION_FIELDS
    )
    for field in ("MainPID", "ExecMainStartTimestampMonotonic"):
        if re.fullmatch(r"[0-9]+", fields[field]) is None:
            raise ActivationCheckError(f"invalid systemd generation field: {field}")
    pid = int(fields["MainPID"])
    started = int(fields["ExecMainStartTimestampMonotonic"])
    invocation = fields["InvocationID"]
    running = (fields["ActiveState"], fields["SubState"]) == ("active", "running")
    if fields["LoadState"] not in {"loaded", "not-found"}:
        raise ActivationCheckError("unexpected embedding unit LoadState")
    if running:
        if (
            fields["LoadState"] != "loaded"
            or pid <= 0
            or started <= 0
            or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
            or not fields["ControlGroup"].startswith("/")
        ):
            raise ActivationCheckError("running embedding generation is malformed")
    elif (
        pid != 0
        or invocation and re.fullmatch(r"[0-9a-f]{32}", invocation) is None
        or fields["ActiveState"]
        not in {"inactive", "failed", "activating", "deactivating"}
    ):
        raise ActivationCheckError("non-running embedding generation is malformed")
    fragment = fields["FragmentPath"]
    if fragment and (not fragment.startswith("/") or ".." in Path(fragment).parts):
        raise ActivationCheckError("embedding FragmentPath is malformed")
    return Generation(
        load=fields["LoadState"],
        active=fields["ActiveState"],
        sub=fields["SubState"],
        fragment=fragment,
        pid=pid,
        cgroup=fields["ControlGroup"],
        invocation=invocation,
        started=started,
    )


def generation_is_new(before: Generation, after: Generation) -> bool:
    return (
        after.running
        and after.invocation != before.invocation
        and after.pid != before.pid
        and after.started > before.started
    )


def _neighbor_state(
    config: RuntimeConfig, unit: str, deadline: float
) -> dict[str, str]:
    arguments = ["show", unit, "--no-pager"]
    arguments.extend(f"--property={field}" for field in NEIGHBOR_FIELDS)
    fields = _parse_exact_fields(
        run_systemctl(config, deadline, *arguments), NEIGHBOR_FIELDS
    )
    for field in ("MainPID", "NRestarts", "ExecMainStartTimestampMonotonic"):
        if re.fullmatch(r"[0-9]+", fields[field]) is None:
            raise ActivationCheckError(f"invalid neighbor state field: {field}")
    invocation = fields["InvocationID"]
    if invocation and re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
        raise ActivationCheckError("invalid neighbor InvocationID")
    return fields


def neighbors(config: RuntimeConfig, deadline: float) -> dict[str, dict[str, str]]:
    return {unit: _neighbor_state(config, unit, deadline) for unit in NEIGHBORS}


def _curl_json(
    config: RuntimeConfig,
    deadline: float,
    endpoint: str,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arguments = [
        config.curl,
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--max-time",
        str(max(1, min(config.command_seconds, int(remaining(deadline))))),
    ]
    input_text: str | None = None
    if request is not None:
        arguments.extend(
            ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        )
        input_text = json.dumps(request, sort_keys=True, allow_nan=False)
    arguments.append(f"http://100.105.4.92:18012{endpoint}")
    payload = json_text(
        command(
            arguments,
            timeout=config.command_seconds,
            input_text=input_text,
            deadline=deadline,
        )
    )
    if not isinstance(payload, dict):
        raise ActivationCheckError("embedding API response is not a JSON object")
    return payload


def verify_models(config: RuntimeConfig, deadline: float) -> None:
    payload = _curl_json(config, deadline, "/v1/models")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ActivationCheckError("embedding model listing is malformed")
    identifiers = [row.get("id") if isinstance(row, dict) else None for row in data]
    if identifiers != list(EXPECTED_MODELS) or len(set(identifiers)) != len(identifiers):
        raise ActivationCheckError("embedding model aliases are not exact")


def capture_baselines(config: RuntimeConfig, deadline: float) -> dict[str, Any]:
    verify_models(config, deadline)
    aliases: dict[str, dict[str, Any]] = {}
    for model in EXPECTED_MODELS:
        payload = _curl_json(
            config,
            deadline,
            "/v1/embeddings",
            {"model": model, "input": list(CANARY_INPUTS)},
        )
        vectors(payload, model)
        aliases[model] = payload
    return {"aliases": aliases}


def wait_new_generation(
    config: RuntimeConfig, before: Generation, deadline: float
) -> Generation:
    ready_deadline = min(deadline, time.monotonic() + config.ready_seconds)
    while time.monotonic() < ready_deadline:
        try:
            current = query_generation(config, deadline)
            if generation_is_new(before, current):
                verify_models(config, deadline)
                after_models = query_generation(config, deadline)
                if after_models.stable == current.stable:
                    return current
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise ActivationCheckError("embedding did not reach a truly new ready generation")
