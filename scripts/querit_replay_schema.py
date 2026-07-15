#!/usr/bin/env python3
"""Stdlib-only schema, hashing, bounded I/O, and receipt gates for Querit replay."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from querit_replay_trust import (
    MODEL_ID,
    PINNED_CONTAINER_IMAGE_DIGEST,
    PINNED_REVISION,
    SOURCE_NAMES,
    TRUSTED_MODEL_LEDGER_SHA256,
)


ARTIFACT_SCHEMA = "querit-dual-replay-v1"
RECEIPT_SCHEMA = "querit-dual-replay-pass-receipt-v1"
LEGACY_CONTRACT = "legacy-physical-last-v1"
CANDIDATE_CONTRACT = "current-prompt-terminal-cls-v1"
POSTPROCESSOR_TOKEN_ID = 151643
TERMINAL_ANCHOR_TOKEN_ID = 151665
MAX_TOKEN_COUNT = 40960
TRACK_DEFINITIONS = {
    LEGACY_CONTRACT: {"prompt_max": 40960, "selection": "physical-last", "terminal_id": POSTPROCESSOR_TOKEN_ID},
    CANDIDATE_CONTRACT: {"prompt_max": 40959, "selection": "attention-sum-minus-one", "terminal_id": TERMINAL_ANCHOR_TOKEN_ID},
}
EXPECTED_CASE_COUNT = 40
EXPECTED_ENCODING_COUNT = 80
EXPECTED_OBSERVATION_COUNT = 680
MAX_ERROR_COUNT = 256
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSONL_BYTES = 256 * 1024 * 1024
MAX_RECORDS = 1024

MANIFEST_NAME = "manifest.json"
CASES_NAME = "cases.jsonl"
ENCODINGS_NAME = "encodings.jsonl"
OBSERVATIONS_NAME = "observations.jsonl"
ERRORS_NAME = "errors.jsonl"
RECEIPT_NAME = "receipt.json"
DATA_ARTIFACT_NAMES = (CASES_NAME, ENCODINGS_NAME, OBSERVATIONS_NAME, ERRORS_NAME)
REQUIRED_PASS_GATES = (
    "artifact_integrity",
    "boundary_exact",
    "candidate_batch_invariance",
    "candidate_permutation_invariance",
    "candidate_repeat_invariance",
    "duplicate_equality",
    "head_attestation",
    "legacy_direct_parity",
    "native_formula",
    "rank_preservation",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SchemaError(ValueError):
    """A replay artifact is malformed, unsafe, unbounded, or not hash-bound."""

def _reject_constant(value: str) -> None:
    raise SchemaError(f"nonfinite JSON constant is forbidden: {value}")

def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate JSON key: {key}")
        result[key] = value
    return result

def _validate_json_value(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError("JSON floats must be finite")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SchemaError("JSON strings must contain Unicode scalar values") from exc
    elif isinstance(value, list):
        for item in value:
            _validate_json_value(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError("JSON object keys must be strings")
            _validate_json_value(key)
            _validate_json_value(item)
    elif value is not None and not isinstance(value, (bool, int)):
        raise SchemaError(f"unsupported JSON value type: {type(value).__name__}")

def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical UTF-8 JSON bytes without a trailing newline."""

    _validate_json_value(value)
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SchemaError("value cannot be represented as canonical JSON") from exc

def sha256_bytes(value: bytes, *, domain: bytes = b"") -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(value)
    return digest.hexdigest()

def identity_sha256(identity: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_json_bytes(dict(identity)), domain=b"querit-replay-identity-v1\0"
    )

def token_ids_sha256(token_ids: Sequence[int]) -> str:
    digest = hashlib.sha256(b"querit-token-ids-v1\0")
    digest.update(struct.pack("<Q", len(token_ids)))
    for token_id in token_ids:
        if isinstance(token_id, bool) or not isinstance(token_id, int) or not 0 <= token_id <= 0xFFFFFFFF:
            raise SchemaError("token IDs must be uint32 values")
        digest.update(struct.pack("<I", token_id))
    return digest.hexdigest()

def attention_mask_sha256(mask: Sequence[int]) -> str:
    digest = hashlib.sha256(b"querit-attention-mask-v1\0")
    digest.update(struct.pack("<Q", len(mask)))
    for value in mask:
        if value not in (0, 1) or isinstance(value, bool):
            raise SchemaError("attention masks must contain integer zero or one")
        digest.update(bytes((value,)))
    return digest.hexdigest()

def float32_cell(value: float) -> dict[str, Any]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SchemaError("float cells must be finite numbers")
    packed = struct.pack(">f", float(value))
    normalized = struct.unpack(">f", packed)[0]
    if not math.isfinite(normalized):
        raise SchemaError("float32 conversion overflowed")
    return {"f32_be": packed.hex(), "value": normalized}

def normalized_f32_tensor_sha256(values: Iterable[float], shape: Sequence[int]) -> str:
    dimensions = list(shape)
    if not dimensions or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in dimensions):
        raise SchemaError("tensor shape must contain positive integer dimensions")
    flat = list(values)
    expected = math.prod(dimensions)
    if len(flat) != expected:
        raise SchemaError("tensor value count does not match shape")
    digest = hashlib.sha256(b"querit-f32le-tensor-v1\0")
    shape_bytes = canonical_json_bytes({"dtype": "float32-le", "shape": dimensions})
    digest.update(struct.pack("<Q", len(shape_bytes)))
    digest.update(shape_bytes)
    for value in flat:
        if not math.isfinite(float(value)):
            raise SchemaError("tensor values must be finite")
        digest.update(struct.pack("<f", float(value)))
    return digest.hexdigest()

def _private_root(root: Path) -> Path:
    absolute = Path(os.path.abspath(root))
    try:
        info = absolute.lstat()
    except OSError as exc:
        raise SchemaError(f"artifact root is unavailable: {absolute}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SchemaError("artifact root must be a real directory")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SchemaError("artifact root must be owner-only")
    return absolute

def _direct_member(path: Path, root: Path) -> tuple[Path, Path]:
    safe_root = _private_root(root)
    absolute = Path(os.path.abspath(path))
    if absolute.parent != safe_root or absolute.name in ("", ".", ".."):
        raise SchemaError("artifact path must be a direct member of its output root")
    return absolute, safe_root

def _read_bounded(path: Path, *, root: Path, maximum_bytes: int) -> bytes:
    absolute, _ = _direct_member(path, root)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = absolute.lstat()
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise SchemaError(f"cannot open artifact without following links: {absolute.name}") from exc
    try:
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or info.st_uid != os.getuid()
            or before.st_uid != os.getuid()
            or info.st_nlink != 1
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != identity
        ):
            raise SchemaError("artifacts must be owner-owned, unlinked regular files")
        if info.st_size > maximum_bytes:
            raise SchemaError(f"artifact exceeds {maximum_bytes} byte limit")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum_bytes:
            raise SchemaError(f"artifact exceeds {maximum_bytes} byte limit")
        try:
            after = absolute.lstat()
        except OSError as exc:
            raise SchemaError("artifact disappeared during bounded read") from exc
        if (
            (after.st_dev, after.st_ino) != identity
            or after.st_size != len(data)
            or after.st_nlink != 1
            or stat.S_ISLNK(after.st_mode)
        ):
            raise SchemaError("artifact changed during bounded read")
        return data
    finally:
        os.close(descriptor)

def _parse_json(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError("artifact is not strict UTF-8 JSON") from exc
    _validate_json_value(value)
    return value

def safe_read_json(path: Path, *, root: Path, maximum_bytes: int = MAX_JSON_BYTES) -> Any:
    data = _read_bounded(path, root=root, maximum_bytes=maximum_bytes)
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise SchemaError("canonical JSON files require exactly one terminal LF")
    value = _parse_json(data[:-1])
    if data[:-1] != canonical_json_bytes(value):
        raise SchemaError("JSON file is not canonical")
    return value

def safe_read_jsonl(
    path: Path,
    *,
    root: Path,
    maximum_bytes: int = MAX_JSONL_BYTES,
    maximum_records: int = MAX_RECORDS,
) -> list[Any]:
    data = _read_bounded(path, root=root, maximum_bytes=maximum_bytes)
    if data and not data.endswith(b"\n"):
        raise SchemaError("JSONL files require a terminal LF")
    lines = data.splitlines()
    if len(lines) > maximum_records:
        raise SchemaError("JSONL record count exceeds limit")
    records: list[Any] = []
    for line in lines:
        if not line:
            raise SchemaError("blank JSONL records are forbidden")
        value = _parse_json(line)
        if line != canonical_json_bytes(value):
            raise SchemaError("JSONL record is not canonical")
        records.append(value)
    return records

def _atomic_write(path: Path, data: bytes, *, root: Path) -> None:
    absolute, safe_root = _direct_member(path, root)
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".querit-", dir=safe_root)
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_path, absolute)
        temporary_path = None
        directory_fd = os.open(safe_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise SchemaError(f"atomic artifact write failed: {absolute.name}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

def write_atomic_json(path: Path, value: Any, *, root: Path) -> None:
    data = canonical_json_bytes(value) + b"\n"
    if len(data) > MAX_JSON_BYTES:
        raise SchemaError("JSON output exceeds byte limit")
    _atomic_write(path, data, root=root)

def write_atomic_jsonl(path: Path, records: Iterable[Any], *, root: Path) -> None:
    rows = list(records)
    if len(rows) > MAX_RECORDS:
        raise SchemaError("JSONL record count exceeds limit")
    data = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    if len(data) > MAX_JSONL_BYTES:
        raise SchemaError("JSONL output exceeds byte limit")
    _atomic_write(path, data, root=root)

def file_sha256(path: Path, maximum_bytes: int = MAX_JSONL_BYTES) -> str:
    root = path.parent
    data = _read_bounded(path, root=root, maximum_bytes=maximum_bytes)
    return hashlib.sha256(data).hexdigest()

def _ledger_entry(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    records = safe_read_jsonl(path, root=root)
    data = _read_bounded(path, root=root, maximum_bytes=MAX_JSONL_BYTES)
    return {"count": len(records), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

def artifact_set_sha256(artifacts: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_json_bytes(dict(artifacts)), domain=b"querit-artifact-set-v1\0"
    )

def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SchemaError(f"{name} must be a lowercase SHA-256")
    return value

def _validate_system_python(value: Any) -> dict[str, Any]:
    required = {
        "authority",
        "chain_sha256",
        "device",
        "gid",
        "inode",
        "launcher_path",
        "mode",
        "nlink",
        "resolved_path",
        "sha256",
        "size",
        "uid",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise SchemaError("system Python attestation fields are not exact")
    if value["authority"] != "runtime-attested-local-system-python":
        raise SchemaError("system Python authority is not runtime-local attestation")
    if value["launcher_path"] != "/usr/bin/python3":
        raise SchemaError("system Python launcher is not the fixed canonical path")
    resolved = value["resolved_path"]
    if (
        not isinstance(resolved, str)
        or not re.fullmatch(
            r"(?:/usr/bin/python3(?:\.[0-9]+)*|/usr/local/bin/python3\.12)",
            resolved,
        )
        or os.path.normpath(resolved) != resolved
    ):
        raise SchemaError("resolved system Python path is outside the trusted root")
    _require_sha256(value["chain_sha256"], "system Python chain")
    _require_sha256(value["sha256"], "system Python executable")
    for name in ("device", "inode", "nlink", "size", "uid", "gid"):
        if isinstance(value[name], bool) or not isinstance(value[name], int):
            raise SchemaError(f"system Python {name} must be an integer")
    if (
        value["device"] < 0
        or value["inode"] <= 0
        or value["nlink"] != 1
        or not 0 < value["size"] <= 128 * 1024 * 1024
        or value["uid"] != 0
        or value["gid"] != 0
    ):
        raise SchemaError("system Python stat identity is unsafe")
    mode = value["mode"]
    if not isinstance(mode, str) or not re.fullmatch(r"0[0-7]{3}", mode):
        raise SchemaError("system Python mode is malformed")
    mode_value = int(mode, 8)
    if mode_value & 0o022 or not mode_value & 0o111:
        raise SchemaError("system Python mode is writable or non-executable")
    return value


def _validate_identity(identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise SchemaError("manifest identity must be an object")
    required = {
        "container_image_digest",
        "model_id",
        "model_revision",
        "runtime_sha256",
        "snapshot_tree_sha256",
        "source_tree_sha256",
        "system_python",
        "trusted_model_ledger_sha256",
    }
    if set(identity) != required:
        raise SchemaError("manifest identity fields are not exact")
    if identity["container_image_digest"] != PINNED_CONTAINER_IMAGE_DIGEST:
        raise SchemaError("container image is not the reviewed immutable digest")
    if identity["model_id"] != MODEL_ID:
        raise SchemaError("unexpected model identity")
    if identity["model_revision"] != PINNED_REVISION:
        raise SchemaError("unexpected model revision")
    if identity["trusted_model_ledger_sha256"] != TRUSTED_MODEL_LEDGER_SHA256:
        raise SchemaError("model identity is not bound to the reviewed trusted ledger")
    for name in ("runtime_sha256", "snapshot_tree_sha256", "source_tree_sha256"):
        _require_sha256(identity[name], name)
    _validate_system_python(identity["system_python"])
    return identity

def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema") != ARTIFACT_SCHEMA:
        raise SchemaError("unexpected replay manifest schema")
    if not isinstance(manifest.get("run_id"), str) or not _RUN_ID_RE.fullmatch(manifest["run_id"]):
        raise SchemaError("invalid replay run ID")
    if manifest.get("status") not in ("SEMANTIC_PASS", "FAIL"):
        raise SchemaError("manifest status must be SEMANTIC_PASS or FAIL")
    _validate_identity(manifest.get("identity"))
    if manifest.get("tracks") != [LEGACY_CONTRACT, CANDIDATE_CONTRACT]:
        raise SchemaError("manifest track order or identity is wrong")
    if manifest.get("track_definitions") != TRACK_DEFINITIONS:
        raise SchemaError("manifest track definitions are wrong")
    constants = manifest.get("constants")
    expected_constants = {
        "anchor_token_id": TERMINAL_ANCHOR_TOKEN_ID,
        "max_model_length": MAX_TOKEN_COUNT,
        "padding_side": "right",
        "postprocessor_token_id": POSTPROCESSOR_TOKEN_ID,
        "truncation_side": "right",
    }
    if constants != expected_constants:
        raise SchemaError("manifest tokenizer constants are wrong")
    _require_sha256(manifest.get("corpus_sha256"), "corpus_sha256")
    _require_sha256(manifest.get("corpus_definition_sha256"), "corpus_definition_sha256")
    _require_sha256(manifest.get("schedule_sha256"), "schedule_sha256")
    head = manifest.get("head")
    if not isinstance(head, dict) or head.get("weight_shape") != [2, 2560] or head.get("bias_shape") != [2]:
        raise SchemaError("head shape attestation is wrong")
    if head.get("normalized_dtype") != "float32-le" or not isinstance(head.get("loaded_dtype"), str):
        raise SchemaError("head dtype attestation is incomplete")
    _require_sha256(head.get("weight_sha256"), "head.weight")
    _require_sha256(head.get("bias_sha256"), "head.bias")
    runtime = manifest.get("runtime")
    runtime_fields = {
        "python", "pytorch", "transformers", "tokenizers", "cuda", "gpu", "sm",
        "tokenizer_class", "tokenizer_is_fast", "snapshot_file_count", "source_hashes",
        "system_python",
    }
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise SchemaError("runtime identity is incomplete")
    source_hashes = runtime.get("source_hashes")
    required_sources = set(SOURCE_NAMES)
    if not isinstance(source_hashes, dict) or set(source_hashes) != required_sources:
        raise SchemaError("source hash set is not exact")
    for name, digest in source_hashes.items():
        if not isinstance(name, str) or "/" in name:
            raise SchemaError("source hash name is unsafe")
        _require_sha256(digest, f"source hash {name}")
    if _validate_system_python(runtime.get("system_python")) != manifest["identity"]["system_python"]:
        raise SchemaError("runtime system Python differs from replay identity")
    source_ledger = manifest.get("source_ledger")
    if not isinstance(source_ledger, list) or [
        row.get("path") if isinstance(row, dict) else None for row in source_ledger
    ] != list(SOURCE_NAMES):
        raise SchemaError("source ledger order and names are not exact")
    for row in source_ledger:
        if (
            set(row) != {"path", "sha256", "size"}
            or source_hashes[row["path"]] != row["sha256"]
            or not isinstance(row["size"], int)
            or isinstance(row["size"], bool)
            or not 0 < row["size"] <= 4 * 1024 * 1024
        ):
            raise SchemaError("source ledger row is invalid")
    if sha256_bytes(
        canonical_json_bytes(source_ledger), domain=b"querit-source-tree-v1\0"
    ) != manifest["identity"]["source_tree_sha256"]:
        raise SchemaError("source ledger tree hash does not match identity")
    runtime_hash = sha256_bytes(
        canonical_json_bytes(runtime), domain=b"querit-runtime-identity-v1\0"
    )
    if runtime_hash != manifest["identity"]["runtime_sha256"]:
        raise SchemaError("runtime identity hash does not match manifest identity")
    ledger = manifest.get("snapshot_ledger")
    if not isinstance(ledger, list) or len(ledger) != runtime["snapshot_file_count"] or not ledger:
        raise SchemaError("snapshot ledger count is inconsistent")
    ledger_paths: set[str] = set()
    for row in ledger:
        if (
            not isinstance(row, dict) or set(row) != {"path", "sha256", "size"}
            or not isinstance(row["path"], str) or row["path"].startswith("/")
            or ".." in Path(row["path"]).parts or not isinstance(row["size"], int)
            or isinstance(row["size"], bool) or row["size"] < 0
        ):
            raise SchemaError("snapshot ledger row is invalid")
        if row["path"] in ledger_paths:
            raise SchemaError("snapshot ledger paths must be unique")
        ledger_paths.add(row["path"])
        _require_sha256(row["sha256"], "snapshot file")
    if sha256_bytes(canonical_json_bytes(ledger), domain=b"querit-snapshot-tree-v1\0") != manifest["identity"]["snapshot_tree_sha256"]:
        raise SchemaError("snapshot ledger tree hash does not match identity")
    if not isinstance(manifest.get("classifier_load_report"), dict):
        raise SchemaError("classifier load report is missing")
    tolerances = manifest.get("tolerances")
    floors = {"logits": (1e-5, 5e-2), "probabilities": (1e-6, 5e-3), "score": (1e-6, 1e-2)}
    if not isinstance(tolerances, dict) or set(tolerances) != set(floors):
        raise SchemaError("tolerance declaration is incomplete")
    for name, (floor, cap) in floors.items():
        row = tolerances[name]
        if not isinstance(row, dict) or row.get("floor") != floor or row.get("cap") != cap:
            raise SchemaError(f"{name} tolerance floor/cap changed")
        value = row.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not floor <= value <= cap:
            raise SchemaError(f"{name} calibrated tolerance is outside predeclared bounds")
    gates = manifest.get("gates")
    if not isinstance(gates, dict) or not set(REQUIRED_PASS_GATES).issubset(gates):
        raise SchemaError("required replay gates are missing")
    if manifest["status"] == "SEMANTIC_PASS" and any(
        gates[name] != "PASS" for name in REQUIRED_PASS_GATES
    ):
        raise SchemaError("semantically passing manifest contains a non-PASS required gate")
    return manifest

def _validate_case_records(records: list[Any]) -> set[str]:
    if len(records) != EXPECTED_CASE_COUNT:
        raise SchemaError("case record count must be exactly 40")
    groups: dict[str, int] = {}
    identifiers: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SchemaError("case records must be objects")
        identifier = record.get("case_id")
        group = record.get("group")
        query = record.get("query")
        document = record.get("document")
        if not isinstance(identifier, str) or identifier in identifiers:
            raise SchemaError("case IDs must be unique strings")
        if group not in ("W", "B", "ZH", "XL", "H", "L"):
            raise SchemaError("unknown corpus group")
        if not isinstance(query, str) or not isinstance(document, str):
            raise SchemaError("case query/document must be strings")
        _validate_json_value(query)
        _validate_json_value(document)
        if len(query) > 4096 or len(document) > 32768:
            raise SchemaError("case exceeds API character bounds")
        target = record.get("target_prepack_tokens")
        if group == "L" and target not in (40958, 40959, 40960, 40961):
            raise SchemaError("long case target is not exact")
        if group != "L" and target is not None:
            raise SchemaError("only long cases may declare target lengths")
        identifiers.add(identifier)
        groups[group] = groups.get(group, 0) + 1
    if groups != {"W": 4, "B": 8, "ZH": 8, "XL": 8, "H": 8, "L": 4}:
        raise SchemaError("corpus group counts changed")
    return identifiers

def _validate_encoding_records(
    records: list[Any], case_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if len(records) != EXPECTED_ENCODING_COUNT:
        raise SchemaError("encoding record count must be exactly 80")
    identifiers: dict[str, dict[str, Any]] = {}
    pairs: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SchemaError("encoding records must be objects")
        case_id, track = record.get("case_id"), record.get("track")
        if case_id not in case_ids or track not in (LEGACY_CONTRACT, CANDIDATE_CONTRACT):
            raise SchemaError("encoding references unknown case/track")
        pair = (case_id, track)
        if pair in pairs:
            raise SchemaError("duplicate case/track encoding")
        identifier = record.get("encoding_id")
        if identifier != f"{case_id}:{track}" or identifier in identifiers:
            raise SchemaError("encoding ID is not canonical and unique")
        ids, mask = record.get("input_ids"), record.get("attention_mask")
        if not isinstance(ids, list) or not isinstance(mask, list) or not ids:
            raise SchemaError("encoding token/mask arrays are required")
        if len(ids) > MAX_TOKEN_COUNT or len(mask) != len(ids):
            raise SchemaError("encoding token/mask length is invalid")
        if record.get("attention_length") != len(mask) or any(value != 1 for value in mask):
            raise SchemaError("stored encoding masks must be unpadded all-one masks")
        if token_ids_sha256(ids) != record.get("input_ids_sha256"):
            raise SchemaError("token ID hash mismatch")
        if attention_mask_sha256(mask) != record.get("attention_mask_sha256"):
            raise SchemaError("attention mask hash mismatch")
        pre_count = record.get("pre_truncation_token_count")
        if isinstance(pre_count, bool) or not isinstance(pre_count, int) or pre_count <= 0:
            raise SchemaError("pre-truncation token count is invalid")
        expected_length = min(pre_count, 40960) if track == LEGACY_CONTRACT else min(pre_count, 40959) + 1
        if len(ids) != expected_length:
            raise SchemaError("encoding length does not match truncation contract")
        expected_drop = max(0, pre_count - (40960 if track == LEGACY_CONTRACT else 40959))
        if record.get("dropped_right_count") != expected_drop:
            raise SchemaError("dropped-right count is inconsistent")
        terminal = POSTPROCESSOR_TOKEN_ID if track == LEGACY_CONTRACT else TERMINAL_ANCHOR_TOKEN_ID
        if ids[-1] != terminal or record.get("expected_terminal_id") != terminal:
            raise SchemaError("encoding terminal token is wrong")
        terminal_string = "<|im_end|>" if track == LEGACY_CONTRACT else "[CLS]"
        if record.get("expected_terminal_string") != terminal_string:
            raise SchemaError("encoding terminal string is wrong")
        if record.get("last_real_index") != len(ids) - 1 or record.get("last_real_id") != ids[-1]:
            raise SchemaError("last-real encoding fields are wrong")
        internal_ids = ids if track == LEGACY_CONTRACT else ids[:-1]
        if record.get("internal_anchor_count") != internal_ids.count(TERMINAL_ANCHOR_TOKEN_ID):
            raise SchemaError("internal anchor count is wrong")
        _require_sha256(record.get("prompt_utf8_sha256"), "prompt_utf8_sha256")
        if not isinstance(record.get("prompt_utf8_length"), int) or record["prompt_utf8_length"] < 0:
            raise SchemaError("prompt UTF-8 length is invalid")
        prefix_hash = record.get("candidate_reference_prefix_sha256")
        if track == CANDIDATE_CONTRACT:
            if prefix_hash != token_ids_sha256(ids[:-1]):
                raise SchemaError("candidate prefix hash mismatch")
        elif prefix_hash is not None:
            raise SchemaError("legacy encoding cannot have candidate prefix hash")
        identifiers[identifier] = record
        pairs.add(pair)
    if len(pairs) != EXPECTED_ENCODING_COUNT:
        raise SchemaError("case/track encoding matrix is incomplete")
    return identifiers

def _validate_float_cell(value: Any, name: str) -> float:
    if not isinstance(value, dict) or set(value) != {"f32_be", "value"}:
        raise SchemaError(f"{name} is not an exact float32 cell")
    bits, number = value["f32_be"], value["value"]
    if not isinstance(bits, str) or not re.fullmatch(r"[0-9a-f]{8}", bits):
        raise SchemaError(f"{name} float bits are invalid")
    if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(float(number)):
        raise SchemaError(f"{name} value must be finite")
    expected = struct.pack(">f", float(number)).hex()
    if bits != expected:
        raise SchemaError(f"{name} bits do not match its JSON value")
    unpacked = struct.unpack(">f", bytes.fromhex(bits))[0]
    if not math.isfinite(unpacked):
        raise SchemaError(f"{name} authoritative bits are nonfinite")
    return unpacked

def _validate_observation_records(
    records: list[Any],
    case_ids: set[str],
    encoding_ids: Mapping[str, dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_OBSERVATION_COUNT:
        raise SchemaError("observation record count must be exactly 680")
    identifiers: set[str] = set()
    batches: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise SchemaError("observation records must be objects")
        identifier = record.get("observation_id")
        if not isinstance(identifier, str) or identifier in identifiers:
            raise SchemaError("observation IDs must be unique strings")
        track, case_id = record.get("track"), record.get("case_id")
        if track not in (LEGACY_CONTRACT, CANDIDATE_CONTRACT) or case_id not in case_ids:
            raise SchemaError("observation case/track reference is invalid")
        encoding_id = record.get("encoding_id")
        if encoding_id not in encoding_ids or encoding_id != f"{case_id}:{track}":
            raise SchemaError("observation encoding reference is invalid")
        encoding = encoding_ids[encoding_id]
        logits, probabilities = record.get("logits"), record.get("probabilities")
        if not isinstance(logits, list) or len(logits) != 2 or not isinstance(probabilities, list) or len(probabilities) != 2:
            raise SchemaError("observation requires two logits and probabilities")
        for index, item in enumerate(logits):
            _validate_float_cell(item, f"logit[{index}]")
        probability_values = [
            _validate_float_cell(item, f"probability[{index}]")
            for index, item in enumerate(probabilities)
        ]
        if any(not 0.0 <= value <= 1.0 for value in probability_values):
            raise SchemaError("probability is outside [0,1]")
        native = _validate_float_cell(record.get("native_score"), "native_score")
        recomputed = _validate_float_cell(record.get("recomputed_score"), "recomputed_score")
        if not -1.0 <= native <= 1.0 or not -1.0 <= recomputed <= 1.0 or record.get("finite_range_ok") is not True:
            raise SchemaError("observation finite/range status is wrong")
        width = record.get("batch_width")
        input_length = len(encoding["input_ids"])
        if not isinstance(width, int) or not input_length <= width <= MAX_TOKEN_COUNT:
            raise SchemaError("batch width is invalid")
        if record.get("padded_token_count") != width - input_length:
            raise SchemaError("observation padding count is inconsistent")
        physical_id = encoding["input_ids"][-1] if input_length == width else POSTPROCESSOR_TOKEN_ID
        if record.get("physical_last_index") != width - 1 or record.get("physical_last_id") != physical_id:
            raise SchemaError("observation physical-last fields are inconsistent")
        if track == LEGACY_CONTRACT:
            selected_index, selected_id = width - 1, physical_id
        else:
            selected_index, selected_id = encoding["last_real_index"], encoding["last_real_id"]
        if record.get("selected_index") != selected_index or record.get("selected_id") != selected_id:
            raise SchemaError("observation selected position/token violates track")
        if record.get("original_index") != record.get("row_position"):
            raise SchemaError("observation original index is inconsistent")
        batch_id = record.get("batch_id")
        if not isinstance(batch_id, str) or record.get("schedule_id") != batch_id:
            raise SchemaError("observation batch/schedule ID is inconsistent")
        batches.setdefault(batch_id, []).append(record)
        identifiers.add(identifier)
    for batch_id, rows in batches.items():
        batch_size = rows[0].get("batch_size")
        if not isinstance(batch_size, int) or not 1 <= batch_size <= 8:
            raise SchemaError("observation batch size is invalid")
        if len(rows) != batch_size or sorted(row.get("row_position") for row in rows) != list(range(batch_size)):
            raise SchemaError("observation batch rows are incomplete")
        if any(
            row.get("batch_size") != batch_size
            or row.get("batch_width") != rows[0].get("batch_width")
            or row.get("track") != rows[0].get("track")
            or row.get("schedule_id") != batch_id
            for row in rows
        ):
            raise SchemaError("observation batch envelope is inconsistent")
        ordered = sorted(rows, key=lambda row: row["row_position"])
        sorted_rows = sorted(
            range(batch_size),
            key=lambda index: (-float(ordered[index]["native_score"]["value"]), index),
        )
        ranks = {row_index: rank for rank, row_index in enumerate(sorted_rows)}
        if any(
            row.get("stable_rank") != ranks[row["row_position"]]
            or row.get("sorted_index") != ranks[row["row_position"]]
            for row in rows
        ):
            raise SchemaError("observation stable ranking is inconsistent")


from querit_replay_receipt import (  # noqa: E402, F401  (intentional public re-exports)
    seal_artifact_set,
    seal_pass_receipt,
    validate_artifact_set,
    verify_pass_receipt,
    write_artifact_set,
)


__all__ = [name for name in globals() if not name.startswith("_")]
