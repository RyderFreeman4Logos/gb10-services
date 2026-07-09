# Querit-4B Reranker Replacement Research Diary — 2026-07-08

> **Diary location:** `docs/research/diary-001-querit-4b-reranker.md`  
> **Date:** 2026-07-08  
> **Status:** research only — do not deploy Querit-4B yet (vLLM head/compat blockers).  
> **Moved from:** `docs/querit-4b-reranker-research-2026-07-08.md`


## Question

Evaluate whether the GB10 reranker service can be changed from
`Qwen/Qwen3-Reranker-8B` to [`Querit/Querit-4B`](https://huggingface.co/Querit/Querit-4B)
while preserving the same operational contract for downstream clients such as
`mempal` and `verbatim`.

Desired operator properties:

- Keep the same raw reranker backend port: `100.105.4.92:18013`.
- Keep the same guard-owned reranker listener: `100.105.4.92:18003`.
- Keep the same public model aliases so callers do not need config changes:
  - `qwen3-reranker-8b`
  - `Qwen/Qwen3-Reranker-8B`
- Keep the same 40,960-token context window if possible.
- Reduce memory footprint if Querit-4B is viable.
- Prefer restarting only `vllm-qwen3-reranker-8b.service` rather than the full
  vLLM stack, when no stale swap/page cleanup is required.

## Current production contract

The current raw reranker unit is `systemd/vllm-qwen3-reranker-8b.service`.
It serves `Qwen/Qwen3-Reranker-8B` through vLLM pooling mode:

```text
vllm serve Qwen/Qwen3-Reranker-8B
  --served-model-name qwen3-reranker-8b Qwen/Qwen3-Reranker-8B
  --runner pooling
  --dtype bfloat16
  --max-model-len 40960
  --max-num-batched-tokens 40960
  --max-num-seqs 64
  --gpu-memory-utilization 0.22
  --kv-cache-memory-bytes 5820M
  --enforce-eager
  --chat-template /vllm-workspace/examples/pooling/score/template/qwen3_reranker.jinja
  --hf-overrides '{"architectures":["Qwen3ForSequenceClassification"],"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}'
  --trust-remote-code
```

`config/llm-guard-proxy/config.toml` routes the stable aliases to the raw
backend:

```toml
[[upstreams]]
name = "qwen3-reranker-8b"
base_url = "http://100.105.4.92:18013/v1"
match_models = ["qwen3-reranker-8b", "Qwen/Qwen3-Reranker-8B"]
request_timeout_ms = 1800000
max_in_flight_requests = 8
max_queued_generation_requests = 8
```

The live `/v1/models` output through raw `18013`, legacy `18003`, and aggregate
`18009` exposes both aliases with `max_model_len = 40960` and root
`Qwen/Qwen3-Reranker-8B`.

## Downstream client compatibility

### `mempal`

Observed local config:

```toml
[search.reranker]
enabled = true
endpoint = "gb10:18003"
model = "qwen3-reranker-8b"
timeout_secs = 30
top_k = 50
```

Relevant code: `../mempal/src/search/rerank.rs`.

Important behavior:

- Sends standard rerank body:
  - `model`
  - `query`
  - `documents`
  - `top_n`
- Normalizes bare endpoints such as `gb10:18003` to
  `http://gb10:18003/v1/rerank`.
- Sends the top search candidates' `content` values directly as documents;
  there is no reranker-specific token truncation before the endpoint call.
- Accepts response arrays under either `results` or `data`.
- Accepts score field as `score` with alias `relevance_score`.
- Sorts by larger finite score and appends unscored/omitted candidates in the
  original order.
- On reranker failure, falls back to original search order and returns a warning
  instead of failing the whole search.
- Some repository docs/examples mention the older alias `qwen3-reranker`
  without `-8b`. The observed local config uses `qwen3-reranker-8b`; future
  deploys can either keep configs on that alias or add `qwen3-reranker` as a
  third compatibility alias in both guard routing and the raw backend.

Conclusion: `mempal` should continue to work without config/code changes if the
service keeps `gb10:18003`, model alias `qwen3-reranker-8b`, and standard vLLM
`/v1/rerank` response shape.

### `verbatim`

Observed local config:

```toml
[rerank]
enabled = true
provider = "vllm"
base_url = "http://gb10:18003"
model = "Qwen/Qwen3-Reranker-8B"
top_n = 12
timeout_seconds = 1800
api_key = <redacted>
```

Relevant code:

- `../verbatim/crates/verbatim-core/src/provider/openai_compatible.rs`
- `../verbatim/crates/verbatim-core/src/provider/endpoint_capability.rs`
- `../verbatim/crates/verbatim-core/src/config.rs`

Important behavior:

- Sends standard rerank body:
  - `model`
  - `query`
  - `documents`
  - `top_n`
- Uses provider kind `vllm`, trying `/v1/rerank` first when base URL is not
  already suffixed with `/v1`.
- Accepts response arrays under `results`, `data`, or `rankings`.
- Accepts score field as `score` with aliases `relevance_score` and
  `rerank_score`.
- Accepts `index` or `document_index` per result and sorts internally by
  descending finite score, not by upstream response order.
- Performs `/v1/models` capability discovery and uses `max_context_tokens` /
  request limits to adapt candidate count and document character budget.
- The retrieval path caps rerank candidates to 50 and truncates each candidate
  document to about 8,000 characters before calling the endpoint. If the first
  request hits a context/payload limit, Verbatim refreshes endpoint capability
  and retries with a smaller shape.
- Repository defaults/docs mention `Qwen/Qwen3-Reranker-4B`; the observed local
  runtime config uses `Qwen/Qwen3-Reranker-8B`. To preserve "no config change",
  the actual runtime config must stay on one of the aliases that guard and raw
  vLLM serve.

Conclusion: `verbatim` should continue to work without config/code changes if
the service keeps `gb10:18003`, model alias `Qwen/Qwen3-Reranker-8B`, and
standard model discovery/rerank response shape.

### Alias routing caveat

`llm-guard-proxy` selects upstreams by exact `model` string match and forwards
the request body without rewriting the model field. Therefore preserving aliases
only in the guard config is insufficient: the raw backend on `18013` must also
serve every alias that clients may send. At minimum keep:

```text
--served-model-name qwen3-reranker-8b Qwen/Qwen3-Reranker-8B
```

Consider also adding `qwen3-reranker` if supporting older mempal examples or
ad-hoc user configs is important.

## Querit-4B model facts

HF model card highlights:

- Model: `Querit/Querit-4B`
- Type: multilingual cross-encoder reranker
- Base model: `Qwen/Qwen3-Embedding-4B`
- Total parameters: 4.02B
- Layers: 36
- Attention heads: 32
- Model card claims context length: 128k

HF `config.json` facts observed on 2026-07-08:

```text
architectures = ["MLQwen3Model"]
model_type = "qwen3"
hidden_size = 2560
num_hidden_layers = 36
num_attention_heads = 32
num_key_value_heads = 8
head_dim = 128
max_position_embeddings = 40960
rope_theta = 1000000
```

Important mismatch: the model card says 128k context, but the config currently
advertises `max_position_embeddings = 40960`. For this GB10 stack, treating
Querit-4B as a 40,960-token model is the safe assumption unless a separate
rope/context extension is tested.

## Memory implications

For BF16 KV cache, per-token KV size is:

```text
2 * num_hidden_layers * num_key_value_heads * head_dim * bytes_per_dtype
```

For both current `Qwen/Qwen3-Reranker-8B` and `Querit/Querit-4B`:

```text
2 * 36 * 8 * 128 * 2 = 147,456 bytes/token
```

Therefore, keeping `max-model-len = 40960` requires essentially the same KV
cache allocation for both models:

```text
147,456 * 40,960 = 5,760 MiB
```

The current production allocation:

```text
kv-cache-memory-bytes = 5820M
```

is only about 1.01x over the required 40,960-token context. It should not be
reduced if the goal is to preserve the same context window and avoid startup
capacity failures.

Expected memory savings from Querit-4B are primarily **weights/non-KV runtime**,
not KV cache. Querit safetensors are about 8 GB class (two shards), compared
with the current 8B reranker at about 16 GB class. A future canary may be able
to reduce Docker cgroup memory from 24 GB, but `kv-cache-memory-bytes` should
initially stay at `5820M`.

## vLLM compatibility findings

The production image is:

```text
ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-01-v0.24.0
```

Registry/config probes in that image showed:

- `MLQwen3Model` is **not** a supported vLLM architecture.
- Native Querit config resolves through vLLM to `Qwen3ForCausalLM` / embed-like
  pooling behavior, not to a rerank sequence classifier.
- Forcing:

  ```text
  --hf-overrides '{"architectures":["TransformersForSequenceClassification"]}'
  ```

  passes vLLM model-config construction and resolves to generic
  `TransformersForSequenceClassification`.

However, vLLM generic sequence classification has a serious head-name issue.
In `vllm/model_executor/models/transformers/pooling.py`, the generic mixin
instantiates `AutoModelForSequenceClassification.from_config(...)` and looks for
one of:

```python
classifier
score
```

Querit checkpoint weights are named:

```text
head.weight
head.bias
```

and its remote code defines:

```python
self.head = nn.Linear(hidden_size, 2)
...
return {"score": rank_scores, ...}
```

There is no `auto_map` in `config.json`, and the config architecture string is
`MLQwen3Model` while the remote code class is `QueritModel`. This makes a naive
vLLM swap unsafe:

- native Querit may run as the wrong task type;
- generic sequence classification may fail to load the trained head; or
- worse, it may start with a randomly initialized `score` classifier and produce
  meaningless rerank scores.

## Operational restart note

If only the reranker model/service is changed and no stale swap/page cleanup is
needed, a single-service restart should be acceptable:

```bash
systemctl --user restart vllm-qwen3-reranker-8b.service
```

This should not restart chat or embedding. The reranker unit does have:

- `Requires=` / `BindsTo=` / `PartOf=` relationship to
  `vllm-aeon-27b-dflash.service`;
- an `ExecStartPre` readiness wait against the chat service.

So chat must be healthy for the reranker restart to complete. During the
reranker restart, guard-owned rerank routes on `18003`/`18009` will be
unavailable or return upstream errors until the raw backend on `18013` is ready.

## Recommendation

Do **not** directly deploy `Querit/Querit-4B` by only changing the model name in
`vllm-qwen3-reranker-8b.service`.

Recommended future path:

1. Wait until unrelated long-running benchmarks are complete, to avoid changing
   GB10 memory/GPU conditions mid-run.
2. Prefetch `Querit/Querit-4B` into the GB10 Hugging Face cache while keeping
   production service unchanged.
3. Build a canary with fast rollback. Prefer one of:
   - temporary alternate port canary if memory headroom permits; or
   - stop only `vllm-qwen3-reranker-8b.service`, start Querit on the same raw
     port `18013`, smoke it, and rollback immediately on failure.
4. Preserve aliases during canary:

   ```text
   --served-model-name qwen3-reranker-8b Qwen/Qwen3-Reranker-8B
   ```

5. Start with the same context/KV budget:

   ```text
   --max-model-len 40960
   --max-num-batched-tokens 40960
   --kv-cache-memory-bytes 5820M
   ```

   Reduce Docker cgroup memory only after startup and load are verified.
6. Smoke tests before exposing it to real retrieval. Test the raw backend, the
   legacy reranker listener, and the aggregate guard entrypoint; test both kept
   aliases because guard routes by exact `model` string and does not rewrite the
   request body:

   ```bash
   curl -fsS http://100.105.4.92:18013/v1/models | python3 -m json.tool
   curl -fsS http://100.105.4.92:18003/v1/models | python3 -m json.tool

   for base in \
     http://100.105.4.92:18013 \
     http://100.105.4.92:18003 \
     http://100.105.4.92:18009; do
     for model in qwen3-reranker-8b Qwen/Qwen3-Reranker-8B; do
       curl -fsS -X POST "$base/v1/rerank" \
         -H 'Content-Type: application/json' \
         -d "{\"model\":\"$model\",\"query\":\"Rust ownership borrow checker memory safety\",\"documents\":[\"sourdough banana recipe\",\"Rust ownership and the borrow checker prevent memory safety bugs\"],\"top_n\":2}"
     done
   done
   ```

   Expected result: HTTP 200; top-level `results`/`data`/`rankings`; each item
   has `index`/`document_index` plus `score`/`relevance_score`/`rerank_score`;
   the Rust document should score higher than the unrelated document. If score
   direction is inverted, both `mempal` and `verbatim` will rank incorrectly.
7. Run a longer-shape rerank smoke close to real clients:
   - `mempal`: top-k near 50, because it sends top candidates' content directly
     and does not do reranker token truncation before the endpoint call.
   - `verbatim`: up to 50 candidates with documents truncated to about 8,000
     chars each; confirm `/v1/models` capability discovery reports enough
     context and that any retry after a context-limit error shrinks the request
     rather than failing.
8. Run downstream smokes:
   - `mempal` search or smoke-test with reranker enabled; verify no fallback
     warning and that relevant documents move upward.
   - `verbatim` retrieve with reranker enabled; verify reranker diagnostics show
     `status=succeeded` and capability discovery sees `max_context_tokens=40960`.
9. Run concurrency/timeout smoke:
   - several concurrent `/v1/rerank` requests through `18003` or `18009`;
   - confirm no unexpected `429`, context errors, or cold-start/JIT latency above
     the effective client timeout (`mempal` local config currently 30s;
     Verbatim local config currently 1800s, though repository defaults are
     shorter).
10. Run qualitative A/B comparisons against current Qwen3 reranker on real
   `mempal` and `verbatim` queries. Do not accept the change based only on a
   synthetic two-document curl probe.

## Possible implementation routes if vLLM direct load fails

1. **Patch a local Querit snapshot for vLLM generic sequence classification.**
   Investigate whether adding a local wrapper with `score = head` and suitable
   `auto_map` allows `AutoModelForSequenceClassification` to expose the trained
   head to vLLM. This must be verified by checking actual loaded scores, not just
   startup.
2. **Write a small dedicated Transformers rerank service.**
   Serve `/v1/models` and `/v1/rerank` on port `18013`, manually load Querit
   remote code, and preserve the two aliases. This avoids vLLM registry issues
   but adds maintenance and throughput risk.
3. **Wait for/port native vLLM support for Querit.**
   Best long-term option if Querit becomes operationally important.

## Decision recorded

As of this diary, do not deploy Querit-4B. Keep this note as reference for a
future canary if the reranker memory/quality trade-off becomes worth testing.
