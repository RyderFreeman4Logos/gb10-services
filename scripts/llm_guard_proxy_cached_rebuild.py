from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast

__all__: list[str] = []

EXPECTED_BOUNDED_PROCESS_SHA256 = "4a7e3cb50fe46d9e8c02728ff9b100553472abcdb6b055b081290218649bb205"
_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
_BOUNDED_PROCESS_PATH = _SCRIPT_DIRECTORY / "gb10_bounded_process.py"
_bounded_fd = os.open(
    _BOUNDED_PROCESS_PATH,
    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
)
try:
    _bounded_metadata = os.fstat(_bounded_fd)
    _bounded_digest = hashlib.sha256()
    while _bounded_chunk := os.read(_bounded_fd, 65536):
        _bounded_digest.update(_bounded_chunk)
finally:
    os.close(_bounded_fd)
if (
    not stat.S_ISREG(_bounded_metadata.st_mode)
    or _bounded_metadata.st_uid != os.geteuid()
    or _bounded_metadata.st_nlink != 1
    or _bounded_metadata.st_mode & 0o022
    or _bounded_digest.hexdigest() != EXPECTED_BOUNDED_PROCESS_SHA256
):
    raise RuntimeError("Guard bounded-process import authority differs")
sys.path.insert(0, str(_SCRIPT_DIRECTORY))
run_bounded = importlib.import_module("gb10_bounded_process").command

UNIT = "llm-guard-proxy.service"
HEALTH_URL = "http://100.105.4.92:18009/health"
PRODUCTION_COMPLETE = "LLM_GUARD_REBUILD_PRODUCTION_COMPLETE"
TEST_COMPLETE = "LLM_GUARD_REBUILD_TEST_ONLY_COMPLETE"
MAX_EXECUTABLE_BYTES = 128 * 1024 * 1024
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


TOOL_NAMES = {
    "ar",
    "bwrap",
    "cargo",
    "cc",
    "curl",
    "git",
    "git_remote_https",
    "ionice",
    "ld",
    "nice",
    "readelf",
    "rustc",
    "systemctl",
}


@dataclass(frozen=True)
class ToolSpec:
    logical: str
    resolved: str
    uid: int
    gid: int
    mode: int
    sha256: str

    @classmethod
    def from_json(cls, name: str, payload: object) -> ToolSpec:
        keys = {"logical", "resolved", "uid", "gid", "mode", "sha256"}
        if not isinstance(payload, dict) or set(payload) != keys:
            fail(f"test-only tool authority is malformed: {name}")
        values = cast(dict[str, object], payload)
        if (
            not all(isinstance(values[key], str) for key in ("logical", "resolved", "sha256"))
            or not all(
                isinstance(values[key], int) and not isinstance(values[key], bool)
                for key in ("uid", "gid", "mode")
            )
        ):
            fail(f"test-only tool numeric authority is malformed: {name}")
        spec = cls(
            logical=cast(str, values["logical"]),
            resolved=cast(str, values["resolved"]),
            uid=cast(int, values["uid"]),
            gid=cast(int, values["gid"]),
            mode=cast(int, values["mode"]),
            sha256=cast(str, values["sha256"]),
        )
        if (
            not all(isinstance(value, str) for value in (spec.logical, spec.resolved, spec.sha256))
            or not all(isinstance(value, int) and value >= 0 for value in (spec.uid, spec.gid, spec.mode))
            or not Path(spec.logical).is_absolute()
            or not Path(spec.resolved).is_absolute()
            or not re.fullmatch(r"[0-9a-f]{64}", spec.sha256)
        ):
            fail(f"test-only tool authority fields are invalid: {name}")
        return spec


@dataclass(frozen=True)
class ToolObjectIdentity:
    device: int
    inode: int
    size: int
    nlink: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass
class HeldTool:
    name: str
    spec: ToolSpec
    descriptor: int
    identity: ToolObjectIdentity

    @property
    def exec_path(self) -> str:
        return f"/proc/self/fd/{self.descriptor}"

    def verify(self) -> None:
        held = os.fstat(self.descriptor)
        try:
            current = os.stat(self.spec.logical, follow_symlinks=False)
        except OSError as error:
            raise RebuildError(
                f"held tool pathname or metadata changed: {self.name}"
            ) from error
        fields = (
            held.st_dev,
            held.st_ino,
            held.st_size,
            held.st_nlink,
            held.st_mtime_ns,
            held.st_ctime_ns,
        )
        expected = (
            self.identity.device,
            self.identity.inode,
            self.identity.size,
            self.identity.nlink,
            self.identity.mtime_ns,
            self.identity.ctime_ns,
        )
        current_fields = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_nlink,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        if fields != expected or current_fields != expected:
            fail(f"held tool pathname or metadata changed: {self.name}")
        if _sha256_fd(self.descriptor) != self.identity.sha256:
            fail(f"held tool bytes changed: {self.name}")

    def receipt(self) -> dict[str, object]:
        return {
            "logical_path": self.spec.logical,
            "resolved_path": self.spec.resolved,
            "expected_uid": self.spec.uid,
            "expected_gid": self.spec.gid,
            "expected_mode": self.spec.mode,
            "device": self.identity.device,
            "inode": self.identity.inode,
            "size": self.identity.size,
            "nlink": self.identity.nlink,
            "mtime_ns": self.identity.mtime_ns,
            "ctime_ns": self.identity.ctime_ns,
            "sha256": self.identity.sha256,
        }

    def close(self) -> None:
        os.close(self.descriptor)


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        offset += len(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def _runtime_python_authority() -> dict[str, object]:
    descriptor = os.open("/proc/self/exe", os.O_RDONLY | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        digest = _sha256_fd(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o002:
        fail("Python runtime object authority is unsafe")
    if not test_only and (
        info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o755
        or info.st_nlink != 1
        or digest
        != "6d972cf21be56fe3c947ab6ba257ff8d08c342dd2714442986791bd9a6dfabfe"
    ):
        fail("Python runtime object authority differs")
    return {
        "logical_path": "/usr/bin/python3" if not test_only else sys.executable,
        "resolved_path": (
            "/usr/bin/python3.11"
            if not test_only
            else os.readlink("/proc/self/exe")
        ),
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "nlink": info.st_nlink,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "sha256": digest,
    }


def _reject_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            fail(f"tool authority has unsafe ancestor: {path}")


def _open_tool(name: str, spec: ToolSpec) -> HeldTool:
    logical = Path(spec.logical)
    _reject_symlink_ancestors(logical)
    try:
        resolved = logical.resolve(strict=True)
    except OSError as error:
        raise RebuildError(f"required tool unavailable: {name}") from error
    if str(resolved) != spec.resolved:
        fail(f"tool resolved path differs: {name}")
    descriptor = os.open(
        logical,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != spec.uid
            or metadata.st_gid != spec.gid
            or stat.S_IMODE(metadata.st_mode) != spec.mode
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o002
            or metadata.st_size <= 0
            or metadata.st_size > 128 * 1024 * 1024
        ):
            fail(f"tool object metadata differs: {name}")
        digest = _sha256_fd(descriptor)
        if digest != spec.sha256:
            fail(f"tool object SHA-256 differs: {name}")
        identity = ToolObjectIdentity(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_nlink,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            digest,
        )
        held = HeldTool(name, spec, descriptor, identity)
        held.verify()
        return held
    except BaseException:
        os.close(descriptor)
        raise


def _production_tool_specs() -> dict[str, ToolSpec]:
    def root(path: str, digest: str, *, gid: int = 0, mode: int = 0o755) -> ToolSpec:
        return ToolSpec(path, path, 0, gid, mode, digest)

    toolchain = (
        "/usr/local/share/mise/installs/rust/stable/toolchains/"
        "stable-x86_64-unknown-linux-gnu"
    )
    return {
        "git": root("/usr/bin/git", "2540879925a6881e3877ff7e3330746ba3027b04edf16a3a12dccd1644c4f32d"),
        "git_remote_https": root(
            "/usr/lib/git-core/git-remote-http",
            "4d3b7807ab261652ae6ae4340e5331c8ce2c6d27a58bbed69e90c175e436adc3",
        ),
        "bwrap": root(
            "/usr/bin/bwrap",
            "85580dd52ed366ece8844e90fa75ac7c4de8802963071344e123221fb9f6f11e",
        ),
        "systemctl": root(
            "/usr/bin/systemctl",
            "93d45f7967f1ae04409dccb3e0730dcc84fe6aecbe33aae5d5df158f4de0012c",
        ),
        "curl": root(
            "/usr/bin/curl",
            "27125f0331490b7fbf4da11f2bd913ce1b94e071367b2fa8e535ce8c5526e29c",
        ),
        "readelf": root(
            "/usr/bin/x86_64-linux-gnu-readelf",
            "afa25ff2dc25a71b79e853b9e3a9abb7b4e8c83efac17c0a637cbbb687442a4f",
        ),
        "nice": root(
            "/usr/bin/nice",
            "144ba2794c120a0347058d48081c9c13c2afe4321f1b44318538616295273060",
        ),
        "ionice": root(
            "/usr/bin/ionice",
            "02ccf10cc32df4c1a13bb1a7f4406a9752c3216c13e02527b58109c79b48f516",
        ),
        "cargo": ToolSpec(
            f"{toolchain}/bin/cargo",
            f"{toolchain}/bin/cargo",
            1001,
            1001,
            0o755,
            "828980723df339d62434390e9fb8ef8831036583343ae2316b7ab5646b5c1953",
        ),
        "rustc": ToolSpec(
            f"{toolchain}/bin/rustc",
            f"{toolchain}/bin/rustc",
            1001,
            1001,
            0o755,
            "d3a664c970a9fd8361b64194861bebc1ae37b9054e5ee3400dc1c9e691797eea",
        ),
        "cc": root(
            "/usr/bin/x86_64-linux-gnu-gcc-12",
            "75e997ec62297a6484f491bae28ab0ccb489daba23e398fd10fe68e9e6f0def8",
        ),
        "ld": root(
            "/usr/bin/x86_64-linux-gnu-ld.bfd",
            "f6d71a1bcd45764550a42dfaa179bc43b63ee879ec6f875bfd39fca013515da7",
        ),
        "ar": root(
            "/usr/bin/x86_64-linux-gnu-ar",
            "3acbee2794e3668a74bcb90f2eaf7d981211fb95288ab940f0e3ac380e8f6023",
        ),
    }


def _reject_duplicate_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate test-only config key: {key}")
        result[key] = value
    return result


def _load_test_authority_config(path: Path) -> dict[str, Any]:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
            or metadata.st_size > 1024 * 1024
        ):
            fail("unsafe test-only rebuild authority config")
        payload = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    try:
        parsed = json.loads(payload, object_pairs_hook=_reject_duplicate_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RebuildError("malformed test-only rebuild authority config") from error
    keys = {
        "schema",
        "registry_cache",
        "registry_index",
        "test_env",
        "toolchain_root",
        "tools",
    }
    if not isinstance(parsed, dict) or set(parsed) != keys or parsed["schema"] != 1:
        fail("test-only rebuild authority config has wrong schema")
    if not isinstance(parsed["tools"], dict) or set(parsed["tools"]) != TOOL_NAMES:
        fail("test-only tool authority set differs")
    parsed["tools"] = {
        name: ToolSpec.from_json(name, value) for name, value in parsed["tools"].items()
    }
    for key in ("registry_cache", "registry_index", "toolchain_root"):
        if not isinstance(parsed[key], str) or not Path(parsed[key]).is_absolute():
            fail(f"test-only authority path is invalid: {key}")
    if not isinstance(parsed["test_env"], dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not re.fullmatch(r"[A-Z0-9_]+", key)
        or "\x00" in value
        for key, value in parsed["test_env"].items()
    ):
        fail("test-only child environment is invalid")
    return parsed


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
    source_repo = "https://github.com/NousResearch/llm-guard-proxy.git"
    source_branch = "main"
    source_ref = "refs/heads/main"
    fetch_protocol = "https"

    service_bin = home / ".local/bin/llm-guard-proxy"
    cache_root = home / ".cache/cargo-target/llm-guard-proxy-main"
    guard_config = home / ".config/llm-guard-proxy/config.toml"
    guard_unit = home / ".config/systemd/user/llm-guard-proxy.service"
    proc_root = Path("/proc")
    receipt_dir = home / ".local/state/llm-guard-proxy-rebuild"
    toolchain_root = Path(
        "/usr/local/share/mise/installs/rust/stable/toolchains/"
        "stable-x86_64-unknown-linux-gnu"
    )
    registry_cache = Path(
        "/home/obj/.cargo/registry/cache/index.crates.io-1949cf8c6b5b557f"
    )
    registry_index = Path(
        "/home/obj/.cargo/registry/index/index.crates.io-1949cf8c6b5b557f"
    )
    tool_specs = _production_tool_specs()
    child_env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
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
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", source_branch):
        fail("test-only source branch is malformed")
    source_ref = f"refs/heads/{source_branch}"
    fetch_protocol = "file"

    service_bin = Path(required_env("SERVICE_BIN"))
    cache_root = Path(required_env("CACHE_ROOT"))
    guard_config = Path(required_env("LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG"))
    guard_unit = Path(required_env("LLM_GUARD_PROXY_REBUILD_GUARD_UNIT"))
    proc_root = Path(required_env("LLM_GUARD_PROXY_REBUILD_PROC_ROOT"))
    receipt_dir = Path(required_env("LLM_GUARD_PROXY_REBUILD_RECEIPT_DIR"))
    authority_config = _load_test_authority_config(
        Path(required_env("LLM_GUARD_REBUILD_TEST_CONFIG"))
    )
    toolchain_root = Path(authority_config["toolchain_root"])
    registry_cache = Path(authority_config["registry_cache"])
    registry_index = Path(authority_config["registry_index"])
    tool_specs = authority_config["tools"]
    child_env = dict(authority_config["test_env"])
    child_env.update(
        {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )

missing_test_tool = (
    os.environ.get("LLM_GUARD_REBUILD_TEST_MISSING_TOOL", "") if test_only else ""
)
held_tools: dict[str, HeldTool] = {}


def _open_all_tools() -> None:
    opened: list[HeldTool] = []
    try:
        for name in sorted(TOOL_NAMES):
            if name == missing_test_tool:
                fail(f"required tool unavailable: {name}")
            held = _open_tool(name, tool_specs[name])
            opened.append(held)
            held_tools[name] = held
    except BaseException:
        for held in reversed(opened):
            held.close()
        held_tools.clear()
        raise


def require_tool(name: str) -> str:
    if name == missing_test_tool or name not in held_tools:
        fail(f"required tool unavailable: {name}")
    return held_tools[name].exec_path


def _tool_fds() -> tuple[int, ...]:
    return tuple(tool.descriptor for tool in held_tools.values())


def _verify_all_tools() -> None:
    for tool in held_tools.values():
        tool.verify()


def execute(
    command: list[str],
    *,
    capture: bool = False,
    pass_fds: tuple[int, ...] = (),
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> bytes:
    log("+ " + shlex.join(command))
    used = [tool for tool in held_tools.values() if tool.exec_path in command]
    effective_fds = tuple(sorted(set(pass_fds + _tool_fds())))
    try:
        try:
            result = subprocess.run(
                command,
                check=True,
                env=child_env if env is None else env,
                cwd=cwd,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                pass_fds=effective_fds,
            )
        except subprocess.CalledProcessError as error:
            fail(f"command failed ({error.returncode}): {Path(command[0]).name}")
        output = result.stdout or b""
        error_output = result.stderr or b""
        if len(output) > 4 * 1024 * 1024 or len(error_output) > 4 * 1024 * 1024:
            fail(f"command output exceeded bound: {Path(command[0]).name}")
        return output
    finally:
        for tool in used:
            tool.verify()


def capture_tool(name: str, *arguments: str) -> bytes:
    return execute([require_tool(name), *arguments], capture=True)


def _git_environment(exec_directory_fd: int | None = None) -> dict[str, str]:
    environment = {
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_COUNT": "0",
        "GIT_ALLOW_PROTOCOL": fetch_protocol,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_NO_LAZY_FETCH": "1",
    }
    if exec_directory_fd is not None:
        environment["GIT_EXEC_PATH"] = f"/proc/self/fd/{exec_directory_fd}"
    return environment


def _git_options() -> list[str]:
    options = [
        "-c",
        "include.path=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.allow=never",
        "-c",
        f"protocol.{fetch_protocol}.allow=always",
        "-c",
        "fetch.fsckObjects=true",
        "-c",
        "transfer.fsckObjects=true",
        "-c",
        "receive.fsckObjects=true",
        "-c",
        "fetch.writeCommitGraph=false",
    ]
    return options


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: int
    oid: str
    size: int


@dataclass(frozen=True)
class DirectoryLedger:
    content_sha256: str
    metadata_sha256: str
    files: int
    bytes: int


@dataclass
class DirectoryAuthority:
    name: str
    path: Path
    descriptor: int
    metadata: tuple[int, int, int, int, int]
    ledger: DirectoryLedger

    def verify(self) -> None:
        held = os.fstat(self.descriptor)
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except OSError as error:
            raise RebuildError(f"directory authority changed: {self.name}") from error
        fields = (
            held.st_dev,
            held.st_ino,
            held.st_mode,
            held.st_mtime_ns,
            held.st_ctime_ns,
        )
        current_fields = (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        if fields != self.metadata or current_fields != self.metadata:
            fail(f"directory authority changed: {self.name}")
        if _directory_ledger(self.descriptor, self.name) != self.ledger:
            fail(f"directory authority ledger changed: {self.name}")

    def receipt(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "device": self.metadata[0],
            "inode": self.metadata[1],
            "mode": stat.S_IMODE(self.metadata[2]),
            "mtime_ns": self.metadata[3],
            "ctime_ns": self.metadata[4],
            "content_sha256": self.ledger.content_sha256,
            "metadata_sha256": self.ledger.metadata_sha256,
            "file_count": self.ledger.files,
            "byte_count": self.ledger.bytes,
        }

    def close(self) -> None:
        os.close(self.descriptor)


def _directory_ledger(root_fd: int, label: str) -> DirectoryLedger:
    content = hashlib.sha256()
    metadata_digest = hashlib.sha256()
    file_count = 0
    byte_count = 0

    def visit(directory_fd: int, prefix: str) -> None:
        nonlocal file_count, byte_count
        names = sorted(os.listdir(directory_fd))
        if len(names) > 100_000:
            fail(f"directory entry bound exceeded: {label}")
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                fail(f"unsafe directory entry: {label}")
            try:
                name.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise RebuildError(f"non-UTF-8 directory entry: {label}") from error
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            metadata_digest.update(
                f"{relative}\0{info.st_dev}\0{info.st_ino}\0{info.st_mode}\0"
                f"{info.st_size}\0{info.st_mtime_ns}\0{info.st_ctime_ns}\n".encode()
            )
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
                continue
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=directory_fd)
                parts = PurePosixPath(target).parts
                if target.startswith("/") or ".." in parts:
                    fail(f"unsafe symlink in directory authority: {label}")
                content.update(f"L\0{relative}\0{mode:o}\0{target}\n".encode())
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                fail(f"unsupported object in directory authority: {label}")
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                before = os.fstat(descriptor)
                digest = _sha256_fd(descriptor)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                fail(f"file changed during directory ledger: {label}")
            file_count += 1
            byte_count += info.st_size
            if file_count > 100_000 or byte_count > 8 * 1024 * 1024 * 1024:
                fail(f"directory authority bound exceeded: {label}")
            content.update(f"F\0{relative}\0{mode:o}\0{info.st_size}\0{digest}\n".encode())

    fresh_root = os.open(
        ".",
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    try:
        visit(fresh_root, "")
    finally:
        os.close(fresh_root)
    return DirectoryLedger(
        content.hexdigest(), metadata_digest.hexdigest(), file_count, byte_count
    )


def _open_directory_authority(name: str, path: Path) -> DirectoryAuthority:
    if not path.is_absolute():
        fail(f"directory authority is not absolute: {name}")
    _reject_symlink_ancestors(path)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002:
            fail(f"unsafe directory authority: {name}")
        metadata = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        authority = DirectoryAuthority(
            name, path, descriptor, metadata, _directory_ledger(descriptor, name)
        )
        authority.verify()
        return authority
    except BaseException:
        os.close(descriptor)
        raise


def _read_fd_limited(descriptor: int, limit: int, label: str) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    total = 0
    while True:
        chunk = os.pread(descriptor, min(1024 * 1024, limit + 1 - total), offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)
        total += len(chunk)
        if total > limit:
            fail(f"{label} exceeded byte bound")


def _validate_raw_git_config(repo_fd: int) -> tuple[int, tuple[int, ...], str]:
    config_fd = os.open(
        "config",
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=repo_fd,
    )
    info = os.fstat(config_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
        os.close(config_fd)
        fail("private Git config metadata is unsafe")
    payload = _read_fd_limited(config_fd, 64 * 1024, "private Git config")
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        os.close(config_fd)
        raise RebuildError("private Git config is not UTF-8") from error
    section = ""
    values: dict[tuple[str, str], str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section != "core":
                os.close(config_fd)
                fail("private Git config section differs")
            continue
        if "=" not in line or section != "core":
            os.close(config_fd)
            fail("private Git config syntax differs")
        key, value = (part.strip().lower() for part in line.split("=", 1))
        pair = (section, key)
        if pair in values:
            os.close(config_fd)
            fail("private Git config has duplicate keys")
        values[pair] = value
    expected = {
        ("core", "repositoryformatversion"): "0",
        ("core", "filemode"): "true",
        ("core", "bare"): "true",
    }
    if values != expected:
        os.close(config_fd)
        fail("private Git config allowlist differs")
    fields = (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
    return config_fd, fields, sha256_bytes(payload)


def _verify_git_config(repo_fd: int, config_fd: int, fields: tuple[int, ...], digest: str) -> None:
    held = os.fstat(config_fd)
    current = os.stat("config", dir_fd=repo_fd, follow_symlinks=False)
    held_fields = (
        held.st_dev,
        held.st_ino,
        held.st_size,
        held.st_mtime_ns,
        held.st_ctime_ns,
    )
    current_fields = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    if held_fields != fields or current_fields != fields or _sha256_fd(config_fd) != digest:
        fail("private Git config changed")


def _parse_tree(payload: bytes) -> list[TreeEntry]:
    records = payload.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    if not records or len(records) > 8192:
        fail("canonical source file-count bound differs")
    entries: list[TreeEntry] = []
    total = 0
    seen: set[str] = set()
    for record in records:
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_text, kind, oid, size_text = header.split(b" ", 3)
            size_text = size_text.strip()
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as error:
            raise RebuildError("canonical Git tree record is malformed") from error
        if kind != b"blob" or mode_text not in {b"100644", b"100755"}:
            fail("canonical Git tree contains symlink, gitlink, or special entry")
        relative = PurePosixPath(path)
        if (
            not path
            or path.startswith("/")
            or ".." in relative.parts
            or "." in relative.parts
            or path in seen
            or not re.fullmatch(rb"[0-9a-f]{40}", oid)
            or not size_text.isdigit()
        ):
            fail("canonical Git tree path or identity is unsafe")
        seen.add(path)
        forbidden = path in {".gitmodules", ".gitattributes"} or path.endswith(
            ("/.gitmodules", "/.gitattributes", "/.cargo/config", "/.cargo/config.toml")
        ) or path in {".cargo/config", ".cargo/config.toml"}
        if forbidden:
            fail(f"canonical Git tree contains forbidden build control: {path}")
        size = int(size_text)
        total += size
        if size > 16 * 1024 * 1024 or total > 128 * 1024 * 1024:
            fail("canonical source byte bound exceeded")
        entries.append(TreeEntry(path, int(mode_text, 8), oid.decode(), size))
    if "Cargo.toml" not in seen or "Cargo.lock" not in seen:
        fail("canonical source lacks Cargo.toml or Cargo.lock")
    return entries


def _extract_archive(archive_path: Path, source_path: Path, commit: str, entries: list[TreeEntry]) -> str:
    expected = {entry.path: entry for entry in entries}
    seen: set[str] = set()
    content = hashlib.sha256()
    with tarfile.open(archive_path, mode="r:") as archive:
        if archive.pax_headers.get("comment") != commit:
            fail("canonical source archive commit identity differs")
        for member in archive:
            name = member.name.rstrip("/")
            if not name:
                continue
            if member.isdir():
                relative = PurePosixPath(name)
                if name.startswith("/") or ".." in relative.parts:
                    fail("source archive directory is unsafe")
                (source_path / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            if not member.isfile() or name not in expected or name in seen:
                fail("source archive object differs from exact tree")
            entry = expected[name]
            if member.size != entry.size:
                fail("source archive blob size differs")
            stream = archive.extractfile(member)
            if stream is None:
                fail("source archive blob is unreadable")
            data = stream.read(entry.size + 1)
            if len(data) != entry.size:
                fail("source archive blob read differs")
            git_oid = hashlib.sha1(
                f"blob {len(data)}\0".encode() + data, usedforsecurity=False
            ).hexdigest()
            if git_oid != entry.oid:
                fail("source archive blob identity differs")
            destination = source_path / PurePosixPath(name)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o500 if entry.mode == 0o100755 else 0o400,
            )
            try:
                written = 0
                while written < len(data):
                    written += os.write(descriptor, data[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            seen.add(name)
            content.update(
                f"{name}\0{entry.mode:o}\0{entry.oid}\0{sha256_bytes(data)}\n".encode()
            )
    if seen != set(expected):
        fail("source archive inventory differs from exact tree")
    for directory, directories, files in os.walk(source_path, topdown=False):
        for name in directories:
            os.chmod(Path(directory) / name, 0o500, follow_symlinks=False)
        for name in files:
            if str((Path(directory) / name).relative_to(source_path)) not in expected:
                fail("source extraction produced an extra file")
    source_path.chmod(0o500)
    return content.hexdigest()


@dataclass
class SourceBundle:
    root: Path
    source: Path
    source_authority: DirectoryAuthority
    repo_fd: int
    config_fd: int
    config_fields: tuple[int, ...]
    config_sha256: str
    commit: str
    tree: str
    archive_sha256: str
    tree_ledger_sha256: str
    file_count: int
    byte_count: int

    def verify(self) -> None:
        self.source_authority.verify()
        _verify_git_config(
            self.repo_fd, self.config_fd, self.config_fields, self.config_sha256
        )
        held_tools["git"].verify()
        held_tools["git_remote_https"].verify()

    def close(self) -> None:
        self.source_authority.close()
        os.close(self.config_fd)
        os.close(self.repo_fd)


def prepare_canonical_source(snapshot_root: Path) -> SourceBundle:
    template = snapshot_root / "empty-template"
    bare = snapshot_root / "objects.git"
    source = snapshot_root / "source"
    git_exec = snapshot_root / "git-exec"
    template.mkdir(mode=0o700)
    git_exec.mkdir(mode=0o700)
    source.mkdir(mode=0o700)
    execute(
        [
            require_tool("git"),
            *_git_options(),
            "init",
            "--bare",
            "--initial-branch=_unused",
            f"--template={template}",
            str(bare),
        ],
        env=_git_environment(),
    )
    repo_fd = os.open(
        bare,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    exec_fd = os.open(
        git_exec,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    config_fd = -1
    try:
        os.symlink(
            held_tools["git_remote_https"].exec_path,
            git_exec / "git-remote-https",
        )
        config_fd, config_fields, config_sha = _validate_raw_git_config(repo_fd)
        git_env = _git_environment(exec_fd)
        git_dir = f"--git-dir=/proc/self/fd/{repo_fd}"
        execute(
            [
                require_tool("git"),
                *_git_options(),
                git_dir,
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-filter",
                "--depth=1",
                "--refmap=",
                source_repo,
                f"+{source_ref}:refs/gb10/rebuild",
            ],
            env=git_env,
            pass_fds=(repo_fd, exec_fd),
        )
        held_tools["git_remote_https"].verify()
        _verify_git_config(repo_fd, config_fd, config_fields, config_sha)
        commit = execute(
            [require_tool("git"), *_git_options(), git_dir, "rev-parse", "refs/gb10/rebuild^{commit}"],
            capture=True,
            env=git_env,
            pass_fds=(repo_fd, exec_fd),
        ).decode("ascii").strip()
        tree = execute(
            [require_tool("git"), *_git_options(), git_dir, "rev-parse", f"{commit}^{{tree}}"],
            capture=True,
            env=git_env,
            pass_fds=(repo_fd, exec_fd),
        ).decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(
            r"[0-9a-f]{40}", tree
        ):
            fail("canonical Git commit or tree identity is malformed")
        execute(
            [
                require_tool("git"),
                *_git_options(),
                git_dir,
                "fsck",
                "--strict",
                "--full",
                "--no-dangling",
                commit,
            ],
            env=git_env,
            pass_fds=(repo_fd, exec_fd),
        )
        reachable = execute(
            [
                require_tool("git"),
                *_git_options(),
                git_dir,
                "rev-list",
                "--objects",
                "--missing=print",
                commit,
            ],
            capture=True,
            env=git_env,
            pass_fds=(repo_fd, exec_fd),
        )
        if any(line.startswith(b"?") for line in reachable.splitlines()):
            fail("canonical Git object graph is incomplete")
        tree_payload = execute(
            [
                require_tool("git"),
                *_git_options(),
                git_dir,
                "ls-tree",
                "-lrz",
                "--full-tree",
                commit,
            ],
            capture=True,
            env=git_env,
            pass_fds=(repo_fd, exec_fd),
        )
        entries = _parse_tree(tree_payload)
        archive_path = snapshot_root / "source.tar"
        execute(
            [
                require_tool("git"),
                *_git_options(),
                git_dir,
                "archive",
                "--format=tar",
                f"--output={archive_path}",
                commit,
            ],
            env=git_env,
            pass_fds=(repo_fd, exec_fd),
        )
        require_secure_regular(archive_path)
        if archive_path.stat().st_size > 160 * 1024 * 1024:
            fail("canonical source archive exceeded bound")
        archive_sha = sha256_file(archive_path)
        tree_ledger = _extract_archive(archive_path, source, commit, entries)
        source_authority = _open_directory_authority("canonical source", source)
        os.close(exec_fd)
        bundle = SourceBundle(
            snapshot_root,
            source,
            source_authority,
            repo_fd,
            config_fd,
            config_fields,
            config_sha,
            commit,
            tree,
            archive_sha,
            tree_ledger,
            len(entries),
            sum(entry.size for entry in entries),
        )
        bundle.verify()
        return bundle
    except BaseException:
        os.close(exec_fd)
        if config_fd >= 0:
            os.close(config_fd)
        os.close(repo_fd)
        raise


def _sandbox_command(
    source_fd: int,
    target_fd: int,
    toolchain_fd: int,
    cache_fd: int,
    index_fd: int,
    cargo_arguments: list[str],
) -> list[str]:
    cargo = "/toolchain/bin/cargo"
    rustc = "/toolchain/bin/rustc"
    cc = held_tools["cc"].exec_path
    ar = held_tools["ar"].exec_path
    return [
        require_tool("nice"),
        "-n",
        "10",
        require_tool("ionice"),
        "-c3",
        require_tool("bwrap"),
        "--unshare-user",
        "--unshare-all",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        "/usr",
        "/usr",
        "--tmpfs",
        "/usr/local",
        "--tmpfs",
        "/home",
        "--dir",
        "/etc",
        "--ro-bind",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--ro-bind",
        f"/proc/self/fd/{source_fd}",
        "/src",
        "--ro-bind",
        f"/proc/self/fd/{toolchain_fd}",
        "/toolchain",
        "--ro-bind",
        held_tools["cargo"].exec_path,
        cargo,
        "--ro-bind",
        held_tools["rustc"].exec_path,
        rustc,
        "--dir",
        "/cargo-home",
        "--dir",
        "/cargo-home/registry",
        "--ro-bind",
        f"/proc/self/fd/{cache_fd}",
        "/cargo-home/registry/cache",
        "--ro-bind",
        f"/proc/self/fd/{index_fd}",
        "/cargo-home/registry/index",
        "--bind",
        f"/proc/self/fd/{target_fd}",
        "/target",
        "--tmpfs",
        "/tmp",
        "--clearenv",
        "--setenv",
        "HOME",
        "/nonexistent",
        "--setenv",
        "PATH",
        "/toolchain/bin:/usr/bin:/bin",
        "--setenv",
        "LC_ALL",
        "C",
        "--setenv",
        "LANG",
        "C",
        "--setenv",
        "CARGO_HOME",
        "/cargo-home",
        "--setenv",
        "CARGO_TARGET_DIR",
        "/target",
        "--setenv",
        "CARGO_NET_OFFLINE",
        "true",
        "--setenv",
        "CARGO_INCREMENTAL",
        "0",
        "--setenv",
        "CARGO_BUILD_JOBS",
        "1",
        "--setenv",
        "RUSTC",
        rustc,
        "--setenv",
        "CC",
        cc,
        "--setenv",
        "AR",
        ar,
        "--setenv",
        "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER",
        cc,
        "--chdir",
        "/src",
        "--",
        cargo,
        *cargo_arguments,
    ]


def _run_sandbox(
    source: SourceBundle,
    target: DirectoryAuthority,
    toolchain: DirectoryAuthority,
    cache: DirectoryAuthority,
    index: DirectoryAuthority,
    arguments: list[str],
) -> str:
    pass_fds = tuple(
        sorted(
            set(
                _tool_fds()
                + (
                    source.source_authority.descriptor,
                    target.descriptor,
                    toolchain.descriptor,
                    cache.descriptor,
                    index.descriptor,
                )
            )
        )
    )
    command = _sandbox_command(
        source.source_authority.descriptor,
        target.descriptor,
        toolchain.descriptor,
        cache.descriptor,
        index.descriptor,
        arguments,
    )
    log("+ " + shlex.join(command))
    try:
        return run_bounded(
            command,
            cwd=Path("/"),
            env=child_env,
            pass_fds=pass_fds,
        )
    finally:
        for name in ("nice", "ionice", "bwrap", "cargo", "rustc", "cc", "ld", "ar"):
            held_tools[name].verify()


def _validate_metadata(payload: str) -> str:
    try:
        metadata = json.loads(payload, object_pairs_hook=_reject_duplicate_json)
    except json.JSONDecodeError as error:
        raise RebuildError("Cargo metadata output is malformed") from error
    if not isinstance(metadata, dict):
        fail("Cargo metadata root is malformed")
    if metadata.get("workspace_root") != "/src" or metadata.get("target_directory") != "/target":
        fail("Cargo metadata escaped sandbox roots")
    packages = metadata.get("packages")
    members = metadata.get("workspace_members")
    resolution = metadata.get("resolve")
    if not isinstance(packages, list) or not isinstance(members, list) or not isinstance(resolution, dict):
        fail("Cargo metadata closure is incomplete")
    package_ids: set[str] = set()
    closure = hashlib.sha256()
    for package in packages:
        if not isinstance(package, dict):
            fail("Cargo metadata package is malformed")
        package_id = package.get("id")
        manifest = package.get("manifest_path")
        source = package.get("source")
        dependencies = package.get("dependencies")
        if not isinstance(package_id, str) or not isinstance(manifest, str) or not isinstance(dependencies, list):
            fail("Cargo metadata package fields are malformed")
        if source is None:
            if not (manifest == "/src/Cargo.toml" or manifest.startswith("/src/")):
                fail("Cargo path dependency escaped canonical workspace")
        elif not (
            isinstance(source, str)
            and source.startswith("registry+https://github.com/rust-lang/crates.io-index")
        ):
            fail("Cargo metadata contains non-registry external source")
        for dependency in dependencies:
            if not isinstance(dependency, dict):
                fail("Cargo metadata dependency is malformed")
            path = dependency.get("path")
            if path is not None and not (
                isinstance(path, str) and (path == "/src" or path.startswith("/src/"))
            ):
                fail("Cargo path dependency escaped canonical workspace")
        package_ids.add(package_id)
        closure.update(
            json.dumps(package, sort_keys=True, separators=(",", ":")).encode()
        )
    if set(members) - package_ids:
        fail("Cargo workspace member is absent from package closure")
    nodes = resolution.get("nodes")
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict) or node.get("id") not in package_ids for node in nodes
    ):
        fail("Cargo resolve closure is malformed")
    return closure.hexdigest()


def _open_target_artifact(target_fd: int) -> int:
    descriptor = os.dup(target_fd)
    try:
        for part in ("x86_64-unknown-linux-gnu", "release"):
            child = os.open(
                part,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        artifact = os.open(
            "llm-guard-proxy",
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        return artifact
    except OSError as error:
        raise RebuildError("built artifact path is unavailable or unsafe") from error
    finally:
        os.close(descriptor)


def _normalize_target_artifact_link(target_fd: int, artifact_fd: int) -> None:
    before = os.fstat(artifact_fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_mode & 0o022
        or not before.st_mode & stat.S_IXUSR
        or before.st_size <= 0
        or before.st_size > MAX_EXECUTABLE_BYTES
        or before.st_nlink not in {1, 2}
    ):
        fail("built artifact object metadata is unsafe")
    release_fd = os.dup(target_fd)
    try:
        for part in ("x86_64-unknown-linux-gnu", "release"):
            child = os.open(
                part,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=release_fd,
            )
            os.close(release_fd)
            release_fd = child
        current = os.stat(
            "llm-guard-proxy", dir_fd=release_fd, follow_symlinks=False
        )
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            fail("built artifact path changed after same-FD open")
        if before.st_nlink == 2:
            deps_fd = os.open(
                "deps",
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=release_fd,
            )
            try:
                names = sorted(os.listdir(deps_fd))
                if len(names) > 100_000:
                    fail("built artifact dependency directory exceeded bound")
                linked = []
                for name in names:
                    if not re.fullmatch(r"llm_guard_proxy-[0-9a-f]{16}", name):
                        continue
                    info = os.stat(name, dir_fd=deps_fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino):
                        linked.append(name)
                if len(linked) != 1:
                    fail("built artifact has unrecognized hardlink authority")
                os.unlink(linked[0], dir_fd=deps_fd)
                os.fsync(deps_fd)
            finally:
                os.close(deps_fd)
        after = os.fstat(artifact_fd)
        current = os.stat(
            "llm-guard-proxy", dir_fd=release_fd, follow_symlinks=False
        )
        def stable(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev,
                info.st_ino,
                info.st_uid,
                info.st_gid,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
            )
        if (
            after.st_nlink != 1
            or current.st_nlink != 1
            or stable(after) != stable(before)
            or stable(current) != stable(before)
        ):
            fail("built artifact same-FD hardlink normalization failed")
    except OSError as error:
        raise RebuildError("built artifact hardlink authority is unsafe") from error
    finally:
        os.close(release_fd)


@dataclass
class BuildBundle:
    candidate: Path
    identity: ExecutableIdentity
    metadata_closure_sha256: str
    sandbox_contract_sha256: str
    inputs: dict[str, object]


def build_sandboxed_candidate(source: SourceBundle) -> BuildBundle:
    authorities = [
        _open_directory_authority("toolchain", toolchain_root),
        _open_directory_authority("registry cache", registry_cache),
        _open_directory_authority("registry index", registry_index),
    ]
    target_root = Path(tempfile.mkdtemp(prefix=".build-target-", dir=cache_root))
    target = _open_directory_authority("build target", target_root)
    try:
        metadata_output = _run_sandbox(
            source,
            target,
            authorities[0],
            authorities[1],
            authorities[2],
            [
                "metadata",
                "--format-version=1",
                "--frozen",
                "--locked",
                "--offline",
                "--filter-platform",
                "x86_64-unknown-linux-gnu",
            ],
        )
        metadata_closure = _validate_metadata(metadata_output)
        inventory_fd = os.open(
            ".",
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=target.descriptor,
        )
        try:
            metadata_target_names = sorted(os.listdir(inventory_fd))
        finally:
            os.close(inventory_fd)
        try:
            metadata_target_info = os.stat(
                ".rustc_info.json",
                dir_fd=target.descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise RebuildError("Cargo metadata target state differs") from error
        metadata_target_ledger = _directory_ledger(
            target.descriptor, "metadata target"
        )
        if (
            metadata_target_names != [".rustc_info.json"]
            or not stat.S_ISREG(metadata_target_info.st_mode)
            or metadata_target_info.st_uid != os.geteuid()
            or metadata_target_info.st_nlink != 1
            or metadata_target_info.st_mode & 0o022
            or not 0 < metadata_target_info.st_size <= 1024 * 1024
            or metadata_target_ledger.files != 1
        ):
            fail(
                "Cargo metadata target state differs: "
                f"names={metadata_target_names!r} uid={metadata_target_info.st_uid} "
                f"mode={stat.S_IMODE(metadata_target_info.st_mode):o} "
                f"nlink={metadata_target_info.st_nlink} "
                f"size={metadata_target_info.st_size} "
                f"files={metadata_target_ledger.files}"
            )
        build_arguments = [
            "build",
            "--release",
            "--frozen",
            "--locked",
            "--offline",
            "--target",
            "x86_64-unknown-linux-gnu",
            "-p",
            "llm-guard-proxy",
            "--features",
            "guard",
        ]
        sandbox_contract = sha256_bytes(
            "\0".join(
                _sandbox_command(
                    source.source_authority.descriptor,
                    target.descriptor,
                    authorities[0].descriptor,
                    authorities[1].descriptor,
                    authorities[2].descriptor,
                    build_arguments,
                )
            ).encode()
        )
        _run_sandbox(
            source,
            target,
            authorities[0],
            authorities[1],
            authorities[2],
            build_arguments,
        )
        artifact_fd = _open_target_artifact(target.descriptor)
        try:
            _normalize_target_artifact_link(target.descriptor, artifact_fd)
            raw_identity = fd_identity(artifact_fd)
            candidate = (
                cache_root
                / "releases"
                / f"{source.commit}-{raw_identity.sha256}"
                / "llm-guard-proxy"
            )
            if candidate.exists():
                require_secure_regular(candidate, executable=True)
                existing = os.open(
                    candidate,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    existing_identity = fd_identity(existing)
                finally:
                    os.close(existing)
                if existing_identity.sha256 != raw_identity.sha256 or existing_identity.build_id != raw_identity.build_id:
                    fail("existing content-addressed candidate identity differs")
            else:
                atomic_copy_fd(artifact_fd, candidate, 0o755)
        finally:
            os.close(artifact_fd)
        candidate_fd = os.open(
            candidate,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            candidate_identity = fd_identity(candidate_fd)
        finally:
            os.close(candidate_fd)
        if (
            candidate_identity.size,
            candidate_identity.sha256,
            candidate_identity.build_id,
        ) != (raw_identity.size, raw_identity.sha256, raw_identity.build_id):
            fail("adopted candidate differs from built artifact")
        source.verify()
        for authority in authorities:
            authority.verify()
        _verify_all_tools()
        inputs = {
            authority.name.replace(" ", "_"): authority.receipt()
            for authority in authorities
        }
        inputs["metadata_target"] = {
            "files": metadata_target_names,
            "content_sha256": metadata_target_ledger.content_sha256,
            "metadata_sha256": metadata_target_ledger.metadata_sha256,
            "file_count": metadata_target_ledger.files,
            "byte_count": metadata_target_ledger.bytes,
        }
        return BuildBundle(
            candidate,
            candidate_identity,
            metadata_closure,
            sandbox_contract,
            inputs,
        )
    finally:
        target.close()
        for authority in reversed(authorities):
            authority.close()
        cleanup_snapshot(target_root)


def _validate_reviewed_tool_versions(cargo: bytes, rustc: bytes) -> None:
    if test_only:
        return
    cargo_text = cargo.decode("ascii", errors="strict")
    rustc_text = rustc.decode("ascii", errors="strict")
    if (
        cargo_text.splitlines()[0] != "cargo 1.97.1 (c980f4866 2026-06-30)"
        or "release: 1.97.1" not in cargo_text.splitlines()
        or "host: x86_64-unknown-linux-gnu" not in cargo_text.splitlines()
        or rustc_text.splitlines()[0]
        != "rustc 1.97.1 (8bab26f4f 2026-07-14)"
        or "host: x86_64-unknown-linux-gnu" not in rustc_text.splitlines()
        or "release: 1.97.1" not in rustc_text.splitlines()
        or "LLVM version: 22.1.6" not in rustc_text.splitlines()
    ):
        fail("reviewed Cargo/rustc version contract differs")


def cleanup_snapshot(root: Path | None) -> None:
    if root is None or not root.exists():
        return
    for directory, directories, files in os.walk(root, topdown=False):
        Path(directory).chmod(0o700)
        for name in files:
            path = Path(directory) / name
            try:
                path.chmod(0o600, follow_symlinks=False)
            except (FileNotFoundError, NotImplementedError):
                pass
            path.unlink(missing_ok=True)
        for name in directories:
            path = Path(directory) / name
            try:
                path.chmod(0o700, follow_symlinks=False)
            except (FileNotFoundError, NotImplementedError):
                pass
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
    try:
        root.chmod(0o700)
    except FileNotFoundError:
        return
    root.rmdir()




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
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or not before.st_mode & stat.S_IXUSR
        or before.st_mode & 0o022
        or before.st_size <= 0
        or before.st_size > MAX_EXECUTABLE_BYTES
    ):
        fail("built artifact or runtime executable metadata is unsafe")
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
    time.sleep(2)


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
source_bundle: SourceBundle | None = None
prestate: Prestate | None = None
accepted: RuntimeAttestation | None = None
rollback_armed = False
restart_attempted = False
runtime_backup: Path | None = None
published_receipt: Path | None = None
try:
    _open_all_tools()
    require_secure_regular(guard_config)
    require_secure_regular(guard_unit)
    config_sha_initial = sha256_file(guard_config)
    unit_sha_initial = sha256_file(guard_unit)
    cargo_identity_initial = capture_tool("cargo", "--version", "--verbose")
    rustc_identity_initial = capture_tool("rustc", "-vV")
    _validate_reviewed_tool_versions(cargo_identity_initial, rustc_identity_initial)
    cargo_identity_sha = sha256_bytes(cargo_identity_initial)
    rustc_identity_sha = sha256_bytes(rustc_identity_initial)

    prestate = snapshot_prestate()

    ensure_private_directory(cache_root)
    snapshot_root = Path(tempfile.mkdtemp(prefix=".rebuild-input-", dir=cache_root))
    snapshot_root.chmod(0o700)
    source_bundle = prepare_canonical_source(snapshot_root)
    build_bundle = build_sandboxed_candidate(source_bundle)
    source_commit = source_bundle.commit
    source_tree = source_bundle.tree
    source_archive_sha = source_bundle.archive_sha256
    snapshot_content_sha = source_bundle.tree_ledger_sha256
    candidate = build_bundle.candidate
    candidate_identity = build_bundle.identity
    source_bundle.verify()

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

    source_bundle.verify()
    if config_sha_initial != sha256_file(guard_config) or unit_sha_initial != sha256_file(guard_unit):
        fail("installed Guard config or unit changed before publication")
    if cargo_identity_initial != capture_tool("cargo", "--version", "--verbose"):
        fail("Cargo toolchain identity changed before publication")
    if rustc_identity_initial != capture_tool("rustc", "-vV"):
        fail("rustc toolchain identity changed before publication")
    final_runtime_attestation(accepted, candidate, candidate_identity)

    receipt_id = secrets.token_hex(16)
    receipt_payload: dict[str, object] = {
        "schema": 2,
        "mode": "test-only" if test_only else "production",
        "receipt_id": receipt_id,
        "canonical_source_url": source_repo,
        "canonical_source_ref": source_ref,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "source_archive_sha256": source_archive_sha,
        "snapshot_content_sha256": snapshot_content_sha,
        "source_file_count": source_bundle.file_count,
        "source_byte_count": source_bundle.byte_count,
        "git_config_sha256": source_bundle.config_sha256,
        "metadata_closure_sha256": build_bundle.metadata_closure_sha256,
        "sandbox_contract_sha256": build_bundle.sandbox_contract_sha256,
        "build_inputs": build_bundle.inputs,
        "tool_authorities": {
            name: held.receipt() for name, held in sorted(held_tools.items())
        },
        "python_runtime_authority": _runtime_python_authority(),
        "cargo_identity_sha256": cargo_identity_sha,
        "rustc_identity_sha256": rustc_identity_sha,
        "guard_config_sha256": config_sha_initial,
        "guard_unit_sha256": unit_sha_initial,
        "binary_sha256": candidate_identity.sha256,
        "binary_size": candidate_identity.size,
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
    source_bundle.verify()
    _verify_all_tools()
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
    if source_bundle is not None:
        source_bundle.close()
    cleanup_snapshot(snapshot_root)
    for held in reversed(list(held_tools.values())):
        held.close()
    held_tools.clear()
