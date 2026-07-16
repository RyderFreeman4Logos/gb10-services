#!/usr/bin/env bash
# Publish the current AEON Docker cgroup for llm-guard-proxy's integrated guardian.
# This helper only validates and publishes identity; it never changes cgroup limits.
set -Eeuo pipefail
umask 077

[[ "$#" == "0" ]] || {
  echo "this publisher accepts no arguments" >&2
  exit 2
}

runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${UID}}"
identity_dir="${runtime_dir}/gb10-memory-guardian"
container_cidfile="${GB10_CONTAINER_CIDFILE:-}"
registration_path="${GB10_CGROUP_REGISTRATION_PATH:-}"
cgroup_root="${GB10_CGROUP_ROOT:-/sys/fs/cgroup}"
systemctl_bin="${GB10_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
systemctl_timeout_seconds="${GB10_SYSTEMCTL_TIMEOUT_SECONDS:-10}"
wait_seconds="${GB10_CGROUP_WAIT_SECONDS:-120}"
registration_tmp=""

cleanup_tmp() {
  if [[ -n "$registration_tmp" ]]; then
    rm -f -- "$registration_tmp"
  fi
}
trap cleanup_tmp EXIT

for value in "$systemctl_timeout_seconds" "$wait_seconds"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    echo "timeout values must be positive integers: $value" >&2
    exit 2
  }
done
[[ "$cgroup_root" == /* ]] || {
  echo "cgroup root must be absolute" >&2
  exit 2
}
[[ "$container_cidfile" == "$identity_dir/aeon-text.cid" ]] || {
  echo "container CID file must use the reviewed AEON runtime path" >&2
  exit 2
}
[[ "$registration_path" == "$identity_dir/text-cgroup.v1" ]] || {
  echo "guardian registration must use the reviewed text runtime path" >&2
  exit 2
}
[[ -d "$identity_dir" && ! -L "$identity_dir" ]] || {
  echo "guardian identity directory is missing or unsafe: $identity_dir" >&2
  exit 1
}
[[ -f "$container_cidfile" && ! -L "$container_cidfile" ]] || {
  echo "immutable launch CID is missing or unsafe: $container_cidfile" >&2
  exit 1
}

cid_lines=()
mapfile -t cid_lines <"$container_cidfile"
[[ "${#cid_lines[@]}" == "1" && "${cid_lines[0]}" =~ ^[0-9a-f]{64}$ ]] || {
  echo "immutable launch CID is malformed: $container_cidfile" >&2
  exit 1
}
cid="${cid_lines[0]}"
scope="docker-${cid}.scope"
expected_control_group="/user.slice/user-${UID}.slice/user@${UID}.service/app.slice/${scope}"
control_group=""
deadline=$((SECONDS + wait_seconds))

while (( SECONDS < deadline )); do
  control_group="$(
    /usr/bin/timeout --signal=TERM --kill-after=2 "$systemctl_timeout_seconds" \
      "$systemctl_bin" --user show -p ControlGroup --value "$scope" 2>/dev/null || true
  )"
  if [[ -n "$control_group" ]]; then
    [[ "$control_group" == "$expected_control_group" ]] || {
      echo "refusing unexpected cgroup identity: $control_group" >&2
      exit 1
    }
    break
  fi
  sleep 1
done
[[ "$control_group" == "$expected_control_group" ]] || {
  echo "could not locate exact Docker cgroup for cid=$cid" >&2
  exit 1
}

cgroup_path="${cgroup_root}${control_group}"
[[ -d "$cgroup_path" && ! -L "$cgroup_path" \
  && -f "$cgroup_path/cgroup.kill" && ! -L "$cgroup_path/cgroup.kill" \
  && -f "$cgroup_path/cgroup.events" && ! -L "$cgroup_path/cgroup.events" ]] || {
  echo "validated cgroup files are missing or unsafe: $cgroup_path" >&2
  exit 1
}

populated_count=0
populated_value=""
while IFS= read -r event; do
  if [[ "$event" == populated\ * ]]; then
    populated_count=$((populated_count + 1))
    populated_value="${event#populated }"
  fi
done <"$cgroup_path/cgroup.events"
[[ "$populated_count" == "1" && "$populated_value" == "1" ]] || {
  echo "registered cgroup is not uniquely populated: $cgroup_path" >&2
  exit 1
}

registration_tmp="$(mktemp "${registration_path}.tmp.XXXXXX")"
chmod 0600 "$registration_tmp"
{
  printf 'version=1\n'
  printf 'container_id=%s\n' "$cid"
  printf 'scope=%s\n' "$scope"
  printf 'control_group=%s\n' "$control_group"
} >"$registration_tmp"
chmod 0600 "$registration_tmp"
mv -f -- "$registration_tmp" "$registration_path"
registration_tmp=""

echo "published llm-guard-proxy guardian target cid=$cid scope=$scope"
