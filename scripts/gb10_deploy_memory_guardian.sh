#!/usr/bin/env bash
# Source-first, fail-closed deployment for the text-only memory guardian.
set -Eeuo pipefail
umask 077

usage() {
  cat >&2 <<'EOF'
usage: GB10_BENCHMARK_EXCLUDED=YES gb10_deploy_memory_guardian.sh install|activate

Build target/release/gb10-memory-guardian from this reviewed checkout first.
The install phase leaves the guardian disabled so an operator can restart text
in an approved maintenance window. The activate phase never restarts or kills a
production model and refuses activation unless text-cgroup.v1 is already armed.
EOF
  exit 2
}

mode="${1:-}"
[[ ( "$mode" == "install" || "$mode" == "activate" ) && $# -eq 1 ]] || usage
[[ "${GB10_BENCHMARK_EXCLUDED:-}" == "YES" ]] || {
  echo "refusing activation: exclude benchmark load first" >&2
  exit 2
}

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
guardian_unit="gb10-memory-guardian.service"
text_unit="vllm-aeon-27b-dflash.service"
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${UID}}/gb10-memory-guardian"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/gb10-memory-guardian"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
bin_dir="$HOME/.local/bin"
systemctl_bin="${GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
command_timeout_seconds="${GB10_MEMORY_GUARDIAN_DEPLOY_TIMEOUT_SECONDS:-15}"
activation_verified=0

[[ "$command_timeout_seconds" =~ ^[1-9][0-9]*$ ]] || {
  echo "deployment timeout must be a positive integer" >&2
  exit 2
}

run_systemctl() {
  /usr/bin/timeout --signal=TERM --kill-after=2 "$command_timeout_seconds" \
    "$systemctl_bin" --user "$@"
}

# This EXIT trap also covers explicit exit paths. Until the final strict canary
# succeeds, any failure leaves the automatic actor stopped and disabled.
fail_closed_activation() {
  local status=$?
  trap - EXIT
  if [[ "$activation_verified" != "1" ]]; then
    if ! run_systemctl disable --now "$guardian_unit" >/dev/null 2>&1; then
      echo "failed to stop and disable unverified guardian" >&2
      status=1
    elif [[ "$status" == "0" ]]; then
      status=1
    fi
  fi
  exit "$status"
}
trap fail_closed_activation EXIT

source_config="$root/config/gb10-memory-guardian/config.toml"
source_binary="$root/target/release/gb10-memory-guardian"
source_helper="$root/scripts/gb10_enforce_docker_cgroup_limits.sh"
source_canary="$root/scripts/gb10_memory_guardian_canary.sh"
source_guardian_unit="$root/systemd/gb10-memory-guardian.service"
source_canary_unit="$root/systemd/gb10-memory-guardian-canary.service"
source_text_unit="$root/systemd/vllm-aeon-27b-dflash.service"
source_querit_unit="$root/systemd/querit-4b-reranker.service"
source_vllm_reranker_unit="$root/systemd/vllm-qwen3-reranker-8b.service"

for source in \
  "$source_config" \
  "$source_binary" \
  "$source_helper" \
  "$source_canary" \
  "$source_guardian_unit" \
  "$source_canary_unit" \
  "$source_text_unit" \
  "$source_querit_unit" \
  "$source_vllm_reranker_unit"; do
  [[ -f "$source" && ! -L "$source" ]] || {
    echo "missing or linked reviewed deployment source: $source" >&2
    exit 1
  }
done
[[ -x "$source_binary" ]] || {
  echo "build the current guardian release binary before deployment: $source_binary" >&2
  exit 1
}

/usr/bin/python3 - "$source_config" "$source_text_unit" "$source_guardian_unit" \
  "$source_querit_unit" "$source_vllm_reranker_unit" <<'PY'
import sys
import tomllib
from pathlib import Path

config_path, text_path, guardian_path, *reranker_paths = map(Path, sys.argv[1:])
config = tomllib.loads(config_path.read_text())
if config != {
    "schema_version": 1,
    "target": {"label": "aeon-text", "registration_file": "text-cgroup.v1"},
}:
    raise SystemExit("reviewed guardian config must be exactly aeon-text/text-cgroup.v1")

text = text_path.read_text()
required_text = (
    "Environment=GB10_CGROUP_REGISTRATION_PATH=%t/gb10-memory-guardian/text-cgroup.v1",
    "Restart=on-failure",
    "--cgroup-parent app.slice",
)
for contract in required_text:
    if contract not in text:
        raise SystemExit(f"reviewed text unit is missing contract: {contract}")
unit_section = text.split("[Service]", 1)[0]
if "gb10-memory-guardian.service" in unit_section:
    raise SystemExit("text unit must not auto-start the guardian before activation")

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
                raise SystemExit(f"{path.name} still owns the text lifecycle: {line}")
    if "http://100.105.4.92:18010" in unit:
        raise SystemExit(f"{path.name} still waits for text readiness")
PY

# Stop the possibly stale automatic actor before replacing any of its source
# contract. No model unit is stopped, started, restarted, or killed here.
run_systemctl disable --now "$guardian_unit" >/dev/null

/usr/bin/install -d -m 0700 "$config_dir"
/usr/bin/install -d -m 0755 "$bin_dir" "$unit_dir"
/usr/bin/install -m 0600 "$root/config/gb10-memory-guardian/config.toml" \
  "$config_dir/config.toml"
/usr/bin/install -m 0755 "$root/target/release/gb10-memory-guardian" \
  "$bin_dir/gb10-memory-guardian"
/usr/bin/install -m 0755 "$root/scripts/gb10_enforce_docker_cgroup_limits.sh" \
  "$bin_dir/gb10_enforce_docker_cgroup_limits.sh"
/usr/bin/install -m 0755 "$root/scripts/gb10_memory_guardian_canary.sh" \
  "$bin_dir/gb10_memory_guardian_canary.sh"
/usr/bin/install -m 0644 "$root/systemd/gb10-memory-guardian.service" \
  "$unit_dir/gb10-memory-guardian.service"
/usr/bin/install -m 0644 "$root/systemd/gb10-memory-guardian-canary.service" \
  "$unit_dir/gb10-memory-guardian-canary.service"
/usr/bin/install -m 0644 "$root/systemd/vllm-aeon-27b-dflash.service" \
  "$unit_dir/vllm-aeon-27b-dflash.service"
/usr/bin/install -m 0644 "$root/systemd/querit-4b-reranker.service" \
  "$unit_dir/querit-4b-reranker.service"
/usr/bin/install -m 0644 "$root/systemd/vllm-qwen3-reranker-8b.service" \
  "$unit_dir/vllm-qwen3-reranker-8b.service"

run_systemctl daemon-reload

if [[ "$mode" == "install" ]]; then
  activation_verified=1
  trap - EXIT
  echo "memory guardian bundle installed with guardian disabled; publish text-cgroup.v1 before activation"
  exit 0
fi

text_registration="$runtime_dir/text-cgroup.v1"
if [[ -e "$runtime_dir/querit-cgroup.v1" ]]; then
  echo "refusing activation: stale querit-cgroup.v1 is still present" >&2
  exit 1
fi
[[ -f "$text_registration" && ! -L "$text_registration" ]] || {
  echo "refusing activation: $text_unit has not published text-cgroup.v1" >&2
  exit 1
}

# The disposable phase kills only its transient sleep cgroup. The configured
# phase below is read-only and never invokes --kill-configured-target.
GB10_BENCHMARK_EXCLUDED=YES \
GB10_MEMORY_GUARDIAN_BIN="$bin_dir/gb10-memory-guardian" \
  "$bin_dir/gb10_memory_guardian_canary.sh" disposable

activated_at="$(date --iso-8601=seconds)"
run_systemctl enable --now "$guardian_unit"
GB10_BENCHMARK_EXCLUDED=YES \
GB10_MEMORY_GUARDIAN_BIN="$bin_dir/gb10-memory-guardian" \
GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT="$text_unit" \
GB10_MEMORY_GUARDIAN_JOURNAL_SINCE="$activated_at" \
  "$bin_dir/gb10_memory_guardian_canary.sh" configured-target

activation_verified=1
trap - EXIT
echo "memory guardian activation passed: aeon-text/text-cgroup.v1 strictly armed"
