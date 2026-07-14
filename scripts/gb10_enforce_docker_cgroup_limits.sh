#!/usr/bin/env bash
# Enforce and verify rootless Docker container cgroup memory/swap limits.
# Usage: gb10_enforce_docker_cgroup_limits.sh <container-name> <expected-memory-gib>
#
# Set GB10_CGROUP_REGISTRATION_PATH only for a unit that opts into publishing a
# guardian target. The path selects the registration file; this helper contains
# no model or service identity policy.
set -Eeuo pipefail
umask 077

name="${1:?container name required}"
expected_gib="${2:?expected GiB required}"
docker_timeout_seconds="${GB10_DOCKER_TIMEOUT_SECONDS:-3}"
systemctl_timeout_seconds="${GB10_SYSTEMCTL_TIMEOUT_SECONDS:-10}"
wait_seconds="${GB10_CGROUP_WAIT_SECONDS:-120}"
registration_path="${GB10_CGROUP_REGISTRATION_PATH:-}"
registration_published=0
registration_tmp=""
docker_bin="${GB10_DOCKER_BIN:-/usr/bin/docker}"
systemctl_bin="${GB10_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
cgroup_root="${GB10_CGROUP_ROOT:-/sys/fs/cgroup}"
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
export DOCKER_HOST="${DOCKER_HOST:-unix://${XDG_RUNTIME_DIR:-/run/user/${UID}}/docker.sock}"

run_docker() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$docker_timeout_seconds" \
    "$docker_bin" "$@"
}

run_systemctl() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$systemctl_timeout_seconds" \
    "$systemctl_bin" --user "$@"
}

reject_registration_destination() {
  echo "$1" >&2
  run_docker stop --time 5 "$name" >/dev/null 2>&1 || true
  exit 2
}

expected_registration_dir=""
registration_dir=""
registration_file=""
if [[ -n "$registration_path" ]]; then
  expected_registration_dir="${XDG_RUNTIME_DIR:-/run/user/${UID}}/gb10-memory-guardian"
  registration_dir="${registration_path%/*}"
  registration_file="${registration_path##*/}"
  if [[ "$registration_dir" != "$expected_registration_dir" ]]; then
    reject_registration_destination \
      "guardian registration must be directly below $expected_registration_dir"
  fi
  if [[ ! "$registration_file" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ || "$registration_file" == "." || "$registration_file" == ".." ]]; then
    reject_registration_destination \
      "unsafe guardian registration filename: $registration_file"
  fi
  if [[ -L "$registration_dir" ]]; then
    reject_registration_destination \
      "guardian registration directory must not be a symlink: $registration_dir"
  fi
  /usr/bin/install -d -m 0700 "$registration_dir" || \
    reject_registration_destination \
      "could not create guardian registration directory: $registration_dir"
  if [[ ! -d "$registration_dir" || -L "$registration_dir" ]]; then
    reject_registration_destination \
      "guardian registration directory is unsafe: $registration_dir"
  fi
fi

fail_closed_registration() {
  local status=$?
  trap - EXIT
  if [[ -n "$registration_tmp" ]]; then
    rm -f -- "$registration_tmp"
  fi
  if [[ "$status" != "0" && -n "$registration_path" && "$registration_published" != "1" ]]; then
    rm -f -- "$registration_path"
    if [[ -z "$cid" ]]; then
      echo "registration publication failed before exact container identity was captured" >&2
      status=1
    elif ! run_docker stop --time 5 "$cid" >/dev/null 2>&1; then
      echo "graceful cleanup failed for exact launched container $cid; forcing exact cleanup" >&2
      if ! run_docker kill "$cid" >/dev/null 2>&1; then
        echo "exact launched container cleanup failed: $cid" >&2
        status=1
      fi
    fi
  fi
  exit "$status"
}
trap fail_closed_registration EXIT

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
  if [[ -n "$cg" && "$cg" != "/" && -e "${cgroup_root}${cg}/memory.swap.max" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "$cid" || -z "$scope" || -z "$cg" || "$cg" == "/" || ! -e "${cgroup_root}${cg}/memory.swap.max" ]]; then
  echo "could not locate docker cgroup for $name cid=${cid:-missing} scope=${scope:-missing} cg=${cg:-missing}" >&2
  exit 1
fi

expected_bytes=$((expected_gib * 1024 * 1024 * 1024))

# Docker can leave memory.swap.max=max even with --memory-swap. Enforce both
# properties directly on the generated scope and verify the live cgroup files.
run_systemctl set-property --runtime "$scope" \
  "MemoryMax=${expected_gib}G" \
  MemorySwapMax=0

swap_max="$(<"${cgroup_root}${cg}/memory.swap.max")"
mem_max="$(<"${cgroup_root}${cg}/memory.max")"

if [[ "$swap_max" != "0" ]]; then
  echo "unexpected $name memory.swap.max=$swap_max expected=0 scope=$scope cg=$cg" >&2
  exit 1
fi
if [[ "$mem_max" != "$expected_bytes" ]]; then
  echo "unexpected $name memory.max=$mem_max expected=$expected_bytes scope=$scope cg=$cg" >&2
  exit 1
fi

if [[ -n "$registration_path" ]]; then
  expected_control_group="/user.slice/user-${UID}.slice/user@${UID}.service/app.slice/${scope}"
  if [[ ! "$cid" =~ ^[0-9a-f]{64}$ || "$scope" != "docker-${cid}.scope" || "$cg" != "$expected_control_group" ]]; then
    echo "refusing unsafe guardian registration cid=$cid scope=$scope cg=$cg expected=$expected_control_group" >&2
    exit 1
  fi

  registration_tmp="$(mktemp "${registration_path}.tmp.XXXXXX")"
  chmod 0600 "$registration_tmp"
  {
    printf 'version=1\n'
    printf 'container_id=%s\n' "$cid"
    printf 'scope=%s\n' "$scope"
    printf 'control_group=%s\n' "$cg"
  } >"$registration_tmp"
  chmod 0600 "$registration_tmp"
  mv -f -- "$registration_tmp" "$registration_path"
  registration_tmp=""

  registration_lines=()
  mapfile -t registration_lines <"$registration_path"
  if [[ ! -f "$registration_path" || -L "$registration_path" \
    || "${#registration_lines[@]}" != "4" \
    || "${registration_lines[0]:-}" != "version=1" \
    || "${registration_lines[1]:-}" != "container_id=$cid" \
    || "${registration_lines[2]:-}" != "scope=$scope" \
    || "${registration_lines[3]:-}" != "control_group=$cg" ]]; then
    echo "guardian registration publication did not preserve the validated bytes: $registration_path" >&2
    exit 1
  fi
  registration_published=1
fi

echo "verified $name cgroup memory.max=$mem_max memory.swap.max=$swap_max scope=$scope"
trap - EXIT
