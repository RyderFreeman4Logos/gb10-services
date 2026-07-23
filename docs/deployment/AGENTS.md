# AGENTS.md — Automated Playbook for gb10 Services

> This file contains actionable deployment instructions, runtime facts, and troubleshooting commands for AI agents operating on `gb10` machines.

Goal: an agent with GB10 operator access (`rootless-docker` and `systemctl --user`) should be able to deploy the same service stack and runtime behavior as the reference host from the files in this repository.

## Host & Port Mapping
* **Host Internal IP / Tailscale IP**: `100.105.4.92` (Verify using `ip route` or `ip addr show`)
* **Docker Environment**: Rootless Docker active at `unix:///run/user/1001/docker.sock`.
* **Port Allocations**:
  * `18009`: `llm-guard-proxy.service` (stable OpenAI-compatible entrypoint for chat, embeddings, and rerank)
  * `18010`: `vllm-aeon-27b-dflash.service` (raw AEON chat backend)
  * `18012`: `vllm-embedding.service` (raw Qwen3-Embedding-8B backend routed by guard)
  * `18013`: `vllm-querit-4b-reranker.service` (canonical raw Querit-4B backend routed by guard)
  * `18002`: `llm-guard-proxy.service` legacy embedding-compatible listener; only embedding upstream profiles are allowed
  * `18003`: `llm-guard-proxy.service` legacy reranker-compatible listener; only reranker upstream profiles are allowed
  * `18005`: `llm-guard-proxy.service` aggregate listener for chat, embedding, and rerank profiles

## Current Reference Runtime

* Tracked vLLM source image for embedding, AEON chat, Querit, and the disabled vLLM reranker fallback: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-16-v0.25.1` (`sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d`; runtime `0.25.1+aeon.sm121a.dflash`).
* Rollback/superseded image retained on GB10: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-14-v0.25.0` (`sha256:18c09e6b80141a530285160781f7fa720a78ef91143b3c15a65a8c9641b44e55`).
* The 0.25.1 AEON build ports the MRv2 `lm_head` sharing fix across DFlash, Eagle, and DSpark, includes vLLM #47888 for torchcodec startup without FFmpeg, and guards the mixed-dtype FlashInfer TP>1 fusion via #48330. AEON calls the DSpark loader fix-covered and TP=2-ready but explicitly leaves TP>1 unvalidated; do not present this source migration as a multi-Spark hardware result.
* `vllm-embedding.service` tracked source contract: BF16 Qwen3-Embedding-8B with 4,096-dimensional output, `max-model-len=32768`, `max-num-batched-tokens=8192`, `max-num-seqs=64`, and `kv-cache-memory-bytes=4800M`. It requests equal 128 GiB Docker memory/swap caps without imposing the obsolete 20 GiB service budget. Its post-start verifier binds full Docker ID/PID/`StartedAt`, `/proc` starttime and canonical Docker scope, scope dev/inode, and authoritative `cgroup.events`, then re-reads the unchanged identity and proves exact `memory.max`, zero `memory.swap.max`, and zero activation-time `memory.swap.current`. The validated 5,820 MiB baseline yielded 41,376 KV tokens; 4,800 MiB projects about 34,124 tokens (4.14% above 32,768) but is not production-verified until an authorized live restart prints at least 32,768 tokens.
* `vllm-aeon-27b-dflash.service`: tracked clean-start v0.25.1 reference (not a live-production activation claim): DFlash n=10, `kv-cache-dtype=fp8_e4m3`, `attention-backend=TRITON_ATTN`, `max-model-len=262144`, `max-num-seqs=16`, `max-num-batched-tokens=4096`, AUTO KV sizing via `gpu-memory-utilization=0.355` with no explicit `kv-cache-memory-bytes`; clean-start capacity 286,962 KV tokens.
* `vllm-querit-4b-reranker.service`: single canonical BF16 pooling production owner on `18013`, with a 32,768-token context, 4,800 MiB KV cache, equal 18 GiB Docker memory/swap caps, and the live-proven AEON scheduler profile `--max-num-batched-tokens 16384`, `--max-num-seqs 32`, `--max-num-partial-prefills 1`, and `--max-long-partial-prefills 1`. Every startup completes the bounded rerank-readiness probe before its unit-owned generation verifier runs; that verifier binds the exact Docker generation without querying the still-starting `Type=simple` service's active state.
* `vllm-qwen3-reranker-8b.service`: BF16 pooling, `max-model-len=40960`, `max-num-batched-tokens=40960`, `kv-cache-memory-bytes=5820M`, verified 41,376 KV tokens.
* `llm-guard-proxy` routes by request `model` to AEON chat (`aeon-ultimate`, `qwen3.6-27b-decensor-by-aeon`), embedding (`qwen3-embedding-8b`, `Qwen/Qwen3-Embedding-8B`), or reranker (`qwen3-reranker-8b`, `Qwen/Qwen3-Reranker-8B`).
* `llm-guard-proxy` uses a shielded AEON retry ladder for chat: max thinking, bounded thinking, then no-thinking direct streaming relay if prior streaming attempts trip the loop guard. The legacy 18002/18003 ports are guard-owned downstream listeners, not raw vLLM publishes.
* `llm-guard-proxy` also hot-reloads `config.toml`. Use `[server]` to change default/chat request parallelism and per-`[[upstreams]]` `max_in_flight_requests` / `max_queued_generation_requests` to tune embedding/reranker independently without restarting vLLM, trading total throughput against single-stream latency.

### llm-guard-proxy Enabled Features

The reference guard config intentionally enables the practical production
features from `llm-guard-proxy/deploy/gb10/config.toml` while keeping
chat-only mutation disabled for embedding/reranker profiles:

* **Named upstream routing**: `aeon-chat`, `qwen3-embedding-8b`, and
  `qwen3-reranker-8b` are explicit `[[upstreams]]` profiles selected by the
  request `model` field. The default and aggregate listeners still use port
  `18009`/`18005`; legacy `18002` only allows the embedding profile and legacy
  `18003` only allows the reranker profile.
* **Admission control**: default/chat concurrency is `4` in-flight and `4`
  queued requests. Embedding and reranker each have independent `8` in-flight
  and `8` queued limits. Guard workflow alias/pre/post execution has a separate
  hard limit of `4` in-flight executions. Full queues return HTTP `429` with
  `Retry-After: 10`.
* **Control-plane headroom**: `max_control_plane_in_flight_requests = 128`, so
  health/metrics/debug traffic is not starved by generation work.
* **Metadata discovery/enrichment**: upstream model metadata discovery and
  response enrichment are enabled with `input_token_safety_margin = 512`.
* **Chat hot-restart/stall protection**: AEON chat has hot-restart readiness
  probes plus upstream stall detection. Pooling profiles explicitly set
  `hot_restart.enabled = false` because the chat probe is not valid for
  embedding/reranker endpoints.
* **Chat parameter override**: AEON chat overrides requests to the service-unit
  sampling defaults (`temperature = 0.6`, `top_p = 0.95`, `top_k = 20`) and
  `max_tokens = 50000`. Embedding/reranker disable parameter override.
* **Thinking policy**: normal chat uses `mode = "bounded_thinking"`,
  `budget_tokens = 32768`, `max_tokens = 50000`,
  `default_injection_schema = "vllm_native"`, and
  `no_thinking_marker_policy = "respect_no_thinking_markers"`. Guard preserves
  `chat_template_kwargs.enable_thinking` for explicit enablement or opt-out and
  emits the effective numeric budget as the top-level `thinking_token_budget`.
  No-thinking requests remove any positive native thinking budget. Do **not**
  change normal chat back to `force_thinking` unless callers should lose that
  opt-out.
* **Shielded retry ladder**: retry remains enabled with max-thinking,
  bounded-thinking, and no-thinking ladder steps. The thinking ladder steps
  also respect no-thinking markers, so a client opt-out is preserved during
  retries.
* **Loop guard**: enforce mode is enabled, including semantic reasoning-loop
  detection (`reasoning_semantic_detection_enabled = true`) and embedding-backed
  self-loop scoring through the local Qwen3-Embedding-8B service. Reasoning-loop
  failures use `on_reasoning_loop = "bounded_answer_from_cot"`, which retries
  from a bounded private pre-loop CoT prefix before falling back to the normal
  retry ladder.
* **Observability**: SQLite observability, Prometheus metrics, upstream health
  probing, debug summaries, and raw observability payload capture are enabled.
  `/debug/recent-requests` currently has no admin token configured; treat it as
  Tailscale-private metadata/debug output.
* **Evidence ledger**: evidence recording runs in quality-debug mode for loop
  detector improvement. Ordinary attempts store redacted raw payloads and
  selected request headers (`include_raw_payloads = true`,
  `include_request_headers = true`). Shadow evidence is enabled for looped
  attempts with bounded-thinking, no-thinking, and CoT-salvage comparison
  attempts; the original looping attempt is also kept running for evidence so
  imperfect loop detection can be audited. Paired comparison sampling is enabled
  for 100% of successful primary requests across max-thinking,
  bounded-thinking, and no-thinking variants, storing redacted raw input,
  output, and reasoning/CoT for offline quality comparison. Evidence retention
  is increased to a 10 GiB/200k-record envelope and paired raw artifacts retain
  up to 8 GiB or 14 days.
* **Streaming heartbeat and Cloudflare mode**: SSE heartbeats are emitted every
  15 seconds and `[cloudflare].enabled = true`.

---

## Deployment & Setup

Run these commands sequentially to deploy the stack from this repository:

### 1. Script Provisioning
```bash
# Ensure target folders exist
mkdir -p /home/obj/scripts /home/obj/.local/bin /home/obj/.config/llm-guard-proxy /home/obj/log

# Copy all scripts
cp scripts/aeon_vllm_wrapper.py /home/obj/scripts/
cp scripts/aeon_hang_guard.py /home/obj/scripts/
install -m 0755 scripts/aeon_text_stop_start.sh /home/obj/scripts/aeon_text_stop_start.sh
cp scripts/aeon_chat_ready.py /home/obj/.local/bin/
cp scripts/gb10_check_mem_available.sh /home/obj/.local/bin/
cp scripts/llm_guard_proxy_cached_rebuild.sh /home/obj/.local/bin/
cp scripts/llm_guard_proxy_publish_cgroup_registration.sh /home/obj/.local/bin/
install -m 0644 scripts/gb10_verify_vllm_no_swap_core.py /home/obj/.local/bin/gb10_verify_vllm_no_swap_core.py
install -m 0755 scripts/gb10_verify_vllm_no_swap.sh /home/obj/.local/bin/gb10_verify_vllm_no_swap.sh
install -m 0755 scripts/gb10_lifecycle.sh /home/obj/.local/bin/gb10_lifecycle.sh
cp scripts/sysmon.sh /home/obj/.local/bin/

# Make executable
chmod +x /home/obj/scripts/*.sh
chmod +x /home/obj/.local/bin/aeon_chat_ready.py \
  /home/obj/.local/bin/gb10_check_mem_available.sh \
  /home/obj/.local/bin/llm_guard_proxy_cached_rebuild.sh \
  /home/obj/.local/bin/llm_guard_proxy_publish_cgroup_registration.sh \
  /home/obj/.local/bin/sysmon.sh
```

### 2. Configuration Setup
```bash
# Copy llm-guard-proxy config
cp config/llm-guard-proxy/config.toml /home/obj/.config/llm-guard-proxy/config.toml
# This same config owns the integrated guardian policy.
```

### 3. Build llm-guard-proxy
```bash
# Build/update the reviewed main branch from a local workspace checkout with a
# persistent Cargo target cache. The script uses CARGO_BUILD_JOBS=1 and
# ionice/nice so rebuilds are safer while the GB10 vLLM stack is resident.
/home/obj/.local/bin/llm_guard_proxy_cached_rebuild.sh
```

The cached rebuild script keeps Cargo build artifacts under
`/home/obj/.cache/cargo-target/llm-guard-proxy-main`, then atomically relinks
`/home/obj/.local/bin/llm-guard-proxy` to the workspace-built release binary.
It explicitly builds with Cargo feature `guard`; production must not inherit the
package's empty default feature set.
If the running guard process still points at a deleted old inode after a
standalone rebuild, the script restarts only `llm-guard-proxy.service` and
smokes `/health`; it does not restart any vLLM backend.

### 4. Verify the integrated guardian profile

The proxy config must enable only `aeon-text` with `mem_threshold_gib = 5`,
`kill_action = "cgroup-kill"`, `poll_interval_secs = 3`, and
`registration_file = "text-cgroup.v1"`. The guardian is built into the proxy;
there is no separate guardian binary or service to install.

### 5. Systemd User Services Installation
```bash
# Create user-level systemd directory if missing
mkdir -p /home/obj/.config/systemd/user/

# Install tracked services.
install -m 0644 systemd/llm-guard-proxy.service \
  systemd/vllm-querit-4b-reranker.service systemd/sysmon.service \
  systemd/vllm-aeon-27b-dflash.service systemd/vllm-embedding.service \
  systemd/vllm-qwen3-reranker-8b.service \
  /home/obj/.config/systemd/user/

# Reload systemd daemon
systemctl --user daemon-reload
```

### 6. Enable and Start Services
```bash
systemctl --user enable --now sysmon.service

# Start model services and the proxy independently.
systemctl --user enable --now vllm-embedding.service
systemctl --user enable --now vllm-aeon-27b-dflash.service
systemctl --user disable --now vllm-qwen3-reranker-8b.service
systemctl --user enable --now vllm-querit-4b-reranker.service
systemctl --user enable --now llm-guard-proxy.service
```

### Model lifecycle audit and investigation lock

For an already-running model service, do **not** call `systemctl --user stop`,
`restart`, or a stack-cycle helper directly. Use the installed
`/home/obj/.local/bin/gb10_lifecycle.sh` wrapper for every manual model
stop/start. It accepts only the tracked AEON, embedding, and reranker units,
requires a content-free `--actor` and `--reason` token, serializes operations,
and writes owner-only records to the fixed production path
`/home/obj/.local/state/gb10-lifecycle/lifecycle-audit.log`. Every record
contains UTC and monotonic timestamps, UID, PID, event, actor, reason, and
outcome. Lifecycle request/result records also contain action and unit; failed
results and failed investigation closes include `exit_status`, and blocked
requests identify the active investigation. The accepted request is written
before `systemctl` executes.

Before a benchmark investigation or other evidence-preserving diagnosis, create
the durable marker first:

```bash
/home/obj/.local/bin/gb10_lifecycle.sh investigation-begin \
  --actor benchmark-forensics --reason incident-26
```

While that investigation marker exists, the wrapper rejects **both** model stops
and starts, including the Guard-configured AEON local-recovery helper. End the
investigation explicitly only after recording the conclusion/authorization; do
not delete the marker by hand:

```bash
/home/obj/.local/bin/gb10_lifecycle.sh investigation-end \
  --actor benchmark-forensics --reason evidence-captured
```

An authorized maintenance cycle must use two separately auditable operations;
`restart` is deliberately rejected:

```bash
/home/obj/.local/bin/gb10_lifecycle.sh stop \
  --unit vllm-aeon-27b-dflash.service \
  --actor maintenance-agent --reason approved-maintenance
/home/obj/.local/bin/gb10_lifecycle.sh start \
  --unit vllm-aeon-27b-dflash.service \
  --actor maintenance-agent --reason approved-maintenance
```

`aeon_text_stop_start.sh` and `gb10_restart_text_safe.sh` route their AEON and
reranker stop/start calls through this wrapper. The independently locked,
no-argument embedding activation transaction retains its own durable receipts;
do not replace that transaction with manual lifecycle commands.

### Generation-bound vLLM no-swap authority

Every tracked vLLM backend must omit the unsupported vLLM `--swap-space` flag,
contain Docker `--memory-swappiness 0`, and use equal Docker `--memory` /
`--memory-swap` values. Those Docker source args are intent, not runtime proof.
The public `gb10_verify_vllm_no_swap.sh` wrapper
opens its fixed non-executable `gb10_verify_vllm_no_swap_core.py` companion with
`O_NOFOLLOW`, verifies owner/link/mode/identity and its embedded SHA-256, then
executes the exact bytes read from that descriptor. The verifier accepts the
tracked unit plus the exact container name and binds the full CID, PID, Docker
`StartedAt`, `/proc/<pid>/stat` starttime, the single canonical unified
`/proc/<pid>/cgroup` path ending in `docker-<full-cid>.scope`, the cgroup
directory device/inode, and authoritative `cgroup.events` population. It then
requires the exact expected `HostConfig.Memory`, `MemorySwap == Memory`, exact
unit/container process argv identity, `memory.max`, `memory.swap.max == 0`, and
activation-time `memory.swap.current == 0`, and rejects any identity change on
re-read.
`systemctl --user show ... ControlGroup` is only a cross-check; neither it nor a
parent-service `MemorySwapMax=0` substitutes for Docker-generation attribution.

Verifier and cleanup calls in production units run through fixed absolute tools
under `env -i` with the fixed rootless socket. The explicit `--test-only` seam is
not present in units. `ExecStartPre`, `ExecStop`, and `ExecStopPost` invoke the
fixed `gb10_verify_vllm_no_swap.sh --cleanup` authority. Each unit uses a private
cidfile, and cleanup validates full CID plus exact name-to-ID before stop/remove;
malformed, stale, or replacement identity fails closed. Install the digest-bound
pair source-identically before units, core `0644` before wrapper `0755`: the
Querit deployment owner includes both in its fixed transactional file bundle,
and the embedding activator snapshots prior absence/bytes/mode for both,
publishes core before wrapper, re-attests both authorities, and restores both
exactly on rollback.

Memory recovery is integrated, source-first, and text-only. The text unit's
post-start publisher validates the immutable Docker CID and exact rootless
`app.slice` scope, then atomically publishes owner-only `text-cgroup.v1`.
`llm-guard-proxy` periodically replaces its pre-opened descriptors when that
registration changes. It releases its touched reserve and writes directly to
`cgroup.kill` below 5 GiB `MemAvailable`; no Docker/systemd subprocess is needed
on the emergency path.

The text unit uses `app.slice` and `Restart=always`. It has no
`Requires=`/`Wants=` edge to embedding. Both rerankers have no `Requires=`,
`BindsTo=`, `PartOf=`, or text-readiness gate, while Guard has ordering only.
Embedding and reranker are never guardian targets. `sysmon.service` remains
observer-only and must never stop, kill, or restart a model.

For updates on an already-running GB10, the embedding 32K profile is a
source-first **single-unit** transaction. Never use the general stack installation
commands above for this change, never copy/sync unrelated branch files, and
never stop, start, or restart text, either reranker, or the proxy. Run only the
reviewed no-argument production entry point from the repository root in an
approved maintenance window:

```bash
scripts/gb10_activate_embedding_profile.sh
```

Do not create loose pre-state files or manually copy, reload, restart, curl, or
invoke an alternate verifier. The locked activator owns the private durable
snapshot: exact prior unit bytes/mode or explicit absence, stable embedding
systemd generation, stable text/reranker generations, and fixed outputs from
both aliases. It rejects canonical source mutation, installs atomically,
daemon-reloads, and restarts only `vllm-embedding.service`. Readiness requires a
new `InvocationID`, PID, and monotonic start generation before the activator
invokes the canonical strict verifier with its transaction evidence.

Commit requires the exact effective unit and Docker argv for 32,768 tokens,
4,800 MiB KV, equal 128 GiB Docker memory/swap caps, and the generation-bound
no-swap proof above without imposing the obsolete 20 GiB service budget; a PID
in the exact current cgroup/container generation; all intended finite engine-process metrics; at
least 32,768 startup KV tokens; exact aliases; finite nonzero 4,096-dimensional
vectors; `0.99999` repeat/alias fixture stability; unchanged fixture-neighbor
ordering; and unchanged text/reranker generations. These are fixture invariants,
not a claim of general embedding quality. The profile remains projected, not
production-verified, until the private activation transaction is durably
`committed`.

`HUP`, `INT`, `TERM`, exceptions, timeouts, stale pre-commit transactions, and
receipt failures restore exact prior unit state, reload, and restart only
embedding. Activation requires the embedding service to be active/running;
rollback proves a fresh stable restored generation/readiness and unchanged
neighbors. `rollback_failed` remains private and resumable; the next lock holder
recovers it before any source preflight. A durable `committed` phase is
authoritative and is never rolled back; an activation receipt explicitly
requires that phase and is not a commit claim by itself. Evidence is owner-only under
`$HOME/.local/state/gb10-embedding-activation/`. Never recover by cycling the
stack or replaying copy/reload/restart fragments.

---

## Operational Monitoring & Verification

### Check Services Health & Processes
```bash
# List all running user systemd services
systemctl --user list-units --type=service --state=running

# Check detailed status of core services
systemctl --user status vllm-embedding vllm-aeon-27b-dflash vllm-querit-4b-reranker \
  llm-guard-proxy sysmon
```

### Retrieve System Resource Log (sysmon output)
```bash
# View last 20 samples from 1Hz monitor
tail -n 20 ~/log/sysmon_$(date +%Y-%m-%d).csv
```

### View Live Service logs
```bash
# View last 50 log lines for chat service
journalctl --user -u vllm-aeon-27b-dflash.service -n 50 --no-pager

# View last 50 log lines for proxy wrapper
journalctl --user -u llm-guard-proxy.service -n 50 --no-pager
```

### Test API Endpoints
```bash
# Test Embedding Endpoint via llm-guard-proxy
curl -s -X POST http://100.105.4.92:18009/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-embedding-8b","input":"hello"}'

# Test Chat via llm-guard-proxy
curl -s -X POST http://100.105.4.92:18009/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "aeon-ultimate", "messages": [{"role": "user", "content": "你好"}]}'

# The shielded chat retry ladder intentionally starts with max-thinking. A
# trivial probe such as "Say OK" may return content like "\n\nOK" plus
# `reasoning_content`; this is the model/parser's final-answer separator, not a
# failed health check.

# Test explicit no-thinking passthrough. This should return a final answer
# without `reasoning_content`; guard debug metadata should show
# thinking_rewrite_reason=caller_no_thinking_marker_passthrough.
curl -s -X POST http://100.105.4.92:18009/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"aeon-ultimate","messages":[{"role":"user","content":"只输出 OK 两个字。"}],"chat_template_kwargs":{"enable_thinking":false},"max_tokens":16}'

# Test Reranker Endpoint via llm-guard-proxy
curl -s -X POST http://100.105.4.92:18009/v1/rerank \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-reranker-8b","query":"hello","documents":["hello world","goodbye"]}'
```

### Hot-reload Chat Parallelism

To tune throughput versus single-stream latency without restarting the slow AEON
vLLM backend, edit `/home/obj/.config/llm-guard-proxy/config.toml` and adjust:

```toml
max_in_flight_requests = 8
max_queued_generation_requests = 8
```

The running Rust proxy hot-reloads the config file. Restarting vLLM is not
required for these proxy-only queue/concurrency changes.

---

## Troubleshooting & Recovery

### 0. v0.25.1 FlashInfer JIT Compilation

The v0.25.1 image (`sha256:c15e2c4b...`) uses FlashInfer 0.6.13 which requires JIT
compilation of 30+ CUTLASS FP4 GEMM kernels on first startup. Without `MAX_JOBS=1`,
ninja compiles in parallel → multiple nvcc/cc1plus procs exhaust UMA → kernel OOM
→ `ninja: build stopped` → `RuntimeError` exit 1.

**This is the #1 cause of text startup failures on v0.25.x.**

The text unit sets `MAX_JOBS=1` + `CMAKE_BUILD_PARALLEL_LEVEL=1` to serialize
compilation. JIT cache is NOT persisted (ephemeral `--rm` container), so a cold
start recompiles for roughly two minutes. The integrated guardian remains active
and enforces the configured 5 GiB `MemAvailable` threshold during startup;
serialized compilation is pressure reduction, not an exemption from that guard.

### 1. CUDA Hang or Service Crash
If `vllm-aeon-27b-dflash.service` hangs or refuses to respond, preserve
content-free evidence first. After explicit authorization and only when no
investigation marker is active, use the audited tracked lifecycle so its
generation-bound cleanup authority remains in control:
```bash
/home/obj/.local/bin/gb10_lifecycle.sh stop \
  --unit vllm-aeon-27b-dflash.service \
  --actor recovery-operator --reason authorized-hang-recovery
/home/obj/.local/bin/gb10_lifecycle.sh start \
  --unit vllm-aeon-27b-dflash.service \
  --actor recovery-operator --reason authorized-hang-recovery
```
Do not use `systemctl restart`.

### 2. Generation-bound cleanup failures

Do not bypass the unit with direct Docker stop, kill, or remove commands.
`ExecStartPre`, `ExecStop`, and `ExecStopPost` all route through the fixed
`gb10_verify_vllm_no_swap.sh --cleanup` authority, which validates the private
cidfile's full CID and exact name-to-ID binding before bounded stop/remove. If it
fails closed, preserve the evidence and inspect the unit journal rather than
removing a possibly replacement container:
```bash
systemctl --user status vllm-aeon-27b-dflash.service --no-pager
journalctl --user -u vllm-aeon-27b-dflash.service -n 100 --no-pager
```

### 3. OOM / Swap Critical
If the swap observer alerts, inspect its evidence and the integrated guardian
messages in the proxy journal. The observer does not mutate service or container
state, while only the integrated text guardian owns emergency recovery:
```bash
journalctl --user -u llm-guard-proxy.service -n 100 --no-pager

# Check memory allocation
free -h

# Check which processes are consuming top swap/RSS
tail -n 10 ~/log/sysmon_$(date +%Y-%m-%d).csv | awk -F, '{print "Time: "$1" | Top Swap PID: "$38" ("$40"MB)"}'
```
