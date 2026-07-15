#!/usr/bin/env python3
"""Fail-closed current-generation verifier for the embedding activation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import pwd
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gb10_embedding_profile_contract import (  # noqa: E402
    EXPECTED_CONTAINER,
    EXPECTED_CONTAINER_ARGV,
    EXPECTED_IMAGE,
    EXPECTED_MODELS,
    validate_effective_commands,
    validate_unit,
    validate_unit_text,
)
from gb10_embedding_verifier_runtime import (  # noqa: E402
    command,
    fail,
    json_file as _json_file,
    json_text as _json_text,
    read_nofollow as _read_nofollow,
    remaining as _remaining,
    secure_write_json as _secure_write_json,
    text_file as _text,
    validate_evidence_dir as _validate_evidence_dir,
)

EXPECTED_MEMORY = 20 * 1024**3
UNIT = "vllm-embedding.service"
CANARY_INPUTS = (
    "gb10-embedding-canary-v1",
    "source-safe deterministic parity anchor",
    "source-safe unrelated control",
)
_SYSTEMD_FIELDS = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "FragmentPath",
    "DropInPaths",
    "MainPID",
    "ControlGroup",
    "InvocationID",
    "ExecMainStartTimestampMonotonic",
    "ExecStart",
    "ExecStartPre",
    "ExecStartPost",
    "ExecStop",
)



@dataclass(frozen=True)
class RuntimeConfig:
    source_unit: Path
    installed_unit: Path
    expected_fragment: Path
    proc_root: Path
    cgroup_root: Path
    systemctl: str
    docker: str
    journalctl: str
    curl: str
    deadline_seconds: float


@dataclass(frozen=True)
class SystemdState:
    invocation: str
    main_pid: int
    start_monotonic: int
    control_group: str
    stable_fields: tuple[str, ...]


@dataclass(frozen=True)
class ContainerState:
    identifier: str
    pid: int
    started_at: str
    cgroup_path: Path
    cgroup_identity: tuple[int, int]


def _parse_systemd_show(text: str, expected_fragment: Path) -> SystemdState:
    if len(text.encode()) > 128 * 1024 or "\x00" in text:
        fail("systemd show output exceeded parser bound")
    fields: dict[str, str] = {}
    for row in text.splitlines():
        key, separator, value = row.partition("=")
        if separator != "=" or key not in _SYSTEMD_FIELDS or key in fields:
            fail("malformed, duplicate, or unexpected systemd show field")
        fields[key] = value
    if set(fields) != set(_SYSTEMD_FIELDS):
        fail("missing systemd show field")
    if fields["Id"] != UNIT or fields["LoadState"] != "loaded":
        fail("systemd unit is not the exact loaded embedding service")
    if (fields["ActiveState"], fields["SubState"]) != ("active", "running"):
        fail("embedding service is not active/running")
    if fields["FragmentPath"] != str(expected_fragment) or fields["DropInPaths"]:
        fail("effective fragment or drop-ins are not canonical")
    if not re.fullmatch(r"[0-9a-f]{32}", fields["InvocationID"]):
        fail("invalid systemd InvocationID")
    if not re.fullmatch(r"[1-9][0-9]*", fields["MainPID"]):
        fail("invalid systemd MainPID")
    if not re.fullmatch(r"[1-9][0-9]*", fields["ExecMainStartTimestampMonotonic"]):
        fail("invalid systemd start generation")
    control_group = fields["ControlGroup"]
    if (
        not control_group.startswith("/")
        or ".." in Path(control_group).parts
        or not control_group.endswith("/app.slice/vllm-embedding.service")
        and control_group != "/app.slice/vllm-embedding.service"
    ):
        fail("invalid embedding systemd control group")
    validate_effective_commands(fields)
    return SystemdState(
        invocation=fields["InvocationID"],
        main_pid=int(fields["MainPID"]),
        start_monotonic=int(fields["ExecMainStartTimestampMonotonic"]),
        control_group=control_group,
        stable_fields=tuple(fields[name] for name in _SYSTEMD_FIELDS),
    )


def _query_systemd(config: RuntimeConfig, deadline: float) -> SystemdState:
    argv = [config.systemctl, "--user", "show", UNIT, "--no-pager"]
    argv.extend(f"--property={name}" for name in _SYSTEMD_FIELDS)
    return _parse_systemd_show(
        command(argv, timeout=10, deadline=deadline), config.expected_fragment
    )


def _unified_cgroup(proc_root: Path, pid: int) -> str:
    rows = _text(proc_root / str(pid) / "cgroup", 64 * 1024).splitlines()
    paths: list[str] = []
    for row in rows:
        pieces = row.split(":", 2)
        if len(pieces) == 3 and pieces[0] == "0" and pieces[1] == "":
            paths.append(pieces[2])
    if len(paths) != 1 or not paths[0].startswith("/") or ".." in Path(paths[0]).parts:
        fail("process lacks one canonical unified cgroup")
    return paths[0]


def _parse_keyed_uints(path: Path, required: set[str]) -> dict[str, int]:
    values: dict[str, int] = {}
    for row in _text(path, 64 * 1024).splitlines():
        pieces = row.split()
        if (
            len(pieces) != 2
            or not re.fullmatch(r"[a-z_]+", pieces[0])
            or not pieces[1].isdigit()
            or pieces[0] in values
        ):
            fail(f"malformed or duplicate metric: {path.name}")
        values[pieces[0]] = int(pieces[1])
    if not required.issubset(values):
        fail(f"missing metric in {path.name}")
    return values


def _read_uint(path: Path) -> int:
    value = _text(path, 128).strip()
    if not re.fullmatch(r"[0-9]+", value):
        fail(f"invalid cgroup metric: {path.name}")
    return int(value)


def _verify_populated(cgroup_root: Path, relative: str) -> tuple[Path, tuple[int, int]]:
    path = cgroup_root / relative.removeprefix("/")
    events = _parse_keyed_uints(path / "cgroup.events", {"populated"})
    if events["populated"] != 1:
        fail("cgroup is not currently populated")
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        fail("cgroup path is not a directory")
    return path, (metadata.st_dev, metadata.st_ino)


def _verify_main_process(config: RuntimeConfig, state: SystemdState) -> tuple[int, int]:
    if _unified_cgroup(config.proc_root, state.main_pid) != state.control_group:
        fail("systemd MainPID is outside the exact service cgroup")
    _path, identity = _verify_populated(config.cgroup_root, state.control_group)
    return identity


def _container_payload(config: RuntimeConfig, deadline: float) -> dict[str, Any]:
    raw = command(
        [config.docker, "inspect", EXPECTED_CONTAINER], timeout=10, deadline=deadline
    )
    payload = _json_text(raw)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        fail("docker inspect must return one container object")
    return payload[0]


def _verify_container(
    config: RuntimeConfig, payload: dict[str, Any]
) -> ContainerState:
    identifier = payload.get("Id")
    name = payload.get("Name")
    state = payload.get("State")
    container_config = payload.get("Config")
    host = payload.get("HostConfig")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        fail("invalid immutable Docker ID")
    if name != f"/{EXPECTED_CONTAINER}" or not isinstance(state, dict):
        fail("Docker identity is not the exact embedding container")
    if state.get("Running") is not True:
        fail("embedding container is not running")
    pid = state.get("Pid")
    started_at = state.get("StartedAt")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(started_at, str)
        or not started_at
    ):
        fail("Docker process generation is malformed")
    if not isinstance(container_config, dict) or not isinstance(host, dict):
        fail("Docker inspect lacks exact config objects")
    if (
        container_config.get("Image") != EXPECTED_IMAGE
        or container_config.get("Entrypoint") != ["python3"]
        or container_config.get("Cmd") != EXPECTED_CONTAINER_ARGV
    ):
        fail("running image, entrypoint, or container argv is not canonical")
    expected_host = {
        "Memory": EXPECTED_MEMORY,
        "MemorySwap": EXPECTED_MEMORY,
        "MemorySwappiness": 0,
        "OomScoreAdj": 0,
        "AutoRemove": True,
        "CgroupParent": "",
        "PortBindings": {
            "8000/tcp": [{"HostIp": "100.105.4.92", "HostPort": "18012"}]
        },
    }
    for key, expected in expected_host.items():
        if host.get(key) != expected:
            fail(f"running Docker host contract differs: {key}")
    relative = _unified_cgroup(config.proc_root, pid)
    expected_suffix = f"/app.slice/docker-{identifier}.scope"
    if not relative.endswith(expected_suffix):
        fail("container PID is outside its immutable Docker cgroup")
    cgroup, identity = _verify_populated(config.cgroup_root, relative)
    expected_metrics = {
        "memory.max": EXPECTED_MEMORY,
        "memory.swap.max": 0,
        "memory.swap.current": 0,
    }
    for filename, expected in expected_metrics.items():
        if _read_uint(cgroup / filename) != expected:
            fail(f"wrong cgroup metric: {filename}")
    memory_events = _parse_keyed_uints(
        cgroup / "memory.events", {"max", "oom", "oom_kill"}
    )
    for key in ("max", "oom", "oom_kill"):
        if memory_events[key] != 0:
            fail(f"nonzero cgroup memory event: {key}")
    return ContainerState(identifier, pid, started_at, cgroup, identity)


def vectors(payload: dict[str, Any], expected_model: str | None = None) -> list[list[float]]:
    if expected_model is not None and payload.get("model") != expected_model:
        fail("embedding response model identity differs from request")
    raw_rows = payload.get("data")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(CANARY_INPUTS):
        fail("embedding response has wrong row count")
    parsed: dict[int, list[float]] = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            fail("embedding response row is not an object")
        index = row.get("index")
        vector = row.get("embedding")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index in parsed
            or not isinstance(vector, list)
            or len(vector) != 4096
        ):
            fail("embedding indices or dimensions are malformed")
        converted: list[float] = []
        for value in vector:
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                fail("embedding response contains a non-finite or boolean value")
            converted.append(float(value))
        _scaled_norm(converted)
        parsed[index] = converted
    if set(parsed) != set(range(len(CANARY_INPUTS))):
        fail("embedding response indices are incomplete")
    return [parsed[index] for index in range(len(CANARY_INPUTS))]


def _scaled_norm(vector: list[float]) -> tuple[float, float]:
    scale = max(abs(value) for value in vector)
    if not math.isfinite(scale) or scale <= 0:
        fail("embedding response has a zero or non-finite norm")
    scaled_sum = math.fsum((value / scale) ** 2 for value in vector)
    norm = math.sqrt(scaled_sum)
    if not math.isfinite(norm) or norm <= 0:
        fail("embedding response has a non-finite scaled norm")
    return scale, norm


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        fail("cosine vectors have incompatible dimensions")
    left_scale, left_norm = _scaled_norm(left)
    right_scale, right_norm = _scaled_norm(right)
    numerator = math.fsum(
        (a / left_scale) * (b / right_scale)
        for a, b in zip(left, right, strict=True)
    )
    denominator = left_norm * right_norm
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0:
        fail("embedding cosine has non-finite derived values")
    score = numerator / denominator
    if not math.isfinite(score):
        fail("embedding cosine is non-finite")
    return max(-1.0, min(1.0, score))


def _rankings(rows: list[list[float]]) -> list[list[int]]:
    result: list[list[int]] = []
    for anchor, vector in enumerate(rows):
        candidates = [
            (cosine(vector, rows[index]), index)
            for index in range(len(rows))
            if index != anchor
        ]
        if any(not math.isfinite(score) for score, _index in candidates):
            fail("embedding ranking contains non-finite score")
        candidates.sort(key=lambda item: (-item[0], item[1]))
        result.append([index for _score, index in candidates])
    return result


def _curl_json(
    config: RuntimeConfig,
    deadline: float,
    url: str,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    argv = [
        config.curl,
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--max-time",
        str(max(1, int(_remaining(deadline, 20)))),
    ]
    input_text: str | None = None
    if request is not None:
        argv.extend(["-H", "Content-Type: application/json", "--data-binary", "@-"])
        input_text = json.dumps(request, sort_keys=True, allow_nan=False)
    argv.append(url)
    payload = _json_text(
        command(argv, timeout=25, input_text=input_text, deadline=deadline)
    )
    if not isinstance(payload, dict):
        fail("HTTP response is not a JSON object")
    return payload


def _verify_models(config: RuntimeConfig, deadline: float) -> None:
    payload = _curl_json(config, deadline, "http://100.105.4.92:18012/v1/models")
    data = payload.get("data")
    if not isinstance(data, list):
        fail("model listing is malformed")
    identifiers: list[str] = []
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            fail("model listing row is malformed")
        identifiers.append(row["id"])
    if identifiers != list(EXPECTED_MODELS) or len(set(identifiers)) != len(identifiers):
        fail("model listing aliases are not exact")


def _verify_quality(
    config: RuntimeConfig, evidence: Path, deadline: float
) -> float:
    baseline_payload = _json_file(evidence / "baselines.json", owner_only=True)
    if not isinstance(baseline_payload, dict):
        fail("baseline payload is malformed")
    aliases = baseline_payload.get("aliases")
    if not isinstance(aliases, dict) or set(aliases) != set(EXPECTED_MODELS):
        fail("baseline aliases are not exact")
    baselines: dict[str, list[list[float]]] = {}
    post: dict[str, list[list[list[float]]]] = {}
    for model in EXPECTED_MODELS:
        baseline = aliases.get(model)
        if not isinstance(baseline, dict):
            fail("baseline response is malformed")
        baselines[model] = vectors(baseline, model)
        post[model] = []
        for _repeat in range(2):
            response = _curl_json(
                config,
                deadline,
                "http://100.105.4.92:18012/v1/embeddings",
                {"model": model, "input": list(CANARY_INPUTS)},
            )
            post[model].append(vectors(response, model))
    comparisons: list[float] = []
    for model in EXPECTED_MODELS:
        baseline_rows = baselines[model]
        baseline_ranking = _rankings(baseline_rows)
        for repetition in post[model]:
            if _rankings(repetition) != baseline_ranking:
                fail("embedding nearest-neighbor ordering changed from baseline")
            comparisons.extend(
                cosine(before, after)
                for before, after in zip(baseline_rows, repetition, strict=True)
            )
        comparisons.extend(
            cosine(left, right)
            for left, right in zip(post[model][0], post[model][1], strict=True)
        )
    for input_index in range(len(CANARY_INPUTS)):
        comparisons.append(
            cosine(
                post[EXPECTED_MODELS[0]][0][input_index],
                post[EXPECTED_MODELS[1]][0][input_index],
            )
        )
    minimum = min(comparisons)
    if not math.isfinite(minimum) or minimum < 0.99999:
        fail("embedding baseline, repeat, or alias stability diverged")
    return minimum


def _verify_engine_capacity(
    config: RuntimeConfig, state: SystemdState, deadline: float
) -> int:
    raw = command(
        [
            config.journalctl,
            "--user",
            "-u",
            UNIT,
            f"_SYSTEMD_INVOCATION_ID={state.invocation}",
            "-o",
            "json",
            "--no-pager",
        ],
        timeout=10,
        deadline=deadline,
    )
    engines: dict[int, int] = {}
    pattern = re.compile(
        r"^\(EngineCore_DP([0-9]+) pid=([1-9][0-9]*)\) "
        r"GPU KV cache size: ([0-9][0-9,]*) tokens$"
    )
    for line in raw.splitlines():
        payload = _json_text(line)
        if not isinstance(payload, dict):
            fail("journal record is not an object")
        if payload.get("_SYSTEMD_INVOCATION_ID") != state.invocation:
            fail("journal record is outside current InvocationID")
        timestamp = payload.get("__MONOTONIC_TIMESTAMP")
        pid = payload.get("_PID")
        message = payload.get("MESSAGE")
        if (
            not isinstance(timestamp, str)
            or not timestamp.isdigit()
            or int(timestamp) < state.start_monotonic
            or not isinstance(pid, str)
            or not re.fullmatch(r"[1-9][0-9]*", pid)
            or not isinstance(message, str)
        ):
            fail("journal record lacks bounded current-generation metadata")
        match = pattern.fullmatch(message)
        if match is None:
            if "GPU KV cache size" in message:
                fail("malformed engine capacity record")
            continue
        rank = int(match.group(1))
        if match.group(2) != pid or rank in engines:
            fail("duplicate or PID-ambiguous engine capacity record")
        capacity_text = match.group(3)
        if not re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+", capacity_text):
            fail("malformed engine capacity value")
        engines[rank] = int(capacity_text.replace(",", ""))
    if set(engines) != {0}:
        fail("missing or unexpected embedding engine process")
    capacity = min(engines.values())
    if capacity < 32768:
        fail("startup KV capacity is below the 32K contract")
    return capacity


def _load_baseline_generation(evidence: Path) -> tuple[str, int, int]:
    payload = _json_file(evidence / "systemd.before.json", owner_only=True)
    if not isinstance(payload, dict) or set(payload) != {
        "InvocationID",
        "MainPID",
        "ExecMainStartTimestampMonotonic",
    }:
        fail("activation baseline generation is malformed")
    invocation = payload["InvocationID"]
    pid = payload["MainPID"]
    started = payload["ExecMainStartTimestampMonotonic"]
    if (
        not isinstance(invocation, str)
        or not re.fullmatch(r"[0-9a-f]{32}", invocation)
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(started, int)
        or isinstance(started, bool)
        or started <= 0
    ):
        fail("activation baseline generation fields are invalid")
    return invocation, pid, started


def _verify_unit_files(config: RuntimeConfig) -> None:
    source = _read_nofollow(config.source_unit, 64 * 1024)
    installed = _read_nofollow(config.installed_unit, 64 * 1024)
    if source != installed:
        fail("installed embedding unit differs from canonical source bytes")
    for path in (config.source_unit, config.installed_unit):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o644:
            fail("embedding unit mode is not canonical 0644")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("embedding unit is not UTF-8") from error
    validate_unit_text(text)


def _production_config() -> RuntimeConfig:
    repository = Path(__file__).resolve().parents[1]
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    installed = home / ".config/systemd/user" / UNIT
    return RuntimeConfig(
        source_unit=repository / "systemd" / UNIT,
        installed_unit=installed,
        expected_fragment=installed,
        proc_root=Path("/proc"),
        cgroup_root=Path("/sys/fs/cgroup"),
        systemctl="/usr/bin/systemctl",
        docker="/usr/bin/docker",
        journalctl="/usr/bin/journalctl",
        curl="/usr/bin/curl",
        deadline_seconds=90.0,
    )


def _test_config(path: Path) -> RuntimeConfig:
    payload = _json_file(path)
    keys = {
        "source_unit",
        "installed_unit",
        "expected_fragment",
        "proc_root",
        "cgroup_root",
        "systemctl",
        "docker",
        "journalctl",
        "curl",
        "deadline_seconds",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        fail("test-only verifier config is malformed")
    deadline = payload["deadline_seconds"]
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not 1 <= deadline <= 30:
        fail("test-only deadline is invalid")
    return RuntimeConfig(
        source_unit=Path(payload["source_unit"]),
        installed_unit=Path(payload["installed_unit"]),
        expected_fragment=Path(payload["expected_fragment"]),
        proc_root=Path(payload["proc_root"]),
        cgroup_root=Path(payload["cgroup_root"]),
        systemctl=payload["systemctl"],
        docker=payload["docker"],
        journalctl=payload["journalctl"],
        curl=payload["curl"],
        deadline_seconds=float(deadline),
    )


def main() -> int:
    if len(sys.argv) == 2:
        config = _production_config()
        evidence = Path(sys.argv[1])
    elif len(sys.argv) == 4 and sys.argv[1] == "--test-only":
        config = _test_config(Path(sys.argv[2]))
        evidence = Path(sys.argv[3])
    else:
        fail("usage: gb10_verify_embedding_profile.py EVIDENCE_DIR")
    _validate_evidence_dir(evidence)
    _verify_unit_files(config)
    baseline_invocation, baseline_pid, baseline_start = _load_baseline_generation(
        evidence
    )
    deadline = time.monotonic() + config.deadline_seconds
    first_systemd = _query_systemd(config, deadline)
    if (
        first_systemd.invocation == baseline_invocation
        or first_systemd.main_pid == baseline_pid
        or first_systemd.start_monotonic <= baseline_start
    ):
        fail("embedding service did not enter a truly new systemd generation")
    service_cgroup_identity = _verify_main_process(config, first_systemd)
    first_container = _verify_container(
        config, _container_payload(config, deadline)
    )
    capacity = _verify_engine_capacity(config, first_systemd, deadline)
    _verify_models(config, deadline)
    minimum_cosine = _verify_quality(config, evidence, deadline)
    second_container = _verify_container(
        config, _container_payload(config, deadline)
    )
    second_systemd = _query_systemd(config, deadline)
    second_service_cgroup = _verify_main_process(config, second_systemd)
    if second_systemd.stable_fields != first_systemd.stable_fields:
        fail("systemd generation changed during verification")
    if second_service_cgroup != service_cgroup_identity:
        fail("systemd cgroup generation changed during verification")
    if (
        second_container.identifier != first_container.identifier
        or second_container.pid != first_container.pid
        or second_container.started_at != first_container.started_at
        or second_container.cgroup_identity != first_container.cgroup_identity
        or second_container.cgroup_path != first_container.cgroup_path
    ):
        fail("Docker or cgroup generation changed during verification")
    _secure_write_json(
        evidence / "verification.receipt.json",
        {
            "canary_input_count": len(CANARY_INPUTS),
            "cgroup_populated": True,
            "engine_process_count": 1,
            "generation_changed": True,
            "generation_stable": True,
            "kv_capacity_tokens": capacity,
            "minimum_stability_cosine": minimum_cosine,
            "profile": "qwen3-embedding-8b-32k-4800M-20GiB",
            "quality_claim": "synthetic-baseline-stability-only",
            "vector_dimensions": 4096,
            "verification": "passed",
            "unit_sha256": hashlib.sha256(
                _read_nofollow(config.source_unit, 64 * 1024)
            ).hexdigest(),
        },
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"embedding verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
