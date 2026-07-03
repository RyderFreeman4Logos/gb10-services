# AGENTS.md — Automated Playbook for gb10 Services

> This file contains actionable deployment instructions, runtime facts, and troubleshooting commands for AI agents operating on `gb10` machines.

Goal: an agent with GB10 operator access (`rootless-docker` and `systemctl --user`) should be able to deploy the same service stack and runtime behavior as the reference host from the files in this repository.

## Host & Port Mapping
* **Host Internal IP / Tailscale IP**: `100.105.4.92` (Verify using `ip route` or `ip addr show`)
* **Docker Environment**: Rootless Docker active at `unix:///run/user/1001/docker.sock`.
* **Port Allocations**:
  * `18002`: `vllm-embedding.service` (Qwen3-Embedding-8B)
  * `18003`: `vllm-qwen3-reranker-8b.service` (Qwen3-Reranker-8B)
  * `18010`: `vllm-aeon-27b-dflash.service` (Direct AEON chat endpoint)
  * `18009`: `llm-guard-proxy.service` (Shielding wrapper protecting port 18010)

## Current Reference Runtime

* vLLM image for embedding, AEON chat, and reranker: `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-07-01-v0.24.0` (`sha256:f6d453d0b4a7ef90eefee486f4ff769cc2e1bb1e206df16d70370da09c02203c`).
* `vllm-embedding.service`: `max-model-len=40960`, `max-num-batched-tokens=8192`, `kv-cache-memory-bytes=5820M`, verified 41,376 KV tokens.
* `vllm-aeon-27b-dflash.service`: DFlash n=10, `kv-cache-dtype=fp8_e4m3`, `attention-backend=TRITON_ATTN`, `max-model-len=262144`, `max-num-batched-tokens=32768`, `kv-cache-memory-bytes=15360M`, verified 269,589 KV tokens.
* `vllm-qwen3-reranker-8b.service`: BF16 pooling, `max-model-len=40960`, `max-num-batched-tokens=40960`, `kv-cache-memory-bytes=5820M`, verified 41,376 KV tokens.
* `llm-guard-proxy` force-disables Qwen3.6-27B thinking by rewriting request parameters because the AEON thinking-loop issue is not fixed yet: [AEON-7/Qwen3.6-27B-AEON-Ultimate-Uncensored-DFlash#14](https://github.com/AEON-7/Qwen3.6-27B-AEON-Ultimate-Uncensored-DFlash/issues/14).
* `llm-guard-proxy` also hot-reloads `config.toml`. Use it to change chat request parallelism (`max_in_flight_requests`, `max_queued_generation_requests`) without restarting vLLM, trading total throughput against single-stream latency.

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
# Clone the remote repository and compile the binary
git clone https://github.com/RyderFreeman4Logos/llm-guard-proxy.git
cd llm-guard-proxy
cargo build --release
cp target/release/llm-guard-proxy /home/obj/.local/bin/
cd ..
rm -rf llm-guard-proxy
```

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
# Test Embedding Endpoint
curl -s http://100.105.4.92:18002/v1/models

# Test Chat via llm-guard-proxy
curl -s -X POST http://100.105.4.92:18009/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "aeon-ultimate", "messages": [{"role": "user", "content": "你好"}]}'

# Test Reranker Endpoint
curl -s -X POST http://100.105.4.92:18003/v1/rerank \
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
