# Querit vLLM Migration — Research and Implementation Plan

**Date:** 2026-07-16
**Status:** Source-controlled canary ready; live probing pending

## Background

The current Querit reranker runs as a Python transformers server (`querit_openai_rerank_server.py`) on port 18013. Under 16x concurrent 32k-token requests, it returns 503/broken pipe for 83% of requests because `INFERENCE_SEMAPHORE(2)` serializes most forward passes. The goal is to replace it with vLLM's native pooling/score engine for proper token-based dynamic batching, chunked prefill, and request scheduling.

### First principle

GB10 local Querit-4B and cloud DeepInfra Qwen3-Reranker-8B must ultimately be transparently interchangeable. Developers program against the public DeepInfra contract for `Qwen/Qwen3-Reranker-8B`. Native vLLM scoring availability is necessary but is not, by itself, evidence of wire compatibility. This pairs with #172 (upstream failover, merged) for eventual automatic local↔cloud switching.

## Research Findings

### AEON v0.25.1 support (verified)

- Image tag `2026-07-16-v0.25.1` resolves to repository digest `sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d` and installed distribution `0.25.1+aeon.sm121a.dflash` (AEON build revision `afd9b8b7faa6fbe2ceab13a14638e97dc5ca718f`; upstream vLLM `752a3a504485790a2e8491cacbb35c137339ad34`).
- OCI inspection reports the stale inherited label `org.opencontainers.image.revision=06e292d0ce7e0ddc4f84bd200c3bdf55c7875eb7`, not the 0.25.1 build revision. Treat the repository digest as image authority; AEON's later README/label-card correction is revision `3374b528b68369189190852314c759305402017e`.
- Release delta: MRv2 `lm_head` sharing is ported across DFlash, Eagle, and DSpark; vLLM #47888 permits startup without FFmpeg when optional torchcodec is absent; #48330 guards mixed-dtype FlashInfer allreduce/RMSNorm/quant fusion under TP>1. AEON documents TP>1 as unvalidated despite the DSpark/TP=2-ready source fixes.
- Rollback/superseded identity retained on GB10: `2026-07-14-v0.25.0`, `sha256:18c09e6b80141a530285160781f7fa720a78ef91143b3c15a65a8c9641b44e55`.
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
- The converter attests the source snapshot against the pinned source ledger before
  writing. It changes `metadata.total_size` exactly from `8,043,564,036` to
  `8,043,558,914` bytes (delta `-5,122`), then seals source/output ledgers and
  hashes in `querit-vllm-artifact-manifest.json`. The validator rejects stale
  totals, missing `score.*` keys, retained `head.*` keys, and any tensor that is
  not consumed exactly once by the index.
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

### DeepInfra wire compatibility adapter

vLLM 0.25 exposes native `/v1/score`, but that endpoint is not the public
DeepInfra contract. The raw backend is loopback-only at `127.0.0.1:18015` for
the local adapter; it has no Tailnet or wildcard publication. A separately
tested adapter owns the DeepInfra-native canary port `100.105.4.92:18014` and
accepts only the version-pinned target
`/v1/inference/Qwen/Qwen3-Reranker-8B?version=5fa94080caafeaa45a15d11f969d7978e087a3db`.
It requires canonical equal-length `queries[]` and `documents[]`, sends the
corresponding positional request to `/v1/score`, validates every response
index and finite Tanh score, transforms `[-1, 1]` to the documented public
`[0, 1]` domain, and returns `scores[]`, `input_tokens`, and `request_id`.
Cloud and local experiment requests therefore differ only in base URL and
authorization; their POST target and body bytes are identical.

This source contract removes the known wire mismatch. It does not provide live
model, numerical, or paid-cloud evidence. No production cutover, guard route
change, or retirement of the Transformers service is permitted on source
evidence alone.

### Memory budget

Current: text 41.4G + emb 20.3G + RR 17.6G = 79.3G / 121.6G, MemAvail 26.6G
vLLM Querit (replacing RR): ~8G weights + 4.8G KV + overhead ≈ 15-18G (same or less)

The aggregate-only candidate receipt observed a minimum `MemAvailable` of
`57,246,636 KiB` while legacy Querit, embedding, and Guard remained online.
The refreshed legacy allocator initially exposed more than 87 GiB, but live
traffic returned the host to `53,027,788 KiB` before the next 60-second gate.
That high-water regrowth repeatedly defeated the earlier 30 GiB reserve even
though PSI remained zero and `pswpout` did not increase. The source-controlled
profile therefore uses the operator-authorized 20 GiB reserve
(`20,971,520 KiB`) and retains the 2 GiB uncertainty margin (`2,097,152 KiB`).
The observed envelope permits at most `34,177,964 KiB` (about 32.595 GiB) for
candidate startup. The unchanged explicit 0.17 GPU-utilization profile has a
conservative `22,817,014 KiB` (21.76 GiB) startup budget and now derives a
`45,885,686 KiB` owner admission floor. The model, BF16 precision, 32K context,
score semantics, and candidate memory ceiling are unchanged.

Before any mutation, the owner parses the sealed candidate unit and rejects
duplicate, missing, or mismatched vLLM profile options. It derives the
admission floor from that same profile authority; the subsequent re-attestation
must freshly satisfy the memory and PSI contract, retain the exact swap
topology, and observe no additional swap-out before install and activation.
This is source-level feasibility only: a fresh live memory gate and an actual
vLLM startup still have to prove the candidate.

The first live artifact publication after admission repair reached the
converter and then failed because the owner invoked `/usr/bin/python3`, while
the GB10 host interpreter intentionally had neither `torch` nor `safetensors`.
The already-pinned canary image contained `torch 2.11.0+cu130` and
`safetensors 0.7.0`. The owner therefore runs conversion in that same immutable
image with pulls and networking disabled, a read-only container root and
source mounts, equal 12 GiB Docker memory/swap caps for the disposable
conversion process, and only the disposable copied snapshot mounted writable.
This conversion-container limit is not the vLLM runtime no-swap attestation.
Every tracked production/canary vLLM backend instead has one direct
`--swap-space 0` pair and equal Docker memory/swap intent; before readiness the
canonical helper binds full CID/PID/`StartedAt`, `/proc` starttime and exact
Docker scope, scope inode/population, then proves exact `memory.max`, zero
`memory.swap.max`, and zero activation-time `memory.swap.current` across an
unchanged re-read. Querit deployment owns source-identical helper installation,
and canary/production transactions verify preflight, post-start, and rollback
without stopping embedding. This avoids mutable host-venv dependencies without
changing the checkpoint conversion or scoring contract.

The next live install also established a user-systemd detail that the original
mock did not model: after a persistent unit file appears, `systemctl show`
reports that unit as loaded/static even while the exact runtime-mask symlink to
`/dev/null` still exists. Runtime-mask ownership is therefore attested from the
symlink's no-follow identity and candidate quiescence. Once the owner removes
that exact symlink, both disabled and static are valid non-enabled states; final
rollback still has to reproduce the complete original unit metadata and mask
layout.

The tracked lifecycle transaction verifies the converted artifact manifest and
snapshots embedding, both current/legacy reranker units, Guard, and text. If
headroom is low it may stop text only, then remeasures. It starts the backend
and adapter with `stop then start` operations only, warms the public wire
endpoint plus native peak-context and batching paths, then checks OOM counters,
headroom, and unit/container/PID identity. It restores text plus canary
pre-state after every failure or termination signal. It never stops embedding,
either production reranker, or Guard, and the canary is never enrolled in the
guardian.

### v0.24 → v0.25.1 note

The original adapter document assumed v0.24.0. Commit `db45ede` moved the
operational contract to the previous AEON release; this source migration now
pins the verified 0.25.1 image identity without changing the adapter or model
parameters.

## Implementation Plan

### Phase 1: Checkpoint conversion (offline)
1. Copy pinned snapshot to `/models/querit-4b-vllm/`
2. Rewrite `model-00002-of-00002.safetensors`: `head.*` → `score.*` (Tanh conversion)
3. Update `config.json`: architectures, num_labels, head_dtype, activation
4. Update `model.safetensors.index.json` weight_map
5. Update `metadata.total_size` from `8,043,564,036` to `8,043,558,914`
   (exact delta `-5,122`)
6. Install verified Jinja template as `querit-rerank.jinja`
7. Generate and verify `querit-vllm-artifact-manifest.json`, including its
   pinned source ledger and output ledger hashes

### Phase 2: Smoke test (temporary port)
1. Install the two tracked canary units, adapter, lifecycle modules, and two
   `gb10_querit_canary_*.py` entry points. Do not enable either unit.
2. Run `gb10_querit_canary_lifecycle.py activate`. The activator alone may
   start the loopback-only raw vLLM backend and the DeepInfra-native adapter.
3. Require the public DeepInfra probe, a native 32,768-token `/score` peak
   allocation, and a 16-pair chunked-prefill probe. Reject any unit/container/PID
   identity change, OOM event, or insufficient post-warm host headroom.
4. Run the cache-safe endpoint equivalence experiment on pinned public data.
5. Run `gb10_querit_canary_lifecycle.py deactivate` to stop adapter then
   backend and restore any text state paused by the transaction.

### Source-closed canary deployment transaction

Do not copy files, unmask units, reload systemd, publish the artifact, or start
either canary unit by hand. The operator-facing owner is
`scripts/gb10_querit_canary_deploy.py`; it is the only path that can remove
its own or explicitly accepted exact candidate runtime masks. It makes a
private exact-HEAD bundle whose manifest
binds each tracked source path to its explicit target path, mode, size, and
SHA-256. The bundle includes the lifecycle/runtime/transaction/artifact and
adapter/equivalence modules, both wrappers and units,
`gb10_service_ready.sh`, the converter, and the pinned Jinja template.
Its explicit library mapping includes `scripts/querit_replay_trust.py` alongside
`scripts/querit_vllm_artifact.py`, `scripts/querit_checkpoint_convert.py`, and
`config/querit/querit-rerank.jinja`; no target is inferred from an executable
bit or a directory convention.

Run from a clean repository root. `SNAPSHOT` is a pinned source snapshot that
may be read but is never changed; the owner creates an owner-only disposable
copy, runs the committed converter and validator on that copy, and atomically
publishes it at `/home/obj/models/querit-4b-vllm`.

```bash
SNAPSHOT=/path/to/pinned/Querit-4B-snapshot

# Read-only source/host attestation, durable prestate receipt, then owner mask.
python3 scripts/gb10_querit_canary_deploy.py prepare --source-root "$PWD"

# Only for the exact two-unit runtime-mask recovery prestate:
python3 scripts/gb10_querit_canary_deploy.py prepare --source-root "$PWD" \
  --accept-runtime-mask-prestate

# Re-attest immediately before mutation; convert, publish, install, reload, and
# verify exact loaded unit bytes. Both candidate units remain disabled.
python3 scripts/gb10_querit_canary_deploy.py install \
  --source-root "$PWD" --source-snapshot "$SNAPSHOT"

# The explicit experiment mode durably owns an active text pause. It never
# restarts or kills text; canonical lifecycle activation starts backend then adapter.
python3 scripts/gb10_querit_canary_deploy.py activate \
  --source-root "$PWD" --pause-text

# Stop adapter then backend through the canonical lifecycle, restore text only
# if this transaction paused it, restore artifact/files/masks, and reload.
python3 scripts/gb10_querit_canary_deploy.py deactivate --source-root "$PWD"
```

`deploy --source-snapshot "$SNAPSHOT" --pause-text` performs the same three
phases as one command. `deactivate` and `rollback` are equivalent recovery
actions for a non-active partial receipt. A subsequent `prepare`/`deploy`
recovers a non-active receipt before it performs new work; an `active` receipt
requires the explicit `deactivate` command.

Before its first file, artifact, mask, daemon-reload, or lifecycle mutation,
the owner re-attests the clean source/bundle identity; exact target prestate;
text and immutable-neighbor service identities; candidate units, masks,
FragmentPath and drop-ins; listeners 18014/18015; candidate containers; sealed
artifact prestate; and memory/swap/PSI admission facts. Persistent masks,
runtime masks without the explicit ownership opt-in, enabled candidate units,
loaded-byte/path/drop-in drift, or pre-existing candidate listeners/containers
fail closed. Rollback stops only
the candidate through the lifecycle, proves candidate PIDs/listeners/containers
are absent, then restores the recorded artifact, files, runtime-mask prestate,
and systemd generation. A partial or failed rollback keeps an owner-only state
file and receipt for recovery; it never touches embedding, legacy Querit on
18013, the qwen reranker, Proxy, or guardian.

The former manual `install`/`daemon-reload` sequence is superseded by the
source-closed owner above. It deliberately cannot be used as a recovery
shortcut because it lacks the deployment receipt, artifact backup, and owned
runtime-mask record.

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
  --gpu-memory-utilization 0.17 \
  --kv-cache-memory-bytes 4800M \
  --kv-cache-dtype auto \
  --tensor-parallel-size 1 \
  --pipeline-parallel-size 1 \
  --swap-space 0 \
  --cpu-offload-gb 0 \
  --max-num-batched-tokens 1024 \
  --max-num-seqs 1 \
  --enable-chunked-prefill \
  --max-num-partial-prefills 1 \
  --max-long-partial-prefills 1 \
  --long-prefill-token-threshold 8192 \
  --enforce-eager \
  --chat-template /home/obj/models/querit-4b-vllm/querit-rerank.jinja
```

The tracked backend publishes container port 8000 exactly once, to
`127.0.0.1:18015`; it is loopback-only and has no direct Tailnet or wildcard
bind. This loopback publish is the adapter's backend path. The adapter publishes
the exact DeepInfra-native API only on Tailscale port 18014.
Docker bounds the backend to 18 GiB memory and swap with swappiness 0; both
systemd and the container use OOM score adjustment 500. The canary explicitly
sets the model dtype, context, GPU utilization, KV allocation/dtype,
tensor/pipeline parallelism, CPU swap/offload, batching, sequences, partial
prefills, and eager mode; it never relies on a vLLM 0.25.1 memory default.
The low-concurrency feasibility profile preserves the exact model, BF16
precision, 32,768-token context, artifact, and scoring contracts while using
`--gpu-memory-utilization 0.17`, `--max-num-batched-tokens 1024`, and
`--max-num-seqs 1`. `Restart=no` and the transaction-authorized
`ExecCondition` prevent retry loops or ad-hoc starts.

## Direct legacy/canary equivalence harness

`scripts/querit_legacy_canary_equivalence.py` is the sealed, direct comparison
for the production legacy Querit native API and the separately deployed canary
adapter. It uses only the committed MIRACL development subset
`data/reranker-equivalence/miracl-reranking-en-zh-dev.jsonl` (200 groups,
2,000 pairs, SHA-256
`2dd35e5a0ce1357ec8c6daaa4893809c220309058e858d62b0c9f1d8c68b954d`) and
the canonical plan artifact
`data/reranker-equivalence/querit-legacy-canary-equivalence-plan-v2.json`
(SHA-256 `18b2ed43c7ce352de048165c5f9497d8f583b2c894b2e8ddaa08f0e49c049e92`).
It is neither CodeSeek testing nor a cloud or paid-endpoint comparison.

First inspect the completely offline schedule. This performs no DNS, socket,
or credential initialization:

```bash
python3 scripts/querit_legacy_canary_equivalence.py --dry-run
```

An operator may then run the planned endpoint-specific experiment with explicit
placeholder URLs and an owner-only receipt location:

```bash
python3 scripts/querit_legacy_canary_equivalence.py \
  --legacy-url 'http://<legacy-host>:18013' \
  --candidate-url 'http://<canary-host>:18014' \
  --output "$HOME/.local/state/querit-equivalence/receipt.json"
```

The legacy request is native `POST /v1/rerank` with one query, ten ordered
documents, and `top_n=10`; sorted legacy results are reconstructed by original
document index. The candidate request is the adapter’s pinned
DeepInfra-compatible model/version path, with the query repeated ten times and
the same documents paired positionally. It deliberately omits `instruction`
and `service_tier`. Neither request uses Authorization handling. The sealed
schedule has no retries: the main native-contract run is 200 groups per
endpoint, while each warm concurrency of 1/2/4/8 has exactly 40 calibration
groups per endpoint. Cold first-score and excluded warmup attempts are reported
separately.

The receipt contains only hashes, corpus/schedule counts, aggregate ranking,
qrels, failure/Wilson, and latency evidence, fixed thresholds, and verdicts.
It never contains endpoint URLs, hostnames, paths, credentials, headers,
payloads, request/response bodies, query/document text, case identifiers, or
per-case hashes. Do not commit generated receipts or raw evidence. The runner
creates an output parent at mode `0700` and receipt file at mode `0600` through
an atomic fsync/rename flow.

Its separate verdicts mean: native API availability is each endpoint satisfying
its own contract; ranking behavior is approximate held-out rank agreement;
qrels quality is the paired non-inferiority bound; reliability and latency are
the fixed operational gates; behavioral usability requires all of those except
latency; and canary operational suitability additionally requires latency.
`wire_drop_in_compatibility` is permanently
`NOT_CLAIMED_CONTRACTS_DIFFER`, while `score_interchangeability` is permanently
`NOT_CLAIMED_EXPECTED_NONIDENTICAL`: legacy physical-last and candidate
last-real pooling intentionally differ. A `PASS` only supports this isolated,
endpoint-specific canary experiment; it is not wire replacement, score consumer
interchangeability, full-MIRACL equivalence, or production-cutover readiness.

Exit code `0` is only a complete canary-operational `PASS`; `2` is a complete
valid `FAIL`; and `3` denotes `INCONCLUSIVE` or transport/identity failure.
By explicit operator instruction, deployment and this direct endpoint run occur
before Tier-4 review; that review still occurs before any push, pull request,
or merge.
