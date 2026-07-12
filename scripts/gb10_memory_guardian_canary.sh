#!/usr/bin/env bash
# Rigid two-stage canary for the direct cgroup.kill path. This script has no
# target argument: it can address only the compiled disposable canary name or
# the strict, registered Querit Docker scope.
set -Eeuo pipefail

usage() {
  cat >&2 <<'EOF'
usage:
  GB10_BENCHMARK_EXCLUDED=YES ./gb10_memory_guardian_canary.sh disposable
  GB10_BENCHMARK_EXCLUDED=YES ./gb10_memory_guardian_canary.sh querit I_UNDERSTAND_QUERIT_WILL_BE_SHED
EOF
  exit 2
}

[[ "${GB10_BENCHMARK_EXCLUDED:-}" == "YES" ]] || {
  echo "refusing canary: set GB10_BENCHMARK_EXCLUDED=YES only after stopping/excluding every benchmark" >&2
  exit 2
}

mode="${1:-}"
guardian_bin="${GB10_MEMORY_GUARDIAN_BIN:-$HOME/.local/bin/gb10-memory-guardian}"
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${UID}}/gb10-memory-guardian"
stamp="$runtime_dir/disposable-canary.passed"
canary_unit="gb10-memory-guardian-disposable-canary.service"
driver_unit="gb10-memory-guardian-canary.service"
protected_units=(
  vllm-aeon-27b-dflash.service
  vllm-embedding.service
  llm-guard-proxy.service
  gb10-memory-guardian.service
)

[[ -x "$guardian_bin" ]] || {
  echo "guardian binary is not executable: $guardian_bin" >&2
  exit 1
}

declare -A protected_before_state
declare -A protected_before_pid
snapshot_protected() {
  local unit
  for unit in "${protected_units[@]}"; do
    protected_before_state["$unit"]="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    protected_before_pid["$unit"]="$(systemctl --user show -p MainPID --value "$unit" 2>/dev/null || true)"
  done
}

verify_protected_unchanged() {
  local unit current_state current_pid
  for unit in "${protected_units[@]}"; do
    current_state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    current_pid="$(systemctl --user show -p MainPID --value "$unit" 2>/dev/null || true)"
    if [[ "$current_state" != "${protected_before_state[$unit]}" ]]; then
      echo "protected unit changed state: $unit ${protected_before_state[$unit]} -> $current_state" >&2
      exit 1
    fi
    if [[ "$current_pid" != "${protected_before_pid[$unit]}" ]]; then
      echo "protected unit changed MainPID: $unit ${protected_before_pid[$unit]} -> $current_pid" >&2
      exit 1
    fi
  done
}

run_disposable() {
  [[ $# -eq 0 ]] || usage
  snapshot_protected
  rm -f -- "$stamp"
  systemctl --user stop "$canary_unit" >/dev/null 2>&1 || true
  systemd-run --user \
    --unit="$canary_unit" \
    --slice=app.slice \
    --property=Type=simple \
    --property=Restart=no \
    /usr/bin/sleep infinity >/dev/null
  cleanup_canary() {
    systemctl --user stop "$canary_unit" >/dev/null 2>&1 || true
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
  /usr/bin/install -d -m 0700 "$runtime_dir"
  {
    printf 'binary_sha256='
    sha256sum "$guardian_bin" | awk '{print $1}'
    printf 'passed_epoch=%s\n' "$(date +%s)"
  } >"$stamp.tmp"
  chmod 0600 "$stamp.tmp"
  mv -f -- "$stamp.tmp" "$stamp"
  trap - EXIT
  cleanup_canary
  echo "disposable direct-kill canary passed; protected services were unchanged"
}

run_querit() {
  [[ $# -eq 1 && "$1" == "I_UNDERSTAND_QUERIT_WILL_BE_SHED" ]] || usage
  [[ -f "$stamp" && ! -L "$stamp" ]] || {
    echo "run the disposable canary first" >&2
    exit 1
  }
  stamp_sha="$(sed -n 's/^binary_sha256=//p' "$stamp")"
  stamp_epoch="$(sed -n 's/^passed_epoch=//p' "$stamp")"
  current_sha="$(sha256sum "$guardian_bin" | awk '{print $1}')"
  now_epoch="$(date +%s)"
  if [[ ! "$stamp_epoch" =~ ^[0-9]+$ || "$stamp_sha" != "$current_sha" || $((now_epoch - stamp_epoch)) -gt 3600 ]]; then
    echo "disposable canary attestation is stale or for another binary" >&2
    exit 1
  fi
  [[ "$(systemctl --user is-active querit-4b-reranker.service 2>/dev/null || true)" == "active" ]] || {
    echo "Querit is not active; nothing to shed" >&2
    exit 1
  }

  snapshot_protected
  "$guardian_bin" --shed-registered-querit
  for _ in $(seq 1 40); do
    [[ "$(systemctl --user is-active querit-4b-reranker.service 2>/dev/null || true)" != "active" ]] && break
    sleep 0.25
  done
  [[ "$(systemctl --user is-active querit-4b-reranker.service 2>/dev/null || true)" != "active" ]] || {
    echo "Querit remained active after direct kill" >&2
    exit 1
  }
  verify_protected_unchanged
  rm -f -- "$stamp"
  echo "controlled Querit direct shed passed; Querit was not restarted"
}

case "$mode" in
  disposable)
    shift
    run_disposable "$@"
    ;;
  querit)
    shift
    run_querit "$@"
    ;;
  *)
    usage
    ;;
esac
