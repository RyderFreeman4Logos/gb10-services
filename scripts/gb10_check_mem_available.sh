#!/usr/bin/env bash
# Refuse model startup unless MemAvailable is at or above the requested GiB.
set -euo pipefail

threshold_gib="${1:?minimum MemAvailable GiB required}"
meminfo_path="${GB10_MEMINFO_PATH:-/proc/meminfo}"
if [[ ! "$threshold_gib" =~ ^[1-9][0-9]*$ ]]; then
    printf 'minimum MemAvailable GiB must be a positive integer: %s\n' "$threshold_gib" >&2
    exit 2
fi

mem_available_kib=0
while IFS=: read -r key rest; do
    if [[ "$key" == "MemAvailable" ]]; then
        read -r mem_available_kib _ <<< "$rest"
        break
    fi
done < "$meminfo_path"

required_kib=$((threshold_gib * 1024 * 1024))
if (( mem_available_kib < required_kib )); then
    printf 'insufficient MemAvailable: %s KiB < %s KiB (%s GiB)\n' \
        "$mem_available_kib" "$required_kib" "$threshold_gib" >&2
    exit 1
fi
