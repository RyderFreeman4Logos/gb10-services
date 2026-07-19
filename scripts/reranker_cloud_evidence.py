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
