"""Bounded DeepInfra transport for cloud reranker baseline collection."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from reranker_cloud_evidence import canonical_json

MAX_CLOUD_RESPONSE_BYTES = 4 * 1024 * 1024


def _reject_echoed_key(raw: bytes, api_key: str) -> None:
    """Reject an upstream response that contains the configured bearer key."""

    if api_key and api_key.encode("utf-8") in raw:
        raise RuntimeError("cloud response contains the configured bearer key")


def call_deepinfra(
    model: str,
    request_body: dict[str, Any],
    api_key: str,
    timeout: float,
    endpoint: str = "https://api.deepinfra.com/v1/inference",
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    url = f"{endpoint}/{model}"
    payload = canonical_json(request_body)
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_CLOUD_RESPONSE_BYTES + 1)
            if len(raw) > MAX_CLOUD_RESPONSE_BYTES:
                raise RuntimeError("cloud response exceeded bounded read limit")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_CLOUD_RESPONSE_BYTES + 1)
        if len(raw) > MAX_CLOUD_RESPONSE_BYTES:
            raise RuntimeError("cloud error response exceeded bounded read limit") from exc
        status = exc.code
    elapsed = time.monotonic() - started
    _reject_echoed_key(raw, api_key)
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        body = {"_raw": raw.decode("utf-8", errors="replace")}
    timing = {
        "http_status": status,
        "elapsed_seconds": round(elapsed, 4),
        "response_bytes": len(raw),
    }
    return status, body, timing
