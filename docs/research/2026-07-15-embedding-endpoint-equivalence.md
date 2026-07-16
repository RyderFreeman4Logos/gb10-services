# Qwen3-Embedding-8B: Cloud vs Local Endpoint Equivalence

**Date**: 2026-07-15
**Status**: PASS — cloud and local endpoints are production-equivalent
**Researcher**: autonomous agent drain

## Objective

Determine whether `Qwen3-Embedding-8B` served locally (vLLM on GB10) and via cloud API (DeepInfra) produce embeddings that are quality-equivalent for production use. Equivalence means no practical quality gap, not byte-identical output.

## Endpoints

| Endpoint | Base URL | Model ID |
|----------|----------|----------|
| Cloud (DeepInfra) | `https://api.deepinfra.com/v1/openai` | `Qwen/Qwen3-Embedding-8B` |
| Local (GB10 vLLM) | `http://gb10:18012/v1` | `qwen3-embedding-8b` |

Both report 4096-dimensional embeddings.

## Methodology

### Test corpus
- **500 documents** + **100 queries** = 600 total texts
- Synthetic corpus covering:
  - Short phrases (topic keywords, shuffled)
  - Medium sentences (template-based, topic-filled)
  - Long paragraphs (2-paragraph domain text)
  - Code snippets (Python, SQL, Rust, shell, JS, kubectl)
  - Multilingual text (Chinese, English, mixed — 8 multilingual entries)
- Deterministic seed (42) for reproducibility

### Metrics
1. **Per-text cosine similarity**: cosine(emb_cloud(text), emb_local(text)) for each text
2. **Retrieval rank correlation**: Spearman ρ between cloud-ranked and local-ranked document lists per query
3. **Recall@5 / Recall@10**: overlap of top-k document sets between the two endpoints
4. **Embedding norm comparison**: both endpoints should produce unit-norm vectors

### Cost
- ~74K estimated tokens
- DeepInfra price: $0.01/M tokens → **$0.0007 total**
- Cloud latency: 128s for 600 texts (batch=64)
- Local latency: 110s for 600 texts (batch=64)

## Results

### 1. Cosine Similarity (same text, cloud vs local)

| Metric | Value |
|--------|-------|
| Mean | 0.999892 |
| Median | 0.999895 |
| P5 | 0.999840 |
| Min | 0.999734 |
| **Verdict** | **PASS** (threshold: min > 0.998) |

Every single text has cosine similarity > 0.9997 between cloud and local embeddings. The two endpoints produce nearly identical direction for the same input.

### 2. Retrieval Rank Correlation

| Metric | Value |
|--------|-------|
| Spearman ρ mean | 0.999627 |
| Spearman ρ median | 0.999647 |
| Spearman ρ P5 | 0.999480 |
| Spearman ρ min | 0.999218 |
| Recall@5 | 0.9480 (94.8%) |
| Recall@10 | 0.9640 (96.4%) |
| **Verdict** | **PASS** (threshold: ρ min > 0.95) |

Retrieval rankings are virtually identical. The minimum Spearman correlation across 100 queries is 0.9992 — far above the 0.95 threshold. Recall@10 is 96.4%, meaning the top 10 results overlap almost perfectly.

### 3. Embedding Norm

| Metric | Value |
|--------|-------|
| Cloud mean norm | 1.0000 |
| Local mean norm | 1.0000 |
| Max abs difference | 0.000000 |

Both endpoints produce exactly unit-normalized vectors (Qwen3-Embedding normalizes output).

### 4. Raw vector comparison (sample)

For `"hello world"`:
- Cloud first 5: `[0.02115, 0.01040, -0.02012, -0.02989, 0.01621]`
- Local first 5: `[0.02092, 0.01040, -0.02046, -0.02965, 0.01574]`

Values differ slightly (last 3-4 decimal places) but cosine similarity is >0.9999. This is expected: cloud and local vLLM may use different batch sizes, attention implementations, or numerical precision paths.

## Conclusion

**The cloud (DeepInfra) and local (GB10 vLLM) endpoints for Qwen3-Embedding-8B are production-equivalent.** The embeddings are functionally interchangeable for:

- Semantic search and retrieval
- Clustering
- Classification
- Any downstream task relying on embedding direction or relative ranking

The minuscule numerical differences (cosine > 0.9997 for all texts) are below the threshold that would affect any practical application.

## Limitations

- Synthetic corpus (not extracted from real production traffic). Future tests should use real datasets from HuggingFace.
- 500 documents may not cover all edge cases (very long context, rare languages, adversarial inputs).
- The test verifies embedding equivalence but not latency/cost characteristics under load.

## Reproduction

```bash
DEEPINFRA_KEY=xxx python3 scripts/embedding_endpoint_equivalence.py \
    --cloud-base-url https://api.deepinfra.com/v1/openai \
    --cloud-model "Qwen/Qwen3-Embedding-8B" \
    --local-base-url http://gb10:18012/v1 \
    --local-model qwen3-embedding-8b \
    --num-docs 500 \
    --num-queries 100 \
    --output /tmp/embedding-equivalence-report.json
```

Script: `scripts/embedding_endpoint_equivalence.py`
Full report: `/tmp/embedding-equivalence-report.json`
