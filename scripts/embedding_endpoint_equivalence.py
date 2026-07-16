#!/usr/bin/env python3
"""
Cloud vs Local embedding endpoint equivalence test.

Compares embeddings from a cloud API (DeepInfra) and a local vLLM endpoint
for the same model. Produces a detailed report on whether the two endpoints
are production-equivalent (no quality gap, not requiring byte-identical output).

Supports two corpus sources:
  1. Built-in JSONL datasets (STS22/STS17) committed under data/embedding-equivalence/
  2. Custom JSONL file with {"sentence1": ..., "sentence2": ..., "score": ...} per line

Usage:
    # Real datasets (default — covers English and Chinese)
    DEEPINFRA_KEY=xxx python3 scripts/embedding_endpoint_equivalence.py \
        --cloud-base-url https://api.deepinfra.com/v1/openai \
        --cloud-model Qwen/Qwen3-Embedding-8B \
        --local-base-url http://gb10:18012/v1 \
        --local-model qwen3-embedding-8b

    # English only
    DEEPINFRA_KEY=xxx python3 scripts/embedding_endpoint_equivalence.py \
        --cloud-base-url ... --cloud-model ... \
        --local-base-url ... --local-model ... \
        --lang en

    # Custom dataset
    DEEPINFRA_KEY=xxx python3 scripts/embedding_endpoint_equivalence.py \
        ... --corpus my_data.jsonl

Metrics produced:
    1. Per-text cosine similarity (local vs cloud embedding of same text)
    2. Retrieval rank correlation (Spearman) for query→document ranking
    3. Recall@5 and Recall@10 agreement
    4. Embedding norm comparison
    5. Cost report
"""

import argparse
import glob
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data" / "embedding-equivalence"

# Built-in datasets and their language codes
DATASET_FILES = {
    "en": ["sts22-en.jsonl", "sts17-en-en.jsonl"],
    "zh": ["sts22-zh.jsonl"],
    "zh-en": ["sts22-zh-en.jsonl"],
}


def load_corpus_from_jsonl(path: str) -> list[str]:
    """Load texts from a JSONL file with sentence1/sentence2 fields."""
    texts: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for key in ("sentence1", "sentence2"):
                val = row.get(key)
                if val:
                    texts.append(val)
    return texts


def load_builtin_corpus(langs: list[str]) -> tuple[list[str], dict[str, int]]:
    """Load texts from built-in JSONL datasets for the given languages."""
    texts: list[str] = []
    source_counts: dict[str, int] = {}

    for lang in langs:
        files = DATASET_FILES.get(lang, [])
        for fname in files:
            path = DATA_DIR / fname
            if not path.exists():
                print(f"WARNING: dataset file not found: {path}", file=sys.stderr)
                continue
            file_texts = load_corpus_from_jsonl(str(path))
            texts.extend(file_texts)
            source_counts[f"{lang}/{fname}"] = len(file_texts)

    return texts, source_counts


def load_custom_corpus(path: str) -> tuple[list[str], dict[str, int]]:
    """Load texts from a custom JSONL file."""
    texts = load_corpus_from_jsonl(path)
    return texts, {f"custom/{Path(path).name}": len(texts)}


# ---------------------------------------------------------------------------
# Embedding API client
# ---------------------------------------------------------------------------

def fetch_embeddings(
    base_url: str,
    model: str,
    texts: list[str],
    api_key: Optional[str] = None,
    batch_size: int = 64,
    timeout: int = 60,
) -> list[list[float]]:
    """Fetch embeddings from an OpenAI-compatible endpoint in batches."""
    all_embeddings: list[list[float]] = []
    total_batches = math.ceil(len(texts) / batch_size)

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        batch_num = i // batch_size + 1

        payload = json.dumps({"model": model, "input": batch}).encode("utf-8")
        url = f"{base_url}/embeddings"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
                    for item in sorted(data["data"], key=lambda x: x["index"]):
                        all_embeddings.append(item["embedding"])
                    break
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                if attempt == 2:
                    raise
                print(f"  batch {batch_num}/{total_batches}: retry ({e})", file=sys.stderr)
                time.sleep(2 ** attempt)

        if batch_num % 5 == 0 or batch_num == total_batches:
            print(f"  batch {batch_num}/{total_batches} done ({len(all_embeddings)}/{len(texts)})",
                  file=sys.stderr)

    return all_embeddings


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embedding_norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def rank_documents(query_emb: list[float], doc_embs: list[list[float]]) -> list[int]:
    """Return document indices sorted by descending cosine similarity."""
    scores = [(i, cosine_similarity(query_emb, doc_embs[i])) for i in range(len(doc_embs))]
    scores.sort(key=lambda x: x[1], reverse=True)
    return [i for i, _ in scores]


def spearman_rank_correlation(ranks_a: list[int], ranks_b: list[int]) -> float:
    n = len(ranks_a)
    if n < 2:
        return 1.0
    pos_a = {idx: rank for rank, idx in enumerate(ranks_a)}
    pos_b = {idx: rank for rank, idx in enumerate(ranks_b)}
    d_sq = sum((pos_a[idx] - pos_b[idx]) ** 2 for idx in range(n))
    return 1.0 - (6.0 * d_sq) / (n * (n * n - 1))


def recall_at_k(ranks_a: list[int], ranks_b: list[int], k: int) -> float:
    top_a = set(ranks_a[:k])
    top_b = set(ranks_b[:k])
    return len(top_a & top_b) / k


def percentile(sorted_list: list[float], p: float) -> float:
    """Return the p-th percentile (0-100) from a sorted list."""
    if not sorted_list:
        return 0.0
    idx = int(len(sorted_list) * p / 100.0)
    idx = min(idx, len(sorted_list) - 1)
    return sorted_list[idx]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Cloud vs local embedding endpoint equivalence test")
    parser.add_argument("--cloud-base-url", required=True)
    parser.add_argument("--cloud-model", required=True)
    parser.add_argument("--cloud-api-key-env", default="DEEPINFRA_KEY",
                        help="Environment variable name for cloud API key")
    parser.add_argument("--local-base-url", required=True)
    parser.add_argument("--local-model", required=True)
    parser.add_argument("--lang", default="en,zh,zh-en",
                        help="Comma-separated language codes for built-in datasets (en, zh, zh-en)")
    parser.add_argument("--corpus", default=None,
                        help="Custom JSONL corpus file (overrides --lang)")
    parser.add_argument("--num-queries", type=int, default=100,
                        help="Number of synthetic queries for retrieval ranking test")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--output", default=None, help="Output JSON report path")
    args = parser.parse_args()

    cloud_key = os.environ.get(args.cloud_api_key_env, "")
    if not cloud_key and "localhost" not in args.cloud_base_url:
        print(f"ERROR: {args.cloud_api_key_env} not set", file=sys.stderr)
        sys.exit(1)

    # Load corpus
    if args.corpus:
        print(f"Loading custom corpus: {args.corpus}", file=sys.stderr)
        corpus_texts, sources = load_custom_corpus(args.corpus)
    else:
        langs = [l.strip() for l in args.lang.split(",")]
        print(f"Loading built-in datasets for languages: {langs}", file=sys.stderr)
        corpus_texts, sources = load_builtin_corpus(langs)

    if not corpus_texts:
        print("ERROR: no corpus texts loaded", file=sys.stderr)
        sys.exit(1)

    # Deduplicate
    seen = set()
    docs = []
    for t in corpus_texts:
        if t not in seen:
            seen.add(t)
            docs.append(t)

    print(f"Loaded {len(docs)} unique documents from:", file=sys.stderr)
    for src, count in sources.items():
        print(f"  {src}: {count} texts", file=sys.stderr)

    # Generate queries (for retrieval ranking test)
    query_topics = [
        "technology", "health", "politics", "science", "sports",
        "business", "culture", "environment", "education", "security",
        "人工智能", "气候变化", "经济发展", "公共卫生", "教育改革",
        "量子计算", "数据隐私", "清洁能源", "国际贸易", "太空探索",
    ]
    query_templates = [
        "What are the latest developments in {topic}?",
        "Explain the key concepts of {topic}.",
        "What are the challenges in {topic}?",
        "Recent news about {topic}.",
        "Applications of {topic} in industry.",
        "{topic}的最新进展是什么？",
        "解释{topic}的关键概念。",
        "{topic}面临哪些挑战？",
    ]
    import random
    rng = random.Random(42)
    queries = []
    for _ in range(args.num_queries):
        template = rng.choice(query_templates)
        topic = rng.choice(query_topics)
        queries.append(template.format(topic=topic))

    all_texts = docs + queries

    # Fetch cloud embeddings
    print(f"\nFetching cloud embeddings from {args.cloud_base_url}...", file=sys.stderr)
    t0 = time.time()
    cloud_embs = fetch_embeddings(
        args.cloud_base_url, args.cloud_model, all_texts,
        api_key=cloud_key, batch_size=args.batch_size)
    cloud_time = time.time() - t0
    print(f"  Done in {cloud_time:.1f}s", file=sys.stderr)

    # Fetch local embeddings
    print(f"\nFetching local embeddings from {args.local_base_url}...", file=sys.stderr)
    t0 = time.time()
    local_embs = fetch_embeddings(
        args.local_base_url, args.local_model, all_texts,
        api_key=None, batch_size=args.batch_size)
    local_time = time.time() - t0
    print(f"  Done in {local_time:.1f}s", file=sys.stderr)

    # Split back
    cloud_docs = cloud_embs[:len(docs)]
    cloud_queries = cloud_embs[len(docs):]
    local_docs = local_embs[:len(docs)]
    local_queries = local_embs[len(docs):]

    # ---- Metrics ----
    print("\nComputing metrics...", file=sys.stderr)

    # 1. Per-text cosine similarity
    cos_sims = []
    cloud_norms = []
    local_norms = []
    for i in range(len(all_texts)):
        cs = cosine_similarity(cloud_embs[i], local_embs[i])
        cos_sims.append(cs)
        cloud_norms.append(embedding_norm(cloud_embs[i]))
        local_norms.append(embedding_norm(local_embs[i]))

    cos_sims_sorted = sorted(cos_sims)
    cos_mean = sum(cos_sims) / len(cos_sims)
    cos_median = cos_sims_sorted[len(cos_sims_sorted) // 2]
    cos_p5 = percentile(cos_sims_sorted, 5)
    cos_min = cos_sims_sorted[0]
    # Count outliers below threshold for diagnostics
    cos_outliers = sum(1 for c in cos_sims if c < 0.998)

    # 2. Retrieval rank correlation
    spearman_scores = []
    recall5_scores = []
    recall10_scores = []
    for qi in range(len(queries)):
        cloud_rank = rank_documents(cloud_queries[qi], cloud_docs)
        local_rank = rank_documents(local_queries[qi], local_docs)
        sp = spearman_rank_correlation(cloud_rank, local_rank)
        spearman_scores.append(sp)
        recall5_scores.append(recall_at_k(cloud_rank, local_rank, min(5, len(docs))))
        recall10_scores.append(recall_at_k(cloud_rank, local_rank, min(10, len(docs))))

    spearman_sorted = sorted(spearman_scores)
    sp_mean = sum(spearman_scores) / len(spearman_scores)
    sp_median = spearman_sorted[len(spearman_sorted) // 2]
    sp_p5 = percentile(spearman_sorted, 5)
    sp_min = spearman_sorted[0]

    r5_mean = sum(recall5_scores) / len(recall5_scores)
    r10_mean = sum(recall10_scores) / len(recall10_scores)

    # 3. Norm comparison
    norm_diffs = sorted(abs(cn - ln) for cn, ln in zip(cloud_norms, local_norms))
    norm_diff_mean = sum(norm_diffs) / len(norm_diffs)
    norm_diff_median = norm_diffs[len(norm_diffs) // 2]
    norm_diff_max = norm_diffs[-1]

    # 4. Token estimate
    total_chars = sum(len(t) for t in all_texts)
    est_tokens = int(total_chars / 1.3)
    cloud_cost = est_tokens / 1_000_000 * 0.01

    # ---- Report ----
    report = {
        "test_config": {
            "cloud_base_url": args.cloud_base_url,
            "cloud_model": args.cloud_model,
            "local_base_url": args.local_base_url,
            "local_model": args.local_model,
            "num_docs": len(docs),
            "num_queries": len(queries),
            "embedding_dim": len(cloud_embs[0]) if cloud_embs else 0,
            "languages": args.lang if not args.corpus else "custom",
            "corpus_sources": sources,
        },
        "cosine_similarity_local_vs_cloud": {
            "mean": round(cos_mean, 6),
            "median": round(cos_median, 6),
            "p5": round(cos_p5, 6),
            "min": round(cos_min, 6),
            "outliers_below_threshold": cos_outliers,
            "verdict": "PASS" if cos_min > 0.998 else ("WARN" if cos_p5 > 0.998 else "FAIL"),
        },
        "retrieval_rank_correlation": {
            "spearman_mean": round(sp_mean, 6),
            "spearman_median": round(sp_median, 6),
            "spearman_p5": round(sp_p5, 6),
            "spearman_min": round(sp_min, 6),
            "recall_at_5_mean": round(r5_mean, 6),
            "recall_at_10_mean": round(r10_mean, 6),
            "verdict": "PASS" if sp_min > 0.95 else ("WARN" if sp_mean > 0.98 else "FAIL"),
        },
        "embedding_norm": {
            "cloud_mean": round(sum(cloud_norms) / len(cloud_norms), 6),
            "local_mean": round(sum(local_norms) / len(local_norms), 6),
            "abs_diff_mean": round(norm_diff_mean, 6),
            "abs_diff_median": round(norm_diff_median, 6),
            "abs_diff_max": round(norm_diff_max, 6),
        },
        "cost": {
            "estimated_tokens": est_tokens,
            "cloud_cost_usd": round(cloud_cost, 4),
            "cloud_time_seconds": round(cloud_time, 1),
            "local_time_seconds": round(local_time, 1),
        },
    }

    # Print summary
    print("\n" + "=" * 60)
    print("EMBEDDING EQUIVALENCE REPORT")
    print("=" * 60)
    print(f"\nDocuments: {len(docs)} | Queries: {len(queries)} | Dim: {len(cloud_embs[0])}")
    print(f"Languages: {args.lang if not args.corpus else 'custom'}")
    print(f"\n1. Cosine Similarity (same text, local vs cloud):")
    print(f"   mean={cos_mean:.6f} median={cos_median:.6f} p5={cos_p5:.6f} min={cos_min:.6f}")
    print(f"   Verdict: {report['cosine_similarity_local_vs_cloud']['verdict']}")
    print(f"\n2. Retrieval Rank Correlation:")
    print(f"   Spearman: mean={sp_mean:.6f} median={sp_median:.6f} p5={sp_p5:.6f} min={sp_min:.6f}")
    print(f"   Recall@5: {r5_mean:.4f} | Recall@10: {r10_mean:.4f}")
    print(f"   Verdict: {report['retrieval_rank_correlation']['verdict']}")
    print(f"\n3. Embedding Norm:")
    print(f"   cloud_mean={report['embedding_norm']['cloud_mean']:.4f}"
          f" local_mean={report['embedding_norm']['local_mean']:.4f}"
          f" max_diff={norm_diff_max:.6f}")
    print(f"\n4. Cost:")
    print(f"   est_tokens={est_tokens} cloud_cost=${cloud_cost:.4f}"
          f" cloud_time={cloud_time:.1f}s local_time={local_time:.1f}s")
    print("=" * 60)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\nFull report saved to {args.output}")

    if (report["cosine_similarity_local_vs_cloud"]["verdict"] == "PASS"
            and report["retrieval_rank_correlation"]["verdict"] == "PASS"):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
