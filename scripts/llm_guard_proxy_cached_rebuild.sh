#!/usr/bin/bash -p
# Build Guard from a verified source snapshot and transactionally publish it.
set -euo pipefail
exec /usr/bin/python3 -I - "$@" <<'PY'
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

UNIT = "llm-guard-proxy.service"
HEALTH_URL = "http://100.105.4.92:18009/health"
PRODUCTION_COMPLETE = "LLM_GUARD_REBUILD_PRODUCTION_COMPLETE"
TEST_COMPLETE = "LLM_GUARD_REBUILD_TEST_ONLY_COMPLETE"
GENERATION_FIELDS = (
    "LoadState",
    "ActiveState",
    "SubState",
    "FragmentPath",
    "DropInPaths",
    "MainPID",
    "InvocationID",
    "ExecMainStartTimestampMonotonic",
)
TEST_OVERRIDES = (
    "SOURCE_REPO",
    "SOURCE_BRANCH",
    "SOURCE_DIR",
    "SERVICE_BIN",
    "CACHE_ROOT",
    "LOG_DIR",
    "LOG_FILE",
    "CARGO_BUILD_JOBS",
    "CARGO_TARGET_DIR",
    "LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG",
    "LLM_GUARD_PROXY_REBUILD_GUARD_UNIT",
    "LLM_GUARD_PROXY_REBUILD_PROC_ROOT",
    "LLM_GUARD_PROXY_REBUILD_RECEIPT_DIR",
    "LLM_GUARD_REBUILD_TEST_ONLY",
    "LLM_GUARD_REBUILD_TEST_MISSING_TOOL",
    "LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE",
    "LLM_GUARD_REBUILD_TEST_REPLACE_EXE_DURING_HASH",
)
rollback_logging = False


class RebuildError(RuntimeError):
    pass


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        print(f"[{stamp}] {message}", flush=True)
    except OSError:
        if not rollback_logging:
            raise


def fail(message: str) -> None:
    raise RebuildError(message)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_secure_regular(path: Path, *, executable: bool = False) -> os.stat_result:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or (executable and not metadata.st_mode & stat.S_IXUSR)
    ):
        fail(f"unsafe regular file authority: {path}")
    return metadata


def ensure_private_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        fail(f"unsafe private directory: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        path.chmod(0o700)
        if stat.S_IMODE(path.lstat().st_mode) != 0o700:
            fail(f"could not make directory private: {path}")
    if not existed:
        fsync_directory(path.parent)


def atomic_copy(source: Path, destination: Path, mode: int) -> None:
    ensure_private_directory(destination.parent)
    temporary = destination.with_name(
        f".{destination.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    )
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    target_fd = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        os.fchmod(target_fd, mode)
        os.fsync(target_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        os.close(target_fd)
    os.replace(temporary, destination)
    fsync_directory(destination.parent)


def atomic_copy_fd(source_fd: int, destination: Path, mode: int) -> None:
    ensure_private_directory(destination.parent)
    temporary = destination.with_name(
        f".{destination.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    )
    target_fd = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        offset = 0
        while True:
            chunk = os.pread(source_fd, 1024 * 1024, offset)
            if not chunk:
                break
            offset += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        os.fchmod(target_fd, mode)
        os.fsync(target_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(target_fd)
    os.replace(temporary, destination)
    fsync_directory(destination.parent)


args = sys.argv[1:]
if not args:
    test_only = False
elif args == ["--test-only"]:
    test_only = True
else:
    print(f"usage: {sys.argv[0]} [--test-only]", file=sys.stderr)
    raise SystemExit(64)

if not test_only:
    inherited = [name for name in TEST_OVERRIDES if name in os.environ]
    if inherited:
        print(
            "test overrides require --test-only: " + ",".join(sorted(inherited)),
            file=sys.stderr,
        )
        raise SystemExit(64)
    home = Path("/home/obj")
    source_repo = "https://github.com/RyderFreeman4Logos/llm-guard-proxy"
    source_branch = "main"
    source_dir = home / ".cache/source/llm-guard-proxy-main"
    service_bin = home / ".local/bin/llm-guard-proxy"
    cache_root = home / ".cache/cargo-target/llm-guard-proxy-main"
    guard_config = home / ".config/llm-guard-proxy/config.toml"
    guard_unit = home / ".config/systemd/user/llm-guard-proxy.service"
    proc_root = Path("/proc")
    receipt_dir = home / ".local/state/llm-guard-proxy-rebuild"
    command_path = (
        "/home/obj/.local/bin:/home/obj/.local/share/mise/shims:"
        "/usr/local/bin:/usr/bin:/bin"
    )
    child_env = {
        "HOME": str(home),
        "PATH": command_path,
        "LC_ALL": "C",
        "LANG": "C",
        "CARGO_BUILD_JOBS": "1",
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    }
else:
    print("LLM_GUARD_REBUILD_TEST_ONLY=1", flush=True)

    def required_env(name: str) -> str:
        value = os.environ.get(name, "")
        if not value:
            fail(f"test-only environment is missing {name}")
        return value

    home = Path(required_env("HOME"))
    source_repo = required_env("SOURCE_REPO")
    source_branch = required_env("SOURCE_BRANCH")
    source_dir = Path(required_env("SOURCE_DIR"))
    service_bin = Path(required_env("SERVICE_BIN"))
    cache_root = Path(required_env("CACHE_ROOT"))
    guard_config = Path(required_env("LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG"))
    guard_unit = Path(required_env("LLM_GUARD_PROXY_REBUILD_GUARD_UNIT"))
    proc_root = Path(required_env("LLM_GUARD_PROXY_REBUILD_PROC_ROOT"))
    receipt_dir = Path(required_env("LLM_GUARD_PROXY_REBUILD_RECEIPT_DIR"))
    command_path = required_env("PATH")
    child_env = dict(os.environ)
    child_env.update(
        {
            "HOME": str(home),
            "PATH": command_path,
            "LC_ALL": "C",
            "LANG": "C",
            "CARGO_BUILD_JOBS": "1",
        }
    )

cargo_target = cache_root / "cargo"
child_env["CARGO_TARGET_DIR"] = str(cargo_target)
missing_test_tool = (
    os.environ.get("LLM_GUARD_REBUILD_TEST_MISSING_TOOL", "") if test_only else ""
)
tool_cache: dict[str, str] = {}


def require_tool(name: str) -> str:
    if name == missing_test_tool:
        fail(f"required tool unavailable: {name}")
    if name not in tool_cache:
        resolved = shutil.which(name, path=command_path)
        if not resolved:
            fail(f"required tool unavailable: {name}")
        tool_cache[name] = resolved
    return tool_cache[name]


def execute(
    command: list[str],
    *,
    capture: bool = False,
    stdin: object | None = None,
    pass_fds: tuple[int, ...] = (),
) -> bytes:
    log("+ " + shlex.join(command))
    try:
        result = subprocess.run(
            command,
            check=True,
            env=child_env,
            stdin=stdin,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            pass_fds=pass_fds,
        )
    except subprocess.CalledProcessError as error:
        fail(f"command failed ({error.returncode}): {Path(command[0]).name}")
    output = result.stdout or b""
    if len(output) > 128 * 1024:
        fail(f"command output exceeded bound: {Path(command[0]).name}")
    return output


def capture_tool(name: str, *arguments: str) -> bytes:
    return execute([require_tool(name), *arguments], capture=True)


def clean_source_identity() -> tuple[str, str]:
    status = capture_tool(
        "git",
        "-C",
        str(source_dir),
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status:
        fail("source checkout is not clean")
    commit = capture_tool("git", "-C", str(source_dir), "rev-parse", "HEAD").decode().strip()
    tree = capture_tool(
        "git", "-C", str(source_dir), "rev-parse", "HEAD^{tree}"
    ).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit):
        fail("source commit identity is malformed")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", tree):
        fail("source tree identity is malformed")
    return commit, tree


def tracked_paths() -> list[bytes]:
    payload = capture_tool("git", "-C", str(source_dir), "ls-files", "-z")
    paths = payload.split(b"\0")
    if paths and paths[-1] == b"":
        paths.pop()
    if not paths or len(paths) != len(set(paths)):
        fail("tracked source inventory is empty or duplicated")
    for path in paths:
        parts = path.split(b"/")
        if not path or path.startswith(b"/") or b".." in parts or b"" in parts:
            fail("tracked source path is unsafe")
    return paths


def path_ledger(root: Path, paths: list[bytes], *, metadata: bool) -> str:
    digest = hashlib.sha256()
    root_bytes = os.fsencode(root)
    for relative in paths:
        path = os.path.join(root_bytes, relative)
        info = os.lstat(path)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        kind = stat.S_IFMT(info.st_mode)
        digest.update(kind.to_bytes(4, "big"))
        digest.update(stat.S_IMODE(info.st_mode).to_bytes(4, "big"))
        if stat.S_ISREG(info.st_mode):
            with open(path, "rb") as source:
                content = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    content.update(chunk)
            digest.update(content.digest())
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            target_bytes = target if isinstance(target, bytes) else os.fsencode(target)
            digest.update(hashlib.sha256(target_bytes).digest())
        else:
            fail("tracked source contains unsupported file type")
        if metadata:
            for value in (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ):
                digest.update(value.to_bytes(16, "big", signed=False))
    return digest.hexdigest()


def make_snapshot_read_only(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o500 if mode & 0o111 else 0o400)
        for name in directories:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(0o500)
    root.chmod(0o500)


def cleanup_snapshot(root: Path | None) -> None:
    if root is None or not root.exists():
        return
    for current, directories, files in os.walk(root, topdown=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(0o600)
        for name in directories:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(0o700)
        current_path.chmod(0o700)
    shutil.rmtree(root)


@dataclass(frozen=True)
class Generation:
    pid: int
    invocation: str
    started: int
    fragment: str


def query_generation() -> Generation:
    command = [
        require_tool("systemctl"),
        "--user",
        "show",
        UNIT,
        "--no-pager",
        *(f"--property={field}" for field in GENERATION_FIELDS),
    ]
    payload = execute(command, capture=True)
    if len(payload) > 8192 or b"\x00" in payload or b"\r" in payload:
        fail("systemd generation output is malformed")
    values: dict[str, str] = {}
    for row in payload.decode("ascii").splitlines():
        key, separator, value = row.partition("=")
        if separator != "=" or key not in GENERATION_FIELDS or key in values:
            fail("systemd generation output has duplicate or extra fields")
        values[key] = value
    if set(values) != set(GENERATION_FIELDS):
        fail("systemd generation output is missing fields")
    if (
        values["LoadState"] != "loaded"
        or values["ActiveState"] != "active"
        or values["SubState"] != "running"
        or values["FragmentPath"] != str(guard_unit)
        or values["DropInPaths"]
        or not re.fullmatch(r"[1-9][0-9]*", values["MainPID"])
        or not re.fullmatch(r"[0-9a-f]{32}", values["InvocationID"])
        or not re.fullmatch(
            r"[1-9][0-9]*", values["ExecMainStartTimestampMonotonic"]
        )
    ):
        fail("llm-guard-proxy.service is inactive or has unsupported authority")
    return Generation(
        pid=int(values["MainPID"]),
        invocation=values["InvocationID"],
        started=int(values["ExecMainStartTimestampMonotonic"]),
        fragment=values["FragmentPath"],
    )


def read_small_regular(path: Path, limit: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            fail(f"non-regular proc authority: {path}")
        payload = os.read(descriptor, limit + 1)
        if len(payload) > limit:
            fail(f"proc authority exceeded byte bound: {path}")
        return payload
    finally:
        os.close(descriptor)


def boot_id() -> str:
    payload = read_small_regular(proc_root / "sys/kernel/random/boot_id", 64)
    if not re.fullmatch(
        rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n",
        payload,
    ):
        fail("boot ID is malformed")
    return payload[:-1].decode("ascii")


def proc_starttime(pid: int) -> int:
    payload = read_small_regular(proc_root / str(pid) / "stat", 4096)
    if not payload.endswith(b"\n") or b"\x00" in payload or b"\r" in payload:
        fail("proc stat is malformed")
    text = payload[:-1].decode("ascii")
    marker = text.rfind(") ")
    if marker < 0 or not text.startswith(f"{pid} ("):
        fail("proc stat command boundary is malformed")
    fields = text[marker + 2 :].split()
    if len(fields) < 20 or not re.fullmatch(r"[1-9][0-9]*", fields[19]):
        fail("proc stat starttime is malformed")
    return int(fields[19])


def elf_build_id(path: str, *, pass_fd: int | None = None) -> str:
    pass_fds = (pass_fd,) if pass_fd is not None else ()
    output = execute(
        [require_tool("readelf"), "-n", "--", path],
        capture=True,
        pass_fds=pass_fds,
    ).decode("ascii", errors="strict")
    matches = re.findall(r"(?m)^\s*Build ID:\s*([0-9A-Fa-f]+)\s*$", output)
    if len(matches) != 1:
        fail("ELF build ID unavailable or malformed")
    return matches[0].lower()


@dataclass(frozen=True)
class ExecutableIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    build_id: str


def fd_identity(
    descriptor: int, during_hash: Callable[[], None] | None = None
) -> ExecutableIdentity:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        fail("running executable is not regular")
    digest = hashlib.sha256()
    offset = 0
    hook_called = False
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        offset += len(chunk)
        digest.update(chunk)
        if during_hash is not None and not hook_called:
            during_hash()
            hook_called = True
    after_hash = os.fstat(descriptor)
    fields_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    fields_after = (
        after_hash.st_dev,
        after_hash.st_ino,
        after_hash.st_size,
        after_hash.st_mtime_ns,
        after_hash.st_ctime_ns,
    )
    if fields_before != fields_after:
        fail("held executable changed during hash")
    build_id = elf_build_id(
        f"/proc/self/fd/{descriptor}", pass_fd=descriptor
    )
    after_build_id = os.fstat(descriptor)
    if fields_before != (
        after_build_id.st_dev,
        after_build_id.st_ino,
        after_build_id.st_size,
        after_build_id.st_mtime_ns,
        after_build_id.st_ctime_ns,
    ):
        fail("held executable changed during build-ID read")
    return ExecutableIdentity(*fields_before, digest.hexdigest(), build_id)


def open_runtime_executable(
    pid: int, replacement_during_hash: str = ""
) -> tuple[int, str, ExecutableIdentity]:
    proc_exe = proc_root / str(pid) / "exe"
    link = os.readlink(proc_exe)
    if link.endswith(" (deleted)"):
        fail("running Guard executable is a deleted inode")
    descriptor = os.open(proc_exe, os.O_RDONLY | os.O_CLOEXEC)

    def replace_proc_entry() -> None:
        temporary = proc_exe.with_name(f".exe.replace.{os.getpid()}")
        os.symlink(replacement_during_hash, temporary)
        os.replace(temporary, proc_exe)

    try:
        identity = fd_identity(
            descriptor, replace_proc_entry if replacement_during_hash else None
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, link, identity


def same_object(left: os.stat_result, right: ExecutableIdentity) -> bool:
    return (left.st_dev, left.st_ino) == (right.device, right.inode)


@dataclass
class Prestate:
    link_target: str | None
    generation: Generation
    boot: str
    proc_start: int
    descriptor: int
    running_link: str
    executable: ExecutableIdentity


def snapshot_prestate() -> Prestate:
    service_bin.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = service_bin.lstat()
    except FileNotFoundError:
        link_target = None
    else:
        if not stat.S_ISLNK(metadata.st_mode):
            fail("service binary prestate is neither an exact symlink nor absence")
        link_target = os.readlink(service_bin)
    generation = query_generation()
    current_boot = boot_id()
    starttime = proc_starttime(generation.pid)
    descriptor, running_link, executable = open_runtime_executable(generation.pid)
    if link_target is not None:
        try:
            linked = os.stat(service_bin)
        except OSError:
            os.close(descriptor)
            fail("prior service symlink target is unavailable")
        if not same_object(linked, executable):
            os.close(descriptor)
            fail("prior service symlink does not name the running executable")
    return Prestate(
        link_target,
        generation,
        current_boot,
        starttime,
        descriptor,
        running_link,
        executable,
    )


def service_link_matches(target: str | None) -> bool:
    try:
        metadata = service_bin.lstat()
    except FileNotFoundError:
        return target is None
    return stat.S_ISLNK(metadata.st_mode) and target is not None and os.readlink(
        service_bin
    ) == target


def assert_prestate_unchanged(prestate: Prestate) -> None:
    if query_generation() != prestate.generation:
        fail("systemd generation changed before cutover")
    if boot_id() != prestate.boot or proc_starttime(prestate.generation.pid) != prestate.proc_start:
        fail("runtime generation changed before cutover")
    current = os.stat(proc_root / str(prestate.generation.pid) / "exe")
    if not same_object(current, prestate.executable):
        fail("runtime executable changed before cutover")
    if os.readlink(proc_root / str(prestate.generation.pid) / "exe") != prestate.running_link:
        fail("runtime executable link changed before cutover")
    if not service_link_matches(prestate.link_target):
        fail("service binary link changed before cutover")


def set_service_link(target: str) -> None:
    temporary = service_bin.with_name(
        f".{service_bin.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    )
    os.symlink(target, temporary)
    try:
        os.replace(temporary, service_bin)
        fsync_directory(service_bin.parent)
    finally:
        temporary.unlink(missing_ok=True)


def remove_service_link() -> None:
    try:
        metadata = service_bin.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISLNK(metadata.st_mode):
        fail("service binary changed to unsupported type during transaction")
    service_bin.unlink()
    fsync_directory(service_bin.parent)


def restart_guard() -> None:
    execute([require_tool("systemctl"), "--user", "restart", UNIT])


def health_check() -> None:
    execute(
        [
            require_tool("curl"),
            "-fsS",
            "-m",
            "10",
            "--output",
            "/dev/null",
            HEALTH_URL,
        ]
    )


def sleep_after_restart() -> None:
    execute([require_tool("sleep"), "2"])


def generation_is_new(before: Generation, after: Generation) -> bool:
    return (
        after.pid != before.pid
        and after.invocation != before.invocation
        and after.started > before.started
    )


@dataclass
class RuntimeAttestation:
    generation: Generation
    boot: str
    proc_start: int
    descriptor: int
    running_link: str
    executable: ExecutableIdentity


def attest_candidate(candidate: Path, expected: ExecutableIdentity) -> RuntimeAttestation:
    generation = query_generation()
    current_boot = boot_id()
    starttime = proc_starttime(generation.pid)
    replacement = (
        os.environ.get("LLM_GUARD_REBUILD_TEST_REPLACE_EXE_DURING_HASH", "")
        if test_only
        else ""
    )
    descriptor, running_link, executable = open_runtime_executable(
        generation.pid, replacement
    )
    if executable.sha256 != expected.sha256:
        os.close(descriptor)
        fail("running executable SHA-256 does not match candidate")
    if (executable.device, executable.inode) != (expected.device, expected.inode):
        os.close(descriptor)
        fail("running executable inode does not match candidate")
    if executable.build_id != expected.build_id:
        os.close(descriptor)
        fail("running executable build ID does not match candidate")
    if not service_link_matches(str(candidate)):
        os.close(descriptor)
        fail("service symlink does not name candidate")
    linked = os.stat(service_bin)
    if not same_object(linked, expected):
        os.close(descriptor)
        fail("service symlink inode does not match candidate")
    return RuntimeAttestation(
        generation,
        current_boot,
        starttime,
        descriptor,
        running_link,
        executable,
    )


def final_runtime_attestation(
    accepted: RuntimeAttestation, candidate: Path, expected: ExecutableIdentity
) -> None:
    if query_generation() != accepted.generation:
        fail("systemd generation changed during attestation")
    if boot_id() != accepted.boot or proc_starttime(accepted.generation.pid) != accepted.proc_start:
        fail("runtime generation changed during attestation")
    current_proc = os.stat(proc_root / str(accepted.generation.pid) / "exe")
    if not same_object(current_proc, accepted.executable):
        fail("current proc executable no longer names held inode")
    if os.readlink(proc_root / str(accepted.generation.pid) / "exe") != accepted.running_link:
        fail("current proc executable link changed during attestation")
    held = os.fstat(accepted.descriptor)
    held_fields = (
        held.st_dev,
        held.st_ino,
        held.st_size,
        held.st_mtime_ns,
        held.st_ctime_ns,
    )
    expected_fields = (
        accepted.executable.device,
        accepted.executable.inode,
        accepted.executable.size,
        accepted.executable.mtime_ns,
        accepted.executable.ctime_ns,
    )
    if held_fields != expected_fields:
        fail("held executable metadata changed during attestation")
    current_candidate = os.stat(candidate)
    if (
        current_candidate.st_dev,
        current_candidate.st_ino,
        current_candidate.st_size,
        current_candidate.st_mtime_ns,
        current_candidate.st_ctime_ns,
    ) != expected_fields:
        fail("candidate executable metadata changed during attestation")
    if not service_link_matches(str(candidate)) or not same_object(
        os.stat(service_bin), expected
    ):
        fail("service symlink changed during attestation")


def materialize_runtime_backup(prestate: Prestate) -> Path:
    backup = (
        cache_root
        / "rollback"
        / f"{prestate.executable.sha256}-{prestate.executable.build_id}"
        / "llm-guard-proxy"
    )
    if backup.exists():
        require_secure_regular(backup, executable=True)
        descriptor = os.open(backup, os.O_RDONLY | os.O_CLOEXEC)
        try:
            identity = fd_identity(descriptor)
        finally:
            os.close(descriptor)
        if (
            identity.sha256 != prestate.executable.sha256
            or identity.build_id != prestate.executable.build_id
        ):
            fail("prior runtime backup identity differs")
    else:
        atomic_copy_fd(prestate.descriptor, backup, 0o755)
    return backup


def attest_restored_runtime(prestate: Prestate, require_same_inode: bool) -> None:
    generation = query_generation()
    if boot_id() != prestate.boot:
        fail("rollback crossed boot generation")
    starttime = proc_starttime(generation.pid)
    if starttime <= 0:
        fail("rollback proc generation is invalid")
    descriptor, _, identity = open_runtime_executable(generation.pid)
    try:
        if (
            identity.sha256 != prestate.executable.sha256
            or identity.build_id != prestate.executable.build_id
        ):
            fail("rollback runtime binary differs from prior Guard")
        if require_same_inode and (
            identity.device,
            identity.inode,
        ) != (prestate.executable.device, prestate.executable.inode):
            fail("rollback runtime inode differs from prior Guard")
        current = os.stat(proc_root / str(generation.pid) / "exe")
        if not same_object(current, identity):
            fail("rollback proc entry does not name held executable")
    finally:
        os.close(descriptor)


def rollback(prestate: Prestate, restart_attempted: bool, backup: Path | None) -> None:
    log("ROLLBACK_BEGIN=1")
    if prestate.link_target is None:
        remove_service_link()
    else:
        set_service_link(prestate.link_target)
    if restart_attempted:
        if prestate.link_target is None:
            if backup is None:
                fail("rollback lacks prior runtime backup")
            set_service_link(str(backup))
        restart_guard()
        sleep_after_restart()
        health_check()
        attest_restored_runtime(prestate, prestate.link_target is not None)
        if prestate.link_target is None:
            remove_service_link()
    else:
        assert_prestate_unchanged(prestate)
    if not service_link_matches(prestate.link_target):
        fail("rollback did not restore exact prior service link state")
    log("ROLLBACK_RESTORED=1")


def publish_receipt(payload: dict[str, object]) -> tuple[Path, str]:
    ensure_private_directory(receipt_dir)
    receipt_bytes = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("ascii")
    digest = sha256_bytes(receipt_bytes)
    final = receipt_dir / f"rebuild-{payload['receipt_id']}.receipt.json"
    temporary = receipt_dir / f".{final.name}.tmp.{os.getpid()}"
    failure = (
        os.environ.get("LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE", "")
        if test_only
        else ""
    )
    descriptor: int | None = None
    renamed = False
    try:
        if failure == "create":
            raise OSError(errno.EIO, "injected receipt create failure")
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if failure == "enospc":
            raise OSError(errno.ENOSPC, "injected receipt ENOSPC")
        written = 0
        while written < len(receipt_bytes):
            if failure == "write":
                raise OSError(errno.EIO, "injected receipt write failure")
            written += os.write(descriptor, receipt_bytes[written:])
        os.fchmod(descriptor, 0o600)
        if failure == "fsync":
            raise OSError(errno.EIO, "injected receipt fsync failure")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if failure == "rename":
            raise OSError(errno.EIO, "injected receipt rename failure")
        os.replace(temporary, final)
        renamed = True
        directory = os.open(
            receipt_dir,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if failure == "dir-fsync":
                raise OSError(errno.EIO, "injected receipt directory fsync failure")
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException as error:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if renamed:
            final.unlink(missing_ok=True)
            try:
                fsync_directory(receipt_dir)
            except OSError:
                pass
        raise RebuildError(f"durable receipt publication failed: {error}") from error
    return final, digest


def signal_handler(signum: int, _frame: object) -> None:
    raise RebuildError(f"received signal {signum}")


for caught_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(caught_signal, signal_handler)


snapshot_root: Path | None = None
prestate: Prestate | None = None
accepted: RuntimeAttestation | None = None
rollback_armed = False
restart_attempted = False
runtime_backup: Path | None = None
published_receipt: Path | None = None
try:
    for tool in ("cargo", "git", "ionice", "nice", "readelf", "rustc", "systemctl", "tar"):
        require_tool(tool)
    require_secure_regular(guard_config)
    require_secure_regular(guard_unit)
    config_sha_initial = sha256_file(guard_config)
    unit_sha_initial = sha256_file(guard_unit)
    cargo_identity_initial = capture_tool("cargo", "--version", "--verbose")
    rustc_identity_initial = capture_tool("rustc", "-vV")
    cargo_identity_sha = sha256_bytes(cargo_identity_initial)
    rustc_identity_sha = sha256_bytes(rustc_identity_initial)

    prestate = snapshot_prestate()

    ensure_private_directory(cache_root)
    ensure_private_directory(cargo_target)
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    if not (source_dir / ".git").is_dir():
        if source_dir.exists():
            fail("source path exists without a Git checkout")
        execute(
            [
                require_tool("git"),
                "clone",
                "--filter=blob:none",
                source_repo,
                str(source_dir),
            ]
        )
    execute(
        [
            require_tool("git"),
            "-C",
            str(source_dir),
            "fetch",
            "--prune",
            "origin",
            source_branch,
        ]
    )
    execute(
        [
            require_tool("git"),
            "-C",
            str(source_dir),
            "checkout",
            "--detach",
            f"origin/{source_branch}",
        ]
    )
    source_commit, source_tree = clean_source_identity()
    paths = tracked_paths()

    snapshot_root = Path(tempfile.mkdtemp(prefix=".rebuild-snapshot-", dir=cache_root))
    snapshot_root.chmod(0o700)
    archive = snapshot_root / "source.tar"
    snapshot = snapshot_root / "source"
    inventory = snapshot_root / "tracked-paths"
    inventory.write_bytes(b"\0".join(paths) + b"\0")
    inventory.chmod(0o600)
    snapshot.mkdir(mode=0o700)
    execute(
        [
            require_tool("git"),
            "-C",
            str(source_dir),
            "archive",
            "--format=tar",
            f"--output={archive}",
            source_commit,
        ]
    )
    require_secure_regular(archive)
    with archive.open("rb") as archive_input:
        archived_commit = execute(
            [require_tool("git"), "get-tar-commit-id"],
            capture=True,
            stdin=archive_input,
        ).decode("ascii").strip()
    if archived_commit != source_commit:
        fail("source archive commit identity differs")
    source_archive_sha = sha256_file(archive)
    execute([require_tool("tar"), "-xf", str(archive), "-C", str(snapshot)])
    source_content_sha = path_ledger(source_dir, paths, metadata=False)
    snapshot_content_sha = path_ledger(snapshot, paths, metadata=False)
    if source_content_sha != snapshot_content_sha:
        fail("source snapshot content differs from committed checkout")
    make_snapshot_read_only(snapshot)
    source_metadata_before = path_ledger(source_dir, paths, metadata=True)
    snapshot_metadata_before = path_ledger(snapshot, paths, metadata=True)

    manifest = snapshot / "Cargo.toml"
    if not manifest.is_file():
        fail("source snapshot Cargo manifest is missing")
    execute(
        [
            require_tool("nice"),
            "-n",
            "10",
            require_tool("ionice"),
            "-c3",
            require_tool("cargo"),
            "build",
            "--release",
            "-p",
            "llm-guard-proxy",
            "--features",
            "guard",
            "--manifest-path",
            str(manifest),
        ]
    )
    if path_ledger(source_dir, paths, metadata=True) != source_metadata_before:
        fail("source checkout metadata changed during build")
    if path_ledger(snapshot, paths, metadata=True) != snapshot_metadata_before:
        fail("immutable source snapshot changed during build")
    if clean_source_identity() != (source_commit, source_tree):
        fail("source checkout identity changed during build")

    raw_binary = cargo_target / "release/llm-guard-proxy"
    require_secure_regular(raw_binary, executable=True)
    raw_sha = sha256_file(raw_binary)
    raw_build_id = elf_build_id(str(raw_binary))
    candidate = (
        cache_root
        / "releases"
        / f"{source_commit}-{raw_sha}"
        / "llm-guard-proxy"
    )
    if candidate.exists():
        require_secure_regular(candidate, executable=True)
        if sha256_file(candidate) != raw_sha or elf_build_id(str(candidate)) != raw_build_id:
            fail("existing content-addressed candidate identity differs")
    else:
        atomic_copy(raw_binary, candidate, 0o755)
    candidate_fd = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC)
    try:
        candidate_identity = fd_identity(candidate_fd)
    finally:
        os.close(candidate_fd)
    if candidate_identity.sha256 != raw_sha or candidate_identity.build_id != raw_build_id:
        fail("content-addressed candidate differs from build output")

    assert_prestate_unchanged(prestate)
    if config_sha_initial != sha256_file(guard_config) or unit_sha_initial != sha256_file(guard_unit):
        fail("installed Guard config or unit changed before cutover")
    if cargo_identity_initial != capture_tool("cargo", "--version", "--verbose"):
        fail("Cargo toolchain identity changed before cutover")
    if rustc_identity_initial != capture_tool("rustc", "-vV"):
        fail("rustc toolchain identity changed before cutover")

    rollback_armed = True
    if not service_link_matches(str(candidate)):
        set_service_link(str(candidate))
    needs_restart = (
        prestate.executable.device,
        prestate.executable.inode,
    ) != (candidate_identity.device, candidate_identity.inode)
    if needs_restart:
        require_tool("curl")
        require_tool("sleep")
        if prestate.link_target is None:
            runtime_backup = materialize_runtime_backup(prestate)
        restart_attempted = True
        restart_guard()
        sleep_after_restart()
        restarted_generation = query_generation()
        restarted_start = proc_starttime(restarted_generation.pid)
        if (
            not generation_is_new(prestate.generation, restarted_generation)
            or restarted_start == prestate.proc_start
            or boot_id() != prestate.boot
        ):
            fail("proxy restart did not create a new runtime generation")
        health_check()
    accepted = attest_candidate(candidate, candidate_identity)
    if needs_restart and (
        accepted.generation != restarted_generation
        or accepted.proc_start != restarted_start
        or accepted.boot != prestate.boot
    ):
        fail("systemd generation changed during health check")
    if needs_restart and not generation_is_new(
        prestate.generation, accepted.generation
    ):
        fail("accepted candidate lacks a new systemd generation")
    if not needs_restart and (
        accepted.generation != prestate.generation
        or accepted.proc_start != prestate.proc_start
        or accepted.boot != prestate.boot
    ):
        fail("unchanged candidate runtime generation drifted")

    if path_ledger(source_dir, paths, metadata=True) != source_metadata_before:
        fail("source checkout metadata changed before publication")
    if path_ledger(snapshot, paths, metadata=True) != snapshot_metadata_before:
        fail("immutable source snapshot changed before publication")
    if clean_source_identity() != (source_commit, source_tree):
        fail("source checkout identity changed before publication")
    if config_sha_initial != sha256_file(guard_config) or unit_sha_initial != sha256_file(guard_unit):
        fail("installed Guard config or unit changed before publication")
    if cargo_identity_initial != capture_tool("cargo", "--version", "--verbose"):
        fail("Cargo toolchain identity changed before publication")
    if rustc_identity_initial != capture_tool("rustc", "-vV"):
        fail("rustc toolchain identity changed before publication")
    final_runtime_attestation(accepted, candidate, candidate_identity)
    cleanup_snapshot(snapshot_root)
    snapshot_root = None

    receipt_id = secrets.token_hex(16)
    receipt_payload: dict[str, object] = {
        "schema": 1,
        "mode": "test-only" if test_only else "production",
        "receipt_id": receipt_id,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "source_archive_sha256": source_archive_sha,
        "snapshot_content_sha256": snapshot_content_sha,
        "cargo_identity_sha256": cargo_identity_sha,
        "rustc_identity_sha256": rustc_identity_sha,
        "guard_config_sha256": config_sha_initial,
        "guard_unit_sha256": unit_sha_initial,
        "binary_sha256": candidate_identity.sha256,
        "elf_build_id": candidate_identity.build_id,
        "binary_device": candidate_identity.device,
        "binary_inode": candidate_identity.inode,
        "boot_id": accepted.boot,
        "main_pid": accepted.generation.pid,
        "invocation_id": accepted.generation.invocation,
        "systemd_start_monotonic": accepted.generation.started,
        "proc_starttime": accepted.proc_start,
    }
    published_receipt, receipt_digest = publish_receipt(receipt_payload)
    if config_sha_initial != sha256_file(guard_config) or unit_sha_initial != sha256_file(guard_unit):
        fail("installed Guard config or unit changed at publication")
    if cargo_identity_initial != capture_tool("cargo", "--version", "--verbose"):
        fail("Cargo toolchain identity changed at publication")
    if rustc_identity_initial != capture_tool("rustc", "-vV"):
        fail("rustc toolchain identity changed at publication")
    final_runtime_attestation(accepted, candidate, candidate_identity)
    if (
        test_only
        and os.environ.get("LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE")
        == "completion-sink"
    ):
        os.close(sys.stdout.fileno())
    marker = TEST_COMPLETE if test_only else PRODUCTION_COMPLETE
    print(
        f"{marker} receipt_sha256={receipt_digest} receipt={published_receipt}",
        flush=True,
    )
    rollback_armed = False
except Exception as error:  # noqa: BLE001 - every late failure must roll back.
    rollback_logging = True
    rollback_error: Exception | None = None
    if rollback_armed and prestate is not None:
        previous_handlers = {
            signum: signal.signal(signum, signal.SIG_IGN)
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        }
        receipt_cleanup_error: Exception | None = None
        if published_receipt is not None:
            try:
                published_receipt.unlink(missing_ok=True)
                fsync_directory(published_receipt.parent)
            except Exception as failure:  # noqa: BLE001 - still attempt rollback.
                receipt_cleanup_error = failure
        try:
            rollback(prestate, restart_attempted, runtime_backup)
        except Exception as failure:  # noqa: BLE001 - report failed rollback.
            rollback_error = failure
        if receipt_cleanup_error is not None:
            if rollback_error is None:
                rollback_error = receipt_cleanup_error
            else:
                rollback_error = RebuildError(
                    f"receipt cleanup failed: {receipt_cleanup_error}; "
                    f"runtime rollback failed: {rollback_error}"
                )
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    print(f"ERROR: {error}", file=sys.stderr, flush=True)
    if rollback_error is not None:
        print(f"ERROR: rollback failed: {rollback_error}", file=sys.stderr, flush=True)
    raise SystemExit(1)
finally:
    if accepted is not None:
        os.close(accepted.descriptor)
    if prestate is not None:
        os.close(prestate.descriptor)
    cleanup_snapshot(snapshot_root)
PY
