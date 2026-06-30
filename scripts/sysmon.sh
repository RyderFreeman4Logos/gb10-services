#!/bin/bash
# gb10 system monitor — logs load, temps, GPU stats every 1s
# Log: ~/log/sysmon_YYYY-MM-DD.csv (rotated daily)
#
# v2 (2026-04-18): INTERVAL 5s → 1s, nvidia-smi runs as a streaming coproc
# instead of fork/exec per sample (previously spent ~30-50ms per sample on
# subprocess + CUDA init, burning ~5% of one core at 1s cadence). Streaming
# mode uses a single persistent nvidia-smi that emits one CSV line per
# second. Header/columns unchanged → same CSV format as v1 for downstream
# analysis continuity.
#
# v3 (2026-06-12): add swap usage and top-5 RSS process IDs. Process IDs are
# stable per process name in ~/log/sysmon_process_names.csv, keeping the main
# 1 Hz CSV compact while preserving enough context to diagnose memory spikes.
#
# v4 (2026-06-13): append disk IO rates, swap-in/out rates, and top-5 swap
# processes. Existing v3 columns keep their order; new fields are appended.

LOG_DIR="$HOME/log"
INTERVAL=1
GPU_LOOP_MS=$((INTERVAL * 1000))
PROC_MAP="$LOG_DIR/sysmon_process_names.csv"

HEADER="timestamp,load_1m,load_5m,load_15m,mem_used_mb,mem_total_mb,swap_used_mb,swap_total_mb,tz0,tz1,tz2,tz3,tz4,tz5,tz6,nvme_c,nvme_s1,nvme_s2,gpu_temp_c,gpu_power_w,gpu_util_pct,gpu_clock_mhz,top1_proc_id,top1_rss_mb,top2_proc_id,top2_rss_mb,top3_proc_id,top3_rss_mb,top4_proc_id,top4_rss_mb,top5_proc_id,top5_rss_mb,disk_read_mb_s,disk_write_mb_s,disk_io_ms_s,swap_in_mb_s,swap_out_mb_s,top1_swap_pid,top1_swap_proc_id,top1_swap_mb,top2_swap_pid,top2_swap_proc_id,top2_swap_mb,top3_swap_pid,top3_swap_proc_id,top3_swap_mb,top4_swap_pid,top4_swap_proc_id,top4_swap_mb,top5_swap_pid,top5_swap_proc_id,top5_swap_mb"

rotate_log() {
    local today
    today=$(date +%Y-%m-%d)
    local logfile="$LOG_DIR/sysmon_${today}.csv"
    if [[ -f "$logfile" ]] && ! head -n 1 "$logfile" | grep -Fxq "$HEADER"; then
        mv "$logfile" "${logfile%.csv}.pre-v4.$(date +%H%M%S).csv"
    fi
    if [[ ! -f "$logfile" ]]; then
        echo "$HEADER" > "$logfile"
    fi
    echo "$logfile"
}

init_proc_map() {
    if [[ ! -f "$PROC_MAP" ]]; then
        echo "proc_id,process_name,first_seen" > "$PROC_MAP"
    fi
}

process_id_for_name() {
    local raw_name="$1"
    local ts="$2"
    local name id
    name="${raw_name//,/_}"
    id=$(awk -F, -v name="$name" 'NR > 1 && $2 == name {print $1; exit}' "$PROC_MAP")
    if [[ -z "$id" ]]; then
        id=$(awk -F, 'NR > 1 && $1 + 0 > max {max = $1 + 0} END {print max + 1}' "$PROC_MAP")
        echo "${id},${name},${ts}" >> "$PROC_MAP"
    fi
    echo "$id"
}

read_vm_counters() {
    awk '
        /^pswpin / {swap_in=$2}
        /^pswpout / {swap_out=$2}
        END {print swap_in "," swap_out}
    ' /proc/vmstat
}

read_disk_counters() {
    awk '
        $3 ~ /^(nvme[0-9]+n[0-9]+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk[0-9]+)$/ {
            read_sectors += $6
            write_sectors += $10
            io_ms += $13
        }
        END {print read_sectors * 512 "," write_sectors * 512 "," io_ms}
    ' /proc/diskstats
}

mb_rate() {
    local current="$1"
    local previous="$2"
    local seconds="$3"
    awk -v current="$current" -v previous="$previous" -v seconds="$seconds" \
        'BEGIN {delta=current-previous; if (delta < 0) delta=0; printf "%.2f", delta/1024/1024/seconds}'
}

page_rate_mb() {
    local current="$1"
    local previous="$2"
    local seconds="$3"
    awk -v current="$current" -v previous="$previous" -v seconds="$seconds" \
        'BEGIN {delta=current-previous; if (delta < 0) delta=0; printf "%.2f", delta*4096/1024/1024/seconds}'
}

counter_rate() {
    local current="$1"
    local previous="$2"
    local seconds="$3"
    awk -v current="$current" -v previous="$previous" -v seconds="$seconds" \
        'BEGIN {delta=current-previous; if (delta < 0) delta=0; printf "%.2f", delta/seconds}'
}

mkdir -p "$LOG_DIR"
init_proc_map

# Find NVMe hwmon path once at startup
NVME_HWMON=""
for hwmon in /sys/class/hwmon/hwmon*/; do
    if [[ "$(cat "${hwmon}name" 2>/dev/null)" == "nvme" ]]; then
        NVME_HWMON="$hwmon"
        break
    fi
done

# Start streaming nvidia-smi as a coproc so we avoid fork/exec per sample.
# --loop-ms=1000 makes nvidia-smi emit one CSV line every second. The coproc
# stdin (${NVS[1]}) is unused; we only consume stdout (${NVS[0]}).
coproc NVS {
    nvidia-smi \
        --query-gpu=temperature.gpu,power.draw,utilization.gpu,clocks.gr \
        --format=csv,noheader,nounits \
        --loop-ms="$GPU_LOOP_MS" 2>/dev/null
}
# Ensure the child nvidia-smi is killed if this script exits.
cleanup() {
    if [[ -n "${NVS_PID:-}" ]] && kill -0 "$NVS_PID" 2>/dev/null; then
        kill -TERM "$NVS_PID" 2>/dev/null
    fi
}
trap cleanup EXIT INT TERM

prev_epoch=$(date +%s)
prev_vm_counters=$(read_vm_counters)
IFS=, read -r prev_swap_in prev_swap_out <<< "$prev_vm_counters"
prev_disk_counters=$(read_disk_counters)
IFS=, read -r prev_disk_read_bytes prev_disk_write_bytes prev_disk_io_ms <<< "$prev_disk_counters"

while true; do
    logfile=$(rotate_log)
    ts=$(date -Iseconds)
    now_epoch=$(date +%s)
    elapsed=$((now_epoch - prev_epoch))
    if (( elapsed <= 0 )); then
        elapsed=1
    fi

    vm_counters=$(read_vm_counters)
    IFS=, read -r swap_in_pages swap_out_pages <<< "$vm_counters"
    disk_counters=$(read_disk_counters)
    IFS=, read -r disk_read_bytes disk_write_bytes disk_io_ms <<< "$disk_counters"

    disk_read_mb_s=$(mb_rate "$disk_read_bytes" "$prev_disk_read_bytes" "$elapsed")
    disk_write_mb_s=$(mb_rate "$disk_write_bytes" "$prev_disk_write_bytes" "$elapsed")
    disk_io_ms_s=$(counter_rate "$disk_io_ms" "$prev_disk_io_ms" "$elapsed")
    swap_in_mb_s=$(page_rate_mb "$swap_in_pages" "$prev_swap_in" "$elapsed")
    swap_out_mb_s=$(page_rate_mb "$swap_out_pages" "$prev_swap_out" "$elapsed")

    prev_epoch="$now_epoch"
    prev_swap_in="$swap_in_pages"
    prev_swap_out="$swap_out_pages"
    prev_disk_read_bytes="$disk_read_bytes"
    prev_disk_write_bytes="$disk_write_bytes"
    prev_disk_io_ms="$disk_io_ms"

    # CPU load
    read -r load1 load5 load15 _ < /proc/loadavg

    # Memory (MB)
    mem_info=$(LANG=C free -m | awk '/^Mem:/ {print $2","$3}')
    mem_total="${mem_info%%,*}"
    mem_used="${mem_info##*,}"

    # Swap (MB). Read /proc/meminfo directly to avoid localized free(1) labels.
    swap_info=$(awk '
        /^SwapTotal:/ {total=int($2 / 1024)}
        /^SwapFree:/ {free=int($2 / 1024)}
        END {print total-free "," total}
    ' /proc/meminfo)
    swap_used="${swap_info%%,*}"
    swap_total="${swap_info##*,}"

    # Thermal zones (millidegrees → degrees)
    tz=()
    for z in /sys/class/thermal/thermal_zone{0,1,2,3,4,5,6}/temp; do
        val=$(cat "$z" 2>/dev/null || echo "0")
        tz+=( "$(awk "BEGIN{printf \"%.1f\", $val/1000}")" )
    done

    # NVMe temps (Composite, Sensor 1, Sensor 2)
    nvme_c="N/A"; nvme_s1="N/A"; nvme_s2="N/A"
    if [[ -n "$NVME_HWMON" ]]; then
        for pair in "temp1_input:nvme_c" "temp2_input:nvme_s1" "temp3_input:nvme_s2"; do
            file="${pair%%:*}"; var="${pair##*:}"
            raw=$(cat "${NVME_HWMON}${file}" 2>/dev/null || echo "")
            if [[ -n "$raw" ]]; then
                eval "$var=$(awk "BEGIN{printf \"%.1f\", $raw/1000}")"
            fi
        done
    fi

    # GPU: read latest line from coproc (non-blocking, 0.5s timeout). If the
    # stream stalled or nvidia-smi died, we fall back to N/A for this sample
    # and continue — log continuity > per-sample GPU readings.
    gpu_temp="N/A"; gpu_power="N/A"; gpu_util="N/A"; gpu_clock="N/A"
    if read -t 0.5 -u "${NVS[0]}" gpu_line 2>/dev/null; then
        IFS=', ' read -r gpu_temp gpu_power gpu_util gpu_clock <<< "$gpu_line"
    fi

    # Top RSS processes. Store process-name dictionary IDs in the hot CSV and
    # keep names in sysmon_process_names.csv to limit 1 Hz log volume.
    top_fields=()
    mapfile -t top_processes < <(ps -eo comm=,rss= --sort=-rss | head -n 5)
    for i in 0 1 2 3 4; do
        if [[ -n "${top_processes[$i]:-}" ]]; then
            read -r proc_name proc_rss_kb <<< "${top_processes[$i]}"
            proc_id=$(process_id_for_name "$proc_name" "$ts")
            proc_rss_mb=$(( (proc_rss_kb + 1023) / 1024 ))
            top_fields+=("$proc_id" "$proc_rss_mb")
        else
            top_fields+=("" "")
        fi
    done

    # Top swap users. Include the PID because multiple vLLM workers share
    # names such as VLLM::EngineCore; the process-name ID keeps CSV volume
    # compact while the PID allows same-day attribution from journal/sysmon.
    top_swap_fields=()
    mapfile -t top_swap_processes < <(
        for status in /proc/[0-9]*/status; do
            pid="${status%/status}"
            pid="${pid##*/}"
            swap_kb=$(awk '/^VmSwap:/ {print $2; exit}' "$status" 2>/dev/null || true)
            if [[ -z "$swap_kb" || "$swap_kb" -le 0 ]]; then
                continue
            fi
            proc_name=$(awk -F'\t' '/^Name:/ {print $2; exit}' "$status" 2>/dev/null || echo unknown)
            printf '%s %s %s\n' "$swap_kb" "$pid" "$proc_name"
        done | sort -nr | head -n 5
    )
    for i in 0 1 2 3 4; do
        if [[ -n "${top_swap_processes[$i]:-}" ]]; then
            read -r swap_kb swap_pid proc_name <<< "${top_swap_processes[$i]}"
            proc_id=$(process_id_for_name "$proc_name" "$ts")
            swap_mb=$(( (swap_kb + 1023) / 1024 ))
            top_swap_fields+=("$swap_pid" "$proc_id" "$swap_mb")
        else
            top_swap_fields+=("" "" "")
        fi
    done

    echo "${ts},${load1},${load5},${load15},${mem_used},${mem_total},${swap_used},${swap_total},${tz[0]},${tz[1]},${tz[2]},${tz[3]},${tz[4]},${tz[5]},${tz[6]},${nvme_c},${nvme_s1},${nvme_s2},${gpu_temp:-N/A},${gpu_power:-N/A},${gpu_util:-N/A},${gpu_clock:-N/A},${top_fields[0]},${top_fields[1]},${top_fields[2]},${top_fields[3]},${top_fields[4]},${top_fields[5]},${top_fields[6]},${top_fields[7]},${top_fields[8]},${top_fields[9]},${disk_read_mb_s},${disk_write_mb_s},${disk_io_ms_s},${swap_in_mb_s},${swap_out_mb_s},${top_swap_fields[0]},${top_swap_fields[1]},${top_swap_fields[2]},${top_swap_fields[3]},${top_swap_fields[4]},${top_swap_fields[5]},${top_swap_fields[6]},${top_swap_fields[7]},${top_swap_fields[8]},${top_swap_fields[9]},${top_swap_fields[10]},${top_swap_fields[11]},${top_swap_fields[12]},${top_swap_fields[13]},${top_swap_fields[14]}" >> "$logfile"

    sleep "$INTERVAL"
done
