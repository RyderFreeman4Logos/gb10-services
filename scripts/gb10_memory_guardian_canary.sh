#!/usr/bin/env bash
# Explicit two-stage canary for the direct cgroup.kill path.
set -Eeuo pipefail

usage() {
  cat >&2 <<'EOF'
usage:
  GB10_BENCHMARK_EXCLUDED=YES gb10_memory_guardian_canary.sh disposable
  GB10_BENCHMARK_EXCLUDED=YES \
    GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT=<text-unit.service> \
    gb10_memory_guardian_canary.sh configured-target I_UNDERSTAND_CONFIGURED_TARGET_WILL_BE_KILLED
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
protected_units=(
  vllm-embedding.service
  querit-4b-reranker.service
  vllm-qwen3-reranker-8b.service
  llm-guard-proxy.service
  gb10-memory-guardian.service
)

[[ -x "$guardian_bin" ]] || {
  echo "guardian binary is not executable: $guardian_bin" >&2
  exit 1
}

declare -A protected_before_state
declare -A protected_before_pid
declare -A protected_before_restarts
snapshot_protected() {
  local unit
  for unit in "${protected_units[@]}"; do
    protected_before_state["$unit"]="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    protected_before_pid["$unit"]="$(systemctl --user show -p MainPID --value "$unit" 2>/dev/null || true)"
    protected_before_restarts["$unit"]="$(systemctl --user show -p NRestarts --value "$unit" 2>/dev/null || true)"
  done
}

verify_protected_unchanged() {
  local unit current_state current_pid current_restarts
  for unit in "${protected_units[@]}"; do
    current_state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    current_pid="$(systemctl --user show -p MainPID --value "$unit" 2>/dev/null || true)"
    current_restarts="$(systemctl --user show -p NRestarts --value "$unit" 2>/dev/null || true)"
    if [[ "$current_state" != "${protected_before_state[$unit]}" ]]; then
      echo "protected unit changed state: $unit ${protected_before_state[$unit]} -> $current_state" >&2
      exit 1
    fi
    if [[ "$current_pid" != "${protected_before_pid[$unit]}" ]]; then
      echo "protected unit changed MainPID: $unit ${protected_before_pid[$unit]} -> $current_pid" >&2
      exit 1
    fi
    if [[ "$current_restarts" != "${protected_before_restarts[$unit]}" ]]; then
      echo "protected unit changed NRestarts: $unit ${protected_before_restarts[$unit]} -> $current_restarts" >&2
      exit 1
    fi
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
    exit 1
  }
  local stamp_sha stamp_epoch current_sha now_epoch
  stamp_sha="$(sed -n 's/^binary_sha256=//p' "$stamp")"
  stamp_epoch="$(sed -n 's/^passed_epoch=//p' "$stamp")"
  current_sha="$(sha256sum "$guardian_bin" | awk '{print $1}')"
  now_epoch="$(date +%s)"
  if [[ ! "$stamp_epoch" =~ ^[0-9]+$ || "$stamp_sha" != "$current_sha" || $((now_epoch - stamp_epoch)) -gt 3600 ]]; then
    echo "disposable canary attestation is stale or for another binary" >&2
    exit 1
  fi
}

run_disposable() {
  [[ $# -eq 0 ]] || usage
  # The real text service is protected during the disposable-only phase.
  protected_units+=(vllm-aeon-27b-dflash.service)
  snapshot_protected
  rm -f -- "$stamp"
  systemctl --user stop "$canary_unit" >/dev/null 2>&1 || true
  systemctl --user reset-failed "$canary_unit" >/dev/null 2>&1 || true
  systemctl --user revert "$canary_unit" >/dev/null 2>&1 || true
  systemd-run --user \
    --unit="$canary_unit" \
    --slice=app.slice \
    --property=Type=simple \
    --property=Restart=no \
    /usr/bin/sleep infinity >/dev/null
  cleanup_canary() {
    systemctl --user stop "$canary_unit" >/dev/null 2>&1 || true
    systemctl --user reset-failed "$canary_unit" >/dev/null 2>&1 || true
    systemctl --user revert "$canary_unit" >/dev/null 2>&1 || true
  }
  trap cleanup_canary EXIT

  for _ in $(seq 1 20); do
    [[ "$(systemctl --user is-active "$canary_unit" 2>/dev/null || true)" == "active" ]] && break
    sleep 0.25
  done
  [[ "$(systemctl --user is-active "$canary_unit" 2>/dev/null || true)" == "active" ]] || {
    echo "disposable canary did not become active" >&2
    exit 1
  }

  systemctl --user reset-failed "$driver_unit" >/dev/null 2>&1 || true
  systemctl --user start "$driver_unit"
  [[ "$(systemctl --user show -p Result --value "$driver_unit" 2>/dev/null || true)" == "success" ]] || {
    echo "sandboxed disposable guardian driver did not report success" >&2
    exit 1
  }
  [[ "$(systemctl --user is-active "$canary_unit" 2>/dev/null || true)" != "active" ]] || {
    echo "direct canary kill did not empty the disposable cgroup" >&2
    exit 1
  }
  verify_protected_unchanged
  write_attestation
  trap - EXIT
  cleanup_canary
  echo "disposable direct-kill canary passed; protected services were unchanged"
}

run_configured_target() {
  [[ $# -eq 1 && "$1" == "I_UNDERSTAND_CONFIGURED_TARGET_WILL_BE_KILLED" ]] || usage
  local target_unit="${GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT:-}"
  [[ "$target_unit" =~ ^[A-Za-z0-9@_.:-]+\.service$ ]] || {
    echo "set GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT to the explicit text unit" >&2
    exit 2
  }
  local protected
  for protected in "${protected_units[@]}"; do
    [[ "$target_unit" != "$protected" ]] || {
      echo "refusing to target protected unit: $target_unit" >&2
      exit 2
    }
  done
  verify_attestation

  local config_path registration_file registration_path unit_environment target_restart
  config_path="${GB10_MEMORY_GUARDIAN_CONFIG_PATH:-${XDG_CONFIG_HOME:-$HOME/.config}/gb10-memory-guardian/config.toml}"
  registration_file="$(
    /usr/bin/python3 - "$config_path" <<'PY'
import os
import re
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
name = target["registration_file"]
if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None:
    raise SystemExit("unsafe registration_file")
print(name)
PY
  )"
  registration_path="$runtime_dir/$registration_file"
  unit_environment="$(systemctl --user show -p Environment --value "$target_unit")"
  [[ " $unit_environment " == *" GB10_CGROUP_REGISTRATION_PATH=$registration_path "* ]] || {
    echo "target unit does not publish the configured registration: $target_unit" >&2
    exit 2
  }
  target_restart="$(systemctl --user show -p Restart --value "$target_unit")"
  [[ "$target_restart" == "on-failure" ]] || {
    echo "target unit must use Restart=on-failure: $target_unit" >&2
    exit 2
  }
  [[ "$(systemctl --user is-active "$target_unit" 2>/dev/null || true)" == "active" ]] || {
    echo "configured target unit is not active: $target_unit" >&2
    exit 1
  }

  local target_pid_before target_restarts_before target_pid_after target_restarts_after
  target_pid_before="$(systemctl --user show -p MainPID --value "$target_unit")"
  target_restarts_before="$(systemctl --user show -p NRestarts --value "$target_unit")"
  snapshot_protected
  "$guardian_bin" --kill-configured-target

  for _ in $(seq 1 180); do
    target_pid_after="$(systemctl --user show -p MainPID --value "$target_unit" 2>/dev/null || true)"
    target_restarts_after="$(systemctl --user show -p NRestarts --value "$target_unit" 2>/dev/null || true)"
    if [[ "$(systemctl --user is-active "$target_unit" 2>/dev/null || true)" == "active" \
      && "$target_pid_after" =~ ^[1-9][0-9]*$ \
      && "$target_pid_after" != "$target_pid_before" \
      && "$target_restarts_after" =~ ^[0-9]+$ \
      && "$target_restarts_before" =~ ^[0-9]+$ \
      && "$target_restarts_after" -gt "$target_restarts_before" ]]; then
      break
    fi
    sleep 1
  done
  [[ "$(systemctl --user is-active "$target_unit" 2>/dev/null || true)" == "active" \
    && "$target_pid_after" != "$target_pid_before" \
    && "$target_restarts_after" -gt "$target_restarts_before" ]] || {
    echo "configured target did not converge through Restart=on-failure: $target_unit" >&2
    exit 1
  }
  verify_protected_unchanged
  rm -f -- "$stamp"
  echo "configured target direct-kill canary passed; protected embedding and rerankers were unchanged"
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
