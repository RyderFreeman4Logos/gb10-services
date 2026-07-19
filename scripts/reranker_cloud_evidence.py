#!/usr/bin/env python3
"""Versioned identity primitives for reranker cloud evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

LEGACY_BASELINE_SCHEMA = "reranker-cloud-baseline-v1"
CURRENT_BASELINE_SCHEMA = "reranker-cloud-baseline-v2"
SUPPORTED_BASELINE_SCHEMAS = frozenset(
    {LEGACY_BASELINE_SCHEMA, CURRENT_BASELINE_SCHEMA}
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def decode_json_strict(payload: bytes) -> Any:
    """Decode one UTF-8 JSON value while rejecting duplicates and extensions."""

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-standard JSON constant is forbidden: {value}")

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON object key is forbidden: {key}")
            result[key] = value
        return result

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def assert_credential_absent(value: Any, credential: str) -> None:
    """Reject a credential in every nested JSON key or string value."""

    if not credential:
        return
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if credential in current:
                raise ValueError("decoded JSON contains the configured credential")
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)

    if credential.encode("utf-8") in canonical_json(value):
        raise ValueError("canonical JSON contains the configured credential")


def request_fingerprint(
    group: dict[str, Any],
    request_body: dict[str, Any],
    *,
    provider: str = "deepinfra",
    model: str = "Qwen/Qwen3-Reranker-8B",
    baseline_schema: str = CURRENT_BASELINE_SCHEMA,
) -> str:
    """Hash the exact request identity according to its baseline schema."""

    if baseline_schema == LEGACY_BASELINE_SCHEMA:
        identity = {
            "query_id": group.get("query_id"),
            "source_language": group.get("source_language"),
            "request": request_body,
        }
    elif baseline_schema == CURRENT_BASELINE_SCHEMA:
        identity = {
            "schema": "reranker-cloud-request-v1",
            "provider": provider,
            "model": model,
            "query_id": group.get("query_id"),
            "source_language": group.get("source_language"),
            "request": request_body,
        }
    else:
        raise ValueError("unsupported cloud baseline schema")
    return hashlib.sha256(canonical_json(identity)).hexdigest()
