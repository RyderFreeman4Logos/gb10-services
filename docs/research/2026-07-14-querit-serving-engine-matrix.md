# Querit-4B serving-engine matrix and replay decision — 2026-07-14

## Scope and bounded decision

This is a source-only audit: no runtime change, GB10 access, or live-state proof.

**Decision:** retain the custom Transformers process, the only audited path that
loads Querit's learned classifier. Do **not** call its scores publisher-equivalent
until deterministic dual-baseline replay resolves the input contract. The other
engines below are not drop-in replacements.

## Pinned artifact and exact score

The deployed artifact is `Querit/Querit-4B` revision
`7b796de30ad8dc772d6c46c75659c1341283a665` (Hub timestamp
2026-06-20T03:53:30Z). Its package is internally inconsistent:

- `config.architectures = ["MLQwen3Model"]`, but the shipped source defines
  `QueritModel` and supplies no `auto_map`;
- `QueritModel` subclasses `Qwen3ForCausalLM`, uses the base model's hidden
  states, and adds a learned `Linear(2560, 2)` classifier with bias;
- the forward pass selects `last_hidden_state[:, -1, :]`, calculates two logits,
  applies `softmax`, and returns `score = -p0 + p1` in `[-1, 1]`.

The checkpoint therefore has a learned two-class relevance head. Its score is
**not** a sigmoid, cosine or normalized-embedding score, and is **not** the
probability of a generated `yes` token or any other yes/no LM-token formula.

## Local adapter facts, not publisher claims

These facts come from `scripts/querit_openai_rerank_server.py` and
`systemd/querit-4b-reranker.service` at source HEAD
`fe9b7d036ef62a1cf9b153a8140b5818df7014e4`:

- the adapter manually imports local `modeling_querit_4b.py` and calls
  `QueritModel.from_pretrained`; `trust_remote_code=True` does not perform class
  resolution here;
- tokenizer, config, and model loads use `local_files_only=True`; the unit also
  sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`;
- it loads BF16, retains the tied LM head only while loading, then discards it;
- its configured limits are 40,960 tokens and eight pairs per inference batch;
- a global lock serializes request inference, while one request can still be
  internally batched in groups of eight;
- it accepts the model's `score`; its fallback reproduces
  `softmax([z0,z1]) · [-1,1]`;
- any non-finite score fails the complete request with a generic HTTP 500;
- descending sort is stable, so exact ties preserve input order.

None of those implementation properties is attributed to the publisher.

## Publisher contract and unresolved serializer

The publisher paper (arXiv:2606.19037v1, submitted 2026-06-17) states the
conceptual input as `X = [Inst; q; t; c; [CLS]]`, says the final `[CLS]` token is
the aggregation position, and defines
`p = softmax(W h_n + b) = [p0,p1]` and `s(q,d) = -p0 + p1`.
The pinned tokenizer contains an added `[CLS]` token.

The paper does **not** publish an executable inference serializer. Exact
instruction text, title/content delimiters, missing-title treatment, escaping,
and training-time string bytes remain unresolved; they must not be invented.

### Source-grounded mismatches and replay hypotheses

| Observation established from source | Consequence that remains a numerical hypothesis |
|---|---|
| The adapter renders a Qwen3 generated-reranker yes/no-style chat prompt ending at `<|im_start|>assistant\n`; it does not append `[CLS]`. | Score/ranking drift relative to the publisher contract, including language- or length-dependent drift. |
| The model reads physical position `-1`, while the adapter explicitly right-pads mixed-length batches. | A short example may change score when batched with longer examples because its physical last position is padding. |
| The adapter enables truncation at 40,960; the tokenizer config supplies no truncation-side override, so the audited path right-truncates. | Near-limit inputs may lose a terminal scoring anchor and change scores or order. |
| Model config says 40,960 positions; tokenizer metadata says 131,072 and the card says 128K. | Behavior beyond 40,960 is unsupported by this service contract and unmeasured. |

The mechanics above are facts. Their magnitude, affected corpus share, and any
quality conclusion are pending replay.

## Serving-engine matrix

| Path pinned in this audit | Load/API semantics versus exact Querit head | aarch64 / SM121 evidence | Decision |
|---|---|---|---|
| **Current Transformers adapter** | Manual import loads the learned two-logit head and returns `p1-p0`; its custom `/v1/rerank` serializer still conflicts with the paper. | The unit pins an AEON image digest, but no image or host ran in this audit. | **Retain as head-faithful, not yet publisher-equivalent.** |
| **SentenceTransformers 5.6.0** (`9c73df3143e97598938a1640d737d3f0f11878e5`) | `CrossEncoder` treats only architecture names ending in `ForCausalLM` as generative. `MLQwen3Model` enters `AutoModelForSequenceClassification`, whose CrossEncoder path may force one label; it cannot resolve the custom class and preserve two-class `p1-p0`. A custom module is another adapter. | No separately audited serving artifact; it inherits Python/PyTorch availability. | **No direct load path.** |
| **vLLM 0.25.1** (`752a3a504485790a2e8491cacbb35c137339ad34`) | Scoring APIs require `num_labels == 1`. Documented official Qwen3 reranker conversion derives a scalar classifier from LM-head `no`/`yes` token rows; the sequence-classifier adapter expects a top-level `.score`. Querit's package name, checkpoint head mapping, two logits, and `p1-p0` formula do not satisfy that contract. `trust_remote_code` cannot repair the absent `auto_map`. | The official tag has a multiarch image including arm64, but audited image metadata advertised compute capability 12.0, not 12.1. An arm64 manifest is not proof of GB10 SM121 kernels. | **No native Querit support; `--hf-overrides` would implement the wrong head.** |
| **SGLang 0.5.15** (`f63458b5beaceabbd9d749b9fc956370e1b649e6`) | Its Qwen3 route computes yes/no `p_yes/(p_yes+p_no)`; the cross-encoder path and Transformers fallback do not provide Querit's class/head mapping. | Arm64 CUDA artifacts exist, but #29317 reports missing aarch64 SM121 `sgl_kernel`; workaround #30562 was open on 2026-07-14. | **No native support; score semantics differ.** |
| **TEI 1.9.3** (`06670157fb6c1523482219bdb2d1660277d38088`) and audited main `fc071b1cb6e1b091b67f20868de7c5982aa7d4d0` | Its Qwen3 loader errors: ``classifier` model type is not supported for Qwen3``; no Python route loads this head. | Main documents experimental `121-1.9` and arm64 source build with `CUDA_COMPUTE_CAP=121`. A 2026-07-14 unauthenticated GHCR lookup returned 404 for that tag while other 1.9 tags resolved; this does not prove permanent absence. | **No-go; source-build docs are not a verified artifact.** |

## SM121 vLLM fork boundary

The audited fork is
[`r0b0tlab/vllm-v0250-cu130-sm121`](https://github.com/r0b0tlab/vllm-v0250-cu130-sm121)
at head `d1b50dba0bb311c78b0179609b0c19d3bf0a933c` (2026-07-13).
Its Dockerfile builds upstream vLLM **v0.25.0** commit
`702f4814fe54fabff350d43cb753ae3e47c0c276` and targets ARM64/SM121. Its
hardware patches do not register `MLQwen3Model`, map `head.{weight,bias}`, append
`[CLS]`, or implement `p1-p0`; hardware compatibility and model compatibility
are separate questions.

The strict boundary for this fork is **no original fixes**. Only provenance-
preserving ports of these already-merged upstream changes are in scope:

1. [vLLM #44490](https://github.com/vllm-project/vllm/pull/44490), merged
   2026-07-07 as `b4cfbc24d33ca17bc764a75ffe749654654521c1`, fixes unbounded
   host growth from undrained `new_block_ids`. The final diff changes three
   files: `kv_cache_coordinator.py`, `sched/scheduler.py`, and
   `single_type_kv_cache_manager.py`; stale one-file wording is not authoritative.
2. [vLLM #48483](https://github.com/vllm-project/vllm/pull/48483), merged
   2026-07-13 as `1be6e937b2b49bae652370d80294f6171bd7b981`, bounds dummy
   KV blocks for large CUDA-graph profiling to per-sequence rather than
   effectively per-token allocation. It changes only `gpu_model_runner.py` and
   records no test result.

Neither change touches model registration, checkpoint mapping, serialization,
classifier post-processing, or rerank endpoint semantics. **Neither makes
Querit compatible.** A Querit vLLM adapter would be a separately reviewed
original implementation, outside this memory-fix fork.

## Deterministic dual-baseline replay

### Frozen inputs

Freeze the model revision, runtime image digest, BF16 dtype, one target GPU,
tokenizer/config/model-source hashes, Transformers version, and 40,960-token
limit. Maintain two named tracks:

1. **Current-contract baseline:** byte-identical current prompt,
   right-padding/truncation, batching, and adapter output.
2. **Publisher-semantics candidate:** terminal `[CLS]`, last real token as the
   aggregation position, two logits, softmax, and `p1-p0`. Keep serializer
   variants explicitly named because the publisher did not release exact bytes.

### Minimum replay corpus

- positive and irrelevant English pairs;
- Chinese and cross-lingual pairs;
- duplicate documents and intended exact ties;
- `[CLS]`, `<|im_start|>`, yes/no instruction, and delimiter injection;
- Unicode, control characters, combining characters, and emoji;
- mixed short/long pairs in one batch;
- batch size 1 versus 8 and every input permutation for the walking skeleton;
- token counts immediately below, at, and above 40,960;
- query, document, aggregate-document, document-count, body-size, `top_n`, alias,
  and empty-input API boundaries.

### Evidence per pair

Record rendered prompt bytes and hash, token IDs, attention mask, non-padding
length, final real and physical tokens, truncation event, raw `z0/z1`, `p0/p1`,
final score, input index, and sorted index. Also record consumed checkpoint keys
and prove `head.weight` and `head.bias` were loaded rather than initialized.

### Gates

- exact token-ID parity for each declared serializer contract;
- publisher track's final non-padding token is `[CLS]` and is the selected hidden
  position;
- all expected classifier weights are consumed, with no missing/reinitialized
  head;
- finite scores in `[-1,1]` and independent recomputation exactly as `p1-p0`;
- batch-size and permutation invariance within a predeclared BF16 tolerance;
- stable ties, `top_n`, aliases, error envelope, and result indexes;
- tolerance derived before comparison from repeated same-runtime reference runs;
- candidate engine exposes logits or equivalent evidence sufficient to recompute
  the score independently.

If the current track fails batch/permutation invariance, treat that as a baseline
correctness defect before evaluating another engine.

## Stale-document corrections to make separately

1. Replace “single-inference Transformers adapter” with “globally serialized,
   request-batched up to eight pairs.”
2. Do not let “aligned with Qwen3-Reranker-style judges” imply publisher input
   compatibility; the current yes/no prompt omits terminal `[CLS]`.
3. Label `qwen3-reranker-8b` names as compatibility aliases, not model identity.
4. Qualify “OpenAI-compatible rerank”: rerank is a compatibility endpoint, not an
   official OpenAI API.
5. Separate TEI main-branch `121-1.9` documentation, source-build instructions,
   and the unverified/missing-on-lookup GHCR artifact.
6. Never equate a generic arm64 image with SM121 support.
7. Describe #44490 from its final three-file diff, not its stale PR-body wording.
8. Record the 40,960 model-config / 131,072 tokenizer / “128K” card discrepancy.
9. Record the `MLQwen3Model` / `QueritModel` / absent-`auto_map` packaging mismatch.

## Primary-source ledger

All remote sources were read-only and checked on 2026-07-14.

- Publisher: [arXiv abstract](https://arxiv.org/abs/2606.19037v1) and
  [PDF](https://arxiv.org/pdf/2606.19037v1), submitted 2026-06-17.
- Model revision: [pinned commit](https://huggingface.co/Querit/Querit-4B/commit/7b796de30ad8dc772d6c46c75659c1341283a665),
  [config](https://huggingface.co/Querit/Querit-4B/blob/7b796de30ad8dc772d6c46c75659c1341283a665/config.json),
  [model source](https://huggingface.co/Querit/Querit-4B/blob/7b796de30ad8dc772d6c46c75659c1341283a665/modeling_querit_4b.py), and
  [tokenizer config](https://huggingface.co/Querit/Querit-4B/blob/7b796de30ad8dc772d6c46c75659c1341283a665/tokenizer_config.json).
- SentenceTransformers 5.6.0: [CrossEncoder](https://github.com/huggingface/sentence-transformers/blob/9c73df3143e97598938a1640d737d3f0f11878e5/sentence_transformers/cross_encoder/model.py) and
  [Transformer module](https://github.com/huggingface/sentence-transformers/blob/9c73df3143e97598938a1640d737d3f0f11878e5/sentence_transformers/base/modules/transformer.py).
- vLLM 0.25.1: [scoring contract](https://github.com/vllm-project/vllm/blob/752a3a504485790a2e8491cacbb35c137339ad34/docs/models/pooling_models/scoring.md) and
  [sequence-classifier adapter](https://github.com/vllm-project/vllm/blob/752a3a504485790a2e8491cacbb35c137339ad34/vllm/model_executor/models/adapters.py).
- SGLang: [v0.5.15 rerank implementation](https://github.com/sgl-project/sglang/blob/f63458b5beaceabbd9d749b9fc956370e1b649e6/python/sglang/srt/entrypoints/openai/serving_rerank.py),
  [SM121 issue #29317](https://github.com/sgl-project/sglang/issues/29317), and
  [workaround PR #30562](https://github.com/sgl-project/sglang/pull/30562).
- TEI: [v1.9.3](https://github.com/huggingface/text-embeddings-inference/tree/06670157fb6c1523482219bdb2d1660277d38088),
  [Qwen3 loader at audited main](https://github.com/huggingface/text-embeddings-inference/blob/fc071b1cb6e1b091b67f20868de7c5982aa7d4d0/backends/candle/src/models/qwen3.rs), and
  [audited main README](https://github.com/huggingface/text-embeddings-inference/blob/fc071b1cb6e1b091b67f20868de7c5982aa7d4d0/README.md).
- SM121 fork: [Dockerfile at audited head](https://github.com/r0b0tlab/vllm-v0250-cu130-sm121/blob/d1b50dba0bb311c78b0179609b0c19d3bf0a933c/docker/Dockerfile) and the two merged vLLM PRs above.

## Explicit uncertainty

No exact publisher serializer was found. No alternative engine was loaded with the
checkpoint. No arm64/SM121 artifact was executed. No service, container, GPU, or
live GB10 endpoint was inspected. This note makes **no claim of live GB10
verification** and records the current adapter's output quality as unresolved
pending the deterministic replay above.
