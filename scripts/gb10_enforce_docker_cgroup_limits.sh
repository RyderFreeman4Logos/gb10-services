#!/usr/bin/env bash
# Enforce one rootless Docker cgroup cap or manage the text guardian identity.
# Usage:
#   gb10_enforce_docker_cgroup_limits.sh <container-name> <expected-memory-gib>
#   gb10_enforce_docker_cgroup_limits.sh --publish-registration <container-name> <expected-memory-gib>
#   gb10_enforce_docker_cgroup_limits.sh --cleanup-registration
set -Eeuo pipefail
umask 077

mode=""
name=""
expected_gib=""
case "${1:-}" in
  --cleanup-registration)
    [[ "$#" == "1" ]] || {
      echo "cleanup mode accepts no additional arguments" >&2
      exit 2
    }
    mode="cleanup"
    ;;
  --publish-registration)
    [[ "$#" == "3" ]] || {
      echo "publish mode requires a container name and expected GiB" >&2
      exit 2
    }
    mode="publish"
    name="$2"
    expected_gib="$3"
    ;;
  --*)
    echo "unknown cgroup helper mode: $1" >&2
    exit 2
    ;;
  *)
    [[ "$#" == "2" ]] || {
      echo "container name and expected GiB are required" >&2
      exit 2
    }
    mode="cap"
    name="$1"
    expected_gib="$2"
    ;;
esac

docker_timeout_seconds="${GB10_DOCKER_TIMEOUT_SECONDS:-3}"
systemctl_timeout_seconds="${GB10_SYSTEMCTL_TIMEOUT_SECONDS:-10}"
wait_seconds="${GB10_CGROUP_WAIT_SECONDS:-120}"
registration_path="${GB10_CGROUP_REGISTRATION_PATH:-}"
container_cidfile="${GB10_CONTAINER_CIDFILE:-}"
identity_dir="${XDG_RUNTIME_DIR:-/run/user/${UID}}/gb10-memory-guardian"
registration_published=0
registration_tmp=""
cid=""
cid_digest=""
registration_cid=""
registration_scope=""
registration_control_group=""
registration_digest=""
docker_state="unknown"
docker_bin="${GB10_DOCKER_BIN:-/usr/bin/docker}"
systemctl_bin="${GB10_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
cgroup_root="${GB10_CGROUP_ROOT:-/sys/fs/cgroup}"
export DOCKER_HOST="${DOCKER_HOST:-unix://${XDG_RUNTIME_DIR:-/run/user/${UID}}/docker.sock}"

run_docker() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$docker_timeout_seconds" \
    "$docker_bin" "$@"
}

run_systemctl() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$systemctl_timeout_seconds" \
    "$systemctl_bin" --user "$@"
}

validate_positive_inputs() {
  local value
  if [[ "$mode" != "cleanup" && ! "$expected_gib" =~ ^[1-9][0-9]*$ ]]; then
    echo "expected GiB must be a positive integer: $expected_gib" >&2
    return 2
  fi
  for value in "$docker_timeout_seconds" "$systemctl_timeout_seconds" "$wait_seconds"; do
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
      echo "timeout values must be positive integers: $value" >&2
      return 2
    fi
  done
}

validate_cidfile_path() {
  [[ "$container_cidfile" == "$identity_dir/aeon-text.cid" ]] || {
    echo "registered container requires the reviewed runtime cidfile path" >&2
    return 1
  }
  [[ ! -L "$identity_dir" ]] || {
    echo "guardian identity directory must not be a symlink: $identity_dir" >&2
    return 1
  }
}

validate_registration_path() {
  local filename
  [[ -n "$registration_path" && "${registration_path%/*}" == "$identity_dir" ]] || {
    echo "guardian registration must be directly below $identity_dir" >&2
    return 1
  }
  filename="${registration_path##*/}"
  [[ "$filename" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ \
    && "$filename" != "." && "$filename" != ".." ]] || {
    echo "unsafe guardian registration filename: $filename" >&2
    return 1
  }
  [[ ! -L "$identity_dir" ]] || {
    echo "guardian identity directory must not be a symlink: $identity_dir" >&2
    return 1
  }
}

sha256_file() {
  local output
  output="$(/usr/bin/sha256sum -- "$1")" || return 1
  printf '%s' "${output%% *}"
}

read_exact_cid() {
  local cid_lines=()
  [[ -f "$container_cidfile" && ! -L "$container_cidfile" ]] || {
    echo "immutable launch CID is missing or unsafe: $container_cidfile" >&2
    return 1
  }
  mapfile -t cid_lines <"$container_cidfile"
  [[ "${#cid_lines[@]}" == "1" && "${cid_lines[0]}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "immutable launch CID is malformed: $container_cidfile" >&2
    return 1
  }
  cid="${cid_lines[0]}"
  cid_digest="$(sha256_file "$container_cidfile")" || return 1
}

acquire_launch_cid() {
  local attempt
  for attempt in {1..10}; do
    if read_exact_cid 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "could not acquire immutable launch CID from $container_cidfile" >&2
  return 1
}

acquire_cap_cid() {
  local output cid_lines=()
  if ! output="$(run_docker inspect -f '{{.Id}}' "$name")"; then
    echo "could not resolve immutable Docker CID for cap-only container: $name" >&2
    return 1
  fi
  mapfile -t cid_lines <<<"$output"
  [[ "${#cid_lines[@]}" == "1" && "${cid_lines[0]}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Docker returned a malformed or non-unique immutable CID for: $name" >&2
    return 1
  }
  cid="${cid_lines[0]}"
}

parse_exact_registration() {
  local lines=() expected_scope expected_control_group
  [[ -f "$registration_path" && ! -L "$registration_path" ]] || {
    echo "guardian registration is missing or unsafe: $registration_path" >&2
    return 1
  }
  mapfile -t lines <"$registration_path"
  [[ "${#lines[@]}" == "4" && "${lines[0]:-}" == "version=1" ]] || {
    echo "guardian registration is malformed: $registration_path" >&2
    return 1
  }
  registration_cid="${lines[1]#container_id=}"
  registration_scope="${lines[2]#scope=}"
  registration_control_group="${lines[3]#control_group=}"
  [[ "${lines[1]}" == "container_id=$registration_cid" \
    && "$registration_cid" =~ ^[0-9a-f]{64}$ ]] || return 1
  expected_scope="docker-${registration_cid}.scope"
  expected_control_group="/user.slice/user-${UID}.slice/user@${UID}.service/app.slice/${expected_scope}"
  [[ "${lines[2]}" == "scope=$expected_scope" \
    && "${lines[3]}" == "control_group=$expected_control_group" ]] || {
    echo "guardian registration identity is not the exact rootless Docker scope" >&2
    return 1
  }
  registration_digest="$(sha256_file "$registration_path")" || return 1
}

inspect_exact_container() {
  local output="" error_output="" error_file status=0
  error_file="$(mktemp)"
  if output="$(run_docker inspect -f '{{.State.Running}}' "$cid" 2>"$error_file")"; then
    status=0
  else
    status=$?
  fi
  error_output="$(<"$error_file")"
  rm -f -- "$error_file"
  if [[ "$status" == "0" ]]; then
    case "$output" in
      true) docker_state="running" ;;
      false) docker_state="stopped" ;;
      *) docker_state="unknown" ;;
    esac
  elif [[ "$error_output" =~ ^Error:\ No\ such\ (object|container):[[:space:]] ]]; then
    docker_state="absent"
  else
    docker_state="unknown"
  fi
}

verify_exact_cgroup_empty() {
  local cgroup_path events=() populated_count=0 populated_value=""
  cgroup_path="${cgroup_root}${registration_control_group}"
  [[ ! -e "$cgroup_path" ]] && return 0
  [[ -d "$cgroup_path" && ! -L "$cgroup_path" \
    && -f "$cgroup_path/cgroup.events" && ! -L "$cgroup_path/cgroup.events" ]] || return 1
  mapfile -t events <"$cgroup_path/cgroup.events"
  for event in "${events[@]}"; do
    if [[ "$event" == populated\ * ]]; then
      populated_count=$((populated_count + 1))
      populated_value="${event#populated }"
    fi
  done
  [[ "$populated_count" == "1" && "$populated_value" == "0" ]]
}

kill_exact_cgroup_and_verify() {
  local cgroup_path
  cgroup_path="${cgroup_root}${registration_control_group}"
  [[ ! -e "$cgroup_path" ]] && return 0
  [[ -d "$cgroup_path" && ! -L "$cgroup_path" \
    && -f "$cgroup_path/cgroup.kill" && ! -L "$cgroup_path/cgroup.kill" ]] || return 1
  printf '1' >"$cgroup_path/cgroup.kill" || return 1
  verify_exact_cgroup_empty
}

cid_file_matches_capture() {
  local current_digest
  [[ -f "$container_cidfile" && ! -L "$container_cidfile" ]] || return 1
  current_digest="$(sha256_file "$container_cidfile")" || return 1
  [[ "$current_digest" == "$cid_digest" ]]
}

registration_file_matches_capture() {
  local current_digest
  [[ -f "$registration_path" && ! -L "$registration_path" ]] || return 1
  current_digest="$(sha256_file "$registration_path")" || return 1
  [[ "$current_digest" == "$registration_digest" ]]
}

remove_captured_cid_if_unchanged() {
  cid_file_matches_capture || return 1
  rm -f -- "$container_cidfile"
}

remove_matched_identity_pair_if_unchanged() {
  cid_file_matches_capture || return 1
  registration_file_matches_capture || return 1
  rm -f -- "$registration_path" "$container_cidfile"
}

finish_proved_cleanup() {
  local registration_matched="$1"
  if [[ "$registration_matched" == "1" ]] \
    && remove_matched_identity_pair_if_unchanged; then
    return 0
  fi

  if ! remove_captured_cid_if_unchanged; then
    echo "captured CID identity changed during cleanup; retaining current identities" >&2
    return 1
  fi
  if [[ "$registration_matched" == "1" ]]; then
    echo "registration changed during exact CID cleanup; retained replacement identity" >&2
  else
    echo "registration did not match captured CID; retained independent registration" >&2
  fi
  return 1
}

cleanup_exact_identity() {
  local registration_authority_valid=0 registration_matched=0 cleanup_proved=0

  validate_cidfile_path || return 1
  if validate_registration_path; then
    registration_authority_valid=1
  fi
  if [[ "$registration_authority_valid" == "1" \
    && ! -e "$registration_path" && ! -e "$container_cidfile" ]]; then
    return 0
  fi

  # The launch CID is independent authority. Capture it before using or parsing
  # the registration destination so a hostile destination cannot block exact cleanup.
  read_exact_cid || return 1
  if [[ "$registration_authority_valid" == "1" ]] \
    && parse_exact_registration \
    && [[ "$registration_cid" == "$cid" ]]; then
    registration_matched=1
  fi

  inspect_exact_container
  case "$docker_state" in
    absent|stopped)
      cleanup_proved=1
      ;;
    running)
      if ! run_docker stop --time 5 "$cid" >/dev/null 2>&1; then
        if ! run_docker kill "$cid" >/dev/null 2>&1; then
          docker_state="unknown"
        fi
      fi
      inspect_exact_container
      if [[ "$docker_state" == "absent" || "$docker_state" == "stopped" ]]; then
        cleanup_proved=1
      fi
      ;;
    unknown) ;;
  esac

  if [[ "$cleanup_proved" != "1" && "$registration_matched" == "1" ]] \
    && kill_exact_cgroup_and_verify; then
    cleanup_proved=1
  fi
  if [[ "$cleanup_proved" != "1" ]]; then
    echo "exact container state is unknown; retaining registration and CID identity" >&2
    return 1
  fi

  finish_proved_cleanup "$registration_matched"
}

fail_closed_registration() {
  local status=$?
  trap - EXIT
  if [[ -n "$registration_tmp" ]]; then
    rm -f -- "$registration_tmp"
  fi
  if [[ "$status" != "0" && "$registration_published" != "1" ]]; then
    if ! cleanup_exact_identity; then
      echo "registration publication failed and exact cleanup was not fully reconciled" >&2
    fi
  fi
  exit "$status"
}

locate_and_enforce_exact_scope() {
  local cg="" scope="" deadline expected_bytes swap_max mem_max expected_control_group
  scope="docker-${cid}.scope"
  deadline=$((SECONDS + wait_seconds))
  while (( SECONDS < deadline )); do
    cg="$(run_systemctl show -p ControlGroup --value "$scope" 2>/dev/null || true)"
    if [[ -n "$cg" && "$cg" != "/" && -e "${cgroup_root}${cg}/memory.swap.max" ]]; then
      break
    fi
    sleep 1
  done

  if [[ -z "$cg" || "$cg" == "/" || ! -e "${cgroup_root}${cg}/memory.swap.max" ]]; then
    echo "could not locate docker cgroup for $name cid=$cid scope=$scope cg=${cg:-missing}" >&2
    return 1
  fi

  expected_control_group="/user.slice/user-${UID}.slice/user@${UID}.service/app.slice/${scope}"
  if [[ "$scope" != "docker-${cid}.scope" || "$cg" != "$expected_control_group" ]]; then
    echo "refusing unsafe cgroup identity cid=$cid scope=$scope cg=$cg expected=$expected_control_group" >&2
    return 1
  fi

  expected_bytes=$((expected_gib * 1024 * 1024 * 1024))
  run_systemctl set-property --runtime "$scope" \
    "MemoryMax=${expected_gib}G" \
    MemorySwapMax=0

  swap_max="$(<"${cgroup_root}${cg}/memory.swap.max")"
  mem_max="$(<"${cgroup_root}${cg}/memory.max")"
  if [[ "$swap_max" != "0" ]]; then
    echo "unexpected $name memory.swap.max=$swap_max expected=0 scope=$scope cg=$cg" >&2
    return 1
  fi
  if [[ "$mem_max" != "$expected_bytes" ]]; then
    echo "unexpected $name memory.max=$mem_max expected=$expected_bytes scope=$scope cg=$cg" >&2
    return 1
  fi

  registration_scope="$scope"
  registration_control_group="$cg"
  echo "verified $name cgroup memory.max=$mem_max memory.swap.max=$swap_max scope=$scope"
}

publish_registration() {
  registration_tmp="$(mktemp "${registration_path}.tmp.XXXXXX")"
  chmod 0600 "$registration_tmp"
  {
    printf 'version=1\n'
    printf 'container_id=%s\n' "$cid"
    printf 'scope=%s\n' "$registration_scope"
    printf 'control_group=%s\n' "$registration_control_group"
  } >"$registration_tmp"
  chmod 0600 "$registration_tmp"
  mv -f -- "$registration_tmp" "$registration_path"
  registration_tmp=""

  registration_cid=""
  registration_scope=""
  registration_control_group=""
  registration_digest=""
  if ! parse_exact_registration || [[ "$registration_cid" != "$cid" ]]; then
    echo "guardian registration publication did not preserve the validated bytes: $registration_path" >&2
    return 1
  fi
  registration_published=1
}

case "$mode" in
  cap)
    validate_positive_inputs
    acquire_cap_cid
    locate_and_enforce_exact_scope
    ;;
  cleanup)
    validate_positive_inputs
    cleanup_exact_identity
    ;;
  publish)
    trap fail_closed_registration EXIT
    validate_positive_inputs
    validate_cidfile_path
    validate_registration_path
    /usr/bin/install -d -m 0700 "$identity_dir"
    [[ -d "$identity_dir" && ! -L "$identity_dir" ]] || {
      echo "guardian registration directory is unsafe: $identity_dir" >&2
      exit 2
    }
    acquire_launch_cid
    locate_and_enforce_exact_scope
    publish_registration
    trap - EXIT
    ;;
esac
