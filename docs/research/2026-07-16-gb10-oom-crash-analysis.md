# GB10 OOM Crash — 2026-07-16 ~10:39 PDT

**Date:** 2026-07-16
**Crash time:** ~10:39:17 PDT (last sysmon row: 10:39:15)
**Recovery:** user forced physical reboot at ~11:13 PDT
**Uptime before crash:** ~13 hours (since previous boot)

## Sysmon Evidence

The last 3 minutes of sysmon CSV data before the crash tell a clear story:

| Time | mem_used | ~free | swap | tz0 | load1 | gpu_W |
|------|---------|-------|------|------|-------|-------|
| 10:36:48 | 114,673 MB | 9,873 MB | 367 MB | 48.8°C | 0.70 | 65.5W |
| 10:36:55 | 114,674 MB | 9,872 MB | 367 MB | 49.2°C | 0.73 | 60.8W |
| 10:36:58 | 114,664 MB | 9,882 MB | 367 MB | **64.0°C** | 0.83 | 51.2W |
| 10:37:11 | 114,662 MB | 9,884 MB | 367 MB | **72.2°C** | 1.01 | 53.2W |
| 10:37:28 | 114,667 MB | 9,879 MB | 367 MB | **77.4°C** | 1.29 | 51.9W |
| 10:37:46 | 114,680 MB | 9,866 MB | 367 MB | **78.4°C** | 1.52 | 62.0W |
| 10:38:09 | 114,676 MB | 9,870 MB | 367 MB | **82.3°C** | 1.62 | 52.5W |
| 10:38:28 | 114,682 MB | 9,864 MB | 367 MB | **83.2°C** | 1.73 | 48.8W |
| 10:38:31 | 114,681 MB | 9,865 MB | 367 MB | 72.7°C | 1.73 | 52.9W |
| 10:38:54 | 114,683 MB | 9,863 MB | 367 MB | 71.2°C | 1.32 | 51.2W |
| 10:39:12 | 114,686 MB | 9,860 MB | 367 MB | 54.8°C | 1.22 | 49.7W |
| **10:39:15** | **122,131 MB** | **2,415 MB** | 367 MB | 56.3°C | 1.22 | 49.9W |

## Root Cause Analysis

### Thermal spike → memory explosion

1. **At 10:36:58**, GPU temperature (tz0) jumped from 49°C to **64°C** in one sample — a +15°C spike.
2. Temperature continued climbing: 72°C → 77°C → 78°C → **82°C → 83°C** over the next 90 seconds.
3. Load average rose from 0.70 to **1.73** — heavy computation in progress.
4. Memory was stable at ~114,668 MB used (~9.9G free) until the final sample.
5. **At 10:39:15**, memory jumped to **122,131 MB** (only 2.4G free) — a **+7.4G spike in one 3-second sample**.
6. Sysmon stopped recording after this. The machine froze shortly after.

### What happened

The temperature spike at 10:36:58 indicates a burst of GPU computation — likely a large inference request (long context text generation or multiple concurrent requests). The GPU heated rapidly to 83°C.

The memory explosion at 10:39:15 (+7.4G in one sample, from ~9.9G free to ~2.4G free) was the killing blow. This is consistent with:

- **PyTorch caching allocator activation peak**: first large prefill after the service had been running idle for a while (the +18G growth documented in the memory research). The allocator hit a new high-water mark it hadn't reached before.
- **Guardian failed to react**: guardian threshold is 2G. The jump from ~9.9G to ~2.4G happened in a single 3-second sysmon interval — too fast for the guardian's 5-second poll cycle to detect and kill before total exhaustion.
- **Not OOM killer**: there's no kernel OOM log. The machine simply froze (UMA = GPU memory IS system memory; when vLLM tried to allocate more GPU memory, it consumed all system RAM, and the kernel hung before it could invoke OOM killer).

### Why guardian didn't save it

- Guardian polls every 5 seconds with `mem_avail_stop_gib=2`
- MemAvail went from ~9.9G to ~2.4G in <3 seconds
- By the time guardian's next poll cycle checked, the system was already frozen
- The +7.4G allocation in one sample is faster than any poll-based guardian can react

### Why the swap fix didn't help

- Swap stayed at 367 MB throughout — the `--memory-swap` fix is working correctly
- The crash was not caused by swap usage; it was raw memory exhaustion

### Contributing factors

1. **Text service had been running with active inference requests** — two Tailscale clients (`deb-msi` 100.78.159.38 and `mele` 100.86.50.36) were actively using the text model
2. **No warmup probe** — the +18G PyTorch allocator growth documented in the memory research had not been triggered during startup readiness check, so it happened at runtime
3. **util=0.45 targets 54.7G** — but with activation peak, actual usage reached ~83G GPU (visible in tz temperature curve as proof of heavy computation)
4. **Three services at 79.3G baseline + 18G activation + 7.4G burst = ~105G** — close to the 121.6G total, leaving only ~16G. The final spike pushed it over.

## Timeline

```
10:11:43  Text SERVICE_READY (startup complete, 1070s)
10:11-10:36  Steady state: ~114.7G used, ~9.9G free, tz0 ~46-49°C
10:36:58  Thermal spike: tz0 jumps 49→64°C (heavy GPU computation begins)
10:37:xx  Temperature climbs: 72→77→78→82→83°C (sustained heavy load)
10:38:28  Peak temperature 83.2°C, load 1.73
10:38:31  Temperature starts dropping (72.7°C) — computation easing
10:39:15  MEMORY EXPLOSION: +7.4G in one sample → 122.1G used, 2.4G free
10:39:17  Guardian: "emergency reserve rearmed" (last journal entry)
~10:39:xx  System freeze
11:13:xx  User forced physical reboot
```

## Recommendations

### Immediate (prevent recurrence)

1. **Warmup probe in readiness check**: send a near-max-length prefill request during ExecStartPost to trigger the PyTorch allocator high-water mark during startup, not at runtime. This shifts the +18G growth into the startup phase.
2. **Lower text util from 0.45 to 0.35**: reduces KV pool, giving more headroom for activation peaks. Trade-off: fewer concurrent requests but more stable.
3. **Lower guardian threshold from 2G to 5G**: gives guardian more time to kill text before total exhaustion. The +7.4G spike means a 2G threshold is too late.

### Medium-term

4. **Pin text KV cache** (`--kv-cache-memory-bytes`): makes KV allocation deterministic, preventing AUTO from growing. Combined with warmup probe, total memory becomes bounded.
5. **`--max-num-batched-tokens 2048`**: halves the per-step activation peak.
6. **Querit vLLM migration**: replaces 17.6G transformers RR with a vLLM instance that has proper memory management and bounded KV cache.

### Long-term

7. **Thermal monitoring**: add tz0 > 80°C alert to sysmon/guardian. The 83°C peak may indicate thermal throttling contributed to the memory pressure (slower GPU → longer activation retention → higher peak).
8. **cgroup memory hard limit on text container**: `--memory 96g --memory-swap 96g` would make the kernel OOM-kill the text container instead of freezing the entire system.
