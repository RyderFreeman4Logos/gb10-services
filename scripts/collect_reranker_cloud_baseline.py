#!/usr/bin/env python3
"""Collect a reproducible cloud-baseline reranker equivalence dataset.

This script sends the fixed MIRACLReranking (en+zh) corpus to a cloud reranker
API (DeepInfra Qwen3-Reranker-8B by default) using its native inference contract,
stores each raw response alongside the request fingerprint, and tracks token
budget so the run is reproducible and auditable.

The output is a companion baseline file under ``data/reranker-equivalence/`` so
that a locally-deployed vLLM Querit-4B (behind llm-guard-proxy) can later replay
the identical requests and the two response sets can be compared deterministically.

Usage::

    DEEPINFRA_KEY=... python3 scripts/collect_reranker_cloud_baseline.py \\
        --provider deepinfra \\
        --model Qwen/Qwen3-Reranker-8B \\
        --corpus data/reranker-equivalence/miracl-reranking-en-zh-dev.jsonl \\
        --output data/reranker-equivalence/cloud-baseline-deepinfra-qwen3-reranker-8b.jsonl \\
        --budget-usd 3.00

The script never prints credentials and writes only privacy-safe metadata
(group id, language, token counts, scores, timings, request fingerprints).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_PRICE_PER_MTOK = 0.05
DEFAULT_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = REPO_ROOT / "data" / "reranker-equivalence" / "miracl-reranking-en-zh-dev.jsonl"


def load_corpus(path: Path) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("corpus line is not a JSON object")
            if "query" not in record or "candidates" not in record:
                raise ValueError("corpus record missing query/candidates")
            groups.append(record)
    return groups


def build_request(group: dict[str, Any], instruction: str) -> dict[str, Any]:
    query = group["query"]
    candidates = group["candidates"]
    documents = [c["document"] for c in candidates]
    return {
        "queries": [query] * len(documents),
        "documents": documents,
        "instruction": instruction,
    }


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def request_fingerprint(group: dict[str, Any], request_body: dict[str, Any]) -> str:
    identity = {
        "query_id": group.get("query_id"),
        "source_language": group.get("source_language"),
        "request": request_body,
    }
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def estimate_tokens(request_body: dict[str, Any]) -> int:
    # Conservative whitespace-split token estimate for budget guards only.
    total_chars = sum(len(q) + len(d) for q, d in zip(request_body["queries"], request_body["documents"]))
    return max(1, math.ceil(total_chars / 3.5))


def call_deepinfra(
    model: str,
    request_body: dict[str, Any],
    api_key: str,
    timeout: float,
    endpoint: str = "https://api.deepinfra.com/v1/inference",
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    url = f"{endpoint}/{model}"
    payload = canonical_json(request_body)
    req = urllib.request.Request(
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    elapsed = time.monotonic() - started
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", default="deepinfra", choices=["deepinfra"])
    parser.add_argument("--model", default="Qwen/Qwen3-Reranker-8B")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    parser.add_argument("--budget-usd", type=float, default=3.00)
    parser.add_argument("--price-per-mtok", type=float, default=DEFAULT_PRICE_PER_MTOK)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--rate-delay-seconds", type=float, default=0.5)
    parser.add_argument("--max-groups", type=int, default=0, help="0 = all groups")
    parser.add_argument("--resume", action="store_true", help="skip groups already in output")
    parser.add_argument("--dry-run", action="store_true", help="build requests, estimate cost, do not call API")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPINFRA_KEY", "")
    if not args.dry_run and not api_key:
        print("ERROR: DEEPINFRA_KEY is not set", file=sys.stderr)
        return 2

    groups = load_corpus(args.corpus)
    if args.max_groups > 0:
        groups = groups[: args.max_groups]

    existing: set[str] = set()
    if args.resume and args.output.exists():
        with args.output.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.add(json.loads(line)["request_fingerprint"])
                except Exception:
                    pass

    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume else "w"
    out_handle = args.output.open(mode, encoding="utf-8")

    total_input_tokens = 0
    total_cost = 0.0
    completed = 0
    skipped = 0
    failed = 0

    for index, group in enumerate(groups, 1):
        request_body = build_request(group, args.instruction)
        fingerprint = request_fingerprint(group, request_body)
        if fingerprint in existing:
            skipped += 1
            continue

        est_tokens = estimate_tokens(request_body)
        est_cost = est_tokens / 1_000_000 * args.price_per_mtok
        if total_cost + est_cost > args.budget_usd:
            print(
                f"WARNING: budget ceiling reached after {completed} groups "
                f"(${total_cost:.6f} / ${args.budget_usd:.2f}); stopping.",
                file=sys.stderr,
            )
            break

        if args.dry_run:
            total_input_tokens += est_tokens
            total_cost += est_cost
            completed += 1
            continue

        try:
            status, body, timing = call_deepinfra(
                args.model, request_body, api_key, args.timeout
            )
        except Exception as exc:
            failed += 1
            print(f"ERROR group {index} {group.get('query_id')}: {exc}", file=sys.stderr)
            continue

        reported_tokens = 0
        if isinstance(body, dict):
            reported_tokens = int(body.get("input_tokens", 0) or 0)
        charged_tokens = reported_tokens or est_tokens
        total_input_tokens += charged_tokens
        total_cost += charged_tokens / 1_000_000 * args.price_per_mtok

        record = {
            "schema": "reranker-cloud-baseline-v1",
            "provider": args.provider,
            "model": args.model,
            "query_id": group.get("query_id"),
            "source_language": group.get("source_language"),
            "request_fingerprint": fingerprint,
            "request_instruction": args.instruction,
            "pair_count": len(request_body["queries"]),
            "candidate_document_ids": [c.get("document_id") for c in group["candidates"]],
            "candidate_relevance": [c.get("relevance") for c in group["candidates"]],
            "response": body,
            "timing": timing,
            "charged_input_tokens": charged_tokens,
            "estimated_input_tokens": est_tokens,
            "cumulative_cost_usd": round(total_cost, 6),
        }
        out_handle.write(canonical_json(record).decode("utf-8") + "\n")
        out_handle.flush()
        completed += 1

        if status != 200:
            failed += 1
            print(
                f"WARN group {index} {group.get('query_id')} http={status} "
                f"cost=${total_cost:.6f}",
                file=sys.stderr,
            )

        if args.rate_delay_seconds > 0:
            time.sleep(args.rate_delay_seconds)

        if index % 10 == 0:
            print(
                f"progress {index}/{len(groups)} completed={completed} "
                f"skipped={skipped} failed={failed} cost=${total_cost:.6f} "
                f"tokens={total_input_tokens}",
                file=sys.stderr,
            )

    out_handle.close()
    print(
        json.dumps(
            {
                "status": "DONE" if not args.dry_run else "DRY_RUN",
                "provider": args.provider,
                "model": args.model,
                "groups_total": len(groups),
                "groups_completed": completed,
                "groups_skipped": skipped,
                "groups_failed": failed,
                "total_input_tokens": total_input_tokens,
                "total_cost_usd": round(total_cost, 6),
                "budget_usd": args.budget_usd,
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
