#!/usr/bin/python3
"""Fail-closed Linux user/network-namespace launcher and in-process proof gate."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import socket
import stat
import struct
import sys
from pathlib import Path
from typing import Sequence

UNSHARE_PATH = Path("/usr/bin/unshare")
OFFLINE_REPLAY_PATH = Path(__file__).resolve().with_name("querit_offline_replay.py")
_ISOLATED_BOOTSTRAP = (
    "import os,runpy,sys;"
    "p=sys.argv[1];sys.argv=sys.argv[1:];"
    "sys.path.insert(0,os.path.dirname(p));"
    "import querit_replay_sandbox as s;s.attest_running_system_python();"
    "runpy.run_path(p,run_name='__main__')"
)

_MAX_PYTHON_BYTES = 128 * 1024 * 1024
_MAX_SYMLINKS = 8


class SandboxError(RuntimeError):
    """A fresh, route-free Linux network namespace could not be proven."""


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _read_decimal(path: Path) -> int:
    try:
        with path.open("r", encoding="ascii") as handle:
            text = handle.read(32)
    except OSError as exc:
        raise SandboxError("cannot inspect user-namespace ownership mapping") from exc
    if not re.fullmatch(r"[0-9]+\n?", text):
        raise SandboxError("user-namespace overflow identity is malformed")
    return int(text)


def _host_id_is_unmapped(path: Path, host_id: int) -> bool:
    try:
        with path.open("r", encoding="ascii") as handle:
            text = handle.read(4096)
    except OSError as exc:
        raise SandboxError("cannot inspect user-namespace ownership mapping") from exc
    if len(text) >= 4096:
        raise SandboxError("user-namespace ownership mapping is oversized")
    ranges: list[tuple[int, int]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3 or any(not field.isdecimal() for field in fields):
            raise SandboxError("user-namespace ownership mapping is malformed")
        _, outside, length = map(int, fields)
        if length <= 0:
            raise SandboxError("user-namespace ownership mapping is empty")
        ranges.append((outside, length))
    if not ranges:
        raise SandboxError("user-namespace ownership mapping is unavailable")
    return not any(start <= host_id < start + length for start, length in ranges)


def _normalized_owner(
    info: os.stat_result, expected_uid: int, expected_gid: int
) -> tuple[int, int]:
    uid_matches = info.st_uid == expected_uid
    gid_matches = info.st_gid == expected_gid
    if expected_uid == 0 and not uid_matches:
        uid_matches = (
            info.st_uid == _read_decimal(Path("/proc/sys/kernel/overflowuid"))
            and _host_id_is_unmapped(Path("/proc/self/uid_map"), 0)
        )
    if expected_gid == 0 and not gid_matches:
        gid_matches = (
            info.st_gid == _read_decimal(Path("/proc/sys/kernel/overflowgid"))
            and _host_id_is_unmapped(Path("/proc/self/gid_map"), 0)
        )
    if not uid_matches or not gid_matches:
        raise SandboxError("Python symlink chain owner is not trusted")
    return expected_uid, expected_gid


def _chain_row(
    path: Path,
    info: os.stat_result,
    *,
    uid: int,
    gid: int,
    target: str | None = None,
) -> dict[str, object]:
    if stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif stat.S_ISREG(info.st_mode):
        kind = "regular"
    else:
        kind = "other"
    row: dict[str, object] = {
        "device": int(info.st_dev),
        "gid": gid,
        "inode": int(info.st_ino),
        "kind": kind,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
        "path": str(path),
        "size": int(info.st_size),
        "uid": uid,
    }
    if target is not None:
        row["target"] = target
    return row


def _attest_python_path(
    launcher: Path,
    *,
    trusted_roots: Sequence[Path],
    trusted_ancestors: Sequence[Path] = (),
    allowed_cross_root_links: Sequence[tuple[Path, Path, Path]] = (),
    expected_uid: int,
    expected_gid: int,
) -> dict[str, object]:
    """Resolve and hash one executable through a bounded declared trust graph."""

    def canonical(value: Path) -> Path:
        normalized = Path(os.path.normpath(os.fspath(value)))
        if not value.is_absolute() or normalized != value:
            raise SandboxError("trusted Python path declaration is not canonical and absolute")
        return normalized

    path = canonical(launcher)
    roots = tuple(canonical(root) for root in trusted_roots)
    if (
        not roots
        or len(set(roots)) != len(roots)
        or any(
            _under(left, right) or _under(right, left)
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        )
    ):
        raise SandboxError("trusted Python root declaration is invalid")

    def selected_root(candidate: Path) -> Path | None:
        return next((root for root in roots if _under(candidate, root)), None)

    root = selected_root(path)
    if root is None:
        raise SandboxError("Python launcher is outside trusted system roots")

    ancestors = tuple(canonical(ancestor) for ancestor in trusted_ancestors)
    if len(set(ancestors)) != len(ancestors) or any(
        not any(_under(candidate_root, ancestor) for candidate_root in roots)
        for ancestor in ancestors
    ):
        raise SandboxError("trusted Python ancestor declaration is invalid")

    transitions: set[tuple[Path, Path, Path]] = set()
    for source_root, link_path, target_path in allowed_cross_root_links:
        transition = (canonical(source_root), canonical(link_path), canonical(target_path))
        target_root = selected_root(transition[2])
        if (
            transition[0] not in roots
            or not _under(transition[1], transition[0])
            or target_root is None
            or target_root == transition[0]
            or transition in transitions
        ):
            raise SandboxError("trusted Python cross-root declaration is invalid")
        transitions.add(transition)

    chain: list[dict[str, object]] = []
    attested_directories: set[Path] = set()

    def attest_root(candidate_root: Path) -> None:
        for directory in (*ancestors, candidate_root):
            if not _under(candidate_root, directory) or directory in attested_directories:
                continue
            attested_directories.add(directory)
            try:
                info = directory.lstat()
            except OSError as exc:
                raise SandboxError("trusted Python root or ancestor is unavailable") from exc
            try:
                owner_uid, owner_gid = _normalized_owner(info, expected_uid, expected_gid)
            except SandboxError as exc:
                raise SandboxError("trusted Python root or ancestor ownership is unsafe") from exc
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_mode & 0o022
            ):
                raise SandboxError("trusted Python root or ancestor mode is unsafe")
            chain.append(_chain_row(directory, info, uid=owner_uid, gid=owner_gid))

    attest_root(root)
    pending = list(path.relative_to(root).parts)
    current = root
    followed: set[tuple[int, int]] = set()
    final: Path | None = None
    final_info: os.stat_result | None = None
    while pending:
        component = pending.pop(0)
        if component in ("", ".", ".."):
            raise SandboxError("Python symlink chain contains an unsafe component")
        candidate = current / component
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise SandboxError("Python symlink chain is unavailable") from exc
        owner_uid, owner_gid = _normalized_owner(info, expected_uid, expected_gid)
        if stat.S_ISLNK(info.st_mode):
            # Linux ignores symlink mode bits and reports 0777. The fully
            # attested non-writable parent directory is its mutation authority.
            identity = (info.st_dev, info.st_ino)
            if identity in followed or len(followed) >= _MAX_SYMLINKS:
                raise SandboxError("Python symlink chain is cyclic or too deep")
            followed.add(identity)
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                raise SandboxError("cannot read Python symlink target") from exc
            target_path = Path(target)
            if (
                not target
                or len(os.fsencode(target)) > 4096
                or target.startswith("//")
                or os.path.normpath(target) != target
                or any(part in (".", "..") for part in target_path.parts)
            ):
                raise SandboxError("Python symlink target is noncanonical or oversized")
            chain.append(
                _chain_row(candidate, info, uid=owner_uid, gid=owner_gid, target=target)
            )
            combined = target_path if target_path.is_absolute() else candidate.parent / target_path
            combined = Path(os.path.normpath(os.fspath(combined)))
            target_root = selected_root(combined)
            if target_root is None:
                raise SandboxError("Python symlink target escapes trusted system roots")
            if target_root != root:
                if pending or (root, candidate, combined) not in transitions:
                    raise SandboxError("Python symlink crosses an undeclared trusted root")
                attest_root(target_root)
            pending = [*combined.relative_to(target_root).parts, *pending]
            current = root = target_root
            continue
        chain.append(_chain_row(candidate, info, uid=owner_uid, gid=owner_gid))
        if pending:
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                raise SandboxError("Python symlink chain directory is unsafe")
            current = candidate
            continue
        final, final_info = candidate, info

    if final is None or final_info is None:
        raise SandboxError("Python launcher did not resolve to an executable")
    final_uid, final_gid = _normalized_owner(final_info, expected_uid, expected_gid)
    if (
        not stat.S_ISREG(final_info.st_mode)
        or final_info.st_mode & 0o022
        or not final_info.st_mode & 0o111
        or final_info.st_nlink != 1
        or not 0 < final_info.st_size <= _MAX_PYTHON_BYTES
    ):
        raise SandboxError("Python target is not a trusted bounded regular executable")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(final, flags)
    except OSError as exc:
        raise SandboxError("cannot open trusted Python target without following links") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        opened_uid, opened_gid = _normalized_owner(opened, expected_uid, expected_gid)
        if (
            identity != (final_info.st_dev, final_info.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened_uid != final_uid
            or opened_gid != final_gid
            or opened.st_mode & 0o022
            or not opened.st_mode & 0o111
            or opened.st_nlink != 1
            or not 0 < opened.st_size <= _MAX_PYTHON_BYTES
        ):
            raise SandboxError("trusted Python target changed before hashing")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PYTHON_BYTES:
                raise SandboxError("trusted Python target exceeded its hash bound")
            digest.update(chunk)
        after = final.lstat()
        if (
            (after.st_dev, after.st_ino) != identity
            or after.st_size != total
            or after.st_mode != opened.st_mode
            or after.st_uid != opened.st_uid
            or after.st_gid != opened.st_gid
            or after.st_nlink != 1
        ):
            raise SandboxError("trusted Python target changed during hashing")
    finally:
        os.close(descriptor)

    chain_bytes = json.dumps(
        chain, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "authority": "runtime-attested-local-system-python",
        "chain_sha256": hashlib.sha256(
            b"querit-system-python-chain-v1\0" + chain_bytes
        ).hexdigest(),
        "device": int(opened.st_dev),
        "gid": opened_gid,
        "inode": int(opened.st_ino),
        "launcher_path": str(path),
        "mode": f"{stat.S_IMODE(opened.st_mode):04o}",
        "nlink": int(opened.st_nlink),
        "resolved_path": str(final),
        "sha256": digest.hexdigest(),
        "size": total,
        "uid": opened_uid,
    }


def attest_system_python() -> dict[str, object]:
    """Attest the fixed production launcher; caller and environment cannot select it."""

    attestation = _attest_python_path(
        Path("/usr/bin/python3"),
        trusted_roots=(Path("/usr/bin"), Path("/usr/local/bin")),
        trusted_ancestors=(
            Path("/"),
            Path("/usr"),
            Path("/usr/bin"),
            Path("/usr/local"),
            Path("/usr/local/bin"),
        ),
        allowed_cross_root_links=(
            (
                Path("/usr/bin"),
                Path("/usr/bin/python3"),
                Path("/usr/local/bin/python3.12"),
            ),
        ),
        expected_uid=0,
        expected_gid=0,
    )
    resolved = attestation["resolved_path"]
    if not isinstance(resolved, str) or not (
        re.fullmatch(r"/usr/bin/python3(?:\.[0-9]+)*", resolved)
        or resolved == "/usr/local/bin/python3.12"
    ):
        raise SandboxError("trusted system Python resolved to an undeclared executable")
    return attestation


def attest_running_system_python() -> dict[str, object]:
    """Prove the isolated process is the fixed interpreter and meets language needs."""

    attestation = attest_system_python()
    running = Path(os.path.realpath(sys.executable))
    if str(running) != attestation["resolved_path"]:
        raise SandboxError("running Python differs from the trusted system interpreter")
    if not (3, 11) <= sys.version_info[:2] < (4, 0):
        raise SandboxError("trusted system Python version is unsupported")
    if not sys.flags.isolated:
        raise SandboxError("trusted system Python is not running in isolated mode")
    for module in ("argparse", "hashlib", "json", "math", "pathlib", "re", "stat", "struct"):
        try:
            __import__(module)
        except ImportError as exc:
            raise SandboxError("trusted system Python lacks a required standard import") from exc
    return attestation


def _read_bounded(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SandboxError("cannot read network-namespace proof source") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
            raise SandboxError("network-namespace proof source is invalid or oversized")
        data = bytearray()
        while len(data) <= maximum_bytes:
            chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum_bytes:
            raise SandboxError("network-namespace proof source exceeded its bound")
        return bytes(data)
    finally:
        os.close(descriptor)


def _namespace_identity(path: Path) -> str:
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise SandboxError("cannot inspect Linux network namespace identity") from exc
    if not re.fullmatch(r"net:\[[0-9]+\]", target):
        raise SandboxError("Linux network namespace identity is malformed")
    return target


def _network_interfaces() -> list[str]:
    lines = _read_bounded(Path("/proc/net/dev"), 64 * 1024).splitlines()
    if len(lines) < 2:
        raise SandboxError("network interface proof is malformed")
    interfaces: list[str] = []
    for line in lines[2:]:
        if b":" not in line:
            raise SandboxError("network interface proof is malformed")
        name = line.split(b":", 1)[0].strip().decode("ascii", "strict")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise SandboxError("network interface name is malformed")
        interfaces.append(name)
    return sorted(interfaces)


def _loopback_is_down() -> bool:
    request = struct.pack("16sH14s", b"lo", 0, b"")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            response = fcntl.ioctl(probe.fileno(), 0x8913, request)
    except OSError as exc:
        raise SandboxError("cannot inspect isolated loopback flags") from exc
    flags = struct.unpack("16sH14s", response)[1]
    return not bool(flags & 0x1)


def _usable_ipv6_routes() -> int:
    count = 0
    for line in _read_bounded(Path("/proc/net/ipv6_route"), 64 * 1024).splitlines():
        fields = line.split()
        if len(fields) != 10:
            raise SandboxError("IPv6 route proof is malformed")
        try:
            flags = int(fields[8], 16)
        except ValueError as exc:
            raise SandboxError("IPv6 route flags are malformed") from exc
        # Fresh namespaces contain kernel-generated reject routes for loopback.
        if not flags & 0x200:
            count += 1
    return count


def attest_network_isolation() -> str:
    """Prove a fresh namespace with only a down loopback and no network routes."""

    if platform.system() != "Linux":
        raise SandboxError("Querit replay network isolation is Linux-only")
    current = _namespace_identity(Path("/proc/self/ns/net"))
    parent = os.environ.get("QUERIT_PARENT_NETNS", "")
    if not re.fullmatch(r"net:\[[0-9]+\]", parent):
        raise SandboxError("trusted launcher network namespace identity is unavailable")
    if current == parent:
        raise SandboxError("replay is still in the launcher network namespace")
    interfaces = _network_interfaces()
    if interfaces != ["lo"]:
        raise SandboxError("isolated namespace contains a non-loopback interface")
    if not _loopback_is_down():
        raise SandboxError("isolated loopback interface is not down")
    route_lines = _read_bounded(Path("/proc/net/route"), 64 * 1024).splitlines()
    if route_lines and not (
        len(route_lines) == 1 and route_lines[0].startswith(b"Iface")
    ):
        raise SandboxError("isolated namespace has an IPv4 route")
    ipv6_routes = _usable_ipv6_routes()
    if ipv6_routes:
        raise SandboxError("isolated namespace has a usable IPv6 route")
    proof = {
        "interfaces": interfaces,
        "ipv4_routes": 0,
        "ipv6_routes": 0,
        "loopback_operstate": "down",
        "method": "linux-unshare-user-net-v1",
        "namespace_differs_from_launcher": True,
    }
    encoded = json.dumps(proof, separators=(",", ":"), sort_keys=True).encode("ascii")
    return hashlib.sha256(b"querit-network-isolation-v1\0" + encoded).hexdigest()


def build_unshare_argv(arguments: Sequence[str]) -> list[str]:
    """Build the only supported replay command: fresh user+network namespace."""

    if not arguments or arguments[0] != "run":
        raise SandboxError("sandbox launcher accepts only the replay run command")
    try:
        unshare = UNSHARE_PATH.lstat()
        replay = OFFLINE_REPLAY_PATH.lstat()
        python_attestation = attest_system_python()
    except OSError as exc:
        raise SandboxError("sandbox launcher dependency is unavailable") from exc
    if (
        not stat.S_ISREG(unshare.st_mode)
        or stat.S_ISLNK(unshare.st_mode)
        or unshare.st_uid != 0
        or unshare.st_mode & 0o022
    ):
        raise SandboxError("unshare executable is not trusted")
    if not stat.S_ISREG(replay.st_mode) or stat.S_ISLNK(replay.st_mode):
        raise SandboxError("offline replay entry point is not a regular source file")
    return [
        str(UNSHARE_PATH),
        "--user",
        "--map-root-user",
        "--net",
        "--",
        str(python_attestation["resolved_path"]),
        "-I",
        "-c",
        _ISOLATED_BOOTSTRAP,
        str(OFFLINE_REPLAY_PATH),
        *arguments,
    ]


def sanitized_environment() -> dict[str, str]:
    """Return a minimal credential-free environment plus the parent namespace proof."""

    environment = {
        "HOME": "/nonexistent",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "QUERIT_PARENT_NETNS": _namespace_identity(Path("/proc/self/ns/net")),
        "TRANSFORMERS_OFFLINE": "1",
    }
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is not None:
        if not re.fullmatch(r"(?:-1|[0-9]+(?:,[0-9]+)*)", visible):
            raise SandboxError("CUDA_VISIBLE_DEVICES is malformed")
        environment["CUDA_VISIBLE_DEVICES"] = visible
    return environment


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = build_unshare_argv(arguments)
    try:
        os.execve(command[0], command, sanitized_environment())
    except OSError as exc:
        raise SandboxError("cannot establish the required network namespace") from exc
    raise AssertionError("unreachable after execve")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SandboxError as error:
        print(f"querit replay sandbox failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
