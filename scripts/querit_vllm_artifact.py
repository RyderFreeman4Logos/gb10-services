#!/usr/bin/env python3
"""Create and verify the immutable manifest for a converted Querit artifact."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import time
from collections.abc import Mapping
from pathlib import Path

from querit_replay_trust import (
    TRUSTED_MODEL_FILES,
    TRUSTED_MODEL_LEDGER_SHA256,
    TrustError,
    attest_trusted_snapshot,
)


MANIFEST_NAME = "querit-vllm-artifact-manifest.json"
SOURCE_REVISION = "7b796de30ad8dc772d6c46c75659c1341283a665"
TRANSFORM = "querit-tanh-scalar-head-v1"
MANIFEST_SCHEMA = "querit-vllm-artifact-manifest-v2"
MAX_FILES = 4096
MAX_FILE_BYTES = 32 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_SAFETENSORS_HEADER_BYTES = 16 * 1024 * 1024
INDEX_NAME = "model.safetensors.index.json"
SOURCE_TOTAL_SIZE = 8_043_564_036
OUTPUT_TOTAL_SIZE = 8_043_558_914
TOTAL_SIZE_DELTA = OUTPUT_TOTAL_SIZE - SOURCE_TOTAL_SIZE
SCALAR_HEAD_KEYS = {
    "score.bias": "model-00002-of-00002.safetensors",
    "score.weight": "model-00002-of-00002.safetensors",
}
OBSOLETE_HEAD_KEYS = frozenset({"head.bias", "head.weight"})
EXPECTED_TEMPLATE_SHA256 = (
    "048e14c0ed521d08717dd31b89990e4bf7ac12366c9ae338f2a2cc5d5d7b301d"
)
SOURCE_LEDGER = tuple(dict(row) for row in TRUSTED_MODEL_FILES)
SOURCE_LEDGER_SHA256 = TRUSTED_MODEL_LEDGER_SHA256


class ArtifactError(RuntimeError):
    """The converted model tree does not match its immutable manifest."""


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _ledger_sha256(ledger: object, domain: bytes) -> str:
    digest = hashlib.sha256(domain)
    digest.update(_canonical_bytes(ledger))
    return digest.hexdigest()


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON object has duplicate keys")
        value[key] = item
    return value


SOURCE_TREE_SHA256 = _ledger_sha256(list(SOURCE_LEDGER), b"querit-snapshot-tree-v1\0")


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
        decoded = json.loads(raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ArtifactError(f"{label} must be a JSON object")
    return decoded


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise ArtifactError("safetensors header is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _safetensors_tensor_keys(path: Path) -> set[str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactError(f"cannot open safetensors shard: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 8
            or before.st_size > MAX_FILE_BYTES
        ):
            raise ArtifactError(f"safetensors shard is unsafe: {path.name}")
        header_size = struct.unpack("<Q", _read_exact(descriptor, 8))[0]
        if (
            header_size > MAX_SAFETENSORS_HEADER_BYTES
            or header_size > before.st_size - 8
        ):
            raise ArtifactError(f"safetensors header is invalid: {path.name}")
        raw = _read_exact(descriptor, header_size)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ArtifactError(f"safetensors shard changed while read: {path.name}")
    finally:
        os.close(descriptor)
    try:
        header = json.loads(raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"safetensors header is not JSON: {path.name}") from exc
    if not isinstance(header, dict):
        raise ArtifactError(f"safetensors header is not an object: {path.name}")
    keys: set[str] = set()
    payload_size = before.st_size - 8 - header_size
    for name, details in header.items():
        if name == "__metadata__":
            if not isinstance(details, dict):
                raise ArtifactError(f"safetensors metadata is invalid: {path.name}")
            continue
        if not isinstance(name, str) or not name or not isinstance(details, dict):
            raise ArtifactError(f"safetensors tensor entry is invalid: {path.name}")
        offsets = details.get("data_offsets")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(
                isinstance(offset, bool) or not isinstance(offset, int)
                for offset in offsets
            )
            or offsets[0] < 0
            or offsets[0] > offsets[1]
            or offsets[1] > payload_size
        ):
            raise ArtifactError(f"safetensors tensor range is invalid: {path.name}")
        keys.add(name)
    if not keys:
        raise ArtifactError(f"safetensors shard has no tensors: {path.name}")
    return keys


def _verify_index_and_tensors(root: Path) -> None:
    index = _read_json_object(root / INDEX_NAME, "converted weight index")
    metadata = index.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ArtifactError("converted weight index metadata is invalid")
    total_size = metadata.get("total_size")
    if (
        isinstance(total_size, bool)
        or not isinstance(total_size, int)
        or total_size != OUTPUT_TOTAL_SIZE
    ):
        raise ArtifactError("converted weight index total_size is not pinned")
    if total_size - SOURCE_TOTAL_SIZE != TOTAL_SIZE_DELTA:
        raise ArtifactError("converted weight index total_size delta is not pinned")
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, Mapping) or not weight_map:
        raise ArtifactError("converted weight index weight_map is invalid")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in weight_map.items()
    ):
        raise ArtifactError("converted weight index key or shard is invalid")
    if not SCALAR_HEAD_KEYS.items() <= weight_map.items():
        raise ArtifactError("converted weight index lacks scalar score keys")
    if OBSOLETE_HEAD_KEYS & set(weight_map) or any(
        key.startswith("head.") for key in weight_map
    ):
        raise ArtifactError(
            "converted weight index retains obsolete two-class head keys"
        )

    expected_shards = {
        str(row["path"])
        for row in SOURCE_LEDGER
        if str(row["path"]).endswith(".safetensors")
    }
    actual_shards = {
        path.name for path in root.iterdir() if path.name.endswith(".safetensors")
    }
    if set(weight_map.values()) != expected_shards or actual_shards != expected_shards:
        raise ArtifactError("converted weight index shard set is invalid")

    consumed: dict[str, str] = {}
    for shard in sorted(expected_shards):
        for tensor in _safetensors_tensor_keys(root / shard):
            if tensor in consumed:
                raise ArtifactError("converted tensor is present in multiple shards")
            consumed[tensor] = shard
    if dict(weight_map) != consumed:
        raise ArtifactError("every converted tensor must be consumed exactly once")


def _verify_semantics(root: Path) -> None:
    config = _read_json_object(root / "config.json", "converted config")
    expected = {
        "architectures": ["Qwen3ForSequenceClassification"],
        "head_dtype": "model",
        "hidden_size": 2560,
        "num_labels": 1,
        "sbert_ce_default_activation_function": ("torch.nn.modules.activation.Tanh"),
    }
    for key, value in expected.items():
        if config.get(key) != value:
            raise ArtifactError(f"converted config {key} is incompatible")
    context = config.get("max_position_embeddings")
    if isinstance(context, bool) or not isinstance(context, int) or context < 32768:
        raise ArtifactError("converted config does not attest 32,768-token context")
    if "problem_type" in config:
        raise ArtifactError(
            "converted config must not override the scoring problem type"
        )
    _verify_index_and_tensors(root)
    _size, template_sha256 = _hash_regular(root / "querit-rerank.jinja")
    if template_sha256 != EXPECTED_TEMPLATE_SHA256:
        raise ArtifactError(
            "converted artifact score template is not the pinned template"
        )


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


def attest_source_snapshot(root: Path) -> str:
    """Require the converter input to match the immutable pinned snapshot ledger."""

    try:
        ledger, tree_sha256 = attest_trusted_snapshot(root)
    except TrustError as exc:
        raise ArtifactError(
            "conversion source does not match the pinned ledger"
        ) from exc
    if tuple(ledger) != SOURCE_LEDGER or tree_sha256 != SOURCE_TREE_SHA256:
        raise ArtifactError("conversion source ledger hash is not pinned")
    return tree_sha256


def write_manifest(
    root: Path, *, source_tree_sha256: str = SOURCE_TREE_SHA256
) -> dict[str, object]:
    if source_tree_sha256 != SOURCE_TREE_SHA256:
        raise ArtifactError("conversion source tree hash is not pinned")
    output_ledger = _inventory(root)
    manifest = {
        "output_ledger": output_ledger,
        "output_tree_sha256": _ledger_sha256(
            output_ledger, b"querit-vllm-output-tree-v1\0"
        ),
        "schema": MANIFEST_SCHEMA,
        "source_ledger": list(SOURCE_LEDGER),
        "source_ledger_sha256": SOURCE_LEDGER_SHA256,
        "source_revision": SOURCE_REVISION,
        "source_tree_sha256": source_tree_sha256,
        "total_size": {
            "delta": TOTAL_SIZE_DELTA,
            "output": OUTPUT_TOTAL_SIZE,
            "source": SOURCE_TOTAL_SIZE,
        },
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
        manifest = json.loads(
            raw, object_pairs_hook=_json_object_without_duplicate_keys
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactError("artifact manifest is unreadable") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "output_ledger",
        "output_tree_sha256",
        "schema",
        "source_ledger",
        "source_ledger_sha256",
        "source_revision",
        "source_tree_sha256",
        "total_size",
        "transform",
    }:
        raise ArtifactError("artifact manifest fields are not exact")
    output_ledger = _inventory(root)
    if (
        manifest["schema"] != MANIFEST_SCHEMA
        or manifest["source_revision"] != SOURCE_REVISION
        or manifest["transform"] != TRANSFORM
        or manifest["source_ledger"] != list(SOURCE_LEDGER)
        or manifest["source_ledger_sha256"] != SOURCE_LEDGER_SHA256
        or manifest["source_tree_sha256"] != SOURCE_TREE_SHA256
        or manifest["total_size"]
        != {
            "delta": TOTAL_SIZE_DELTA,
            "output": OUTPUT_TOTAL_SIZE,
            "source": SOURCE_TOTAL_SIZE,
        }
        or _json_bytes(manifest) != raw
        or manifest["output_ledger"] != output_ledger
        or manifest["output_tree_sha256"]
        != _ledger_sha256(output_ledger, b"querit-vllm-output-tree-v1\0")
    ):
        raise ArtifactError("artifact manifest does not match converted files")
    return manifest


def manifest_sha256(root: Path) -> str:
    verify_manifest(root)
    return _hash_regular(root / MANIFEST_NAME)[1]


__all__ = [
    "ArtifactError",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "OUTPUT_TOTAL_SIZE",
    "SOURCE_REVISION",
    "SOURCE_LEDGER_SHA256",
    "SOURCE_TOTAL_SIZE",
    "SOURCE_TREE_SHA256",
    "TOTAL_SIZE_DELTA",
    "TRANSFORM",
    "attest_source_snapshot",
    "manifest_sha256",
    "verify_manifest",
    "write_manifest",
]
