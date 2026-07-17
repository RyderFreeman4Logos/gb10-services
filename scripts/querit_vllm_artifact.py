#!/usr/bin/env python3
"""Create and verify the immutable manifest for a converted Querit artifact."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path


MANIFEST_NAME = "querit-vllm-artifact-manifest.json"
SOURCE_REVISION = "7b796de30ad8dc772d6c46c75659c1341283a665"
TRANSFORM = "querit-tanh-scalar-head-v1"
MAX_FILES = 4096
MAX_FILE_BYTES = 32 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
EXPECTED_TEMPLATE_SHA256 = (
    "048e14c0ed521d08717dd31b89990e4bf7ac12366c9ae338f2a2cc5d5d7b301d"
)


class ArtifactError(RuntimeError):
    """The converted model tree does not match its immutable manifest."""


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _hash_regular(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactError(f"cannot open artifact file: {path.name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 0
            or metadata.st_size > MAX_FILE_BYTES
        ):
            raise ArtifactError(f"artifact file is unsafe: {path.name}")
        digest = hashlib.sha256()
        observed = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > MAX_FILE_BYTES:
                raise ArtifactError(f"artifact file grew beyond limit: {path.name}")
            digest.update(chunk)
        if observed != metadata.st_size:
            raise ArtifactError(f"artifact file changed during hashing: {path.name}")
        return observed, digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size < 0
                or before.st_size > MAX_METADATA_BYTES
            ):
                raise ArtifactError(f"{label} is not a bounded regular file")
            raw = os.read(descriptor, MAX_METADATA_BYTES + 1)
            after = os.fstat(descriptor)
            stable_before = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_nlink,
                before.st_uid,
                before.st_gid,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            stable_after = (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_nlink,
                after.st_uid,
                after.st_gid,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if stable_before != stable_after or len(raw) != before.st_size:
                raise ArtifactError(f"{label} changed while it was read")
        finally:
            os.close(descriptor)
        decoded = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ArtifactError(f"{label} must be a JSON object")
    return decoded


def _verify_semantics(root: Path) -> None:
    config = _read_json_object(root / "config.json", "converted config")
    expected = {
        "architectures": ["Qwen3ForSequenceClassification"],
        "head_dtype": "model",
        "hidden_size": 2560,
        "num_labels": 1,
        "sbert_ce_default_activation_function": (
            "torch.nn.modules.activation.Tanh"
        ),
    }
    for key, value in expected.items():
        if config.get(key) != value:
            raise ArtifactError(f"converted config {key} is incompatible")
    context = config.get("max_position_embeddings")
    if isinstance(context, bool) or not isinstance(context, int) or context < 32768:
        raise ArtifactError("converted config does not attest 32,768-token context")
    if "problem_type" in config:
        raise ArtifactError("converted config must not override the scoring problem type")
    _size, template_sha256 = _hash_regular(root / "querit-rerank.jinja")
    if template_sha256 != EXPECTED_TEMPLATE_SHA256:
        raise ArtifactError("converted artifact score template is not the pinned template")


def _inventory(root: Path) -> list[dict[str, object]]:
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ArtifactError("cannot inspect artifact root") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise ArtifactError("artifact root must be a real directory")
    files: list[dict[str, object]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for dirname in dirnames:
            child = directory_path / dirname
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ArtifactError(f"artifact contains unsafe directory: {child.name}")
        for filename in filenames:
            path = directory_path / filename
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST_NAME:
                continue
            if path.is_symlink():
                raise ArtifactError(f"artifact contains symlink: {relative}")
            size, digest = _hash_regular(path)
            files.append({"path": relative, "sha256": digest, "size": size})
            if len(files) > MAX_FILES:
                raise ArtifactError("artifact contains too many files")
    files.sort(key=lambda row: str(row["path"]))
    names = {row["path"] for row in files}
    required = {
        "config.json",
        "model.safetensors.index.json",
        "querit-rerank.jinja",
    }
    if not required.issubset(names) or not any(
        str(name).endswith(".safetensors") for name in names
    ):
        raise ArtifactError("converted artifact is missing required model files")
    _verify_semantics(root)
    return files


def write_manifest(root: Path) -> dict[str, object]:
    manifest = {
        "files": _inventory(root),
        "schema": "querit-vllm-artifact-manifest-v1",
        "source_revision": SOURCE_REVISION,
        "transform": TRANSFORM,
    }
    _atomic_write(root / MANIFEST_NAME, _json_bytes(manifest))
    return manifest


def verify_manifest(root: Path) -> dict[str, object]:
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise ArtifactError("artifact manifest must not be a symlink")
    _hash_regular(manifest_path)
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("artifact manifest is unreadable") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "files",
        "schema",
        "source_revision",
        "transform",
    }:
        raise ArtifactError("artifact manifest fields are not exact")
    if (
        manifest["schema"] != "querit-vllm-artifact-manifest-v1"
        or manifest["source_revision"] != SOURCE_REVISION
        or manifest["transform"] != TRANSFORM
        or _json_bytes(manifest) != raw
        or manifest["files"] != _inventory(root)
    ):
        raise ArtifactError("artifact manifest does not match converted files")
    return manifest


def manifest_sha256(root: Path) -> str:
    verify_manifest(root)
    return _hash_regular(root / MANIFEST_NAME)[1]


__all__ = [
    "ArtifactError",
    "MANIFEST_NAME",
    "SOURCE_REVISION",
    "TRANSFORM",
    "manifest_sha256",
    "verify_manifest",
    "write_manifest",
]
