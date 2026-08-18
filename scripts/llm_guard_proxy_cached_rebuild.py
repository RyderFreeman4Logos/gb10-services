from __future__ import annotations

import errno
import ctypes
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import stat
import struct
import sys
import tarfile
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

__all__: list[str] = []

EXPECTED_BOUNDED_PROCESS_SHA256 = (
    "248762c2fdc73fdf54914fc5a20c2292bcc90430e59523c5767409ebf0f4c230"
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
run_scoped = _bounded_module.scoped_command
ScopePolicy = _bounded_module.ScopePolicy

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
FRAME_MAGIC = b"GB10ART1"
FRAME_HEADER_BYTES = 16 * 1024 * 1024
FETCH_TMPFS_BYTES = 384 * 1024 * 1024
FETCH_TMP_BYTES = 64 * 1024 * 1024
BUILD_TARGET_TMPFS_BYTES = 6 * 1024 * 1024 * 1024
BUILD_TMP_BYTES = 512 * 1024 * 1024
HOST_WRITE_BUDGET_BYTES = 576 * 1024 * 1024
HOST_FREE_FLOOR_BYTES = 8 * 1024 * 1024 * 1024
FETCH_MEMORY_MIN_BYTES = 7 * 1024 * 1024 * 1024
BUILD_MEMORY_MIN_BYTES = 16 * 1024 * 1024 * 1024
SCOPE_UNIT_TOKEN = "@GB10_SCOPE_UNIT@"
SCRATCH_MARKER = ".gb10-rebuild-scratch.v1"
SCRATCH_DELETE_PREFIX = ".gb10-rebuild-scratch-delete.v1."
SCRATCH_DELETE_SLOT = ".gb10-rebuild-scratch-delete-slot.v1"
LEAF_PARK_DIRECTORY = ".gb10-host-write-leaf-park.v1"
LEAF_PARK_SLOT = ".gb10-host-write-leaf-park-slot.v1"
SCRATCH_DIRECT_PATTERN = re.compile(r"\.rebuild-input-([0-9a-f]{32})")
SCRATCH_PREPUBLICATION_PATTERN = re.compile(
    r"\.(\.rebuild-input-[0-9a-f]{32})\.publish\.[1-9][0-9]*\.[0-9a-f]{8}"
)
SCRATCH_TOMBSTONE_PATTERN = re.compile(
    r"\.\.(rebuild-input-[0-9a-f]{32})\.cleanup\.[1-9][0-9]*\.[0-9a-f]{8}"
)
SCRATCH_DELETE_PATTERN = re.compile(
    re.escape(SCRATCH_DELETE_PREFIX)
    + r"([0-9a-f]{64})\.([0-9a-f]+)\.([0-9a-f]+)\.([0-9a-f]+)\.([0-9a-f]+)\.([0-9a-f]{16})"
)
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_LIBC_RENAMEAT2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
if _LIBC_RENAMEAT2 is not None:
    _LIBC_RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _LIBC_RENAMEAT2.restype = ctypes.c_int
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


def atomic_copy_fd(
    source_fd: int, destination: Path, mode: int, budget: HostWriteBudget
) -> None:
    budget.copy_fd(source_fd, destination, mode)


TOOL_NAMES = {
    "ar",
    "as",
    "bwrap",
    "ca_cert",
    "cargo",
    "cc",
    "curl",
    "git",
    "git_remote_https",
    "ionice",
    "ld",
    "nice",
    "nsswitch",
    "hosts",
    "prlimit",
    "python",
    "readelf",
    "resolv_conf",
    "rustc",
    "scoped_worker",
    "systemd_run",
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
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NONBLOCK
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise RebuildError(f"unsafe {label} authority") from error
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
        "systemd_run": root(
            "/usr/bin/systemd-run",
            "20886766c7aec37baf11daba7974cba999eedec181d15ac1e269cbd122d0a2f8",
        ),
        "prlimit": root(
            "/usr/bin/prlimit",
            "663634070079386b7401ccc9fb92522ec3ece10f07f84a83fe96ec3ecb0bc74b",
        ),
        "python": root(
            "/usr/bin/python3.11",
            "6d972cf21be56fe3c947ab6ba257ff8d08c342dd2714442986791bd9a6dfabfe",
        ),
        "scoped_worker": ToolSpec(
            "/home/obj/.local/bin/llm_guard_proxy_scoped_worker.py",
            "/home/obj/.local/bin/llm_guard_proxy_scoped_worker.py",
            1001,
            1001,
            0o644,
            "a1538f69cdb2fe334fe3d8fe1bb5e67e2dcbed86da751ddb7707f8a844a0b50b",
        ),
        "ca_cert": root(
            "/etc/ssl/certs/ca-certificates.crt",
            "b543499f6fde79f360c7c9da22b74d33230563dad47cd287d8bbb50e2132418b",
            mode=0o644,
        ),
        "resolv_conf": root(
            "/etc/resolv.conf",
            "ccc451bf09f40aa94d6ff7bc68d662473c6d5c8d815d111f9a02a12e65a814ce",
            mode=0o644,
        ),
        "nsswitch": root(
            "/etc/nsswitch.conf",
            "cf4b86500454b477d4a15e93f28d3cda0ec4bd3649967cf5c2678a18f521993c",
            mode=0o644,
        ),
        "hosts": root(
            "/etc/hosts",
            "ac9b3caaa1e5d78e40bef1be61989425d9aa57869ba6005472ff9c9b4ef50fc3",
            mode=0o644,
        ),
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
        "cgroup_root",
        "registry_cache",
        "registry_index",
        "gcc_root",
        "sysroot_lib",
        "sysroot_include",
        "python_stdlib",
        "test_free_bytes",
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
        "python_stdlib",
        "cgroup_root",
    ):
        if not isinstance(parsed[key], str) or not Path(parsed[key]).is_absolute():
            fail(f"test-only authority path is invalid: {key}")
    if (
        not isinstance(parsed["test_free_bytes"], int)
        or isinstance(parsed["test_free_bytes"], bool)
        or parsed["test_free_bytes"] < 0
    ):
        fail("test-only free-space authority is invalid")
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
    cgroup_root = Path("/sys/fs/cgroup")
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
    python_stdlib = Path("/usr/lib/python3.11")
    test_free_bytes: int | None = None
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
    python_stdlib = Path(authority_config["python_stdlib"])
    cgroup_root = Path(authority_config["cgroup_root"])
    test_free_bytes = cast(int, authority_config["test_free_bytes"])
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


def _scope_policy(phase: str) -> Any:
    if phase == "fetch":
        return ScopePolicy(
            phase=phase,
            memory_high=768 * 1024 * 1024,
            memory_max=1024 * 1024 * 1024,
            tasks_max=32,
            cpu_percent=100,
            fsize_bytes=160 * 1024 * 1024,
            min_mem_available=FETCH_MEMORY_MIN_BYTES,
            runtime_seconds=300,
            proc_root=proc_root,
            cgroup_root=cgroup_root,
        )
    if phase not in {"metadata", "build"}:
        fail("unknown scoped phase")
    return ScopePolicy(
        phase=phase,
        memory_high=8 * 1024 * 1024 * 1024,
        memory_max=10 * 1024 * 1024 * 1024,
        tasks_max=768,
        cpu_percent=200,
        fsize_bytes=128 * 1024 * 1024,
        min_mem_available=BUILD_MEMORY_MIN_BYTES,
        runtime_seconds=300 if phase == "metadata" else 1800,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
    )


def _bounded_primary_error(error: BaseException) -> str:
    line = str(error).splitlines()[0] if str(error) else type(error).__name__
    return "".join(character if character.isprintable() else "?" for character in line)[:512]


def execute_scoped(
    phase: str,
    sandbox_arguments: list[str],
    *,
    max_output_bytes: int,
    pass_fds: tuple[int, ...] = (),
) -> bytes:
    deadline = operation_deadline
    if deadline is None:
        fail("scoped command invoked without a transaction deadline")
    budget = deadline - time.monotonic()
    if budget <= 0:
        fail("transaction scoped-command deadline exhausted")
    policy = _scope_policy(phase)
    log(f"+ verified hard-containment scope phase={phase}")
    output: bytes | None = None
    primary_error: BaseException | None = None
    try:
        output = run_scoped(
            sandbox_arguments,
            policy,
            systemd_run=require_tool("systemd_run"),
            systemctl=require_tool("systemctl"),
            nice=require_tool("nice"),
            ionice=require_tool("ionice"),
            prlimit=require_tool("prlimit"),
            timeout=budget,
            deadline=deadline,
            env=child_env,
            pass_fds=tuple(sorted(set(_tool_fds() + pass_fds))),
            max_output_bytes=max_output_bytes,
            cleanup_reserve=min(8.0, max(2.0, budget / 2)),
        )
    except BaseException as error:
        primary_error = error
    tool_error: BaseException | None = None
    try:
        _verify_all_tools()
    except BaseException as error:
        tool_error = error
    if primary_error is not None:
        primary_reason = (
            f"hard-contained phase failed: {phase}; "
            f"reason={_bounded_primary_error(primary_error)}"
        )
        if tool_error is not None:
            raise RebuildError(
                f"{primary_reason}; tool verification="
                f"{_bounded_primary_error(tool_error)}"
            ) from primary_error
        if isinstance(primary_error, RuntimeError):
            raise RebuildError(primary_reason) from primary_error
        raise primary_error
    if tool_error is not None:
        raise tool_error
    if output is None:
        fail("hard-contained phase returned no output")
    return output


def capture_tool(name: str, *arguments: str) -> bytes:
    return execute([require_tool(name), *arguments], capture=True)


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
                authorized_external = label == "sandbox Python standard library" and (
                    (
                        relative == "sitecustomize.py"
                        and target == "/etc/python3.11/sitecustomize.py"
                    )
                    or (
                        relative
                        == "config-3.11-x86_64-linux-gnu/libpython3.11.so"
                        and target == "../../x86_64-linux-gnu/libpython3.11.so.1"
                    )
                )
                if (target.startswith("/") or ".." in parts) and not authorized_external:
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


@dataclass(frozen=True)
class _LeafAuthority:
    device: int
    inode: int
    kind: int
    uid: int
    nlink: int
    mode: int


def _leaf_authority(metadata: os.stat_result) -> _LeafAuthority:
    return _LeafAuthority(
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        metadata.st_nlink,
        stat.S_IMODE(metadata.st_mode),
    )


def _leaf_matches(
    authority: _LeafAuthority, expected: _LeafAuthority | tuple[int, int]
) -> bool:
    return authority == expected if isinstance(expected, _LeafAuthority) else (
        authority.device,
        authority.inode,
    ) == expected


def _open_leaf_at(parent_fd: int, name: str) -> tuple[int, os.stat_result]:
    if not name or Path(name).name != name:
        fail("host write leaf name is unsafe")
    descriptor = os.open(
        name,
        os.O_PATH | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    return descriptor, os.fstat(descriptor)


def _renameat2_between(
    source_parent_fd: int,
    source: str,
    destination_parent_fd: int,
    destination: str,
    flags: int,
) -> None:
    if _LIBC_RENAMEAT2 is None:
        fail("scratch exact-leaf rename is unavailable")
    result = _LIBC_RENAMEAT2(
        source_parent_fd,
        os.fsencode(source),
        destination_parent_fd,
        os.fsencode(destination),
        flags,
    )
    if result == 0:
        return
    number = ctypes.get_errno()
    error = OSError(number, os.strerror(number))
    if number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise RebuildError("scratch exact-leaf rename is unavailable") from error
    raise RebuildError(
        f"scratch exact-leaf rename failed: {source!r} -> {destination!r}"
    ) from error


def _renameat2(parent_fd: int, source: str, destination: str, flags: int) -> None:
    _renameat2_between(parent_fd, source, parent_fd, destination, flags)


def _rename_exchange_between(
    source_parent_fd: int,
    source: str,
    destination_parent_fd: int,
    destination: str,
) -> None:
    _renameat2_between(
        source_parent_fd,
        source,
        destination_parent_fd,
        destination,
        _RENAME_EXCHANGE,
    )


def _rename_noreplace_between(
    source_parent_fd: int,
    source: str,
    destination_parent_fd: int,
    destination: str,
) -> None:
    _renameat2_between(
        source_parent_fd,
        source,
        destination_parent_fd,
        destination,
        _RENAME_NOREPLACE,
    )


def _rename_exchange(parent_fd: int, source: str, destination: str) -> None:
    _rename_exchange_between(parent_fd, source, parent_fd, destination)


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    _rename_noreplace_between(parent_fd, source, parent_fd, destination)


@dataclass
class HostWriteBudget:
    root: Path
    used: int = 0
    used_by_device: dict[int, int] = field(default_factory=dict)
    _directories: dict[Path, int] = field(default_factory=dict, init=False, repr=False)

    @staticmethod
    def _absolute(path: Path) -> Path:
        normalized = Path(os.path.normpath(path))
        if not normalized.is_absolute() or ".." in normalized.parts:
            fail("host write destination is not an exact absolute path")
        return normalized

    def _admit_fd(self, directory_fd: int, amount: int) -> None:
        if amount < 0 or self.used + amount > HOST_WRITE_BUDGET_BYTES:
            fail("host write budget exceeded")
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            fail("host write destination is not a directory")
        device_used = self.used_by_device.get(metadata.st_dev, 0)
        if test_free_bytes is None:
            filesystem = os.fstatvfs(directory_fd)
            available = filesystem.f_bavail * filesystem.f_frsize
        else:
            available = max(0, test_free_bytes - device_used)
        if available - amount < HOST_FREE_FLOOR_BYTES:
            fail("host free-space admission failed")
        self.used += amount
        self.used_by_device[metadata.st_dev] = device_used + amount

    def _open_directory(self, path: Path, *, create: bool) -> int:
        path = self._absolute(path)
        cached = self._directories.get(path)
        if cached is not None:
            if not stat.S_ISDIR(os.fstat(cached).st_mode):
                fail("held host write directory changed type")
            return cached
        ancestors = [
            candidate
            for candidate in self._directories
            if candidate == path or candidate in path.parents
        ]
        if ancestors:
            current_path = max(ancestors, key=lambda candidate: len(candidate.parts))
            current_fd = self._directories[current_path]
        else:
            current_path = Path("/")
            current_fd = os.open(
                "/",
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
            )
            self._directories[current_path] = current_fd
        for part in path.relative_to(current_path).parts:
            try:
                child_fd = os.open(
                    part,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                if not create:
                    raise
                self._admit_fd(current_fd, 0)
                os.mkdir(part, 0o700, dir_fd=current_fd)
                os.fsync(current_fd)
                child_fd = os.open(
                    part,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            metadata = os.fstat(child_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child_fd)
                fail("host write path crossed a non-directory")
            current_path /= part
            self._directories[current_path] = child_fd
            current_fd = child_fd
        return current_fd

    def _safe_parent(self, path: Path, *, create: bool) -> int:
        directory_fd = self._open_directory(path, create=create)
        metadata = os.fstat(directory_fd)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            fail("host write parent authority is unsafe")
        return directory_fd

    def reserve(self, path: Path, amount: int) -> int:
        path = self._absolute(path)
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            directory = path.parent
        else:
            directory = path if stat.S_ISDIR(metadata.st_mode) else path.parent
        try:
            directory_fd = self._safe_parent(directory, create=False)
        except FileNotFoundError:
            ancestor = directory
            while True:
                ancestor = ancestor.parent
                try:
                    directory_fd = self._safe_parent(ancestor, create=False)
                    break
                except FileNotFoundError:
                    if ancestor == ancestor.parent:
                        raise
        self._admit_fd(directory_fd, amount)
        return directory_fd

    def _parent_for_write(self, path: Path, amount: int) -> int:
        path = self._absolute(path)
        try:
            directory_fd = self._safe_parent(path.parent, create=False)
        except FileNotFoundError:
            self.reserve(path, amount)
            return self._safe_parent(path.parent, create=True)
        self._admit_fd(directory_fd, amount)
        return directory_fd

    def ensure_private_directory(self, path: Path) -> int:
        directory_fd = self._open_directory(path, create=True)
        metadata = os.fstat(directory_fd)
        if metadata.st_uid != os.geteuid():
            fail("private host write directory has unsafe owner")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            self._admit_fd(directory_fd, 0)
            os.fchmod(directory_fd, 0o700)
            os.fsync(directory_fd)
        return directory_fd

    def ensure_write_directory(self, path: Path) -> int:
        return self._safe_parent(path, create=True)

    def held_directory(self, path: Path) -> int:
        directory_fd = self._safe_parent(path, create=False)
        self._admit_fd(directory_fd, 0)
        return directory_fd

    def move_leaf(
        self,
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
        *,
        expected: _LeafAuthority | tuple[int, int] | None = None,
    ) -> _LeafAuthority:
        source_fd, source = _open_leaf_at(source_parent_fd, source_name)
        try:
            authority = _leaf_authority(source)
            if expected is not None and not _leaf_matches(authority, expected):
                fail("host write source leaf authority differs")
            _test_boundary("host-leaf-move-validated")
            self._admit_fd(source_parent_fd, 0)
            self._admit_fd(destination_parent_fd, 0)
            if source_parent_fd == destination_parent_fd:
                _rename_noreplace(
                    source_parent_fd, source_name, destination_name
                )
            else:
                _rename_noreplace_between(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )
            moved = True
            try:
                destination_fd, destination = _open_leaf_at(
                    destination_parent_fd, destination_name
                )
                try:
                    if _leaf_authority(destination) != authority:
                        fail("host write source replacement was preserved")
                finally:
                    os.close(destination_fd)
                try:
                    os.stat(
                        source_name,
                        dir_fd=source_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    fail("host write source name was repopulated during move")
            except BaseException as error:
                if moved:
                    try:
                        self._admit_fd(destination_parent_fd, 0)
                        self._admit_fd(source_parent_fd, 0)
                        if source_parent_fd == destination_parent_fd:
                            _rename_noreplace(
                                source_parent_fd, destination_name, source_name
                            )
                        else:
                            _rename_noreplace_between(
                                destination_parent_fd,
                                destination_name,
                                source_parent_fd,
                                source_name,
                            )
                        os.fsync(destination_parent_fd)
                        if source_parent_fd != destination_parent_fd:
                            os.fsync(source_parent_fd)
                    except BaseException as restore_error:
                        raise RebuildError(
                            "host write source replacement retained both leaves"
                        ) from restore_error
                raise RebuildError(
                    "host write source replacement was preserved"
                ) from error
            os.fsync(source_parent_fd)
            if destination_parent_fd != source_parent_fd:
                os.fsync(destination_parent_fd)
            return authority
        finally:
            os.close(source_fd)

    def exchange_leaves(
        self,
        left_parent_fd: int,
        left_name: str,
        right_parent_fd: int,
        right_name: str,
        *,
        expected_left: _LeafAuthority | tuple[int, int] | None = None,
        expected_right: _LeafAuthority | tuple[int, int] | None = None,
    ) -> tuple[_LeafAuthority, _LeafAuthority]:
        left_fd, left = _open_leaf_at(left_parent_fd, left_name)
        right_fd = -1
        try:
            right_fd, right = _open_leaf_at(right_parent_fd, right_name)
            left_authority = _leaf_authority(left)
            right_authority = _leaf_authority(right)
            if expected_left is not None and not _leaf_matches(
                left_authority, expected_left
            ):
                fail("host write exchange left authority differs")
            if expected_right is not None and not _leaf_matches(
                right_authority, expected_right
            ):
                fail("host write exchange right authority differs")
            _test_boundary("host-leaf-exchange-validated")
            self._admit_fd(left_parent_fd, 0)
            self._admit_fd(right_parent_fd, 0)
            if left_parent_fd == right_parent_fd:
                _rename_exchange(left_parent_fd, left_name, right_name)
            else:
                _rename_exchange_between(
                    left_parent_fd, left_name, right_parent_fd, right_name
                )
            post_left_fd = -1
            post_right_fd = -1
            try:
                post_left_fd, post_left = _open_leaf_at(left_parent_fd, left_name)
                post_right_fd, post_right = _open_leaf_at(right_parent_fd, right_name)
                if (
                    _leaf_authority(post_left) != right_authority
                    or _leaf_authority(post_right) != left_authority
                ):
                    fail("host write exchange replacement was preserved")
            except BaseException as error:
                try:
                    self._admit_fd(left_parent_fd, 0)
                    self._admit_fd(right_parent_fd, 0)
                    if left_parent_fd == right_parent_fd:
                        _rename_exchange(left_parent_fd, left_name, right_name)
                    else:
                        _rename_exchange_between(
                            left_parent_fd, left_name, right_parent_fd, right_name
                        )
                    os.fsync(left_parent_fd)
                    if right_parent_fd != left_parent_fd:
                        os.fsync(right_parent_fd)
                except BaseException as restore_error:
                    raise RebuildError(
                        "host write exchange replacement retained both leaves"
                    ) from restore_error
                raise RebuildError(
                    "host write exchange replacement was preserved"
                ) from error
            finally:
                if post_right_fd >= 0:
                    os.close(post_right_fd)
                if post_left_fd >= 0:
                    os.close(post_left_fd)
            os.fsync(left_parent_fd)
            if right_parent_fd != left_parent_fd:
                os.fsync(right_parent_fd)
            return left_authority, right_authority
        finally:
            if right_fd >= 0:
                os.close(right_fd)
            os.close(left_fd)

    def park_leaf(
        self,
        source_parent_fd: int,
        source_name: str,
        *,
        expected: _LeafAuthority | tuple[int, int] | None = None,
    ) -> str:
        source_fd, metadata = _open_leaf_at(source_parent_fd, source_name)
        try:
            authority = _leaf_authority(metadata)
            if expected is not None and not _leaf_matches(authority, expected):
                fail("host write parked leaf authority differs")
        finally:
            os.close(source_fd)
        self._admit_fd(source_parent_fd, 0)
        park_parent_fd = self.ensure_private_directory(self.root)
        park_fd, park_name, park_authority = self._leaf_park_fd(park_parent_fd)
        parent = os.fstat(source_parent_fd)
        token = sha256_bytes(
            (
                f"{parent.st_dev:x}:{parent.st_ino:x}:{source_name}:"
                f"{authority.device:x}:{authority.inode:x}"
            ).encode("utf-8", errors="strict")
        )
        parked_name = (
            f"leaf.{token}.{authority.device:x}.{authority.inode:x}."
            f"{secrets.token_hex(8)}"
        )
        try:
            if os.fstat(park_fd).st_dev != authority.device:
                fail("host write leaf park crosses filesystem authority")
            self.move_leaf(
                source_parent_fd,
                source_name,
                park_fd,
                parked_name,
                expected=authority,
            )
            try:
                current = os.stat(
                    park_name, dir_fd=park_parent_fd, follow_symlinks=False
                )
                if _leaf_authority(current) != park_authority:
                    fail("host write leaf park identity changed")
            except BaseException as error:
                try:
                    self.move_leaf(
                        park_fd,
                        parked_name,
                        source_parent_fd,
                        source_name,
                        expected=authority,
                    )
                except BaseException as restore_error:
                    raise RebuildError(
                        "host write leaf retained its identity-bound park"
                    ) from restore_error
                raise RebuildError("host write leaf park was rolled back") from error
        finally:
            os.close(park_fd)
        return parked_name

    def _leaf_park_fd(
        self, parent_fd: int
    ) -> tuple[int, str, _LeafAuthority]:
        entries = _directory_entries(parent_fd)
        final_names = [
            entry.name
            for entry in entries
            if entry.name.startswith(f"{LEAF_PARK_DIRECTORY}.")
        ]
        slot_names = [entry.name for entry in entries if entry.name == LEAF_PARK_SLOT]
        if len(final_names) > 1 or len(slot_names) > 1 or final_names and slot_names:
            fail("host write leaf park inventory is ambiguous")
        if not final_names:
            if slot_names:
                slot_fd = os.open(
                    LEAF_PARK_SLOT,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                try:
                    slot = os.fstat(slot_fd)
                    if (
                        slot.st_uid != os.geteuid()
                        or stat.S_IMODE(slot.st_mode) != 0o700
                        or _directory_entries(slot_fd)
                    ):
                        fail("host write leaf park slot authority differs")
                    identity = (slot.st_dev, slot.st_ino)
                finally:
                    os.close(slot_fd)
            else:
                identity = self.mkdir_at(parent_fd, LEAF_PARK_SLOT)
            final_name = (
                f"{LEAF_PARK_DIRECTORY}.{identity[0]:x}.{identity[1]:x}"
            )
            self.move_leaf(
                parent_fd,
                LEAF_PARK_SLOT,
                parent_fd,
                final_name,
                expected=identity,
            )
        else:
            final_name = final_names[0]
        match = re.fullmatch(
            rf"{re.escape(LEAF_PARK_DIRECTORY)}\.([0-9a-f]+)\.([0-9a-f]+)",
            final_name,
        )
        if match is None:
            fail("host write leaf park name is malformed")
        park_fd = os.open(
            final_name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            metadata = os.fstat(park_fd)
            authority = _leaf_authority(metadata)
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or authority.device != int(match.group(1), 16)
                or authority.inode != int(match.group(2), 16)
            ):
                fail("host write leaf park authority differs")
            parked_pattern = re.compile(
                r"leaf\.[0-9a-f]{64}\.([0-9a-f]+)\.([0-9a-f]+)\."
                r"[0-9a-f]{16}"
            )
            for entry in _directory_entries(park_fd):
                parked = parked_pattern.fullmatch(entry.name)
                if parked is None:
                    fail("host write leaf park contains an unknown entry")
                leaf = entry.stat(follow_symlinks=False)
                if (leaf.st_dev, leaf.st_ino) != (
                    int(parked.group(1), 16),
                    int(parked.group(2), 16),
                ):
                    fail("host write parked leaf identity differs")
            return park_fd, final_name, authority
        except BaseException:
            os.close(park_fd)
            raise

    def mkdir_at(self, parent_fd: int, name: str) -> tuple[int, int]:
        if not name or Path(name).name != name:
            fail("host write directory name is unsafe")
        self._admit_fd(parent_fd, 0)
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        directory_fd = os.open(
            name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            metadata = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                fail("created host write directory authority differs")
            os.fsync(directory_fd)
            os.fsync(parent_fd)
            return metadata.st_dev, metadata.st_ino
        finally:
            os.close(directory_fd)

    def mkdir(self, path: Path) -> tuple[int, int]:
        path = self._absolute(path)
        parent_fd = self._parent_for_write(path, 0)
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        directory_fd = os.open(
            path.name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        self._directories[path] = directory_fd
        metadata = os.fstat(directory_fd)
        return metadata.st_dev, metadata.st_ino

    def write_new(self, path: Path, payload: bytes, mode: int) -> None:
        path = self._absolute(path)
        parent_fd = self._parent_for_write(path, len(payload))
        self._admit_fd(parent_fd, 0)
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        except BaseException:
            try:
                self.park_leaf(
                    parent_fd,
                    path.name,
                    expected=_leaf_authority(os.fstat(descriptor)),
                )
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(descriptor)

    def copy_fd(self, source_fd: int, destination: Path, mode: int) -> None:
        destination = self._absolute(destination)
        parent_fd = self._parent_for_write(destination, os.fstat(source_fd).st_size)
        temporary = f".{destination.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        self._admit_fd(parent_fd, 0)
        target_fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
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
                    view = view[os.write(target_fd, view) :]
            os.fchmod(target_fd, mode)
            os.fsync(target_fd)
            self.rename(
                destination.parent / temporary,
                destination,
            )
        except BaseException:
            try:
                self.park_leaf(
                    parent_fd,
                    temporary,
                    expected=_leaf_authority(os.fstat(target_fd)),
                )
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(target_fd)

    def rename(self, source: Path, destination: Path, *, replace: bool = False) -> None:
        source = self._absolute(source)
        destination = self._absolute(destination)
        source_parent = self._safe_parent(source.parent, create=False)
        destination_parent = self._parent_for_write(destination, 0)
        source_fd, source_metadata = _open_leaf_at(source_parent, source.name)
        try:
            source_authority = _leaf_authority(source_metadata)
        finally:
            os.close(source_fd)
        try:
            destination_fd, destination_metadata = _open_leaf_at(
                destination_parent, destination.name
            )
        except FileNotFoundError:
            self.move_leaf(
                source_parent,
                source.name,
                destination_parent,
                destination.name,
                expected=source_authority,
            )
        else:
            try:
                destination_authority = _leaf_authority(destination_metadata)
            finally:
                os.close(destination_fd)
            if not replace:
                fail("host write destination already exists")
            self.exchange_leaves(
                source_parent,
                source.name,
                destination_parent,
                destination.name,
                expected_left=source_authority,
                expected_right=destination_authority,
            )
            try:
                self.park_leaf(
                    source_parent,
                    source.name,
                    expected=destination_authority,
                )
            except BaseException as error:
                try:
                    self.exchange_leaves(
                        source_parent,
                        source.name,
                        destination_parent,
                        destination.name,
                        expected_left=destination_authority,
                        expected_right=source_authority,
                    )
                except BaseException as restore_error:
                    raise RebuildError(
                        "host write replacement retained its durable park"
                    ) from restore_error
                raise RebuildError("host write replacement was rolled back") from error
        for key in list(self._directories):
            if key in {source, destination} or source in key.parents or destination in key.parents:
                os.close(self._directories.pop(key))

    def replace_symlink(self, destination: Path, target: str) -> None:
        destination = self._absolute(destination)
        parent_fd = self._parent_for_write(destination, len(target.encode()))
        temporary = f".{destination.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        self._admit_fd(parent_fd, 0)
        os.symlink(target, temporary, dir_fd=parent_fd)
        try:
            self.rename(
                destination.parent / temporary,
                destination,
                replace=True,
            )
        finally:
            try:
                self.unlink(
                    destination.parent / temporary,
                    missing_ok=True,
                    symlink_only=True,
                )
            except FileNotFoundError:
                pass

    def unlink(
        self,
        path: Path,
        *,
        missing_ok: bool = False,
        symlink_only: bool = False,
        regular_mode: int | None = None,
    ) -> None:
        path = self._absolute(path)
        parent_fd = self._parent_for_write(path, 0)
        try:
            descriptor, metadata = _open_leaf_at(parent_fd, path.name)
        except FileNotFoundError:
            if not missing_ok:
                raise
            return
        try:
            if symlink_only and not stat.S_ISLNK(metadata.st_mode):
                fail("host write unlink authority changed type")
            if regular_mode is not None and (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != regular_mode
            ):
                fail("host write unlink regular-file authority changed")
            authority = _leaf_authority(metadata)
        finally:
            os.close(descriptor)
        self.park_leaf(parent_fd, path.name, expected=authority)

    def chmod(self, path: Path, mode: int, *, directory: bool = False) -> None:
        path = self._absolute(path)
        if directory:
            descriptor = self._open_directory(path, create=False)
            parent_fd = self._safe_parent(path.parent, create=False)
            close_descriptor = False
        else:
            parent_fd = self._safe_parent(path.parent, create=False)
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            close_descriptor = True
        self._admit_fd(parent_fd, 0)
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.geteuid() or (
                directory != stat.S_ISDIR(metadata.st_mode)
            ):
                fail("host write chmod authority is unsafe")
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            os.fsync(parent_fd)
        finally:
            if close_descriptor:
                os.close(descriptor)

    def close(self) -> None:
        for descriptor in set(self._directories.values()):
            os.close(descriptor)
        self._directories.clear()


def _decode_frame(
    frame: bytes,
    kind: str,
    maximum_payload: int,
) -> tuple[dict[str, Any], bytes]:
    if len(frame) < 12 or frame[:8] != FRAME_MAGIC:
        fail("scoped artifact frame is malformed")
    header_size = struct.unpack(">I", frame[8:12])[0]
    if header_size <= 0 or header_size > FRAME_HEADER_BYTES or len(frame) < 12 + header_size:
        fail("scoped artifact frame header is malformed")
    encoded = frame[12 : 12 + header_size]
    try:
        header = json.loads(encoded, object_pairs_hook=_reject_duplicate_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RebuildError("scoped artifact frame header is malformed") from error
    if (
        not isinstance(header, dict)
        or json.dumps(
            header, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
        != encoded
        or header.get("schema") != 1
        or header.get("kind") != kind
        or not isinstance(header.get("payload_size"), int)
        or isinstance(header.get("payload_size"), bool)
        or not 0 <= header["payload_size"] <= maximum_payload
    ):
        fail("scoped artifact frame header differs")
    payload = frame[12 + header_size :]
    if len(payload) != header["payload_size"]:
        fail("scoped artifact frame length differs")
    return cast(dict[str, Any], header), payload


def _entries_from_header(header: dict[str, Any]) -> list[TreeEntry]:
    expected_keys = {
        "archive_sha256",
        "byte_count",
        "commit",
        "entries",
        "file_count",
        "git_config_sha256",
        "kind",
        "payload_size",
        "schema",
        "tree",
    }
    if set(header) != expected_keys:
        fail("fetch frame authority set differs")
    for name, width in (
        ("commit", 40),
        ("tree", 40),
        ("archive_sha256", 64),
        ("git_config_sha256", 64),
    ):
        if not isinstance(header[name], str) or re.fullmatch(
            rf"[0-9a-f]{{{width}}}", header[name]
        ) is None:
            fail("fetch frame identity is malformed")
    raw_entries = header["entries"]
    if not isinstance(raw_entries, list) or not 0 < len(raw_entries) <= 8192:
        fail("fetch frame inventory is malformed")
    entries: list[TreeEntry] = []
    seen: set[str] = set()
    total = 0
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {"mode", "oid", "path", "size"}:
            fail("fetch frame entry is malformed")
        path = raw["path"]
        mode = raw["mode"]
        oid = raw["oid"]
        size = raw["size"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or path in seen
            or ".." in PurePosixPath(path).parts
            or "." in PurePosixPath(path).parts
            or mode not in {0o100644, 0o100755}
            or not isinstance(oid, str)
            or re.fullmatch(r"[0-9a-f]{40}", oid) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= 16 * 1024 * 1024
        ):
            fail("fetch frame entry authority differs")
        if path in {".gitmodules", ".gitattributes", ".cargo/config", ".cargo/config.toml"} or path.endswith(
            ("/.gitmodules", "/.gitattributes", "/.cargo/config", "/.cargo/config.toml")
        ):
            fail("fetch frame contains forbidden build control")
        seen.add(path)
        total += size
        if total > 128 * 1024 * 1024:
            fail("fetch frame source byte bound exceeded")
        entries.append(TreeEntry(path, mode, oid, size))
    if (
        "Cargo.toml" not in seen
        or "Cargo.lock" not in seen
        or header["file_count"] != len(entries)
        or header["byte_count"] != total
    ):
        fail("fetch frame inventory differs")
    return entries


def _extract_archive(
    archive_path: Path,
    source_path: Path,
    commit: str,
    entries: list[TreeEntry],
    budget: HostWriteBudget,
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
                budget.ensure_private_directory(source_path / relative)
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
            budget.ensure_private_directory(destination.parent)
            budget.write_new(
                destination, data, 0o500 if entry.mode == 0o100755 else 0o400
            )
            seen.add(name)
            content.update(
                f"{name}\0{entry.mode:o}\0{entry.oid}\0{sha256_bytes(data)}\n".encode()
            )
    if seen != set(expected):
        fail("source archive inventory differs from exact tree")
    for directory, directories, files in os.walk(source_path, topdown=False):
        for name in directories:
            budget.chmod(Path(directory) / name, 0o500, directory=True)
        for name in files:
            if str((Path(directory) / name).relative_to(source_path)) not in expected:
                fail("source extraction produced an extra file")
    budget.chmod(source_path, 0o500, directory=True)
    return content.hexdigest()


@dataclass
class SourceBundle:
    root: Path
    source: Path
    source_authority: DirectoryAuthority
    runtime_lib_authority: DirectoryAuthority
    python_stdlib_authority: DirectoryAuthority
    config_sha256: str
    commit: str
    tree: str
    archive_sha256: str
    tree_ledger_sha256: str
    file_count: int
    byte_count: int

    def verify(self) -> None:
        self.source_authority.verify()
        self.runtime_lib_authority.verify()
        self.python_stdlib_authority.verify()
        _verify_all_tools()

    def close(self) -> None:
        self.source_authority.close()
        self.runtime_lib_authority.close()
        self.python_stdlib_authority.close()


def _sandbox_runtime_prefix(
    runtime_lib: DirectoryAuthority,
    python_lib: DirectoryAuthority,
) -> list[str]:
    return [
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
        "/usr/lib/python3.11",
        "--dir",
        "/usr/lib/x86_64-linux-gnu",
        "--dir",
        "/sys",
        "--dir",
        "/sys/fs",
        "--dir",
        "/sys/fs/cgroup",
        "--dir",
        "/tools",
        "--ro-bind",
        f"/proc/self/fd/{runtime_lib.descriptor}",
        "/usr/lib/x86_64-linux-gnu",
        "--ro-bind",
        f"/proc/self/fd/{python_lib.descriptor}",
        "/usr/lib/python3.11",
        "--ro-bind",
        held_tools["python"].exec_path,
        "/tools/python",
        "--ro-bind",
        held_tools["scoped_worker"].exec_path,
        "/worker.py",
        "--ro-bind",
        str(cgroup_root),
        "/sys/fs/cgroup",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib/x86_64-linux-gnu",
        "/lib64",
        "--tmpfs",
        "/home",
        "--clearenv",
        "--setenv",
        "GB10_RESOURCE_FENCE",
        "1",
        "--setenv",
        "LANG",
        "C",
        "--setenv",
        "LC_ALL",
        "C",
        "--setenv",
        "PATH",
        "/tools",
    ]


def _worker_payload(operation: str, config: dict[str, object]) -> list[str]:
    # The digest-pinned worker keeps Cargo --frozen, --locked, and --offline.
    return [
        "--chdir",
        "/",
        "--",
        "/tools/python",
        "-I",
        "-B",
        "-S",
        "/worker.py",
        operation,
        json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False),
        SCOPE_UNIT_TOKEN,
    ]


def _fetch_sandbox(
    runtime_lib: DirectoryAuthority,
    python_lib: DirectoryAuthority,
) -> list[str]:
    policy = _scope_policy("fetch")
    config: dict[str, object] = {
        "policy": policy.contract(),
        "source_protocol": fetch_protocol,
        "source_ref": source_ref,
        "source_repo": source_repo,
    }
    return [
        *_sandbox_runtime_prefix(runtime_lib, python_lib),
        "--share-net",
        "--dir",
        "/etc",
        "--dir",
        "/etc/ssl",
        "--dir",
        "/etc/ssl/certs",
        "--ro-bind",
        held_tools["ca_cert"].exec_path,
        "/etc/ssl/certs/ca-certificates.crt",
        "--ro-bind",
        held_tools["resolv_conf"].exec_path,
        "/etc/resolv.conf",
        "--ro-bind",
        held_tools["nsswitch"].exec_path,
        "/etc/nsswitch.conf",
        "--ro-bind",
        held_tools["hosts"].exec_path,
        "/etc/hosts",
        "--ro-bind",
        held_tools["git"].exec_path,
        "/tools/git",
        "--ro-bind",
        held_tools["git_remote_https"].exec_path,
        "/tools/git-remote-https",
        "--size",
        str(FETCH_TMPFS_BYTES),
        "--tmpfs",
        "/fetch",
        "--size",
        str(FETCH_TMP_BYTES),
        "--tmpfs",
        "/tmp",
        *_worker_payload("fetch", config),
    ]


def prepare_canonical_source(
    snapshot_root: Path,
    budget: HostWriteBudget,
) -> SourceBundle:
    source = snapshot_root / "source"
    budget.mkdir(source)
    runtime_lib = _open_directory_authority("sandbox runtime libraries", sysroot_lib)
    try:
        python_lib = _open_directory_authority("sandbox Python standard library", python_stdlib)
    except BaseException:
        runtime_lib.close()
        raise
    try:
        frame = execute_scoped(
            "fetch",
            _fetch_sandbox(runtime_lib, python_lib),
            max_output_bytes=FRAME_HEADER_BYTES + 160 * 1024 * 1024 + 12,
            pass_fds=(runtime_lib.descriptor, python_lib.descriptor),
        )
        header, archive = _decode_frame(frame, "fetch", 160 * 1024 * 1024)
        entries = _entries_from_header(header)
        if sha256_bytes(archive) != header["archive_sha256"]:
            fail("fetch frame archive digest differs")
        archive_path = snapshot_root / "source.tar"
        budget.write_new(archive_path, archive, 0o400)
        tree_ledger = _extract_archive(
            archive_path,
            source,
            cast(str, header["commit"]),
            entries,
            budget,
        )
        source_authority = _open_directory_authority("canonical source", source)
        bundle = SourceBundle(
            snapshot_root,
            source,
            source_authority,
            runtime_lib,
            python_lib,
            cast(str, header["git_config_sha256"]),
            cast(str, header["commit"]),
            cast(str, header["tree"]),
            cast(str, header["archive_sha256"]),
            tree_ledger,
            len(entries),
            sum(entry.size for entry in entries),
        )
        bundle.verify()
        return bundle
    except BaseException:
        python_lib.close()
        runtime_lib.close()
        raise


def _cargo_sandbox(
    source: SourceBundle,
    toolchain: DirectoryAuthority,
    cache: DirectoryAuthority,
    index: DirectoryAuthority,
    gcc: DirectoryAuthority,
    sysroot_include_authority: DirectoryAuthority,
    phase: str,
) -> tuple[list[str], tuple[int, ...]]:
    policy = _scope_policy(phase)
    config: dict[str, object] = {"policy": policy.contract()}
    arguments = [
        *_sandbox_runtime_prefix(
            source.runtime_lib_authority,
            source.python_stdlib_authority,
        ),
        "--dir",
        "/usr/lib/gcc",
        "--dir",
        "/usr/lib/gcc/x86_64-linux-gnu",
        "--ro-bind",
        f"/proc/self/fd/{gcc.descriptor}",
        "/usr/lib/gcc/x86_64-linux-gnu/12",
        "--ro-bind",
        f"/proc/self/fd/{sysroot_include_authority.descriptor}",
        "/usr/include",
        "--ro-bind",
        held_tools["cc"].exec_path,
        "/usr/bin/cc",
        "--ro-bind",
        held_tools["as"].exec_path,
        "/usr/bin/as",
        "--ro-bind",
        held_tools["ld"].exec_path,
        "/usr/bin/ld",
        "--ro-bind",
        held_tools["ar"].exec_path,
        "/usr/bin/ar",
        "--ro-bind",
        f"/proc/self/fd/{source.source_authority.descriptor}",
        "/src",
        "--ro-bind",
        f"/proc/self/fd/{toolchain.descriptor}",
        "/toolchain",
        "--ro-bind",
        held_tools["cargo"].exec_path,
        "/toolchain/bin/cargo",
        "--ro-bind",
        held_tools["rustc"].exec_path,
        "/toolchain/bin/rustc",
        "--dir",
        "/cargo-home",
        "--dir",
        "/cargo-home/registry",
        "--ro-bind",
        f"/proc/self/fd/{cache.descriptor}",
        "/cargo-home/registry/cache",
        "--ro-bind",
        f"/proc/self/fd/{index.descriptor}",
        "/cargo-home/registry/index",
        "--size",
        str(BUILD_TARGET_TMPFS_BYTES),
        "--tmpfs",
        "/target",
        "--size",
        str(BUILD_TMP_BYTES),
        "--tmpfs",
        "/tmp",
        *_worker_payload(phase, config),
    ]
    descriptors = (
        source.source_authority.descriptor,
        source.runtime_lib_authority.descriptor,
        source.python_stdlib_authority.descriptor,
        toolchain.descriptor,
        cache.descriptor,
        index.descriptor,
        gcc.descriptor,
        sysroot_include_authority.descriptor,
    )
    return arguments, descriptors


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


@dataclass
class BuildBundle:
    candidate: Path
    identity: ExecutableIdentity
    metadata_closure_sha256: str
    sandbox_contract_sha256: str
    inputs: dict[str, object]
    authority: FileAuthority


def _containment_contract_sha256() -> str:
    contract = {
        "schema": 1,
        "frame": {
            "header_bytes": FRAME_HEADER_BYTES,
            "magic": FRAME_MAGIC.decode("ascii"),
            "source_bytes": 160 * 1024 * 1024,
            "candidate_bytes": MAX_EXECUTABLE_BYTES,
        },
        "host": {
            "free_floor_bytes": HOST_FREE_FLOOR_BYTES,
            "write_budget_bytes": HOST_WRITE_BUDGET_BYTES,
        },
        "scope": {
            phase: _scope_policy(phase).contract()
            for phase in ("fetch", "metadata", "build")
        },
        "sandbox": {
            "build_target_tmpfs_bytes": BUILD_TARGET_TMPFS_BYTES,
            "build_tmp_bytes": BUILD_TMP_BYTES,
            "fetch_tmpfs_bytes": FETCH_TMPFS_BYTES,
            "fetch_tmp_bytes": FETCH_TMP_BYTES,
            "network": {"fetch": "shared", "metadata": "isolated", "build": "isolated"},
            "writable_host_mounts": [],
        },
        "tool_sha256": {
            name: held.identity.sha256 for name, held in sorted(held_tools.items())
        },
    }
    return sha256_bytes(
        json.dumps(
            contract, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
    )


def _publish_candidate(
    source: SourceBundle,
    header: dict[str, Any],
    payload: bytes,
    budget: HostWriteBudget,
) -> tuple[Path, ExecutableIdentity, FileAuthority]:
    if set(header) != {
        "kind",
        "payload_sha256",
        "payload_size",
        "schema",
    } or not isinstance(header.get("payload_sha256"), str) or re.fullmatch(
        r"[0-9a-f]{64}", header["payload_sha256"]
    ) is None:
        fail("build frame authority set differs")
    digest = sha256_bytes(payload)
    if digest != header["payload_sha256"] or not payload:
        fail("build frame candidate digest differs")
    candidate = (
        cache_root
        / "releases"
        / f"{source.commit}-{digest}"
        / "llm-guard-proxy"
    )
    candidate_preexists = candidate.exists()
    if not candidate_preexists:
        scratch = os.fstat(budget.ensure_private_directory(source.root))
        destination = os.fstat(budget.ensure_private_directory(candidate.parent))
        if scratch.st_dev != destination.st_dev:
            fail("candidate publication scratch crosses filesystem authority")
        temporary = source.root / (
            f".candidate.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        )
        try:
            budget.write_new(temporary, payload, 0o755)
            _test_boundary("candidate-written")
            descriptor = os.open(
                temporary,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                built_identity = fd_identity(descriptor)
            finally:
                os.close(descriptor)
            if built_identity.sha256 != digest:
                fail("candidate publication identity differs")
            _test_boundary("candidate-validated")
            budget.rename(temporary, candidate)
        except BaseException:
            budget.unlink(temporary, missing_ok=True)
            raise
    candidate_file = _open_file_authority(
        "built artifact" if candidate_preexists else "candidate executable",
        candidate,
        expected_mode=0o755,
        max_bytes=MAX_EXECUTABLE_BYTES,
    )
    identity = fd_identity(candidate_file.descriptor)
    if identity.sha256 != digest or identity.size != len(payload):
        candidate_file.close()
        fail("adopted candidate differs from build frame")
    return candidate, identity, candidate_file


def build_sandboxed_candidate(
    source: SourceBundle,
    budget: HostWriteBudget,
) -> BuildBundle:
    authorities = [
        _open_directory_authority("toolchain", toolchain_root),
        _open_directory_authority("registry cache", registry_cache),
        _open_directory_authority("registry index", registry_index),
        _open_directory_authority("gcc closure", gcc_root),
        _open_directory_authority("sysroot include", sysroot_include),
    ]
    try:
        metadata_command, metadata_fds = _cargo_sandbox(
            source,
            authorities[0],
            authorities[1],
            authorities[2],
            authorities[3],
            authorities[4],
            "metadata",
        )
        metadata_frame = execute_scoped(
            "metadata",
            metadata_command,
            max_output_bytes=FRAME_HEADER_BYTES + 16 * 1024 * 1024 + 12,
            pass_fds=metadata_fds,
        )
        metadata_header, metadata_payload = _decode_frame(
            metadata_frame, "metadata", 16 * 1024 * 1024
        )
        if set(metadata_header) != {"kind", "payload_size", "schema"}:
            fail("metadata frame authority set differs")
        try:
            metadata_text = metadata_payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise RebuildError("Cargo metadata frame is not UTF-8") from error
        metadata_closure = _validate_metadata(metadata_text)

        build_command, build_fds = _cargo_sandbox(
            source,
            authorities[0],
            authorities[1],
            authorities[2],
            authorities[3],
            authorities[4],
            "build",
        )
        build_frame = execute_scoped(
            "build",
            build_command,
            max_output_bytes=FRAME_HEADER_BYTES + MAX_EXECUTABLE_BYTES + 12,
            pass_fds=build_fds,
        )
        build_header, candidate_payload = _decode_frame(
            build_frame, "build", MAX_EXECUTABLE_BYTES
        )
        candidate, candidate_identity, candidate_file = _publish_candidate(
            source, build_header, candidate_payload, budget
        )
        source.verify()
        for authority in authorities:
            authority.verify()
        _verify_all_tools()
        inputs: dict[str, object] = {
            authority.name.replace(" ", "_"): authority.receipt()
            for authority in authorities
        }
        inputs["sysroot_lib"] = source.runtime_lib_authority.receipt()
        inputs["python_stdlib"] = source.python_stdlib_authority.receipt()
        return BuildBundle(
            candidate,
            candidate_identity,
            metadata_closure,
            _containment_contract_sha256(),
            inputs,
            candidate_file,
        )
    finally:
        for authority in reversed(authorities):
            authority.close()


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


@dataclass(frozen=True)
class _ScratchDeleteRecord:
    name: str
    target_hash: str
    target_identity: tuple[int, int]
    placeholder_identity: tuple[int, int]


def _scratch_original_name(name: str) -> str | None:
    if SCRATCH_DIRECT_PATTERN.fullmatch(name) is not None:
        return name
    match = SCRATCH_PREPUBLICATION_PATTERN.fullmatch(name)
    if match is not None:
        return match.group(1)
    match = SCRATCH_TOMBSTONE_PATTERN.fullmatch(name)
    if match is not None:
        return f".{match.group(1)}"
    return None


def _scratch_name_hash(name: str) -> str:
    try:
        payload = name.encode("ascii", errors="strict")
    except UnicodeEncodeError as error:
        raise RebuildError("scratch exact-leaf name is malformed") from error
    return sha256_bytes(payload)


def _directory_entries(directory_fd: int) -> list[os.DirEntry[str]]:
    try:
        os.lseek(directory_fd, 0, os.SEEK_SET)
        with os.scandir(directory_fd) as entries:
            return list(entries)
    except OSError as error:
        raise RebuildError("held directory scan failed") from error


def _parse_scratch_delete_record(name: str) -> _ScratchDeleteRecord:
    match = SCRATCH_DELETE_PATTERN.fullmatch(name)
    if match is None:
        fail("scratch deletion record name is malformed")
    target_identity = (int(match.group(2), 16), int(match.group(3), 16))
    placeholder_identity = (int(match.group(4), 16), int(match.group(5), 16))
    if target_identity[1] == 0 or placeholder_identity[1] == 0:
        fail("scratch deletion record identity is malformed")
    return _ScratchDeleteRecord(
        name, match.group(1), target_identity, placeholder_identity
    )


def _scratch_delete_records(parent_fd: int) -> list[_ScratchDeleteRecord]:
    records = [
        _parse_scratch_delete_record(entry.name)
        for entry in _directory_entries(parent_fd)
        if entry.name.startswith(SCRATCH_DELETE_PREFIX)
    ]
    if len(records) > 8:
        fail("scratch deletion record inventory exceeded bound")
    return sorted(records, key=lambda record: record.name)


def _open_scratch_leaf(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None,
    allow_marker: bool,
    require_empty: bool = True,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise RebuildError("scratch exact leaf is unavailable") from error
    try:
        parent = os.fstat(parent_fd)
        metadata = os.fstat(descriptor)
        entries = _directory_entries(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (require_empty and not 0 < metadata.st_nlink <= 2)
            or metadata.st_dev != parent.st_dev
            or (
                expected_identity is not None
                and (metadata.st_dev, metadata.st_ino) != expected_identity
            )
            or (
                require_empty
                and entries
                and (
                    not allow_marker
                    or len(entries) != 1
                    or entries[0].name != SCRATCH_MARKER
                )
            )
        ):
            fail("scratch exact leaf authority differs")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _scratch_marker_present(
    descriptor: int, target_name: str
) -> _LeafAuthority | None:
    entries = _directory_entries(descriptor)
    if not entries:
        return None
    if not any(entry.name == SCRATCH_MARKER for entry in entries):
        return None
    marker_fd = os.open(
        SCRATCH_MARKER,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=descriptor,
    )
    try:
        marker = os.fstat(marker_fd)
        if (
            not stat.S_ISREG(marker.st_mode)
            or marker.st_uid != os.geteuid()
            or marker.st_nlink != 1
            or stat.S_IMODE(marker.st_mode) != 0o600
            or not 0 < marker.st_size <= 512
        ):
            fail("scratch marker authority differs")
        authority = _leaf_authority(marker)
        payload = _read_fd_limited(marker_fd, 512, "scratch marker")
    finally:
        os.close(marker_fd)
    original_name = _scratch_original_name(target_name)
    if original_name is None or payload != _scratch_payload(original_name):
        fail("scratch marker identity differs")
    return authority


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


def _remove_tree_contents(
    budget: HostWriteBudget,
    directory_fd: int,
    expected_device: int,
    *,
    scratch_root: bool = False,
) -> None:
    _deadline_checkpoint("snapshot-cleanup")
    metadata = os.fstat(directory_fd)
    if metadata.st_uid != os.geteuid() or metadata.st_dev != expected_device:
        fail("owned cleanup tree crossed its filesystem authority")
    budget._admit_fd(directory_fd, 0)
    os.fchmod(directory_fd, 0o700)
    entries = _directory_entries(directory_fd)
    entries.sort(key=lambda entry: entry.name)
    for entry in entries:
        _deadline_checkpoint("snapshot-cleanup")
        if scratch_root and entry.name == SCRATCH_MARKER:
            continue
        scanned_inode = entry.inode()
        scanned = entry.stat(follow_symlinks=False)
        if scanned.st_ino != scanned_inode or scanned.st_dev != metadata.st_dev:
            fail("owned cleanup leaf changed after scan")
        authority = _leaf_authority(scanned)
        _test_boundary("cleanup-leaf-scanned")
        budget._admit_fd(directory_fd, 0)
        current_fd, current = _open_leaf_at(directory_fd, entry.name)
        try:
            if _leaf_authority(current) != authority:
                fail("owned cleanup replacement leaf was preserved")
        finally:
            os.close(current_fd)


def _scratch_payload(name: str) -> bytes:
    token = name.removeprefix(".rebuild-input-")
    return json.dumps(
        {
            "kind": "llm-guard-rebuild-scratch",
            "name": name,
            "schema": 1,
            "token": token,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def create_snapshot_root(budget: HostWriteBudget) -> tuple[Path, tuple[int, int]]:
    name = f".rebuild-input-{secrets.token_hex(16)}"
    root = cache_root / name
    prepublication = cache_root / (
        f".{name}.publish.{os.getpid()}.{secrets.token_hex(4)}"
    )
    parent_fd = budget.held_directory(cache_root)
    identity = _reuse_completed_scratch(budget, parent_fd, prepublication)
    if identity is None:
        identity = budget.mkdir(prepublication)
    published = False
    try:
        _test_boundary("scratch-prepublication")
        budget.write_new(
            prepublication / SCRATCH_MARKER, _scratch_payload(name), 0o600
        )
        os.fsync(budget.ensure_write_directory(prepublication))
        _test_boundary("scratch-marker-directory-fsynced")
        budget.rename(prepublication, root)
        published = True
        _test_boundary("scratch-published")
        if _directory_identity(root) != identity:
            fail("scratch identity changed during publication")
        return root, identity
    except BaseException:
        if published:
            cleanup_snapshot(root, identity, budget=budget)
        else:
            _remove_quarantined_scratch(budget, prepublication, identity)
        raise


def _verify_scratch_marker(root: Path, name: str) -> tuple[int, int]:
    metadata = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail("orphan scratch directory authority differs")
    descriptor = os.open(
        root / SCRATCH_MARKER,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        marker = os.fstat(descriptor)
        if (
            not stat.S_ISREG(marker.st_mode)
            or marker.st_uid != os.geteuid()
            or marker.st_nlink != 1
            or stat.S_IMODE(marker.st_mode) != 0o600
            or not 0 < marker.st_size <= 512
        ):
            fail("orphan scratch marker authority differs")
        payload = _read_fd_limited(descriptor, 512, "scratch marker")
    finally:
        os.close(descriptor)
    if payload != _scratch_payload(name):
        fail("orphan scratch marker identity differs")
    return metadata.st_dev, metadata.st_ino


def _verify_prepublication_scratch(root: Path) -> tuple[int, int]:
    identity = _directory_identity(root)
    entries = list(os.scandir(root))
    if len(entries) > 1 or any(entry.name != SCRATCH_MARKER for entry in entries):
        fail("prepublication scratch contains an unknown artifact")
    if entries:
        marker = entries[0].stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(marker.st_mode)
            or marker.st_uid != os.geteuid()
            or marker.st_nlink != 1
            or stat.S_IMODE(marker.st_mode) != 0o600
            or marker.st_size > 512
        ):
            fail("prepublication scratch marker authority differs")
    return identity


def _target_name_for_record(
    parent_fd: int, record: _ScratchDeleteRecord
) -> str | None:
    matches = [
        entry.name
        for entry in _directory_entries(parent_fd)
        if _scratch_original_name(entry.name) is not None
        and _scratch_name_hash(entry.name) == record.target_hash
    ]
    if len(matches) > 1:
        fail("scratch deletion target inventory is ambiguous")
    return matches[0] if matches else None


def _ensure_scratch_delete_slot(
    budget: HostWriteBudget, parent_fd: int
) -> tuple[int, int]:
    try:
        descriptor, metadata = _open_scratch_leaf(
            parent_fd,
            SCRATCH_DELETE_SLOT,
            expected_identity=None,
            allow_marker=False,
        )
    except RebuildError as error:
        try:
            os.stat(SCRATCH_DELETE_SLOT, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return budget.mkdir_at(parent_fd, SCRATCH_DELETE_SLOT)
        raise error
    try:
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _prepare_scratch_delete(
    budget: HostWriteBudget,
    parent_fd: int,
    target_name: str,
    target_identity: tuple[int, int],
    *,
    require_empty: bool = False,
) -> _ScratchDeleteRecord:
    if _scratch_original_name(target_name) is None:
        fail("scratch deletion target name is malformed")
    target_hash = _scratch_name_hash(target_name)
    if any(
        record.target_hash == target_hash for record in _scratch_delete_records(parent_fd)
    ):
        fail("scratch deletion record already exists")
    target_fd, _ = _open_scratch_leaf(
        parent_fd,
        target_name,
        expected_identity=target_identity,
        allow_marker=True,
        require_empty=require_empty,
    )
    try:
        if require_empty and _directory_entries(target_fd):
            fail("scratch deletion target gained an artifact before ownership record")
    finally:
        os.close(target_fd)
    placeholder_identity = _ensure_scratch_delete_slot(budget, parent_fd)
    record = _ScratchDeleteRecord(
        (
            f"{SCRATCH_DELETE_PREFIX}{target_hash}."
            f"{target_identity[0]:x}.{target_identity[1]:x}."
            f"{placeholder_identity[0]:x}.{placeholder_identity[1]:x}."
            f"{secrets.token_hex(8)}"
        ),
        target_hash,
        target_identity,
        placeholder_identity,
    )
    try:
        budget.move_leaf(
            parent_fd,
            SCRATCH_DELETE_SLOT,
            parent_fd,
            record.name,
            expected=placeholder_identity,
        )
        record_fd, _ = _open_scratch_leaf(
            parent_fd,
            record.name,
            expected_identity=placeholder_identity,
            allow_marker=False,
        )
        try:
            budget._admit_fd(record_fd, 0)
            os.fchmod(record_fd, 0o700)
            os.fsync(record_fd)
        finally:
            os.close(record_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise RebuildError("scratch deletion record could not be made durable") from error
    _test_boundary("scratch-delete-record-created")
    return record


def _park_exact_leaf(
    budget: HostWriteBudget,
    parent_fd: int,
    name: str,
    descriptor: int,
    identity: tuple[int, int],
    destination: str,
) -> None:
    held = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(held.st_mode)
        or (held.st_dev, held.st_ino) != identity
        or _directory_entries(descriptor)
    ):
        fail("scratch exact-leaf identity changed before parking")
    _test_boundary("scratch-delete-placeholder-validated")
    budget.move_leaf(
        parent_fd,
        name,
        parent_fd,
        destination,
        expected=identity,
    )


def _exchange_and_remove_scratch(
    budget: HostWriteBudget,
    parent_fd: int,
    target_name: str,
    target_fd: int,
    target_identity: tuple[int, int],
    record: _ScratchDeleteRecord,
) -> None:
    if (
        record.target_identity != target_identity
        or record.target_hash != _scratch_name_hash(target_name)
    ):
        fail("scratch deletion record does not bind the target")
    placeholder_fd, placeholder = _open_scratch_leaf(
        parent_fd,
        record.name,
        expected_identity=None,
        allow_marker=False,
    )
    placeholder_identity = (placeholder.st_dev, placeholder.st_ino)
    if placeholder_identity != record.placeholder_identity:
        os.close(placeholder_fd)
        fail("scratch deletion placeholder identity differs")
    try:
        budget.exchange_leaves(
            parent_fd,
            target_name,
            parent_fd,
            record.name,
            expected_left=target_identity,
            expected_right=placeholder_identity,
        )
        _test_boundary("scratch-delete-exchanged")

        post_target_fd = -1
        try:
            post_target_fd, _ = _open_scratch_leaf(
                parent_fd,
                target_name,
                expected_identity=placeholder_identity,
                allow_marker=False,
            )
            _park_exact_leaf(
                budget,
                parent_fd,
                target_name,
                post_target_fd,
                placeholder_identity,
                SCRATCH_DELETE_SLOT,
            )
            _test_boundary("scratch-delete-placeholder-removed")
            _recover_scratch_delete(budget, parent_fd, record)
            _test_boundary("scratch-delete-target-removed")
        except BaseException as error:
            try:
                current_target = os.stat(
                    target_name, dir_fd=parent_fd, follow_symlinks=False
                )
                restorable = (
                    current_target.st_dev,
                    current_target.st_ino,
                ) == placeholder_identity
            except OSError:
                restorable = False
            if restorable:
                try:
                    budget.exchange_leaves(
                        parent_fd,
                        target_name,
                        parent_fd,
                        record.name,
                        expected_left=placeholder_identity,
                        expected_right=target_identity,
                    )
                except BaseException as restore_error:
                    raise RebuildError(
                        "scratch exact-leaf exchange could not be restored"
                    ) from restore_error
            raise RebuildError("scratch exact-leaf exchange validation failed") from error
        finally:
            if post_target_fd >= 0:
                os.close(post_target_fd)
    finally:
        os.close(placeholder_fd)


def _finish_scratch_delete(
    budget: HostWriteBudget,
    parent_fd: int,
    target_name: str,
    target_fd: int,
    target_identity: tuple[int, int],
    record: _ScratchDeleteRecord | None = None,
) -> None:
    metadata = os.fstat(target_fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (metadata.st_dev, metadata.st_ino) != target_identity
    ):
        fail("scratch target identity differs before deletion")
    matching = [
        candidate
        for candidate in _scratch_delete_records(parent_fd)
        if candidate.target_hash == _scratch_name_hash(target_name)
    ]
    if record is None:
        if len(matching) > 1:
            fail("scratch deletion record inventory is ambiguous")
        record = (
            matching[0]
            if matching
            else _prepare_scratch_delete(
                budget, parent_fd, target_name, target_identity
            )
        )
    elif matching != [record]:
        fail("scratch deletion record authority differs")
    if record.target_identity != target_identity:
        fail("scratch deletion record identity differs")
    current = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != target_identity:
        fail("scratch target changed before marker removal")
    marker = _scratch_marker_present(target_fd, target_name)
    if marker is not None:
        budget.park_leaf(
            target_fd,
            SCRATCH_MARKER,
            expected=marker,
        )
        _test_boundary("scratch-marker-removed")
    _exchange_and_remove_scratch(
        budget, parent_fd, target_name, target_fd, target_identity, record
    )


def _recover_scratch_delete(
    budget: HostWriteBudget, parent_fd: int, record: _ScratchDeleteRecord
) -> None:
    try:
        record_fd, record_metadata = _open_leaf_at(parent_fd, record.name)
    except OSError as error:
        raise RebuildError("scratch deletion record is unavailable") from error
    try:
        record_identity = (record_metadata.st_dev, record_metadata.st_ino)
    finally:
        os.close(record_fd)
    if record_metadata.st_dev != os.fstat(parent_fd).st_dev:
        fail("scratch deletion record crossed filesystem authority")
    if record_identity not in {
        record.target_identity,
        record.placeholder_identity,
    }:
        fail("foreign scratch entry preserved after record replacement")
    target_name = _target_name_for_record(parent_fd, record)
    if record_identity == record.target_identity:
        record_fd, _ = _open_scratch_leaf(
            parent_fd,
            record.name,
            expected_identity=record.target_identity,
            allow_marker=False,
            require_empty=False,
        )
        try:
            if target_name is not None:
                placeholder_fd, _ = _open_scratch_leaf(
                    parent_fd,
                    target_name,
                    expected_identity=record.placeholder_identity,
                    allow_marker=False,
                )
                try:
                    _park_exact_leaf(
                        budget,
                        parent_fd,
                        target_name,
                        placeholder_fd,
                        record.placeholder_identity,
                        SCRATCH_DELETE_SLOT,
                    )
                finally:
                    os.close(placeholder_fd)
                _test_boundary("scratch-delete-placeholder-removed")
            try:
                slot_fd, _ = _open_scratch_leaf(
                    parent_fd,
                    SCRATCH_DELETE_SLOT,
                    expected_identity=record.placeholder_identity,
                    allow_marker=False,
                )
            except RebuildError as error:
                try:
                    os.stat(
                        SCRATCH_DELETE_SLOT,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    _ensure_scratch_delete_slot(budget, parent_fd)
                    budget.park_leaf(
                        parent_fd,
                        record.name,
                        expected=record.target_identity,
                    )
                    return
                raise RebuildError("foreign scratch slot preserved") from error
            try:
                _test_boundary("scratch-delete-record-validated")
                current_record = os.stat(
                    record.name, dir_fd=parent_fd, follow_symlinks=False
                )
                current_slot = os.stat(
                    SCRATCH_DELETE_SLOT,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    (current_record.st_dev, current_record.st_ino)
                    != record.target_identity
                    or (current_slot.st_dev, current_slot.st_ino)
                    != record.placeholder_identity
                ):
                    fail("scratch parked deletion record identity differs")
            finally:
                os.close(slot_fd)
        finally:
            os.close(record_fd)
        return
    if target_name is None:
        record_fd, _ = _open_scratch_leaf(
            parent_fd,
            record.name,
            expected_identity=record.placeholder_identity,
            allow_marker=False,
        )
        try:
            slot_fd, _ = _open_scratch_leaf(
                parent_fd,
                SCRATCH_DELETE_SLOT,
                expected_identity=record.target_identity,
                allow_marker=False,
                require_empty=False,
            )
            os.close(slot_fd)
        finally:
            os.close(record_fd)
        budget.exchange_leaves(
            parent_fd,
            record.name,
            parent_fd,
            SCRATCH_DELETE_SLOT,
            expected_left=record.placeholder_identity,
            expected_right=record.target_identity,
        )
        _test_boundary("scratch-reuse-rollback")
        _recover_scratch_delete(budget, parent_fd, record)
        return
    target_fd, _ = _open_scratch_leaf(
        parent_fd,
        target_name,
        expected_identity=record.target_identity,
        allow_marker=True,
        require_empty=False,
    )
    try:
        _finish_scratch_delete(
            budget,
            parent_fd,
            target_name,
            target_fd,
            record.target_identity,
            record,
        )
    finally:
        os.close(target_fd)


def _reuse_completed_scratch(
    budget: HostWriteBudget, parent_fd: int, destination: Path
) -> tuple[int, int] | None:
    records = _scratch_delete_records(parent_fd)
    if not records:
        return None
    if len(records) != 1:
        fail("reusable scratch record inventory is ambiguous")
    record = records[0]
    _recover_scratch_delete(budget, parent_fd, record)
    try:
        os.stat(record.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if _target_name_for_record(parent_fd, record) is not None:
        fail("reusable scratch target name still exists")
    record_fd, _ = _open_scratch_leaf(
        parent_fd,
        record.name,
        expected_identity=record.target_identity,
        allow_marker=False,
        require_empty=False,
    )
    slot_fd = -1
    try:
        slot_fd, _ = _open_scratch_leaf(
            parent_fd,
            SCRATCH_DELETE_SLOT,
            expected_identity=record.placeholder_identity,
            allow_marker=False,
        )
        budget.move_leaf(
            parent_fd,
            SCRATCH_DELETE_SLOT,
            parent_fd,
            destination.name,
            expected=record.placeholder_identity,
        )
        _test_boundary("scratch-reuse-published")
        reused_fd, _ = _open_scratch_leaf(
            parent_fd,
            destination.name,
            expected_identity=record.placeholder_identity,
            allow_marker=False,
        )
        os.close(reused_fd)
        budget.park_leaf(
            parent_fd,
            record.name,
            expected=record.target_identity,
        )
        return record.placeholder_identity
    finally:
        if slot_fd >= 0:
            os.close(slot_fd)
        os.close(record_fd)


def _remove_quarantined_scratch(
    budget: HostWriteBudget,
    root: Path,
    identity: tuple[int, int],
    parent_fd: int | None = None,
) -> None:
    if parent_fd is None:
        parent_fd = budget.held_directory(root.parent)
    root_fd = os.open(
        root.name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            fail("quarantined scratch identity differs")
        _remove_tree_contents(
            budget,
            root_fd,
            identity[0],
            scratch_root=True,
        )
        _finish_scratch_delete(
            budget, parent_fd, root.name, root_fd, identity
        )
    finally:
        os.close(root_fd)


def _remove_prepublication_scratch(
    budget: HostWriteBudget,
    parent_fd: int,
    target_name: str,
    target_identity: tuple[int, int],
) -> None:
    target_fd, _ = _open_scratch_leaf(
        parent_fd,
        target_name,
        expected_identity=target_identity,
        allow_marker=False,
    )
    try:
        if _directory_entries(target_fd):
            fail("markerless prepublication scratch is not empty")
    finally:
        os.close(target_fd)
    record = _prepare_scratch_delete(
        budget,
        parent_fd,
        target_name,
        target_identity,
        require_empty=True,
    )
    target_fd, _ = _open_scratch_leaf(
        parent_fd,
        target_name,
        expected_identity=target_identity,
        allow_marker=False,
    )
    try:
        _finish_scratch_delete(
            budget,
            parent_fd,
            target_name,
            target_fd,
            target_identity,
            record,
        )
    finally:
        os.close(target_fd)


def _remove_empty_markerless_tombstone(
    root: Path,
    budget: HostWriteBudget,
    parent_fd: int | None = None,
) -> None:
    if parent_fd is None:
        parent_fd = budget.held_directory(root.parent)
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
    ):
        fail("markerless scratch parent authority differs")
    root_fd, metadata = _open_scratch_leaf(
        parent_fd,
        root.name,
        expected_identity=None,
        allow_marker=False,
    )
    try:
        identity = (metadata.st_dev, metadata.st_ino)
    finally:
        os.close(root_fd)
    matching = [
        record
        for record in _scratch_delete_records(parent_fd)
        if record.target_hash == _scratch_name_hash(root.name)
        and record.target_identity == identity
    ]
    if len(matching) != 1:
        fail("markerless scratch lacks a durable external ownership record")
    _recover_scratch_delete(budget, parent_fd, matching[0])


def sweep_orphan_scratch(budget: HostWriteBudget) -> None:
    parent_fd = budget.held_directory(cache_root)
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
    ):
        fail("scratch recovery parent authority differs")
    for record in _scratch_delete_records(parent_fd):
        _recover_scratch_delete(budget, parent_fd, record)
    candidates = [
        entry
        for entry in _directory_entries(parent_fd)
        if entry.name.startswith((".rebuild-input-", "..rebuild-input-"))
    ]
    if len(candidates) > 8:
        fail("orphan scratch inventory exceeded bound")
    for entry in candidates:
        direct_match = SCRATCH_DIRECT_PATTERN.fullmatch(entry.name)
        prepublication_match = SCRATCH_PREPUBLICATION_PATTERN.fullmatch(entry.name)
        tombstone_match = SCRATCH_TOMBSTONE_PATTERN.fullmatch(entry.name)
        root = cache_root / entry.name
        if direct_match is not None:
            identity = _verify_scratch_marker(root, entry.name)
            cleanup_snapshot(
                root,
                identity,
                budget=budget,
                parent_fd=parent_fd,
            )
            continue
        if prepublication_match is not None:
            identity = _verify_prepublication_scratch(root)
            root_fd, _ = _open_scratch_leaf(
                parent_fd,
                entry.name,
                expected_identity=identity,
                allow_marker=True,
            )
            try:
                marker_present = bool(_directory_entries(root_fd))
            finally:
                os.close(root_fd)
            if marker_present:
                _remove_quarantined_scratch(
                    budget, root, identity, parent_fd
                )
            else:
                _remove_prepublication_scratch(
                    budget, parent_fd, entry.name, identity
                )
            continue
        if tombstone_match is not None:
            original_name = f".{tombstone_match.group(1)}"
            root_fd, metadata = _open_scratch_leaf(
                parent_fd,
                entry.name,
                expected_identity=None,
                allow_marker=True,
            )
            try:
                marker_present = bool(_directory_entries(root_fd))
                identity = (metadata.st_dev, metadata.st_ino)
            finally:
                os.close(root_fd)
            if not marker_present:
                _remove_empty_markerless_tombstone(root, budget, parent_fd)
            else:
                verified_identity = _verify_scratch_marker(root, original_name)
                if verified_identity != identity:
                    fail("orphan scratch marker identity changed")
                _remove_quarantined_scratch(
                    budget, root, identity, parent_fd
                )
            continue
        fail("orphan scratch name is malformed")


def cleanup_snapshot(
    root: Path | None,
    expected_identity: tuple[int, int] | None = None,
    *,
    budget: HostWriteBudget | None = None,
    parent_fd: int | None = None,
) -> None:
    if root is None:
        return
    if expected_identity is None:
        fail("owned directory cleanup lacks exact identity")
    if budget is None:
        fail("owned directory cleanup lacks its active host write budget")
    scratch_root = root.name.startswith(".rebuild-input-")
    _deadline_checkpoint("snapshot-cleanup")
    if parent_fd is None:
        parent_fd = budget.held_directory(root.parent)
    if parent_fd is not None:
        parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o022
        ):
            fail(f"unsafe owned cleanup parent: {root.parent}")
        budget._admit_fd(parent_fd, 0)
        cleanup_pattern = re.compile(
            rf"\.{re.escape(root.name)}\.cleanup\.[1-9][0-9]*\.[0-9a-f]{{8}}"
        )
        tombstones: list[str] = []
        for entry in _directory_entries(parent_fd):
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
            records = (
                [
                    record
                    for record in _scratch_delete_records(parent_fd)
                    if record.target_identity == expected_identity
                ]
                if scratch_root
                else []
            )
            if len(records) > 1:
                fail("owned scratch deletion record inventory is ambiguous")
            if records:
                if budget is None:
                    fail("scratch recovery lacks a host write budget")
                _recover_scratch_delete(budget, parent_fd, records[0])
                return
            if not tombstones:
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
        renamed = owned_name == root.name
        if renamed:
            if budget is None:
                fail("owned cleanup quarantine lacks a host write budget")
            tombstone = f".{root.name}.cleanup.{os.getpid()}.{secrets.token_hex(4)}"
            _deadline_checkpoint("snapshot-cleanup")
            budget.move_leaf(
                parent_fd,
                root.name,
                parent_fd,
                tombstone,
                expected=actual,
            )
        if renamed and scratch_root:
            _test_boundary("scratch-tombstone-renamed")
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
            _remove_tree_contents(
                budget,
                tombstone_fd,
                actual[0],
                scratch_root=scratch_root,
            )
            if scratch_root:
                if budget is None:
                    fail("scratch cleanup lacks a host write budget")
                _finish_scratch_delete(
                    budget, parent_fd, tombstone, tombstone_fd, actual
                )
            else:
                if budget is None:
                    fail("owned cleanup leaf park lacks a host write budget")
                budget.park_leaf(
                    parent_fd,
                    tombstone,
                    expected=_leaf_authority(quarantined),
                )
        finally:
            os.close(tombstone_fd)
        _deadline_checkpoint("snapshot-cleanup")
        _deadline_checkpoint("snapshot-cleanup")


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


def set_service_link(target: str, budget: HostWriteBudget) -> None:
    _deadline_checkpoint("rollback")
    if not _safe_absolute(target):
        fail("refusing unsafe service link target")
    budget.replace_symlink(service_bin, target)


def remove_service_link(budget: HostWriteBudget) -> None:
    _deadline_checkpoint("rollback")
    budget.unlink(service_bin, missing_ok=True, symlink_only=True)


def _secure_directory(path: Path) -> None:
    _deadline_checkpoint("filesystem-read")
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


def _secure_directory_fd(descriptor: int, label: Path) -> None:
    _deadline_checkpoint("filesystem-read")
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail(f"unsafe rebuild state directory: {label}")


def _secure_state_file_at(directory_fd: int, name: str, label: Path) -> os.stat_result:
    _deadline_checkpoint("filesystem-read")
    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > STATE_MAX_BYTES
    ):
        fail(f"unsafe rebuild state file: {label}")
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


def acquire_rebuild_lock(budget: HostWriteBudget) -> int:
    budget.ensure_private_directory(receipt_dir)
    lock_path = receipt_dir / "lock.v1"
    parent_fd = budget.reserve(lock_path, 0)
    try:
        os.stat(lock_path.name, dir_fd=parent_fd, follow_symlinks=False)
        existed = True
    except FileNotFoundError:
        existed = False
    budget._admit_fd(parent_fd, 0)
    flags = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    if not existed:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(
        lock_path.name,
        flags,
        0o600,
        dir_fd=parent_fd,
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
            os.fsync(parent_fd)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RebuildError("rebuild lock is already held") from error
        current = os.stat(
            lock_path.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if _leaf_authority(current) != _leaf_authority(metadata):
            fail("rebuild lock leaf changed during acquisition")
        for name in ("rollback", "receipts"):
            budget.ensure_private_directory(receipt_dir / name)
        os.fsync(parent_fd)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _transaction_dir() -> Path:
    return receipt_dir / "transaction.v1"


def _state_path() -> Path:
    return _transaction_dir() / "state.json"


def _transaction_artifacts(
    budget: HostWriteBudget, parent_fd: int
) -> list[Path]:
    _deadline_checkpoint("filesystem-read")
    patterns = (
        r"\.transaction\.v1\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}",
        r"\.transaction\.v1\.cleanup\.[1-9][0-9]*\.[0-9a-f]{8}",
    )
    artifacts: list[Path] = []
    for entry in _directory_entries(parent_fd):
        _deadline_checkpoint("filesystem-read")
        if entry.name == "transaction.v1" or any(
            re.fullmatch(pattern, entry.name) for pattern in patterns
        ):
            artifacts.append(receipt_dir / entry.name)
    return sorted(artifacts, key=lambda path: path.name)


def _prepare_transaction_namespace(budget: HostWriteBudget) -> bool:
    _deadline_checkpoint("filesystem-read")
    parent_fd = budget.held_directory(receipt_dir)
    artifacts = _transaction_artifacts(budget, parent_fd)
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
        metadata = os.stat(
            cleanup.name, dir_fd=parent_fd, follow_symlinks=False
        )
        cleanup_snapshot(
            cleanup,
            (metadata.st_dev, metadata.st_ino),
            budget=budget,
            parent_fd=parent_fd,
        )
    if any(path.name == canonical.name for path in artifacts):
        if temporaries:
            fail("canonical transaction conflicts with prepublication temp")
        canonical_fd = budget.held_directory(canonical)
        _secure_directory_fd(canonical_fd, canonical)
        entries = sorted(entry.name for entry in _directory_entries(canonical_fd))
        if "state.json" not in entries or any(
            name != "state.json"
            and re.fullmatch(r"\.state\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}", name)
            is None
            for name in entries
        ):
            fail("transaction contains an unknown publication artifact")
        for name in entries:
            if name != "state.json":
                _secure_state_file_at(canonical_fd, name, canonical / name)
        return bool(artifacts)
    if not temporaries:
        return bool(artifacts)
    temporary = temporaries[0]
    temporary_fd = budget.held_directory(temporary)
    _secure_directory_fd(temporary_fd, temporary)
    entries = sorted(entry.name for entry in _directory_entries(temporary_fd))
    if not entries:
        fail("empty prepublication transaction retained")
    if entries == ["state.json"]:
        _secure_state_file_at(temporary_fd, "state.json", temporary / "state.json")
        budget.rename(temporary, canonical)
        return True
    if len(entries) == 1 and re.fullmatch(
        r"\.state\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}", entries[0]
    ):
        _secure_state_file_at(temporary_fd, entries[0], temporary / entries[0])
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
            "python_stdlib",
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
    if re.fullmatch(r"\.rebuild-input-[0-9a-f]{32}", Path(snapshot).name) is None:
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


def persist_wal(
    wal: dict[str, Any], budget: HostWriteBudget, *, initial: bool = False
) -> None:
    transaction = _transaction_dir()
    if initial:
        temporary_transaction = receipt_dir / (
            f".transaction.v1.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        )
        try:
            budget.mkdir(temporary_transaction)
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
    budget.write_new(temporary, payload, 0o600)
    _secure_state_file(temporary)
    if initial:
        budget.rename(temporary, transaction / "state.json", replace=True)
        budget.rename(transaction, _transaction_dir())
        transaction = _transaction_dir()
    else:
        budget.rename(temporary, _state_path(), replace=True)
    _secure_state_file(_state_path())


def persist_phase(
    wal: dict[str, Any],
    phase: str,
    budget: HostWriteBudget,
    *,
    committed: dict[str, Any] | None = None,
) -> None:
    if phase not in PHASES:
        fail("invalid transaction phase")
    updated = dict(wal)
    updated["phase"] = phase
    updated["committed"] = committed
    persist_wal(updated, budget)
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


def _make_backup(
    prestate: Prestate, budget: HostWriteBudget
) -> tuple[Path, ExecutableIdentity]:
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
        atomic_copy_fd(prestate.descriptor, backup, 0o700, budget)
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


def _record_job(wal: dict[str, Any], budget: HostWriteBudget, job: int) -> None:
    jobs = cast(list[int], wal["manager_job_ids"])
    if job not in jobs:
        updated = dict(wal)
        updated["manager_job_ids"] = [*jobs, job]
        persist_wal(updated, budget)
        wal.clear()
        wal.update(updated)


def _set_restart_intent(
    wal: dict[str, Any], budget: HostWriteBudget, value: bool
) -> None:
    if wal["restart_intent"] == value:
        return
    updated = dict(wal)
    updated["restart_intent"] = value
    persist_wal(updated, budget)
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
    budget: HostWriteBudget,
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
    _set_restart_intent(wal, budget, True)
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
        _record_job(wal, budget, job)
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
    _set_restart_intent(wal, budget, False)
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


def _remove_backup(wal: dict[str, Any], budget: HostWriteBudget) -> None:
    _deadline_checkpoint("rollback")
    backup = Path(cast(dict[str, Any], wal["prior"])["backup"]["path"])
    budget.unlink(backup, missing_ok=True, regular_mode=0o700)


def sweep_orphan_backups(budget: HostWriteBudget) -> None:
    _deadline_checkpoint("filesystem-read")
    rollback = receipt_dir / "rollback"
    rollback_fd = budget.ensure_private_directory(rollback)
    entries: list[str] = []
    for entry in _directory_entries(rollback_fd):
        _deadline_checkpoint("filesystem-read")
        entries.append(entry.name)
    if len(entries) > 64:
        fail("rollback backup inventory exceeded its bound")
    for name in entries:
        if re.fullmatch(r"[0-9a-f]{64}\.bin", name) is None:
            fail("rollback directory contains an unknown entry")
        budget.unlink(rollback / name, regular_mode=0o700)


def _snapshot_wal_identity(wal: dict[str, Any]) -> tuple[int, int]:
    value = wal["snapshot_identity"]
    if isinstance(value, tuple):
        return cast(tuple[int, int], value)
    payload = cast(dict[str, int], value)
    return payload["device"], payload["inode"]


def cleanup_transaction(wal: dict[str, Any], budget: HostWriteBudget) -> None:
    snapshot = Path(cast(str, wal["snapshot_root"]))
    cleanup_snapshot(
        snapshot,
        _snapshot_wal_identity(wal),
        budget=budget,
        parent_fd=budget.held_directory(snapshot.parent),
    )
    _remove_backup(wal, budget)
    transaction = _transaction_dir()
    transaction_fd = budget.held_directory(transaction)
    metadata = os.fstat(transaction_fd)
    cleanup_snapshot(
        transaction,
        (metadata.st_dev, metadata.st_ino),
        budget=budget,
        parent_fd=budget.held_directory(transaction.parent),
    )


def rollback_wal(wal: dict[str, Any], budget: HostWriteBudget) -> None:
    log("ROLLBACK_BEGIN=1")
    _deadline_checkpoint("rollback")
    _assert_fixed_authorities(wal)
    cancel_manager_jobs(wal)
    prestate = _prestate_from_wal(wal)
    if _exact_prior_running(wal):
        health_check()
        if not _exact_prior_running(wal):
            fail("exact prior generation drifted during rollback health check")
        cleanup_transaction(wal, budget)
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
                remove_service_link(budget)
            else:
                set_service_link(prestate.link_target, budget)
            if not _exact_prior_running(wal):
                fail("exact prior runtime or link drifted during rollback")
            health_check()
            cleanup_transaction(wal, budget)
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
        set_service_link(prestate.running_link, budget)
    else:
        prior_target = Path(prestate.link_target)
        if file_identity(prior_target) != prestate.executable:
            fail("prior-present rollback target no longer names exact prior object")
        set_service_link(prestate.link_target, budget)
    if _exact_prior_running(wal):
        health_check()
        if not _exact_prior_running(wal):
            fail("exact prior generation drifted during rollback health check")
        cleanup_transaction(wal, budget)
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
            budget,
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
            remove_service_link(budget)
        if not service_link_matches(prestate.link_target):
            fail("rollback did not restore exact prior service link state")
        cleanup_transaction(wal, budget)
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


def archive_committed(
    wal: dict[str, Any], budget: HostWriteBudget
) -> tuple[Path, str]:
    _deadline_checkpoint("rollback")
    if wal["phase"] != "committed":
        fail("only committed state may be archived")
    snapshot = Path(cast(str, wal["snapshot_root"]))
    cleanup_snapshot(
        snapshot,
        _snapshot_wal_identity(wal),
        budget=budget,
        parent_fd=budget.held_directory(snapshot.parent),
    )
    _remove_backup(wal, budget)
    receipts = receipt_dir / "receipts"
    receipts_fd = budget.ensure_private_directory(receipts)
    destination = receipts / cast(str, wal["txid"])
    try:
        os.stat(destination.name, dir_fd=receipts_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        fail("committed receipt destination already exists")
    transaction = _transaction_dir()
    transaction_fd = budget.ensure_private_directory(transaction)
    for entry in _directory_entries(transaction_fd):
        _deadline_checkpoint("filesystem-read")
        if entry.name == "state.json":
            continue
        if re.fullmatch(
            r"\.state\.tmp\.[1-9][0-9]*\.[0-9a-f]{8}", entry.name
        ) is None:
            fail("transaction contains an unknown publication artifact")
        _deadline_checkpoint("filesystem-write")
        budget.unlink(transaction / entry.name, regular_mode=0o600)
    budget.rename(transaction, destination)
    _secure_directory(destination)
    state_path = destination / "state.json"
    _secure_state_file(state_path)
    return state_path, sha256_file(state_path)


def recover_stale_transaction(
    wal: dict[str, Any], budget: HostWriteBudget
) -> None:
    phase = cast(str, wal["phase"])
    if phase == "committed":
        _verify_committed(wal)
        archive_committed(wal, budget)
    elif phase == "prestate" and _exact_prior_running(wal):
        _assert_fixed_authorities(wal)
        prove_no_manager_job()
        cleanup_transaction(wal, budget)
    else:
        if phase == "prestate":
            persist_phase(wal, "mutated", budget)
        rollback_wal(wal, budget)
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
    write_budget: HostWriteBudget | None = None
    lock_acquired = False
    try:
        write_budget = HostWriteBudget(cache_root)
        for destination in (receipt_dir, cache_root, service_bin):
            write_budget.reserve(destination, 0)
        write_budget.ensure_write_directory(service_bin.parent)
        lock_descriptor = acquire_rebuild_lock(write_budget)
        lock_acquired = True
        operation_deadline = time.monotonic() + _test_deadline(
            "LLM_GUARD_REBUILD_TEST_RECOVERY_SECONDS", RECOVERY_SECONDS
        )
        recovered_namespace = _prepare_transaction_namespace(write_budget)
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
            recover_stale_transaction(stale, write_budget)
            return 75
        if recovered_namespace:
            print(f"{RECOVERY_COMPLETE} phase=cleanup txid=unknown", flush=True)
            return 75

        operation_deadline = time.monotonic() + _test_deadline(
            "LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS", FORWARD_SECONDS
        )
        write_budget.ensure_private_directory(cache_root)
        sweep_orphan_scratch(write_budget)
        sweep_orphan_backups(write_budget)
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
        snapshot_root, snapshot_root_identity = create_snapshot_root(write_budget)
        source_bundle = prepare_canonical_source(snapshot_root, write_budget)
        build_bundle = build_sandboxed_candidate(source_bundle, write_budget)
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

        backup, backup_identity = _make_backup(prestate, write_budget)
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
        persist_wal(wal, write_budget, initial=True)
        _test_boundary("prestate-fsynced")
        persist_phase(wal, "mutated", write_budget)
        _test_boundary("mutated-fsynced")

        if not service_link_matches(str(candidate)):
            set_service_link(str(candidate), write_budget)
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
                write_budget,
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
        persist_phase(wal, "committed", write_budget, committed=committed)
        _test_boundary("committed-fsynced")
        _, receipt_digest = archive_committed(wal, write_budget)
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
                if write_budget is None:
                    fail("host write budget is unavailable during rollback")
                rollback_wal(wal, write_budget)
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
        cleanup_snapshot(
            snapshot_root,
            snapshot_root_identity,
            budget=write_budget,
            parent_fd=(
                None
                if snapshot_root is None or write_budget is None
                else write_budget.held_directory(snapshot_root.parent)
            ),
        )
        for held in reversed(list(held_tools.values())):
            held.close()
        held_tools.clear()
        for authority in fixed_authorities.values():
            authority.close()
        fixed_authorities.clear()
        operation_deadline = None
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if write_budget is not None:
            write_budget.close()


raise SystemExit(run_transaction())
