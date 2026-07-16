#!/usr/bin/env python3
"""
Cloud vs Local embedding equivalence test.

Compares embeddings from a cloud API (DeepInfra) and a local vLLM endpoint
for the same model (Qwen3-Embedding-8B). Produces a detailed report on whether
the two endpoints are production-equivalent (no quality gap, not requiring
byte-identical output).

Usage:
    # Set DEEPINFRA_KEY env var first
    DEEPINFRA_KEY=xxx python3 scripts/embedding_endpoint_equivalence.py \
        --cloud-base-url https://api.deepinfra.com/v1/openai \
        --cloud-model Qwen/Qwen3-Embedding-8B \
        --local-base-url http://gb10:18012/v1 \
        --local-model qwen3-embedding-8b \
        --num-docs 500 \
        --num-queries 100

Metrics produced:
    1. Per-text cosine similarity (local vs cloud embedding of same text)
    2. Retrieval rank correlation (Spearman) for query→document ranking
    3. Recall@5 and Recall@10 agreement
    4. Embedding norm comparison
    5. Cost report

The test corpus is generated synthetically to cover:
    - Short phrases (1-10 tokens)
    - Medium sentences (10-50 tokens)
    - Long paragraphs (50-200 tokens)
    - Code snippets
    - Multilingual text (Chinese, English, mixed)
    - Technical/domain-specific text
"""

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.request
import urllib.error
from typing import Optional


# ---------------------------------------------------------------------------
# Synthetic corpus generation
# ---------------------------------------------------------------------------

TOPICS = [
    "machine learning", "distributed systems", "quantum computing",
    "climate change", "ancient history", "software engineering",
    "molecular biology", "game theory", "cryptography", "linguistics",
    "astrophysics", "neural networks", "database optimization", "container orchestration",
    "natural language processing", "computer vision", "reinforcement learning",
    "blockchain consensus", "edge computing", "data privacy",
]

CODE_SNIPPETS = [
    "def fibonacci(n): return n if n <= 1 else fibonacci(n-1) + fibonacci(n-2)",
    "SELECT u.name, COUNT(o.id) FROM users u LEFT JOIN orders o ON u.id = o.user_id GROUP BY u.name",
    "import numpy as np; matrix = np.random.randn(100, 100); eigenvalues = np.linalg.eigvals(matrix)",
    "async fn fetch_data(url: &str) -> Result<String, Box<dyn std::error::Error>> { /* ... */ }",
    "docker run --rm -v $(pwd):/data python:3.12 python /data/script.py --input data.csv",
    "kubectl apply -f deployment.yaml && kubectl rollout status deployment/api-server",
    "const pipe = (fns) => (x) => fns.reduce((v, f) => f(v), x);",
    "git rebase -i HEAD~3  # squash the last three commits into one",
]

MULTILINGUAL = [
    "机器学习是人工智能的一个分支，它使计算机系统能够从数据中学习并做出决策。",
    "分布式系统是由多个独立计算机组成的系统，这些计算机通过消息传递进行通信。",
    "The quick brown fox jumps over the lazy dog. 这只敏捷的棕色狐狸跳过了那只懒狗。",
    "量子计算机利用量子比特进行计算，能够解决经典计算机难以处理的问题。",
    "容器编排是自动化部署、扩展和管理容器化应用程序的过程。",
    "密码学研究如何在存在 adversaries 的情况下保护信息安全。",
    "深度学习使用多层神经网络从大量数据中提取层次化的特征表示。",
    "边缘计算将数据处理从中心数据中心移到靠近数据源的设备上。",
]

SENTENCE_TEMPLATES = [
    "The fundamental principle of {topic} involves understanding complex interactions between multiple components.",
    "Recent advances in {topic} have demonstrated significant improvements in efficiency and accuracy.",
    "A critical challenge in {topic} is balancing theoretical elegance with practical implementation constraints.",
    "Researchers studying {topic} often employ empirical methods to validate theoretical predictions.",
    "The history of {topic} spans several decades, with key breakthroughs occurring in recent years.",
    "Industry adoption of {topic} technologies has accelerated due to decreasing computational costs.",
    "Future directions in {topic} include integration with emerging paradigms and cross-disciplinary applications.",
    "Open problems in {topic} continue to motivate active research across academic and industrial labs.",
]

PARAGRAPH_TEMPLATES = [
    "{topic} represents a convergence of theoretical foundations and practical engineering. "
    "The core methodology relies on iterative refinement, where initial approximations are "
    "progressively improved through feedback mechanisms. This approach has proven effective "
    "across diverse domains, from scientific computing to consumer applications. However, "
    "scaling these methods to production environments introduces unique challenges related "
    "to resource allocation, fault tolerance, and maintainability.",

    "In the context of {topic}, several key metrics are used to evaluate performance. "
    "These include throughput, latency, accuracy, and resource utilization. Modern systems "
    "must balance these competing objectives, often employing multi-objective optimization "
    "techniques. The tradeoff space is vast, and optimal configurations depend heavily on "
    "the specific workload characteristics and deployment environment. Recent work has "
    "explored automated tuning approaches using reinforcement learning and Bayesian optimization.",
]


def generate_corpus(num_docs: int, num_queries: int) -> tuple[list[str], list[str]]:
    """Generate a synthetic corpus of documents and queries."""
    rng = random.Random(42)  # deterministic
    docs: list[str] = []
    queries: list[str] = []

    # Mix of content types
    total = num_docs
    n_code = max(1, total // 8)
    n_multi = max(1, total // 8)
    n_short = max(1, total // 6)
    n_medium = max(1, total // 3)
    n_long = total - n_code - n_multi - n_short - n_medium

    # Code snippets
    for _ in range(n_code):
        docs.append(rng.choice(CODE_SNIPPETS))

    # Multilingual
    for _ in range(n_multi):
        docs.append(rng.choice(MULTILINGUAL))

    # Short phrases
    for _ in range(n_short):
        topic = rng.choice(TOPICS)
        words = topic.split()
        rng.shuffle(words)
        docs.append(" ".join(words))

    # Medium sentences
    for _ in range(n_medium):
        template = rng.choice(SENTENCE_TEMPLATES)
        topic = rng.choice(TOPICS)
        docs.append(template.format(topic=topic))

    # Long paragraphs
    for _ in range(n_long):
        template = rng.choice(PARAGRAPH_TEMPLATES)
        topic = rng.choice(TOPICS)
        docs.append(template.format(topic=topic))

    rng.shuffle(docs)

    # Generate queries
    query_templates = [
        "What is {topic}?",
        "How does {topic} work?",
        "Explain the key concepts of {topic}.",
        "What are the challenges in {topic}?",
        "Recent advances in {topic}.",
        "Applications of {topic} in industry.",
        "Best practices for {topic}.",
        "Comparison of approaches in {topic}.",
    ]
    for _ in range(num_queries):
        template = rng.choice(query_templates)
        topic = rng.choice(TOPICS)
        queries.append(template.format(topic=topic))

    return docs, queries


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
    """Spearman's rank correlation coefficient."""
    n = len(ranks_a)
    if n < 2:
        return 1.0
    # Convert to rank positions
    pos_a = {idx: rank for rank, idx in enumerate(ranks_a)}
    pos_b = {idx: rank for rank, idx in enumerate(ranks_b)}
    d_sq = sum((pos_a[idx] - pos_b[idx]) ** 2 for idx in range(n))
    return 1.0 - (6.0 * d_sq) / (n * (n * n - 1))


def recall_at_k(ranks_a: list[int], ranks_b: list[int], k: int) -> float:
    """Fraction of top-k items from ranks_a that also appear in top-k of ranks_b."""
    top_a = set(ranks_a[:k])
    top_b = set(ranks_b[:k])
    return len(top_a & top_b) / k


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
    parser.add_argument("--num-docs", type=int, default=500)
    parser.add_argument("--num-queries", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default=None, help="Output JSON report path")
    args = parser.parse_args()

    cloud_key = os.environ.get(args.cloud_api_key_env, "")
    if not cloud_key and "localhost" not in args.cloud_base_url:
        print(f"ERROR: {args.cloud_api_key_env} not set", file=sys.stderr)
        sys.exit(1)

    print(f"Generating corpus: {args.num_docs} docs, {args.num_queries} queries...",
          file=sys.stderr)
    docs, queries = generate_corpus(args.num_docs, args.num_queries)
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

    cos_sims.sort()
    cos_mean = sum(cos_sims) / len(cos_sims)
    cos_median = cos_sims[len(cos_sims) // 2]
    cos_p5 = cos_sims[int(len(cos_sims) * 0.05)]
    cos_min = cos_sims[0]

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

    spearman_scores.sort()
    sp_mean = sum(spearman_scores) / len(spearman_scores)
    sp_median = spearman_scores[len(spearman_scores) // 2]
    sp_p5 = spearman_scores[int(len(spearman_scores) * 0.05)]
    sp_min = spearman_scores[0]

    r5_mean = sum(recall5_scores) / len(recall5_scores)
    r10_mean = sum(recall10_scores) / len(recall10_scores)

    # 3. Norm comparison
    norm_diffs = [abs(cn - ln) for cn, ln in zip(cloud_norms, local_norms)]
    norm_diffs.sort()
    norm_diff_mean = sum(norm_diffs) / len(norm_diffs)
    norm_diff_median = norm_diffs[len(norm_diffs) // 2]
    norm_diff_max = norm_diffs[-1]

    # 4. Token estimate (rough: ~1.3 chars per token)
    total_chars = sum(len(t) for t in all_texts)
    est_tokens = int(total_chars / 1.3)
    cloud_cost = est_tokens / 1_000_000 * 0.01  # $0.01/M tokens

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
            "seed": args.seed,
        },
        "cosine_similarity_local_vs_cloud": {
            "mean": round(cos_mean, 6),
            "median": round(cos_median, 6),
            "p5": round(cos_p5, 6),
            "min": round(cos_min, 6),
            "verdict": "PASS" if cos_min > 0.998 else ("WARN" if cos_min > 0.995 else "FAIL"),
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
            json.dump(report, f, indent=2)
        print(f"\nFull report saved to {args.output}")

    # Exit code: PASS only if both metrics pass
    if (report["cosine_similarity_local_vs_cloud"]["verdict"] == "PASS"
            and report["retrieval_rank_correlation"]["verdict"] == "PASS"):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
