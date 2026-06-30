# AGENTS.md — Automated Playbook for gb10 Services

> This file contains actionable deployment instructions, runtime facts, and troubleshooting commands for AI agents operating on `gb10` machines.

## Host & Port Mapping
* **Host Internal IP / Tailscale IP**: `100.105.4.92` (Verify using `ip route` or `ip addr show`)
* **Docker Environment**: Rootless Docker active at `unix:///run/user/1001/docker.sock`.
* **Port Allocations**:
  * `18002`: `vllm-embedding.service` (Qwen3-Embedding-8B)
  * `18003`: `vllm-qwen3-reranker-8b.service` (Qwen3-Reranker-8B)
  * `18010`: `vllm-aeon-27b-dflash-n12.service` (Direct AEON chat endpoint)
  * `18009`: `llm-guard-proxy.service` (Shielding wrapper protecting port 18010)

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
systemctl --user enable --now vllm-aeon-27b-dflash-n12.service
systemctl --user enable --now vllm-qwen3-reranker-8b.service
systemctl --user enable --now llm-guard-proxy.service
```

---

## Operational Monitoring & Verification

### Check Services Health & Processes
```bash
# List all running user systemd services
systemctl --user list-units --type=service --state=running

# Check detailed status of core services
systemctl --user status vllm-embedding vllm-aeon-27b-dflash-n12 vllm-qwen3-reranker-8b llm-guard-proxy sysmon gb10-swap-guard
```

### Retrieve System Resource Log (sysmon output)
```bash
# View last 20 samples from 1Hz monitor
tail -n 20 ~/log/sysmon_$(date +%Y-%m-%d).csv
```

### View Live Service logs
```bash
# View last 50 log lines for chat service
journalctl --user -u vllm-aeon-27b-dflash-n12.service -n 50 --no-pager

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
```

---

## Troubleshooting & Recovery

### 1. CUDA Hang or Service Crash
If `vllm-aeon-27b-dflash-n12.service` hangs or refuses to respond:
```bash
# Restart the chat service (ExecStartPre will automatically purge hung docker containers)
systemctl --user restart vllm-aeon-27b-dflash-n12.service
```

### 2. Manual Docker Cleanups
If a Docker container gets stuck in a dead state and systemd fails to restart:
```bash
# Explicitly force remove the containers
docker rm -f vllm-aeon-27b-dflash-n12 vllm-qwen3-reranker-8b vllm-embedding

# Restart target systemd service
systemctl --user restart vllm-aeon-27b-dflash-n12.service
```

### 3. OOM / Swap Critical
If the swap guard alerts or kills workloads:
```bash
# Check memory allocation
free -h

# Check which processes are consuming top swap/RSS
tail -n 10 ~/log/sysmon_$(date +%Y-%m-%d).csv | awk -F, '{print "Time: "$1" | Top Swap PID: "$38" ("$40"MB)"}'
```
