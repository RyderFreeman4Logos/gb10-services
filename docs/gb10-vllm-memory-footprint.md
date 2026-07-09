# GB10 vLLM memory footprint

This note records the live memory footprint of the three co-resident vLLM
containers on the GB10 host (`promaxgb10-e659`) and explains how to interpret
Docker, cgroup, model-weight, KV-cache, and NVIDIA unified-memory accounting.

## Snapshot

- Captured at: `2026-07-08T23:34:38Z` (`2026-07-08 16:34:38 -0700`).
- Host: `promaxgb10-e659`, NVIDIA GB10, rootless Docker at
  `unix:///run/user/1001/docker.sock`.
- Raw evidence captured during this audit:
  - `target/evidence/gb10-vllm-memory-snapshot-20260708T163437.json`
  - `target/evidence/gb10-vllm-startup-memory-lines-20260708T163538.txt`
- Important workload caveat: the clean final three-route quality benchmark was
  running during the snapshot. AEON chat had live generation load
  (`num_requests_running=3`, `kv_cache_usage_perc=0.5818`). Embedding and
  reranker were idle (`num_requests_running=0`, `kv_cache_usage_perc=0`).
- Host memory at snapshot:
  - RAM total: `121.63 GiB`
  - RAM used: `109.87 GiB`
  - RAM available: `11.76 GiB`
  - Swap total: `16.00 GiB`
  - Swap used: `0.35 GiB`
- `nvidia-smi --query-gpu=memory.total,memory.used,memory.free` reports `N/A`
  on this GB10 stack, but per-compute-process `used_memory` is available and is
  the best live source for vLLM unified-memory residency.

## How to read the numbers

GB10 uses NVIDIA unified memory. A vLLM service therefore appears in several
partly overlapping ledgers:

1. **vLLM startup logs** report model loading memory and the configured KV-cache
   reservation. These are the best source for the `weights` and `KV reserve`
   rows below.
2. **`nvidia-smi --query-compute-apps`** reports the `VLLM::EngineCore` process
   memory. This is the best live per-service view of GPU/unified-memory
   residency. It includes weights, KV cache, CUDA/vLLM runtime allocations,
   graph/warmup state, and any active-request working set.
3. **Docker stats** is the Docker/cgroup CPU-memory view that Docker chooses to
   display. It can exclude page cache and does not include the full NVIDIA
   unified-memory residency.
4. **cgroup v2 `memory.current`** includes ordinary container memory and page
   cache. It is useful for CPU/RSS/swap safety, but it is not a complete budget
   for NVIDIA unified allocations.
5. **`/proc/*/smaps_rollup` PSS/RSS** is CPU process memory. It is useful for
   leak triage, but it is also not a complete GPU/unified-memory ledger.

Do not add Docker/cgroup memory and NVIDIA process memory as an exact physical
sum. Treat them as different accounting views of the same service.

## Summary table

All GiB values are binary GiB. `EngineCore overhead` is a rough diagnostic:

```text
nvidia_smi EngineCore GiB - vLLM model-loading GiB - configured KV reserve GiB
```

It includes CUDA/vLLM runtime allocations, graph/warmup state, active-request
working memory, and any memory not separately identified by the vLLM startup
log. It is not a hard budget knob.

| Service | Container | Docker limit | Docker stats | cgroup current | cgroup peak | cgroup swap | NVIDIA EngineCore | vLLM weights | KV reserve | Active KV at snapshot | EngineCore overhead | Model cache on disk |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Embedding | `vllm-embedding` | `24 GiB` | `3.419 GiB / 24 GiB` | `3.68 GiB` | `15.05 GiB` | `0 GiB` | `21.23 GiB` | `14.11 GiB` | `5.68 GiB` | `0.00 GiB` | `1.44 GiB` | `14.11 GiB` |
| AEON chat | `vllm-aeon-27b-dflash-n12` | `64 GiB` | `8.028 GiB / 64 GiB` | `8.15 GiB` | `22.82 GiB` | `0 GiB` | `43.00 GiB` | `22.16 GiB` | `15.00 GiB` | `8.73 GiB` | `5.84 GiB` | `22.38 GiB` |
| Reranker | `vllm-qwen3-reranker-8b` | `24 GiB` | `3.407 GiB / 24 GiB` | `7.99 GiB` | `13.91 GiB` | `0 GiB` | `26.54 GiB` | `14.11 GiB` | `5.68 GiB` | `0.00 GiB` | `6.75 GiB` | `15.27 GiB` |

Observed NVIDIA EngineCore subtotal: `90.77 GiB`.

Observed cgroup `memory.current` subtotal: `19.82 GiB`.

The large difference is expected on GB10: the NVIDIA unified-memory resident
weights/KV are visible via per-process NVIDIA accounting but are not fully
represented in Docker stats or container cgroup current memory.

## Per-service details

### `vllm-embedding.service`

Runtime contract:

- Raw backend: `100.105.4.92:18012`
- Container: `vllm-embedding`
- Image: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-01-v0.24.0`
- Model: `Qwen/Qwen3-Embedding-8B`
- Docker memory flags: `--memory 24g --memory-swap 24g --memory-swappiness 0`
- Live cgroup verification: `memory.max=24.00 GiB`, `memory.swap.max=0`
- vLLM flags:
  - `--convert embed`
  - `--max-model-len 40960`
  - `--max-num-batched-tokens 8192`
  - `--max-num-seqs 64`
  - `--kv-cache-memory-bytes 5820M`
  - `--enforce-eager`

Startup facts from journal:

- `Model loading took 14.11 GiB memory`
- `reserved 5.68 GiB memory for KV Cache`
- `GPU KV cache size: 41,376 tokens`
- `Maximum concurrency for 40,960 tokens per request: 1.01x`

Live memory at snapshot:

- Docker stats: `3.419 GiB / 24 GiB`, `209` PIDs.
- cgroup current: `3.68 GiB`:
  - anon: `3.24 GiB`
  - file cache: `0.37 GiB`
  - kernel: `0.07 GiB`
- cgroup peak: `15.05 GiB`.
- cgroup swap current: `0 GiB`.
- Process PSS subtotal: `3.41 GiB`.
- Process RSS subtotal: `3.57 GiB`.
- NVIDIA `VLLM::EngineCore`: `21.23 GiB`.
- Active KV usage: `0.0%` of the configured KV cache (`0.00 GiB`) at snapshot.
- Rough EngineCore residual after weights and KV: `1.44 GiB`.
- Model disk cache:
  - `/home/obj/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-8B`
  - apparent unique-inode size: `14.11 GiB`

KV details:

- `kv_cache_memory_bytes=6102712320` (`5.68 GiB`)
- `kv_cache_size_tokens=41376`
- block size: `16`
- GPU blocks: `2586`
- derived configured KV bytes/token: about `147,401 B/token`

### `vllm-aeon-27b-dflash.service`

Runtime contract:

- Raw backend: `100.105.4.92:18010`
- Container: `vllm-aeon-27b-dflash-n12`
- Image: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-01-v0.24.0`
- Model: AEON-7 Qwen3.6-27B Ultimate NVFP4 with DFlash draft model
- Docker memory flags: `--memory 64g --memory-swap 64g --memory-swappiness 0`
- Live cgroup verification: `memory.max=64.00 GiB`, `memory.swap.max=0`
- vLLM flags:
  - `--quantization modelopt`
  - `--kv-cache-dtype fp8_e4m3`
  - `--attention-backend TRITON_ATTN`
  - `--mamba-cache-dtype float32`
  - `--max-model-len 262144`
  - `--max-num-seqs 64`
  - `--max-num-batched-tokens 32768`
  - `--gpu-memory-utilization 0.49`
  - `--kv-cache-memory-bytes 15360M`
  - DFlash speculative config: `method=dflash`, `num_speculative_tokens=10`,
    `attention_backend=TRITON_ATTN`

Startup facts from journal:

- The target and auxiliary DFlash weights are loaded in two phases:
  - `Loading weights took 106.87 seconds`
  - `Loading weights took 18.93 seconds`
- Combined vLLM startup report: `Model loading took 22.16 GiB memory`
- `reserved 15.0 GiB memory for KV Cache`
- `GPU KV cache size: 269,589 tokens`
- `Maximum concurrency for 262,144 tokens per request: 1.03x`
- Engine initialization, including profiling, KV creation, warmup, and
  compilation: about `591.77 s` in the latest restart.

Live memory at snapshot:

- Docker stats: `8.028 GiB / 64 GiB`, `251` PIDs.
- cgroup current: `8.15 GiB`:
  - anon: `7.29 GiB`
  - file cache: `0.72 GiB`
- cgroup peak: `22.82 GiB`.
- cgroup swap current: `0 GiB`.
- Process PSS subtotal: `8.01 GiB`.
- Process RSS subtotal: `8.19 GiB`.
- NVIDIA `VLLM::EngineCore`: `43.00 GiB`.
- Active KV usage: `58.18%` of the configured KV cache during the benchmark,
  roughly `8.73 GiB` of the `15.00 GiB` reserve.
- Rough EngineCore residual after weights and KV: `5.84 GiB`.
- Model disk cache:
  - AEON target: `19.16 GiB`
    (`/home/obj/.cache/huggingface/hub/models--AEON-7--Qwen3.6-27B-AEON-Ultimate-Uncensored-Multimodal-NVFP4-MTP-XS`)
  - DFlash draft: `3.22 GiB`
    (`/home/obj/.cache/huggingface/hub/models--z-lab--Qwen3.6-27B-DFlash`)
  - total apparent unique-inode size: `22.38 GiB`

KV details:

- `kv_cache_memory_bytes=16106127360` (`15.00 GiB`)
- `kv_cache_size_tokens=269589`
- block size: `1808`
- GPU blocks: `869`
- cache dtype: `fp8_e4m3`
- Mamba cache: `float32`, mode `align`
- derived configured KV bytes/token: about `59,743 B/token`

Benchmark-specific note:

- At the snapshot, `/metrics` reported `num_requests_running=3`,
  `num_requests_waiting=0`, and `num_preemptions_total=12`.
- Because this service was under live generation load, `Active KV at snapshot`
  is meaningful here. The other two services were idle.

### `vllm-qwen3-reranker-8b.service`

Runtime contract:

- Raw backend: `100.105.4.92:18013`
- Container: `vllm-qwen3-reranker-8b`
- Image: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-01-v0.24.0`
- Model: `Qwen/Qwen3-Reranker-8B`
- Docker memory flags: `--memory 24g --memory-swap 24g --memory-swappiness 0`
- Live cgroup verification: `memory.max=24.00 GiB`, `memory.swap.max=0`
- vLLM flags:
  - `--runner pooling`
  - `--dtype bfloat16`
  - `--max-model-len 40960`
  - `--max-num-batched-tokens 40960`
  - `--max-num-seqs 64`
  - `--gpu-memory-utilization 0.22`
  - `--kv-cache-memory-bytes 5820M`
  - `--enforce-eager`

Startup facts from journal:

- `Model loading took 14.11 GiB memory`
- `reserved 5.68 GiB memory for KV Cache`
- `GPU KV cache size: 41,376 tokens`
- `Maximum concurrency for 40,960 tokens per request: 1.01x`

Live memory at snapshot:

- Docker stats: `3.407 GiB / 24 GiB`, `206` PIDs.
- cgroup current: `7.99 GiB`:
  - anon: `2.74 GiB`
  - file cache: `5.13 GiB`
  - kernel: `0.12 GiB`
- cgroup peak: `13.91 GiB`.
- cgroup swap current: `0 GiB`.
- Process PSS subtotal: `3.56 GiB`.
- Process RSS subtotal: `3.92 GiB`.
- NVIDIA `VLLM::EngineCore`: `26.54 GiB`.
- Active KV usage: `0.0%` of the configured KV cache (`0.00 GiB`) at snapshot.
- Rough EngineCore residual after weights and KV: `6.75 GiB`.
- Model disk cache:
  - `/home/obj/.cache/huggingface/hub/models--Qwen--Qwen3-Reranker-8B`
  - apparent unique-inode size: `15.27 GiB`

KV details:

- `kv_cache_memory_bytes=6102712320` (`5.68 GiB`)
- `kv_cache_size_tokens=41376`
- block size: `16`
- GPU blocks: `2586`
- derived configured KV bytes/token: about `147,401 B/token`

The reranker has a large cgroup file-cache component (`5.13 GiB`) even though
Docker stats shows only `3.407 GiB`. This is a good example of why Docker stats
and cgroup `memory.current` should be recorded separately.

## Allocation model

The most useful mental model for this stack is:

| Component | Embedding | AEON chat | Reranker |
|---|---:|---:|---:|
| Model weights loaded by vLLM | `14.11 GiB` | `22.16 GiB` | `14.11 GiB` |
| Configured KV cache reserve | `5.68 GiB` | `15.00 GiB` | `5.68 GiB` |
| Approx. vLLM/CUDA/graph/working overhead | `1.44 GiB` | `5.84 GiB` | `6.75 GiB` |
| NVIDIA EngineCore total | `21.23 GiB` | `43.00 GiB` | `26.54 GiB` |

For the Qwen3 8B embedding and reranker services, the configured KV budget is
identical. The larger live NVIDIA footprint of the reranker is therefore not a
larger configured KV cache; it is runtime overhead and cache/accounting behavior
outside the simple `weights + configured KV` model.

For AEON chat, the DFlash/FP8-KV setup has lower configured KV bytes/token than
the Qwen3 8B BF16/auto-KV services, but it has a much larger model plus DFlash
runtime/compile/warmup overhead.

## Reproduction commands

Read-only snapshot commands used by this audit were equivalent to:

```bash
# Run on the operator machine and execute read-only probes on GB10.
ssh obj@gb10 'python3 -' < /tmp/gb10_vllm_memory_snapshot.py \
  > target/evidence/gb10-vllm-memory-snapshot-$(date +%Y%m%dT%H%M%S).json

ssh obj@gb10 'set -euo pipefail
for u in vllm-embedding.service vllm-aeon-27b-dflash.service vllm-qwen3-reranker-8b.service; do
  echo "===== $u ====="
  journalctl --user -u "$u" -n 20000 --no-pager \
    | grep -Ei "Loading weights|Model loading took|Initial free memory|reserved .*KV|GPU KV cache size|Maximum concurrency|CacheConfig|cache_config_info|num_gpu_blocks|DFlash|speculative|kv-cache|kv cache|Available KV|memory profiling" \
    | grep -Ev "loggers.py|GPU KV cache usage|Prefix cache hit|Avg prompt throughput" \
    | tail -80 || true
  echo
done' > target/evidence/gb10-vllm-startup-memory-lines-$(date +%Y%m%dT%H%M%S).txt
```

Useful one-off live probes:

```bash
# Docker CPU/cgroup-facing view.
DOCKER_HOST=unix:///run/user/1001/docker.sock docker stats --no-stream \
  vllm-embedding vllm-aeon-27b-dflash-n12 vllm-qwen3-reranker-8b

# Per-process NVIDIA unified-memory view.
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits

# vLLM KV/runtime view.
for port in 18012 18010 18013; do
  curl -fsS "http://100.105.4.92:${port}/metrics" \
    | grep -E 'vllm:(cache_config_info|num_requests_running|num_requests_waiting|kv_cache_usage_perc|num_preemptions_total)'
done
```

## Operational takeaways

- The live three-service NVIDIA EngineCore subtotal is about `90.77 GiB`, while
  Docker stats shows only about `14.85 GiB` across the three containers. This is
  normal for GB10 unified-memory attribution; Docker stats is not the memory
  budget for model weights/KV residency.
- The configured Docker limits remain useful guardrails for ordinary container
  memory. At snapshot time all three generated docker scopes had
  `memory.swap.max=0`, so the post-start swap-enforcement helper was effective.
- The configured vLLM KV reserves are:
  - embedding: `5.68 GiB`
  - AEON chat: `15.00 GiB`
  - reranker: `5.68 GiB`
- Under the live benchmark, AEON was actively using about `8.73 GiB` of its
  `15.00 GiB` KV reserve. Embedding and reranker were idle.

Future memory triage should always capture both ledgers: Docker/cgroup/PSS for
CPU and swap behavior, plus `nvidia-smi --query-compute-apps` and vLLM startup
logs for model/KV unified-memory residency.

## Can we force high-load memory to stay flat?

Short answer: **partly for KV cache, not absolutely for total process memory**.

What is already fixed in this stack:

- All three services use explicit `--kv-cache-memory-bytes`, so vLLM does not
  size KV cache opportunistically from current free memory. The configured KV
  pools are created at startup and request load consumes blocks inside those
  pools.
- `kv_cache_usage_perc` therefore means "how much of the configured KV pool is
  currently occupied", not "how much new KV memory was allocated from the OS".
  In the snapshot, AEON was using about `58.18%` of its already configured
  `15.00 GiB` KV reserve.
- The Docker scopes also had `memory.swap.max=0`, so ordinary container memory
  should fail/OOM instead of silently growing into swap.

What cannot be made perfectly flat just by lowering Docker memory limits:

- Docker/cgroup `memory.max` mainly constrains ordinary container memory. On
  GB10, it does not precisely cap NVIDIA unified-memory residency reported by
  `nvidia-smi --query-compute-apps`.
- PyTorch/CUDA/vLLM can still allocate non-KV memory after startup: request
  working buffers, attention workspaces, tokenizer/API buffers, prefix-cache
  metadata, allocator fragmentation, JIT kernels, and CUDA graph capture state.
- AEON chat currently runs compiled/DFlash mode rather than `--enforce-eager`.
  Its startup config shows CUDA graph capture sizes up to `512`, and the latest
  startup log included a Triton JIT warning during inference. That means new
  request shapes can still cause extra one-time runtime allocations even though
  KV itself is fixed.

Practical ways to make high-load memory more bounded:

1. **Keep explicit KV sizing** (`--kv-cache-memory-bytes`) and verify startup
   logs after every change. This is already done.
2. **Limit admission before vLLM sees the load.** Guard/proxy queue limits are
   the safest production control. Raw vLLM ports should stay maintenance-only;
   otherwise raw clients bypass the guard and can force vLLM to exercise larger
   scheduler/graph shapes.
3. **Cap vLLM scheduler shape** if strict memory flatness is more important
   than raw headroom: lower `--max-num-seqs` and/or
   `--max-num-batched-tokens`. This bounds concurrent work and graph/workspace
   shape, but it can reduce throughput and may reject workloads that were
   previously admitted.
4. **Avoid or restrict CUDA graph capture** for stricter memory predictability.
   Embedding and reranker already use `--enforce-eager`. AEON could be tested
   with eager or smaller graph-capture settings, but that is a performance and
   DFlash-risk trade-off and should be benchmarked before production use.
5. **Warm up representative shapes** after restart if compiled/CUDA-graph mode
   stays enabled. This moves one-time JIT/graph growth into startup/warmup so
   later production traffic is less likely to grow memory unexpectedly.
6. **Use cgroup limits as fail-fast guardrails, not as precise GPU memory
   caps.** Further lowering Docker `--memory` can make CPU-side leaks fail
   earlier, but it will not make NVIDIA EngineCore memory equal Docker stats.

Operationally, the target should be **bounded and fail-fast**, not literally
"no allocation after startup". For this stack the closest safe contract is:

- fixed KV reserve;
- warmed-up compiled shapes or eager mode;
- guard admission below the largest warmed shape;
- `memory.swap.max=0` for ordinary container memory;
- monitoring on `nvidia-smi` EngineCore memory, cgroup memory, and active swap
  I/O.
