#!/bin/bash
set -euo pipefail
repo_root=/home/obj/project/github/RyderFreeman4Logos/gb10-services
run_dir=$repo_root/artifacts/aeon-ultimate-dflash-32to1-16k-2026-08-30
cfg=$run_dir/aeon-ultimate-18010-32to1-16k.toml
state=$run_dir/run.state.json
saved_pid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$state")
saved_start=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["start_time"])' "$state")
if [ -r "/proc/$saved_pid/stat" ] && [ "$(awk '{print $22}' "/proc/$saved_pid/stat")" = "$saved_start" ]; then
  echo "refusing resume: owner $saved_pid still live" >&2
  exit 1
fi
start_time=$(awk '{print $22}' "/proc/$$/stat")
printf '%s %s\n' "$$" "$start_time" >"$run_dir/run.pid-start"
exec python3 "$repo_root/scripts/sglang_6k_concurrency_throughput.py" \
  --config "$cfg" \
  --state "$state" \
  --progress "$run_dir/run.progress.yaml" \
  --out "$run_dir/run.json" \
  --resume \
  >"$run_dir/run.log" 2>&1
