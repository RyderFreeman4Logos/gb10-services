# GB10 post-reboot memory baseline (2026-07-15)

## Context

Captured at 27 minutes uptime after a forced power cycle. The machine had
frozen at `MemAvailable = 0.0 GiB` for approximately 13 hours
(01:25 PDT → 14:22 PDT) after a Querit offline replay container exhausted
unified memory. All three model services were restarted manually after SSH
returned.

## Snapshot

### Host memory (`/proc/meminfo`)

| Field              | KiB          | GiB   |
|--------------------|-------------|-------|
| MemTotal           | 127 535 224 | 121.6 |
| MemFree            |   4 510 768 |   4.3 |
| MemAvailable       |  23 135 528 |  22.1 |
| Cached             |  20 448 480 |  19.5 |
| Buffers            |      77 328 |   0.1 |
| Shmem              |    755 460  |   0.7 |
| SReclaimable       |    231 480  |   0.2 |
| SwapTotal          |  16 777 212 |  16.0 |
| SwapFree           |  16 723 228 |  15.9 |
| SwapUsed           |      53 984 |   0.05|

PSI: all zero (avg10/60/300).

### GPU unified memory per process (NVML)

| PID    | Process              | MiB    | Service                          |
|--------|----------------------|--------|----------------------------------|
| 57 686 | VLLM::EngineCore     | 41 458 | aeon-27b-dflash (text)           |
| 85 196 | python3              | 22 439 | querit-4b-reranker               |
| 44 202 | VLLM::EngineCore     | 20 816 | vllm-embedding                   |
| **Total** |                     | **84 713** | **≈ 82.7 GiB**             |

### Host-side RSS (`/proc`, top model processes)

| PID    | RSS MiB | Process                          |
|--------|---------|----------------------------------|
| 57 686 | 4 939   | AEON EngineCore                  |
| 45 601 | 3 776   | AEON wrapper (python3)           |
| 44 202 | 2 436   | embedding EngineCore             |
| 85 196 | 1 716   | Querit python3                   |
| 43 970 | 1 247   | embedding wrapper (python3)      |

Top-20 RSS sum: ~14.8 GiB (includes gnome-shell, dockerd, etc.).

### Docker cgroup memory (`memory.current`)

| Container                  | Docker cap | cgroup current | NVML    |
|----------------------------|-----------|----------------|---------|
| vllm-aeon-27b-dflash-n12   | 69.0 GiB  | 8.2 GiB        | 40.5 GiB|
| querit-4b-reranker         | 18.0 GiB  | 2.2 GiB        | 21.9 GiB|
| vllm-embedding             | 18.6 GiB  | not found      | 20.3 GiB|

### Service status

| Unit                           | Active | PID    |
|--------------------------------|--------|--------|
| vllm-embedding.service         | active | 43 899 |
| querit-4b-reranker.service     | active | 85 128 |
| vllm-aeon-27b-dflash.service   | active | 45 532 |
| gb10-swap-guard.service        | active | 45 728 |
| gb10-memory-guardian.service   | inactive | 0    |

All three raw endpoints returned HTTP 200 on `/v1/models`.

### Thermal

GPU: 48 °C, 11.56 W (idle). ACPI thermal zones: 54–70 °C (trip point 104.8 °C).

## Analysis

### cgroup vs NVML accounting gap

The Docker `memory.current` cgroup counter reports far less than NVML for
every container. This is the core UMA accounting gap:

- **AEON**: cgroup 8.2 GiB vs NVML 40.5 GiB → **32.3 GiB unaccounted**
- **Querit**: cgroup 2.2 GiB vs NVML 21.9 GiB → **19.7 GiB unaccounted**
- **Embedding**: cgroup not found vs NVML 20.3 GiB

Docker `--memory` caps only limit CPU-side RSS allocations tracked by the
cgroup. CUDA unified memory allocations on GB10's UMA architecture are not
fully attributed to the container's cgroup `memory.current`. This means the
Docker cap is not a physical memory protection boundary for GPU workloads.

This was the direct cause of the 2026-07-15 freeze: a replay container with
an 18 GiB Docker cap consumed GPU unified memory far in excess of that cap,
and no cgroup or Docker enforcement prevented it.

### Steady-state memory budget

```
Physical total:     121.6 GiB
GPU UMA total:       82.7 GiB  (68%)
System + daemons:    ~16.3 GiB
Cached (reclaimable): 19.5 GiB
Available:           22.1 GiB
```

The system operates with approximately 22 GiB headroom in steady state.
This is consistent with the pre-freeze Spark Doctor snapshot
(`MemAvailable = 23.5 GiB` at 00:58 PDT). The freeze was not caused by a
slow leak reaching this state—it was the normal baseline. The replay
container's CUDA UMA allocation pushed consumption past 100%.

### Comparison to pre-freeze state

| Metric        | Pre-freeze (00:58) | Post-reboot (14:49) |
|---------------|--------------------|--------------------|
| MemAvailable  | 23.5 GiB           | 22.1 GiB           |
| MemUsed       | 121.6 GiB          | ~121.0 GiB         |
| SwapUsed      | 0.4 GiB            | 0.05 GiB           |
| Services      | 3 active           | 3 active           |
| Guardian      | disabled (wrong target) | inactive      |

The baseline is nearly identical. The system is at its comfortable limit
with all three services running.

### Anti-freeze implications

1. The three services consume ~82.7 GiB UMA in steady state, leaving only
   ~22 GiB for system daemons, page cache, and headroom. Any additional GPU
   workload (offline replay, benchmark, debugging tool) must first stop an
   existing service to make room. On GB10 UMA, a Docker `--memory` cap does
   NOT enforce this for CUDA allocations—the 2026-07-15 freeze happened
   because a replay container with an 18 GiB Docker cap consumed GPU unified
   memory far in excess of that cap while all three services were still
   running.

2. The Rust memory guardian remains inactive. The deployed swap-guard
   (scripts/gb10-swap-guard.sh source) is observer-only, but the live GB10
   instance still runs the old version that actively kills reranker—a
   policy mismatch that was harmless during this incident only because
   Querit was already stopped.

3. The two-tier anti-freeze design (Tier 1: cgroup.kill text → grace check;
   Tier 2: stop all three → restart in priority order) targets this exact
   failure mode: an unexpected process consuming UMA with no registered
   cgroup target for the guardian to kill.

## Artifacts

- Spark Doctor report (last pre-freeze): `~/log/spark-doctor/spark-doctor-20260715T005825-0700.md`
- swap-guard journal (freeze timeline): `journalctl --user -b -1`
- Replay log (truncated at model load): `~/tmp/querit-replay-9af007f/replay-gb10-cd5ff29-20260715t081621z.log`
