#!/usr/bin/env bash
# Durable, fail-closed deployment for the text-only memory guardian.
set -Eeuo pipefail
umask 077

usage() {
  cat >&2 <<'EOF'
usage: GB10_BENCHMARK_EXCLUDED=YES gb10_deploy_memory_guardian.sh install|activate

Build target/release/gb10-memory-guardian from this reviewed checkout first.
Install atomically publishes a private rollback transaction and leaves the
 guardian disabled. Activate commits only after both bounded canaries pass.
EOF
  exit 2
}

mode="${1:-}"
[[ ( "$mode" == "install" || "$mode" == "activate" ) && $# -eq 1 ]] || usage
[[ "${GB10_BENCHMARK_EXCLUDED:-}" == "YES" ]] || {
  echo "refusing deployment: exclude benchmark load first" >&2
  exit 2
}

script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
root="${GB10_MEMORY_GUARDIAN_DEPLOY_SOURCE_ROOT:-$script_root}"
guardian_unit="gb10-memory-guardian.service"
text_unit="vllm-aeon-27b-dflash.service"
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${UID}}/gb10-memory-guardian"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/gb10-memory-guardian"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
bin_dir="$HOME/.local/bin"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/gb10-memory-guardian"
transaction_dir="$state_dir/deploy-transaction.v2"
systemctl_bin="${GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
command_timeout_seconds="${GB10_MEMORY_GUARDIAN_DEPLOY_TIMEOUT_SECONDS:-15}"
fail_at="${GB10_MEMORY_GUARDIAN_DEPLOY_FAIL_AT:-}"
transaction_ready=0
preserve_pending=0
committed=0
rollback_running=0
lock_acquired=0

[[ "$command_timeout_seconds" =~ ^[1-9][0-9]*$ ]] || {
  echo "deployment timeout must be a positive integer" >&2
  exit 2
}

run_systemctl() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$command_timeout_seconds" \
    "$systemctl_bin" --user "$@"
}

verify_guardian_disabled_inactive() {
  local enabled_output="" enabled_status=0 unit_state=""
  enabled_output="$(run_systemctl is-enabled "$guardian_unit" 2>/dev/null)" \
    || enabled_status=$?
  [[ "$enabled_status" == "1" && "$enabled_output" == "disabled" ]] || {
    echo "guardian must be disabled before canary activation" >&2
    return 1
  }
  unit_state="$(run_systemctl show "$guardian_unit" \
    --property=LoadState --property=ActiveState --property=SubState)"
  [[ "$unit_state" == $'LoadState=loaded\nActiveState=inactive\nSubState=dead' ]] || {
    echo "guardian must be strictly loaded/inactive/dead before canary activation" >&2
    return 1
  }
}

run_canary() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$command_timeout_seconds" \
    env GB10_BENCHMARK_EXCLUDED=YES \
    GB10_MEMORY_GUARDIAN_BIN="$bin_dir/gb10-memory-guardian" \
    GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT="$text_unit" \
    "$bin_dir/gb10_memory_guardian_canary.sh" "$1"
}

maybe_fail() {
  local boundary="$1"
  if [[ ",${fail_at}," == *",${boundary},"* ]]; then
    echo "injected deployment failure at $boundary" >&2
    return 97
  fi
}

source_config="$root/config/gb10-memory-guardian/config.toml"
source_binary="$root/target/release/gb10-memory-guardian"
source_helper="$root/scripts/gb10_enforce_docker_cgroup_limits.sh"
source_canary="$root/scripts/gb10_memory_guardian_canary.sh"
source_guardian_unit="$root/systemd/gb10-memory-guardian.service"
source_canary_unit="$root/systemd/gb10-memory-guardian-canary.service"
source_text_unit="$root/systemd/vllm-aeon-27b-dflash.service"
source_querit_unit="$root/systemd/querit-4b-reranker.service"
source_vllm_reranker_unit="$root/systemd/vllm-qwen3-reranker-8b.service"

sources=(
  "$source_config" "$source_binary" "$source_helper" "$source_canary"
  "$source_guardian_unit" "$source_canary_unit" "$source_text_unit"
  "$source_querit_unit" "$source_vllm_reranker_unit"
)
destinations=(
  "$config_dir/config.toml" "$bin_dir/gb10-memory-guardian"
  "$bin_dir/gb10_enforce_docker_cgroup_limits.sh"
  "$bin_dir/gb10_memory_guardian_canary.sh"
  "$unit_dir/gb10-memory-guardian.service"
  "$unit_dir/gb10-memory-guardian-canary.service"
  "$unit_dir/vllm-aeon-27b-dflash.service"
  "$unit_dir/querit-4b-reranker.service"
  "$unit_dir/vllm-qwen3-reranker-8b.service"
)
install_modes=(0600 0755 0755 0755 0644 0644 0644 0644 0644)
boundaries=(
  install-config install-binary install-helper install-canary
  install-guardian-unit install-canary-unit install-text-unit
  install-querit-unit install-vllm-reranker-unit
)

transaction_python() {
  local action="$1"
  shift
  /usr/bin/python3 - "$action" "$transaction_dir" "$state_dir" "$HOME" \
    "${sources[@]}" "${destinations[@]}" "${install_modes[@]}" "$@" <<'PY'
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

COUNT = 9
args = sys.argv[1:]
action, transaction_raw, state_raw, home_raw = args[:4]
sources = [Path(value) for value in args[4 : 4 + COUNT]]
destinations = [Path(value) for value in args[4 + COUNT : 4 + 2 * COUNT]]
modes = [int(value, 8) for value in args[4 + 2 * COUNT : 4 + 3 * COUNT]]
extra = args[4 + 3 * COUNT :]
transaction = Path(transaction_raw)
state_dir = Path(state_raw)
home = Path(home_raw)
uid = os.geteuid()
ALLOWED_PHASES = {
    "prepared", "installed", "disposable_passed", "guardian_started",
    "configured_passed", "rolling_back", "committed", "rollback_failed",
}
TRANSITIONS = {
    "prepared": {"installed", "rolling_back"},
    "installed": {"disposable_passed", "rolling_back"},
    "disposable_passed": {"guardian_started", "rolling_back"},
    "guardian_started": {"configured_passed", "rolling_back"},
    "configured_passed": {"committed", "rolling_back"},
    "rolling_back": {"rolling_back", "rollback_failed"},
    "rollback_failed": {"rolling_back", "rollback_failed"},
    "committed": set(),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def secure_lstat(path: Path, *, directory: bool, mode: int) -> os.stat_result:
    metadata = path.lstat()
    valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (
        not valid_type
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != uid
        or stat.S_IMODE(metadata.st_mode) != mode
        or (not directory and metadata.st_nlink != 1)
    ):
        raise RuntimeError(f"unsafe transaction authority: {path}")
    return metadata


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as sink:
            sink.write(data)
            sink.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, mode, follow_symlinks=False)
    fsync_directory(path.parent)


def decode_unique(payload: bytes) -> dict:
    if len(payload) > 131072 or not payload.endswith(b"\n"):
        raise RuntimeError("invalid transaction manifest size")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError(f"duplicate manifest field: {key}")
            result[key] = value
        return result
    return json.loads(payload, object_pairs_hook=unique)


def read_secure_file(path: Path, limit: int = 131072) -> bytes:
    secure_lstat(path, directory=False, mode=0o600)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        data = os.read(fd, limit + 1)
        if len(data) > limit or os.read(fd, 1):
            raise RuntimeError(f"oversized transaction file: {path}")
        return data
    finally:
        os.close(fd)


def parent_paths() -> list[Path]:
    result = set()
    for destination in destinations:
        parent = destination.parent
        while parent != home:
            if home not in parent.parents:
                raise RuntimeError(f"destination escapes HOME: {destination}")
            result.add(parent)
            parent = parent.parent
    return sorted(result, key=lambda value: (len(value.parts), str(value)))


def inspect_source(path: Path) -> tuple[bytes, os.stat_result]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(f"unsafe reviewed source: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        data = bytearray()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 16 * 1024 * 1024:
                raise RuntimeError(f"reviewed source is oversized: {path}")
        return bytes(data), metadata
    finally:
        os.close(fd)


def load_transaction() -> tuple[dict, str]:
    secure_lstat(state_dir, directory=True, mode=0o700)
    secure_lstat(transaction, directory=True, mode=0o700)
    manifest_bytes = read_secure_file(transaction / "manifest.json")
    complete = read_secure_file(transaction / "complete", 256)
    if complete != f"manifest_sha256={sha256(manifest_bytes)}\n".encode():
        raise RuntimeError("transaction completeness receipt mismatch")
    phase_bytes = read_secure_file(transaction / "phase", 64)
    try:
        phase = phase_bytes.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError as error:
        raise RuntimeError("invalid transaction phase") from error
    if phase not in ALLOWED_PHASES or phase_bytes != f"{phase}\n".encode():
        raise RuntimeError("invalid transaction phase")
    manifest = decode_unique(manifest_bytes)
    if set(manifest) != {"schema", "artifacts", "parents"} or manifest["schema"] != 2:
        raise RuntimeError("invalid transaction manifest schema")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != COUNT:
        raise RuntimeError("invalid transaction artifact count")
    artifact_keys = {
        "source", "destination", "source_sha256", "source_mode",
        "installed_sha256", "installed_mode", "prior",
    }
    prior_keys = {"present", "mode", "sha256", "backup"}
    expected_backups = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict) or set(artifact) != artifact_keys:
            raise RuntimeError("invalid transaction artifact fields")
        if artifact["source"] != str(sources[index]) or artifact["destination"] != str(destinations[index]):
            raise RuntimeError("transaction path mismatch")
        if (
            not isinstance(artifact["source_mode"], int)
            or isinstance(artifact["source_mode"], bool)
            or not 0 <= artifact["source_mode"] <= 0o7777
            or artifact["installed_mode"] != modes[index]
        ):
            raise RuntimeError("transaction install mode mismatch")
        if not isinstance(artifact["prior"], dict) or set(artifact["prior"]) != prior_keys:
            raise RuntimeError("invalid prior artifact fields")
        for field in ("source_sha256", "installed_sha256"):
            if not isinstance(artifact[field], str) or re.fullmatch(r"[0-9a-f]{64}", artifact[field]) is None:
                raise RuntimeError("invalid artifact hash")
        if artifact["installed_sha256"] != artifact["source_sha256"]:
            raise RuntimeError("source/install manifest hash mismatch")
        prior = artifact["prior"]
        if not isinstance(prior["present"], bool):
            raise RuntimeError("invalid prior presence")
        if prior["present"]:
            if (
                not isinstance(prior["mode"], int)
                or isinstance(prior["mode"], bool)
                or not 0 <= prior["mode"] <= 0o7777
                or not isinstance(prior["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", prior["sha256"]) is None
                or prior["backup"] != f"backups/{index}"
            ):
                raise RuntimeError("invalid prior artifact receipt")
            expected_backups.add(str(index))
        elif any(prior[field] is not None for field in ("mode", "sha256", "backup")):
            raise RuntimeError("absent prior artifact has backup fields")
    expected_parents = parent_paths()
    parents = manifest["parents"]
    if not isinstance(parents, list) or len(parents) != len(expected_parents):
        raise RuntimeError("invalid transaction parent count")
    for expected, record in zip(expected_parents, parents, strict=True):
        if not isinstance(record, dict) or set(record) != {"path", "present", "mode"}:
            raise RuntimeError("invalid transaction parent fields")
        if record["path"] != str(expected) or not isinstance(record["present"], bool):
            raise RuntimeError("transaction parent mismatch")
        if record["present"]:
            if (
                not isinstance(record["mode"], int)
                or isinstance(record["mode"], bool)
                or not 0 <= record["mode"] <= 0o7777
            ):
                raise RuntimeError("invalid prior parent mode")
        elif record["mode"] is not None:
            raise RuntimeError("absent prior parent has a mode")
    backup_dir = transaction / "backups"
    secure_lstat(backup_dir, directory=True, mode=0o700)
    actual_backups = {entry.name for entry in backup_dir.iterdir()}
    if actual_backups != expected_backups:
        raise RuntimeError("transaction backup set mismatch")
    for artifact in artifacts:
        prior = artifact["prior"]
        if prior["present"]:
            backup = transaction / prior["backup"]
            if sha256(read_secure_file(backup, 16 * 1024 * 1024)) != prior["sha256"]:
                raise RuntimeError(f"corrupt rollback backup: {backup}")
    return manifest, phase


def set_phase(new_phase: str, *, validate_manifest: bool = True) -> None:
    if new_phase not in ALLOWED_PHASES:
        raise RuntimeError("unsupported transaction phase")
    if validate_manifest:
        _, old_phase = load_transaction()
        if new_phase not in TRANSITIONS[old_phase]:
            raise RuntimeError(f"illegal transaction phase transition: {old_phase}->{new_phase}")
    else:
        secure_lstat(transaction, directory=True, mode=0o700)
        phase_path = transaction / "phase"
        if phase_path.exists():
            secure_lstat(phase_path, directory=False, mode=0o600)
    write_atomic(transaction / "phase", f"{new_phase}\n".encode(), 0o600)


def verify_current_file(path: Path, expected_hash: str, expected_mode: int) -> None:
    data, metadata = inspect_source(path)
    if sha256(data) != expected_hash or stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise RuntimeError(f"artifact drift: {path}")


if action == "create":
    secure_lstat(state_dir, directory=True, mode=0o700)
    if transaction.exists() or transaction.is_symlink():
        raise RuntimeError("transaction already exists")
    temporary = state_dir / f".{transaction.name}.tmp.{os.getpid()}"
    os.mkdir(temporary, 0o700)
    backups = temporary / "backups"
    os.mkdir(backups, 0o700)
    artifacts = []
    parents = []
    try:
        for parent in parent_paths():
            if parent.exists() or parent.is_symlink():
                metadata = parent.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != uid:
                    raise RuntimeError(f"unsafe destination parent: {parent}")
                parents.append({"path": str(parent), "present": True, "mode": stat.S_IMODE(metadata.st_mode)})
            else:
                parents.append({"path": str(parent), "present": False, "mode": None})
        for index, (source, destination, install_mode) in enumerate(zip(sources, destinations, modes, strict=True)):
            source_data, source_metadata = inspect_source(source)
            prior = {"present": False, "mode": None, "sha256": None, "backup": None}
            if destination.exists() or destination.is_symlink():
                prior_data, prior_metadata = inspect_source(destination)
                backup = backups / str(index)
                write_atomic(backup, prior_data, 0o600)
                prior = {
                    "present": True,
                    "mode": stat.S_IMODE(prior_metadata.st_mode),
                    "sha256": sha256(prior_data),
                    "backup": f"backups/{index}",
                }
            digest = sha256(source_data)
            artifacts.append({
                "source": str(source),
                "destination": str(destination),
                "source_sha256": digest,
                "source_mode": stat.S_IMODE(source_metadata.st_mode),
                "installed_sha256": digest,
                "installed_mode": install_mode,
                "prior": prior,
            })
        manifest_bytes = (
            json.dumps({"schema": 2, "artifacts": artifacts, "parents": parents}, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        write_atomic(temporary / "manifest.json", manifest_bytes, 0o600)
        write_atomic(temporary / "phase", b"prepared\n", 0o600)
        write_atomic(temporary / "complete", f"manifest_sha256={sha256(manifest_bytes)}\n".encode(), 0o600)
        fsync_directory(backups)
        fsync_directory(temporary)
        os.replace(temporary, transaction)
        fsync_directory(state_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
elif action == "phase":
    if len(extra) != 1:
        raise RuntimeError("phase action requires one value")
    set_phase(extra[0])
elif action == "mark-rollback-failed":
    set_phase("rollback_failed", validate_manifest=False)
elif action == "read-phase":
    _, phase = load_transaction()
    print(phase)
elif action == "read-phase-secure":
    secure_lstat(transaction, directory=True, mode=0o700)
    phase = read_secure_file(transaction / "phase", 128).decode("ascii").removesuffix("\n")
    if phase not in ALLOWED_PHASES:
        raise RuntimeError("invalid transaction phase")
    print(phase)
elif action == "verify-installed":
    manifest, phase = load_transaction()
    if phase not in {"prepared", "installed", "disposable_passed", "guardian_started", "configured_passed"}:
        raise RuntimeError("transaction phase cannot verify installation")
    for artifact, source, destination in zip(manifest["artifacts"], sources, destinations, strict=True):
        verify_current_file(source, artifact["source_sha256"], artifact["source_mode"])
        verify_current_file(destination, artifact["installed_sha256"], artifact["installed_mode"])
elif action == "restore":
    manifest, phase = load_transaction()
    if phase == "committed":
        raise RuntimeError("committed transaction must not roll back")
    injected = os.environ.get("GB10_MEMORY_GUARDIAN_DEPLOY_FAIL_AT", "")
    for record in sorted(manifest["parents"], key=lambda item: len(Path(item["path"]).parts)):
        parent = Path(record["path"])
        if record["present"]:
            parent.mkdir(mode=record["mode"], parents=True, exist_ok=True)
            metadata = parent.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != uid:
                raise RuntimeError(f"unsafe restore parent: {parent}")
    for index, artifact in enumerate(manifest["artifacts"]):
        if f"rollback-artifact-{index}" in injected.split(","):
            raise RuntimeError(f"injected rollback failure at artifact {index}")
        destination = Path(artifact["destination"])
        prior = artifact["prior"]
        if prior["present"]:
            backup = transaction / prior["backup"]
            backup_data = read_secure_file(backup, 16 * 1024 * 1024)
            if sha256(backup_data) != prior["sha256"]:
                raise RuntimeError(f"corrupt rollback backup: {backup}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(destination, backup_data, prior["mode"])
        elif destination.exists() or destination.is_symlink():
            metadata = destination.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != uid:
                raise RuntimeError(f"unsafe rollback destination: {destination}")
            destination.unlink()
            fsync_directory(destination.parent)
    for record in sorted(manifest["parents"], key=lambda item: len(Path(item["path"]).parts), reverse=True):
        parent = Path(record["path"])
        if record["present"]:
            os.chmod(parent, record["mode"], follow_symlinks=False)
        elif parent.exists():
            parent.rmdir()
    for artifact in manifest["artifacts"]:
        destination = Path(artifact["destination"])
        prior = artifact["prior"]
        if prior["present"]:
            verify_current_file(destination, prior["sha256"], prior["mode"])
        elif destination.exists() or destination.is_symlink():
            raise RuntimeError(f"rollback did not restore absence: {destination}")
    for record in manifest["parents"]:
        parent = Path(record["path"])
        if record["present"]:
            metadata = secure_lstat(parent, directory=True, mode=record["mode"])
            if metadata.st_uid != uid:
                raise RuntimeError("rollback parent ownership changed")
        elif parent.exists() or parent.is_symlink():
            raise RuntimeError(f"rollback did not restore parent absence: {parent}")
elif action == "cleanup":
    _, phase = load_transaction()
    if phase not in {"committed", "installed", "rolling_back", "rollback_failed", "prepared", "disposable_passed", "guardian_started", "configured_passed"}:
        raise RuntimeError("transaction cannot be cleaned")
    tombstone = state_dir / f".{transaction.name}.cleanup"
    if tombstone.exists() or tombstone.is_symlink():
        raise RuntimeError("transaction cleanup tombstone already exists")
    os.replace(transaction, tombstone)
    fsync_directory(state_dir)
    shutil.rmtree(tombstone)
    fsync_directory(state_dir)
elif action == "cleanup-temporaries":
    secure_lstat(state_dir, directory=True, mode=0o700)
    for candidate in state_dir.glob(f".{transaction.name}.tmp.*"):
        secure_lstat(candidate, directory=True, mode=0o700)
        shutil.rmtree(candidate)
    tombstone = state_dir / f".{transaction.name}.cleanup"
    if tombstone.exists() or tombstone.is_symlink():
        secure_lstat(tombstone, directory=True, mode=0o700)
        shutil.rmtree(tombstone)
    fsync_directory(state_dir)
else:
    raise RuntimeError(f"unsupported transaction action: {action}")
PY
}

validate_reviewed_sources() {
  /usr/bin/python3 - "$source_config" "$source_text_unit" "$source_guardian_unit" \
    "$source_querit_unit" "$source_vllm_reranker_unit" <<'PY'
import os
import stat
import sys
import tomllib
from pathlib import Path

paths = list(map(Path, sys.argv[1:]))
for path in paths:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
        raise SystemExit(f"unsafe reviewed source: {path}")
config_path, text_path, guardian_path, *reranker_paths = paths
config = tomllib.loads(config_path.read_text())
if config != {"schema_version": 1, "target": {"label": "aeon-text", "registration_file": "text-cgroup.v1"}}:
    raise SystemExit("reviewed guardian config must be exactly aeon-text/text-cgroup.v1")
text = text_path.read_text()
for contract in (
    "Environment=GB10_CGROUP_REGISTRATION_PATH=%t/gb10-memory-guardian/text-cgroup.v1",
    "Restart=always", "--cgroup-parent app.slice",
):
    if contract not in text:
        raise SystemExit(f"reviewed text unit is missing contract: {contract}")
if "gb10-memory-guardian.service" in text.split("[Service]", 1)[0]:
    raise SystemExit("text unit must not auto-start guardian")
guardian = guardian_path.read_text()
for contract in (
    "Environment=GB10_MEMORY_GUARDIAN_EXPECTED_LABEL=aeon-text",
    "Environment=GB10_MEMORY_GUARDIAN_EXPECTED_REGISTRATION_FILE=text-cgroup.v1",
):
    if contract not in guardian:
        raise SystemExit(f"reviewed guardian unit is missing identity pin: {contract}")
for path in reranker_paths:
    unit = path.read_text()
    unit_section = unit.split("[Service]", 1)[0]
    for relationship in ("Requires=", "BindsTo=", "PartOf="):
        for line in unit_section.splitlines():
            if line.startswith(relationship) and "vllm-aeon-27b-dflash.service" in line:
                raise SystemExit(f"{path.name} owns text lifecycle: {line}")
    if "http://100.105.4.92:18010" in unit:
        raise SystemExit(f"{path.name} waits for text readiness")
PY
  [[ -x "$source_binary" ]] || {
    echo "build the current guardian release binary before deployment" >&2
    return 1
  }
}

rollback_bundle() {
  local rollback_status=0 durable_phase=""
  [[ "$rollback_running" == "0" ]] || return 1
  rollback_running=1
  durable_phase="$(transaction_python read-phase-secure 2>/dev/null)" || {
    transaction_python mark-rollback-failed 2>/dev/null || true
    rollback_running=0
    return 1
  }
  if [[ "$durable_phase" == "committed" ]]; then
    committed=1
    rollback_running=0
    return 0
  fi
  if [[ "$durable_phase" != "rolling_back" ]]; then
    if ! transaction_python phase rolling_back; then
      transaction_python mark-rollback-failed 2>/dev/null || {
        rollback_running=0
        return 1
      }
      transaction_python phase rolling_back || {
        rollback_running=0
        return 1
      }
    fi
  fi
  [[ "$(transaction_python read-phase-secure 2>/dev/null)" == "rolling_back" ]] || {
    rollback_running=0
    return 1
  }
  if ! run_systemctl disable --now "$guardian_unit" >/dev/null 2>&1; then
    echo "failed to stop and disable unverified guardian" >&2
    rollback_status=1
  fi
  if ! transaction_python restore; then
    echo "rollback artifact restoration failed" >&2
    rollback_status=1
  fi
  if ! run_systemctl daemon-reload; then
    echo "rollback daemon-reload failed" >&2
    rollback_status=1
  fi
  if [[ "$rollback_status" == "0" ]]; then
    transaction_python cleanup
    transaction_ready=0
    rollback_running=0
    return 0
  fi
  transaction_python mark-rollback-failed 2>/dev/null || true
  echo "rollback incomplete; private recovery state retained at $transaction_dir" >&2
  rollback_running=0
  return 1
}

# Installed before source preflight so signals and stale transactions always fail closed.
fail_closed_activation() {
  local status=$? durable_phase=""
  trap - EXIT INT TERM
  if [[ "$lock_acquired" != "1" ]]; then
    exit "$status"
  fi
  if [[ -e "$transaction_dir" && ! -L "$transaction_dir" ]]; then
    durable_phase="$(transaction_python read-phase-secure 2>/dev/null)" || true
    if [[ "$durable_phase" == "committed" ]]; then
      committed=1
    fi
  fi
  if [[ "$committed" == "1" || ( "$preserve_pending" == "1" && "$status" == "0" ) ]]; then
    exit "$status"
  fi
  if [[ "$transaction_ready" == "1" || -e "$transaction_dir" || -L "$transaction_dir" ]]; then
    rollback_bundle || status=1
  elif [[ "$status" == "0" ]]; then
    status=1
  fi
  exit "$status"
}

/usr/bin/install -d -m 0700 "$state_dir"
[[ -d "$state_dir" && ! -L "$state_dir" \
  && "$(stat -c '%u' "$state_dir")" == "$UID" \
  && "$(stat -c '%a' "$state_dir")" == "700" ]] || {
  echo "unsafe guardian deployment state directory: $state_dir" >&2
  exit 1
}
exec 9>"$state_dir/deploy.lock"
/usr/bin/flock -n 9 || {
  echo "another guardian deployment transaction is running" >&2
  exit 1
}
lock_acquired=1
trap fail_closed_activation EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
transaction_python cleanup-temporaries

if [[ -e "$transaction_dir" || -L "$transaction_dir" ]]; then
  transaction_ready=1
  pending_phase="$(transaction_python read-phase)"
  if [[ "$pending_phase" == "committed" ]]; then
    transaction_python cleanup
    transaction_ready=0
  elif [[ "$mode" == "activate" && "$pending_phase" == "installed" ]]; then
    :
  else
    echo "recovering stale guardian deployment transaction in phase $pending_phase" >&2
    rollback_bundle
    if [[ "$mode" == "activate" ]]; then
      echo "stale transaction recovered; run install again before activation" >&2
      exit 1
    fi
  fi
fi

validate_reviewed_sources

if [[ "$mode" == "install" ]]; then
  transaction_python create
  transaction_ready=1
  maybe_fail transaction-prepared
  run_systemctl disable --now "$guardian_unit" >/dev/null
  /usr/bin/install -d -m 0700 "$config_dir"
  /usr/bin/install -d -m 0755 "$bin_dir" "$unit_dir"
  for index in "${!sources[@]}"; do
    /usr/bin/install -m "${install_modes[$index]}" \
      "${sources[$index]}" "${destinations[$index]}"
    maybe_fail "${boundaries[$index]}"
  done
  transaction_python verify-installed
  run_systemctl daemon-reload
  maybe_fail install-daemon-reload
  transaction_python verify-installed
  verify_guardian_disabled_inactive
  transaction_python phase installed
  preserve_pending=1
  echo "memory guardian bundle installed with guardian disabled; private complete transaction is pending"
  exit 0
fi

transaction_python verify-installed
verify_guardian_disabled_inactive
text_registration="$runtime_dir/text-cgroup.v1"
if [[ -e "$runtime_dir/querit-cgroup.v1" ]]; then
  echo "refusing activation: stale querit-cgroup.v1 is still present" >&2
  exit 1
fi
[[ -f "$text_registration" && ! -L "$text_registration" ]] || {
  echo "refusing activation: $text_unit has not published text-cgroup.v1" >&2
  exit 1
}

run_canary disposable
maybe_fail activate-disposable
transaction_python verify-installed
transaction_python phase disposable_passed

run_systemctl start "$guardian_unit"
maybe_fail activate-start
transaction_python phase guardian_started
run_canary configured-target
maybe_fail activate-configured
transaction_python verify-installed
transaction_python phase configured_passed

run_systemctl enable "$guardian_unit"
maybe_fail activate-enable
transaction_python phase committed
maybe_fail activate-committed
committed=1
transaction_python cleanup
transaction_ready=0
trap - EXIT INT TERM
echo "memory guardian activation passed: aeon-text/text-cgroup.v1 strictly armed"
