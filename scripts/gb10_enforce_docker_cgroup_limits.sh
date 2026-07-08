#!/usr/bin/env bash
# Enforce and verify rootless Docker container cgroup memory/swap limits.
# Usage: gb10_enforce_docker_cgroup_limits.sh <container-name> <expected-memory-gib>
set -Eeuo pipefail

name="${1:?container name required}"
expected_gib="${2:?expected GiB required}"
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1001/docker.sock}"

cid=""
cg=""
scope=""
for _ in $(seq 1 120); do
  cid="$(/usr/bin/docker inspect -f '{{.Id}}' "$name" 2>/dev/null || true)"
  if [ -z "$cid" ]; then
    sleep 1
    continue
  fi
  scope="docker-${cid}.scope"
  cg="$(/usr/bin/systemctl --user show -p ControlGroup --value "$scope" 2>/dev/null || true)"
  if [ -n "$cg" ] && [ "$cg" != "/" ] && [ -e "/sys/fs/cgroup${cg}/memory.swap.max" ]; then
    break
  fi
  sleep 1
done

if [ -z "$cid" ] || [ -z "$scope" ] || [ -z "$cg" ] || [ "$cg" = "/" ] || [ ! -e "/sys/fs/cgroup${cg}/memory.swap.max" ]; then
  echo "could not locate docker cgroup for $name cid=${cid:-missing} scope=${scope:-missing} cg=${cg:-missing}" >&2
  exit 1
fi

/usr/bin/systemctl --user set-property --runtime "$scope" MemorySwapMax=0

swap_max="$(cat "/sys/fs/cgroup${cg}/memory.swap.max")"
mem_max="$(cat "/sys/fs/cgroup${cg}/memory.max")"
expected_bytes=$((expected_gib * 1024 * 1024 * 1024))

if [ "$swap_max" != "0" ]; then
  echo "unexpected $name memory.swap.max=$swap_max expected=0 scope=$scope cg=$cg" >&2
  exit 1
fi
if [ "$mem_max" != "$expected_bytes" ]; then
  echo "unexpected $name memory.max=$mem_max expected=$expected_bytes scope=$scope cg=$cg" >&2
  exit 1
fi

echo "verified $name cgroup memory.max=$mem_max memory.swap.max=$swap_max scope=$scope"
