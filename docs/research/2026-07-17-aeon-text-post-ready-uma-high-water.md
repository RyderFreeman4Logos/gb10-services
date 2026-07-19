# AEON text post-ready UMA high-water — 2026-07-17

- **Evidence cut:** 2026-07-17 03:08 PDT
- **Question:** What explains the same-process, post-readiness host-memory step in the AEON text generation, and is the later plateau operationally safe?
- **Decision status:** Investigation complete enough for containment and a cold-canary plan; no runtime fix is claimed or approved.
- **Live change in this documentation transaction:** None.

## Executive result

> **`plateau != operational stability`**

The immutable AEON text generation retained a post-ready host-memory high-water after two dominant steps totaling 11.90 GiB and a net event-window increase of **12.07 GiB**. The service PID and container generation did not change. Host-side API and engine RSS were flat to slightly lower across the steps, while later heavy traffic did not produce a second comparable step. This does **not** prove a sustained leak.

It proves that readiness did not establish operationally safe headroom: the generation retained about 12.07 GiB of additional host use after it was already ready. The evidence makes an insufficiently warmed expensive shape the high-confidence explanation, but it does not prove a row-level request cause. At the evidence cut, only about 15.35 GiB was available. A bare predictive watermark of guardian floor plus the observed residual is **5.00 + 12.07 = 17.07 GiB before any safety margin**. Repeating a residual of the same size from the current state would project only about **3.27 GiB** available, below the guardian's 5 GiB stop floor. A stable high-water can therefore still be one request shape away from crossing the reactive guard.

### Proven

1. The running image, runtime, container, host PID, and container creation generation remained unchanged throughout the observed event.
2. The service became application-ready before the 01:33–01:37 memory steps. The complete KV pool and startup CUDA-graph pool had already been allocated.
3. The event window added 12,364 MiB (12.07 GiB) of host used memory; 12,182 MiB (11.90 GiB) was concentrated in two short steps.
4. API and engine RSS were flat to slightly lower during both steps. That contradicts a 12 GiB host-RSS heap-growth explanation, although it does not exclude CUDA/UVM residency or allocator retention.
5. Host memory, text cgroup memory, process RSS/HWM, and NVML process memory are divergent ledgers on this UMA host. They must not be added as independent physical totals.
6. The post-event window plateaued: a heavy guarded workload later processed 2.18 million input tokens without a second comparable host-memory step.
7. The current 15.35 GiB available is below the 17.07 GiB bare predictive watermark derived from the observed residual step and the 5 GiB guardian floor.
8. The raw text backend remains tailnet-published on port 18010, so external callers can bypass guard admission on port 18009.
9. Guard and backend aggregate token evidence do not close over the same request boundary. At least 18 residual 50k–100k prompt completions were not represented by guard admission/usage records after conservative reconciliation.
10. The pulled v0.25.1 image was not deployed. Its official v0.25.0-to-v0.25.1 source delta does not contain a declared fix for this runtime-memory path.

### High-confidence

1. The event is a **retained, shape-dependent post-ready high-water**, most plausibly involving long-prefill target/drafter/backend workspaces plus PyTorch/CUDA allocator retention in CUDA/UVM accounting.
2. Long-prompt first touch is the strongest workload-class attribution. The request-boundary reconstruction, prefix-cache collapse, KV pressure, and lack of recurrence under already-warmed traffic support that class, but not a specific request or client.
3. The later plateau is consistent with reusable allocator/workspace capacity remaining resident after the expensive shape. It is evidence against continuous growth during the observed window, not evidence that all larger or more concurrent shapes are warm.
4. A 3-second reactive poll cannot reliably protect against the observed dominant steps: 4.51 GiB accumulated within one 3-second sample interval, and 7.39 GiB accumulated across five seconds. The retained 12.07 GiB event residual then creates repeat-shape capacity risk. Predictive admission and representative warmup are required.

### Not proven

1. No exact allocator, tensor, layer, backend workspace, or CUDA/UVM owner has been instrumented for the 12.07 GiB class.
2. A sustained memory leak is not proven. The observed plateau and later workload argue against one during this window, but they do not prove indefinite stability.
3. No particular client, request, or route is independently attributable to the event. Rootless NAT collapses backend source attribution, and the operator's report of one Hermes-origin workload is contextual evidence only.
4. It is not proven that an identical request would allocate another 12.07 GiB. The repeat projection is a fail-safe capacity calculation, not a forecast of allocator behavior.
5. It is not proven that turning DFlash off, changing CUDA-graph mode, changing allocator configuration, or reducing batch size will remove the high-water without measured cold A/B evidence.
6. v0.25.1 is not proven to fix this event; it must not be advertised as a fix.

## Scope and non-actions

The evidence cut is 2026-07-17 03:08 PDT. The investigation used only control-plane `GET /models` and `GET /metrics` requests against model endpoints. It sent no generation, stress, replay, or warmup request. It performed no stop, start, restart, kill, reload, deployment, image activation, or configuration mutation. The live PID/CID generation remained constant.

This note is content-independent. It contains no request IDs, source IP addresses, prompts, request bodies, or generated text. Request evidence is limited to aggregate counts, token buckets, byte sizes, timing, status classes, and content-free scheduler/usage fields.

The immutable running container is authoritative for the live configuration described below. Loaded, staged, or tracked unit text is not substituted for the running generation. This isolated documentation change does not modify, deploy, or reconcile service source outside this diary and index.

## Immutable running generation and actual configuration

### Identity

| Field | Immutable live value at the cut |
|---|---|
| Image digest | `sha256:18c09e6b80141a530285160781f7fa720a78ef91143b3c15a65a8c9641b44e55` |
| Runtime version | `0.25.0+aeon.sm121a.dflash` |
| Container | `vllm-aeon-27b-dflash-n12` |
| Host PID | `2118389` |
| Container created | 2026-07-16 23:38:40 PDT |
| Application ready | 2026-07-16 23:56:13 PDT |
| systemd active | 2026-07-16 23:56:24 PDT |
| Raw publish | Tailnet-reachable port `18010` |

A newer image was pulled but **not deployed**:

- digest: `sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d`
- version family: v0.25.1
- operational status: image-store candidate only; it is not evidence about the running generation

### Model and command contract observed from the running container

The target is a Qwen3.5/Qwen3.6-family conditional-generation hybrid with 64 layers: 48 linear-attention layers and 16 full-attention layers.

| Area | Actual running setting |
|---|---|
| Attention/speculation | Target and DFlash route through `TRITON_ATTN`; DFlash speculative depth `n=10` |
| Draft model | BF16, hidden size 5,120, five layers |
| Context | `max_model_len=262144` |
| Scheduling | `max_num_seqs=16`; `max_num_batched_tokens=4096` |
| Partial prefill | maximum partial prefills `1`; maximum long partial prefills `1` |
| Cache behavior | chunked prefill; prefix cache; Mamba cache dtype `float32` |
| KV sizing | AUTO with GPU-memory utilization `0.355` |
| Compilation/graphs | `FULL_DECODE_ONLY`; graph warmups `0`; JIT-warning mode enabled |
| Container memory | Docker memory and memory-swap both 128 GiB; `oom_score_adj=800` |

Physical `MemTotal` is only 121.63 GiB. The 128 GiB Docker setting is therefore not a host-protection boundary. The observed cgroup/UMA accounting divergence independently shows why it cannot be treated as a physical CUDA/UVM cap.

## Startup allocation ledger

| Stage | Host used memory | Derived/observed detail |
|---|---:|---|
| Before text start | 47,835 MiB | Co-resident baseline before this text generation |
| Application ready | 95,901 MiB | Cold-to-ready increase 48,066 MiB = **46.94 GiB** |
| Ready headroom | — | Derived available memory **27.97 GiB** |

Startup telemetry reported:

- model loading: 22.16 GiB overall;
- target checkpoint: 19.15 GiB;
- draft checkpoint: 3.22 GiB;
- KV pool: 16.05 GiB, 288,513 tokens;
- hybrid physical-attention block size: 1,808 tokens;
- full-context concurrency estimate: 1.10;
- actual CUDA-graph pool: 0.42 GiB versus a 0.01 GiB estimator value.

The loader summaries have independently rounded scopes; they are preserved as reported rather than forced to arithmetically reconcile.

The full KV pool existed before readiness. Graph capture was startup-only. Nine JIT warnings had all appeared by 00:00:30, well before the incident, and no JIT or graph warning occurred at 01:33. Startup used `MAX_JOBS=1` with the Triton route; there was no multi-job FlashInfer compilation event in the memory-step window.

These facts rule out “KV blocks were gradually charged only when requests arrived,” startup graph capture at incident time, and an incident-time multi-job compile as explanations for the post-ready step.

## Event timeline

`sysmon` reports `free(1)`-style used memory. For this analysis, used memory is total minus available and was cross-checked against `/proc/meminfo` at the evidence cut.

| Time (PDT) | Host memory observation | Other aligned evidence |
|---|---:|---|
| 01:32 | used 96,435 MiB; available 27.45 GiB | Pre-event baseline |
| 01:33:55–01:33:58 | **+4,614 MiB** (+4.51 GiB) | First dominant step; GPU utilization sampled at zero |
| 01:37:07–01:37:12 | **+7,568 MiB** (+7.39 GiB) | Second dominant step; host swap used increased 881 MiB; GPU utilization sampled at zero |
| Post-step | used 108,799 MiB; available 15.38 GiB | Net event-window increase 12,364 MiB = **12.07 GiB** |
| 01:39–01:59 | +41 MiB | Immediate retained plateau |
| 01:43 onward | — | Compute activity became visible only later; peak GPU utilization reached 95% |
| 02:03–02:30 | min 108,800; max 109,499; end 108,895 MiB | Heavy guarded traffic; end was 604 MiB below the interval maximum |
| 03:08 | used 108,830 MiB = 106.28 GiB; available 15.35 GiB | Swap used about 2.99 GiB; memory PSI zero |

The two dominant steps sum to 12,182 MiB (11.90 GiB); the entire window adds 12,364 MiB (12.07 GiB). That concentration is step-like, not the small linear slope expected from the known host-list leak discussed later.

Across both dominant steps:

- text API RSS was approximately 3.82 GiB before and 3.80 GiB after;
- text engine RSS was approximately 3.55 GiB before and 3.54 GiB after.

Flat RSS is strong negative evidence for a host-RSS heap leak of this magnitude. It does not exclude CUDA allocations, UVM migration/residency, driver accounting, or PyTorch CUDA allocator reservation.

## Request-boundary reconstruction

### The event window itself

From 01:32 through 01:40, the proxy database was opened with SQLite read-only/query-only controls. It contained only one 168-byte, 64 ms, client-disconnected health attempt and no actual generation attempt in that interval.

After 01:33, vLLM engine-stat and journal reporting was silent until one raw completion at 01:58. There was no incident-time JIT or graph warning. Logging silence is not proof that no backend work occurred; it limits row-level attribution.

Backend source addresses cannot identify a caller because rootless NAT collapses them to one bridge source. The operator reported one Hermes-origin workload, but that identity cannot be independently proven from backend access evidence. This note therefore attributes only a workload class, never a client.

### Aggregate guard/backend reconciliation at 03:08

| Aggregate ledger | Count | Input/prompt token sum |
|---|---:|---:|
| vLLM request-prompt histogram | 220 | 5,209,344 |
| Proxy text attempts with usage | 116 | 2,985,693 |

Content-free histogram-bucket subtraction produced 25 usage-unaccounted completions in the 50k–100k prompt bucket. The read-only proxy database separately identified seven large-body retries, failures, or aborts with null usage:

- three could be backfilled from exact sibling usage in the approximately 81.8k–88.8k range;
- four were conservatively assigned to the same 50k–100k bucket.

After that conservative reconciliation, **18 residual 50k–100k completions** remained absent from guard admission/usage accounting. Their conservative aggregate prompt volume is 1.352–1.670 million tokens, with a mean of 75,115–92,769 tokens.

This is count- and bucket-level evidence, not row-level causality. Retries, null usage, cumulative counter boundaries, and NAT prevent mapping the residual set to a specific client or event request. It does establish that raw/backend completion accounting does not close over the guard admission/usage boundary. Raw port 18010 is therefore both an ingress-control gap and an observability gap.

### Guard-recorded workload after the event

For 02:00–02:30, guard usage records show:

| Metric | Value |
|---|---:|
| Attempts with usage | 40 |
| Total input tokens | 2,183,400 |
| Median input tokens | 81,042 |
| Approximate p95 lower bound | 86,702 |
| Maximum input tokens | 88,207 |
| Maximum observed long-attempt overlap | 2 |
| Peak active prompt tokens | 94,601 |
| Policy output reserve | 90,960 |
| Total observed obligation | 185,561 = **64.3%** of 288,513-token KV capacity |

The live guard permits four requests in flight, with an upstream queue of 64 and server queue of 128. Four requests at the observed maximum would require 352,828 prompt tokens, **122.3%** of the 288,513-token KV capacity before output reservation. That four-way case was permitted by configuration but was not observed in this interval.

vLLM logs reached:

- maximum `Running=6`;
- maximum `Waiting=3`;
- KV occupancy 98.1%;
- interval prefix-cache hit rate collapse from 86.7% to 8.3%;
- six cumulative preemptions.

The backend `/v1/models` response exposes `max_model_len=262144`, but admission reads only static `metadata.context_*_override` fields. The live override is null, and records show `skipped_no_context_window`. The request-count semaphore is therefore neither a context-window preflight nor token-weighted admission.

The important negative result is that 2.18 million later guarded input tokens did not create a second 12 GiB step. That supports a first-touch/shape high-water interpretation, but the traffic did not exhaustively repeat every distinct shape or permitted four-way obligation.

## Three independent memory ledgers at 03:08

These views overlap and account UMA differently. **Do not sum them.**

### Host physical ledger

| Metric | Value |
|---|---:|
| `MemAvailable` | 15,704 MiB (about 15.35 GiB) |
| Used memory | 108,830 MiB (106.28 GiB) |
| Swap used | about 2.99 GiB |
| Memory PSI | zero |

### Text cgroup and host-process ledger

| View | Current | Historical high-water |
|---|---:|---:|
| Text cgroup `memory.current` | 7,426 MiB | `memory.peak` 22,129 MiB |
| Text cgroup swap | 0 | — |
| API RSS | 3,802 MiB | HWM 3,965 MiB |
| Engine RSS | 3,506 MiB | HWM 20,501 MiB |

### Current NVML process ledger

| Service | Current NVML memory |
|---|---:|
| Reranker | 32,837 MiB |
| Embedding | 20,818 MiB |
| Text | 41,619 MiB |

There is no historical per-PID NVML series for the 01:33–01:37 steps. Consequently, the current NVML snapshot cannot retroactively name the allocator or process-memory subcomponent responsible for the host delta. The ledger divergence is itself a finding: host available memory is the physical safety authority, while cgroup/RSS and NVML are complementary attribution views.

## Source-level allocation analysis

### Facts from the live image source

1. Mamba alignment first-forward setup in `_ensure_align_ctx` creates small per-layer/request metadata. It is not a GiB-scale allocation class.
2. Fixed hybrid recurrent state is allocated in the startup KV pool, which was complete before readiness.
3. The DFlash constructor allocates a fixed `[max_num_batched_tokens, hidden_size]` BF16 buffer. At the live values, `4096 × 5120 × 2 bytes` is about **40 MiB**, and it is a startup allocation.
4. FLA Gated DeltaNet chunk prefill has shape-dependent temporaries including:
   - `A [B,T,H,64]` in float32;
   - `w`, `u`, and `v_new [B,T,H,128]` in BF16;
   - `h [B,num_chunks,H,128,128]` in BF16.
5. At nominal batch 4,096 and up to 16 sequences, the known simultaneous temporary lower bound is about **336 MiB**. It must **not** be multiplied by 48 linear-attention layers: sequential layer execution, buffer reuse, and actual lifetimes have not been instrumented.

The known tensors explain why long-prefill shape matters, but they do not independently add to 12 GiB. Target attention, drafter speculation, FLA/Triton workspaces, q/k/v and MLP activations, temporary casts, allocator binning, fragmentation, and UVM residency still lack event-time instrumentation.

### Ruled out or strongly contradicted

| Candidate explanation | Evidence verdict |
|---|---|
| KV blocks gradually allocated after readiness | Ruled out: the complete 16.05 GiB KV pool existed before readiness |
| Incident-time CUDA graph capture | Ruled out: graph capture was startup-only and no graph warning aligned with the event |
| Incident-time multi-job FlashInfer/JIT compilation | Ruled out: startup used `MAX_JOBS=1`, all nine JIT warnings ended by 00:00:30, and no incident warning appeared |
| 12 GiB Python/host heap growth | Strongly contradicted: API and engine RSS were flat to slightly lower across both steps |
| New service/container generation | Ruled out: immutable PID/CID generation did not change |
| Continuous growth proportional to later request count | Not observed: later heavy traffic plateaued without a second comparable step |
| Docker 128 GiB cap protects the host | False as an operational premise: it exceeds physical memory and CUDA/UMA is not fully represented by text cgroup current |

### Ranked remaining hypotheses

1. **High confidence — shape-dependent long-prefill workspace plus allocator-retained high-water.** The best current explanation is a new long-prefill/concurrency shape touching target, drafter, Triton/FLA, and PyTorch/CUDA workspace classes whose reserved pages remained resident in CUDA/UVM accounting.
2. **Medium confidence — allocator fragmentation or shape-bin expansion amplified the retained delta.** This is compatible with the two steps and later reuse, but event-time `memory_allocated`, `memory_reserved`, max counters, and `memory_snapshot` are absent.
3. **Medium confidence — raw/guard boundary gaps allowed an inadequately admitted shape.** This explains why guard records do not fully describe backend work, not which allocator retained memory.
4. **Low confidence for this event — upstream `new_block_ids` host leak.** vLLM PR #44490 addresses an undrained host list for pure full-attention behavior. The live model is hybrid and requires cache zeroing; the known leak is linear and small relative to a GiB-scale two-step event. It is worth carrying as source risk, not naming as root cause.
5. **Low confidence for this event — startup dummy-KV graph profiling.** PR #48483 bounds temporary dummy KV during startup graph profiling. This event occurred post-ready with no graph activity, so its symptom timing does not match.

Neither #44490 nor #48483 is contained in the two official tags compared here.

## Why the plateau is still unsafe

The guardian watches only the current text cgroup target, uses a 5,120 MiB host-available threshold, and polls every three seconds. It is a last-resort reactive kill path, not admission control.

A minimal predictive relationship is:

```text
required_host_available
  = guardian_floor
  + largest_measured_residual_step_for_an_unwarmed_admitted_shape
  + safety_margin
```

Using this event only:

```text
guardian_floor              =  5.00 GiB
observed residual step      = 12.07 GiB
bare watermark              = 17.07 GiB
current available           = 15.35 GiB
projected after same step   =  3.27 GiB
```

The 17.07 GiB value is a **diagnostic lower bound, not approved policy**. It has no safety margin, and the largest not-yet-tested shape may be larger. The final watermark must substitute the measured residual step after representative cold warmup and repeated warmed waves.

Zero PSI and a flat 20-minute window describe current pressure, not the next shape. The first dominant step consumed 4.51 GiB in one 3-second sample interval, and the second consumed 7.39 GiB across five seconds; a poll can observe the condition only after substantial headroom is gone. The full 12.07 GiB is the retained event residual, not a single three-second allocation. That is the operational meaning of `plateau != operational stability`.

## Guard, readiness, ingress, and telemetry gaps

### Ingress

- External clients should reach guard port 18009 only.
- Raw port 18010 is currently tailnet-published and permits admission bypass.
- Proposed source change: bind raw vLLM to loopback and let only the local guard and monitor access it.
- **No ingress change is made or authorized by this note.**

### Admission

- Populate the static context override with the immutable 262,144-token contract so preflight no longer records `skipped_no_context_window`.
- Replace request-count-only admission with a token-weighted obligation: active input tokens plus reserved output tokens, evaluated against block-aligned KV capacity and headroom.
- Keep an explicit predictive host-memory watermark derived from guardian floor, measured residual shape step, and safety margin.
- Concurrency four is not safe merely because the semaphore allows it; four observed maxima already exceed KV capacity before output reserve.

### Readiness

The current ready hook sends only a tiny liveness request with `max_tokens=2`. It proves endpoint liveness but does not warm long prefill, distinct-prefix concurrency, DFlash, hybrid-linear temporaries, or allocator shape bins. Readiness must remain closed until a representative worst-shape warmup has completed in a controlled cold candidate.

### Guardian

A 5 GiB floor and 3-second poll is necessarily reactive. It cannot substitute for token admission or shape warmup. The guardian remains valuable as fail-closed containment, but acceptance must be established above it.

### Monitoring

- The `vllm-perf` monitor currently queries guard port 18009 `/metrics`, not raw vLLM port 18010 metrics.
- Across 376 rows, running, waiting, KV, token, and throughput columns are all empty. Endpoint-up rows must not be interpreted as an idle scheduler.
- The live `sysmon` CSV lacks `MemAvailable`, per-PID GPU/NVML memory, and sample-lag fields.
- Source authority should include the monitor configuration, raw loopback metrics, and schema assertions that fail when required columns remain empty.
- Future capture must align 1-second host, cgroup, RSS/PSS, per-PID NVML, PSI, swap, scheduler, and engine allocator counters.

## Why v0.25.1 is not a memory fix

The official v0.25.0-to-v0.25.1 comparison contains only two commits:

1. FFmpeg/TorchCodec launch handling (#47888);
2. mixed-dtype all-reduce RMSNorm fusion (#48330).

The changed files are unrelated to the observed long-prefill/runtime-memory path. AEON v0.25.0 and v0.25.1 image labels both carry the same `uma-clamp` revision claim. The v0.25.1 label adds MRv2, DSpark, and TP2 flags, but declares no new runtime-memory clamp.

Therefore:

- the pulled v0.25.1 digest is not deployed evidence;
- a version bump cannot be marketed as the fix;
- any candidate must pass the same cold A/B matrix and immutable-image attestation;
- source risk from #44490 and #48483 remains separate from this event attribution, and neither tag contains those changes.

## Tracked source drift requiring later integration

A later integration broker must reconcile tracked deployment guidance that advertises `max-num-batched-tokens=32768` plus fixed `15360M` KV and unit comments that still refer to a pinned 15 GiB profile. The immutable live generation instead uses batch 4,096 and AUTO KV utilization 0.355.

This research-only transaction does not edit deployment guidance, units, scripts, or tests. The drift must **not** be “resolved” by changing the unit to batch 32,768.

A source-derived counterfactual shows why:

| Known temporary class | Batch 4,096 | Batch 32,768 | Increase |
|---|---:|---:|---:|
| Gated DeltaNet known simultaneous lower bound | about 336 MiB | about 2,352 MiB | about 2,016 MiB |
| DFlash fixed hidden buffer | 40 MiB | 320 MiB | 280 MiB |
| Combined known increase | — | — | **2,296 MiB = about 2.24 GiB** |

That lower bound excludes q/k/v, MLP, target/drafter workspace, casts, and allocator overhead. It is a **future recipe counterfactual**, not a current staged delta and not a production sizing model. Any such recipe requires a cold canary; it is not a production default.

## Phased mitigation and experiment plan

No phase below authorizes a live change now.

### Phase 0 — containment and source closure

1. Make guard port 18009 the only externally reachable text ingress.
2. Bind raw vLLM port 18010 to loopback for local guard and monitor access.
3. Set the guard's static context override to 262,144 and require context preflight.
4. Implement token-weighted admission over active input plus reserved output obligations.
5. Repair monitor authority: raw vLLM metrics, nonempty scheduler fields, 1-second host/cgroup/NVML/PSI/swap collection, and per-PID identity.
6. Introduce the predictive watermark formula. Treat 17.07 GiB as only the current no-margin example; do not approve it as policy.
7. Reconcile tracked documentation/comments to immutable intended source without changing batch to 32,768.

### Phase 1 — transactional cold candidate, embedding stays up

Use a text-only source-first stop/start transaction with exact rollback. Keep embedding running throughout; do not couple or cycle co-resident services. Every variant starts from a cold text generation so retained state cannot contaminate comparison.

Collect at 1-second cadence:

- host used/available, swap, and memory PSI;
- text cgroup current/peak/swap/events;
- API and engine RSS/PSS/HWM;
- per-PID NVML memory with stable PID/CID identity;
- scheduler running/waiting/KV/prefix/preemption counters;
- engine-side `torch.cuda.memory_allocated`, `memory_reserved`, and maximum counters;
- a canary-only `memory_snapshot` captured around first-touch transitions.

Run this workload matrix in controlled order:

1. one approximately 88k cold-prefix miss, then an exact repeat;
2. two distinct approximately 88k prompts concurrently — not three or four initially;
3. agentic branch-churn shapes with changing prefixes;
4. a single 262,144-token contract-boundary case;
5. mixed smaller shapes after the large-shape wave.

Advance concurrency only after the prior cold and repeat waves satisfy the fail-closed gate.

### Phase 2 — cold A/B isolation

Run every variant from a new cold text generation:

1. `max_num_batched_tokens`: 4,096 baseline versus 2,048 and 1,024 candidates for predictability;
2. DFlash: `K=10`, lower K, and completely off;
3. graph mode: `FULL_DECODE_ONLY` versus CUDA graph `NONE` versus enforce-eager;
4. allocator configuration experiments with captured allocator counters/snapshots;
5. diagnostic idle `empty_cache` only inside the disposable canary.

Cautions:

- `K=0` may still retain drafter obligations, as reflected in the context-length-aware DFlash RFC; prove actual image/process memory rather than assuming the draft unloaded.
- Removing speculation could reclaim the 3.22 GiB draft weights only if the drafter is truly absent; benchmark latency, throughput, and output QoS before considering it.
- `empty_cache` can release unused cached blocks and help diagnose retention/fragmentation. It is not a cure for execution peak and must not be a production request-path dependency.
- Do not lower KV below a block-aligned 262,144-token single-context contract plus measured headroom.
- Batch sizes, DFlash K values, and graph modes above are experiment candidates, not approved policy.

## Fail-closed acceptance and rollback gate

Keep readiness closed until all of these are true on one immutable candidate generation:

1. representative worst-shape cold warmup completed before external readiness;
2. repeated warmed waves on the same PID establish no material new host or per-PID NVML high-water;
3. no interval swap growth, memory-pressure event, guardian action, new preemption, or request-wave JIT/compiler activity;
4. per-PID NVML, host available, text cgroup, and RSS/PSS converge to an explained plateau without hiding ledger differences;
5. allocator allocated/reserved/max counters and canary snapshots explain the residual step class sufficiently to size it;
6. the final measured residual step is substituted into the predictive watermark formula with an explicit safety margin;
7. raw port 18010 is unreachable remotely while local guard and monitoring remain healthy;
8. context preflight no longer records `skipped_no_context_window`;
9. `vllm_perf` running/waiting/KV/token/throughput fields are nonempty and schema-validated;
10. tracked source, image digest, runtime version, command line, PID, CID, and loaded unit identity form one auditable chain;
11. the 262,144-token contract passes without reducing block-aligned KV capacity below the contract plus headroom.

**Rollback trigger:** any additional material repeat-wave high-water, swap growth, PSI pressure, guardian action, preemption delta, unplanned request-wave JIT, allocator OOM/hang, telemetry gap, identity mismatch, or context-contract failure closes readiness and fails the candidate. Restore the exact previous committed text image/config through a text-only stop/start transaction and verify its immutable identity and guard path. Embedding remains running. Do not use `empty_cache` to waive a failure.

No numeric high-water tolerance or final safety margin is approved in this note. Those values must be chosen from the cold-canary distribution, not from a single event.

## Evidence commands and data provenance

This note was authored from the supplied 03:08 evidence bundle. The documentation transaction itself did not query or mutate the live host. The underlying investigation used these read-only evidence classes:

- `systemctl --user show` for active timestamp, main PID, and generation/restart fields;
- `docker inspect` for immutable image digest, CID generation, command, publish, HostConfig memory, and OOM adjustment;
- `free(1)` and `/proc/meminfo` for host used/available/swap cross-checks;
- cgroup v2 `memory.current`, `memory.peak`, `memory.swap.current`, and event files;
- `/proc/<pid>/status` and smaps-derived process RSS/PSS/HWM views;
- `nvidia-smi`/NVML per-process memory snapshots;
- `sysmon` and `vllm-perf` CSVs for aligned historical rows and telemetry-gap detection;
- vLLM and proxy journals for ready time, model/KV/graph/JIT telemetry, scheduler statistics, and warnings;
- control-plane `GET /models` and `GET /metrics` only;
- SQLite opened in read-only/query-only mode for content-free attempt counts, usage, status class, size, and timing;
- read-only source inspection of the immutable live image for Mamba, DFlash, and FLA allocation shapes;
- immutable upstream tag comparison and primary issues/pull requests listed below.

Method caveats:

- sysmon has no historical per-PID NVML series and no recorded `MemAvailable` field;
- vLLM-performance scheduler columns are blank because the monitor targets the wrong metrics endpoint/schema;
- histograms support bucket subtraction, not row-level client attribution;
- rootless NAT prevents independent backend client identity;
- current RSS, cgroup, and NVML snapshots cannot reconstruct every transient allocation;
- no generation or stress request was introduced to reproduce the event.

## External primary sources

- [vLLM v0.25.0…v0.25.1 compare](https://github.com/vllm-project/vllm/compare/v0.25.0...v0.25.1)
- Immutable release commits: [v0.25.0 `702f4814fe54fabff350d43cb753ae3e47c0c276`](https://github.com/vllm-project/vllm/commit/702f4814fe54fabff350d43cb753ae3e47c0c276) and [v0.25.1 `752a3a504485790a2e8491cacbb35c137339ad34`](https://github.com/vllm-project/vllm/commit/752a3a504485790a2e8491cacbb35c137339ad34)
- [vLLM PR #44490 — drain `new_block_ids` host growth](https://github.com/vllm-project/vllm/pull/44490); main-branch merge commit [`b4cfbc24d33ca17bc764a75ffe749654654521c1`](https://github.com/vllm-project/vllm/commit/b4cfbc24d33ca17bc764a75ffe749654654521c1)
- [vLLM PR #48483 — bound temporary dummy KV during startup graph profiling](https://github.com/vllm-project/vllm/pull/48483); main-branch merge commit [`1be6e937b2b49bae652370d80294f6171bd7b981`](https://github.com/vllm-project/vllm/commit/1be6e937b2b49bae652370d80294f6171bd7b981)
- [vLLM issue #42317 — hybrid prefix-cache alignment behavior](https://github.com/vllm-project/vllm/issues/42317)
- [vLLM issue #48627 — context-length-aware speculative scheduling RFC](https://github.com/vllm-project/vllm/issues/48627)
- [PyTorch CUDA memory-management notes](https://docs.pytorch.org/docs/stable/notes/cuda.html#cuda-memory-management)

## Running log

- **2026-07-17 03:08 PDT:** Evidence cut. Same-PID post-ready 12.07 GiB retained high-water established; current available memory is below the no-margin predictive watermark. No sustained leak or exact allocator claimed.
- **2026-07-17 documentation transaction:** Research diary and index only. No live service, deployment source, request traffic, or runtime generation changed.

Future evidence must be appended here with its own immutable generation and timestamp. Historical conclusions should not be silently rewritten.
