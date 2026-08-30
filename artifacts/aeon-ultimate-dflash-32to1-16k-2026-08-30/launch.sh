#!/bin/bash
set -euo pipefail
repo_root=/home/obj/project/github/RyderFreeman4Logos/gb10-services
run_dir=$repo_root/artifacts/aeon-ultimate-dflash-32to1-16k-2026-08-30
cfg=$run_dir/aeon-ultimate-18010-32to1-16k.toml
pid_file=$run_dir/run.pid-start
if [ -f "$pid_file" ]; then
  read -r old_pid old_start <"$pid_file"
  if [ -r "/proc/$old_pid/stat" ] && [ "$(awk '{print $22}' "/proc/$old_pid/stat")" = "$old_start" ]; then
    echo "refusing launch: owner $old_pid still live" >&2
    exit 1
  fi
fi
start_time=$(awk '{print $22}' "/proc/$$/stat")
printf '%s %s\n' "$$" "$start_time" >"$pid_file"
exec python3 "$repo_root/scripts/sglang_6k_concurrency_throughput.py" \
  --config "$cfg" \
  --state "$run_dir/run.state.json" \
  --progress "$run_dir/run.progress.yaml" \
  --out "$run_dir/run.json" \
  >"$run_dir/run.log" 2>&1
