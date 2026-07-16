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
  * `18013`: `vllm-qwen3-reranker-8b.service` (raw Qwen3-Reranker-8B backend routed by guard)
  * `18002`: `llm-guard-proxy.service` legacy embedding-compatible listener; only embedding upstream profiles are allowed
  * `18003`: `llm-guard-proxy.service` legacy reranker-compatible listener; only reranker upstream profiles are allowed
  * `18005`: `llm-guard-proxy.service` aggregate listener for chat, embedding, and rerank profiles

## Current Reference Runtime

* vLLM image for embedding, AEON chat, and the disabled vLLM reranker fallback: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-01-v0.24.0` (`sha256:f6d453d0b4a7ef90eefee486f4ff769cc2e1bb1e206df16d70370da09c02203c`).
* `vllm-embedding.service` tracked source contract: BF16 Qwen3-Embedding-8B with 4,096-dimensional output, `max-model-len=32768`, `max-num-batched-tokens=8192`, `max-num-seqs=64`, `kv-cache-memory-bytes=4800M`, and a 20 GiB no-swap hard cap. The validated 5,820 MiB baseline yielded 41,376 KV tokens; 4,800 MiB projects about 34,124 tokens (4.14% above 32,768) but is not production-verified until a live restart prints at least 32,768 tokens.
* `vllm-aeon-27b-dflash.service`: DFlash n=10, `kv-cache-dtype=fp8_e4m3`, `attention-backend=TRITON_ATTN`, `max-model-len=262144`, `max-num-batched-tokens=32768`, `kv-cache-memory-bytes=15360M`, verified 269,589 KV tokens.
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
install -d -m 0700 /home/obj/.config/gb10-memory-guardian

# Copy all scripts
cp scripts/aeon_vllm_wrapper.py /home/obj/scripts/
cp scripts/aeon_hang_guard.py /home/obj/scripts/
cp scripts/aeon_healthcheck.sh /home/obj/scripts/
cp scripts/aeon_chat_ready.py /home/obj/.local/bin/
cp scripts/gb10_check_mem_available.sh /home/obj/.local/bin/
cp scripts/llm_guard_proxy_cached_rebuild.sh /home/obj/.local/bin/
cp scripts/sysmon.sh /home/obj/.local/bin/
cp scripts/gb10-swap-guard.sh /home/obj/.local/bin/
cp scripts/gb10_stack_recovery.sh /home/obj/.local/bin/

# Make executable
chmod +x /home/obj/scripts/*.sh /home/obj/.local/bin/*
```

### 2. Configuration Setup
```bash
# Copy llm-guard-proxy config
cp config/llm-guard-proxy/config.toml /home/obj/.config/llm-guard-proxy/config.toml
# The guardian transaction below owns its owner-only config, binary, helpers,
# text/reranker units, private backups, rollback, reload, and activation canaries.
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

### 4. Build and install the memory guardian
```bash
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --locked
cargo build --release --locked -p gb10-memory-guardian
```

### 5. Systemd User Services Installation
```bash
# Create user-level systemd directory if missing
mkdir -p /home/obj/.config/systemd/user/

# Install only units outside the guardian/text/reranker transaction.
install -m 0644 systemd/aeon-healthcheck.service systemd/aeon-healthcheck.timer \
  systemd/gb10-swap-guard.service systemd/gb10-stack-recovery.service \
  systemd/llm-guard-proxy.service \
  systemd/sysmon.service systemd/vllm-embedding.service \
  /home/obj/.config/systemd/user/

# Reload systemd daemon
systemctl --user daemon-reload
```

### 6. Enable and Start Services
```bash
# Start auxiliary services first
systemctl --user enable --now sysmon.service
systemctl --user enable --now gb10-swap-guard.service
systemctl --user enable --now aeon-healthcheck.timer

# Complete any separately approved model-service maintenance before opening the
# guardian transaction. The deployer never changes these lifecycles.
systemctl --user enable --now vllm-embedding.service
systemctl --user enable --now vllm-aeon-27b-dflash.service
systemctl --user disable --now vllm-qwen3-reranker-8b.service
systemctl --user enable --now querit-4b-reranker.service
systemctl --user enable --now llm-guard-proxy.service

# Install and immediately activate the complete fail-closed guardian bundle.
# Do not insert model lifecycle, copy, receipt removal, reload, or standalone
# canary commands between these transaction phases.
export GB10_BENCHMARK_EXCLUDED=YES
scripts/gb10_deploy_memory_guardian.sh install
scripts/gb10_deploy_memory_guardian.sh activate
```

Memory recovery is source-first and text-only. The deployer atomically publishes
an owner-only manifest that binds source and installed hashes/modes plus exact
prior artifact and parent-directory bytes, modes, and absences. Install leaves
the guardian disabled in phase `installed`; activation runs bounded disposable
and the read-only configured-target identity check for `aeon-text` /
`text-cgroup.v1`,
starts the guardian unenabled, and
enables it only after both pass. Every failure or signal restores the complete
prior generation before a bounded `daemon-reload`; `rollback_failed` state is
retained for idempotent recovery and can never be activated. Never replace this
transaction with loose copy, removal, reload, canary, or guardian lifecycle
fragments.

The text unit uses `app.slice` and `Restart=on-failure`. It has no
`Requires=`/`Wants=` edge to embedding. Both rerankers have no `Requires=`,
`BindsTo=`, `PartOf=`, or text-readiness gate, while Guard has ordering only.
The Rust guardian is the Tier 1 automatic recovery actor: its allocation-free
text `cgroup.kill` path remains the first response to critical memory pressure.
`gb10-stack-recovery.service` is the separate Tier 2 fail-closed oneshot for a
bounded grace-check failure or failed text restart. It acquires a nonblocking
runtime lock, permits one attempt per boot, snapshots all three model units,
stops text, reranker, and embedding, proves their prior PIDs and cgroups are
gone, and requires at least 40 GiB `MemAvailable` before restoring embedding,
reranker, then text with raw `/v1/models` readiness gates. It is installed but
not enabled as an ordinary boot service; only the reviewed escalation trigger
or an explicitly approved operator action should start it. A failed stop,
release check, start, or readiness gate does not retry the cycle.
`gb10-swap-guard.service` remains observer-only and must never stop, kill, or
restart a model. The guardian deployer itself never changes text, embedding,
reranker, proxy, or Tier 2 coordinator lifecycle.

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
4,800 MiB KV, and a 20 GiB no-swap cgroup/container; a PID in the exact current
cgroup/container generation; all intended finite engine-process metrics; at
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
systemctl --user status vllm-embedding vllm-aeon-27b-dflash querit-4b-reranker \
  llm-guard-proxy gb10-memory-guardian gb10-swap-guard sysmon
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

### 0. v0.25 FlashInfer JIT Compilation

The v0.25 image (`sha256:18c09e6b...`) uses FlashInfer 0.6.13 which requires JIT
compilation of 30+ CUTLASS FP4 GEMM kernels on first startup. Without `MAX_JOBS=1`,
ninja compiles in parallel → multiple nvcc/cc1plus procs exhaust UMA → kernel OOM
→ `ninja: build stopped` → `RuntimeError` exit 1.

**This is the #1 cause of text startup failures on v0.25.**

The text unit sets `MAX_JOBS=1` + `CMAKE_BUILD_PARALLEL_LEVEL=1` to serialize
compilation. Peak memory stays ~7G above floor — well clear of the guardian's
2G kill threshold. JIT cache is NOT persisted (ephemeral `--rm` container);
recompilation adds ~2 min to every cold start but eliminates deployment complexity.

Guardian can stay running during text startup — it only kills at MemAvail < 2G,
and serialized compilation never drops that low.

### 1. CUDA Hang or Service Crash
If `vllm-aeon-27b-dflash.service` hangs or refuses to respond:
```bash
# Restart the chat service (ExecStartPre will automatically purge hung docker containers)
systemctl --user restart vllm-aeon-27b-dflash.service
```

### 2. Manual Docker Cleanups
If a Docker container gets stuck in a dead state and systemd fails to restart:
```bash
# Explicitly force remove the containers
docker rm -f vllm-aeon-27b-dflash vllm-aeon-27b-dflash-n12 vllm-qwen3-reranker-8b vllm-embedding

# Restart target systemd service
systemctl --user restart vllm-aeon-27b-dflash.service
```

### 3. OOM / Swap Critical
If the swap observer alerts, inspect its evidence and the Rust guardian log.
The observer does not mutate service or container state:
```bash
# Check memory allocation
free -h

# Check which processes are consuming top swap/RSS
tail -n 10 ~/log/sysmon_$(date +%Y-%m-%d).csv | awk -F, '{print "Time: "$1" | Top Swap PID: "$38" ("$40"MB)"}'
```
