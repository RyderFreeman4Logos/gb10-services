from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

__all__: list[str] = []

_FRAME_MAGIC = b"GB10ART1"
_FRAME_HEADER_BYTES = 16 * 1024 * 1024
_LOG_BYTES = 4 * 1024 * 1024
_ARCHIVE_BYTES = 160 * 1024 * 1024
_CANDIDATE_BYTES = 128 * 1024 * 1024
_READ_BYTES = 64 * 1024
_TARGET_TRIPLE = "aarch64-unknown-linux-gnu"
_EXPECTED_GIT_CONFIG = (
    b"[core]\n"
    b"\trepositoryformatversion = 0\n"
    b"\tfilemode = true\n"
    b"\tbare = true\n"
    b"[gc]\n"
    b"\tauto = 0\n"
    b"[transfer]\n"
    b"\tfsckobjects = true\n"
    b"[fetch]\n"
    b"\tfsckobjects = true\n"
    b"[receive]\n"
    b"\tfsckobjects = true\n"
)


class WorkerError(RuntimeError):
    pass


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _strict_object(payload: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=pairs)
    except (ValueError, json.JSONDecodeError) as error:
        raise WorkerError("configuration") from error
    if not isinstance(value, dict):
        raise WorkerError("configuration")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise WorkerError("configuration")


def _read_small(path: Path, maximum: int = 64 * 1024) -> bytes:
    try:
        with path.open("rb", buffering=0) as stream:
            payload = stream.read(maximum + 1)
    except OSError as error:
        raise WorkerError("scope") from error
    if len(payload) > maximum:
        raise WorkerError("scope")
    return payload


def _self_attest(unit: str, policy: object) -> None:
    if not isinstance(policy, dict):
        raise WorkerError("scope")
    required = {
        "cpu_percent",
        "fsize_bytes",
        "memory_high",
        "memory_max",
        "min_mem_available",
        "phase",
        "runtime_seconds",
        "tasks_max",
    }
    _exact_keys(policy, required)
    if not unit.endswith(".scope") or Path(unit).name != unit:
        raise WorkerError("scope")
    try:
        lines = _read_small(Path("/proc/self/cgroup")).decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise WorkerError("scope") from error
    if lines != ["0::/"]:
        raise WorkerError("scope")
    root = Path("/sys/fs/cgroup")
    expected = {
        "memory.high": str(policy["memory_high"]),
        "memory.max": str(policy["memory_max"]),
        "memory.oom.group": "1",
        "memory.swap.max": "0",
        "pids.max": str(policy["tasks_max"]),
        "cpu.max": f"{int(policy['cpu_percent']) * 1000} 100000",
    }
    try:
        for name, value in expected.items():
            if _read_small(root / name).decode("ascii").strip() != value:
                raise WorkerError("scope")
        if str(os.getpid()).encode("ascii") not in _read_small(root / "cgroup.procs").split():
            raise WorkerError("scope")
        events = dict(
            line.split() for line in _read_small(root / "cgroup.events").decode("ascii").splitlines()
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise WorkerError("scope") from error
    if events.get("populated") != "1":
        raise WorkerError("scope")


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    for number, wait_seconds in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 1.0)):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
    raise WorkerError("child-cleanup")


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    output_limit: int = _LOG_BYTES,
) -> bytes:
    try:
        process = subprocess.Popen(
            list(arguments),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        raise WorkerError("child-spawn") from error
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    streams: dict[int, object] = {}
    output = bytearray()
    diagnostic = bytearray()
    deadline = time.monotonic() + timeout
    try:
        for stream in (process.stdout, process.stderr):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
            streams[descriptor] = stream
        while streams or process.poll() is None:
            if time.monotonic() >= deadline:
                raise WorkerError("child-timeout")
            for key, _ in selector.select(0.05):
                descriptor = key.fd
                try:
                    payload = os.read(descriptor, _READ_BYTES)
                except BlockingIOError:
                    continue
                if not payload:
                    selector.unregister(descriptor)
                    streams.pop(descriptor).close()  # type: ignore[union-attr]
                    continue
                capture = output if descriptor == process.stdout.fileno() else diagnostic
                limit = output_limit if capture is output else _LOG_BYTES
                if len(capture) + len(payload) > limit:
                    raise WorkerError("child-output")
                capture.extend(payload)
            process.poll()
        if process.returncode != 0:
            raise WorkerError("child-status")
        return bytes(output)
    except BaseException:
        _kill_group(process)
        raise
    finally:
        for descriptor, stream in list(streams.items()):
            try:
                selector.unregister(descriptor)
            except (KeyError, ValueError):
                pass
            stream.close()  # type: ignore[union-attr]
        selector.close()


def _run_to_file(
    arguments: Sequence[str],
    destination: Path,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    size_limit: int,
) -> None:
    try:
        with destination.open("xb", buffering=0) as output:
            process = subprocess.Popen(
                list(arguments),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
            assert process.stderr is not None
            os.set_blocking(process.stderr.fileno(), False)
            diagnostic = bytearray()
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline or output.tell() > size_limit:
                        raise WorkerError("child-bound")
                    try:
                        payload = os.read(process.stderr.fileno(), _READ_BYTES)
                    except BlockingIOError:
                        payload = b""
                    if len(diagnostic) + len(payload) > _LOG_BYTES:
                        raise WorkerError("child-output")
                    diagnostic.extend(payload)
                    time.sleep(0.02)
                if process.returncode != 0 or output.tell() > size_limit:
                    raise WorkerError("child-status")
            except BaseException:
                _kill_group(process)
                raise
    except FileExistsError as error:
        raise WorkerError("destination") from error


def _git_env(protocol: str) -> dict[str, str]:
    if protocol not in {"https", "file"}:
        raise WorkerError("protocol")
    return {
        "GIT_ALLOW_PROTOCOL": protocol,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_EXEC_PATH": "/git-exec",
        "GIT_PROTOCOL_FROM_USER": "0",
        "HOME": "/fetch",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/tools",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    }


def _git(arguments: Sequence[str], env: Mapping[str, str], *, output_limit: int = _LOG_BYTES) -> bytes:
    return _run(
        ["/tools/git", *arguments],
        cwd=Path("/fetch"),
        env=env,
        timeout=180,
        output_limit=output_limit,
    )


def _oid(payload: bytes) -> str:
    value = payload.decode("ascii", errors="strict").strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise WorkerError("oid")
    return value


def _parse_tree(payload: bytes) -> tuple[list[dict[str, object]], int]:
    if not payload.endswith(b"\0"):
        raise WorkerError("tree")
    entries: list[dict[str, object]] = []
    total = 0
    for record in payload[:-1].split(b"\0"):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, raw_oid, raw_size = metadata.split()
            path = raw_path.decode("utf-8", errors="strict")
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as error:
            raise WorkerError("tree") from error
        if (
            kind != b"blob"
            or mode not in {b"100644", b"100755", b"120000"}
            or len(raw_oid) != 40
            or any(byte not in b"0123456789abcdef" for byte in raw_oid)
            or size < 0
            or not path
            or "\x00" in path
        ):
            raise WorkerError("tree")
        entries.append(
            {
                "mode": int(mode, 8),
                "oid": raw_oid.decode("ascii"),
                "path": path,
                "size": size,
            }
        )
        total += size
        if len(entries) > 16384 or total > _CANDIDATE_BYTES:
            raise WorkerError("tree-bound")
    return entries, total


def _write_frame(header: dict[str, object], payload_path: Path | None, payload: bytes | None) -> None:
    if (payload_path is None) == (payload is None):
        raise WorkerError("frame")
    encoded = _canonical_json(header)
    if len(encoded) > _FRAME_HEADER_BYTES:
        raise WorkerError("frame-header")
    os.write(1, _FRAME_MAGIC + struct.pack(">I", len(encoded)) + encoded)
    if payload is not None:
        view = memoryview(payload)
        while view:
            view = view[os.write(1, view):]
        return
    assert payload_path is not None
    with payload_path.open("rb", buffering=0) as stream:
        while True:
            chunk = stream.read(_READ_BYTES)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                view = view[os.write(1, view):]


def _fetch(config: Mapping[str, object]) -> None:
    _exact_keys(config, {"policy", "source_protocol", "source_ref", "source_repo"})
    source_repo = config["source_repo"]
    source_ref = config["source_ref"]
    protocol = config["source_protocol"]
    if (
        not isinstance(source_repo, str)
        or not source_repo
        or not isinstance(source_ref, str)
        or not source_ref
        or not isinstance(protocol, str)
        or not protocol
    ):
        raise WorkerError("configuration")
    os.mkdir("/fetch/objects.git", 0o700)
    os.mkdir("/git-exec", 0o700)
    os.symlink("/tools/git-remote-https", "/git-exec/git-remote-https")
    os.symlink("/tools/git-remote-https", "/git-exec/git-remote-http")
    env = _git_env(protocol)
    _git(["init", "--bare", "/fetch/objects.git"], env)
    config_path = Path("/fetch/objects.git/config")
    if config_path.read_bytes() != _EXPECTED_GIT_CONFIG:
        raise WorkerError("git-config")
    _git(["-C", "/fetch/objects.git", "remote", "add", "origin", source_repo], env)
    _git(
        [
            "-C",
            "/fetch/objects.git",
            "-c",
            "protocol.version=2",
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--force",
            "--depth=1",
            "origin",
            f"+{source_ref}:refs/rebuild/candidate",
        ],
        env,
    )
    commit = _oid(_git(["-C", "/fetch/objects.git", "rev-parse", "refs/rebuild/candidate^{commit}"], env))
    tree = _oid(_git(["-C", "/fetch/objects.git", "rev-parse", f"{commit}^{{tree}}"], env))
    _git(["-C", "/fetch/objects.git", "fsck", "--strict", "--full", "--no-dangling", commit], env)
    missing = _git(["-C", "/fetch/objects.git", "rev-list", "--objects", "--missing=print", commit], env)
    if any(line.startswith(b"?") for line in missing.splitlines()):
        raise WorkerError("missing-object")
    entries, total = _parse_tree(
        _git(
            ["-C", "/fetch/objects.git", "ls-tree", "-lrz", "--full-tree", commit],
            env,
            output_limit=_FRAME_HEADER_BYTES,
        )
    )
    archive = Path("/fetch/source.tar")
    _run_to_file(
        ["/tools/git", "-C", "/fetch/objects.git", "archive", "--format=tar", commit],
        archive,
        cwd=Path("/fetch"),
        env=env,
        timeout=180,
        size_limit=_ARCHIVE_BYTES,
    )
    archive_size = archive.stat().st_size
    if archive_size <= 0 or archive_size > _ARCHIVE_BYTES:
        raise WorkerError("archive-bound")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    _write_frame(
        {
            "archive_sha256": digest,
            "byte_count": total,
            "commit": commit,
            "entries": entries,
            "file_count": len(entries),
            "git_config_sha256": hashlib.sha256(_EXPECTED_GIT_CONFIG).hexdigest(),
            "kind": "fetch",
            "payload_size": archive_size,
            "schema": 1,
            "tree": tree,
        },
        archive,
        None,
    )


def _cargo_env() -> dict[str, str]:
    return {
        "AR": "/usr/bin/aarch64-linux-gnu-ar",
        "AR_aarch64_unknown_linux_gnu": "/usr/bin/aarch64-linux-gnu-ar",
        "AS": "/usr/bin/aarch64-linux-gnu-as",
        "CARGO_HOME": "/cargo-home",
        "CARGO_NET_OFFLINE": "true",
        "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER": "/usr/bin/aarch64-linux-gnu-gcc-13",
        "CARGO_TARGET_DIR": "/target",
        "CC": "/usr/bin/aarch64-linux-gnu-gcc-13",
        "CC_aarch64_unknown_linux_gnu": "/usr/bin/aarch64-linux-gnu-gcc-13",
        "HOME": "/tmp",
        "LANG": "C",
        "LC_ALL": "C",
        "LD": "/usr/bin/aarch64-linux-gnu-ld.bfd",
        "PATH": "/toolchain/bin:/usr/bin",
        "RUST_BACKTRACE": "0",
        "RUSTC": "/toolchain/bin/rustc",
        "SOURCE_DATE_EPOCH": "0",
        "TMPDIR": "/tmp",
    }


def _metadata(config: Mapping[str, object]) -> None:
    _exact_keys(config, {"policy"})
    payload = _run(
        [
            "/toolchain/bin/cargo",
            "metadata",
            "--locked",
            "--offline",
            "--format-version=1",
            "--manifest-path",
            "/src/llm-guard-proxy/Cargo.toml",
        ],
        cwd=Path("/src"),
        env=_cargo_env(),
        timeout=120,
        output_limit=_FRAME_HEADER_BYTES,
    )
    _write_frame(
        {"kind": "metadata", "payload_size": len(payload), "schema": 1},
        None,
        payload,
    )


def _normalize_candidate(path: Path) -> int:
    try:
        info = path.lstat()
    except OSError as error:
        raise WorkerError("candidate") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
        or not stat.S_IMODE(info.st_mode) & 0o111
        or info.st_size <= 0
        or info.st_size > _CANDIDATE_BYTES
    ):
        raise WorkerError("candidate")
    if info.st_nlink != 1:
        replacement = path.with_name(".candidate-normalized")
        source = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        destination = os.open(
            replacement,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o700,
        )
        try:
            while True:
                chunk = os.read(source, _READ_BYTES)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(destination, view):]
            os.fsync(destination)
        finally:
            os.close(destination)
            os.close(source)
        os.replace(replacement, path)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        final = os.fstat(descriptor)
        if not stat.S_ISREG(final.st_mode) or final.st_nlink != 1 or final.st_size <= 0:
            raise WorkerError("candidate")
        return final.st_size
    finally:
        os.close(descriptor)


def _build(config: Mapping[str, object]) -> None:
    _exact_keys(config, {"policy"})
    _run(
        [
            "/toolchain/bin/cargo",
            "build",
            "--release",
            "--locked",
            "--offline",
            "--target",
            _TARGET_TRIPLE,
            "--manifest-path",
            "/src/llm-guard-proxy/Cargo.toml",
            "--package",
            "llm-guard-proxy",
            "--no-default-features",
            "--features",
            "guard",
        ],
        cwd=Path("/src"),
        env=_cargo_env(),
        timeout=1800,
    )
    candidate = Path(f"/target/{_TARGET_TRIPLE}/release/llm-guard-proxy")
    size = _normalize_candidate(candidate)
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    _write_frame(
        {
            "kind": "build",
            "payload_sha256": digest,
            "payload_size": size,
            "schema": 1,
        },
        candidate,
        None,
    )


def _resource_snapshot_fence() -> None:
    if os.environ.get("GB10_RESOURCE_FENCE") != "1":
        return
    try:
        if os.write(0, b"R") != 1 or os.read(0, 2) != b"G":
            raise WorkerError("resource-fence")
    except OSError as error:
        raise WorkerError("resource-fence") from error


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in {"fetch", "metadata", "build"}:
        raise WorkerError("arguments")
    operation = sys.argv[1]
    config = _strict_object(sys.argv[2])
    _self_attest(sys.argv[3], config.get("policy"))
    try:
        if operation == "fetch":
            _fetch(config)
        elif operation == "metadata":
            _metadata(config)
        else:
            _build(config)
    except BaseException as error:
        try:
            _resource_snapshot_fence()
        except BaseException as fence_error:
            error.add_note(f"resource fence diagnostic: {fence_error}")
        raise
    _resource_snapshot_fence()
    return 0


if __name__ == "__main__":
    try:
        result = main()
    except WorkerError as error:
        sys.stderr.write(f"worker_failure phase={sys.argv[1] if len(sys.argv) > 1 else 'unknown'} reason={error}\n")
        raise SystemExit(1)
    except BaseException:
        sys.stderr.write(
            f"worker_failure phase={sys.argv[1] if len(sys.argv) > 1 else 'unknown'} reason=internal\n"
        )
        raise SystemExit(1)
    raise SystemExit(result)
