from __future__ import annotations

import dataclasses
import json
import os
import posixpath
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, Sequence


__all__ = ["main"]


class ContractError(RuntimeError):
    pass


class NotReady(RuntimeError):
    pass


def reject(message: str) -> NoReturn:
    raise ContractError(message)


DOCKER_BIN, SYSTEMCTL_BIN, PROC_ROOT_RAW, CGROUP_ROOT_RAW = sys.argv[1:5]
try:
    WAIT_SECONDS = int(sys.argv[5])
    COMMAND_TIMEOUT_SECONDS = int(sys.argv[6])
    TEST_ONLY = sys.argv[7] == "1"
except (IndexError, ValueError) as error:
    raise SystemExit("gb10_vllm_no_swap: internal launcher contract is invalid") from error
ARGUMENTS = sys.argv[8:]
PROC_ROOT = Path(PROC_ROOT_RAW)
CGROUP_ROOT = Path(CGROUP_ROOT_RAW)
MAX_OUTPUT = 2 * 1024 * 1024
FULL_ID = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
STARTED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z")
COMPONENT = re.compile(r"[A-Za-z0-9_.@:-]+")


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclasses.dataclass(frozen=True)
class UnitContract:
    path: Path
    container: str
    cidfile: str
    memory: int
    image: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ContainerSnapshot:
    identifier: str
    name: str
    pid: int
    started_at: str
    memory: int
    memory_swap: int
    image: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class CgroupEvidence:
    path: str
    device: int
    inode: int
    events: bytes
    memory_max: int
    swap_max: int
    swap_current: int


def run_command(
    arguments: Sequence[str],
    *,
    timeout: int | None = None,
    allow_failure: bool = False,
) -> CommandResult:
    if not arguments or any(not isinstance(value, str) or "\x00" in value for value in arguments):
        reject("internal command argv is invalid")
    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except OSError as error:
        reject(f"cannot execute required command: {arguments[0]}: {error}")
    try:
        stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT_SECONDS if timeout is None else timeout)
    except subprocess.TimeoutExpired:
        for selected_signal, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 1)):
            try:
                os.killpg(process.pid, selected_signal)
            except OSError:
                pass
            try:
                process.communicate(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            for stream in (process.stdout, process.stderr):
                if stream is not None:  # Stop escaped descendants from owning our read ends.
                    stream.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                reject(f"bounded command could not be reaped after timeout: {arguments[0]}")
        reject(f"bounded command timed out: {arguments[0]}")
    if len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT:
        reject(f"bounded command output exceeded limit: {arguments[0]}")
    result = CommandResult(process.returncode, stdout, stderr)
    if result.returncode != 0 and not allow_failure:
        reject(f"required command failed: {arguments[0]}")
    return result


def canonical_absolute(raw: str, label: str) -> Path:
    if (
        not raw.startswith("/")
        or raw == "/"
        or raw.endswith("/")
        or "//" in raw
        or "/./" in raw
        or "/../" in raw
        or raw.endswith(("/.", "/.."))
        or "\x00" in raw
        or posixpath.normpath(raw) != raw
    ):
        reject(f"{label} is not a canonical absolute path")
    return Path(raw)


def require_directory_chain(path: Path, label: str) -> os.stat_result:
    raw = str(path)
    canonical_absolute(raw, label)
    current = Path("/")
    metadata = os.lstat(current)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            reject(f"{label} is unavailable: {error}")
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            reject(f"{label} contains a non-directory or symlink component")
    return metadata


def read_regular(path: Path, limit: int, label: str) -> tuple[bytes, os.stat_result]:
    require_directory_chain(path.parent, f"{label} parent")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        reject(f"{label} is missing, unreadable, or unsafe: {error}")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            reject(f"{label} is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                reject(f"{label} exceeds its byte bound")
        return b"".join(chunks), metadata
    finally:
        os.close(descriptor)


def decode_text(payload: bytes, label: str) -> str:
    if b"\x00" in payload:
        reject(f"{label} contains a NUL byte")
    try:
        return payload.decode("utf-8")
    except UnicodeError as error:
        reject(f"{label} is not strict UTF-8: {error}")


def logical_exec_start(text: str) -> list[str]:
    commands: list[list[str]] = []
    pending: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if pending:
            value = line
        elif line.startswith("ExecStart="):
            value = line.removeprefix("ExecStart=")
        else:
            continue
        continued = value.endswith("\\")
        pending.append(value[:-1].rstrip() if continued else value)
        if not continued:
            try:
                commands.append(shlex.split(" ".join(pending), posix=True))
            except ValueError as error:
                reject(f"unit ExecStart cannot be parsed: {error}")
            pending = []
    if pending or len(commands) != 1:
        reject("unit must contain exactly one complete ExecStart")
    return commands[0]


DOCKER_ONE_VALUE = {
    "--cgroup-parent",
    "--cidfile",
    "--dns",
    "--entrypoint",
    "--env",
    "--gpus",
    "--hostname",
    "--ipc",
    "--memory",
    "--memory-swap",
    "--memory-swappiness",
    "--name",
    "--network",
    "--oom-score-adj",
    "--publish",
    "--shm-size",
    "--tmpfs",
    "--user",
    "--volume",
    "-e",
    "-p",
    "-v",
}
DOCKER_ZERO_VALUE = {"--init", "--read-only", "--rm"}


def parse_memory(raw: str) -> int:
    matched = re.fullmatch(r"([1-9][0-9]*)([bBkKmMgG]?)", raw)
    if matched is None:
        reject("Docker memory limit is not an exact positive size")
    multiplier = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[
        matched.group(2).lower()
    ]
    value = int(matched.group(1)) * multiplier
    if value <= 0:
        reject("Docker memory limit is invalid")
    return value


def parse_unit(path_raw: str) -> UnitContract:
    path = canonical_absolute(path_raw, "unit path")
    payload, metadata = read_regular(path, 256_000, "unit file")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        reject("unit file owner or mode is unsafe")
    argv = logical_exec_start(decode_text(payload, "unit file"))
    if len(argv) < 4 or argv[:2] != ["/usr/bin/docker", "run"]:
        reject("unit ExecStart is not one direct absolute Docker run")

    options: dict[str, list[str]] = {}
    index = 2
    while index < len(argv):
        token = argv[index]
        if token == "--":
            reject("Docker option terminator is forbidden before the image")
        if not token.startswith("-"):
            break
        if "=" in token:
            option, value = token.split("=", 1)
            if option not in DOCKER_ONE_VALUE or not value:
                reject(f"unsupported or malformed Docker option: {token}")
            options.setdefault(option, []).append(value)
            index += 1
            continue
        if token in DOCKER_ZERO_VALUE:
            options.setdefault(token, []).append("")
            index += 1
            continue
        if token not in DOCKER_ONE_VALUE or index + 1 >= len(argv):
            reject(f"unsupported or incomplete Docker option: {token}")
        value = argv[index + 1]
        if not value or (value.startswith("-") and token not in {"-e", "--env"}):
            reject(f"Docker option lacks a direct value: {token}")
        options.setdefault(token, []).append(value)
        index += 2
    if index >= len(argv):
        reject("Docker run image is missing")
    image = argv[index]
    command = argv[index + 1 :]
    if re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) is None:
        reject("Docker image is not immutable by sha256 digest")
    if len(command) < 3:
        reject("container command is incomplete")

    def exactly_one(option: str) -> str:
        values = options.get(option, [])
        if len(values) != 1:
            reject(f"unit requires exactly one {option}")
        return values[0]

    container = exactly_one("--name")
    if NAME.fullmatch(container) is None:
        reject("unit container name is unsafe")
    cidfile = exactly_one("--cidfile")
    if cidfile.startswith("%t/"):
        suffix = cidfile.removeprefix("%t/")
        if not suffix or "//" in suffix or ".." in Path(suffix).parts:
            reject("unit cidfile path is unsafe")
    else:
        canonical_absolute(cidfile, "unit cidfile path")
    memory_raw = exactly_one("--memory")
    memory_swap_raw = exactly_one("--memory-swap")
    if memory_raw != memory_swap_raw:
        reject("Docker MemorySwap intent does not exactly equal Memory")
    memory = parse_memory(memory_raw)
    if exactly_one("--memory-swappiness") != "0":
        reject("Docker memory swappiness intent is not exactly zero")
    if any(
        token.split("=", 1)[0].replace("_", "-") == "--swap-space"
        for token in command
    ):
        reject("unsupported vLLM --swap-space option is forbidden")
    entrypoint_value = exactly_one("--entrypoint")
    if entrypoint_value != "python3":
        reject("container entrypoint is not the direct pinned Python launcher")

    if command[0] not in {
        "/usr/local/bin/vllm",
        "/opt/hang_guard/aeon_vllm_wrapper.py",
    } or command[1] != "serve":
        reject("container command is not a direct tracked vLLM launcher")
    if any(token in {"/bin/sh", "/bin/bash", "sh", "bash", "-c"} for token in command[:3]):
        reject("shell-wrapped vLLM command is forbidden")
    return UnitContract(
        path,
        container,
        cidfile,
        memory,
        image,
        (entrypoint_value,),
        tuple(command),
    )


def known_absence(result: CommandResult) -> bool:
    if result.returncode == 0:
        return False
    try:
        message = result.stderr.decode("utf-8", "strict")
    except UnicodeError:
        return False
    return (
        "No such object:" in message
        or "No such container:" in message
        or "No such container: " in message
    )


def inspect_payload(reference: str, *, allow_absent: bool) -> dict[str, object] | None:
    result = run_command(
        [DOCKER_BIN, "inspect", "--type", "container", reference],
        allow_failure=True,
    )
    if result.returncode != 0:
        if allow_absent and known_absence(result):
            return None
        reject(f"Docker inspect failed closed for {reference}")
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as error:
        reject(f"Docker inspect JSON is malformed for {reference}: {error}")
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        reject(f"Docker inspect cardinality is invalid for {reference}")
    return payload[0]


def parse_snapshot(payload: dict[str, object], contract: UnitContract) -> ContainerSnapshot:
    identifier = payload.get("Id")
    name = payload.get("Name")
    state = payload.get("State")
    host = payload.get("HostConfig")
    config = payload.get("Config")
    if not isinstance(identifier, str) or FULL_ID.fullmatch(identifier) is None:
        reject("Docker inspect lacks one full immutable container ID")
    if name != f"/{contract.container}":
        reject("Docker inspect name does not match the unit container")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise NotReady("container is not running")
    pid = state.get("Pid")
    started_at = state.get("StartedAt")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(started_at, str)
        or STARTED_AT.fullmatch(started_at) is None
    ):
        reject("Docker PID or StartedAt generation evidence is malformed")
    if not isinstance(host, dict) or not isinstance(config, dict):
        reject("Docker inspect lacks HostConfig or Config authority")
    memory = host.get("Memory")
    memory_swap = host.get("MemorySwap")
    if (
        not isinstance(memory, int)
        or isinstance(memory, bool)
        or not isinstance(memory_swap, int)
        or isinstance(memory_swap, bool)
        or memory != contract.memory
        or memory_swap != contract.memory
    ):
        reject("Docker HostConfig Memory/MemorySwap differs from the exact unit cap")
    image = config.get("Image")
    entrypoint = config.get("Entrypoint")
    command = config.get("Cmd")
    if (
        image != contract.image
        or not isinstance(entrypoint, list)
        or any(not isinstance(value, str) for value in entrypoint)
        or tuple(entrypoint) != contract.entrypoint
        or not isinstance(command, list)
        or any(not isinstance(value, str) for value in command)
        or tuple(command) != contract.command
    ):
        reject("immutable Docker image, Entrypoint, or Cmd differs from the unit")
    return ContainerSnapshot(
        identifier,
        contract.container,
        pid,
        started_at,
        memory,
        memory_swap,
        image,
        tuple(entrypoint),
        tuple(command),
    )


def wait_snapshot(contract: UnitContract) -> ContainerSnapshot:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        payload = inspect_payload(contract.container, allow_absent=True)
        if payload is not None:
            try:
                return parse_snapshot(payload, contract)
            except NotReady:
                pass
        if time.monotonic() >= deadline:
            reject(f"container did not become running before deadline: {contract.container}")
        time.sleep(min(1.0, max(0.01, deadline - time.monotonic())))


def proc_starttime(pid: int) -> int:
    payload, _metadata = read_regular(
        PROC_ROOT / str(pid) / "stat", 64 * 1024, "process stat"
    )
    text = decode_text(payload, "process stat").rstrip("\n")
    prefix = f"{pid} ("
    end = text.rfind(")")
    if not text.startswith(prefix) or end < len(prefix):
        reject("process stat identity is malformed")
    suffix = text[end + 1 :].strip().split()
    if len(suffix) < 20 or not suffix[19].isdigit() or int(suffix[19]) <= 0:
        reject("process stat starttime is malformed")
    return int(suffix[19])


def canonical_proc_scope(pid: int, identifier: str) -> str:
    payload, _metadata = read_regular(
        PROC_ROOT / str(pid) / "cgroup", 64 * 1024, "process cgroup"
    )
    text = decode_text(payload, "process cgroup")
    rows = text.splitlines()
    unified: list[str] = []
    for row in rows:
        pieces = row.split(":", 2)
        if len(pieces) != 3:
            reject("process cgroup row is malformed")
        if pieces[0] == "0" and pieces[1] == "":
            unified.append(pieces[2])
    if len(unified) != 1:
        reject("process lacks exactly one unified cgroup row")
    path = unified[0]
    canonical_absolute(path, "process unified cgroup path")
    components = path.removeprefix("/").split("/")
    if any(COMPONENT.fullmatch(component) is None for component in components):
        reject("process unified cgroup path has an unsafe component")
    expected_scope = f"docker-{identifier}.scope"
    if components[-1] != expected_scope:
        reject("process unified cgroup does not end in the exact full-ID Docker scope")
    if any(component.endswith(".scope") for component in components[:-1]):
        reject("process unified cgroup contains a wrapper or sibling scope")
    return path


def systemd_scope(identifier: str) -> str:
    result = run_command(
        [
            SYSTEMCTL_BIN,
            "--user",
            "show",
            "-p",
            "ControlGroup",
            "--value",
            f"docker-{identifier}.scope",
        ]
    )
    text = decode_text(result.stdout, "systemd Docker ControlGroup")
    rows = text.splitlines()
    if len(rows) != 1:
        reject("systemd Docker ControlGroup is not exactly one row")
    canonical_absolute(rows[0], "systemd Docker ControlGroup")
    return rows[0]


def read_uint(path: Path, label: str) -> int:
    payload, _metadata = read_regular(path, 128, label)
    if re.fullmatch(rb"(?:0|[1-9][0-9]*)\n?", payload) is None:
        reject(f"{label} is not one canonical unsigned integer")
    return int(payload.rstrip(b"\n"))


def read_populated(path: Path) -> bytes:
    payload, _metadata = read_regular(path, 64 * 1024, "cgroup.events")
    text = decode_text(payload, "cgroup.events")
    values: dict[str, int] = {}
    for row in text.splitlines():
        pieces = row.split()
        if len(pieces) != 2 or COMPONENT.fullmatch(pieces[0]) is None or not pieces[1].isdigit():
            reject("cgroup.events has a malformed row")
        if pieces[0] in values:
            reject("cgroup.events has a duplicate authority key")
        values[pieces[0]] = int(pieces[1])
    if values.get("populated") != 1:
        reject("cgroup.events does not authoritatively report populated 1")
    return payload


def cgroup_evidence(path: str, expected_memory: int) -> CgroupEvidence:
    root_metadata = require_directory_chain(CGROUP_ROOT, "cgroup root")
    if not stat.S_ISDIR(root_metadata.st_mode):
        reject("cgroup root is not a directory")
    scope_path = CGROUP_ROOT / path.removeprefix("/")
    metadata = require_directory_chain(scope_path, "container cgroup directory")
    events = read_populated(scope_path / "cgroup.events")
    memory_max = read_uint(scope_path / "memory.max", "memory.max")
    swap_max = read_uint(scope_path / "memory.swap.max", "memory.swap.max")
    swap_current = read_uint(scope_path / "memory.swap.current", "memory.swap.current")
    if memory_max != expected_memory:
        reject("memory.max does not equal the exact unit-derived cap")
    if swap_max != 0:
        reject("memory.swap.max is not exactly zero")
    if swap_current != 0:
        reject("activation-time memory.swap.current is not exactly zero")
    return CgroupEvidence(
        path,
        metadata.st_dev,
        metadata.st_ino,
        events,
        memory_max,
        swap_max,
        swap_current,
    )


def verify_generation(contract: UnitContract) -> None:
    first = wait_snapshot(contract)
    first_starttime = proc_starttime(first.pid)
    first_proc_scope = canonical_proc_scope(first.pid, first.identifier)
    first_systemd_scope = systemd_scope(first.identifier)
    if first_systemd_scope != first_proc_scope:
        reject("systemd ControlGroup cross-check disagrees with /proc attribution")
    first_cgroup = cgroup_evidence(first_proc_scope, contract.memory)

    payload = inspect_payload(contract.container, allow_absent=False)
    assert payload is not None
    try:
        second = parse_snapshot(payload, contract)
    except NotReady as error:
        reject(f"container stopped during generation verification: {error}")
    if second != first:
        reject("Docker ID/PID/StartedAt/config generation changed during verification")
    second_starttime = proc_starttime(second.pid)
    second_proc_scope = canonical_proc_scope(second.pid, second.identifier)
    second_systemd_scope = systemd_scope(second.identifier)
    second_cgroup = cgroup_evidence(second_proc_scope, contract.memory)
    if second_systemd_scope != second_proc_scope:
        reject("systemd ControlGroup cross-check changed or disagrees with /proc")
    if first_starttime != second_starttime:
        reject("process starttime changed during verification")
    if first_proc_scope != second_proc_scope:
        reject("process unified cgroup path changed during verification")
    if first_systemd_scope != second_systemd_scope:
        reject("systemd Docker ControlGroup changed during verification")
    if first_cgroup != second_cgroup:
        reject("cgroup directory identity or authoritative files changed during verification")


def docker_cgroup_v2_preflight() -> None:
    result = run_command(
        [DOCKER_BIN, "info", "--format", "{{.CgroupVersion}}"],
        allow_failure=True,
    )
    if result.returncode != 0 or result.stdout not in {b"2", b"2\n"}:
        reject("Docker must report cgroup version exactly 2 before verification")


def minimal_identity(payload: dict[str, object], reference: str) -> tuple[str, str]:
    identifier = payload.get("Id")
    name = payload.get("Name")
    if (
        not isinstance(identifier, str)
        or FULL_ID.fullmatch(identifier) is None
        or not isinstance(name, str)
        or NAME.fullmatch(name.removeprefix("/")) is None
        or name != "/" + name.removeprefix("/")
    ):
        reject(f"cleanup Docker identity is malformed for {reference}")
    return identifier, name.removeprefix("/")


def cleanup(container: str, cidfile_raw: str) -> None:
    if NAME.fullmatch(container) is None:
        reject("cleanup container name is unsafe")
    cidfile = canonical_absolute(cidfile_raw, "cleanup cidfile")
    require_directory_chain(cidfile.parent, "cleanup cidfile parent")
    try:
        lexical = os.lstat(cidfile)
    except FileNotFoundError:
        lexical = None
    except OSError as error:
        reject(f"cannot inspect cleanup cidfile: {error}")
    if lexical is None:
        by_name = inspect_payload(container, allow_absent=True)
        if by_name is not None:
            reject("container exists without its private cidfile authority")
        return
    if stat.S_ISLNK(lexical.st_mode) or not stat.S_ISREG(lexical.st_mode):
        reject("cleanup cidfile is not a regular non-symlink file")
    if lexical.st_uid != os.geteuid() or stat.S_IMODE(lexical.st_mode) & 0o077:
        reject("cleanup cidfile is not owner-only")
    payload, opened = read_regular(cidfile, 128, "cleanup cidfile")
    if (opened.st_dev, opened.st_ino) != (lexical.st_dev, lexical.st_ino):
        reject("cleanup cidfile changed while opening")
    text = decode_text(payload, "cleanup cidfile")
    if re.fullmatch(r"[0-9a-f]{64}\n?", text) is None:
        reject("cleanup cidfile does not contain exactly one full container ID")
    identifier = text.rstrip("\n")
    by_name = inspect_payload(container, allow_absent=True)
    by_identifier = inspect_payload(identifier, allow_absent=True)
    if by_name is None and by_identifier is None:
        current = os.lstat(cidfile)
        if (current.st_dev, current.st_ino) != (lexical.st_dev, lexical.st_ino):
            reject("cleanup cidfile changed before idempotent removal")
        os.unlink(cidfile)
        return
    if by_name is None or by_identifier is None:
        reject("cleanup name and full-ID authority disagree")
    name_id, name_value = minimal_identity(by_name, container)
    id_id, id_name = minimal_identity(by_identifier, identifier)
    if (
        name_id != identifier
        or id_id != identifier
        or name_value != container
        or id_name != container
    ):
        reject("cleanup detected a stale or replacement container identity")

    stopped = run_command(
        [DOCKER_BIN, "stop", "--time", "20", identifier],
        timeout=35,
        allow_failure=True,
    )
    if stopped.returncode != 0:
        reject("generation-bound Docker stop failed")
    removed = run_command(
        [DOCKER_BIN, "rm", "-f", identifier],
        timeout=15,
        allow_failure=True,
    )
    if removed.returncode != 0 and not known_absence(removed):
        reject("generation-bound Docker remove failed")
    if inspect_payload(container, allow_absent=True) is not None:
        reject("container name still resolves after cleanup")
    if inspect_payload(identifier, allow_absent=True) is not None:
        reject("full container ID still resolves after cleanup")
    current = os.lstat(cidfile)
    if (current.st_dev, current.st_ino) != (lexical.st_dev, lexical.st_ino):
        reject("cleanup cidfile changed before final removal")
    os.unlink(cidfile)


def parse_cli(arguments: Sequence[str]) -> tuple[bool, list[str], list[str], str | None]:
    cleanup_mode = False
    units: list[str] = []
    containers: list[str] = []
    cidfile: str | None = None
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--cleanup":
            if cleanup_mode:
                reject("--cleanup may appear only once")
            cleanup_mode = True
            index += 1
            continue
        if token in {"--unit", "--container", "--cidfile"}:
            if index + 1 >= len(arguments):
                reject(f"{token} requires one direct value")
            value = arguments[index + 1]
            if token == "--unit":
                units.append(value)
            elif token == "--container":
                containers.append(value)
            else:
                if cidfile is not None:
                    reject("--cidfile may appear only once")
                cidfile = value
            index += 2
            continue
        if token in {"-h", "--help"}:
            reject(
                "usage: [--unit ABSOLUTE_PATH]... [--container NAME]... or "
                "--cleanup --container NAME --cidfile ABSOLUTE_PATH"
            )
        reject(f"unknown CLI argument: {token}")
    if cleanup_mode:
        if units or len(containers) != 1 or cidfile is None:
            reject("cleanup requires exactly one --container and one --cidfile")
    else:
        if cidfile is not None or not units:
            reject("verification requires at least one --unit")
        if len(set(units)) != len(units) or len(set(containers)) != len(containers):
            reject("verification arguments contain duplicates")
    return cleanup_mode, units, containers, cidfile


def main() -> None:
    cleanup_mode, unit_paths, containers, cidfile = parse_cli(ARGUMENTS)
    if cleanup_mode:
        assert cidfile is not None
        cleanup(containers[0], cidfile)
        print("gb10_vllm_no_swap: cleanup verified")
        return

    docker_cgroup_v2_preflight()
    contracts = [parse_unit(path) for path in unit_paths]
    by_container: dict[str, UnitContract] = {}
    for contract in contracts:
        if contract.container in by_container:
            reject(f"multiple unit contracts claim container: {contract.container}")
        by_container[contract.container] = contract
    for container in containers:
        if NAME.fullmatch(container) is None:
            reject(f"unsafe container name: {container}")
        contract = by_container.get(container)
        if contract is None:
            reject(f"container lacks one matching --unit authority: {container}")
        verify_generation(contract)
    print(
        "gb10_vllm_no_swap: verified "
        f"units={len(contracts)} containers={len(containers)} cgroup_version=2"
    )


try:
    main()
except ContractError as error:
    print(f"gb10_vllm_no_swap: {error}", file=sys.stderr)
    raise SystemExit(1)
except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as error:
    print(f"gb10_vllm_no_swap: fail-closed evidence error: {error}", file=sys.stderr)
    raise SystemExit(1)
