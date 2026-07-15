#!/usr/bin/env bash
# Tier 2 GB10 recovery: stop the model stack, prove UMA release, then restore by priority.
set -uo pipefail
umask 077

readonly embedding_unit="vllm-embedding.service"
readonly reranker_unit="querit-4b-reranker.service"
readonly text_unit="vllm-aeon-27b-dflash.service"
readonly units=("$embedding_unit" "$reranker_unit" "$text_unit")
readonly stop_units=("$text_unit" "$reranker_unit" "$embedding_unit")

declare -Ar containers=(
  ["$embedding_unit"]="vllm-embedding"
  ["$reranker_unit"]="querit-4b-reranker"
  ["$text_unit"]="vllm-aeon-27b-dflash-n12"
)
declare -Ar ports=(
  ["$embedding_unit"]="18012"
  ["$reranker_unit"]="18013"
  ["$text_unit"]="18010"
)

systemctl_bin="${GB10_STACK_RECOVERY_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
docker_bin="${GB10_STACK_RECOVERY_DOCKER_BIN:-/usr/bin/docker}"
curl_bin="${GB10_STACK_RECOVERY_CURL_BIN:-/usr/bin/curl}"
flock_bin="${GB10_STACK_RECOVERY_FLOCK_BIN:-/usr/bin/flock}"
timeout_bin="${GB10_STACK_RECOVERY_TIMEOUT_BIN:-/usr/bin/timeout}"
install_bin="${GB10_STACK_RECOVERY_INSTALL_BIN:-/usr/bin/install}"
mv_bin="${GB10_STACK_RECOVERY_MV_BIN:-/usr/bin/mv}"
rm_bin="${GB10_STACK_RECOVERY_RM_BIN:-/usr/bin/rm}"
sleep_bin="${GB10_STACK_RECOVERY_SLEEP_BIN:-/usr/bin/sleep}"

command_timeout="${GB10_STACK_RECOVERY_COMMAND_TIMEOUT_SECONDS:-15}"
stop_timeout="${GB10_STACK_RECOVERY_STOP_TIMEOUT_SECONDS:-90}"
start_timeout="${GB10_STACK_RECOVERY_START_TIMEOUT_SECONDS:-900}"
readiness_timeout="${GB10_STACK_RECOVERY_READINESS_TIMEOUT_SECONDS:-900}"
probe_timeout="${GB10_STACK_RECOVERY_PROBE_TIMEOUT_SECONDS:-5}"
poll_seconds="${GB10_STACK_RECOVERY_POLL_SECONDS:-5}"
kill_after="${GB10_STACK_RECOVERY_KILL_AFTER_SECONDS:-5}"
minimum_available_gib="${GB10_STACK_RECOVERY_MIN_MEM_AVAILABLE_GIB:-40}"
endpoint_host="${GB10_STACK_RECOVERY_ENDPOINT_HOST:-100.105.4.92}"

meminfo_path="${GB10_STACK_RECOVERY_MEMINFO_PATH:-/proc/meminfo}"
boot_id_path="${GB10_STACK_RECOVERY_BOOT_ID_PATH:-/proc/sys/kernel/random/boot_id}"
proc_root="${GB10_STACK_RECOVERY_PROC_ROOT:-/proc}"
cgroup_root="${GB10_STACK_RECOVERY_CGROUP_ROOT:-/sys/fs/cgroup}"
runtime_dir="${GB10_STACK_RECOVERY_RUNTIME_DIR:-${RUNTIME_DIRECTORY:-${XDG_RUNTIME_DIR:-/tmp}/gb10-stack-recovery}}"
state_dir="${GB10_STACK_RECOVERY_STATE_DIR:-${STATE_DIRECTORY:-${XDG_STATE_HOME:-$HOME/.local/state}/gb10-stack-recovery}}"
docker_cgroup_prefix="${GB10_STACK_RECOVERY_DOCKER_CGROUP_PREFIX:-/user.slice/user-${EUID}.slice/user@${EUID}.service/app.slice}"
lock_path="$runtime_dir/coordinator.lock"
marker_path="$state_dir/attempted-boot-id.v1"
receipt_path="$state_dir/receipt.v1"
evidence_path=""
marker_tmp=""
receipt_tmp=""
boot_id=""
started_epoch=""
release_epoch=""
completed_epoch=""
mem_available_kib=""
min_mem_available_kib=""
evidence_lines=0
readonly evidence_line_limit=96

# State captured before destructive action and after successful recovery.
declare -A before_active=()
declare -A before_sub=()
declare -A before_pid=()
declare -A before_restarts=()
declare -A before_cgroup=()
declare -A before_cid=()
declare -A before_engine_pid=()
declare -A before_docker_cgroup=()
declare -A after_pid=()
declare -A after_restarts=()

positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

safe_absolute_path() {
  [[ "$1" == /* && "$1" != *$'\n'* && "$1" != *"/../"* && "$1" != */.. ]]
}

validate_configuration() {
  local value
  for value in \
    "$command_timeout" "$stop_timeout" "$start_timeout" "$readiness_timeout" \
    "$probe_timeout" "$poll_seconds" "$kill_after" "$minimum_available_gib"; do
    positive_integer "$value" || {
      printf 'GB10_STACK_RECOVERY invalid_positive_integer value=%s\n' "$value" >&2
      return 2
    }
  done
  [[ "$endpoint_host" =~ ^[A-Za-z0-9.:-]+$ ]] || {
    printf 'GB10_STACK_RECOVERY invalid_endpoint_host\n' >&2
    return 2
  }
  for value in "$runtime_dir" "$state_dir" "$meminfo_path" "$boot_id_path" \
    "$proc_root" "$cgroup_root" "$docker_cgroup_prefix"; do
    safe_absolute_path "$value" || {
      printf 'GB10_STACK_RECOVERY unsafe_path\n' >&2
      return 2
    }
  done
}

run_with_timeout() {
  local duration="$1"
  shift
  "${timeout_bin}" --signal=TERM --kill-after="${kill_after}" "$duration" "$@"
}

run_systemctl() {
  local duration="$1"
  shift
  run_with_timeout "$duration" "${systemctl_bin}" --user "$@"
}

run_docker() {
  local duration="$1"
  shift
  run_with_timeout "$duration" "${docker_bin}" "$@"
}

run_curl() {
  local duration="$1"
  shift
  run_with_timeout "$duration" "${curl_bin}" "$@"
}

run_flock() {
  local duration="$1"
  shift
  run_with_timeout "$duration" "${flock_bin}" "$@"
}

run_file_command() {
  local duration="$1"
  shift
  run_with_timeout "$duration" "$@"
}

current_epoch() {
  printf '%(%s)T' -1
}

emit_event() {
  local message="$1"
  printf 'GB10_STACK_RECOVERY %s\n' "$message" >&2
  if [[ -n "$evidence_path" && "$evidence_lines" -lt "$evidence_line_limit" ]]; then
    printf '%s\n' "$message" >>"$evidence_path" 2>/dev/null || true
    evidence_lines=$((evidence_lines + 1))
  fi
}

cleanup_temporary_files() {
  local path
  for path in "$marker_tmp" "$receipt_tmp"; do
    if [[ -n "$path" && -e "$path" ]]; then
      run_file_command "$command_timeout" "$rm_bin" -f -- "$path" >/dev/null 2>&1 || true
    fi
  done
}
trap cleanup_temporary_files EXIT

fail_closed() {
  local reason="$1" status="${2:-1}"
  completed_epoch="$(current_epoch)"
  emit_event "result=failure reason=$reason completed_epoch=$completed_epoch"
  exit "$status"
}

read_boot_id() {
  local lines=()
  [[ -f "$boot_id_path" && ! -L "$boot_id_path" ]] || return 1
  mapfile -t lines <"$boot_id_path" || return 1
  [[ "${#lines[@]}" == "1" ]] || return 1
  boot_id="${lines[0]}"
  [[ "$boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
}

read_mem_available() {
  local key value _ count=0 candidate=""
  while read -r key value _; do
    if [[ "$key" == "MemAvailable:" ]]; then
      count=$((count + 1))
      candidate="$value"
    fi
  done <"$meminfo_path" || return 1
  [[ "$count" == "1" && "$candidate" =~ ^[0-9]+$ ]] || return 1
  mem_available_kib="$candidate"
  if [[ -z "$min_mem_available_kib" ]] || ((candidate < min_mem_available_kib)); then
    min_mem_available_kib="$candidate"
  fi
}

write_attempt_marker() {
  local previous=() previous_boot=""
  if [[ -e "$marker_path" ]]; then
    [[ -f "$marker_path" && ! -L "$marker_path" ]] || return 1
    mapfile -t previous <"$marker_path" || return 1
    [[ "${#previous[@]}" == "2" && "${previous[0]}" == "version=1" ]] || return 1
    previous_boot="${previous[1]#boot_id=}"
    [[ "${previous[1]}" == "boot_id=$previous_boot" ]] || return 1
    if [[ "$previous_boot" == "$boot_id" ]]; then
      emit_event "already_attempted_this_boot boot_id=$boot_id"
      return 75
    fi
  fi

  marker_tmp="$state_dir/.attempted-boot-id.v1.$$"
  {
    printf 'version=1\n'
    printf 'boot_id=%s\n' "$boot_id"
  } >"$marker_tmp" || return 1
  run_file_command "$command_timeout" "$mv_bin" -f -- "$marker_tmp" "$marker_path" || return 1
  marker_tmp=""
}

parse_unit_snapshot() {
  local unit="$1" phase="$2" output line key value
  local load_state="" active_state="" sub_state="" main_pid="" restarts="" control_group=""
  local seen_load=0 seen_active=0 seen_sub=0 seen_pid=0 seen_restarts=0 seen_cgroup=0

  output="$(run_systemctl "$command_timeout" show \
    --property=LoadState --property=ActiveState --property=SubState \
    --property=MainPID --property=NRestarts --property=ControlGroup "$unit")" || return 1
  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      LoadState) load_state="$value"; seen_load=$((seen_load + 1)) ;;
      ActiveState) active_state="$value"; seen_active=$((seen_active + 1)) ;;
      SubState) sub_state="$value"; seen_sub=$((seen_sub + 1)) ;;
      MainPID) main_pid="$value"; seen_pid=$((seen_pid + 1)) ;;
      NRestarts) restarts="$value"; seen_restarts=$((seen_restarts + 1)) ;;
      ControlGroup) control_group="$value"; seen_cgroup=$((seen_cgroup + 1)) ;;
      *) return 1 ;;
    esac
  done <<<"$output"

  [[ "$seen_load" == 1 && "$seen_active" == 1 && "$seen_sub" == 1 \
    && "$seen_pid" == 1 && "$seen_restarts" == 1 && "$seen_cgroup" == 1 ]] || return 1
  [[ "$load_state" == "loaded" ]] || return 1
  [[ "$active_state" =~ ^[a-z-]+$ && "$sub_state" =~ ^[a-z-]+$ ]] || return 1
  [[ "$main_pid" =~ ^[0-9]+$ && "$restarts" =~ ^[0-9]+$ ]] || return 1
  if [[ -n "$control_group" ]]; then
    safe_absolute_path "$control_group" || return 1
  fi

  if [[ "$phase" == "before" ]]; then
    before_active["$unit"]="$active_state"
    before_sub["$unit"]="$sub_state"
    before_pid["$unit"]="$main_pid"
    before_restarts["$unit"]="$restarts"
    before_cgroup["$unit"]="$control_group"
    emit_event "snapshot unit=$unit active_state=$active_state sub_state=$sub_state main_pid=$main_pid nrestarts=$restarts"
  else
    [[ "$active_state" == "active" && "$main_pid" != "0" ]] || return 1
    after_pid["$unit"]="$main_pid"
    after_restarts["$unit"]="$restarts"
  fi
}

snapshot_container() {
  local unit="$1" name="${containers[$1]}" output inspect_output cid pid running
  output="$(run_docker "$command_timeout" ps --all --quiet --no-trunc \
    --filter "name=^/${name}$")" || return 1
  if [[ -z "$output" ]]; then
    before_cid["$unit"]=""
    before_engine_pid["$unit"]="0"
    before_docker_cgroup["$unit"]=""
    return 0
  fi
  [[ "$output" =~ ^[0-9a-f]{64}$ ]] || return 1
  cid="$output"
  inspect_output="$(run_docker "$command_timeout" inspect --format \
    'id={{.Id}} pid={{.State.Pid}} running={{.State.Running}}' "$cid")" || return 1
  [[ "$inspect_output" =~ ^id=([0-9a-f]{64})\ pid=([0-9]+)\ running=(true|false)$ ]] || return 1
  [[ "${BASH_REMATCH[1]}" == "$cid" ]] || return 1
  pid="${BASH_REMATCH[2]}"
  running="${BASH_REMATCH[3]}"
  if [[ "$running" == "true" ]]; then
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  else
    [[ "$pid" == "0" ]] || return 1
  fi
  before_cid["$unit"]="$cid"
  before_engine_pid["$unit"]="$pid"
  before_docker_cgroup["$unit"]="$docker_cgroup_prefix/docker-${cid}.scope"
}

verify_pid_gone() {
  local pid="$1"
  [[ "$pid" == "0" || ! -e "$proc_root/$pid" ]]
}

verify_cgroup_empty() {
  local control_group="$1" label="$2" path line populated_count=0 populated="" process_count=0
  [[ -n "$control_group" ]] || return 0
  path="$cgroup_root$control_group"
  [[ ! -e "$path" ]] && return 0
  [[ -d "$path" && ! -L "$path" && -f "$path/cgroup.events" \
    && ! -L "$path/cgroup.events" && -f "$path/cgroup.procs" \
    && ! -L "$path/cgroup.procs" ]] || {
    emit_event "cgroup_not_empty unit=$label reason=unsafe_cgroup_files"
    return 1
  }
  while IFS= read -r line; do
    if [[ "$line" == populated\ * ]]; then
      populated_count=$((populated_count + 1))
      populated="${line#populated }"
    fi
  done <"$path/cgroup.events" || return 1
  [[ "$populated_count" == 1 && "$populated" == "0" ]] || {
    emit_event "cgroup_not_empty unit=$label reason=populated"
    return 1
  }
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^[1-9][0-9]*$ ]] || return 1
    process_count=$((process_count + 1))
  done <"$path/cgroup.procs" || return 1
  if ((process_count != 0)); then
    emit_event "cgroup_not_empty unit=$label reason=processes count=$process_count"
    return 1
  fi
}

verify_container_absent() {
  local unit="$1" name="${containers[$1]}" output
  output="$(run_docker "$command_timeout" ps --all --quiet --no-trunc \
    --filter "name=^/${name}$")" || return 1
  [[ -z "$output" ]] || {
    emit_event "container_still_present unit=$unit"
    return 1
  }
}

verify_unit_released() {
  local unit="$1" current_main="" output
  output="$(run_systemctl "$command_timeout" show \
    --property=LoadState --property=ActiveState --property=SubState \
    --property=MainPID --property=NRestarts --property=ControlGroup "$unit")" || {
    emit_event "release_check_failed unit=$unit reason=systemctl"
    return 1
  }
  [[ "$output" == *$'LoadState=loaded\n'* || "$output" == LoadState=loaded$'\n'* ]] || {
    emit_event "release_check_failed unit=$unit reason=load_state"
    return 1
  }
  [[ "$output" == *$'ActiveState=inactive\n'* || "$output" == *$'ActiveState=failed\n'* ]] || {
    emit_event "release_check_failed unit=$unit reason=active_state"
    return 1
  }
  current_main="$(while IFS= read -r line; do
    [[ "$line" == MainPID=* ]] && printf '%s' "${line#MainPID=}"
  done <<<"$output")"
  [[ "$current_main" == "0" ]] || {
    emit_event "release_check_failed unit=$unit reason=main_pid"
    return 1
  }
  verify_pid_gone "${before_pid[$unit]}" || {
    emit_event "release_check_failed unit=$unit reason=prior_main_pid"
    return 1
  }
  verify_pid_gone "${before_engine_pid[$unit]}" || {
    emit_event "release_check_failed unit=$unit reason=prior_engine_pid"
    return 1
  }
  verify_cgroup_empty "${before_cgroup[$unit]}" "$unit" || return 1
  verify_cgroup_empty "${before_docker_cgroup[$unit]}" "$unit" || return 1
  verify_container_absent "$unit" || {
    emit_event "release_check_failed unit=$unit reason=container_state"
    return 1
  }
}

wait_for_readiness() {
  local unit="$1" port="${ports[$1]}" deadline url curl_deadline sleep_deadline
  deadline=$((SECONDS + readiness_timeout))
  url="http://${endpoint_host}:${port}/v1/models"
  curl_deadline=$((probe_timeout + kill_after + 1))
  sleep_deadline=$((poll_seconds + kill_after + 1))
  while ((SECONDS <= deadline)); do
    if run_systemctl "$command_timeout" is-active --quiet "$unit" >/dev/null 2>&1 \
      && run_curl "$curl_deadline" --fail --silent --show-error \
        --max-time "$probe_timeout" --output /dev/null "$url" >/dev/null 2>&1; then
      emit_event "ready unit=$unit port=$port"
      return 0
    fi
    ((SECONDS >= deadline)) && break
    run_file_command "$sleep_deadline" "$sleep_bin" "$poll_seconds" >/dev/null 2>&1 || return 1
  done
  return 1
}

cleanup_failed_stage() {
  local unit="$1"
  emit_event "cleanup_failed_stage unit=$unit"
  if ! run_systemctl "$stop_timeout" stop "$unit"; then
    emit_event "cleanup_stop_failed unit=$unit"
    return 1
  fi
  if ! verify_unit_released "$unit"; then
    emit_event "cleanup_release_unverified unit=$unit"
    return 1
  fi
}

unit_key() {
  case "$1" in
    "$embedding_unit") printf 'vllm_embedding' ;;
    "$reranker_unit") printf 'querit_4b_reranker' ;;
    "$text_unit") printf 'vllm_aeon_27b_dflash' ;;
    *) return 1 ;;
  esac
}

write_receipt() {
  local unit key
  receipt_tmp="$state_dir/.receipt.v1.$$"
  {
    printf 'version=1\n'
    printf 'result=success\n'
    printf 'boot_id=%s\n' "$boot_id"
    printf 'started_epoch=%s\n' "$started_epoch"
    printf 'release_epoch=%s\n' "$release_epoch"
    printf 'completed_epoch=%s\n' "$completed_epoch"
    printf 'min_mem_available_kib=%s\n' "$min_mem_available_kib"
    for unit in "${units[@]}"; do
      key="$(unit_key "$unit")" || return 1
      printf '%s_before_pid=%s\n' "$key" "${before_pid[$unit]}"
      printf '%s_after_pid=%s\n' "$key" "${after_pid[$unit]}"
      printf '%s_before_nrestarts=%s\n' "$key" "${before_restarts[$unit]}"
      printf '%s_after_nrestarts=%s\n' "$key" "${after_restarts[$unit]}"
    done
  } >"$receipt_tmp" || return 1
  run_file_command "$command_timeout" "$mv_bin" -f -- "$receipt_tmp" "$receipt_path" || return 1
  receipt_tmp=""
}

main() {
  local unit stop_failed=0 threshold_kib marker_status
  validate_configuration || exit $?
  run_file_command "$command_timeout" "$install_bin" -d -m 0700 -- "$runtime_dir" "$state_dir" \
    || fail_closed "state_directory_failed"
  [[ -d "$runtime_dir" && ! -L "$runtime_dir" && -d "$state_dir" && ! -L "$state_dir" ]] \
    || fail_closed "unsafe_state_directory"

  exec {lock_fd}>"$lock_path" || fail_closed "lock_open_failed"
  if ! run_flock "$command_timeout" --nonblock "$lock_fd"; then
    emit_event "lock_busy"
    exit 0
  fi

  read_boot_id || fail_closed "boot_id_invalid"
  started_epoch="$(current_epoch)"
  write_attempt_marker
  marker_status=$?
  if [[ "$marker_status" == "75" ]]; then
    exit 75
  elif [[ "$marker_status" != "0" ]]; then
    fail_closed "attempt_marker_invalid"
  fi

  evidence_path="$state_dir/attempt-${boot_id}.v1"
  printf 'version=1\nboot_id=%s\nstarted_epoch=%s\n' \
    "$boot_id" "$started_epoch" >"$evidence_path" || fail_closed "evidence_open_failed"
  evidence_lines=3
  emit_event "attempt_started boot_id=$boot_id started_epoch=$started_epoch"

  for unit in "${units[@]}"; do
    parse_unit_snapshot "$unit" before || fail_closed "snapshot_failed unit=$unit"
    snapshot_container "$unit" || fail_closed "snapshot_failed unit=$unit boundary=docker"
  done
  read_mem_available || fail_closed "meminfo_invalid"
  emit_event "memory_sample phase=before_stop mem_available_kib=$mem_available_kib"

  for unit in "${stop_units[@]}"; do
    if ! run_systemctl "$stop_timeout" stop "$unit"; then
      emit_event "stop_failed unit=$unit"
      stop_failed=1
    else
      emit_event "stopped unit=$unit"
    fi
  done
  ((stop_failed == 0)) || fail_closed "stop_failed"

  for unit in "${units[@]}"; do
    verify_unit_released "$unit" || fail_closed "cgroup_or_pid_release_failed unit=$unit"
  done
  read_mem_available || fail_closed "meminfo_invalid_after_stop"
  threshold_kib=$((minimum_available_gib * 1048576))
  emit_event "memory_sample phase=after_stop mem_available_kib=$mem_available_kib threshold_kib=$threshold_kib"
  if ((mem_available_kib < threshold_kib)); then
    fail_closed "release_threshold_not_met"
  fi
  release_epoch="$(current_epoch)"
  emit_event "uma_release_verified release_epoch=$release_epoch"

  for unit in "${units[@]}"; do
    emit_event "start_begin unit=$unit"
    if ! run_systemctl "$start_timeout" start "$unit"; then
      cleanup_failed_stage "$unit" || true
      fail_closed "start_failed unit=$unit"
    fi
    if ! wait_for_readiness "$unit"; then
      cleanup_failed_stage "$unit" || true
      fail_closed "readiness_failed unit=$unit"
    fi
    parse_unit_snapshot "$unit" after || {
      cleanup_failed_stage "$unit" || true
      fail_closed "final_snapshot_failed unit=$unit"
    }
    read_mem_available || {
      cleanup_failed_stage "$unit" || true
      fail_closed "meminfo_invalid_after_start unit=$unit"
    }
    emit_event "memory_sample phase=ready unit=$unit mem_available_kib=$mem_available_kib"
  done

  completed_epoch="$(current_epoch)"
  write_receipt || fail_closed "receipt_write_failed"
  emit_event "result=success completed_epoch=$completed_epoch receipt=$receipt_path"
  trap - EXIT
  return 0
}

main "$@"
