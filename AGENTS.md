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

* vLLM image for embedding, AEON chat, and reranker: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-01-v0.24.0` (`sha256:f6d453d0b4a7ef90eefee486f4ff769cc2e1bb1e206df16d70370da09c02203c`).
* `vllm-embedding.service`: `max-model-len=40960`, `max-num-batched-tokens=8192`, `kv-cache-memory-bytes=5820M`, verified 41,376 KV tokens.
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
  and `8` queued limits. Full queues return HTTP `429` with `Retry-After: 10`.
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
  `no_thinking_marker_policy = "respect_no_thinking_markers"`. Requests that
  explicitly send `chat_template_kwargs: {"enable_thinking": false}` are
  preserved as no-thinking requests; do **not** change normal chat back to
  `force_thinking` unless callers should lose that opt-out.
* **Shielded retry ladder**: retry remains enabled with max-thinking,
  bounded-thinking, and no-thinking ladder steps. The thinking ladder steps
  also respect no-thinking markers, so a client opt-out is preserved during
  retries.
* **Loop guard**: enforce mode is enabled, including semantic reasoning-loop
  detection (`reasoning_semantic_detection_enabled = true`).
* **Observability**: SQLite observability, Prometheus metrics, upstream health
  probing, debug summaries, and raw observability payload capture are enabled.
  `/debug/recent-requests` currently has no admin token configured; treat it as
  Tailscale-private metadata/debug output.
* **Evidence ledger**: evidence recording is enabled with metadata only.
  `include_raw_payloads = false`, `include_request_headers = false`, and
  shadow attempts remain disabled.
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
cp scripts/llm_guard_proxy_cached_rebuild.sh /home/obj/.local/bin/
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
# Build/update the reviewed main branch through mise with a persistent Cargo
# target cache. The script uses CARGO_BUILD_JOBS=1 and ionice/nice so rebuilds
# are safer while the GB10 vLLM stack is resident.
/home/obj/.local/bin/llm_guard_proxy_cached_rebuild.sh
```

The cached rebuild script keeps Cargo build artifacts under
`/home/obj/.cache/cargo-target/llm-guard-proxy-main`, then atomically relinks
`/home/obj/.local/bin/llm-guard-proxy` to the mise-managed `ref-main` binary.
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

# Start vLLM stack and shielding proxy
systemctl --user enable --now vllm-embedding.service
systemctl --user enable --now vllm-aeon-27b-dflash.service
systemctl --user enable --now vllm-qwen3-reranker-8b.service
systemctl --user enable --now llm-guard-proxy.service
```

For updates on an already-running GB10, do not restart just one vLLM service when
changing memory/context profiles. Stop the full vLLM stack first to clear stale
pages, then start in dependency order:

```bash
systemctl --user stop vllm-qwen3-reranker-8b.service
systemctl --user stop vllm-aeon-27b-dflash.service
systemctl --user stop vllm-embedding.service

systemctl --user start vllm-embedding.service
systemctl --user start vllm-aeon-27b-dflash.service
systemctl --user start vllm-qwen3-reranker-8b.service
systemctl --user start llm-guard-proxy.service
```

---

## Operational Monitoring & Verification

### Check Services Health & Processes
```bash
# List all running user systemd services
systemctl --user list-units --type=service --state=running

# Check detailed status of core services
systemctl --user status vllm-embedding vllm-aeon-27b-dflash vllm-qwen3-reranker-8b llm-guard-proxy sysmon gb10-swap-guard
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
If the swap guard alerts or kills workloads:
```bash
# Check memory allocation
free -h

# Check which processes are consuming top swap/RSS
tail -n 10 ~/log/sysmon_$(date +%Y-%m-%d).csv | awk -F, '{print "Time: "$1" | Top Swap PID: "$38" ("$40"MB)"}'
```
