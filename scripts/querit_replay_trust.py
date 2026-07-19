#!/usr/bin/env python3
"""Immutable Querit model/source trust roots and bounded final re-attestation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

MODEL_ID = "Querit/Querit-4B"
PINNED_REVISION = "7b796de30ad8dc772d6c46c75659c1341283a665"
PINNED_CONTAINER_IMAGE_DIGEST = (
    "sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d"
)
HF_COMMIT_URL = (
    "https://huggingface.co/Querit/Querit-4B/commit/"
    "7b796de30ad8dc772d6c46c75659c1341283a665"
)
HF_BLOBS_API_URL = (
    "https://huggingface.co/api/models/Querit/Querit-4B/revision/"
    "7b796de30ad8dc772d6c46c75659c1341283a665?blobs=true"
)

# Non-LFS SHA-256 values were calculated over pinned resolve bytes. LFS values
# are the publisher's primary sha256 OIDs at the pinned commit; weights were not
# downloaded to establish this ledger.
TRUSTED_MODEL_FILES: tuple[dict[str, Any], ...] = (
    {"path": ".gitattributes", "size": 1631, "sha256": "5595ada225d01a462d3b942d82dc8c93fbd376296d0eeb43e6abbd98545eeac3"},
    {"path": "MTEB-multilingual-v2.png", "size": 247981, "sha256": "4008b0c002147014273d75234ccb7a521ca94f9356fedc3b1a2463e4416405eb"},
    {"path": "README.md", "size": 2660, "sha256": "4ec6c3d078ad225397e9e15896272d7f7fa1574e92081d0bddb85c0a091b1570"},
    {"path": "chat_template.jinja", "size": 2427, "sha256": "44d5f08f3f72b837eaad09f13a54c1f9f4eb58d75240334548b7fd52a5437fa5"},
    {"path": "config.json", "size": 1532, "sha256": "4fd7167e58d6adbf806ddd06894e65efdee4d3dfa7532bd897f0ca68ac84fb4c"},
    {"path": "generation_config.json", "size": 121, "sha256": "bb52bfdd308deaea4ec800bf0165e75770b0a4e5c105963bee1b0398f4043d3e"},
    {"path": "model-00001-of-00002.safetensors", "size": 4965832928, "sha256": "5b2b13727c7138ba8b75e87a9c38f321f1fab710633f19bee2b48cece6d06bbf"},
    {"path": "model-00002-of-00002.safetensors", "size": 3077776988, "sha256": "79aa6357725b61757d902afac3ff52e79b9193b2f5db8dd7a9ce3ba312469694"},
    {"path": "model.safetensors.index.json", "size": 32958, "sha256": "a2add79bc7d4ed33f397302c63f79b3b4a51d4603261de7debf292f7d0cd7b52"},
    {"path": "modeling_querit_4b.py", "size": 3770, "sha256": "3a6b98dab1aa5505c1e17508618e67e3f628cd1e33d123f2802f8f5647f37e3f"},
    {"path": "special_tokens_map.json", "size": 468, "sha256": "97d72d503040b49a5455c3b3b8662bca887d7b23b7c5ff88193656dea794e132"},
    {"path": "tokenizer.json", "size": 11423129, "sha256": "3d5994f990fc4d9d85f0683ce8c79984ef43cf18e4d5047d9ac2ee72210222b9"},
    {"path": "tokenizer_config.json", "size": 4587, "sha256": "fcd3ad3c12549e7b9bba58f94c5312c3e80b252592e6fbddf52d34bbed4ede0c"},
)

SOURCE_NAMES = (
    "querit_offline_replay.py",
    "querit_openai_rerank_server.py",
    "querit_replay_plan.py",
    "querit_replay_receipt.py",
    "querit_replay_runtime.py",
    "querit_replay_sandbox.py",
    "querit_replay_schema.py",
    "querit_replay_trust.py",
    "querit_replay_validation.py",
    "querit_score_contract.py",
)


class TrustError(RuntimeError):
    """A trusted input is missing, replaced, linked, oversized, or byte-different."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _domain_hash(value: Any, domain: bytes) -> str:
    digest = hashlib.sha256(domain)
    digest.update(_canonical(value))
    return digest.hexdigest()


TRUSTED_MODEL_LEDGER_SHA256 = _domain_hash(
    list(TRUSTED_MODEL_FILES), b"querit-trusted-model-ledger-v1\0"
)


def _real_owned_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        info = absolute.lstat()
    except OSError as exc:
        raise TrustError("trusted input directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise TrustError("trusted input root must be a real owner-owned directory")
    return absolute


def _hash_file(path: Path, maximum_bytes: int) -> tuple[int, str, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TrustError(f"cannot open trusted file without following links: {path.name}") from exc
    try:
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
            or info.st_uid != os.getuid()
            or before.st_nlink != 1
            or info.st_nlink != 1
            or (before.st_dev, before.st_ino) != identity
            or info.st_size > maximum_bytes
        ):
            raise TrustError("trusted file is non-regular, linked, replaced, or oversized")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise TrustError("trusted file grew beyond its read bound")
            digest.update(chunk)
        try:
            after = path.lstat()
        except OSError as exc:
            raise TrustError("trusted file disappeared during attestation") from exc
        if (
            (after.st_dev, after.st_ino) != identity
            or after.st_size != total
            or after.st_nlink != 1
            or stat.S_ISLNK(after.st_mode)
        ):
            raise TrustError("trusted file changed during attestation")
        return total, digest.hexdigest(), identity
    finally:
        os.close(descriptor)


def attest_trusted_snapshot(root: Path) -> tuple[list[dict[str, Any]], str]:
    """Re-hash the exact pinned snapshot, rejecting extras, links, and inode aliases."""

    snapshot = _real_owned_directory(root)
    expected = {row["path"]: row for row in TRUSTED_MODEL_FILES}
    try:
        entries = list(os.scandir(snapshot))
    except OSError as exc:
        raise TrustError("cannot enumerate trusted snapshot") from exc
    actual_names = {entry.name for entry in entries}
    if len(actual_names) != len(entries) or actual_names != set(expected):
        raise TrustError("snapshot file set differs from the immutable trusted ledger")
    ledger: list[dict[str, Any]] = []
    identities: set[tuple[int, int]] = set()
    for name in sorted(expected):
        wanted = expected[name]
        size, digest, identity = _hash_file(snapshot / name, int(wanted["size"]))
        if identity in identities:
            raise TrustError("snapshot contains duplicate hard-linked file identities")
        identities.add(identity)
        row = {"path": name, "sha256": digest, "size": size}
        if row != wanted:
            raise TrustError(f"trusted snapshot byte identity mismatch: {name}")
        ledger.append(row)
    tree_hash = _domain_hash(ledger, b"querit-snapshot-tree-v1\0")
    return ledger, tree_hash


def attest_source_tree(directory: Path) -> tuple[list[dict[str, Any]], str, dict[str, str]]:
    """Re-hash every replay/runtime source file through nofollow single-link reads."""

    root = _real_owned_directory(directory)
    ledger: list[dict[str, Any]] = []
    identities: set[tuple[int, int]] = set()
    for name in SOURCE_NAMES:
        size, digest, identity = _hash_file(root / name, 4 * 1024 * 1024)
        if identity in identities:
            raise TrustError("source ledger contains duplicate hard-linked file identities")
        identities.add(identity)
        ledger.append({"path": name, "sha256": digest, "size": size})
    return (
        ledger,
        _domain_hash(ledger, b"querit-source-tree-v1\0"),
        {row["path"]: row["sha256"] for row in ledger},
    )


def final_reattest(
    *,
    snapshot_root: Path,
    source_root: Path,
    manifest: Mapping[str, Any],
    network_isolation_sha256: str,
    validation_sha256: str,
) -> dict[str, Any]:
    """Re-read every trusted input and prove equality with the validated manifest."""

    ledger, snapshot_hash = attest_trusted_snapshot(snapshot_root)
    source_ledger, source_hash, source_hashes = attest_source_tree(source_root)
    identity = manifest["identity"]
    runtime = manifest["runtime"]
    runtime_hash = _domain_hash(runtime, b"querit-runtime-identity-v1\0")
    from querit_replay_sandbox import attest_running_system_python

    system_python = attest_running_system_python()
    if (
        system_python != identity["system_python"]
        or system_python != runtime["system_python"]
    ):
        raise TrustError("final system Python re-attestation differs from validated identity")
    if ledger != manifest["snapshot_ledger"] or snapshot_hash != identity["snapshot_tree_sha256"]:
        raise TrustError("final snapshot re-attestation differs from validated manifest")
    if (
        source_ledger != manifest["source_ledger"]
        or source_hashes != runtime["source_hashes"]
        or source_hash != identity["source_tree_sha256"]
    ):
        raise TrustError("final source re-attestation differs from validated manifest")
    if runtime_hash != identity["runtime_sha256"]:
        raise TrustError("final runtime re-attestation differs from validated manifest")
    if identity["trusted_model_ledger_sha256"] != TRUSTED_MODEL_LEDGER_SHA256:
        raise TrustError("manifest does not use the immutable trusted model ledger")
    return {
        "model_revision": PINNED_REVISION,
        "network_isolation_sha256": network_isolation_sha256,
        "runtime_sha256": runtime_hash,
        "snapshot_tree_sha256": snapshot_hash,
        "source_tree_sha256": source_hash,
        "status": "PASS",
        "system_python": system_python,
        "trusted_model_ledger_sha256": TRUSTED_MODEL_LEDGER_SHA256,
        "validation_sha256": validation_sha256,
    }


__all__ = [
    "HF_BLOBS_API_URL",
    "HF_COMMIT_URL",
    "MODEL_ID",
    "PINNED_CONTAINER_IMAGE_DIGEST",
    "PINNED_REVISION",
    "SOURCE_NAMES",
    "TRUSTED_MODEL_FILES",
    "TRUSTED_MODEL_LEDGER_SHA256",
    "TrustError",
    "attest_source_tree",
    "attest_trusted_snapshot",
    "final_reattest",
]
