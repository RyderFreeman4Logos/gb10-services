# Querit vLLM Migration — Research and Implementation Plan

**Date:** 2026-07-16
**Status:** Source-controlled canary ready; live probing pending

## Background

The current Querit reranker runs as a Python transformers server (`querit_openai_rerank_server.py`) on port 18013. Under 16x concurrent 32k-token requests, it returns 503/broken pipe for 83% of requests because `INFERENCE_SEMAPHORE(2)` serializes most forward passes. The goal is to replace it with vLLM's native pooling/score engine for proper token-based dynamic batching, chunked prefill, and request scheduling.

### First principle

GB10 local Querit-4B and cloud DeepInfra Qwen3-Reranker-8B must ultimately be transparently interchangeable. Developers program against the public DeepInfra contract for `Qwen/Qwen3-Reranker-8B`. Native vLLM scoring availability is necessary but is not, by itself, evidence of wire compatibility. This pairs with #172 (upstream failover, merged) for eventual automatic local↔cloud switching.

## Research Findings

### AEON v0.25 support (verified)

- Image `sha256:18c09e6b` = vLLM `0.25.0+aeon.sm121a.dflash` (upstream commit `702f4814`)
- `Qwen3ForSequenceClassification` resolved dynamically (not literal registry entry):
  - suffix `ForSequenceClassification` → runner `pooling`, task `classify`
  - `Qwen3ForCausalLM` wrapped by `as_seq_cls_model`
  - expects top-level scalar `score` head
  - `/score`, `/v1/score`, `/rerank`, `/v1/rerank`, `/v2/rerank` all present
  - requires `num_labels == 1`

### Checkpoint conversion

Pinned Querit revision: `7b796de30ad8dc772d6c46c75659c1341283a665`
- `head.weight`: BF16 `[2, 2560]`
- `head.bias`: BF16 `[2]`
- checkpoint: ~7.49 GiB, head tensors in `model-00002-of-00002.safetensors`

**Recommended: Tanh conversion (zero guard-proxy change)**

```python
score.weight = (head.weight[1:2] - head.weight[0:1]) / 2
score.bias   = (head.bias[1:2]   - head.bias[0:1])   / 2
```

Because `p1 - p0 = tanh((z1 - z0) / 2)`, vLLM with Tanh activation outputs the existing `[-1, 1]` score directly.

Config:
```json
{
  "architectures": ["Qwen3ForSequenceClassification"],
  "num_labels": 1,
  "head_dtype": "model",
  "sbert_ce_default_activation_function": "torch.nn.modules.activation.Tanh"
}
```

- Set `head_dtype: "model"` for BF16 head behavior (vLLM defaults pooling heads to FP32)
- Do NOT set `problem_type` to regression or single-label classification
- Must rewrite the second safetensors shard (remove `head.*`, add `score.*`)
- Synthetic test: max rewrite error ~7.3e-5

### Prompt template

Jinja template verified byte-exact match to `render_current_prompt()`:
- rendered UTF-8 bytes: exact match
- token IDs: exact match
- final token: `151643` (`<|endoftext|>`)

### Score contract change

| Aspect | Current (Transformers) | vLLM |
|--------|----------------------|------|
| Pooling | `LEGACY_PHYSICAL_LAST_V1` (physical padded last position) | `LAST` (last real token per sequence) |
| Batch-dependent | Yes (padding changes which position is "last") | No (each sequence independent) |
| Correctness | Batch-dependent = potentially unstable | Batch-invariant = correct |

This is an improvement, not a regression. The new contract should be named `querit-prompt-last-real-v1`.

### Native scoring support

vLLM provides native `/v1/rerank` and `/v1/score` endpoints. With Tanh conversion, the expected model score domain is `[-1, 1]`. The temporary unit exposes these endpoints for a parent-run live canary; this source change does not assert public wire compatibility or authorize a route change.

### Live-probe blocker: DeepInfra wire compatibility

The canonical public endpoint is `/v1/inference/Qwen/Qwen3-Reranker-8B`: it takes equal-length `queries[]` and `documents[]` arrays and returns `scores[]`, `input_tokens`, and optional request metadata. Repository evidence currently establishes only vLLM's native `/v1/rerank` and `/v1/score` behavior. It does not establish that the raw vLLM server accepts the DeepInfra batch body or emits its response schema.

The parent must live-probe the canary's native endpoints and either prove an exact compatibility path or add a separately tested adapter before directing `reranker_endpoint_equivalence.py` at port 18014. No production cutover, guard route change, or retirement of the Transformers service is permitted on source evidence alone.

### Memory budget

Current: text 41.4G + emb 20.3G + RR 17.6G = 79.3G / 121.6G, MemAvail 26.6G
vLLM Querit (replacing RR): ~8G weights + 4.8G KV + overhead ≈ 15-18G (same or less)

### v0.24 → v0.25 note

The original adapter document assumed v0.24.0. All references updated to v0.25.0 in commit `db45ede`.

## Implementation Plan

### Phase 1: Checkpoint conversion (offline)
1. Copy pinned snapshot to `/models/querit-4b-vllm/`
2. Rewrite `model-00002-of-00002.safetensors`: `head.*` → `score.*` (Tanh conversion)
3. Update `config.json`: architectures, num_labels, head_dtype, activation
4. Update `model.safetensors.index.json` weight_map
5. Install verified Jinja template as `querit-rerank.jinja`

### Phase 2: Smoke test (temporary port)
1. Install and start `vllm-querit-4b-canary.service` with its conservative 32K profile on host port 18014
2. Verify `/v1/models`, `/v1/score`, `/v1/rerank`
3. Prove or adapt the canonical DeepInfra wire contract before endpoint equivalence testing
4. Compare scores against transformers server (max_batch=1) on pinned public data

### Phase 3: Production cutover
1. Stop current transformers RR
2. Deploy vLLM Querit unit on port 18013
3. Three-service startup + load test
4. Lower guard active requests to 4 initially

### Phase 4: Validation
1. Numerical replay: max_batch=1 transformers vs vLLM across lengths/languages
2. Mixed-length batch invariance test
3. Concurrency: 1/2/4/8 active, measure p50/p95/p99, pairs/s, memory

## Recommended vLLM config

```bash
vllm serve /home/obj/models/querit-4b-vllm \
  --host 0.0.0.0 \
  --port 8000 \
  --served-model-name qwen3-reranker-8b Qwen/Qwen3-Reranker-8B Querit/Querit-4B \
  --runner pooling \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --kv-cache-memory-bytes 4800M \
  --max-num-batched-tokens 4096 \
  --max-num-seqs 16 \
  --enable-chunked-prefill \
  --max-num-partial-prefills 1 \
  --max-long-partial-prefills 1 \
  --long-prefill-token-threshold 8192 \
  --enforce-eager \
  --chat-template /home/obj/models/querit-4b-vllm/querit-rerank.jinja
```

The tracked canary publishes container port 8000 as host port 18014. It has no lifecycle dependency or conflict with embedding or production reranking, removes and stops only its own container, and is not a recovery target. Docker bounds it to 18 GiB memory and swap with swappiness 0; both systemd and the container use OOM score adjustment 500.
