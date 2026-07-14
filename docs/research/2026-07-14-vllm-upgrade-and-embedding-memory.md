# vLLM upgrade and Qwen3 embedding memory profile research

- **Date:** 2026-07-14
- **Last updated:** 2026-07-14T08:44:18-07:00
- **Question:** Can the reliability-critical `Qwen/Qwen3-Embedding-8B` service safely move from a 40,960-token / 5,820 MiB KV / 24 GiB container profile to a 32,768-token / 4,800 MiB KV / 20 GiB source contract, and should the service move to raw vLLM v0.25.x now?
- **Scope:** Source and read-only evidence only. No service, Docker, systemd, deployment, or model-runtime mutation was performed.
- **Decision status:** Commit the smaller source profile; defer live activation to a maintenance-window canary. Keep the pinned v0.24.0 image for now while preparing a qualified v0.25.x route. This is a deferral, not a decision to never upgrade.

An existing ignored draft contains a longer incident chronology. This tracked note intentionally preserves only the aggregate non-secret facts needed for the service decision; the historical draft was not modified.

## Facts

### Current source contract

At the start of this research, `systemd/vllm-embedding.service` specified:

- `Qwen/Qwen3-Embedding-8B` with aliases `qwen3-embedding-8b` and `Qwen/Qwen3-Embedding-8B`;
- built-in embedding conversion, BF16 model semantics, and the model's full 4,096-dimensional output;
- `--max-model-len 40960`;
- `--max-num-batched-tokens 8192` and `--max-num-seqs 64`;
- explicit `--kv-cache-memory-bytes 5820M`;
- Docker `--memory 24g` and `--memory-swap 24g`;
- post-start cgroup enforcement at `24G`;
- the existing highest-priority embedding OOM intent and no quantization.

The recorded validated baseline is 5,820 MiB for 41,376 KV tokens. That startup result applies to the currently pinned vLLM v0.24.0 image. It is the basis for projection, not proof of a future 4,800 MiB startup.

### Aggregate live evidence captured before this source-only change

On 2026-07-14 the existing embedding container was active under the old 24 GiB profile:

- cgroup `memory.current` was about 3.514 GiB;
- cgroup `memory.peak` was 16,870,580,224 bytes, or about 15.712 GiB;
- `memory.swap.current` was zero;
- recent GPU KV-cache usage was 0.0% at sampled idle points;
- recent prefix-cache hit rate was approximately 0% to 3.4% while embedding traffic was active.

These are aggregate resource metrics only. No prompts, request bodies, model outputs, credentials, or private logs are copied here. The low KV usage and low prefix-hit ratio do not prove that KV can be arbitrarily reduced; they do show no sampled evidence that a larger cache was improving hit rate or preventing capacity pressure.

### Shared GB10 source caps

The current ordinary source caps after the memory-guardian work are:

- AEON text: 69 GiB;
- Qwen3 embedding: 24 GiB before this change;
- Querit reranker: 18 GiB.

That is `69 + 24 + 18 = 111 GiB`. Older unit comments referring to `64 + 24 + 24 = 112 GiB` are stale because text is now 69 GiB and the active Querit profile is 18 GiB.

### vLLM v0.25.x evidence

1. Upstream vLLM tag `v0.25.0` points to commit [`702f4814fe54fabff350d43cb753ae3e47c0c276`](https://github.com/vllm-project/vllm/commit/702f4814fe54fabff350d43cb753ae3e47c0c276). Tag `v0.25.1` points to `752a3a504485790a2e8491cacbb35c137339ad34`.
2. The candidate [`r0b0tlab/vllm-v0250-cu130-sm121`](https://github.com/r0b0tlab/vllm-v0250-cu130-sm121) Dockerfile checks out that exact upstream v0.25.0 commit. It does not apply the memory fixes below, so it is a raw release build rather than a GB10 memory-patched build.
3. Upstream issue [#44175](https://github.com/vllm-project/vllm/issues/44175) reports linear host RSS growth under sustained V1 classification load.
4. Fix PR [#44490](https://github.com/vllm-project/vllm/pull/44490), merged on 2026-07-07, identifies an undrained `new_block_ids` list. Its scope explicitly includes standard full-attention models such as Qwen. The v0.25.0 and v0.25.1 tag histories diverge before the merge commit, so neither release contains that fix.
5. Pooling uses the shared V1 GPU model-runner path; embeddings are not outside the affected engine family merely because they use a pooling endpoint.
6. CUDA-graph startup-memory PR [#48483](https://github.com/vllm-project/vllm/pull/48483) merged to main on 2026-07-13 as commit [`f89989106fafc9c82b0025609065b5b0c1d43435`](https://github.com/vllm-project/vllm/commit/f89989106fafc9c82b0025609065b5b0c1d43435). It is not in v0.25.1. The PR changed only `vllm/v1/worker/gpu_model_runner.py`; its author recorded no corresponding test or test result.
7. Other v0.25-era memory-related PRs do not close these risks:
   - [#47483](https://github.com/vllm-project/vllm/pull/47483) frees Model Runner V2 model references on shutdown;
   - [#46746](https://github.com/vllm-project/vllm/pull/46746) bounds Model Runner V2 memory for large logprobs requests;
   - [#47010](https://github.com/vllm-project/vllm/pull/47010) prevents image decompression-bomb OOM denial of service.
   None establishes that the V1 pooling `new_block_ids` growth or large CUDA-graph startup over-allocation is fixed in v0.25.0/0.25.1.
8. The candidate README records image digest `sha256:a13c9964937f398b66d4a7e4fb8f80be8a60327052ca50bc8fbc2ce40c36beae`, while a read-only GHCR manifest lookup on 2026-07-14 returned `sha256:2d144fafe3f330fa17fa1facf4f589eee49b75bdf539ac69d1fe002b5b5bb0a5` for the named immutable-looking tag. No public SBOM, signature, or provenance attestation was discoverable in the repository tree, OCI referrers endpoint, or related signature/attestation tags. This is a provenance gap, not proof that no private build records exist.

### Alternative server evidence

- Hugging Face Text Embeddings Inference (TEI) lists native Qwen3 embedding support and an **experimental** SM121/GB10 image, not a stable production-qualified path. Issue [TEI #845](https://github.com/huggingface/text-embeddings-inference/issues/845) reproduces Qwen3-Embedding-8B all-NaN vectors from FP16 overflow and explains that native BF16 support is the required fix, targeted for a later release. The current audited route therefore cannot preserve the existing BF16 embedding contract safely.
- TEI's published reranker matrix does not establish support for Qwen3-Reranker or Querit. The open Qwen3 reranker request [#643](https://github.com/huggingface/text-embeddings-inference/issues/643) confirms that native embedding support must not be generalized into reranker compatibility.
- Infinity issues [#598](https://github.com/michaelfeil/infinity/issues/598), [#611](https://github.com/michaelfeil/infinity/issues/611), and [#642](https://github.com/michaelfeil/infinity/issues/642) leave model/version and exact post-processing quality concerns. Infinity is not a source-grounded drop-in replacement for this stack.
- SGLang evaluation remains pending and is recorded as an unknown below rather than assumed unsuitable.

## Calculations and inferences

All capacity projections scale from the validated baseline:

```text
projected tokens = candidate MiB × 41,376 tokens / 5,820 MiB
```

| Explicit KV budget | Projected capacity | Margin over 32,768 | Decision |
|---:|---:|---:|---|
| 4,610 MiB | 32,773.77 tokens | 5.77 tokens / 0.0176% | Reject: effectively no margin |
| 4,800 MiB | 34,124.54 tokens | 1,356.54 tokens / 4.1398% | Select for source contract |
| 4,864 MiB | 34,579.53 tokens | 1,811.53 tokens / 5.5284% | Viable but retains 64 MiB more UMA pressure |

The selected 4,800 MiB profile:

- reduces the explicit KV allocation by `5,820 - 4,800 = 1,020 MiB` (17.526%);
- projects at least a 4% token-capacity margin above the 32,768-token contract;
- preserves BF16 weights, 4,096 output dimensions, aliases, `max-num-batched-tokens=8192`, and `max-num-seqs=64`;
- does not use quantization or change vector-quality semantics.

The proposed 20 GiB hard cap has:

- 4.288 GiB of raw distance above the old measured 15.712 GiB cgroup peak;
- approximately 5.284 GiB of projected distance if the 1,020 MiB smaller explicit KV allocation reduces peak memory one-for-one.

The second value is an inference and must not be reported as a measured production peak. Docker `--memory 20g` is a hard ceiling, not reserved memory. Lowering the cap alone does not free UMA; the expected headroom improvement comes mainly from the 1,020 MiB smaller explicit KV allocation.

The target ordinary cap arithmetic is:

```text
69 GiB text + 20 GiB embedding + 18 GiB Querit = 107 GiB
121.6 GiB host - 107 GiB caps = about 14.6 GiB nominal cap headroom
```

Caps are safety ceilings and do not describe actual simultaneous residency, especially on unified memory. They are still useful for reconciling source policy and preventing stale 111/112 GiB planning assumptions.

## Decision

1. Set the tracked embedding contract to exactly 32,768 tokens, 4,800 MiB explicit KV, Docker memory/swap 20 GiB, and post-start helper cap 20 GiB.
2. Keep embedding as the highest-priority service. Do not change model, BF16 semantics, 4,096 dimensions, aliases, batching, sequence count, eager mode, or quantization.
3. Make no live change in this task. The 4,800 MiB capacity is **projected, not production-verified** until a real restart reports at least 32,768 KV tokens and passes quality/capacity checks.
4. Do not promote raw vLLM v0.25.0 or v0.25.1 yet. A future route should be either:
   - a formal upstream release containing both #44490 and #48483; or
   - a v0.25.1-derived, digest-pinned internal image with both fixes backported, plus a public/internal SBOM, signature, build provenance, and GB10 canary evidence.
5. Revisit the upgrade when that evidence exists; the current decision explicitly leaves the upgrade path open.

## Future activation canary

Activation belongs in an approved maintenance window and must use the repository's full-stack memory-profile restart procedure rather than an ad hoc live edit.

Before any stop/restart, snapshot:

- embedding `ActiveState`, `MainPID`, and `NRestarts`;
- text and both reranker states, PIDs, and restart counts;
- embedding cgroup `memory.current`, `memory.peak`, `memory.max`, and swap state;
- the old startup capacity line and current image digest;
- a fixed synthetic embedding quality canary, including HTTP success, finite values, exact 4,096 dimensions, and comparison to the accepted baseline tolerance.

After activation, require all of the following:

1. the embedding unit becomes ready without restart looping or OOM/cap events;
2. startup logs report KV capacity **at least 32,768 tokens** under 4,800 MiB;
3. Docker `memory.max` is 20 GiB and swap remains disabled/aligned by both Docker and the helper;
4. the fixed embedding canary preserves aliases, 4,096 dimensions, finite vectors, and accepted quality similarity;
5. current/peak cgroup memory and host UMA/swap remain healthy under a bounded representative embedding request;
6. text and reranker state/PID/restart-count snapshots show no unintended change.

Do not call the new profile production-verified until these receipts are captured.

## Rollback criteria and procedure

Rollback immediately if startup fails, reported KV capacity is below 32,768, the endpoint fails or changes dimensions/quality beyond tolerance, the 20 GiB cap is hit, swap grows unexpectedly, or text/reranker invariants fail.

The rollback source profile is exactly:

```text
max-model-len=40960
kv-cache-memory-bytes=5820M
Docker memory/memory-swap=24g
post-start helper cap=24G
```

Restore that reviewed unit, reinstall/reload it, and repeat the same full-stack maintenance procedure and snapshots. Preserve failed-canary logs and aggregate metrics; do not overwrite them with a second attempt.

## Unknowns

- Actual vLLM startup capacity at 4,800 MiB on the pinned GB10 image is unknown until a live maintenance-window restart.
- The post-change representative cgroup peak and true UMA residency reduction are unknown; proportional KV and peak reductions are projections.
- Whether #44490 alone fully explains this host's historical memory steps is unknown; its upstream bug class is relevant, but no private workload replay has been performed on a fixed image.
- Whether a future formal release will contain both #44490 and #48483, with adequate tests and SM121 artifacts, is unknown.
- SGLang embedding/reranker contract fidelity, SM121 artifact quality, memory profile, and OpenAI-compatible alias/dimension behavior remain pending research.

## Running log

- **2026-07-14T08:44:18-07:00:** Recorded current source/live aggregate evidence, proportional KV sizing, 20 GiB cap reasoning, v0.25.x fix/provenance gaps, and source-first decision. SGLang remains pending.
- **Future entry:** Add SGLang source/artifact/quality findings without rewriting the facts above; record new evidence and decision deltas with a new timestamp.
