from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import stat
import sys
import tarfile
import tempfile
import time
import types
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

__all__: list[str] = []

EXPECTED_BOUNDED_PROCESS_SHA256 = (
    "8787dba9c1545e146f08db3e5d395ffeea60c7bd2ea05f687270cf65ad401264"
)
_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
_BOUNDED_PROCESS_PATH = _SCRIPT_DIRECTORY / "gb10_bounded_process.py"
_bounded_fd = os.open(
    _BOUNDED_PROCESS_PATH,
    os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
)
try:
    _bounded_metadata = os.fstat(_bounded_fd)
    if (
        not stat.S_ISREG(_bounded_metadata.st_mode)
        or _bounded_metadata.st_uid != os.geteuid()
        or _bounded_metadata.st_nlink != 1
        or _bounded_metadata.st_mode & 0o022
        or not 0 < _bounded_metadata.st_size <= 1024 * 1024
    ):
        raise RuntimeError("Guard bounded-process import authority differs")
    _bounded_chunks: list[bytes] = []
    _bounded_size = 0
    while _bounded_chunk := os.read(_bounded_fd, 65536):
        _bounded_size += len(_bounded_chunk)
        if _bounded_size > 1024 * 1024:
            raise RuntimeError("Guard bounded-process import authority is oversized")
        _bounded_chunks.append(_bounded_chunk)
finally:
    os.close(_bounded_fd)
_bounded_payload = b"".join(_bounded_chunks)
if hashlib.sha256(_bounded_payload).hexdigest() != EXPECTED_BOUNDED_PROCESS_SHA256:
    raise RuntimeError("Guard bounded-process import authority differs")
_bounded_module = types.ModuleType("gb10_bounded_process")
_bounded_module.__file__ = str(_BOUNDED_PROCESS_PATH)
_bounded_module.__package__ = ""
sys.modules[_bounded_module.__name__] = _bounded_module
exec(
    compile(_bounded_payload, str(_BOUNDED_PROCESS_PATH), "exec"),
    _bounded_module.__dict__,
)
run_bounded = _bounded_module.command

UNIT = "llm-guard-proxy.service"
HEALTH_URL = "http://100.105.4.92:18009/health"
PRODUCTION_COMPLETE = "LLM_GUARD_PROXY_REBUILD_COMPLETE"
TEST_COMPLETE = "LLM_GUARD_PROXY_REBUILD_TEST_ONLY_COMPLETE"
RECOVERY_COMPLETE = "LLM_GUARD_PROXY_REBUILD_RECOVERED"
FORWARD_SECONDS = 1800.0
RECOVERY_SECONDS = 180.0
STATE_MAX_BYTES = 64 * 1024
PHASES = {"prestate", "mutated", "committed"}
MAX_EXECUTABLE_BYTES = 128 * 1024 * 1024
GENERATION_FIELDS = (
    "LoadState",
    "ActiveState",
    "SubState",
    "FragmentPath",
    "DropInPaths",
    "MainPID",
    "InvocationID",
    "ActiveEnterTimestampMonotonic",
    "Result",
    "Job",
    "ExecStart",
    "LoadCredential",
    "NoNewPrivileges",
    "PrivateTmp",
    "ProtectSystem",
    "ProtectHome",
    "UMask",
    "Environment",
    "EnvironmentFiles",
)
MANAGER_FIELDS = ("InvocationID", "UserspaceTimestampMonotonic")
MANAGER_POLL_SECONDS = 0.2
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
    "LLM_GUARD_REBUILD_TEST_CONFIG",
    "LLM_GUARD_REBUILD_TEST_MISSING_TOOL",
    "LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE",
    "LLM_GUARD_REBUILD_TEST_REPLACE_EXE_DURING_HASH",
    "LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS",
    "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS",
    "LLM_GUARD_REBUILD_TEST_CRASH_POINT",
    "LLM_GUARD_REBUILD_TEST_CRASH_MARKER",
    "LLM_GUARD_REBUILD_TEST_DELAY_STAGE",
    "LLM_GUARD_REBUILD_TEST_DELAY_SECONDS",
)
rollback_logging = False
operation_deadline: float | None = None
test_only_mode = False


class RebuildError(RuntimeError):
    pass


def _deadline_checkpoint(label: str) -> None:
    deadline = operation_deadline
    if deadline is None:
        return
    if (
        test_only
        and os.environ.get("LLM_GUARD_REBUILD_TEST_DELAY_STAGE") == label
    ):
        raw = os.environ.get("LLM_GUARD_REBUILD_TEST_DELAY_SECONDS", "")
        if re.fullmatch(r"[1-9][0-9]*(?:\.[0-9]+)?", raw) is None:
            fail("test-only deadline delay is malformed")
        time.sleep(min(float(raw), max(0.0, deadline - time.monotonic())))
    if time.monotonic() >= deadline:
        fail(f"{label} deadline exhausted")


def log(message: str) -> None:
    _deadline_checkpoint("diagnostic")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    descriptor = sys.stdout.fileno()
    blocking = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
        os.write(descriptor, f"[{stamp}] {message}\n".encode())
    except (BlockingIOError, OSError):
        if not rollback_logging:
            raise
    finally:
        os.set_blocking(descriptor, blocking)


def _report_error(message: str) -> None:
    descriptor = sys.stderr.fileno()
    blocking = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
        os.write(descriptor, f"ERROR: {message}\n".encode())
    except (BlockingIOError, OSError):
        pass
    finally:
        os.set_blocking(descriptor, blocking)


def fail(message: str) -> NoReturn:
    raise RebuildError(message)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or not 0 < info.st_size <= MAX_EXECUTABLE_BYTES
        ):
            fail(f"unsafe regular file authority: {path}")
        return sha256_bytes(_read_fd_limited(descriptor, info.st_size, str(path)))
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    _deadline_checkpoint("filesystem-write")
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        os.fsync(descriptor)
        _deadline_checkpoint("filesystem-write")
    finally:
        os.close(descriptor)


def require_secure_regular(path: Path, *, executable: bool = False) -> os.stat_result:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or not 0 < metadata.st_size <= MAX_EXECUTABLE_BYTES
            or (executable and not metadata.st_mode & stat.S_IXUSR)
        ):
            fail(f"unsafe regular file authority: {path}")
        return metadata
    finally:
        os.close(descriptor)


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
    "as",
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
        if not all(
            isinstance(values[key], str) for key in ("logical", "resolved", "sha256")
        ) or not all(
            isinstance(values[key], int) and not isinstance(values[key], bool)
            for key in ("uid", "gid", "mode")
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
            not all(
                isinstance(value, str)
                for value in (spec.logical, spec.resolved, spec.sha256)
            )
            or not all(
                isinstance(value, int) and value >= 0
                for value in (spec.uid, spec.gid, spec.mode)
            )
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


@dataclass
class FileAuthority:
    label: str
    path: Path
    descriptor: int
    fields: tuple[int, ...]
    sha256: str
    max_bytes: int

    def verify(self) -> None:
        held = os.fstat(self.descriptor)
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except OSError as error:
            raise RebuildError(f"{self.label} authority changed") from error
        held_fields = _file_fields(held)
        if (
            held_fields != self.fields
            or _file_fields(current) != self.fields
            or _sha256_fd(self.descriptor, self.max_bytes, self.label) != self.sha256
        ):
            fail(f"{self.label} authority changed")

    def close(self) -> None:
        os.close(self.descriptor)


def _file_fields(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_size,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _open_file_authority(
    label: str,
    path: Path,
    *,
    expected_mode: int,
    max_bytes: int,
) -> FileAuthority:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != expected_mode
            or not 0 < info.st_size <= max_bytes
        ):
            fail(f"unsafe {label} authority")
        payload = _read_fd_limited(descriptor, max_bytes, label)
        authority = FileAuthority(
            label,
            path,
            descriptor,
            _file_fields(info),
            sha256_bytes(payload),
            max_bytes,
        )
        authority.verify()
        return authority
    except BaseException:
        os.close(descriptor)
        raise


def _sha256_fd(
    descriptor: int,
    limit: int = MAX_EXECUTABLE_BYTES,
    label: str = "file authority",
) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        if operation_deadline is not None and time.monotonic() >= operation_deadline:
            fail(f"{label} hash deadline exhausted")
        chunk = os.pread(descriptor, min(1024 * 1024, limit + 1 - offset), offset)
        if not chunk:
            break
        offset += len(chunk)
        if offset > limit:
            fail(f"{label} exceeded byte bound")
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
        or digest != "6d972cf21be56fe3c947ab6ba257ff8d08c342dd2714442986791bd9a6dfabfe"
    ):
        fail("Python runtime object authority differs")
    return {
        "logical_path": "/usr/bin/python3" if not test_only else sys.executable,
        "resolved_path": (
            "/usr/bin/python3.11" if not test_only else os.readlink("/proc/self/exe")
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


def _runtime_python_authority_sha256() -> str:
    return sha256_bytes(
        json.dumps(
            _runtime_python_authority(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    )


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
        "git": root(
            "/usr/bin/git",
            "2540879925a6881e3877ff7e3330746ba3027b04edf16a3a12dccd1644c4f32d",
        ),
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
        "as": root(
            "/usr/bin/x86_64-linux-gnu-as",
            "41fe4f5a03389ea5cf7c92d6753fa1ecc69b45b12534fd8713c53bba0e2d7e17",
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
        "gcc_root",
        "sysroot_lib",
        "sysroot_include",
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
    for key in (
        "registry_cache",
        "registry_index",
        "toolchain_root",
        "gcc_root",
        "sysroot_lib",
        "sysroot_include",
    ):
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
    gcc_root = Path("/usr/lib/gcc/x86_64-linux-gnu/12")
    sysroot_lib = Path("/usr/lib/x86_64-linux-gnu")
    sysroot_include = Path("/usr/include")
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
    gcc_root = Path(authority_config["gcc_root"])
    sysroot_lib = Path(authority_config["sysroot_lib"])
    sysroot_include = Path(authority_config["sysroot_include"])
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
fixed_authorities: dict[str, FileAuthority] = {}
candidate_authority: FileAuthority | None = None


def _open_fixed_authorities(expected: dict[str, Any] | None = None) -> None:
    opened = {
        "config": _open_file_authority(
            "installed Guard config",
            guard_config,
            expected_mode=0o644,
            max_bytes=1024 * 1024,
        ),
        "unit": _open_file_authority(
            "installed Guard unit",
            guard_unit,
            expected_mode=0o644,
            max_bytes=1024 * 1024,
        ),
    }
    try:
        if expected is not None and (
            opened["config"].sha256 != expected["guard_config_sha256"]
            or opened["unit"].sha256 != expected["guard_unit_sha256"]
        ):
            fail("installed Guard config or unit differs from transaction authority")
        fixed_authorities.update(opened)
    except BaseException:
        for authority in opened.values():
            authority.close()
        raise


def _verify_fixed_authorities() -> None:
    for authority in fixed_authorities.values():
        authority.verify()


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
    del capture
    deadline = operation_deadline
    if deadline is None:
        fail("bounded command invoked without a transaction deadline")
    budget = deadline - time.monotonic()
    if budget <= 0:
        fail("transaction command deadline exhausted")
    log("+ " + shlex.join(command))
    used = [tool for tool in held_tools.values() if tool.exec_path in command]
    effective_fds = tuple(sorted(set(pass_fds + _tool_fds())))
    try:
        output = run_bounded(
            command,
            timeout=budget,
            deadline=operation_deadline,
            cwd=cwd,
            env=child_env if env is None else env,
            pass_fds=effective_fds,
            cleanup_reserve=min(5.0, max(0.0, budget / 2)),
        )
        return output.encode("utf-8")
    except RuntimeError as error:
        raise RebuildError(
            f"bounded command failed: {Path(command[0]).name}"
        ) from error
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
                digest = _sha256_fd(
                    descriptor, 8 * 1024 * 1024 * 1024, f"{label}/{relative}"
                )
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
            content.update(
                f"F\0{relative}\0{mode:o}\0{info.st_size}\0{digest}\n".encode()
            )

    fresh_root = os.open(
        ".",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
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
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
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
        if operation_deadline is not None and time.monotonic() >= operation_deadline:
            fail(f"{label} read deadline exhausted")
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


def _verify_git_config(
    repo_fd: int, config_fd: int, fields: tuple[int, ...], digest: str
) -> None:
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
    if (
        held_fields != fields
        or current_fields != fields
        or _sha256_fd(config_fd) != digest
    ):
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
        forbidden = (
            path in {".gitmodules", ".gitattributes"}
            or path.endswith(
                (
                    "/.gitmodules",
                    "/.gitattributes",
                    "/.cargo/config",
                    "/.cargo/config.toml",
                )
            )
            or path in {".cargo/config", ".cargo/config.toml"}
        )
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


def _extract_archive(
    archive_path: Path, source_path: Path, commit: str, entries: list[TreeEntry]
) -> str:
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
        commit = (
            execute(
                [
                    require_tool("git"),
                    *_git_options(),
                    git_dir,
                    "rev-parse",
                    "refs/gb10/rebuild^{commit}",
                ],
                capture=True,
                env=git_env,
                pass_fds=(repo_fd, exec_fd),
            )
            .decode("ascii")
            .strip()
        )
        tree = (
            execute(
                [
                    require_tool("git"),
                    *_git_options(),
                    git_dir,
                    "rev-parse",
                    f"{commit}^{{tree}}",
                ],
                capture=True,
                env=git_env,
                pass_fds=(repo_fd, exec_fd),
            )
            .decode("ascii")
            .strip()
        )
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
    gcc_fd: int,
    sysroot_lib_fd: int,
    sysroot_include_fd: int,
    cargo_arguments: list[str],
) -> list[str]:
    cargo = "/toolchain/bin/cargo"
    rustc = "/toolchain/bin/rustc"
    cc = "/usr/bin/x86_64-linux-gnu-gcc-12"
    ar = "/usr/bin/x86_64-linux-gnu-ar"
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
        "--dir",
        "/usr",
        "--dir",
        "/usr/bin",
        "--dir",
        "/usr/lib",
        "--dir",
        "/usr/lib/gcc",
        "--dir",
        "/usr/lib/gcc/x86_64-linux-gnu",
        "--ro-bind",
        f"/proc/self/fd/{gcc_fd}",
        "/usr/lib/gcc/x86_64-linux-gnu/12",
        "--ro-bind",
        f"/proc/self/fd/{sysroot_lib_fd}",
        "/usr/lib/x86_64-linux-gnu",
        "--ro-bind",
        f"/proc/self/fd/{sysroot_include_fd}",
        "/usr/include",
        "--ro-bind",
        held_tools["cc"].exec_path,
        cc,
        "--ro-bind",
        held_tools["as"].exec_path,
        "/usr/bin/as",
        "--ro-bind",
        held_tools["ld"].exec_path,
        "/usr/bin/ld",
        "--ro-bind",
        held_tools["ar"].exec_path,
        ar,
        "--tmpfs",
        "/usr/local",
        "--tmpfs",
        "/home",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib/x86_64-linux-gnu",
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
        "/toolchain/bin:/usr/bin",
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
    gcc: DirectoryAuthority,
    sysroot_lib_authority: DirectoryAuthority,
    sysroot_include_authority: DirectoryAuthority,
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
                    gcc.descriptor,
                    sysroot_lib_authority.descriptor,
                    sysroot_include_authority.descriptor,
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
        gcc.descriptor,
        sysroot_lib_authority.descriptor,
        sysroot_include_authority.descriptor,
        arguments,
    )
    try:
        return execute(
            command,
            capture=True,
            cwd=Path("/"),
            env=child_env,
            pass_fds=pass_fds,
        ).decode("utf-8")
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
    if (
        metadata.get("workspace_root") != "/src"
        or metadata.get("target_directory") != "/target"
    ):
        fail("Cargo metadata escaped sandbox roots")
    packages = metadata.get("packages")
    members = metadata.get("workspace_members")
    resolution = metadata.get("resolve")
    if (
        not isinstance(packages, list)
        or not isinstance(members, list)
        or not isinstance(resolution, dict)
    ):
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
        if (
            not isinstance(package_id, str)
            or not isinstance(manifest, str)
            or not isinstance(dependencies, list)
        ):
            fail("Cargo metadata package fields are malformed")
        if source is None:
            if not (manifest == "/src/Cargo.toml" or manifest.startswith("/src/")):
                fail("Cargo path dependency escaped canonical workspace")
        elif not (
            isinstance(source, str)
            and source.startswith(
                "registry+https://github.com/rust-lang/crates.io-index"
            )
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
        not isinstance(node, dict) or node.get("id") not in package_ids
        for node in nodes
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
        current = os.stat("llm-guard-proxy", dir_fd=release_fd, follow_symlinks=False)
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
        current = os.stat("llm-guard-proxy", dir_fd=release_fd, follow_symlinks=False)

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
    authority: FileAuthority


def build_sandboxed_candidate(source: SourceBundle) -> BuildBundle:
    authorities = [
        _open_directory_authority("toolchain", toolchain_root),
        _open_directory_authority("registry cache", registry_cache),
        _open_directory_authority("registry index", registry_index),
        _open_directory_authority("gcc closure", gcc_root),
        _open_directory_authority("sysroot lib", sysroot_lib),
        _open_directory_authority("sysroot include", sysroot_include),
    ]
    target_root = Path(tempfile.mkdtemp(prefix=".build-target-", dir=cache_root))
    target = _open_directory_authority("build target", target_root)
    target_identity = (target.metadata[0], target.metadata[1])
    try:
        metadata_output = _run_sandbox(
            source,
            target,
            authorities[0],
            authorities[1],
            authorities[2],
            authorities[3],
            authorities[4],
            authorities[5],
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
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
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
        metadata_target_ledger = _directory_ledger(target.descriptor, "metadata target")
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
                    authorities[3].descriptor,
                    authorities[4].descriptor,
                    authorities[5].descriptor,
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
            authorities[3],
            authorities[4],
            authorities[5],
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
                if (
                    existing_identity.sha256 != raw_identity.sha256
                    or existing_identity.build_id != raw_identity.build_id
                ):
                    fail("existing content-addressed candidate identity differs")
            else:
                atomic_copy_fd(artifact_fd, candidate, 0o755)
        finally:
            os.close(artifact_fd)
        candidate_file = _open_file_authority(
            "candidate executable",
            candidate,
            expected_mode=0o755,
            max_bytes=MAX_EXECUTABLE_BYTES,
        )
        candidate_identity = fd_identity(candidate_file.descriptor)
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
            candidate_file,
        )
    finally:
        target.close()
        for authority in reversed(authorities):
            authority.close()
        cleanup_snapshot(target_root, target_identity)


def _validate_reviewed_tool_versions(cargo: bytes, rustc: bytes) -> None:
    if test_only:
        return
    cargo_text = cargo.decode("ascii", errors="strict")
    rustc_text = rustc.decode("ascii", errors="strict")
    if (
        cargo_text.splitlines()[0] != "cargo 1.97.1 (c980f4866 2026-06-30)"
        or "release: 1.97.1" not in cargo_text.splitlines()
        or "host: x86_64-unknown-linux-gnu" not in cargo_text.splitlines()
        or rustc_text.splitlines()[0] != "rustc 1.97.1 (8bab26f4f 2026-07-14)"
        or "host: x86_64-unknown-linux-gnu" not in rustc_text.splitlines()
        or "release: 1.97.1" not in rustc_text.splitlines()
        or "LLVM version: 22.1.6" not in rustc_text.splitlines()
    ):
        fail("reviewed Cargo/rustc version contract differs")


def _directory_identity(path: Path) -> tuple[int, int]:
    _deadline_checkpoint("filesystem-read")
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        fail(f"unsafe owned directory: {path}")
    return metadata.st_dev, metadata.st_ino


def _remove_tree_contents(directory_fd: int, expected_device: int) -> None:
    _deadline_checkpoint("snapshot-cleanup")
    metadata = os.fstat(directory_fd)
    if metadata.st_uid != os.geteuid() or metadata.st_dev != expected_device:
        fail("owned cleanup tree crossed its filesystem authority")
    os.fchmod(directory_fd, 0o700)
    for entry in os.scandir(directory_fd):
        _deadline_checkpoint("snapshot-cleanup")
        if not entry.is_dir(follow_symlinks=False):
            os.unlink(entry.name, dir_fd=directory_fd)
            continue
        child_fd = os.open(
            entry.name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            _remove_tree_contents(child_fd, expected_device)
        finally:
            os.close(child_fd)
        os.rmdir(entry.name, dir_fd=directory_fd)


def cleanup_snapshot(
    root: Path | None, expected_identity: tuple[int, int] | None = None
) -> None:
    if root is None:
        return
    if expected_identity is None:
        fail("owned directory cleanup lacks exact identity")
    _deadline_checkpoint("snapshot-cleanup")
    parent_fd = os.open(
        root.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o022
        ):
            fail(f"unsafe owned cleanup parent: {root.parent}")
        cleanup_pattern = re.compile(
            rf"\.{re.escape(root.name)}\.cleanup\.[1-9][0-9]*\.[0-9a-f]{{8}}"
        )
        tombstones: list[str] = []
        for entry in os.scandir(parent_fd):
            _deadline_checkpoint("snapshot-cleanup")
            if cleanup_pattern.fullmatch(entry.name):
                tombstones.append(entry.name)
        if len(tombstones) > 1:
            fail(f"owned directory cleanup inventory is ambiguous: {root}")
        try:
            root_fd = os.open(
                root.name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            if expected_identity is None or not tombstones:
                return
            owned_name = tombstones[0]
        else:
            if tombstones:
                os.close(root_fd)
                fail(f"owned directory conflicts with cleanup tombstone: {root}")
            owned_name = root.name
        if owned_name != root.name:
            root_fd = os.open(
                owned_name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        try:
            metadata = os.fstat(root_fd)
            actual = (metadata.st_dev, metadata.st_ino)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
                or actual != expected_identity
            ):
                fail(f"owned directory identity differs: {root}")
        finally:
            os.close(root_fd)
        tombstone = owned_name
        if owned_name == root.name:
            tombstone = f".{root.name}.cleanup.{os.getpid()}.{secrets.token_hex(4)}"
            _deadline_checkpoint("snapshot-cleanup")
            os.rename(
                root.name, tombstone, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
        moved = os.stat(tombstone, dir_fd=parent_fd, follow_symlinks=False)
        if (moved.st_dev, moved.st_ino) != actual or not stat.S_ISDIR(moved.st_mode):
            fail(f"owned directory changed during quarantine: {root}")
        os.fsync(parent_fd)
        tombstone_fd = os.open(
            tombstone,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            quarantined = os.fstat(tombstone_fd)
            if (
                not stat.S_ISDIR(quarantined.st_mode)
                or quarantined.st_uid != os.geteuid()
                or quarantined.st_mode & 0o022
                or (quarantined.st_dev, quarantined.st_ino) != actual
            ):
                fail(f"owned directory changed after quarantine: {root}")
            _remove_tree_contents(tombstone_fd, actual[0])
        finally:
            os.close(tombstone_fd)
        _deadline_checkpoint("snapshot-cleanup")
        os.rmdir(tombstone, dir_fd=parent_fd)
        os.fsync(parent_fd)
        _deadline_checkpoint("snapshot-cleanup")
    finally:
        os.close(parent_fd)


@dataclass(frozen=True)
class Generation:
    pid: int
    invocation: str
    started: int
    fragment: str
    result: str
    job: int | None


@dataclass(frozen=True)
class ManagerGeneration:
    invocation: str
    started: int


@dataclass(frozen=True)
class ManagerJob:
    job_id: int
    job_type: str
    state: str


def _verify_manager_contract(values: dict[str, str]) -> None:
    credential = "/run/credentials/llm-guard-proxy.service/llm-guard-config"
    runtime_dir = f"/run/user/{os.getuid()}/gb10-memory-guardian"
    expected_argv = (
        f"{service_bin} --config {credential} --guardian-runtime-dir {runtime_dir}"
    )
    match = re.fullmatch(
        r"\{ path=([^ ]+) ; argv\[\]=(.+?) ; ignore_errors=no(?: ; .*)? ; \}",
        values["ExecStart"],
    )
    expected = {
        "LoadCredential": f"llm-guard-config:{guard_config}",
        "NoNewPrivileges": "yes",
        "PrivateTmp": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "read-only",
        "UMask": "0077",
        "Environment": "",
        "EnvironmentFiles": "",
    }
    if (
        match is None
        or match.group(1) != str(service_bin)
        or match.group(2) != expected_argv
        or any(values[key] != value for key, value in expected.items())
    ):
        fail("manager-loaded Guard contract differs")


def query_manager_generation() -> ManagerGeneration:
    payload = execute(
        [
            require_tool("systemctl"),
            "--user",
            "show",
            *(f"--property={field}" for field in MANAGER_FIELDS),
        ],
        capture=True,
    )
    if len(payload) > 1024 or b"\x00" in payload or b"\r" in payload:
        fail("user manager generation output is malformed")
    values: dict[str, str] = {}
    try:
        rows = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise RebuildError("user manager generation output is not ASCII") from error
    for row in rows:
        key, separator, value = row.partition("=")
        if separator != "=" or key not in MANAGER_FIELDS or key in values:
            fail("user manager generation output has duplicate or extra fields")
        values[key] = value
    if (
        set(values) != set(MANAGER_FIELDS)
        or re.fullmatch(r"[0-9a-f]{32}", values["InvocationID"]) is None
        or re.fullmatch(r"[1-9][0-9]*", values["UserspaceTimestampMonotonic"])
        is None
    ):
        fail("user manager generation is malformed")
    return ManagerGeneration(
        values["InvocationID"], int(values["UserspaceTimestampMonotonic"])
    )


def query_generation(*, require_running: bool = True) -> Generation:
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
    try:
        rows = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise RebuildError("systemd generation output is not ASCII") from error
    for row in rows:
        key, separator, value = row.partition("=")
        if separator != "=" or key not in GENERATION_FIELDS or key in values:
            fail("systemd generation output has duplicate or extra fields")
        values[key] = value
    if set(values) != set(GENERATION_FIELDS):
        fail("systemd generation output is missing fields")
    _verify_manager_contract(values)
    if (
        values["LoadState"] != "loaded"
        or values["FragmentPath"] != str(guard_unit)
        or values["DropInPaths"]
        or not re.fullmatch(r"[0-9]+", values["MainPID"])
        or not re.fullmatch(r"[0-9]+", values["ActiveEnterTimestampMonotonic"])
    ):
        fail("llm-guard-proxy.service has unsupported authority")
    pid = int(values["MainPID"])
    started = int(values["ActiveEnterTimestampMonotonic"])
    invocation = values["InvocationID"]
    job_text = values["Job"]
    if job_text in {"", "0"}:
        job = None
    elif re.fullmatch(r"[1-9][0-9]*", job_text):
        job = int(job_text)
    else:
        fail("systemd Job authority is malformed")
    running = (values["ActiveState"], values["SubState"]) == ("active", "running")
    if running:
        if (
            pid <= 0
            or started <= 0
            or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
            or values["Result"] != "success"
        ):
            fail("running Guard generation is malformed")
    elif (
        pid != 0
        or (invocation and re.fullmatch(r"[0-9a-f]{32}", invocation) is None)
        or values["ActiveState"]
        not in {"inactive", "failed", "activating", "deactivating"}
    ):
        fail("non-running Guard generation is malformed")
    if require_running and not running:
        fail("llm-guard-proxy.service is not active and running")
    return Generation(
        pid=pid,
        invocation=invocation,
        started=started,
        fragment=values["FragmentPath"],
        result=values["Result"],
        job=job,
    )


def read_small_regular(path: Path, limit: int) -> bytes:
    _deadline_checkpoint("filesystem-read")
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            fail(f"non-regular proc authority: {path}")
        chunks: list[bytes] = []
        remaining_bytes = limit + 1
        while remaining_bytes:
            _deadline_checkpoint("filesystem-read")
            chunk = os.read(descriptor, min(65536, remaining_bytes))
            if not chunk:
                break
            chunks.append(chunk)
            remaining_bytes -= len(chunk)
        payload = b"".join(chunks)
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
    if pid <= 0:
        return 0
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


def verify_running_config(pid: int) -> None:
    if "config" not in fixed_authorities:
        fail("installed Guard config authority is unavailable")
    credential = "/run/credentials/llm-guard-proxy.service/llm-guard-config"
    expected_cmdline = (
        str(service_bin).encode()
        + b"\0--config\0"
        + credential.encode()
        + b"\0--guardian-runtime-dir\0"
        + f"/run/user/{os.getuid()}/gb10-memory-guardian".encode()
        + b"\0"
    )
    if read_small_regular(proc_root / str(pid) / "cmdline", 4096) != expected_cmdline:
        fail("running Guard launch argv differs")
    applied = _open_file_authority(
        "running Guard config",
        proc_root / str(pid) / "root" / credential.lstrip("/"),
        expected_mode=0o400,
        max_bytes=1024 * 1024,
    )
    try:
        if applied.sha256 != fixed_authorities["config"].sha256:
            fail("running Guard config authority differs")
        applied.verify()
    finally:
        applied.close()


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
    mode: int
    uid: int
    nlink: int


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
        if operation_deadline is not None and time.monotonic() >= operation_deadline:
            fail("executable identity hash deadline exhausted")
        chunk = os.pread(
            descriptor, min(1024 * 1024, MAX_EXECUTABLE_BYTES + 1 - offset), offset
        )
        if not chunk:
            break
        offset += len(chunk)
        if offset > MAX_EXECUTABLE_BYTES:
            fail("executable identity exceeded byte bound")
        digest.update(chunk)
        if during_hash is not None and not hook_called:
            during_hash()
            hook_called = True
    fields_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        stat.S_IMODE(before.st_mode),
        before.st_uid,
        before.st_nlink,
    )
    after_hash = os.fstat(descriptor)
    fields_after = (
        after_hash.st_dev,
        after_hash.st_ino,
        after_hash.st_size,
        after_hash.st_mtime_ns,
        after_hash.st_ctime_ns,
        stat.S_IMODE(after_hash.st_mode),
        after_hash.st_uid,
        after_hash.st_nlink,
    )
    if fields_before != fields_after:
        fail("held executable changed during hash")
    build_id = elf_build_id(f"/proc/self/fd/{descriptor}", pass_fd=descriptor)
    after_build_id = os.fstat(descriptor)
    if fields_before != (
        after_build_id.st_dev,
        after_build_id.st_ino,
        after_build_id.st_size,
        after_build_id.st_mtime_ns,
        after_build_id.st_ctime_ns,
        stat.S_IMODE(after_build_id.st_mode),
        after_build_id.st_uid,
        after_build_id.st_nlink,
    ):
        fail("held executable changed during build-ID read")
    return ExecutableIdentity(
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        digest.hexdigest(),
        build_id,
        stat.S_IMODE(before.st_mode),
        before.st_uid,
        before.st_nlink,
    )


def file_identity(path: Path) -> ExecutableIdentity:
    _deadline_checkpoint("filesystem-read")
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return fd_identity(descriptor)
    finally:
        os.close(descriptor)


def open_runtime_executable(
    pid: int, replacement_during_hash: str = ""
) -> tuple[int, str, ExecutableIdentity]:
    _deadline_checkpoint("filesystem-read")
    proc_exe = proc_root / str(pid) / "exe"
    link = os.readlink(proc_exe)
    if link.endswith(" (deleted)") or not _safe_absolute(link):
        fail("running Guard executable link is unsafe")
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


def identity_equal(left: ExecutableIdentity, right: ExecutableIdentity) -> bool:
    return left == right


@dataclass
class Prestate:
    link_target: str | None
    generation: Generation
    boot: str
    proc_start: int
    descriptor: int | None
    restart_descriptor: int | None
    running_link: str
    executable: ExecutableIdentity


def _safe_absolute(value: str) -> bool:
    return (
        isinstance(value, str)
        and "\x00" not in value
        and Path(value).is_absolute()
        and os.path.normpath(value) == value
        and ".." not in Path(value).parts
    )


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
        if not _safe_absolute(link_target):
            fail("service binary prestate target is not a safe absolute path")
    generation = query_generation()
    verify_running_config(generation.pid)
    if generation.job is not None:
        fail("Guard has an existing manager job before prestate")
    current_boot = boot_id()
    starttime = proc_starttime(generation.pid)
    descriptor, running_link, executable = open_runtime_executable(generation.pid)
    restart_descriptor: int | None = None
    if link_target is not None:
        try:
            linked = os.stat(service_bin)
        except OSError:
            os.close(descriptor)
            fail("prior service symlink target is unavailable")
        if not same_object(linked, executable):
            os.close(descriptor)
            fail("prior service symlink does not name the running executable")
    else:
        if not _safe_absolute(running_link):
            os.close(descriptor)
            fail("prior absent-link runtime lacks a stable pathname")
        try:
            restart_descriptor = os.open(
                running_link,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            if fd_identity(restart_descriptor) != executable:
                fail("prior absent-link stable pathname differs from runtime")
        except BaseException:
            if restart_descriptor is not None:
                os.close(restart_descriptor)
            os.close(descriptor)
            raise
    return Prestate(
        link_target,
        generation,
        current_boot,
        starttime,
        descriptor,
        restart_descriptor,
        running_link,
        executable,
    )


def service_link_matches(target: str | None) -> bool:
    _deadline_checkpoint("filesystem-read")
    try:
        metadata = service_bin.lstat()
    except FileNotFoundError:
        return target is None
    return (
        stat.S_ISLNK(metadata.st_mode)
        and target is not None
        and os.readlink(service_bin) == target
    )


def assert_prestate_unchanged(prestate: Prestate) -> None:
    if query_generation() != prestate.generation:
        fail("systemd generation changed before cutover")
    if (
        boot_id() != prestate.boot
        or proc_starttime(prestate.generation.pid) != prestate.proc_start
    ):
        fail("runtime generation changed before cutover")
    current = os.stat(proc_root / str(prestate.generation.pid) / "exe")
    if not same_object(current, prestate.executable):
        fail("runtime executable changed before cutover")
    if (
        os.readlink(proc_root / str(prestate.generation.pid) / "exe")
        != prestate.running_link
    ):
        fail("runtime executable link changed before cutover")
    if not service_link_matches(prestate.link_target):
        fail("service binary link changed before cutover")
    if (
        prestate.descriptor is not None
        and fd_identity(prestate.descriptor) != prestate.executable
    ):
        fail("held prior executable changed before cutover")
    if prestate.link_target is None and (
        prestate.restart_descriptor is None
        or fd_identity(prestate.restart_descriptor) != prestate.executable
        or file_identity(Path(prestate.running_link)) != prestate.executable
    ):
        fail("prior absent-link stable pathname changed before cutover")


def set_service_link(target: str) -> None:
    _deadline_checkpoint("rollback")
    if not _safe_absolute(target):
        fail("refusing unsafe service link target")
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
    _deadline_checkpoint("rollback")
    try:
        metadata = service_bin.lstat()
    except FileNotFoundError:
        fsync_directory(service_bin.parent)
        return
    if not stat.S_ISLNK(metadata.st_mode):
        fail("service binary changed to unsupported type during transaction")
    service_bin.unlink()
    fsync_directory(service_bin.parent)


def _secure_directory(path: Path, *, create: bool = False) -> None:
    _deadline_checkpoint("filesystem-read")
    if create:
        try:
            path.mkdir(mode=0o700, parents=True)
            fsync_directory(path.parent)
        except FileExistsError:
            pass
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail(f"unsafe rebuild state directory: {path}")


def _secure_state_file(path: Path) -> os.stat_result:
    _deadline_checkpoint("filesystem-read")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > STATE_MAX_BYTES
    ):
        fail(f"unsafe rebuild state file: {path}")
    return metadata


def _secure_backup_file(path: Path) -> os.stat_result:
    _deadline_checkpoint("filesystem-read")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_size > MAX_EXECUTABLE_BYTES
    ):
        fail(f"unsafe rollback backup: {path}")
    return metadata


def acquire_rebuild_lock() -> int:
    _secure_directory(receipt_dir, create=True)
    lock_path = receipt_dir / "lock.v1"
    existed = lock_path.exists() or lock_path.is_symlink()
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            fail("unsafe rebuild lock authority")
        if not existed:
            os.fsync(descriptor)
            fsync_directory(receipt_dir)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RebuildError("rebuild lock is already held") from error
        for name in ("rollback", "receipts"):
            _secure_directory(receipt_dir / name, create=True)
        fsync_directory(receipt_dir)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _transaction_dir() -> Path:
    return receipt_dir / "transaction.v1"


def _state_path() -> Path:
    return _transaction_dir() / "state.json"


def _transaction_artifacts() -> list[Path]:
    _deadline_checkpoint("filesystem-read")
    patterns = (
        r"\.transaction\.v1\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}",
        r"\.transaction\.v1\.cleanup\.[1-9][0-9]*\.[0-9a-f]{8}",
    )
    artifacts: list[Path] = []
    for entry in receipt_dir.iterdir():
        _deadline_checkpoint("filesystem-read")
        if entry.name == "transaction.v1" or any(
            re.fullmatch(pattern, entry.name) for pattern in patterns
        ):
            artifacts.append(entry)
    return sorted(artifacts, key=lambda path: path.name)


def _prepare_transaction_namespace() -> bool:
    _deadline_checkpoint("filesystem-read")
    artifacts = _transaction_artifacts()
    if len(artifacts) > 2:
        fail("transaction recovery artifact inventory exceeded its bound")
    canonical = _transaction_dir()
    cleanups = [
        path
        for path in artifacts
        if re.fullmatch(
            r"\.transaction\.v1\.cleanup\.[1-9][0-9]*\.[0-9a-f]{8}",
            path.name,
        )
    ]
    temporaries = [
        path
        for path in artifacts
        if re.fullmatch(
            r"\.transaction\.v1\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}", path.name
        )
    ]
    if len(cleanups) > 1 or len(temporaries) > 1:
        fail("transaction recovery artifact inventory is ambiguous")
    for cleanup in cleanups:
        cleanup_snapshot(cleanup, _directory_identity(cleanup))
    if canonical.exists() or canonical.is_symlink():
        if temporaries:
            fail("canonical transaction conflicts with prepublication temp")
        _secure_directory(canonical)
        entries = []
        for path in canonical.iterdir():
            _deadline_checkpoint("filesystem-read")
            entries.append(path.name)
        entries.sort()
        if "state.json" not in entries or any(
            name != "state.json"
            and re.fullmatch(r"\.state\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}", name)
            is None
            for name in entries
        ):
            fail("transaction contains an unknown publication artifact")
        for name in entries:
            if name != "state.json":
                _secure_state_file(canonical / name)
        return bool(artifacts)
    if not temporaries:
        return bool(artifacts)
    temporary = temporaries[0]
    _secure_directory(temporary)
    entries = []
    for path in temporary.iterdir():
        _deadline_checkpoint("filesystem-read")
        entries.append(path.name)
    entries.sort()
    if not entries:
        fail("empty prepublication transaction retained")
    if entries == ["state.json"]:
        _secure_state_file(temporary / "state.json")
        os.rename(temporary, canonical)
        fsync_directory(receipt_dir)
        return True
    if len(entries) == 1 and re.fullmatch(
        r"\.state\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}", entries[0]
    ):
        _secure_state_file(temporary / entries[0])
        fail("incomplete prepublication transaction retained")
    fail("prepublication transaction contains an unknown artifact")


def _identity_payload(identity: ExecutableIdentity) -> dict[str, object]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "ctime_ns": identity.ctime_ns,
        "sha256": identity.sha256,
        "build_id": identity.build_id,
        "mode": identity.mode,
        "uid": identity.uid,
        "nlink": identity.nlink,
    }


def _generation_payload(generation: Generation, proc_start: int) -> dict[str, object]:
    return {
        "pid": generation.pid,
        "invocation": generation.invocation,
        "started": generation.started,
        "proc_start": proc_start,
        "fragment": generation.fragment,
        "result": generation.result,
        "job": generation.job,
    }


def _manager_generation_payload(generation: ManagerGeneration) -> dict[str, object]:
    return {"invocation": generation.invocation, "started": generation.started}


def _exact_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        fail(f"{label} schema is malformed")
    return cast(dict[str, Any], value)


def _strict_int(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        fail(f"{label} is malformed")
    return value


def _strict_hex(value: object, label: str, length: int = 64) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None
    ):
        fail(f"{label} is malformed")
    return value


def _identity_from_payload(value: object, label: str) -> ExecutableIdentity:
    payload = _exact_keys(
        value,
        {
            "device",
            "inode",
            "size",
            "mtime_ns",
            "ctime_ns",
            "sha256",
            "build_id",
            "mode",
            "uid",
            "nlink",
        },
        label,
    )
    identity = ExecutableIdentity(
        _strict_int(payload["device"], f"{label}.device"),
        _strict_int(payload["inode"], f"{label}.inode", minimum=1),
        _strict_int(payload["size"], f"{label}.size", minimum=1),
        _strict_int(payload["mtime_ns"], f"{label}.mtime_ns"),
        _strict_int(payload["ctime_ns"], f"{label}.ctime_ns"),
        _strict_hex(payload["sha256"], f"{label}.sha256"),
        cast(str, payload["build_id"]),
        _strict_int(payload["mode"], f"{label}.mode"),
        _strict_int(payload["uid"], f"{label}.uid"),
        _strict_int(payload["nlink"], f"{label}.nlink", minimum=1),
    )
    if (
        not isinstance(identity.build_id, str)
        or re.fullmatch(r"[0-9a-f]{8,128}", identity.build_id) is None
        or identity.size > MAX_EXECUTABLE_BYTES
        or identity.uid != os.geteuid()
        or identity.nlink != 1
        or identity.mode not in {0o700, 0o755}
    ):
        fail(f"{label} identity is unsafe")
    return identity


def _generation_from_payload(value: object, label: str) -> tuple[Generation, int]:
    payload = _exact_keys(
        value,
        {"pid", "invocation", "started", "proc_start", "fragment", "result", "job"},
        label,
    )
    job_value = payload["job"]
    if job_value is not None:
        job_value = _strict_int(job_value, f"{label}.job", minimum=1)
    generation = Generation(
        _strict_int(payload["pid"], f"{label}.pid", minimum=1),
        cast(str, payload["invocation"]),
        _strict_int(payload["started"], f"{label}.started", minimum=1),
        cast(str, payload["fragment"]),
        cast(str, payload["result"]),
        cast(int | None, job_value),
    )
    proc_value = _strict_int(payload["proc_start"], f"{label}.proc_start", minimum=1)
    if (
        re.fullmatch(r"[0-9a-f]{32}", generation.invocation) is None
        or generation.fragment != str(guard_unit)
        or generation.result != "success"
    ):
        fail(f"{label} generation is unsafe")
    return generation, proc_value


def _manager_generation_from_payload(value: object) -> ManagerGeneration:
    payload = _exact_keys(
        value, {"invocation", "started"}, "manager generation"
    )
    invocation = payload["invocation"]
    started = _strict_int(
        payload["started"], "manager generation.started", minimum=1
    )
    if not isinstance(invocation, str) or re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
        fail("manager generation.invocation is malformed")
    return ManagerGeneration(invocation, started)


def _validate_tool_receipt(value: object, label: str) -> None:
    payload = _exact_keys(
        value,
        {
            "logical_path",
            "resolved_path",
            "expected_uid",
            "expected_gid",
            "expected_mode",
            "device",
            "inode",
            "size",
            "nlink",
            "mtime_ns",
            "ctime_ns",
            "sha256",
        },
        label,
    )
    for key in ("logical_path", "resolved_path"):
        if not isinstance(payload[key], str) or not _safe_absolute(payload[key]):
            fail(f"{label}.{key} is unsafe")
    for key in (
        "expected_uid",
        "expected_gid",
        "expected_mode",
        "device",
        "inode",
        "size",
        "nlink",
        "mtime_ns",
        "ctime_ns",
    ):
        _strict_int(payload[key], f"{label}.{key}")
    _strict_hex(payload["sha256"], f"{label}.sha256")


def _validate_authorities(value: object) -> dict[str, Any]:
    payload = _exact_keys(
        value,
        {
            "canonical_source_url",
            "canonical_source_ref",
            "source_commit",
            "source_tree",
            "source_archive_sha256",
            "snapshot_content_sha256",
            "source_file_count",
            "source_byte_count",
            "git_config_sha256",
            "metadata_closure_sha256",
            "sandbox_contract_sha256",
            "build_inputs",
            "build_inputs_sha256",
            "cargo_identity_sha256",
            "rustc_identity_sha256",
            "guard_config_sha256",
            "guard_unit_sha256",
            "tool_authorities",
            "python_runtime_authority_sha256",
        },
        "authorities",
    )
    if (
        payload["canonical_source_url"] != source_repo
        or payload["canonical_source_ref"] != source_ref
    ):
        fail("canonical source authority differs")
    for key in ("source_commit", "source_tree"):
        _strict_hex(payload[key], f"authorities.{key}", length=40)
    for key in (
        "source_archive_sha256",
        "snapshot_content_sha256",
        "git_config_sha256",
        "metadata_closure_sha256",
        "sandbox_contract_sha256",
        "build_inputs_sha256",
        "cargo_identity_sha256",
        "rustc_identity_sha256",
        "guard_config_sha256",
        "guard_unit_sha256",
        "python_runtime_authority_sha256",
    ):
        _strict_hex(payload[key], f"authorities.{key}")
    build_inputs = payload["build_inputs"]
    if (
        not isinstance(build_inputs, dict)
        or set(build_inputs)
        != {
            "toolchain",
            "registry_cache",
            "registry_index",
            "gcc_closure",
            "sysroot_lib",
            "sysroot_include",
            "metadata_target",
        }
        or sha256_bytes(
            json.dumps(
                build_inputs,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        )
        != payload["build_inputs_sha256"]
    ):
        fail("build input ledger differs")
    _strict_int(
        payload["source_file_count"], "authorities.source_file_count", minimum=1
    )
    _strict_int(
        payload["source_byte_count"], "authorities.source_byte_count", minimum=1
    )
    tools = payload["tool_authorities"]
    if not isinstance(tools, dict) or set(tools) != TOOL_NAMES:
        fail("tool authority closure is malformed")
    for name, authority in tools.items():
        _validate_tool_receipt(authority, f"tool_authorities.{name}")
    return payload


def validate_wal(value: object) -> dict[str, Any]:
    wal = _exact_keys(
        value,
        {
            "schema",
            "phase",
            "txid",
            "mode",
            "service_bin",
            "guard_config",
            "guard_unit",
            "proc_root",
            "snapshot_root",
            "snapshot_identity",
            "candidate",
            "prior",
            "authorities",
            "manager_generation",
            "manager_job_ids",
            "restart_intent",
            "committed",
            "error",
        },
        "transaction",
    )
    if wal["schema"] != 1 or wal["phase"] not in PHASES:
        fail("transaction version or phase is unsupported")
    _strict_hex(wal["txid"], "transaction.txid", length=32)
    expected_mode = "test-only" if test_only else "production"
    if wal["mode"] != expected_mode or wal["error"] is not None:
        fail("transaction mode or error field is unsafe")
    expected_paths = {
        "service_bin": service_bin,
        "guard_config": guard_config,
        "guard_unit": guard_unit,
        "proc_root": proc_root,
    }
    for key, expected in expected_paths.items():
        if wal[key] != str(expected) or not _safe_absolute(wal[key]):
            fail(f"transaction {key} authority differs")
    snapshot = wal["snapshot_root"]
    if not isinstance(snapshot, str) or not _safe_absolute(snapshot):
        fail("transaction snapshot path is unsafe")
    try:
        Path(snapshot).relative_to(cache_root)
    except ValueError:
        fail("transaction snapshot escaped cache root")
    if not Path(snapshot).name.startswith(".rebuild-input-"):
        fail("transaction snapshot name is unsafe")
    snapshot_identity = _exact_keys(
        wal["snapshot_identity"], {"device", "inode"}, "snapshot identity"
    )
    wal["snapshot_identity"] = (
        _strict_int(snapshot_identity["device"], "snapshot identity.device"),
        _strict_int(snapshot_identity["inode"], "snapshot identity.inode", minimum=1),
    )
    candidate = _exact_keys(wal["candidate"], {"path", "identity"}, "candidate")
    candidate_path = candidate["path"]
    if not isinstance(candidate_path, str) or not _safe_absolute(candidate_path):
        fail("candidate path is unsafe")
    try:
        Path(candidate_path).relative_to(cache_root / "releases")
    except ValueError:
        fail("candidate escaped release root")
    candidate["identity"] = _identity_from_payload(
        candidate["identity"], "candidate.identity"
    )
    prior = _exact_keys(
        wal["prior"],
        {
            "link_target",
            "boot_id",
            "generation",
            "running_link",
            "executable",
            "backup",
        },
        "prior",
    )
    if prior["link_target"] is not None and (
        not isinstance(prior["link_target"], str)
        or not _safe_absolute(prior["link_target"])
    ):
        fail("prior link target is unsafe")
    if (
        not isinstance(prior["running_link"], str)
        or not _safe_absolute(prior["running_link"])
        or not isinstance(prior["boot_id"], str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            prior["boot_id"],
        )
        is None
    ):
        fail("prior runtime authority is malformed")
    generation, proc_value = _generation_from_payload(
        prior["generation"], "prior.generation"
    )
    prior["generation"] = (generation, proc_value)
    prior["executable"] = _identity_from_payload(
        prior["executable"], "prior.executable"
    )
    backup = _exact_keys(prior["backup"], {"path", "identity"}, "prior.backup")
    expected_backup = receipt_dir / "rollback" / f"{prior['executable'].sha256}.bin"
    if backup["path"] != str(expected_backup) or not _safe_absolute(backup["path"]):
        fail("prior backup path is unsafe")
    backup_identity = _identity_from_payload(
        backup["identity"], "prior.backup.identity"
    )
    if (
        backup_identity.sha256,
        backup_identity.build_id,
        backup_identity.size,
        backup_identity.uid,
    ) != (
        prior["executable"].sha256,
        prior["executable"].build_id,
        prior["executable"].size,
        prior["executable"].uid,
    ) or backup_identity.mode != 0o700:
        fail("prior backup identity differs from prior executable")
    backup["identity"] = backup_identity
    _validate_authorities(wal["authorities"])
    wal["manager_generation"] = _manager_generation_from_payload(
        wal["manager_generation"]
    )
    jobs = wal["manager_job_ids"]
    if not isinstance(jobs, list):
        fail("manager job ledger is malformed")
    parsed_jobs = [_strict_int(job, "manager job ID", minimum=1) for job in jobs]
    if len(parsed_jobs) > 32 or len(set(parsed_jobs)) != len(parsed_jobs):
        fail("manager job ledger is unsafe")
    wal["manager_job_ids"] = parsed_jobs
    if not isinstance(wal["restart_intent"], bool):
        fail("restart intent is malformed")
    committed = wal["committed"]
    if wal["phase"] == "committed":
        committed_payload = _exact_keys(
            committed,
            {"generation", "boot_id", "running_link", "executable"},
            "committed",
        )
        committed_payload["generation"] = _generation_from_payload(
            committed_payload["generation"], "committed.generation"
        )
        committed_payload["executable"] = _identity_from_payload(
            committed_payload["executable"], "committed.executable"
        )
        if (
            committed_payload["boot_id"] != prior["boot_id"]
            or committed_payload["running_link"] != candidate_path
            or not identity_equal(
                committed_payload["executable"], candidate["identity"]
            )
        ):
            fail("committed runtime authority differs")
    elif committed is not None:
        fail("precommit transaction has committed evidence")
    return wal


def load_wal() -> dict[str, Any] | None:
    _deadline_checkpoint("filesystem-read")
    transaction = _transaction_dir()
    try:
        _secure_directory(transaction)
    except FileNotFoundError:
        return None
    state_path = _state_path()
    _secure_state_file(state_path)
    descriptor = os.open(
        state_path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        payload = bytearray()
        while len(payload) <= STATE_MAX_BYTES:
            _deadline_checkpoint("filesystem-read")
            chunk = os.read(descriptor, min(65536, STATE_MAX_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    if len(payload) > STATE_MAX_BYTES:
        fail("transaction state exceeds byte bound")
    try:
        value = json.loads(
            bytes(payload),
            object_pairs_hook=_reject_duplicate_json,
            parse_constant=lambda item: fail(f"invalid JSON constant: {item}"),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RebuildError("transaction state JSON is malformed") from error
    return validate_wal(value)


def _state_bytes(wal: dict[str, Any]) -> bytes:
    serializable = _serializable_wal(wal)
    payload = (
        json.dumps(serializable, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("ascii")
    if len(payload) > STATE_MAX_BYTES:
        fail("transaction state exceeds byte bound")
    return payload


def _serializable_wal(wal: dict[str, Any]) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, ExecutableIdentity):
            return _identity_payload(value)
        if isinstance(value, ManagerGeneration):
            return _manager_generation_payload(value)
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], Generation)
        ):
            return _generation_payload(value[0], value[1])
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return cast(dict[str, Any], convert(wal))


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        _deadline_checkpoint("filesystem-write")
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError(errno.EIO, "short state write")
        written += count


def persist_wal(wal: dict[str, Any], *, initial: bool = False) -> None:
    transaction = _transaction_dir()
    if initial:
        temporary_transaction = receipt_dir / (
            f".transaction.v1.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        )
        try:
            temporary_transaction.mkdir(mode=0o700)
        except FileExistsError:
            fail("transaction publication temp already exists")
        transaction = temporary_transaction
    _secure_directory(transaction)
    payload = _state_bytes(wal)
    failure = (
        os.environ.get("LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE", "")
        if test_only and wal["phase"] == "committed"
        else ""
    )
    if failure and failure != "completion-sink":
        raise RebuildError(f"test-only injected committed-state failure: {failure}")
    temporary = transaction / f".state.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, payload)
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            fail("temporary state file authority is unsafe")
        os.fsync(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    if initial:
        os.replace(temporary, transaction / "state.json")
        fsync_directory(transaction)
        os.rename(transaction, _transaction_dir())
        transaction = _transaction_dir()
        fsync_directory(receipt_dir)
    else:
        os.replace(temporary, _state_path())
    _secure_state_file(_state_path())
    fsync_directory(transaction)
    fsync_directory(receipt_dir / "rollback")
    fsync_directory(receipt_dir / "receipts")
    fsync_directory(receipt_dir)


def persist_phase(
    wal: dict[str, Any], phase: str, *, committed: dict[str, Any] | None = None
) -> None:
    if phase not in PHASES:
        fail("invalid transaction phase")
    updated = dict(wal)
    updated["phase"] = phase
    updated["committed"] = committed
    persist_wal(updated)
    wal.clear()
    wal.update(updated)


def _test_boundary(name: str) -> None:
    if not test_only or os.environ.get("LLM_GUARD_REBUILD_TEST_CRASH_POINT") != name:
        return
    marker_text = os.environ.get("LLM_GUARD_REBUILD_TEST_CRASH_MARKER", "")
    if not marker_text or not _safe_absolute(marker_text):
        fail("test-only crash boundary lacks a safe marker")
    marker = Path(marker_text)
    descriptor = os.open(
        marker,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, (name + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(marker.parent)
    while True:
        signal.pause()


def _make_backup(prestate: Prestate) -> tuple[Path, ExecutableIdentity]:
    if prestate.descriptor is None:
        fail("prior executable descriptor is unavailable")
    rollback_dir = receipt_dir / "rollback"
    backup = rollback_dir / f"{prestate.executable.sha256}.bin"
    if backup.exists() or backup.is_symlink():
        _secure_backup_file(backup)
        identity = file_identity(backup)
    else:
        before = fd_identity(prestate.descriptor)
        if before != prestate.executable:
            fail("prior held executable changed before backup")
        atomic_copy_fd(prestate.descriptor, backup, 0o700)
        identity = file_identity(backup)
        if fd_identity(prestate.descriptor) != before:
            fail("prior held executable changed during backup")
        fsync_directory(rollback_dir)
        fsync_directory(receipt_dir)
    if (
        identity.sha256 != prestate.executable.sha256
        or identity.build_id != prestate.executable.build_id
        or identity.size != prestate.executable.size
        or identity.mode != 0o700
        or identity.uid != prestate.executable.uid
        or identity.nlink != 1
    ):
        fail("prior runtime backup proof differs")
    return backup, identity


def _prestate_from_wal(wal: dict[str, Any]) -> Prestate:
    prior = cast(dict[str, Any], wal["prior"])
    generation, proc_value = cast(tuple[Generation, int], prior["generation"])
    return Prestate(
        cast(str | None, prior["link_target"]),
        generation,
        cast(str, prior["boot_id"]),
        proc_value,
        None,
        None,
        cast(str, prior["running_link"]),
        cast(ExecutableIdentity, prior["executable"]),
    )


def _assert_fixed_authorities(wal: dict[str, Any]) -> None:
    authorities = cast(dict[str, Any], wal["authorities"])
    _verify_fixed_authorities()
    if (
        fixed_authorities["config"].sha256 != authorities["guard_config_sha256"]
        or fixed_authorities["unit"].sha256 != authorities["guard_unit_sha256"]
    ):
        fail("installed Guard config or unit differs from transaction authority")
    _verify_all_tools()
    for name, held in held_tools.items():
        if held.receipt() != authorities["tool_authorities"][name]:
            fail(f"held tool differs from transaction authority: {name}")


def _matching_jobs() -> list[ManagerJob]:
    payload = execute(
        [
            require_tool("systemctl"),
            "--user",
            "list-jobs",
            "--output=json",
        ],
        capture=True,
    )
    if len(payload) > 128 * 1024 or b"\x00" in payload:
        fail("systemd job list exceeded its bound")
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_json)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RebuildError("systemd job list is malformed") from error
    if not isinstance(value, list) or len(value) > 256:
        fail("systemd job list root is malformed")
    jobs: list[ManagerJob] = []
    for row in value:
        item = _exact_keys(row, {"job", "unit", "type", "state"}, "systemd job")
        if item["unit"] != UNIT:
            continue
        job = _strict_int(item["job"], "systemd job ID", minimum=1)
        if not all(isinstance(item[key], str) for key in ("unit", "type", "state")):
            fail("systemd job fields are malformed")
        jobs.append(ManagerJob(job, item["type"], item["state"]))
    if len({job.job_id for job in jobs}) != len(jobs):
        fail("systemd job list contains duplicates")
    return sorted(jobs, key=lambda job: job.job_id)


def _sleep_poll() -> None:
    deadline = operation_deadline
    if deadline is None or deadline - time.monotonic() <= MANAGER_POLL_SECONDS:
        fail("transaction deadline exhausted while polling")
    time.sleep(MANAGER_POLL_SECONDS)
    if deadline - time.monotonic() <= MANAGER_POLL_SECONDS:
        fail("transaction deadline exhausted while polling")


def _record_job(wal: dict[str, Any], job: int) -> None:
    jobs = cast(list[int], wal["manager_job_ids"])
    if job not in jobs:
        updated = dict(wal)
        updated["manager_job_ids"] = [*jobs, job]
        persist_wal(updated)
        wal.clear()
        wal.update(updated)


def _set_restart_intent(wal: dict[str, Any], value: bool) -> None:
    if wal["restart_intent"] == value:
        return
    updated = dict(wal)
    updated["restart_intent"] = value
    persist_wal(updated)
    wal.clear()
    wal.update(updated)


def _wal_manager_generation(wal: dict[str, Any]) -> ManagerGeneration:
    value = wal["manager_generation"]
    if isinstance(value, ManagerGeneration):
        return value
    return _manager_generation_from_payload(value)


def _require_same_manager(wal: dict[str, Any]) -> None:
    if query_manager_generation() != _wal_manager_generation(wal):
        fail("user manager generation differs from transaction")


def prove_no_manager_job() -> None:
    generation = query_generation(require_running=False)
    jobs = _matching_jobs()
    if generation.job is not None or jobs:
        fail("Guard has a nonterminal manager job")


def _prove_restarted_runtime(
    *,
    service_link: str,
    running_link: str,
    expected: ExecutableIdentity,
    replaced_generation: Generation,
    replaced_proc_start: int,
    prior_generation: Generation,
    expected_boot: str,
) -> tuple[Generation, int]:
    generation = query_generation()
    verify_running_config(generation.pid)
    current_boot = boot_id()
    starttime = proc_starttime(generation.pid)
    if (
        not generation_is_new(replaced_generation, generation)
        or generation.started <= prior_generation.started
        or starttime == replaced_proc_start
        or current_boot != expected_boot
    ):
        fail("Guard restart did not create the expected fresh runtime generation")
    descriptor, actual_link, identity = open_runtime_executable(generation.pid)
    try:
        if (
            actual_link != running_link
            or identity != expected
            or fd_identity(descriptor) != expected
            or not service_link_matches(service_link)
            or not same_object(os.stat(service_bin), expected)
        ):
            fail("Guard restart did not execute the expected sealed runtime identity")
        if (
            query_generation() != generation
            or boot_id() != current_boot
            or proc_starttime(generation.pid) != starttime
        ):
            fail("Guard restart runtime generation drifted during proof")
    finally:
        os.close(descriptor)
    return generation, starttime


def restart_guard(
    wal: dict[str, Any],
    *,
    service_link: str,
    running_link: str,
    expected: ExecutableIdentity,
    replaced_generation: Generation,
    replaced_proc_start: int,
    prior_generation: Generation,
    expected_boot: str,
    boundary: str | None = None,
) -> tuple[Generation, int]:
    _require_same_manager(wal)
    prove_no_manager_job()
    _set_restart_intent(wal, True)
    execute(
        [
            require_tool("systemctl"),
            "--user",
            "restart",
            "--no-block",
            "--job-mode=fail",
            "--",
            UNIT,
        ]
    )
    _test_boundary("forward-job-dispatched")
    _require_same_manager(wal)
    jobs = _matching_jobs()
    _require_same_manager(wal)
    if len(jobs) > 1:
        fail("restart published multiple matching manager jobs")
    job: int | None = None
    if jobs:
        manager_job = jobs[0]
        if manager_job.job_type != "restart" or manager_job.state not in {
            "running",
            "waiting",
        }:
            fail("restart published an unsupported manager job")
        job = manager_job.job_id
        _record_job(wal, job)
    if boundary is not None:
        _test_boundary(boundary)
    while job is not None:
        _require_same_manager(wal)
        current = _matching_jobs()
        if not current:
            break
        if (
            len(current) != 1
            or current[0].job_id != job
            or current[0].job_type != "restart"
            or current[0].state not in {"running", "waiting"}
        ):
            fail("matching manager job identity drifted")
        _sleep_poll()
    _require_same_manager(wal)
    if query_generation(require_running=False).job is not None:
        fail("systemd still reports a manager job after restart")
    restarted = _prove_restarted_runtime(
        service_link=service_link,
        running_link=running_link,
        expected=expected,
        replaced_generation=replaced_generation,
        replaced_proc_start=replaced_proc_start,
        prior_generation=prior_generation,
        expected_boot=expected_boot,
    )
    _set_restart_intent(wal, False)
    return restarted


def cancel_manager_jobs(wal: dict[str, Any]) -> None:
    _require_same_manager(wal)
    owned = set(cast(list[int], wal["manager_job_ids"]))
    while True:
        _require_same_manager(wal)
        current = _matching_jobs()
        if not current:
            break
        cancellable = [
            job
            for job in current
            if job.job_id in owned
            and job.job_type == "restart"
            and job.state in {"running", "waiting"}
        ]
        foreign = [job for job in current if job not in cancellable]
        if foreign:
            _sleep_poll()
            continue
        for job in cancellable:
            execute(
                [
                    require_tool("systemctl"),
                    "--user",
                    "cancel",
                    str(job.job_id),
                ]
            )
        _sleep_poll()
    if query_generation(require_running=False).job is not None:
        fail("systemd manager job remained after exact cancellation")


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


def generation_is_new(before: Generation, after: Generation) -> bool:
    return (
        after.pid != before.pid
        and after.invocation != before.invocation
        and after.started > before.started
        and after.result == "success"
        and after.job is None
    )


@dataclass
class RuntimeAttestation:
    generation: Generation
    boot: str
    proc_start: int
    descriptor: int
    running_link: str
    executable: ExecutableIdentity


def attest_candidate(
    candidate: Path, expected: ExecutableIdentity
) -> RuntimeAttestation:
    generation = query_generation()
    verify_running_config(generation.pid)
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
    if executable != expected:
        os.close(descriptor)
        fail("running executable identity does not match candidate")
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
    verify_running_config(accepted.generation.pid)
    if (
        boot_id() != accepted.boot
        or proc_starttime(accepted.generation.pid) != accepted.proc_start
    ):
        fail("runtime generation changed during attestation")
    current_proc = os.stat(proc_root / str(accepted.generation.pid) / "exe")
    if not same_object(current_proc, accepted.executable):
        fail("current proc executable no longer names held inode")
    if (
        os.readlink(proc_root / str(accepted.generation.pid) / "exe")
        != accepted.running_link
    ):
        fail("current proc executable link changed during attestation")
    if fd_identity(accepted.descriptor) != accepted.executable:
        fail("held executable changed during attestation")
    if candidate_authority is None:
        fail("candidate executable authority is unavailable")
    candidate_authority.verify()
    if fd_identity(candidate_authority.descriptor) != expected:
        fail("candidate executable changed during attestation")
    if not service_link_matches(str(candidate)) or not same_object(
        os.stat(service_bin), expected
    ):
        fail("service symlink changed during attestation")


def _exact_prior_runtime(wal: dict[str, Any]) -> bool:
    prestate = _prestate_from_wal(wal)
    if boot_id() != prestate.boot:
        return False
    try:
        generation = query_generation()
    except RebuildError:
        return False
    if generation != prestate.generation:
        return False
    try:
        starttime = proc_starttime(generation.pid)
        descriptor, running_link, identity = open_runtime_executable(generation.pid)
    except (OSError, RebuildError):
        return False
    try:
        return (
            starttime == prestate.proc_start
            and running_link == prestate.running_link
            and identity == prestate.executable
            and same_object(os.stat(proc_root / str(generation.pid) / "exe"), identity)
        )
    finally:
        os.close(descriptor)


def _exact_prior_running(wal: dict[str, Any]) -> bool:
    prestate = _prestate_from_wal(wal)
    return service_link_matches(prestate.link_target) and _exact_prior_runtime(wal)


def _remove_backup(wal: dict[str, Any]) -> None:
    _deadline_checkpoint("rollback")
    backup = Path(cast(dict[str, Any], wal["prior"])["backup"]["path"])
    try:
        metadata = backup.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail("rollback backup became unsafe")
    backup.unlink()
    fsync_directory(backup.parent)
    fsync_directory(receipt_dir)


def sweep_orphan_backups() -> None:
    _deadline_checkpoint("filesystem-read")
    rollback = receipt_dir / "rollback"
    entries = []
    for entry in rollback.iterdir():
        _deadline_checkpoint("filesystem-read")
        entries.append(entry)
    if len(entries) > 64:
        fail("rollback backup inventory exceeded its bound")
    for entry in entries:
        if re.fullmatch(r"[0-9a-f]{64}\.bin", entry.name) is None:
            fail("rollback directory contains an unknown entry")
        _secure_backup_file(entry)
        entry.unlink()
    if entries:
        fsync_directory(rollback)
        fsync_directory(receipt_dir)


def _snapshot_wal_identity(wal: dict[str, Any]) -> tuple[int, int]:
    value = wal["snapshot_identity"]
    if isinstance(value, tuple):
        return cast(tuple[int, int], value)
    payload = cast(dict[str, int], value)
    return payload["device"], payload["inode"]


def cleanup_transaction(wal: dict[str, Any]) -> None:
    cleanup_snapshot(
        Path(cast(str, wal["snapshot_root"])),
        _snapshot_wal_identity(wal),
    )
    _remove_backup(wal)
    transaction = _transaction_dir()
    cleanup_snapshot(transaction, _directory_identity(transaction))


def rollback_wal(wal: dict[str, Any]) -> None:
    log("ROLLBACK_BEGIN=1")
    _deadline_checkpoint("rollback")
    _assert_fixed_authorities(wal)
    cancel_manager_jobs(wal)
    prestate = _prestate_from_wal(wal)
    if _exact_prior_running(wal):
        health_check()
        if not _exact_prior_running(wal):
            fail("exact prior generation drifted during rollback health check")
        cleanup_transaction(wal)
        log("ROLLBACK_RESTORED=1")
        return
    stable_prior_fd: int | None = None
    if prestate.link_target is None:
        stable_prior_fd = os.open(
            prestate.running_link,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        if fd_identity(stable_prior_fd) != prestate.executable:
            os.close(stable_prior_fd)
            fail("prior absent-link stable pathname is no longer exact")
    if _exact_prior_runtime(wal):
        try:
            if prestate.link_target is None:
                remove_service_link()
            else:
                set_service_link(prestate.link_target)
            if not _exact_prior_running(wal):
                fail("exact prior runtime or link drifted during rollback")
            health_check()
            cleanup_transaction(wal)
            if stable_prior_fd is not None and (
                fd_identity(stable_prior_fd) != prestate.executable
                or file_identity(Path(prestate.running_link)) != prestate.executable
            ):
                fail("prior executable is not restartable after cleanup")
            log("ROLLBACK_RESTORED=1")
            return
        finally:
            if stable_prior_fd is not None:
                os.close(stable_prior_fd)
    failed = query_generation(require_running=False)
    failed_proc = proc_starttime(failed.pid) if failed.pid else 0
    if prestate.link_target is None:
        set_service_link(prestate.running_link)
    else:
        prior_target = Path(prestate.link_target)
        if file_identity(prior_target) != prestate.executable:
            fail("prior-present rollback target no longer names exact prior object")
        set_service_link(prestate.link_target)
    if _exact_prior_running(wal):
        health_check()
        if not _exact_prior_running(wal):
            fail("exact prior generation drifted during rollback health check")
        cleanup_transaction(wal)
        log("ROLLBACK_RESTORED=1")
        return
    backup_record = cast(dict[str, Any], cast(dict[str, Any], wal["prior"])["backup"])
    backup = Path(cast(str, backup_record["path"]))
    backup_identity = cast(ExecutableIdentity, backup_record["identity"])
    if file_identity(backup) != backup_identity:
        fail("rollback backup identity changed")
    try:
        restored_generation, restored_start = restart_guard(
            wal,
            service_link=(
                prestate.running_link
                if prestate.link_target is None
                else prestate.link_target
            ),
            running_link=prestate.running_link,
            expected=prestate.executable,
            replaced_generation=failed,
            replaced_proc_start=failed_proc,
            prior_generation=prestate.generation,
            expected_boot=prestate.boot,
        )
        health_check()
        descriptor, running_link, identity = open_runtime_executable(
            restored_generation.pid
        )
        try:
            if identity != prestate.executable:
                fail("rollback runtime did not execute the exact prior object")
            if running_link != prestate.running_link:
                fail("rollback runtime link differs from exact prior authority")
            if query_generation() != restored_generation:
                fail("rollback generation drifted after health")
            if proc_starttime(restored_generation.pid) != restored_start:
                fail("rollback proc starttime drifted after health")
            current = os.stat(proc_root / str(restored_generation.pid) / "exe")
            if not same_object(current, identity) or fd_identity(descriptor) != identity:
                fail("rollback held executable proof drifted")
        finally:
            os.close(descriptor)
        if stable_prior_fd is not None and (
            fd_identity(stable_prior_fd) != prestate.executable
            or file_identity(Path(prestate.running_link)) != prestate.executable
        ):
            fail("prior absent-link stable pathname drifted during rollback")
        if prestate.link_target is None:
            remove_service_link()
        if not service_link_matches(prestate.link_target):
            fail("rollback did not restore exact prior service link state")
        cleanup_transaction(wal)
        if stable_prior_fd is not None and (
            fd_identity(stable_prior_fd) != prestate.executable
            or file_identity(Path(prestate.running_link)) != prestate.executable
        ):
            fail("prior executable is not restartable after cleanup")
        log("ROLLBACK_RESTORED=1")
    finally:
        if stable_prior_fd is not None:
            os.close(stable_prior_fd)


def _verify_committed(wal: dict[str, Any], *, cancel_jobs: bool = True) -> None:
    _assert_fixed_authorities(wal)
    if cancel_jobs:
        cancel_manager_jobs(wal)
    else:
        prove_no_manager_job()
    committed = cast(dict[str, Any], wal["committed"])
    generation, proc_value = cast(tuple[Generation, int], committed["generation"])
    candidate_record = cast(dict[str, Any], wal["candidate"])
    candidate = Path(cast(str, candidate_record["path"]))
    expected = cast(ExecutableIdentity, candidate_record["identity"])
    committed_expected = cast(ExecutableIdentity, committed["executable"])
    if candidate_authority is not None:
        candidate_authority.verify()
    if (
        not service_link_matches(str(candidate))
        or candidate_authority is None
        or fd_identity(candidate_authority.descriptor) != expected
        or committed_expected != expected
        or boot_id() != committed["boot_id"]
        or query_generation() != generation
        or proc_starttime(generation.pid) != proc_value
    ):
        fail("committed candidate generation cannot be proven")
    descriptor, running_link, identity = open_runtime_executable(generation.pid)
    try:
        if (
            running_link != committed["running_link"]
            or identity != expected
            or identity != committed_expected
            or fd_identity(descriptor) != expected
        ):
            fail("committed executable proof differs")
        health_check()
        if (
            query_generation() != generation
            or proc_starttime(generation.pid) != proc_value
        ):
            fail("committed generation drifted during recovery")
    finally:
        os.close(descriptor)


def archive_committed(wal: dict[str, Any]) -> tuple[Path, str]:
    _deadline_checkpoint("rollback")
    if wal["phase"] != "committed":
        fail("only committed state may be archived")
    cleanup_snapshot(
        Path(cast(str, wal["snapshot_root"])),
        _snapshot_wal_identity(wal),
    )
    _remove_backup(wal)
    receipts = receipt_dir / "receipts"
    destination = receipts / cast(str, wal["txid"])
    if destination.exists() or destination.is_symlink():
        fail("committed receipt destination already exists")
    transaction = _transaction_dir()
    for entry in transaction.iterdir():
        _deadline_checkpoint("filesystem-read")
        if entry.name == "state.json":
            continue
        if re.fullmatch(
            r"\.state\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}", entry.name
        ) is None:
            fail("transaction contains an unknown publication artifact")
        _secure_state_file(entry)
        _deadline_checkpoint("filesystem-write")
        entry.unlink()
    fsync_directory(transaction)
    os.rename(transaction, destination)
    _secure_directory(destination)
    state_path = destination / "state.json"
    _secure_state_file(state_path)
    fsync_directory(destination)
    fsync_directory(receipts)
    fsync_directory(receipt_dir)
    return state_path, sha256_file(state_path)


def recover_stale_transaction(wal: dict[str, Any]) -> None:
    phase = cast(str, wal["phase"])
    if phase == "committed":
        _verify_committed(wal)
        archive_committed(wal)
    elif phase == "prestate" and _exact_prior_running(wal):
        _assert_fixed_authorities(wal)
        prove_no_manager_job()
        cleanup_transaction(wal)
    else:
        if phase == "prestate":
            persist_phase(wal, "mutated")
        rollback_wal(wal)
    print(
        f"{RECOVERY_COMPLETE} phase={phase} txid={wal['txid']}",
        flush=True,
    )


def _build_wal(
    prestate: Prestate,
    backup: Path,
    backup_identity: ExecutableIdentity,
    snapshot: Path,
    snapshot_identity: tuple[int, int],
    manager_generation: ManagerGeneration,
    candidate: Path,
    candidate_identity: ExecutableIdentity,
    authorities: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": 1,
        "phase": "prestate",
        "txid": secrets.token_hex(16),
        "mode": "test-only" if test_only else "production",
        "service_bin": str(service_bin),
        "guard_config": str(guard_config),
        "guard_unit": str(guard_unit),
        "proc_root": str(proc_root),
        "snapshot_root": str(snapshot),
        "snapshot_identity": {
            "device": snapshot_identity[0],
            "inode": snapshot_identity[1],
        },
        "candidate": {
            "path": str(candidate),
            "identity": candidate_identity,
        },
        "prior": {
            "link_target": prestate.link_target,
            "boot_id": prestate.boot,
            "generation": (prestate.generation, prestate.proc_start),
            "running_link": prestate.running_link,
            "executable": prestate.executable,
            "backup": {
                "path": str(backup),
                "identity": backup_identity,
            },
        },
        "authorities": authorities,
        "manager_generation": _manager_generation_payload(manager_generation),
        "manager_job_ids": [],
        "restart_intent": False,
        "committed": None,
        "error": None,
    }


def _test_deadline(name: str, default: float) -> float:
    if not test_only:
        return default
    value = os.environ.get(name)
    if value is None:
        return default
    if re.fullmatch(r"[1-9][0-9]*(?:\.[0-9]+)?", value) is None:
        fail(f"invalid test-only deadline: {name}")
    parsed = float(value)
    if not 0.5 <= parsed <= default:
        fail(f"test-only deadline outside bound: {name}")
    return parsed


def final_commit_bracket(
    source: SourceBundle,
    accepted: RuntimeAttestation,
    candidate: Path,
    candidate_identity: ExecutableIdentity,
    config_sha256: str,
    unit_sha256: str,
    cargo_identity: bytes,
    rustc_identity: bytes,
) -> None:
    source.verify()
    _verify_fixed_authorities()
    if (
        config_sha256 != fixed_authorities["config"].sha256
        or unit_sha256 != fixed_authorities["unit"].sha256
    ):
        fail("installed Guard config or unit changed before publication")
    if cargo_identity != capture_tool("cargo", "--version", "--verbose"):
        fail("Cargo toolchain identity changed before publication")
    if rustc_identity != capture_tool("rustc", "-vV"):
        fail("rustc toolchain identity changed before publication")
    _verify_all_tools()
    final_runtime_attestation(accepted, candidate, candidate_identity)
    prove_no_manager_job()


def signal_handler(signum: int, _frame: object) -> None:
    raise RebuildError(f"received signal {signum}")


for caught_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(caught_signal, signal_handler)


def run_transaction() -> int:
    global candidate_authority, operation_deadline, rollback_logging

    lock_descriptor: int | None = None
    snapshot_root: Path | None = None
    snapshot_root_identity: tuple[int, int] | None = None
    source_bundle: SourceBundle | None = None
    prestate: Prestate | None = None
    accepted: RuntimeAttestation | None = None
    wal: dict[str, Any] | None = None
    lock_acquired = False
    try:
        lock_descriptor = acquire_rebuild_lock()
        lock_acquired = True
        operation_deadline = time.monotonic() + _test_deadline(
            "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS", RECOVERY_SECONDS
        )
        recovered_namespace = _prepare_transaction_namespace()
        stale = load_wal()
        if stale is not None:
            authorities = cast(dict[str, Any], stale["authorities"])
            if (
                _runtime_python_authority_sha256()
                != authorities["python_runtime_authority_sha256"]
            ):
                fail("Python runtime authority differs from stale transaction")
            _open_fixed_authorities(authorities)
            candidate_record = cast(dict[str, Any], stale["candidate"])
            candidate_identity = cast(ExecutableIdentity, candidate_record["identity"])
            candidate_authority = _open_file_authority(
                "candidate executable",
                Path(cast(str, candidate_record["path"])),
                expected_mode=candidate_identity.mode,
                max_bytes=MAX_EXECUTABLE_BYTES,
            )
            if candidate_authority.sha256 != candidate_identity.sha256:
                fail("candidate executable authority differs from transaction")
            _open_all_tools()
            recover_stale_transaction(stale)
            return 75
        if recovered_namespace:
            print(f"{RECOVERY_COMPLETE} phase=cleanup txid=unknown", flush=True)
            return 75

        operation_deadline = time.monotonic() + _test_deadline(
            "LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS", FORWARD_SECONDS
        )
        sweep_orphan_backups()
        _open_fixed_authorities()
        _open_all_tools()
        config_sha_initial = fixed_authorities["config"].sha256
        unit_sha_initial = fixed_authorities["unit"].sha256
        cargo_identity_initial = capture_tool("cargo", "--version", "--verbose")
        rustc_identity_initial = capture_tool("rustc", "-vV")
        _validate_reviewed_tool_versions(cargo_identity_initial, rustc_identity_initial)
        cargo_identity_sha = sha256_bytes(cargo_identity_initial)
        rustc_identity_sha = sha256_bytes(rustc_identity_initial)

        prestate = snapshot_prestate()
        manager_generation = query_manager_generation()
        prove_no_manager_job()
        ensure_private_directory(cache_root)
        snapshot_root = Path(tempfile.mkdtemp(prefix=".rebuild-input-", dir=cache_root))
        snapshot_root.chmod(0o700)
        snapshot_root_identity = _directory_identity(snapshot_root)
        source_bundle = prepare_canonical_source(snapshot_root)
        build_bundle = build_sandboxed_candidate(source_bundle)
        candidate_authority = build_bundle.authority
        candidate = build_bundle.candidate
        candidate_identity = build_bundle.identity
        source_bundle.verify()
        assert_prestate_unchanged(prestate)
        prove_no_manager_job()
        if (
            config_sha_initial != fixed_authorities["config"].sha256
            or unit_sha_initial != fixed_authorities["unit"].sha256
        ):
            fail("installed Guard config or unit changed before cutover")
        _verify_fixed_authorities()
        if cargo_identity_initial != capture_tool("cargo", "--version", "--verbose"):
            fail("Cargo toolchain identity changed before cutover")
        if rustc_identity_initial != capture_tool("rustc", "-vV"):
            fail("rustc toolchain identity changed before cutover")
        candidate_authority.verify()
        if query_manager_generation() != manager_generation:
            fail("user manager generation changed before cutover")

        backup, backup_identity = _make_backup(prestate)
        tool_receipts = {
            name: held.receipt() for name, held in sorted(held_tools.items())
        }
        authorities: dict[str, Any] = {
            "canonical_source_url": source_repo,
            "canonical_source_ref": source_ref,
            "source_commit": source_bundle.commit,
            "source_tree": source_bundle.tree,
            "source_archive_sha256": source_bundle.archive_sha256,
            "snapshot_content_sha256": source_bundle.tree_ledger_sha256,
            "source_file_count": source_bundle.file_count,
            "source_byte_count": source_bundle.byte_count,
            "git_config_sha256": source_bundle.config_sha256,
            "metadata_closure_sha256": build_bundle.metadata_closure_sha256,
            "sandbox_contract_sha256": build_bundle.sandbox_contract_sha256,
            "build_inputs_sha256": sha256_bytes(
                json.dumps(
                    build_bundle.inputs,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("ascii")
            ),
            "build_inputs": build_bundle.inputs,
            "cargo_identity_sha256": cargo_identity_sha,
            "rustc_identity_sha256": rustc_identity_sha,
            "guard_config_sha256": config_sha_initial,
            "guard_unit_sha256": unit_sha_initial,
            "tool_authorities": tool_receipts,
            "python_runtime_authority_sha256": _runtime_python_authority_sha256(),
        }
        wal = _build_wal(
            prestate,
            backup,
            backup_identity,
            snapshot_root,
            snapshot_root_identity,
            manager_generation,
            candidate,
            candidate_identity,
            authorities,
        )
        persist_wal(wal, initial=True)
        _test_boundary("prestate-fsynced")
        persist_phase(wal, "mutated")
        _test_boundary("mutated-fsynced")

        if not service_link_matches(str(candidate)):
            set_service_link(str(candidate))
        _test_boundary("link-renamed")
        needs_restart = (
            prestate.executable.device,
            prestate.executable.inode,
        ) != (candidate_identity.device, candidate_identity.inode)
        restarted_generation: Generation | None = None
        restarted_start = 0
        if needs_restart:
            restarted_generation, restarted_start = restart_guard(
                wal,
                service_link=str(candidate),
                running_link=str(candidate),
                expected=candidate_identity,
                replaced_generation=prestate.generation,
                replaced_proc_start=prestate.proc_start,
                prior_generation=prestate.generation,
                expected_boot=prestate.boot,
                boundary="forward-job",
            )
        health_check()
        accepted = attest_candidate(candidate, candidate_identity)
        if needs_restart and (
            accepted.generation != restarted_generation
            or accepted.proc_start != restarted_start
            or accepted.boot != prestate.boot
        ):
            fail("systemd generation changed during health check")
        if not needs_restart and (
            accepted.generation != prestate.generation
            or accepted.proc_start != prestate.proc_start
            or accepted.boot != prestate.boot
        ):
            fail("unchanged candidate runtime generation drifted")

        final_commit_bracket(
            source_bundle,
            accepted,
            candidate,
            candidate_identity,
            config_sha_initial,
            unit_sha_initial,
            cargo_identity_initial,
            rustc_identity_initial,
        )
        _test_boundary("candidate-attested")
        final_commit_bracket(
            source_bundle,
            accepted,
            candidate,
            candidate_identity,
            config_sha_initial,
            unit_sha_initial,
            cargo_identity_initial,
            rustc_identity_initial,
        )
        committed = {
            "generation": (accepted.generation, accepted.proc_start),
            "boot_id": accepted.boot,
            "running_link": accepted.running_link,
            "executable": accepted.executable,
        }
        persist_phase(wal, "committed", committed=committed)
        _test_boundary("committed-fsynced")
        _, receipt_digest = archive_committed(wal)
        snapshot_root = None
        snapshot_root_identity = None
        _verify_committed(wal, cancel_jobs=False)
        if (
            test_only
            and os.environ.get("LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE")
            == "completion-sink"
        ):
            raise OSError(errno.EPIPE, "test-only completion sink failure")
        marker = TEST_COMPLETE if test_only else PRODUCTION_COMPLETE
        print(f"{marker} receipt_sha256={receipt_digest}", flush=True)
        return 0
    except Exception as error:  # noqa: BLE001 - transaction boundary is fail-closed.
        rollback_logging = True
        rollback_error: Exception | None = None
        if lock_acquired and wal is not None and wal.get("phase") != "committed":
            operation_deadline = time.monotonic() + _test_deadline(
                "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS", RECOVERY_SECONDS
            )
            previous_handlers = {
                signum: signal.signal(signum, signal.SIG_IGN)
                for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }
            try:
                rollback_wal(wal)
            except Exception as failure:  # noqa: BLE001 - retain WAL and report.
                rollback_error = failure
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
        _report_error(str(error))
        if rollback_error is not None:
            _report_error(f"rollback blocked; transaction retained: {rollback_error}")
        return 1
    finally:
        if accepted is not None:
            os.close(accepted.descriptor)
        if prestate is not None and prestate.descriptor is not None:
            os.close(prestate.descriptor)
        if prestate is not None and prestate.restart_descriptor is not None:
            os.close(prestate.restart_descriptor)
        if source_bundle is not None:
            source_bundle.close()
        if candidate_authority is not None:
            candidate_authority.close()
            candidate_authority = None
        cleanup_snapshot(snapshot_root, snapshot_root_identity)
        for held in reversed(list(held_tools.values())):
            held.close()
        held_tools.clear()
        for authority in fixed_authorities.values():
            authority.close()
        fixed_authorities.clear()
        operation_deadline = None
        if lock_descriptor is not None:
            os.close(lock_descriptor)


raise SystemExit(run_transaction())
