#!/usr/bin/env python3
"""Replay a cloud-baseline reranker corpus against a local endpoint and compare.

Reads a baseline produced by ``collect_reranker_cloud_baseline.py`` and replays
each request against a local reranker endpoint (vLLM Querit-4B ``/v1/score`` by
default, optionally through the llm-guard-proxy DeepInfra-native adapter). The
local scalar scores are converted to the same ``[0, 1]`` probability domain used
by the cloud baseline, then the two response sets are compared deterministically
using rank correlation, top-k agreement, and nDCG@10 against the corpus qrels.

The output is a privacy-safe comparison receipt suitable for committing under
``data/reranker-equivalence/`` so the community can audit the equivalence claim.

Usage::

    python3 scripts/replay_reranker_local_vs_cloud.py \\
        --baseline data/reranker-equivalence/cloud-baseline-deepinfra-qwen3-reranker-8b.jsonl \\
        --corpus data/reranker-equivalence/miracl-reranking-en-zh-dev.jsonl \\
        --local-url http://gb10:18016 \\
        --local-path /v1/score \\
        --local-model Querit/Querit-4B \\
        --output data/reranker-equivalence/local-vs-cloud-receipt.json

The ``--local-contract`` flag selects how the local response is normalized:

* ``vllm-score`` (default): vLLM native ``/v1/score`` returning ``data[].score``
  in ``[-1, 1]`` (scalar Tanh head), mapped to ``[0, 1]`` by ``(s + 1) / 2``.
* ``deepinfra``: llm-guard-proxy DeepInfra-native adapter returning
  ``scores[]`` already in ``[0, 1]``.
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

REPO_ROOT = Path(__file__).resolve().parents[1]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_corpus_index(path: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for group in load_jsonl(path):
        qid = group.get("query_id")
        if qid:
            index[qid] = group
    return index


def normalize_vllm_score(body: dict[str, Any], expected: int) -> tuple[float, ...]:
    data = body.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise ValueError("vLLM score response cardinality mismatch")
    restored: list[float | None] = [None] * expected
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("vLLM score row is not an object")
        idx = row.get("index")  # type: ignore[assignment]
        score = row.get("score")
        if not isinstance(idx, int) or not 0 <= idx < expected or restored[idx] is not None:
            raise ValueError("vLLM score index is invalid")
        if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            raise ValueError("vLLM score is non-finite")
        s = float(score)
        if not -1.0 <= s <= 1.0:
            raise ValueError("vLLM score outside [-1, 1]")
        restored[idx] = (s + 1.0) / 2.0
    if any(v is None for v in restored):
        raise ValueError("vLLM score response missing a position")
    return tuple(float(v) for v in restored)  # type: ignore[misc]


def normalize_deepinfra(body: dict[str, Any], expected: int) -> tuple[float, ...]:
    scores = body.get("scores")
    if not isinstance(scores, list) or len(scores) != expected:
        raise ValueError("DeepInfra response scores cardinality mismatch")
    out: list[float] = []
    for s in scores:
        if not isinstance(s, (int, float)) or not math.isfinite(float(s)):
            raise ValueError("DeepInfra score is non-finite")
        v = float(s)
        if not 0.0 <= v <= 1.0:
            raise ValueError("DeepInfra score outside [0, 1]")
        out.append(v)
    return tuple(out)


def extract_cloud_scores(baseline_row: dict[str, Any], expected: int) -> tuple[float, ...]:
    response = baseline_row.get("response", {})
    scores = response.get("scores")
    if not isinstance(scores, list) or len(scores) != expected:
        raise ValueError("cloud baseline scores cardinality mismatch")
    out: list[float] = []
    for s in scores:
        if not isinstance(s, (int, float)) or not math.isfinite(float(s)):
            raise ValueError("cloud score is non-finite")
        out.append(float(s))
    return tuple(out)


def ranks(scores: tuple[float, ...]) -> list[int]:
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    rank = [0] * len(scores)
    for position, idx in enumerate(order):
        rank[idx] = position + 1
    return rank


def top_k_overlap(a: tuple[float, ...], b: tuple[float, ...], k: int) -> float:
    if k <= 0 or len(a) < k:
        return float("nan")
    ra = set(ranks(a)[:k])
    rb = set(ranks(b)[:k])
    return len(ra & rb) / k


def top1_agreement(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return ranks(a)[0] == ranks(b)[0]


def spearman(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    n = len(a)
    if n < 2:
        return float("nan")
    ra = ranks(a)
    rb = ranks(b)
    d2 = sum((ra[i] - rb[i]) ** 2 for i in range(n))
    return 1.0 - 6.0 * d2 / (n * (n * n - 1))


def dcg(rels: list[int]) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))


def ndcg_at_k(scores: tuple[float, ...], relevance: list[int], k: int = 10) -> float:
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    gained = [relevance[i] for i in order]
    ideal = sorted(relevance, reverse=True)[:k]
    idcg = dcg(ideal)
    return dcg(gained) / idcg if idcg > 0 else 0.0


def call_local(
    url: str,
    path: str,
    model: str,
    request_body: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    endpoint = url.rstrip("/") + path
    payload = canonical_json(request_body)
    req = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        body = {"_raw": raw.decode("utf-8", errors="replace")}
    return status, body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--local-url", required=True)
    parser.add_argument("--local-path", default="/v1/score")
    parser.add_argument("--local-model", default="Querit/Querit-4B")
    parser.add_argument("--local-contract", default="vllm-score", choices=["vllm-score", "deepinfra"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--rate-delay-seconds", type=float, default=0.3)
    parser.add_argument("--max-groups", type=int, default=0)
    args = parser.parse_args()

    corpus_index = load_corpus_index(args.corpus)
    baseline_rows = load_jsonl(args.baseline)
    if args.max_groups > 0:
        baseline_rows = baseline_rows[: args.max_groups]

    normalizer = normalize_vllm_score if args.local_contract == "vllm-score" else normalize_deepinfra

    groups_compared = 0
    failed = 0
    spearman_values: list[float] = []
    top1_matches = 0
    top3_overlaps: list[float] = []
    top5_overlaps: list[float] = []
    local_ndcg: list[float] = []
    cloud_ndcg: list[float] = []

    args.output.parent.mkdir(parents=True, exist_ok=True)
    per_group_path = args.output.with_suffix(".groups.jsonl")
    per_group_handle = per_group_path.open("w", encoding="utf-8")

    for row in baseline_rows:
        qid = row.get("query_id")
        group = corpus_index.get(qid)
        if not group:
            failed += 1
            continue
        candidates = group["candidates"]
        expected = len(candidates)
        query = group["query"]
        documents = [c["document"] for c in candidates]
        if args.local_contract == "deepinfra":
            request_body = {
                "queries": [query] * expected,
                "documents": documents,
            }
        else:
            request_body = {
                "model": args.local_model,
                "text_1": [query] * expected,
                "text_2": documents,
            }
        try:
            status, body = call_local(
                args.local_url, args.local_path, args.local_model, request_body, args.timeout
            )
            local_scores = normalizer(body, expected)
            cloud_scores = extract_cloud_scores(row, expected)
        except Exception as exc:
            failed += 1
            print(f"ERROR {qid}: {exc}", file=sys.stderr)
            continue

        if status != 200:
            failed += 1
            continue

        relevance = [int(c.get("relevance", 0)) for c in candidates]
        sp = spearman(local_scores, cloud_scores)
        t1 = top1_agreement(local_scores, cloud_scores)
        t3 = top_k_overlap(local_scores, cloud_scores, 3)
        t5 = top_k_overlap(local_scores, cloud_scores, 5)
        l_ndcg = ndcg_at_k(local_scores, relevance, 10)
        c_ndcg = ndcg_at_k(cloud_scores, relevance, 10)

        spearman_values.append(sp)
        top3_overlaps.append(t3)
        top5_overlaps.append(t5)
        local_ndcg.append(l_ndcg)
        cloud_ndcg.append(c_ndcg)
        if t1:
            top1_matches += 1
        groups_compared += 1

        per_group = {
            "query_id": qid,
            "source_language": group.get("source_language"),
            "pair_count": expected,
            "spearman": round(sp, 6),
            "top1_match": t1,
            "top3_overlap": round(t3, 4),
            "top5_overlap": round(t5, 4),
            "local_ndcg10": round(l_ndcg, 6),
            "cloud_ndcg10": round(c_ndcg, 6),
        }
        per_group_handle.write(canonical_json(per_group).decode("utf-8") + "\n")
        per_group_handle.flush()

        if args.rate_delay_seconds > 0:
            time.sleep(args.rate_delay_seconds)

    per_group_handle.close()

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else float("nan")

    receipt = {
        "schema": "reranker-local-vs-cloud-receipt-v1",
        "baseline": str(args.baseline),
        "corpus": str(args.corpus),
        "local_url": args.local_url,
        "local_path": args.local_path,
        "local_model": args.local_model,
        "local_contract": args.local_contract,
        "groups_compared": groups_compared,
        "groups_failed": failed,
        "mean_spearman": round(mean(spearman_values), 6),
        "top1_agreement": round(top1_matches / groups_compared, 6) if groups_compared else float("nan"),
        "mean_top3_overlap": round(mean(top3_overlaps), 6),
        "mean_top5_overlap": round(mean(top5_overlaps), 6),
        "mean_local_ndcg10": round(mean(local_ndcg), 6),
        "mean_cloud_ndcg10": round(mean(cloud_ndcg), 6),
        "per_group_file": per_group_path.name,
    }
    args.output.write_text(canonical_json(receipt).decode("utf-8") + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
