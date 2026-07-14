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
4. Fix PR [#44490](https://github.com/vllm-project/vllm/pull/44490), merged on 2026-07-07, identifies an undrained `new_block_ids` list. Its scope explicitly includes standard full-attention models such as Qwen. GitHub records PR head [`f67a21788ae4f7578b5930c52d6c47831e556882`](https://github.com/vllm-project/vllm/commit/f67a21788ae4f7578b5930c52d6c47831e556882) and the main-branch merge/squash commit as [`b4cfbc24d33ca17bc764a75ffe749654654521c1`](https://github.com/vllm-project/vllm/commit/b4cfbc24d33ca17bc764a75ffe749654654521c1). The v0.25.0 and v0.25.1 tag histories diverge before the main-branch commit, so neither release contains that fix.
5. Pooling uses the shared V1 GPU model-runner path; embeddings are not outside the affected engine family merely because they use a pooling endpoint.
6. CUDA-graph startup-memory PR [#48483](https://github.com/vllm-project/vllm/pull/48483) merged on 2026-07-13. Its PR head is [`f89989106fafc9c82b0025609065b5b0c1d43435`](https://github.com/vllm-project/vllm/commit/f89989106fafc9c82b0025609065b5b0c1d43435), while GitHub records the main-branch merge/squash commit as [`1be6e937b2b49bae652370d80294f6171bd7b981`](https://github.com/vllm-project/vllm/commit/1be6e937b2b49bae652370d80294f6171bd7b981). GitHub's commit API reports the same one-file patch for both SHAs, changing only `vllm/v1/worker/gpu_model_runner.py`. It is not in v0.25.1; the PR author recorded no corresponding test or test result.
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

Activation belongs in an approved maintenance window and is a source-first,
single-unit change. Run from the reviewed repository root. Install only
`systemd/vllm-embedding.service`; do not sync the branch, copy `systemd/*`, or
stop/start/restart text or either reranker. The following blocks are intended to
run sequentially in the same Bash session so the private receipt directory and
snapshot helpers remain available.

First preserve the exact rollback source, embedding runtime/cgroup state, fixed
synthetic outputs for both aliases, and the three neighbor invariants:

```bash
set -euo pipefail
umask 077
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
EVIDENCE="$HOME/log/vllm-embedding-32k-canary-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$EVIDENCE"
test -f systemd/vllm-embedding.service
test -f "$UNIT_DIR/vllm-embedding.service"
install -m 0600 "$UNIT_DIR/vllm-embedding.service" \
  "$EVIDENCE/vllm-embedding.service.before"

snapshot_neighbors() {
  local output=$1 unit
  : >"$output"
  for unit in vllm-aeon-27b-dflash.service querit-4b-reranker.service \
    vllm-qwen3-reranker-8b.service; do
    timeout 10s systemctl --user show "$unit" \
      --property=Id --property=ActiveState --property=MainPID \
      --property=NRestarts >>"$output"
  done
}

snapshot_embedding_runtime() {
  local label=$1 container_pid cgroup_relative cgroup
  timeout 10s docker inspect vllm-embedding \
    >"$EVIDENCE/$label.docker.json"
  container_pid=$(timeout 10s docker inspect \
    --format '{{.State.Pid}}' vllm-embedding)
  cgroup_relative=$(awk -F: '$1 == "0" {print $3}' \
    "/proc/$container_pid/cgroup")
  case "$cgroup_relative" in /*) ;; *) return 1 ;; esac
  cgroup="/sys/fs/cgroup$cgroup_relative"
  {
    printf 'memory.current='; cat "$cgroup/memory.current"
    printf 'memory.peak='; cat "$cgroup/memory.peak"
    printf 'memory.max='; cat "$cgroup/memory.max"
    printf 'memory.swap.current='; cat "$cgroup/memory.swap.current"
    printf 'memory.swap.max='; cat "$cgroup/memory.swap.max"
    while read -r key value; do
      printf 'memory.events.%s=%s\n' "$key" "$value"
    done <"$cgroup/memory.events"
  } >"$EVIDENCE/$label.cgroup"
}

snapshot_neighbors "$EVIDENCE/neighbors.before"
timeout 10s systemctl --user show vllm-embedding.service \
  --property=Id --property=ActiveState --property=MainPID --property=NRestarts \
  >"$EVIDENCE/embedding.before.systemd"
timeout 10s journalctl --user -u vllm-embedding.service -n 200 --no-pager \
  >"$EVIDENCE/embedding.before.journal"
snapshot_embedding_runtime before

models=(qwen3-embedding-8b Qwen/Qwen3-Embedding-8B)
for index in "${!models[@]}"; do
  timeout 25s curl --fail-with-body --silent --show-error --max-time 20 \
    -H 'Content-Type: application/json' \
    --data "{\"model\":\"${models[$index]}\",\"input\":[\"gb10-embedding-canary-v1\",\"source-safe deterministic parity anchor\"]}" \
    http://100.105.4.92:18012/v1/embeddings \
    >"$EVIDENCE/alias-$index.before.json"
done
printf 'EVIDENCE=%q\n' "$EVIDENCE"
```

Install only the reviewed unit, reload systemd, request only the embedding
restart without waiting on the unit's 600-second startup timeout, and enforce a
hard 92-second readiness deadline:

```bash
install -m 0644 systemd/vllm-embedding.service \
  "$UNIT_DIR/vllm-embedding.service"
timeout 10s systemctl --user daemon-reload
ACTIVATED_AT=$(date --iso-8601=seconds)
timeout 15s systemctl --user --no-block restart vllm-embedding.service
timeout 92s bash -c '
  until systemctl --user is-active --quiet vllm-embedding.service &&
    curl --fail --silent --show-error --max-time 2 \
      http://100.105.4.92:18012/v1/models >/dev/null; do
    sleep 2
  done
'
```

Capture post-state, then verify the running Docker argv, startup KV capacity,
20 GiB Docker/cgroup cap, zero swap/cap events, both 4,096-dimensional alias
outputs, and exact neighbor invariants. The cosine threshold is deliberately
fixed at `0.99999` against each pre-restart alias and between post-restart
aliases; changing it requires a separately reviewed quality decision.

```bash
snapshot_neighbors "$EVIDENCE/neighbors.after"
timeout 10s systemctl --user show vllm-embedding.service \
  --property=Id --property=ActiveState --property=MainPID --property=NRestarts \
  >"$EVIDENCE/embedding.after.systemd"
timeout 10s journalctl --user -u vllm-embedding.service \
  --since "$ACTIVATED_AT" --no-pager >"$EVIDENCE/embedding.after.journal"
snapshot_embedding_runtime after

for index in "${!models[@]}"; do
  timeout 25s curl --fail-with-body --silent --show-error --max-time 20 \
    -H 'Content-Type: application/json' \
    --data "{\"model\":\"${models[$index]}\",\"input\":[\"gb10-embedding-canary-v1\",\"source-safe deterministic parity anchor\"]}" \
    http://100.105.4.92:18012/v1/embeddings \
    >"$EVIDENCE/alias-$index.after.json"
done

cmp "$EVIDENCE/neighbors.before" "$EVIDENCE/neighbors.after"
python3 - "$EVIDENCE" <<'PY'
import json
import math
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected_bytes = 20 * 1024**3
container = json.loads((root / "after.docker.json").read_text())[0]
host = container["HostConfig"]
assert host["Memory"] == expected_bytes, host["Memory"]
assert host["MemorySwap"] == expected_bytes, host["MemorySwap"]

argv = container["Config"]["Cmd"]
def one_value(flag: str) -> str:
    assert argv.count(flag) == 1, (flag, argv)
    index = argv.index(flag)
    assert index + 1 < len(argv), flag
    return argv[index + 1]
assert one_value("--max-model-len") == "32768"
assert one_value("--kv-cache-memory-bytes") == "4800M"
assert one_value("--dtype") == "bfloat16"
assert argv.count("--enforce-eager") == 1

metrics = {}
for line in (root / "after.cgroup").read_text().splitlines():
    key, value = line.split("=", 1)
    metrics[key] = value
assert metrics["memory.max"] == str(expected_bytes), metrics
assert metrics["memory.swap.max"] == "0", metrics
assert metrics["memory.swap.current"] == "0", metrics
for key in ("memory.events.max", "memory.events.oom", "memory.events.oom_kill"):
    assert metrics.get(key) == "0", (key, metrics.get(key))

journal = (root / "embedding.after.journal").read_text(errors="replace")
capacities = [
    int(value.replace(",", ""))
    for value in re.findall(r"GPU KV cache size:\s*([0-9,]+)\s*tokens", journal)
]
assert capacities and capacities[-1] >= 32768, capacities

def vectors(name: str) -> list[list[float]]:
    payload = json.loads((root / name).read_text())
    rows = sorted(payload["data"], key=lambda row: row["index"])
    result = [row["embedding"] for row in rows]
    assert len(result) == 2, len(result)
    assert all(len(vector) == 4096 for vector in result)
    assert all(math.isfinite(value) for vector in result for value in vector)
    return result

def cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    assert left_norm > 0 and right_norm > 0
    return numerator / (left_norm * right_norm)

before = [vectors(f"alias-{index}.before.json") for index in range(2)]
after = [vectors(f"alias-{index}.after.json") for index in range(2)]
comparisons = []
for alias in range(2):
    for input_index in range(2):
        comparisons.append(cosine(before[alias][input_index], after[alias][input_index]))
for input_index in range(2):
    comparisons.append(cosine(after[0][input_index], after[1][input_index]))
assert min(comparisons) >= 0.99999, comparisons
print({"kv_capacity": capacities[-1], "minimum_cosine": min(comparisons)})
PY
```

Do not call the new profile production-verified until every command above exits
zero and the receipts are retained.

## Rollback criteria and procedure

Rollback on any timeout, startup failure, KV capacity below 32,768, malformed or
quality-divergent embedding, nonzero cap/swap event, incorrect 20 GiB limit, or
changed neighbor tuple. Restore the exact saved embedding unit rather than
reconstructing an assumed 40,960/5,820 MiB/24 GiB profile. Reload systemd and
restart only embedding; never cycle text or either reranker:

```bash
set -euo pipefail
: "${EVIDENCE:?set EVIDENCE to the failed canary receipt directory}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
test -f "$EVIDENCE/vllm-embedding.service.before"
install -m 0644 "$EVIDENCE/vllm-embedding.service.before" \
  "$UNIT_DIR/vllm-embedding.service"
timeout 10s systemctl --user daemon-reload
ROLLBACK_AT=$(date --iso-8601=seconds)
timeout 15s systemctl --user --no-block restart vllm-embedding.service
timeout 92s bash -c '
  until systemctl --user is-active --quiet vllm-embedding.service &&
    curl --fail --silent --show-error --max-time 2 \
      http://100.105.4.92:18012/v1/models >/dev/null; do
    sleep 2
  done
'
: >"$EVIDENCE/neighbors.rollback"
for unit in vllm-aeon-27b-dflash.service querit-4b-reranker.service \
  vllm-qwen3-reranker-8b.service; do
  timeout 10s systemctl --user show "$unit" \
    --property=Id --property=ActiveState --property=MainPID \
    --property=NRestarts >>"$EVIDENCE/neighbors.rollback"
done
cmp "$EVIDENCE/neighbors.before" "$EVIDENCE/neighbors.rollback"
timeout 10s systemctl --user show vllm-embedding.service \
  --property=Id --property=ActiveState --property=MainPID --property=NRestarts \
  >"$EVIDENCE/embedding.rollback.systemd"
timeout 10s journalctl --user -u vllm-embedding.service \
  --since "$ROLLBACK_AT" --no-pager >"$EVIDENCE/embedding.rollback.journal"
```

Preserve both failed-canary and rollback receipts. A rollback restart validates
only restoration of the previous embedding source; it does not validate the new
32K profile.

## Unknowns

- Actual vLLM startup capacity at 4,800 MiB on the pinned GB10 image is unknown until a live maintenance-window restart.
- The post-change representative cgroup peak and true UMA residency reduction are unknown; proportional KV and peak reductions are projections.
- Whether #44490 alone fully explains this host's historical memory steps is unknown; its upstream bug class is relevant, but no private workload replay has been performed on a fixed image.
- Whether a future formal release will contain both #44490 and #48483, with adequate tests and SM121 artifacts, is unknown.
- SGLang embedding/reranker contract fidelity, SM121 artifact quality, memory profile, and OpenAI-compatible alias/dimension behavior remain pending research.

## Running log

- **2026-07-14T08:44:18-07:00:** Recorded current source/live aggregate evidence, proportional KV sizing, 20 GiB cap reasoning, v0.25.x fix/provenance gaps, and source-first decision. SGLang remains pending.
- **Future entry:** Add SGLang source/artifact/quality findings without rewriting the facts above; record new evidence and decision deltas with a new timestamp.
