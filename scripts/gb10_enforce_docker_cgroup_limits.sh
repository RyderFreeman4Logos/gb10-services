#!/usr/bin/env bash
# Enforce and verify rootless Docker container cgroup memory/swap limits.
# Usage: gb10_enforce_docker_cgroup_limits.sh <container-name> <expected-memory-gib>
#
# Rootless Docker + systemd cgroup driver places the workload in
# docker-<id>.scope. Service-level MemoryMax on the wrapper unit does not
# constrain the container. This helper:
#   1) waits for the generated docker scope
#   2) sets MemoryMax=<N>G and MemorySwapMax=0 on that scope
#   3) verifies live cgroup files match the intended hard cap
set -Eeuo pipefail

name="${1:?container name required}"
expected_gib="${2:?expected GiB required}"
docker_timeout_seconds="${GB10_DOCKER_TIMEOUT_SECONDS:-3}"
systemctl_timeout_seconds="${GB10_SYSTEMCTL_TIMEOUT_SECONDS:-10}"
wait_seconds="${GB10_CGROUP_WAIT_SECONDS:-120}"
if [[ ! "$expected_gib" =~ ^[1-9][0-9]*$ ]]; then
  echo "expected GiB must be a positive integer: $expected_gib" >&2
  exit 2
fi
for value in "$docker_timeout_seconds" "$systemctl_timeout_seconds" "$wait_seconds"; do
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "timeout values must be positive integers: $value" >&2
    exit 2
  fi
done
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1001/docker.sock}"

run_docker() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$docker_timeout_seconds" \
    /usr/bin/docker "$@"
}

run_systemctl() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$systemctl_timeout_seconds" \
    /usr/bin/systemctl --user "$@"
}

cid=""
cg=""
scope=""
deadline=$((SECONDS + wait_seconds))
while (( SECONDS < deadline )); do
  cid="$(run_docker inspect -f '{{.Id}}' "$name" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    sleep 1
    continue
  fi
  scope="docker-${cid}.scope"
  cg="$(run_systemctl show -p ControlGroup --value "$scope" 2>/dev/null || true)"
  if [[ -n "$cg" && "$cg" != "/" && -e "/sys/fs/cgroup${cg}/memory.swap.max" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "$cid" || -z "$scope" || -z "$cg" || "$cg" == "/" || ! -e "/sys/fs/cgroup${cg}/memory.swap.max" ]]; then
  echo "could not locate docker cgroup for $name cid=${cid:-missing} scope=${scope:-missing} cg=${cg:-missing}" >&2
  exit 1
fi

expected_bytes=$((expected_gib * 1024 * 1024 * 1024))

# Hard-cap ordinary container memory and ban additional swap. Re-apply both
# properties so Docker's incomplete --memory-swap mapping cannot leave
# memory.swap.max=max after start.
run_systemctl set-property --runtime "$scope" \
  "MemoryMax=${expected_gib}G" \
  MemorySwapMax=0

swap_max="$(cat "/sys/fs/cgroup${cg}/memory.swap.max")"
mem_max="$(cat "/sys/fs/cgroup${cg}/memory.max")"

if [[ "$swap_max" != "0" ]]; then
  echo "unexpected $name memory.swap.max=$swap_max expected=0 scope=$scope cg=$cg" >&2
  exit 1
fi
if [[ "$mem_max" != "$expected_bytes" ]]; then
  echo "unexpected $name memory.max=$mem_max expected=$expected_bytes scope=$scope cg=$cg" >&2
  exit 1
fi

echo "verified $name cgroup memory.max=$mem_max memory.swap.max=$swap_max scope=$scope"
