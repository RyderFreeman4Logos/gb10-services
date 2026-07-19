# MIRACL reranker equivalence corpus

This directory contains a deterministic, network-free runtime corpus derived
only from the public `mteb/MIRACLReranking` development split. The source is
pinned to Hub commit `ab6f54eff185a84bc1f6ab96b56bc7df87433228` and is licensed
under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).

The corpus contains 100 English and 100 Chinese query groups. Every group has
exactly 10 documents selected from `top_ranked`: the first qrels-positive and
the first nine qrels-zero hard negatives, restored to their original ranking
order. Query and corpus `text` fields are copied without generation or
rewriting; the separate source `title` field is not used.
`metadata.json` records the exact source URLs, sizes, SHA-256 checksums, license,
revision, and selection algorithm.

Attribution follows the pinned dataset card: Zhang et al., *MIRACL: A
Multilingual Retrieval Dataset Covering 18 Diverse Languages*,
[doi:10.1162/tacl_a_00595](https://doi.org/10.1162/tacl_a_00595), together with
the MMTEB and MTEB benchmark papers listed in `metadata.json`.

Regenerate with a disposable source cache:

```bash
uv run --python 3.11 scripts/prepare_reranker_equivalence_corpus.py \
  --source-cache /tmp/miracl-reranking-pinned
```

The committed JSONL is sufficient for normal experiment runs; neither network
access nor PyArrow is required by `reranker_endpoint_equivalence.py`.

Preview the conservative cloud upper bound without any endpoint request:

```bash
python3 scripts/reranker_endpoint_equivalence.py --dry-run
```

The full 2,000-pair corpus currently estimates to 1,731,142 input tokens under
the byte-based upper bound, so the default 1,000,000-token / $0.05 hard cap
fails closed. A paid full-corpus run therefore requires explicit increases to
both `--max-estimated-input-tokens` and `--max-cloud-cost-usd`; use the dry-run
output to choose them. `--cache-only` loads both cloud and local responses from
their evidence caches and performs no DNS, socket, or endpoint operation. It
requires no cloud API key and ignores the send-only cost caps; every planned
request hash for both endpoints must already have complete response evidence or
the run aborts.
