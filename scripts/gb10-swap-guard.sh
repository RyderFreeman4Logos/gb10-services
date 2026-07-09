#!/usr/bin/env bash
# GB10 vLLM swap / free-memory guard. Runs locally on gb10 as a user service.
#
# Goals:
# - Prefer fail-fast over thrash: free headroom and swap ceilings, not silent growth.
# - When host free memory is critically low, shed the non-critical reranker first
#   so AEON chat + embedding keep the last ~1GiB of breathing room.
set -euo pipefail

LOG_DIR="${GB10_SWAP_GUARD_LOG_DIR:-$HOME/log}"
LOG="${GB10_SWAP_GUARD_LOG:-$LOG_DIR/gb10_swap_guard.log}"
INTERVAL="${GB10_SWAP_GUARD_INTERVAL:-20}"
WARN_GIB="${GB10_SWAP_WARN_GIB:-7.5}"
STOP_GIB="${GB10_SWAP_STOP_GIB:-12}"
# Stop secondary reranker when MemAvailable falls below this threshold.
MEM_AVAIL_STOP_GIB="${GB10_MEM_AVAIL_STOP_GIB:-1}"
ATTRIBUTION_INTERVAL="${GB10_SWAP_ATTRIBUTION_INTERVAL:-300}"
DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/$(id -u)/docker.sock}"
export DOCKER_HOST

mkdir -p "$LOG_DIR"

bytes_from_gib() {
    python3 - "$1" <<'PY'
from decimal import Decimal
import sys
print(int(Decimal(sys.argv[1]) * 1024 * 1024 * 1024))
PY
}

WARN_BYTES=$(bytes_from_gib "$WARN_GIB")
STOP_BYTES=$(bytes_from_gib "$STOP_GIB")
MEM_AVAIL_STOP_BYTES=$(bytes_from_gib "$MEM_AVAIL_STOP_GIB")

log() {
    printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG"
}

read_mem_bytes() {
    python3 <<'PY'
from pathlib import Path
mem = {}
for line in Path('/proc/meminfo').read_text().splitlines():
    key, value = line.split(':', 1)
    mem[key] = int(value.strip().split()[0]) * 1024
mem_total = mem.get('MemTotal', 0)
mem_available = mem.get('MemAvailable', 0)
swap_total = mem.get('SwapTotal', 0)
swap_free = mem.get('SwapFree', 0)
print(max(0, mem_total - mem_available), mem_available, max(0, swap_total - swap_free), swap_total)
PY
}

bytes_to_gib() {
    awk -v b="$1" 'BEGIN {printf "%.1f", b/1024/1024/1024}'
}

log_vmstat() {
    {
        echo '--- free/swapon/vmstat ---'
        date -Is
        free -h
        swapon --show
        vmstat 1 3
    } 2>&1 | tee -a "$LOG"
}

log_swap_attribution() {
    python3 <<'PY' | tee -a "$LOG"
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
import subprocess

print('--- top swap attribution ---')
containers: dict[str, tuple[str, int]] = {}
try:
    ps = subprocess.run(
        ['docker', 'ps', '--format', '{{.ID}}\t{{.Names}}'],
        check=False,
        text=True,
        capture_output=True,
        timeout=8,
    )
    for line in ps.stdout.splitlines():
        cid, name = line.split('\t', 1)
        insp = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.Pid}}', cid],
            check=False,
            text=True,
            capture_output=True,
            timeout=8,
        )
        try:
            root = int(insp.stdout.strip())
        except ValueError:
            continue
        containers[cid] = (name, root)
except Exception as exc:
    print(f'docker_inspect_error={exc!r}')

parents: dict[int, int] = {}
cmds: dict[int, str] = {}
comms: dict[int, str] = {}
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    pid = int(entry.name)
    try:
        status = (entry / 'status').read_text(errors='replace').splitlines()
        ppid = 0
        for line in status:
            if line.startswith('PPid:'):
                ppid = int(line.split()[1])
                break
        parents[pid] = ppid
        comms[pid] = (entry / 'comm').read_text(errors='replace').strip()
        cmds[pid] = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')[:220]
    except Exception:
        continue

root_by_pid = {root: name for name, root in containers.values()}

def service_for(pid: int) -> str:
    current = pid
    seen: set[int] = set()
    while current and current not in seen:
        seen.add(current)
        if current in root_by_pid:
            return root_by_pid[current]
        current = parents.get(current, 0)
    return 'host_or_other'

rows: list[tuple[int, int, str, str, str]] = []
by_service: defaultdict[str, int] = defaultdict(int)
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    pid = int(entry.name)
    try:
        swap_kib = 0
        for line in (entry / 'status').read_text(errors='replace').splitlines():
            if line.startswith('VmSwap:'):
                swap_kib = int(line.split()[1])
                break
        if swap_kib <= 0:
            continue
        svc = service_for(pid)
        by_service[svc] += swap_kib
        rows.append((swap_kib, pid, svc, comms.get(pid, ''), cmds.get(pid, '')))
    except Exception:
        continue

for svc, kib in sorted(by_service.items(), key=lambda item: item[1], reverse=True):
    print(f'service={svc}\tswap_mib={kib/1024:.1f}')
print('TOP_SWAP_PROCESSES')
for swap_kib, pid, svc, comm, cmd in sorted(rows, reverse=True)[:20]:
    print(f'pid={pid}\tsvc={svc}\tcomm={comm}\tswap_mib={swap_kib/1024:.1f}\tcmd={cmd}')
PY
}

stop_reranker() {
    reason="${1:-unspecified}"
    log "STOP_RERANKER: reason=${reason}; stopping vllm-qwen3-reranker-8b.service"
    systemctl --user stop vllm-qwen3-reranker-8b.service 2>&1 | tee -a "$LOG" || true
    docker stop vllm-qwen3-reranker-8b 2>&1 | tee -a "$LOG" || true
    log_vmstat
    log_swap_attribution
}

log "GB10 swap/memory guard started warn_swap=${WARN_GIB}GiB stop_swap=${STOP_GIB}GiB mem_avail_stop=${MEM_AVAIL_STOP_GIB}GiB interval=${INTERVAL}s attribution_interval=${ATTRIBUTION_INTERVAL}s"
warned=0
stopped_swap=0
stopped_mem=0
last_attribution=0

while true; do
    read -r mem_used mem_avail swap_used swap_total <<< "$(read_mem_bytes)"
    mem_used_gib=$(bytes_to_gib "$mem_used")
    mem_avail_gib=$(bytes_to_gib "$mem_avail")
    swap_used_gib=$(bytes_to_gib "$swap_used")
    swap_total_gib=$(bytes_to_gib "$swap_total")
    log "sample mem_used=${mem_used_gib}GiB mem_avail=${mem_avail_gib}GiB swap_used=${swap_used_gib}GiB swap_total=${swap_total_gib}GiB"

    now=$(date +%s)
    if (( swap_used >= WARN_BYTES )) && (( warned == 0 )); then
        warned=1
        log "WARN_SWAP: swap >= ${WARN_GIB}GiB"
        log_vmstat
        log_swap_attribution
        last_attribution="$now"
    fi

    if (( swap_used >= WARN_BYTES )) && (( now - last_attribution >= ATTRIBUTION_INTERVAL )); then
        log_swap_attribution
        last_attribution="$now"
    fi

    # Free-memory first: shed reranker before the host has no room left.
    if (( mem_avail < MEM_AVAIL_STOP_BYTES )) && (( stopped_mem == 0 )); then
        stopped_mem=1
        stop_reranker "mem_avail<${MEM_AVAIL_STOP_GIB}GiB (avail=${mem_avail_gib}GiB)"
    fi

    if (( swap_used >= STOP_BYTES )) && (( stopped_swap == 0 )); then
        stopped_swap=1
        stop_reranker "swap>=${STOP_GIB}GiB (used=${swap_used_gib}GiB)"
    fi

    if (( swap_used < WARN_BYTES )); then
        warned=0
    fi
    # Allow another free-mem stop after recovery so pressure events can re-trigger.
    if (( mem_avail >= MEM_AVAIL_STOP_BYTES * 2 )); then
        stopped_mem=0
    fi
    if (( swap_used < STOP_BYTES )); then
        stopped_swap=0
    fi

    sleep "$INTERVAL"
done
