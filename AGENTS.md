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
  * `18013`: `querit-4b-reranker.service` (raw Querit-4B backend; preserves Qwen3 reranker model aliases for Guard)
  * `18002`: `llm-guard-proxy.service` legacy embedding-compatible listener; only embedding upstream profiles are allowed
  * `18003`: `llm-guard-proxy.service` legacy reranker-compatible listener; only reranker upstream profiles are allowed
  * `18005`: `llm-guard-proxy.service` aggregate listener for chat, embedding, and rerank profiles

## Current Reference Runtime

* Container image for embedding, AEON chat, and the Querit wrapper: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-01-v0.24.0` (`sha256:f6d453d0b4a7ef90eefee486f4ff769cc2e1bb1e206df16d70370da09c02203c`).
* `vllm-embedding.service`: `max-model-len=40960`, `max-num-batched-tokens=8192`, `kv-cache-memory-bytes=5820M`, verified 41,376 KV tokens, Docker `MemoryMax=24G`.
* `vllm-aeon-27b-dflash.service`: DFlash n=10, `kv-cache-dtype=fp8_e4m3`, `attention-backend=TRITON_ATTN`, `max-model-len=262144`, `max-num-batched-tokens=32768`, `kv-cache-memory-bytes=36864M` (36 GiB, about 2.47 full context windows), Docker `MemoryMax=69G`.
* `querit-4b-reranker.service`: `Querit/Querit-4B`, `max-model-len=40960`, `max-batch=8`, BF16, Docker `MemoryMax=18G`; it serves the existing `qwen3-reranker-8b` and `Qwen/Qwen3-Reranker-8B` aliases on port 18013.
* `llm-guard-proxy` routes by request `model` to AEON chat (`aeon-ultimate`, `qwen3.6-27b-decensor-by-aeon`), embedding (`qwen3-embedding-8b`, `Qwen/Qwen3-Embedding-8B`), or the Querit-backed reranker profile (`qwen3-reranker-8b`, `Qwen/Qwen3-Reranker-8B`).
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
* **Admission control**: default/AEON chat concurrency is `8` in-flight and `24`
  queued requests. Embedding and reranker each have independent `8` in-flight
  and `8` queued limits. Generation queue and upstream request deadlines are
  7,200 seconds; AEON first/inter-chunk stall detection uses a 300-second idle
  timeout. Full queues return HTTP `429` with `Retry-After: 10`.
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
  `default_injection_schema = "chat_template_kwargs"`, and
  `no_thinking_marker_policy = "respect_no_thinking_markers"`. The current
  schema reliably controls `enable_thinking`, but its template-level numeric
  budget is not a sampler-enforced hard bound on the deployed vLLM 0.24 stack;
  top-level `thinking_token_budget` support must land in Guard before benchmark
  use. Requests that explicitly send
  `chat_template_kwargs: {"enable_thinking": false}` are preserved as
  no-thinking requests; do **not** change normal chat back to `force_thinking`
  unless callers should lose that opt-out.
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
cp scripts/aeon_healthcheck.sh /home/obj/scripts/
cp scripts/aeon_chat_ready.py /home/obj/.local/bin/
cp scripts/gb10_apply_aeon_querit_profile.sh /home/obj/.local/bin/
cp scripts/gb10_enforce_docker_cgroup_limits.sh /home/obj/.local/bin/
cp scripts/llm_guard_proxy_cached_rebuild.sh /home/obj/.local/bin/
cp scripts/querit_openai_rerank_server.py /home/obj/.local/bin/
cp scripts/sysmon.sh /home/obj/.local/bin/
cp scripts/gb10-swap-guard.sh /home/obj/.local/bin/

# Make executable
chmod +x /home/obj/scripts/*.sh /home/obj/.local/bin/*
```

### 2. Configuration Setup
```bash
# Copy llm-guard-proxy config
cp config/llm-guard-proxy/config.toml /home/obj/.config/llm-guard-proxy/config.toml
```

### 3. Build llm-guard-proxy
```bash
# Build/update the reviewed main branch from a local workspace checkout with a
# persistent Cargo target cache. The script uses CARGO_BUILD_JOBS=1 and
# ionice/nice so rebuilds are safer while the GB10 model stack is resident.
/home/obj/.local/bin/llm_guard_proxy_cached_rebuild.sh
```

The cached rebuild script keeps Cargo build artifacts under
`/home/obj/.cache/cargo-target/llm-guard-proxy-main`, then atomically relinks
`/home/obj/.local/bin/llm-guard-proxy` to the workspace-built release binary.
If the running guard process still points at a deleted old inode after a
standalone rebuild, the script restarts only `llm-guard-proxy.service` and
smokes `/health`; it does not restart any vLLM backend.

### 4. Systemd User Services Installation
```bash
# Create user-level systemd directory if missing
mkdir -p /home/obj/.config/systemd/user/

# Copy all systemd service and timer files
cp systemd/* /home/obj/.config/systemd/user/

# Reload systemd daemon
systemctl --user daemon-reload
```

### 5. Enable and Start Services
```bash
# Start auxiliary services first
systemctl --user enable --now sysmon.service
systemctl --user enable --now gb10-swap-guard.service
systemctl --user enable --now aeon-healthcheck.timer

# Start model stack and shielding proxy
systemctl --user enable --now vllm-embedding.service
systemctl --user enable --now vllm-aeon-27b-dflash.service
systemctl --user enable --now querit-4b-reranker.service
systemctl --user enable --now llm-guard-proxy.service
```

For updates on an already-running GB10, do not restart just one model service when
changing memory/context profiles. Stop the full model stack first to clear stale
pages, then start in dependency order:

```bash
systemctl --user stop querit-4b-reranker.service
systemctl --user stop vllm-aeon-27b-dflash.service
systemctl --user stop vllm-embedding.service

systemctl --user start vllm-embedding.service
systemctl --user start vllm-aeon-27b-dflash.service
systemctl --user start querit-4b-reranker.service
systemctl --user start llm-guard-proxy.service
```

---

## Operational Monitoring & Verification

### Check Services Health & Processes
```bash
# List all running user systemd services
systemctl --user list-units --type=service --state=running

# Check detailed status of core services
systemctl --user status vllm-embedding vllm-aeon-27b-dflash querit-4b-reranker llm-guard-proxy sysmon gb10-swap-guard
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
max_queued_generation_requests = 24
```

The running Rust proxy hot-reloads the config file. Restarting vLLM is not
required for these proxy-only queue/concurrency changes.

---

## Troubleshooting & Recovery

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
docker rm -f vllm-aeon-27b-dflash vllm-aeon-27b-dflash-n12 querit-4b-reranker vllm-embedding

# Restart target systemd service
systemctl --user restart vllm-aeon-27b-dflash.service
```

### 3. OOM / Swap / Low Free Memory
Hard memory contract (ordinary container memory + fail-fast):
- Docker scopes: AEON `MemoryMax=69G`, embedding `24G`, Querit reranker `18G` (**111GiB** total), each with `MemorySwapMax=0` via `gb10_enforce_docker_cgroup_limits.sh`.
- Wrapper units also set a small `MemoryMax`/`MemorySwapMax=0` for the docker CLI only.
- `llm-guard-proxy`: `MemoryMax=2G`, `MemoryHigh=1536M`, `MemorySwapMax=0`.
- `gb10-swap-guard`: stops **reranker** when `MemAvailable < 1GiB` (`GB10_MEM_AVAIL_STOP_GIB`) **or** swap used ≥ `GB10_SWAP_STOP_GIB` (default 12GiB). This sheds non-critical load; it does not lower chat concurrency.

If the swap/memory guard alerts or kills workloads:
```bash
# Check memory allocation
free -h

# Check which processes are consuming top swap/RSS
tail -n 10 ~/log/sysmon_$(date +%Y-%m-%d).csv | awk -F, '{print "Time: "$1" | Top Swap PID: "$38" ("$40"MB)"}'

# Guard log
tail -n 50 ~/log/gb10_swap_guard.log

# Verify live docker scope hard-caps
systemctl --user list-units 'docker-*.scope' --all --no-pager
for s in $(systemctl --user list-units 'docker-*.scope' --no-legend --all | awk '{print $1}'); do
  echo "--- $s"
  systemctl --user show "$s" -p MemoryMax -p MemorySwapMax -p MemoryCurrent -p MemorySwapCurrent
done
```

Note: on GB10 unified memory, Docker/cgroup caps ordinary container memory; NVIDIA EngineCore residency is a separate ledger. The operational goal is **bounded + fail-fast**, not literally zero post-startup allocation.
