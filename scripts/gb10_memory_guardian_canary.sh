#!/usr/bin/env bash
# Disposable direct-kill canary plus read-only configured-target identity proof.
set -Eeuo pipefail

usage() {
  cat >&2 <<'EOF'
usage:
  GB10_BENCHMARK_EXCLUDED=YES gb10_memory_guardian_canary.sh disposable
  GB10_BENCHMARK_EXCLUDED=YES \
    GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT=vllm-aeon-27b-dflash.service \
    gb10_memory_guardian_canary.sh configured-target
EOF
  exit 2
}

[[ "${GB10_BENCHMARK_EXCLUDED:-}" == "YES" ]] || {
  echo "refusing canary: set GB10_BENCHMARK_EXCLUDED=YES only after excluding all benchmark load" >&2
  exit 2
}

mode="${1:-}"
guardian_bin="${GB10_MEMORY_GUARDIAN_BIN:-$HOME/.local/bin/gb10-memory-guardian}"
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${UID}}/gb10-memory-guardian"
stamp="$runtime_dir/disposable-canary.passed"
canary_unit="gb10-memory-guardian-disposable-canary.service"
driver_unit="gb10-memory-guardian-canary.service"
text_unit="vllm-aeon-27b-dflash.service"
guardian_unit="gb10-memory-guardian.service"
systemctl_bin="${GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
systemd_run_bin="${GB10_MEMORY_GUARDIAN_SYSTEMD_RUN_BIN:-/usr/bin/systemd-run}"
command_timeout_seconds="${GB10_MEMORY_GUARDIAN_CANARY_COMMAND_TIMEOUT_SECONDS:-10}"
protected_units=(
  vllm-embedding.service
  querit-4b-reranker.service
  vllm-qwen3-reranker-8b.service
  llm-guard-proxy.service
  gb10-memory-guardian.service
)

[[ "$command_timeout_seconds" =~ ^[1-9][0-9]*$ ]] || {
  echo "canary command timeout must be a positive integer" >&2
  exit 2
}
[[ -x "$guardian_bin" ]] || {
  echo "guardian binary is not executable: $guardian_bin" >&2
  exit 1
}

run_systemctl() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$command_timeout_seconds" \
    "$systemctl_bin" --user "$@"
}

run_systemd_run() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$command_timeout_seconds" \
    "$systemd_run_bin" --user "$@"
}


# Populated by load_unit_state. Every requested systemd field must appear exactly
# once, with no unrequested lines. Callers then compare exact values rather than
# accepting grep/substring matches such as Result=successfully.
UNIT_LOAD_STATE=""
UNIT_ACTIVE_STATE=""
UNIT_SUB_STATE=""
UNIT_MAIN_PID=""
UNIT_RESULT=""
UNIT_EXEC_MAIN_CODE=""
UNIT_EXEC_MAIN_STATUS=""
UNIT_NRESTARTS=""
UNIT_RESTART=""
UNIT_ENVIRONMENT=""
UNIT_INVOCATION_ID=""
load_unit_state() {
  local unit="$1" output line key value
  output="$(run_systemctl show "$unit" \
    --property=LoadState \
    --property=ActiveState \
    --property=SubState \
    --property=MainPID \
    --property=Result \
    --property=ExecMainCode \
    --property=ExecMainStatus \
    --property=NRestarts \
    --property=Restart \
    --property=Environment \
    --property=InvocationID)" || {
      echo "bounded systemd state query failed: $unit" >&2
      return 1
    }

  declare -A fields=()
  while IFS= read -r line; do
    [[ "$line" == *=* ]] || {
      echo "malformed systemd state line for $unit" >&2
      return 1
    }
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      LoadState|ActiveState|SubState|MainPID|Result|ExecMainCode|ExecMainStatus|NRestarts|Restart|Environment|InvocationID) ;;
      *)
        echo "unexpected systemd state field for $unit: $key" >&2
        return 1
        ;;
    esac
    [[ ! -v "fields[$key]" ]] || {
      echo "duplicate systemd state field for $unit: $key" >&2
      return 1
    }
    fields["$key"]="$value"
  done <<<"$output"

  local required
  for required in LoadState ActiveState SubState MainPID Result ExecMainCode ExecMainStatus NRestarts Restart Environment InvocationID; do
    [[ -v "fields[$required]" ]] || {
      echo "missing systemd state field for $unit: $required" >&2
      return 1
    }
  done
  [[ "${#fields[@]}" == "11" ]] || {
    echo "unexpected systemd state field count for $unit" >&2
    return 1
  }
  [[ "${fields[MainPID]}" =~ ^[0-9]+$ \
    && "${fields[ExecMainCode]}" =~ ^[0-9]+$ \
    && "${fields[ExecMainStatus]}" =~ ^[0-9]+$ \
    && "${fields[NRestarts]}" =~ ^[0-9]+$ ]] || {
    echo "non-numeric systemd PID/exit/restart field for $unit" >&2
    return 1
  }
  [[ "${fields[LoadState]}" =~ ^[a-z][a-z-]*$ \
    && "${fields[ActiveState]}" =~ ^[a-z][a-z-]*$ \
    && "${fields[SubState]}" =~ ^[a-z][a-z-]*$ \
    && "${fields[Result]}" =~ ^[a-z][a-z-]*$ \
    && "${fields[Restart]}" =~ ^[a-z][a-z-]*$ ]] || {
    echo "malformed systemd enum field for $unit" >&2
    return 1
  }
  [[ "${fields[InvocationID]}" =~ ^[0-9a-f]{32}$ ]] || {
    echo "malformed systemd invocation ID for $unit" >&2
    return 1
  }

  UNIT_LOAD_STATE="${fields[LoadState]}"
  UNIT_ACTIVE_STATE="${fields[ActiveState]}"
  UNIT_SUB_STATE="${fields[SubState]}"
  UNIT_MAIN_PID="${fields[MainPID]}"
  UNIT_RESULT="${fields[Result]}"
  UNIT_EXEC_MAIN_CODE="${fields[ExecMainCode]}"
  UNIT_EXEC_MAIN_STATUS="${fields[ExecMainStatus]}"
  UNIT_NRESTARTS="${fields[NRestarts]}"
  UNIT_RESTART="${fields[Restart]}"
  UNIT_ENVIRONMENT="${fields[Environment]}"
  UNIT_INVOCATION_ID="${fields[InvocationID]}"
}

require_running_unit() {
  local unit="$1"
  load_unit_state "$unit"
  [[ "$UNIT_LOAD_STATE" == "loaded" \
    && "$UNIT_ACTIVE_STATE" == "active" \
    && "$UNIT_SUB_STATE" == "running" \
    && "$UNIT_MAIN_PID" =~ ^[1-9][0-9]*$ \
    && "$UNIT_RESULT" == "success" \
    && "$UNIT_EXEC_MAIN_STATUS" == "0" ]] || {
    echo "unit is not strictly loaded/active/running/successful: $unit" >&2
    return 1
  }
}

unit_fingerprint() {
  local unit="$1"
  load_unit_state "$unit"
  [[ "$UNIT_LOAD_STATE" == "loaded" ]] || {
    echo "protected unit is not loaded and cannot be safely snapshotted: $unit load=$UNIT_LOAD_STATE" >&2
    return 1
  }
  printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\n' \
    "$UNIT_LOAD_STATE" "$UNIT_ACTIVE_STATE" "$UNIT_SUB_STATE" "$UNIT_MAIN_PID" \
    "$UNIT_RESULT" "$UNIT_EXEC_MAIN_CODE" "$UNIT_EXEC_MAIN_STATUS" "$UNIT_NRESTARTS" \
    "$UNIT_INVOCATION_ID"
}

declare -A protected_before
snapshot_protected() {
  local unit
  for unit in "${protected_units[@]}"; do
    protected_before["$unit"]="$(unit_fingerprint "$unit")"
  done
}

verify_protected_unchanged() {
  local unit current
  for unit in "${protected_units[@]}"; do
    current="$(unit_fingerprint "$unit")"
    [[ "$current" == "${protected_before[$unit]}" ]] || {
      echo "protected unit state/PID/result/restart tuple changed: $unit" >&2
      return 1
    }
  done
}

write_attestation() {
  /usr/bin/install -d -m 0700 "$runtime_dir"
  {
    printf 'binary_sha256='
    sha256sum "$guardian_bin" | awk '{print $1}'
    printf 'passed_epoch=%s\n' "$(date +%s)"
  } >"$stamp.tmp"
  chmod 0600 "$stamp.tmp"
  mv -f -- "$stamp.tmp" "$stamp"
}

verify_attestation() {
  [[ -f "$stamp" && ! -L "$stamp" ]] || {
    echo "run the disposable canary first" >&2
    return 1
  }
  local stamp_sha stamp_epoch current_sha now_epoch
  stamp_sha="$(sed -n 's/^binary_sha256=//p' "$stamp")"
  stamp_epoch="$(sed -n 's/^passed_epoch=//p' "$stamp")"
  current_sha="$(sha256sum "$guardian_bin" | awk '{print $1}')"
  now_epoch="$(date +%s)"
  if [[ ! "$stamp_epoch" =~ ^[0-9]+$ || "$stamp_sha" != "$current_sha" \
    || $((now_epoch - stamp_epoch)) -lt 0 || $((now_epoch - stamp_epoch)) -gt 3600 ]]; then
    echo "disposable canary attestation is stale or for another binary" >&2
    return 1
  fi
}

run_disposable() {
  [[ $# -eq 0 ]] || usage
  # The real text service is protected during this disposable-only phase.
  protected_units+=("$text_unit")
  snapshot_protected
  rm -f -- "$stamp"
  run_systemctl stop "$canary_unit" >/dev/null 2>&1 || true
  run_systemctl reset-failed "$canary_unit" >/dev/null 2>&1 || true
  run_systemctl revert "$canary_unit" >/dev/null 2>&1 || true
  run_systemd_run \
    --unit="$canary_unit" \
    --slice=app.slice \
    --property=Type=simple \
    --property=Restart=no \
    /usr/bin/sleep infinity >/dev/null
  cleanup_canary() {
    run_systemctl stop "$canary_unit" >/dev/null 2>&1 || true
    run_systemctl reset-failed "$canary_unit" >/dev/null 2>&1 || true
    run_systemctl revert "$canary_unit" >/dev/null 2>&1 || true
  }
  trap cleanup_canary EXIT

  local ready=0
  for _ in $(seq 1 20); do
    if require_running_unit "$canary_unit" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 0.25
  done
  [[ "$ready" == "1" ]] || {
    echo "disposable canary did not become strictly active" >&2
    exit 1
  }

  run_systemctl reset-failed "$driver_unit" >/dev/null 2>&1 || true
  run_systemctl start "$driver_unit"
  load_unit_state "$driver_unit"
  [[ "$UNIT_LOAD_STATE" == "loaded" \
    && "$UNIT_ACTIVE_STATE" == "inactive" \
    && "$UNIT_SUB_STATE" == "dead" \
    && "$UNIT_MAIN_PID" == "0" \
    && "$UNIT_RESULT" == "success" \
    && "$UNIT_EXEC_MAIN_CODE" == "1" \
    && "$UNIT_EXEC_MAIN_STATUS" == "0" ]] || {
    echo "sandboxed disposable guardian driver did not report a strict successful exit" >&2
    exit 1
  }

  load_unit_state "$canary_unit"
  [[ "$UNIT_LOAD_STATE" == "loaded" \
    && "$UNIT_ACTIVE_STATE" == "failed" \
    && "$UNIT_SUB_STATE" == "failed" \
    && "$UNIT_MAIN_PID" == "0" \
    && "$UNIT_RESULT" == "signal" \
    && "$UNIT_EXEC_MAIN_CODE" == "2" \
    && "$UNIT_EXEC_MAIN_STATUS" == "9" ]] || {
    echo "direct canary kill did not produce a strict terminal disposable state" >&2
    exit 1
  }
  verify_protected_unchanged
  write_attestation
  trap - EXIT
  cleanup_canary
  echo "disposable direct-kill canary passed; protected services were unchanged"
}

validate_text_registration() {
  local registration_path="$1"
  /usr/bin/python3 - "$registration_path" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
metadata = os.fstat(fd)
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.geteuid()
    or stat.S_IMODE(metadata.st_mode) != 0o600
    or metadata.st_nlink != 1
):
    os.close(fd)
    raise SystemExit("unsafe text registration source")
with os.fdopen(fd, encoding="ascii") as source:
    lines = source.read().splitlines()
if len(lines) != 4 or lines[0] != "version=1":
    raise SystemExit("invalid text registration schema")
fields = {}
for line in lines[1:]:
    if "=" not in line:
        raise SystemExit("invalid text registration field")
    key, value = line.split("=", 1)
    if key in fields:
        raise SystemExit("duplicate text registration field")
    fields[key] = value
if set(fields) != {"container_id", "scope", "control_group"}:
    raise SystemExit("unexpected text registration fields")
container_id = fields["container_id"]
if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
    raise SystemExit("invalid text registration container ID")
scope = f"docker-{container_id}.scope"
expected = (
    f"/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/"
    f"app.slice/{scope}"
)
if fields["scope"] != scope or fields["control_group"] != expected:
    raise SystemExit("text registration is not the current user's app.slice scope")
PY
}

verify_current_guardian_status() {
  local registration_path="$1" guardian_pid="$2" guardian_invocation_id="$3"
  local status_path="$runtime_dir/guardian-status.v2"
  local cgroup_root="${GB10_MEMORY_GUARDIAN_CGROUP_ROOT:-/sys/fs/cgroup}"
  /usr/bin/python3 - "$runtime_dir" "$status_path" "$registration_path" "$cgroup_root" \
    "$guardian_pid" "$guardian_invocation_id" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

runtime_dir, status_path, registration_path, cgroup_root = map(Path, sys.argv[1:5])
expected_pid, expected_invocation_id = sys.argv[5:7]
if status_path.parent != runtime_dir or registration_path.parent != runtime_dir:
    raise RuntimeError("current receipts escaped the guardian runtime directory")
if status_path.name != "guardian-status.v2" or registration_path.name != "text-cgroup.v1":
    raise RuntimeError("unexpected current receipt name")


def generation(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns // 1_000_000_000,
        metadata.st_mtime_ns % 1_000_000_000,
        metadata.st_ctime_ns // 1_000_000_000,
        metadata.st_ctime_ns % 1_000_000_000,
    )


def open_owner_directory(path: Path) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path, flags)
    metadata = os.fstat(fd)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(fd)
        raise RuntimeError("guardian runtime directory is not owner-only")
    return fd, (metadata.st_dev, metadata.st_ino)


def read_secure_at(directory_fd: int, name: str) -> tuple[bytes, tuple[int, ...]]:
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(fd)
        raise RuntimeError(f"unsafe current-status source: {name}")
    with os.fdopen(fd, "rb") as source:
        data = source.read(4097)
    if len(data) > 4096 or not data or not data.endswith(b"\n") or b"\r" in data:
        raise RuntimeError(f"invalid current-status size or termination: {name}")
    return data, generation(metadata)


def parse_ordered(data: bytes, keys: list[str]) -> dict[str, str]:
    try:
        decoded = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError("non-ASCII current-status source") from error
    lines = decoded[:-1].split("\n")
    if len(lines) != len(keys):
        raise RuntimeError("wrong current-status field count")
    fields: dict[str, str] = {}
    for line, expected_key in zip(lines, keys, strict=True):
        if "=" not in line:
            raise RuntimeError("malformed current-status field")
        key, value = line.split("=", 1)
        if key != expected_key or not value or key in fields:
            raise RuntimeError("unexpected or duplicate current-status field")
        fields[key] = value
    return fields


def exact_nonnegative(value: str, field: str) -> int:
    if re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise RuntimeError(f"invalid {field}")
    return int(value)


def open_cgroup(control_group: str) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(cgroup_root, flags)
    try:
        for component in control_group.removeprefix("/").split("/"):
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


runtime_fd, runtime_identity = open_owner_directory(runtime_dir)
try:
    status_bytes, status_generation = read_secure_at(runtime_fd, status_path.name)
    registration_bytes, registration_generation = read_secure_at(
        runtime_fd, registration_path.name
    )
    status = parse_ordered(
        status_bytes,
        [
            "version",
            "state",
            "label",
            "registration_file",
            "registration_device",
            "registration_inode",
            "registration_size",
            "registration_modified_seconds",
            "registration_modified_nanoseconds",
            "registration_changed_seconds",
            "registration_changed_nanoseconds",
            "container_id",
            "scope",
            "control_group",
            "cgroup_device",
            "cgroup_inode",
            "guardian_pid",
            "guardian_invocation_id",
        ],
    )
    registration = parse_ordered(
        registration_bytes,
        ["version", "container_id", "scope", "control_group"],
    )
    if status["version"] != "2" or registration["version"] != "1":
        raise RuntimeError("unsupported current-status version")
    if status["state"] != "armed":
        raise RuntimeError("latest guardian state is not armed")
    if status["label"] != "aeon-text" or status["registration_file"] != registration_path.name:
        raise RuntimeError("current-status target identity mismatch")
    if (
        status["guardian_pid"] != expected_pid
        or re.fullmatch(r"[1-9][0-9]*", expected_pid) is None
        or status["guardian_invocation_id"] != expected_invocation_id
        or re.fullmatch(r"[0-9a-f]{32}", expected_invocation_id) is None
    ):
        raise RuntimeError("current-status receipt is not from the running guardian generation")

    registration_fields = (
        "registration_device",
        "registration_inode",
        "registration_size",
        "registration_modified_seconds",
        "registration_modified_nanoseconds",
        "registration_changed_seconds",
        "registration_changed_nanoseconds",
    )
    recorded_registration_generation = tuple(
        exact_nonnegative(status[field], field) for field in registration_fields
    )
    if recorded_registration_generation != registration_generation:
        raise RuntimeError("current-status receipt does not bind the current registration generation")

    container_id = registration["container_id"]
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise RuntimeError("invalid current registration container identity")
    scope = f"docker-{container_id}.scope"
    control_group = (
        f"/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/"
        f"app.slice/{scope}"
    )
    if registration["scope"] != scope or registration["control_group"] != control_group:
        raise RuntimeError("registration is not the exact current-user Docker cgroup")
    for key, expected in (
        ("container_id", container_id),
        ("scope", scope),
        ("control_group", control_group),
    ):
        if status[key] != expected:
            raise RuntimeError("current-status receipt does not match current registration")

    expected_device = exact_nonnegative(status["cgroup_device"], "cgroup device")
    expected_inode = exact_nonnegative(status["cgroup_inode"], "cgroup inode")
    if expected_device <= 0 or expected_inode <= 0:
        raise RuntimeError("missing cgroup generation identity")
    cgroup_fd = open_cgroup(control_group)
    try:
        cgroup_metadata = os.fstat(cgroup_fd)
        if (
            cgroup_metadata.st_dev != expected_device
            or cgroup_metadata.st_ino != expected_inode
        ):
            raise RuntimeError("configured cgroup generation was replaced")
        events_fd = os.open(
            "cgroup.events",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=cgroup_fd,
        )
        events_metadata = os.fstat(events_fd)
        if not stat.S_ISREG(events_metadata.st_mode) or events_metadata.st_nlink != 1:
            os.close(events_fd)
            raise RuntimeError("unsafe cgroup.events identity")
        with os.fdopen(events_fd, "rb") as events_source:
            events_data = events_source.read(4097)
        if (
            len(events_data) > 4096
            or not events_data.endswith(b"\n")
            or b"\r" in events_data
        ):
            raise RuntimeError("cgroup.events is oversized or noncanonical")
        try:
            event_lines = events_data.decode("ascii")[:-1].split("\n")
        except UnicodeDecodeError as error:
            raise RuntimeError("cgroup.events is not ASCII") from error
        event_fields: dict[str, str] = {}
        for line in event_lines:
            parts = line.split(" ")
            if (
                len(parts) != 2
                or not parts[0]
                or re.fullmatch(r"0|[1-9][0-9]*", parts[1]) is None
                or parts[0] in event_fields
            ):
                raise RuntimeError("malformed or duplicate cgroup.events field")
            event_fields[parts[0]] = parts[1]
        if event_fields.get("populated") != "1":
            raise RuntimeError("configured cgroup is not populated")

        confirming_cgroup_fd = open_cgroup(control_group)
        try:
            confirming = os.fstat(confirming_cgroup_fd)
            if (confirming.st_dev, confirming.st_ino) != (
                cgroup_metadata.st_dev,
                cgroup_metadata.st_ino,
            ):
                raise RuntimeError("configured cgroup changed during verification")
        finally:
            os.close(confirming_cgroup_fd)
    finally:
        os.close(cgroup_fd)

    confirming_status, confirming_status_generation = read_secure_at(
        runtime_fd, status_path.name
    )
    confirming_registration, confirming_registration_generation = read_secure_at(
        runtime_fd, registration_path.name
    )
    if (
        (confirming_status, confirming_status_generation)
        != (status_bytes, status_generation)
        or (confirming_registration, confirming_registration_generation)
        != (registration_bytes, registration_generation)
    ):
        raise RuntimeError("current identity changed during verification")
finally:
    os.close(runtime_fd)

confirming_runtime_fd, confirming_runtime_identity = open_owner_directory(runtime_dir)
os.close(confirming_runtime_fd)
if confirming_runtime_identity != runtime_identity:
    raise RuntimeError("guardian runtime directory changed during verification")
PY
}

run_configured_target() {
  [[ $# -eq 0 ]] || usage
  local target_unit="${GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT:-$text_unit}"
  [[ "$target_unit" == "$text_unit" ]] || {
    echo "configured-target identity check accepts only $text_unit" >&2
    exit 2
  }
  verify_attestation

  local config_path config_identity label registration_file registration_path
  config_path="${GB10_MEMORY_GUARDIAN_CONFIG_PATH:-${XDG_CONFIG_HOME:-$HOME/.config}/gb10-memory-guardian/config.toml}"
  config_identity="$(
    /usr/bin/python3 - "$config_path" <<'PY'
import os
import stat
import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1])
fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
metadata = os.fstat(fd)
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.geteuid()
    or stat.S_IMODE(metadata.st_mode) != 0o600
    or metadata.st_nlink != 1
):
    os.close(fd)
    raise SystemExit("unsafe guardian config source")
with os.fdopen(fd, "rb") as source:
    config = tomllib.load(source)
if set(config) != {"schema_version", "target"} or config["schema_version"] != 1:
    raise SystemExit("unsupported guardian config schema")
target = config["target"]
if set(target) != {"label", "registration_file"}:
    raise SystemExit("unexpected guardian target fields")
if target["label"] != "aeon-text" or target["registration_file"] != "text-cgroup.v1":
    raise SystemExit("guardian target must be aeon-text/text-cgroup.v1")
print(f'{target["label"]}\t{target["registration_file"]}')
PY
  )"
  IFS=$'\t' read -r label registration_file <<<"$config_identity"
  [[ "$label" == "aeon-text" && "$registration_file" == "text-cgroup.v1" ]] || {
    echo "guardian target identity could not be proved" >&2
    exit 1
  }

  registration_path="$runtime_dir/$registration_file"
  if [[ -e "$runtime_dir/querit-cgroup.v1" ]]; then
    echo "refusing stale guardian state: querit-cgroup.v1 is still present" >&2
    exit 1
  fi
  [[ -f "$registration_path" && ! -L "$registration_path" ]] || {
    echo "configured text registration is missing or unsafe: $registration_path" >&2
    exit 1
  }
  validate_text_registration "$registration_path"

  require_running_unit "$target_unit"
  [[ "$UNIT_RESTART" == "on-failure" ]] || {
    echo "text target must use Restart=on-failure" >&2
    exit 1
  }
  local expected_environment="GB10_CGROUP_REGISTRATION_PATH=$registration_path"
  local token registration_environment_count=0
  for token in $UNIT_ENVIRONMENT; do
    if [[ "$token" == "$expected_environment" ]]; then
      registration_environment_count=$((registration_environment_count + 1))
    fi
  done
  [[ "$registration_environment_count" == "1" ]] || {
    echo "text unit does not publish exactly $registration_path" >&2
    exit 1
  }
  local text_generation="$UNIT_MAIN_PID|$UNIT_INVOCATION_ID|$UNIT_RESTART|$UNIT_ENVIRONMENT"

  require_running_unit "$guardian_unit"
  [[ "$UNIT_RESTART" == "always" ]] || {
    echo "production guardian must use Restart=always" >&2
    exit 1
  }
  local guardian_pid="$UNIT_MAIN_PID"
  local guardian_invocation_id="$UNIT_INVOCATION_ID"
  local guardian_generation="$UNIT_MAIN_PID|$UNIT_INVOCATION_ID|$UNIT_RESTART"

  verify_current_guardian_status \
    "$registration_path" "$guardian_pid" "$guardian_invocation_id"

  require_running_unit "$target_unit"
  [[ "$UNIT_MAIN_PID|$UNIT_INVOCATION_ID|$UNIT_RESTART|$UNIT_ENVIRONMENT" == "$text_generation" ]] || {
    echo "text target generation changed during current-status verification" >&2
    exit 1
  }
  require_running_unit "$guardian_unit"
  [[ "$UNIT_MAIN_PID|$UNIT_INVOCATION_ID|$UNIT_RESTART" == "$guardian_generation" ]] || {
    echo "guardian generation changed during current-status verification" >&2
    exit 1
  }

  echo "configured-target read-only identity check passed; current aeon-text generation is strictly armed"
}

case "$mode" in
  disposable)
    shift
    run_disposable "$@"
    ;;
  configured-target)
    shift
    run_configured_target "$@"
    ;;
  *)
    usage
    ;;
esac
