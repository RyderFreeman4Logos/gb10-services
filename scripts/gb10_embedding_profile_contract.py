#!/usr/bin/env python3
"""Canonical Qwen3 embedding unit and effective-command contract."""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path
from typing import NoReturn

__all__ = [
    "EXPECTED_CONTAINER",
    "EXPECTED_CONTAINER_ARGV",
    "EXPECTED_EXEC_START",
    "EXPECTED_IMAGE",
    "EXPECTED_MODELS",
    "EXPECTED_PROFILE",
    "validate_effective_commands",
    "validate_unit",
    "validate_unit_text",
]

EXPECTED_IMAGE = (
    "ghcr.io/aeon-7/aeon-vllm-ultimate@"
    "sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d"
)
EXPECTED_CONTAINER = "vllm-embedding"
EXPECTED_MODELS = ("qwen3-embedding-8b", "Qwen/Qwen3-Embedding-8B")
EXPECTED_PROFILE = "qwen3-embedding-8b-32k-4800M-128GiB"
EXPECTED_UNIT_SHA256 = "05516709daf448e84ebc1e2a7b9074fd5480c5279039ccfe5001eecd500a0df4"
EXPECTED_NO_SWAP_PREFIX = [
    "/usr/bin/env",
    "-i",
    "HOME=/home/obj",
    "PATH=/usr/bin:/bin",
    "LC_ALL=C",
    "DOCKER_HOST=unix:///run/user/1001/docker.sock",
    "/usr/bin/bash",
    "--noprofile",
    "--norc",
    "/home/obj/.local/bin/gb10_verify_vllm_no_swap.sh",
]
EXPECTED_CIDFILE = "%t/gb10-vllm-cids/vllm-embedding.cid"
EXPECTED_HOST_ARGV = [
    "/usr/bin/docker",
    "run",
    "--rm",
    f"--cidfile={EXPECTED_CIDFILE}",
    "--name",
    EXPECTED_CONTAINER,
    "--gpus",
    "all",
    "--ipc",
    "host",
    "-p",
    "100.105.4.92:18012:8000",
    "-v",
    "/home/obj/.cache/huggingface:/root/.cache/huggingface",
    "-e",
    "HF_HUB_OFFLINE=1",
    "--memory-swappiness",
    "0",
    "--memory",
    "128g",
    "--memory-swap",
    "128g",
    "--oom-score-adj",
    "0",
    "--entrypoint",
    "python3",
]
EXPECTED_CONTAINER_ARGV = [
    "/usr/local/bin/vllm",
    "serve",
    "Qwen/Qwen3-Embedding-8B",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
    "--served-model-name",
    *EXPECTED_MODELS,
    "--convert",
    "embed",
    "--dtype",
    "bfloat16",
    "--max-model-len",
    "32768",
    "--max-num-batched-tokens",
    "8192",
    "--max-num-seqs",
    "64",
    "--kv-cache-memory-bytes",
    "4800M",
    "--gpu-memory-utilization",
    "0.15",
    "--enforce-eager",
    "--swap-space",
    "0",
]
EXPECTED_EXEC_START = [*EXPECTED_HOST_ARGV, EXPECTED_IMAGE, *EXPECTED_CONTAINER_ARGV]
EXPECTED_EXEC_CONDITION = [
    *EXPECTED_NO_SWAP_PREFIX,
    "--unit",
    "/home/obj/.config/systemd/user/vllm-embedding.service",
]
EXPECTED_CLEANUP = [
    *EXPECTED_NO_SWAP_PREFIX,
    "--cleanup",
    "--container",
    EXPECTED_CONTAINER,
    "--cidfile",
    EXPECTED_CIDFILE,
]
EXPECTED_EXEC_START_PRE = (
    ["/usr/bin/install", "-d", "-m", "0700", "%t/gb10-vllm-cids"],
    EXPECTED_CLEANUP,
)
EXPECTED_EXEC_START_POST = (
    [
        *EXPECTED_NO_SWAP_PREFIX,
        "--unit",
        "/home/obj/.config/systemd/user/vllm-embedding.service",
        "--container",
        EXPECTED_CONTAINER,
    ],
    [
        "/home/obj/.local/bin/gb10_service_ready.sh",
        "embedding",
        "http://100.105.4.92:18012",
        "qwen3-embedding-8b",
        "--deadline",
        "300",
    ],
)
EXPECTED_EXEC_STOP = EXPECTED_CLEANUP
EXPECTED_EXEC_STOP_POST = EXPECTED_CLEANUP
_MAX_UNIT_BYTES = 64 * 1024
_MAX_UNIT_LINES = 512
_MAX_TOKENS = 256


def _fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def _logical_directives(text: str, directive: str) -> list[list[str]]:
    if len(text.encode()) > _MAX_UNIT_BYTES:
        _fail("unit exceeds parser byte bound")
    raw_lines = text.splitlines()
    if len(raw_lines) > _MAX_UNIT_LINES or "\x00" in text:
        _fail("unit exceeds parser line bound or contains NUL")
    result: list[list[str]] = []
    pending: list[str] = []
    prefix = f"{directive}="
    for raw in raw_lines:
        if len(raw) > 4096:
            _fail("unit line exceeds parser bound")
        line = raw.strip()
        if pending:
            if not line or line.startswith(("#", ";", "[")):
                _fail(f"invalid {directive} continuation")
            value = line
        elif line.startswith(prefix):
            value = line[len(prefix) :]
        else:
            continue
        continued = value.endswith("\\")
        pending.append(value[:-1].rstrip() if continued else value)
        if not continued:
            try:
                argv = shlex.split(" ".join(pending), posix=True)
            except ValueError as error:
                raise RuntimeError(f"malformed {directive} quoting") from error
            if not argv or len(argv) > _MAX_TOKENS:
                _fail(f"empty or oversized {directive}")
            result.append(argv)
            pending = []
    if pending:
        _fail(f"unterminated {directive} continuation")
    return result


def _expect_commands(text: str, directive: str, expected: object) -> None:
    if _logical_directives(text, directive) != expected:
        _fail(f"{directive} does not match the canonical embedding contract")


def validate_unit_text(text: str) -> None:
    """Reject any noncanonical command or lifecycle-bearing unit directive."""

    if hashlib.sha256(text.encode()).hexdigest() != EXPECTED_UNIT_SHA256:
        _fail("unit bytes do not match the canonical embedding source")
    _expect_commands(text, "ExecCondition", [EXPECTED_EXEC_CONDITION])
    _expect_commands(text, "ExecStart", [EXPECTED_EXEC_START])
    _expect_commands(text, "ExecStartPre", list(EXPECTED_EXEC_START_PRE))
    _expect_commands(text, "ExecStartPost", list(EXPECTED_EXEC_START_POST))
    _expect_commands(text, "ExecStop", [EXPECTED_EXEC_STOP])
    _expect_commands(text, "ExecStopPost", [EXPECTED_EXEC_STOP_POST])
    for directive in (
        "ExecReload",
        "RootDirectoryStartOnly",
    ):
        if _logical_directives(text, directive):
            _fail(f"unexpected lifecycle directive: {directive}")


def validate_unit(path: Path) -> None:
    """Validate one regular, bounded unit file without following a symlink."""

    if path.is_symlink() or not path.is_file():
        _fail("embedding unit must be a regular non-symlink file")
    validate_unit_text(path.read_text())


def _parse_effective_exec(value: str) -> tuple[list[str], bool]:
    prefix = "{ path="
    argv_marker = " ; argv[]="
    ignore_marker = " ; ignore_errors="
    suffix = " ; }"
    if not value.startswith(prefix) or not value.endswith(suffix):
        _fail("malformed effective systemd command")
    body = value[len(prefix) : -len(suffix)]
    path_value, separator, remainder = body.partition(argv_marker)
    if separator != argv_marker or not path_value or " " in path_value:
        _fail("malformed effective systemd path")
    argv_text, separator, ignore_value = remainder.rpartition(ignore_marker)
    if separator != ignore_marker or ignore_value not in {"yes", "no"}:
        _fail("malformed effective systemd command metadata")
    try:
        argv = shlex.split(argv_text, posix=True)
    except ValueError as error:
        raise RuntimeError("malformed effective systemd argv") from error
    if not argv or len(argv) > _MAX_TOKENS:
        _fail("empty or oversized effective systemd argv")
    executable = argv[0].removeprefix("-")
    if executable != path_value:
        _fail("effective systemd path and argv disagree")
    return argv, ignore_value == "yes"


def _parse_effective_execs(value: str) -> list[tuple[list[str], bool]]:
    separator = " ; } ; "
    parts = value.split(separator)
    if not parts:
        _fail("effective systemd command is missing")
    commands: list[tuple[list[str], bool]] = []
    for index, part in enumerate(parts):
        candidate = part if index == len(parts) - 1 else part + " ; }"
        commands.append(_parse_effective_exec(candidate))
    return commands


def validate_effective_commands(fields: dict[str, str]) -> None:
    """Apply the exact tracked command contract to systemd's effective view."""

    expected = {
        "ExecCondition": [(EXPECTED_EXEC_CONDITION, False)],
        "ExecStart": [(EXPECTED_EXEC_START, False)],
        "ExecStartPre": [(argv, False) for argv in EXPECTED_EXEC_START_PRE],
        "ExecStartPost": [(argv, False) for argv in EXPECTED_EXEC_START_POST],
        "ExecStop": [(EXPECTED_EXEC_STOP, False)],
        "ExecStopPost": [(EXPECTED_EXEC_STOP_POST, False)],
    }
    for name, contract in expected.items():
        if _parse_effective_execs(fields[name]) != contract:
            _fail(f"effective {name} differs from canonical unit")
