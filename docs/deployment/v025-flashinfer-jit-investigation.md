# GB10 AEON v0.25 Startup Memory & FlashInfer JIT Investigation Log

**Date:** 2026-07-16  
**Author:** obj  
**Status:** Historical investigation — root cause identified; the current source profile uses AUTO KV at `gpu-memory-utilization=0.355`.

---

## TL;DR

Text LLM (AEON 27B DFlash) cannot restart on GB10 with v0.25 image when embedding
and reranker are co-resident. Root cause is **NOT** a single issue but a chain:

1. **gb10-memory-guardian kills text during startup** (`mem_avail_stop_gib=2`)
2. **FlashInfer 0.6.13 JIT compilation fails** when MemAvail is tight
3. Both compound: text starts → guardian kills it → restart → FlashInfer JIT
   cache lost (--rm) → JIT fails again → cycle

**v0.24 did not have this problem** because:
- FlashInfer was an older version (0.6.6/0.6.7) with pre-compiled kernels
- guardian may not have been deployed yet, or threshold was different
- The kernel JIT workspace demand was lower

---

## Timeline of discoveries

### Phase 1: Suspected batch/KV config (WRONG)

Initial hypothesis: `--max-num-batched-tokens` too large → compile peak → OOM 137.

Tested configurations:
| Config | batch | KV | util | Result |
|--------|-------|-----|------|--------|
| AEON spec | 16384 | AUTO | 0.6 | 137 (guardian kill) |
| Pin KV | 4096 | 15360M | 0.49 | 137 (guardian kill) |
| Reduced batch | 8192 | AUTO | 0.6 | 137 (guardian kill) |
| Tiny batch | 4096 | AUTO | 0.6 | exit 1 (FlashInfer JIT fail) |

None solved the problem. Batch reduction did not help because the kill happens
AFTER torch.compile, during FlashInfer JIT or KV allocation.

### Phase 2: Discovered guardian is the killer

Stopped `gb10-memory-guardian` → text survived compile peak (MemAvail 7.7G
but not killed). But then hit a different failure: FlashInfer JIT
`ninja: build stopped: subcommand failed`.

Guardian config: `mem_avail_stop_gib = 2` — kills text cgroup when
MemAvailable drops below 2 GiB. During startup, text legitimately needs
to dip low temporarily (compile + JIT + KV allocation + cudagraph capture).

### Phase 3: FlashInfer JIT cache ephemeral (--rm)

Container uses `--rm`, so `/root/.cache/flashinfer/` is lost every restart.
Each restart re-compiles 18 CUTLASS FP4 GEMM kernels via nvcc, which:
- Takes ~4 minutes
- Consumes significant host RAM (nvcc processes)
- Fails when MemAvail is tight (gcc/nvcc subprocess killed)

The ONE successful seed (02:35-02:43) happened when text was alone
(emb stopped) with ~95G MemAvail. FlashInfer JIT compiled all 18 kernels
successfully and saved 132 autotune configs.

### Phase 4: Fix — persistent FlashInfer cache volumes

Added two host volume mounts to the text unit:
- `/home/obj/.cache/flashinfer-025` → `/root/.cache/flashinfer`
- `/home/obj/.cache/vllm-flashinfer-autotune-025` → `/root/.cache/vllm/flashinfer_autotune_cache`

After first successful seed, subsequent restarts read from cache and skip JIT.

---

## Root cause analysis: Why v0.24 worked but v0.25 doesn't

| Factor | v0.24 | v0.25 |
|--------|-------|-------|
| FlashInfer version | 0.6.6/0.6.7 | 0.6.13 |
| Kernel JIT | Mostly pre-compiled | Requires JIT for FP4 CUTLASS |
| nvcc compilation per restart | Minimal | 18 kernels, ~4 min |
| Peak RAM during JIT | Low | High (nvcc subprocesses) |
| Guardian deployed | Unknown/unlikely | Yes (mem_avail_stop_gib=2) |

The v0.25 image upgraded FlashInfer from 0.6.x to 0.6.13, which introduced
FP4 GEMM CUTLASS kernel JIT compilation. These kernels are not pre-compiled
in the image and must be compiled on first use. The `--rm` container flag
means the compiled cache is discarded after each restart.

---

## Solution architecture

```
┌─────────────────────────────────────────────────────┐
│ Seed phase (one-time, text alone, guardian stopped) │
│  1. Stop guardian                                    │
│  2. Stop rr (free memory for JIT)                    │
│  3. Start text → FlashInfer JIT compiles to          │
│     persistent host volume                           │
│  4. Wait for /v1/models 200                          │
│  5. Start rr                                         │
│  6. Start guardian                                   │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│ Normal restart (cache warm)                          │
│  1. Text restarts → reads FlashInfer cache → no JIT  │
│  2. Startup peak is much lower (no nvcc)             │
│  3. Guardian may need grace period or higher         │
│     mem_avail_stop_gib                               │
└─────────────────────────────────────────────────────┘
```

---

## Open questions

1. **Does warm cache eliminate the guardian kill?** Need to verify: after
   seed, does a restart with emb+rr resident survive without guardian
   intervention?

2. **Should guardian have a startup grace period?** Guardian kills text
   when MemAvail < 2G, but text's startup peak is legitimate. Options:
   - Increase `mem_avail_stop_gib` to a value that doesn't trigger during
     normal startup
   - Add a "startup window" to guardian (don't kill for N minutes after
     target registration)
   - Use `gb10_restart_text_safe.sh` which stops guardian before text restart

3. **Guardian → llm-guard-proxy migration:** User confirmed guardian will be
   deprecated. The replacement (llm-guard-proxy) should also account for
   startup grace periods.
