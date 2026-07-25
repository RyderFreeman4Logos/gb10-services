# AEON service interruption during sustained chat benchmark — 2026-07-23

- **Evidence cut:** 2026-07-23 06:24 PDT (13:24 UTC)
- **Scope:** content-free incident forensics and recovery only. This record contains no prompts, completions, request bodies, request IDs, client addresses, or credentials.
- **Live mutation at this evidence cut:** none. The recovery transaction is recorded separately after completion.
- **Relationship to final comparison:** This is an interruption/recovery record, not the final quality result. Its failed 174-case run must not be used for quality ranking; the complete content-free three-arm comparison is published at [`../benchmarks/2026-07-25-three-arm-rerun/README.md`](../benchmarks/2026-07-25-three-arm-rerun/README.md).

## Executive result

The available evidence does **not** support a host-kernel OOM, CUDA OOM, or an AEON-container cgroup OOM as the immediate cause of the benchmark outage. Instead, at **06:06:30 PDT** user systemd began *stopping* the AEON unit while the host was under active GPU load. Its expected Docker cleanup then ended the main process with status 137; systemd started a new generation at 06:06:51. The raw backend was unavailable while the replacement loaded, and the benchmark heartbeat later recorded an arm-runner failure at 06:09:17.

The initiating actor for the stop/restart is not present in the available user-systemd, Docker, kernel, or Guard evidence. It must remain an unresolved attribution, rather than be labeled an OOM crash.

A separate material finding is a **no-swap contract violation on the long-lived Querit reranker generation**: its exact Docker cgroup reported `memory.swap.max=max` and `memory.swap.current=979,656,704` bytes (~934 MiB), despite Docker intent showing equal 18 GiB memory/swap caps. That accounts for most of the observed 1.3 GiB host swap use. AEON and embedding exact live cgroups reported `memory.swap.max=0` and `memory.swap.current=0` at the evidence cut.

## Timeline (PDT)

| Time | Content-free evidence |
|---|---|
| 06:05:56 | `sysmon`: 94,078 MiB host memory used; 1,347 MiB swap used; GPU utilization 96%. |
| 06:06:29 | One second before the lifecycle event, `sysmon` was effectively unchanged: 94,073 MiB used, 1,347 MiB swap, GPU utilization 96%; zero swap-out in the sample. |
| 06:06:30 | User systemd journal: `Stopping vllm-aeon-27b-dflash.service`. No preceding kernel/cgroup OOM record was found. |
| 06:06:50–06:06:51 | `sysmon` fell from the pre-stop level to 47,982 MiB used. Systemd recorded the AEON main process exit as status 137 and then immediately began a new generation. The AEON cleanup verifier subsequently logged `cleanup verified`. |
| 06:06:51 | The new AEON unit passed its unit preflight and Docker-generation no-swap verifier; the Guard cgroup publisher registered the new AEON cgroup. Guard also logged that its old target could not be armed because the old cgroup directory no longer existed—an effect of the stop, not evidence that Guard initiated it. |
| 06:07:27 | Replacement model loading: 48,320 MiB host memory used; swap unchanged at 1,347 MiB. |
| 06:09:15–06:09:17 | `sysmon`: 77,501 MiB used while the replacement loaded. The benchmark heartbeat in the supplied run directory recorded `phase=failed`, `arm=B_raw_no_think`, `completed=0`, `remaining=174`, `wave=1`, with `arm_runner_failure=RuntimeError`. |
| 06:24 | Replacement AEON generation was verified `active`; raw `:18010/health` returned HTTP 200. |

The incident-window `sysmon` range was 45,197–94,184 MiB used, with swap fixed at 1,347 MiB. It recorded no material swap-out (maximum 0.00 MiB/s; swap-in peak 0.02 MiB/s). This is inconsistent with a sudden host-memory-exhaustion sequence during the stop.

## Evidence

### Systemd, AEON journal, Docker, and kernel

- Unit chronology is stop → status-137 exit → new start, not a spontaneous exit followed by the unit's configured `RestartSec=30` recovery delay. At the capture, the new generation reported `NRestarts=0`.
- The bounded AEON journal search found no CUDA OOM, Python traceback, allocator OOM, or kernel-OOM text associated with the event. The current Docker container had only replacement-startup/model-load messages; the preceding `--rm` container was no longer available for Docker-log inspection.
- `dmesg` and the kernel journal contained no `oom-killer`, `Killed process`, `Out of memory`, or memory-cgroup OOM record for the incident date. `/proc/vmstat` showed a cumulative `oom_kill=13`, but it has no per-event timestamp and cannot be attributed to this interruption.
- The current AEON cgroup (new generation) was correctly bound to its full Docker scope and showed `memory.max=128 GiB`, `memory.swap.max=0`, `memory.swap.current=0`, and zero `memory.events` OOM / OOM-kill counters. Its observed cgroup `memory.peak` was ~22.70 GiB.

### Guard

- Guard remained active. Its current Prometheus counters were all zero for stuck-watchdog detections, restarts, recovery successes, timeouts, and task failures; queue depth was zero.
- A targeted Guard-journal search found no guardian `cgroup.kill`, stuck-engine watchdog, hot-restart, or recovery-attempt record for this event.
- Downstream upstream-unavailable errors occurred while AEON was not ready. These are impact signals, not a recovery action.

### Co-resident services, cgroups, swap, and GPU snapshot

| Generation at capture | cgroup observation | Interpretation |
|---|---|---|
| AEON replacement | `memory.swap.max=0`, `memory.swap.current=0`, no memory/OOM events | Its live no-swap enforcement was valid after the replacement started. |
| Embedding | `memory.max=128 GiB`, `memory.swap.max=0`, `memory.swap.current=0`, no memory/OOM events; cgroup peak ~14.67 GiB | No direct cgroup-swap evidence of embedding pressure. |
| Querit reranker | `memory.max=18 GiB`, `memory.swap.max=max`, `memory.swap.current=979,656,704`, no OOM events; cgroup peak ~9.76 GiB | Long-lived stale generation violates the live no-swap contract and explains most host swap use. It must be recreated through its committed unit lifecycle. |

A same-time `nvidia-smi` snapshot showed three `VLLM::EngineCore` allocations of 14,740, 20,818, and 39,819 MiB (reranker, embedding, and the new AEON generation respectively, by their generation/PID order). These ledgers are complementary UMA views and must not be summed as independent physical memory totals.

Host memory at the later evidence cut was 79 GiB used / 42 GiB available with 1.3 GiB swap used. The presence of nonzero host swap is a policy violation and operational risk, but the evidence does not make it the causal mechanism for the 06:06 lifecycle stop.

### Sysmon

`sysmon.service` was active and observer-only. Its current-day CSV used the pre-v5 schema (no historical `MemAvailable` field), but supplied 1 Hz host-used-memory, swap, GPU, and swap-I/O samples used above. No sysmon service error was recorded.

## Root-cause assessment

### High confidence

1. **The benchmark lost AEON availability because an external unit stop/restart began at 06:06:30.** The systemd stop marker precedes status 137, the memory drop, the replacement startup, Guard's missing-old-cgroup message, and the benchmark failure heartbeat.
2. **The observed status 137 is part of the stop/cleanup path, not sufficient evidence of OOM.** The journal's ordering, cleanup-verifier success, zero current-generation cgroup OOM events, absence of kernel OOM records, and absence of a runtime traceback/CUDA-OOM record all support that conclusion.
3. **The reranker was using swap in its exact live Docker cgroup.** Equal Docker intent values did not prove the old rootless-Docker generation had zero `memory.swap.max`; direct cgroup evidence disproved it.

### Unresolved / not proven

- Which actor requested the AEON lifecycle transition is not recoverable from the retained user-systemd/Docker/Guard/kernel evidence.
- A model-runtime allocator failure, benchmark request shape, or Guard action is not proven as the initiating cause. No prompt/completion inspection was performed.
- The current 1.3 GiB host swap and reranker no-swap breach are real contributing operational risk, but the incident window did not show a correlated swap-out burst or OOM sequence.

## Impact

- The benchmark did not resume and must remain halted; the supplied heartbeat showed the current arm failed with 0/174 completed cases.
- During AEON replacement loading, the Guard's chat upstream was unavailable and downstream callers saw availability failures.
- No evidence indicates that Guard's stuck-watchdog or memory-guardian recovery mechanisms protected this event.

## Recommended mitigations

1. **Preserve lifecycle attribution.** Add/retain an auditable, content-free record for every AEON stop/restart request (initiator, monotonic time, unit invocation, and intended maintenance reason), so an externally initiated stop cannot be misclassified as an OOM crash.
2. **Benchmark admission gate.** Require AEON systemd `active` plus raw health HTTP 200 before a wave begins, and halt immediately with a lifecycle-interruption reason if the unit leaves `active`.
3. **Recreate and re-attest all backend generations sequentially.** Use documented stop+start lifecycle operations, not `restart`; after every new container, verify its exact rootless cgroup has `memory.swap.max=0` and `memory.swap.current=0`.
4. **Close the reranker no-swap drift.** The freshly recreated reranker must be rejected/left unavailable if its generation still reports `memory.swap.max=max` or nonzero `memory.swap.current`; equal Docker `HostConfig.MemorySwap` alone is insufficient.
5. **Keep Guard out of the recovery transaction.** Do not restart or reconfigure Guard as a workaround. Its zero watchdog counters are evidence that it did not perform this recovery; any future policy change needs separately reviewed evidence.
6. **Improve sysmon provenance.** Deploy/verify the schema that includes `MemAvailable`, preserve generation identity alongside GPU/cgroup samples, and record cgroup swap state for each backend at restart boundaries.

## Evidence commands (content-free)

- `systemctl --user show` for lifecycle timestamps, result, restart count, and cgroup state.
- `journalctl --user -u vllm-aeon-27b-dflash.service` and `journalctl --user -u llm-guard-proxy.service`, filtered to lifecycle/allocator/guardian terms.
- Rootless-Docker `inspect` plus PID-derived cgroup-v2 `memory.*` and `memory.events` files for exact live generations.
- `free -h`, `/proc/meminfo`, `/proc/vmstat`, `dmesg`, and `journalctl -k` for host pressure/OOM evidence.
- `sysmon` numeric telemetry, `nvidia-smi` process-memory snapshot, Guard Prometheus counters, and benchmark heartbeat metadata.

## Recovery result

The authorized recovery used **staggered `stop` + `start` operations only**, never `restart`, in this order:

1. Preflight confirmed the prior AEON replacement was systemd-`active` and raw `:18010/health` returned HTTP 200.
2. Stopped AEON, then Querit reranker, then embedding; each stop was awaited before the next. After all three were down, host `MemAvailable` rose to about **118 GiB** and host swap fell from 1.3 GiB to **412 MiB**.
3. Started embedding first and verified systemd `active` plus raw `:18012/health` HTTP 200 after 110 seconds.
4. Started AEON second while reranker remained stopped; systemd recorded its new generation active at 07:00 PDT, and raw `:18010/health` / `/v1/models` both returned HTTP 200.
5. Started reranker last and verified systemd `active` plus raw `:18013/health` HTTP 200 after 110 seconds.

The three resulting exact Docker generations all had PID-derived cgroups matching their systemd Docker scopes, `memory.swap.max=0`, `memory.swap.current=0`, and zero cgroup OOM/OOM-kill counters. This includes the reranker replacement, which corrected the prior `memory.swap.max=max` / ~934 MiB live-swap violation.

After all three were healthy, the host reported about **30 GiB MemAvailable** and **420 MiB** swap used. The remaining swap users were desktop/graphics, Docker, updater, and monitoring processes; no vLLM backend had nonzero swap in its exact cgroup. This is near-zero relative to the 15 GiB swap device and a 0.9 GiB improvement, but it is not literal zero. No unrelated process was stopped to force it lower.

`llm-guard-proxy.service` was not restarted or reconfigured: its `ExecMainStartTimestamp` / `ActiveEnterTimestamp` remained 01:23:58 PDT and `NRestarts=0`.

## Running log

- **2026-07-23 06:24 PDT:** Forensic evidence cut complete. No source/service configuration was modified and no request payload was read.
- **2026-07-23 07:04 PDT:** Sequential backend recreation complete. All three raw backend health endpoints returned HTTP 200; all fresh backend cgroups proved zero current/max swap and no OOM events. No benchmark process matching `run_one_arm` or `aeon_pod` remained. The supplied heartbeat was updated to `phase=halted` with a non-resume reason.
