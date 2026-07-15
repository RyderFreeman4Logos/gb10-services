#!/usr/bin/env python3
"""Semantic artifact sealing, final re-attestation, and compact PASS receipts."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

import querit_replay_schema as schema
from querit_replay_sandbox import attest_network_isolation
from querit_replay_trust import final_reattest


def _receipt_for(
    manifest: Mapping[str, Any], manifest_hash: str, final_attestation: Mapping[str, Any]
) -> dict[str, Any]:
    artifacts = manifest["artifacts"]
    gates = {name: manifest["gates"][name] for name in schema.REQUIRED_PASS_GATES}
    final = dict(final_attestation)
    return {
        "artifact_set_sha256": schema.artifact_set_sha256(artifacts),
        "counts": {
            name: artifacts[name]["count"] for name in schema.DATA_ARTIFACT_NAMES
        },
        "final_attestation": final,
        "final_attestation_sha256": schema.sha256_bytes(
            schema.canonical_json_bytes(final),
            domain=b"querit-final-attestation-v1\0",
        ),
        "gates_sha256": schema.sha256_bytes(
            schema.canonical_json_bytes(gates), domain=b"querit-gates-v1\0"
        ),
        "identity_sha256": schema.identity_sha256(manifest["identity"]),
        "manifest_sha256": manifest_hash,
        "run_id_sha256": schema.sha256_bytes(
            manifest["run_id"].encode("utf-8"), domain=b"querit-run-id-v1\0"
        ),
        "schema": schema.RECEIPT_SCHEMA,
        "selected_candidate": schema.CANDIDATE_CONTRACT,
        "status": "PASS",
    }


def _remove_stale_receipt(root: Path) -> None:
    receipt_path = root / schema.RECEIPT_NAME
    if not receipt_path.exists() and not receipt_path.is_symlink():
        return
    info = receipt_path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != root.stat().st_uid
        or info.st_nlink != 1
    ):
        raise schema.SchemaError("refusing to remove unsafe stale receipt")
    receipt_path.unlink()
    directory_fd = os.open(
        root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def seal_artifact_set(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the four data artifacts into a semantic manifest, never a PASS receipt."""

    safe_root = schema._private_root(root)
    _remove_stale_receipt(safe_root)
    sealed = dict(manifest)
    sealed["artifacts"] = {
        name: schema._ledger_entry(safe_root, name)
        for name in schema.DATA_ARTIFACT_NAMES
    }
    schema._validate_manifest(sealed)
    schema.write_atomic_json(
        safe_root / schema.MANIFEST_NAME, sealed, root=safe_root
    )
    return sealed


def write_artifact_set(
    root: Path,
    *,
    cases: Iterable[Mapping[str, Any]],
    encodings: Iterable[Mapping[str, Any]],
    observations: Iterable[Mapping[str, Any]],
    errors: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Write private semantic evidence; final attestation is a separate authority."""

    safe_root = schema._private_root(root)
    schema.write_atomic_jsonl(safe_root / schema.CASES_NAME, cases, root=safe_root)
    schema.write_atomic_jsonl(
        safe_root / schema.ENCODINGS_NAME, encodings, root=safe_root
    )
    schema.write_atomic_jsonl(
        safe_root / schema.OBSERVATIONS_NAME, observations, root=safe_root
    )
    schema.write_atomic_jsonl(safe_root / schema.ERRORS_NAME, errors, root=safe_root)
    sealed = seal_artifact_set(safe_root, manifest)
    validate_artifact_set(safe_root, require_receipt=False)
    return sealed


def verify_pass_receipt(
    receipt_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_identity_sha256: str,
) -> dict[str, Any]:
    schema._require_sha256(expected_manifest_sha256, "expected_manifest_sha256")
    schema._require_sha256(expected_identity_sha256, "expected_identity_sha256")
    receipt = schema.safe_read_json(receipt_path, root=receipt_path.parent)
    expected_keys = {
        "artifact_set_sha256",
        "counts",
        "final_attestation",
        "final_attestation_sha256",
        "gates_sha256",
        "identity_sha256",
        "manifest_sha256",
        "run_id_sha256",
        "schema",
        "selected_candidate",
        "status",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        raise schema.SchemaError("receipt fields are not exact")
    if receipt["schema"] != schema.RECEIPT_SCHEMA or receipt["status"] != "PASS":
        raise schema.SchemaError("receipt is not a PASS receipt")
    if receipt["selected_candidate"] != schema.CANDIDATE_CONTRACT:
        raise schema.SchemaError("receipt is not bound to the candidate contract")
    if receipt["manifest_sha256"] != expected_manifest_sha256:
        raise schema.SchemaError("receipt is stale for the expected manifest")
    if receipt["identity_sha256"] != expected_identity_sha256:
        raise schema.SchemaError("receipt is stale for the expected runtime/source identity")
    for name in (
        "artifact_set_sha256",
        "final_attestation_sha256",
        "gates_sha256",
        "manifest_sha256",
        "identity_sha256",
        "run_id_sha256",
    ):
        schema._require_sha256(receipt[name], name)
    expected_counts = {
        schema.CASES_NAME: schema.EXPECTED_CASE_COUNT,
        schema.ENCODINGS_NAME: schema.EXPECTED_ENCODING_COUNT,
        schema.OBSERVATIONS_NAME: schema.EXPECTED_OBSERVATION_COUNT,
        schema.ERRORS_NAME: 0,
    }
    if receipt["counts"] != expected_counts:
        raise schema.SchemaError("receipt record counts are not exact")
    final = receipt["final_attestation"]
    final_keys = {
        "model_revision",
        "network_isolation_sha256",
        "runtime_sha256",
        "snapshot_tree_sha256",
        "source_tree_sha256",
        "status",
        "system_python",
        "trusted_model_ledger_sha256",
        "validation_sha256",
    }
    if not isinstance(final, dict) or set(final) != final_keys or final["status"] != "PASS":
        raise schema.SchemaError("receipt final attestation is incomplete")
    from querit_replay_trust import (
        MODEL_ID,
        PINNED_CONTAINER_IMAGE_DIGEST,
        PINNED_REVISION,
        TRUSTED_MODEL_LEDGER_SHA256,
    )

    if (
        final["model_revision"] != PINNED_REVISION
        or final["trusted_model_ledger_sha256"] != TRUSTED_MODEL_LEDGER_SHA256
    ):
        raise schema.SchemaError("receipt final model identity is not trusted")
    schema._validate_system_python(final["system_python"])
    final_identity = {
        "container_image_digest": PINNED_CONTAINER_IMAGE_DIGEST,
        "model_id": MODEL_ID,
        "model_revision": final["model_revision"],
        "runtime_sha256": final["runtime_sha256"],
        "snapshot_tree_sha256": final["snapshot_tree_sha256"],
        "source_tree_sha256": final["source_tree_sha256"],
        "system_python": final["system_python"],
        "trusted_model_ledger_sha256": final["trusted_model_ledger_sha256"],
    }
    if schema.identity_sha256(final_identity) != receipt["identity_sha256"]:
        raise schema.SchemaError("receipt final attestation differs from bound identity")
    for name in final_keys - {"model_revision", "status", "system_python"}:
        schema._require_sha256(final[name], f"final {name}")
    expected_final_hash = schema.sha256_bytes(
        schema.canonical_json_bytes(final), domain=b"querit-final-attestation-v1\0"
    )
    if receipt["final_attestation_sha256"] != expected_final_hash:
        raise schema.SchemaError("receipt final attestation hash mismatch")
    serialized = schema.canonical_json_bytes(receipt).decode("utf-8")
    if any(
        forbidden in serialized.lower()
        for forbidden in (
            '"query":',
            '"document":',
            '"prompt":',
            '"prompt_utf8',
            "authorization",
            "bearer ",
            "password",
            "/home/",
        )
    ) or re.search(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", serialized):
        raise schema.SchemaError("compact receipt contains private or host-specific content")
    return receipt


def validate_artifact_set(
    root: Path,
    *,
    expected_identity: Mapping[str, Any] | None = None,
    require_receipt: bool = True,
) -> dict[str, Any]:
    safe_root = schema._private_root(root)
    manifest = schema._validate_manifest(
        schema.safe_read_json(safe_root / schema.MANIFEST_NAME, root=safe_root)
    )
    if expected_identity is not None and manifest["identity"] != dict(expected_identity):
        raise schema.SchemaError(
            "manifest identity does not match the expected replay identity"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(
        schema.DATA_ARTIFACT_NAMES
    ):
        raise schema.SchemaError("manifest artifact ledger is incomplete")
    for name in schema.DATA_ARTIFACT_NAMES:
        actual = schema._ledger_entry(safe_root, name)
        if artifacts.get(name) != actual:
            raise schema.SchemaError(f"artifact ledger mismatch: {name}")
    cases = schema.safe_read_jsonl(safe_root / schema.CASES_NAME, root=safe_root)
    encodings = schema.safe_read_jsonl(
        safe_root / schema.ENCODINGS_NAME, root=safe_root
    )
    observations = schema.safe_read_jsonl(
        safe_root / schema.OBSERVATIONS_NAME, root=safe_root
    )
    errors = schema.safe_read_jsonl(safe_root / schema.ERRORS_NAME, root=safe_root)
    case_ids = schema._validate_case_records(cases)
    encoding_ids = schema._validate_encoding_records(encodings, case_ids)
    schema._validate_observation_records(observations, case_ids, encoding_ids)
    from querit_replay_validation import EvidenceError, validate_replay_evidence

    try:
        derived_gates, derived_errors = validate_replay_evidence(
            manifest, cases, encodings, observations
        )
    except EvidenceError as exc:
        raise schema.SchemaError(str(exc)) from exc
    if manifest["gates"] != derived_gates:
        raise schema.SchemaError("manifest gates were not derived from persisted evidence")
    if manifest["status"] == "FAIL" and errors != derived_errors:
        raise schema.SchemaError("failure records were not derived from persisted evidence")
    if len(errors) > schema.MAX_ERROR_COUNT or (
        manifest["status"] == "SEMANTIC_PASS" and errors
    ):
        raise schema.SchemaError("semantically passing artifact must have zero errors")
    expected_counts = {
        schema.CASES_NAME: schema.EXPECTED_CASE_COUNT,
        schema.ENCODINGS_NAME: schema.EXPECTED_ENCODING_COUNT,
        schema.OBSERVATIONS_NAME: schema.EXPECTED_OBSERVATION_COUNT,
        schema.ERRORS_NAME: len(errors),
    }
    if {
        name: artifacts[name]["count"] for name in schema.DATA_ARTIFACT_NAMES
    } != expected_counts:
        raise schema.SchemaError("artifact counts are not exact")
    if manifest["status"] == "SEMANTIC_PASS" and require_receipt:
        receipt = verify_pass_receipt(
            safe_root / schema.RECEIPT_NAME,
            expected_manifest_sha256=schema.file_sha256(
                safe_root / schema.MANIFEST_NAME, schema.MAX_JSON_BYTES
            ),
            expected_identity_sha256=schema.identity_sha256(manifest["identity"]),
        )
        if receipt["artifact_set_sha256"] != schema.artifact_set_sha256(artifacts):
            raise schema.SchemaError("receipt artifact-set hash mismatch")
        expected_run_id_hash = schema.sha256_bytes(
            manifest["run_id"].encode("utf-8"), domain=b"querit-run-id-v1\0"
        )
        if receipt["run_id_sha256"] != expected_run_id_hash:
            raise schema.SchemaError("receipt run ID hash mismatch")
        gates = {
            name: manifest["gates"][name] for name in schema.REQUIRED_PASS_GATES
        }
        expected_gate_hash = schema.sha256_bytes(
            schema.canonical_json_bytes(gates), domain=b"querit-gates-v1\0"
        )
        if receipt["gates_sha256"] != expected_gate_hash:
            raise schema.SchemaError("receipt gate hash mismatch")
        expected_validation_hash = schema.sha256_bytes(
            schema.canonical_json_bytes(
                {
                    "artifact_set_sha256": schema.artifact_set_sha256(artifacts),
                    "gates_sha256": expected_gate_hash,
                    "manifest_sha256": schema.file_sha256(
                        safe_root / schema.MANIFEST_NAME, schema.MAX_JSON_BYTES
                    ),
                    "semantic_validation": "PASS",
                }
            ),
            domain=b"querit-final-validation-v1\0",
        )
        if receipt["final_attestation"]["validation_sha256"] != expected_validation_hash:
            raise schema.SchemaError("receipt final validation hash mismatch")
        identity = manifest["identity"]
        final = receipt["final_attestation"]
        for name in ("runtime_sha256", "snapshot_tree_sha256", "source_tree_sha256"):
            if final[name] != identity[name]:
                raise schema.SchemaError("receipt final identity differs from manifest")
        if final["system_python"] != identity["system_python"]:
            raise schema.SchemaError("receipt final system Python differs from manifest")
    return manifest


def seal_pass_receipt(
    root: Path, *, snapshot_root: Path, source_root: Path, runtime: Any
) -> dict[str, Any]:
    """Create authority only after semantic validation and immediate final re-attestation."""

    safe_root = schema._private_root(root)
    manifest = validate_artifact_set(safe_root, require_receipt=False)
    if manifest["status"] != "SEMANTIC_PASS":
        raise schema.SchemaError("only semantically passing evidence may be finalized")
    manifest_hash = schema.file_sha256(
        safe_root / schema.MANIFEST_NAME, schema.MAX_JSON_BYTES
    )
    artifacts_hash = schema.artifact_set_sha256(manifest["artifacts"])
    gates = {name: manifest["gates"][name] for name in schema.REQUIRED_PASS_GATES}
    gates_hash = schema.sha256_bytes(
        schema.canonical_json_bytes(gates), domain=b"querit-gates-v1\0"
    )
    validation_sha256 = schema.sha256_bytes(
        schema.canonical_json_bytes(
            {
                "artifact_set_sha256": artifacts_hash,
                "gates_sha256": gates_hash,
                "manifest_sha256": manifest_hash,
                "semantic_validation": "PASS",
            }
        ),
        domain=b"querit-final-validation-v1\0",
    )
    try:
        network_hash = attest_network_isolation()
        from querit_replay_runtime import reattest_loaded_runtime

        reattest_loaded_runtime(runtime, manifest)
        final = final_reattest(
            snapshot_root=snapshot_root,
            source_root=source_root,
            manifest=manifest,
            network_isolation_sha256=network_hash,
            validation_sha256=validation_sha256,
        )
    except (OSError, RuntimeError) as exc:
        _remove_stale_receipt(safe_root)
        raise schema.SchemaError("final replay re-attestation failed") from exc
    receipt = _receipt_for(manifest, manifest_hash, final)
    try:
        schema.write_atomic_json(
            safe_root / schema.RECEIPT_NAME, receipt, root=safe_root
        )
        return verify_pass_receipt(
            safe_root / schema.RECEIPT_NAME,
            expected_manifest_sha256=manifest_hash,
            expected_identity_sha256=schema.identity_sha256(manifest["identity"]),
        )
    except (OSError, schema.SchemaError):
        _remove_stale_receipt(safe_root)
        raise


__all__ = [
    "seal_artifact_set",
    "seal_pass_receipt",
    "validate_artifact_set",
    "verify_pass_receipt",
    "write_artifact_set",
]
