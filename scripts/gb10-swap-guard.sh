#!/usr/bin/env bash
# GB10 vLLM swap / free-memory guard. Runs locally on gb10 as a user service.
# The detection loop is intentionally faster than telemetry logging so a rapid
# unified-memory collapse cannot fit entirely between two guard samples.
set -euo pipefail

LOG_DIR="${GB10_SWAP_GUARD_LOG_DIR:-$HOME/log}"
LOG="${GB10_SWAP_GUARD_LOG:-$LOG_DIR/gb10_swap_guard.log}"
INTERVAL="${GB10_SWAP_GUARD_INTERVAL:-1}"
SAMPLE_LOG_INTERVAL="${GB10_SWAP_GUARD_SAMPLE_LOG_INTERVAL:-20}"
WARN_GIB="${GB10_SWAP_WARN_GIB:-7.5}"
STOP_GIB="${GB10_SWAP_STOP_GIB:-12}"
MEM_AVAIL_STOP_GIB="${GB10_MEM_AVAIL_STOP_GIB:-1}"
ATTRIBUTION_INTERVAL="${GB10_SWAP_ATTRIBUTION_INTERVAL:-300}"
STOP_RETRY_INTERVAL="${GB10_SWAP_GUARD_STOP_RETRY_INTERVAL:-5}"
DOCKER_STOP_TIMEOUT="${GB10_SWAP_GUARD_DOCKER_STOP_TIMEOUT:-3}"
DOCKER_KILL_AFTER="${GB10_SWAP_GUARD_DOCKER_KILL_AFTER:-1}"
SYSTEMCTL_STOP_TIMEOUT="${GB10_SWAP_GUARD_SYSTEMCTL_STOP_TIMEOUT:-1}"
SYSTEMCTL_KILL_AFTER="${GB10_SWAP_GUARD_SYSTEMCTL_KILL_AFTER:-1}"
ONESHOT="${GB10_SWAP_GUARD_ONESHOT:-0}"
SKIP_DIAGNOSTICS="${GB10_SWAP_GUARD_SKIP_DIAGNOSTICS:-0}"
WAIT_DIAGNOSTICS="${GB10_SWAP_GUARD_WAIT_DIAGNOSTICS:-0}"
MAX_CYCLES="${GB10_SWAP_GUARD_MAX_CYCLES:-0}"
TEST_NOW_START="${GB10_SWAP_GUARD_TEST_NOW_START:-0}"
TEST_NOW_STEP="${GB10_SWAP_GUARD_TEST_NOW_STEP:-0}"
TEST_EVENT_LOG="${GB10_SWAP_GUARD_TEST_EVENT_LOG:-}"
DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/${UID}/docker.sock}"
export DOCKER_HOST

bytes_from_gib() {
    local value="$1" whole fraction denominator=1 gib=$((1024 * 1024 * 1024)) i
    if [[ ! "$value" =~ ^([0-9]+)(\.([0-9]+))?$ ]]; then
        printf 'invalid GiB value: %s\n' "$value" >&2
        return 2
    fi
    whole="${BASH_REMATCH[1]}"
    fraction="${BASH_REMATCH[3]:-0}"
    for ((i = 0; i < ${#fraction}; i++)); do
        denominator=$((denominator * 10))
    done
    printf '%d' "$((whole * gib + fraction * gib / denominator))"
}

WARN_BYTES=$(bytes_from_gib "$WARN_GIB")
STOP_BYTES=$(bytes_from_gib "$STOP_GIB")
MEM_AVAIL_STOP_BYTES=$(bytes_from_gib "$MEM_AVAIL_STOP_GIB")

log() {
    local timestamp line
    if [[ -n "$TEST_EVENT_LOG" ]]; then
        printf 'log %s\n' "$*" >> "$TEST_EVENT_LOG" 2>/dev/null || true
    fi
    printf -v timestamp '%(%Y-%m-%dT%H:%M:%S%z)T' -1
    line="[$timestamp] $*"
    printf '%s\n' "$line" || true
    if [[ -d "$LOG_DIR" ]] || mkdir -p "$LOG_DIR" 2>/dev/null; then
        printf '%s\n' "$line" >> "$LOG" 2>/dev/null || true
    fi
    return 0
}

read_mem_bytes() {
    local key rest value
    local mem_total_kib=0 mem_available_kib=0 swap_total_kib=0 swap_free_kib=0
    while IFS=: read -r key rest; do
        read -r value _ <<< "$rest"
        case "$key" in
            MemTotal) mem_total_kib="${value:-0}" ;;
            MemAvailable) mem_available_kib="${value:-0}" ;;
            SwapTotal) swap_total_kib="${value:-0}" ;;
            SwapFree) swap_free_kib="${value:-0}" ;;
        esac
    done < "${GB10_SWAP_GUARD_MEMINFO_PATH:-/proc/meminfo}"

    local mem_total_bytes=$((mem_total_kib * 1024))
    local mem_available_bytes=$((mem_available_kib * 1024))
    local swap_total_bytes=$((swap_total_kib * 1024))
    local swap_free_bytes=$((swap_free_kib * 1024))
    mem_used=$((mem_total_bytes > mem_available_bytes ? mem_total_bytes - mem_available_bytes : 0))
    mem_avail="$mem_available_bytes"
    swap_used=$((swap_total_bytes > swap_free_bytes ? swap_total_bytes - swap_free_bytes : 0))
    swap_total="$swap_total_bytes"
}

bytes_to_gib() {
    local bytes="$1" gib=$((1024 * 1024 * 1024)) tenths
    tenths=$(((bytes * 10 + gib / 2) / gib))
    printf '%d.%d' "$((tenths / 10))" "$((tenths % 10))"
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
    local reason="${1:-unspecified}" unit_properties=""
    local querit_load_state="unknown" querit_active_state="unknown" key value
    timeout --signal=TERM --kill-after="$SYSTEMCTL_KILL_AFTER" "$SYSTEMCTL_STOP_TIMEOUT" systemctl --user --no-block stop querit-4b-reranker.service >/dev/null 2>&1 || true
    timeout --signal=TERM --kill-after="$DOCKER_KILL_AFTER" "$DOCKER_STOP_TIMEOUT" docker stop --time 2 querit-4b-reranker >/dev/null 2>&1 || true
    timeout --signal=TERM --kill-after="$SYSTEMCTL_KILL_AFTER" "$SYSTEMCTL_STOP_TIMEOUT" systemctl --user --no-block stop vllm-qwen3-reranker-8b.service >/dev/null 2>&1 || true
    timeout --signal=TERM --kill-after="$DOCKER_KILL_AFTER" "$DOCKER_STOP_TIMEOUT" docker stop --time 2 vllm-qwen3-reranker-8b >/dev/null 2>&1 || true
    log "STOP_RERANKER_ATTEMPT: reason=${reason}; stop actions dispatched"
    if unit_properties="$(timeout --signal=TERM --kill-after="$SYSTEMCTL_KILL_AFTER" "$SYSTEMCTL_STOP_TIMEOUT" systemctl --user show querit-4b-reranker.service --property=LoadState --property=ActiveState 2>/dev/null)"; then
        while IFS='=' read -r key value; do
            case "$key" in
                LoadState) querit_load_state="$value" ;;
                ActiveState) querit_active_state="$value" ;;
            esac
        done <<< "$unit_properties"
    fi
    if [[ "$querit_load_state" == "loaded" ]] && [[ "$querit_active_state" == "inactive" || "$querit_active_state" == "failed" ]]; then
        log "STOP_RERANKER_CONFIRMED: reason=${reason}; querit_load_state=${querit_load_state}; querit_active_state=${querit_active_state}"
        return 0
    fi
    log "STOP_RERANKER_RETRY: reason=${reason}; querit_load_state=${querit_load_state}; querit_active_state=${querit_active_state}"
    return 1
}

run_diagnostics_async() {
    if [[ "$SKIP_DIAGNOSTICS" == "1" ]]; then
        return
    fi
    if (( diagnostics_pid > 0 )); then
        if kill -0 "$diagnostics_pid" 2>/dev/null; then
            return
        fi
        wait "$diagnostics_pid" || true
    fi
    (
        log_vmstat
        log_swap_attribution
    ) &
    diagnostics_pid=$!
}

warned=0
stopped_swap=0
stopped_mem=0
last_attribution=0
last_sample_log=0
last_stop_attempt=0
diagnostics_pid=0
cycle_count=0
started_logged=0

while true; do
    read_mem_bytes
    if (( TEST_NOW_STEP > 0 )); then
        now=$((TEST_NOW_START + cycle_count * TEST_NOW_STEP))
    else
        now="${EPOCHSECONDS:-$(date +%s)}"
    fi
    stop_reason=""
    trigger_mem=0
    trigger_swap=0
    if (( mem_avail < MEM_AVAIL_STOP_BYTES )) && (( stopped_mem == 0 )); then
        trigger_mem=1
        mem_avail_gib=$(bytes_to_gib "$mem_avail")
        stop_reason="mem_avail<${MEM_AVAIL_STOP_GIB}GiB (avail=${mem_avail_gib}GiB)"
    fi
    if (( swap_used >= STOP_BYTES )) && (( stopped_swap == 0 )); then
        trigger_swap=1
        swap_used_gib=$(bytes_to_gib "$swap_used")
        if [[ -n "$stop_reason" ]]; then
            stop_reason+="; "
        fi
        stop_reason+="swap>=${STOP_GIB}GiB (used=${swap_used_gib}GiB)"
    fi

    emergency_attempted=0
    if [[ -n "$stop_reason" ]] && (( now - last_stop_attempt >= STOP_RETRY_INTERVAL )); then
        emergency_attempted=1
        last_stop_attempt="$now"
        if stop_reranker "$stop_reason"; then
            if (( trigger_mem == 1 )); then
                stopped_mem=1
            fi
            if (( trigger_swap == 1 )); then
                stopped_swap=1
            fi
        fi
    fi

    if (( started_logged == 0 )); then
        log "GB10 swap/memory guard started warn_swap=${WARN_GIB}GiB stop_swap=${STOP_GIB}GiB mem_avail_stop=${MEM_AVAIL_STOP_GIB}GiB interval=${INTERVAL}s sample_log_interval=${SAMPLE_LOG_INTERVAL}s stop_retry_interval=${STOP_RETRY_INTERVAL}s attribution_interval=${ATTRIBUTION_INTERVAL}s"
        started_logged=1
    fi

    if (( emergency_attempted == 1 )); then
        run_diagnostics_async
    fi

    if (( now - last_sample_log >= SAMPLE_LOG_INTERVAL )); then
        mem_used_gib=$(bytes_to_gib "$mem_used")
        mem_avail_gib=$(bytes_to_gib "$mem_avail")
        swap_used_gib=$(bytes_to_gib "$swap_used")
        swap_total_gib=$(bytes_to_gib "$swap_total")
        log "sample mem_used=${mem_used_gib}GiB mem_avail=${mem_avail_gib}GiB swap_used=${swap_used_gib}GiB swap_total=${swap_total_gib}GiB"
        last_sample_log="$now"
    fi
    if (( swap_used >= WARN_BYTES )) && (( warned == 0 )); then
        warned=1
        log "WARN_SWAP: swap >= ${WARN_GIB}GiB"
        run_diagnostics_async
        last_attribution="$now"
    fi

    if (( swap_used >= WARN_BYTES )) && (( now - last_attribution >= ATTRIBUTION_INTERVAL )); then
        run_diagnostics_async
        last_attribution="$now"
    fi

    if (( swap_used < WARN_BYTES )); then
        warned=0
    fi
    if (( mem_avail >= MEM_AVAIL_STOP_BYTES * 2 )); then
        stopped_mem=0
    fi
    if (( swap_used < STOP_BYTES )); then
        stopped_swap=0
    fi

    cycle_count=$((cycle_count + 1))
    if [[ "$ONESHOT" == "1" ]] || (( MAX_CYCLES > 0 && cycle_count >= MAX_CYCLES )); then
        if [[ "$WAIT_DIAGNOSTICS" == "1" ]] && (( diagnostics_pid > 0 )); then
            wait "$diagnostics_pid" || true
        fi
        break
    fi

    if (( TEST_NOW_STEP == 0 )); then
        sleep "$INTERVAL"
    fi
done
