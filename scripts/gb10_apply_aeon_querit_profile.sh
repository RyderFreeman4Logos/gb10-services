#!/usr/bin/env bash
# Apply the committed production AEON + Querit profile on GB10.
set -euo pipefail

export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1001/docker.sock}"

AEON_UNIT=vllm-aeon-27b-dflash.service
RERANK_UNIT=querit-4b-reranker.service
LEGACY_UNIT=vllm-qwen3-reranker-8b.service
GUARD_UNIT=llm-guard-proxy.service
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD_CONFIG_SOURCE="$ROOT_DIR/config/llm-guard-proxy/config.toml"
GUARD_CONFIG_PATH="${GB10_GUARD_CONFIG_PATH:-$HOME/.config/llm-guard-proxy/config.toml}"
AEON_CONTAINER=vllm-aeon-27b-dflash-n12
AEON_URL="${GB10_AEON_URL:-http://100.105.4.92:18010}"
RERANK_URL="${GB10_RERANK_URL:-http://100.105.4.92:18013}"
GUARD_SCORE_URL="${GB10_GUARD_SCORE_URL:-http://100.105.4.92:18003/v1/score}"
EXPECTED_AEON_KV_MIB=${GB10_EXPECTED_AEON_KV_MIB:-36864}
EXPECTED_AEON_MEMORY_GIB=${GB10_EXPECTED_AEON_MEMORY_GIB:-69}
MIN_AVAILABLE_GIB=${GB10_MIN_AVAILABLE_GIB:-4}
AEON_READY_ATTEMPTS=${GB10_AEON_READY_ATTEMPTS:-120}
RERANK_READY_ATTEMPTS=${GB10_RERANK_READY_ATTEMPTS:-180}
SYSTEMCTL_TIMEOUT_SECONDS=${GB10_SYSTEMCTL_TIMEOUT_SECONDS:-120}
SYSTEMCTL_START_TIMEOUT_SECONDS=${GB10_SYSTEMCTL_START_TIMEOUT_SECONDS:-1900}
DOCKER_TIMEOUT_SECONDS=${GB10_DOCKER_TIMEOUT_SECONDS:-15}
RESTART_AEON=0

for value in \
    "$EXPECTED_AEON_KV_MIB" \
    "$EXPECTED_AEON_MEMORY_GIB" \
    "$MIN_AVAILABLE_GIB" \
    "$AEON_READY_ATTEMPTS" \
    "$RERANK_READY_ATTEMPTS" \
    "$SYSTEMCTL_TIMEOUT_SECONDS" \
    "$SYSTEMCTL_START_TIMEOUT_SECONDS" \
    "$DOCKER_TIMEOUT_SECONDS"; do
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
        echo "profile and timeout values must be positive integers: $value" >&2
        exit 2
    fi
done

if [[ ${1:-} == "--restart-aeon" ]]; then
    RESTART_AEON=1
elif [[ $# -ne 0 ]]; then
    echo "usage: $0 [--restart-aeon]" >&2
    exit 2
fi

run_systemctl() {
    /usr/bin/timeout --signal=TERM --kill-after=5 "$SYSTEMCTL_TIMEOUT_SECONDS" \
        systemctl --user "$@"
}

run_systemctl_start() {
    /usr/bin/timeout --signal=TERM --kill-after=5 \
        "$SYSTEMCTL_START_TIMEOUT_SECONDS" systemctl --user "$@"
}

run_docker() {
    /usr/bin/timeout --signal=TERM --kill-after=5 "$DOCKER_TIMEOUT_SECONDS" \
        docker "$@"
}

unit_enabled_state() {
    local state rc=0
    state="$(run_systemctl is-enabled "$1" 2>/dev/null)" || rc=$?
    if [[ -z "$state" || ( $rc -ne 0 && $rc -ne 1 ) ]]; then
        echo "cannot determine enablement state for $1 rc=$rc" >&2
        return 45
    fi
    printf '%s' "$state"
}

unit_active_state() {
    local state rc=0
    state="$(run_systemctl is-active "$1" 2>/dev/null)" || rc=$?
    if [[ -z "$state" || ( $rc -ne 0 && $rc -ne 3 ) ]]; then
        echo "cannot determine active state for $1 rc=$rc" >&2
        return 46
    fi
    printf '%s' "$state"
}

restore_unit_enablement() {
    local unit="$1" state="$2"
    case "$state" in
        enabled) run_systemctl enable "$unit" ;;
        enabled-runtime) run_systemctl enable --runtime "$unit" ;;
        disabled) run_systemctl disable "$unit" ;;
        masked) run_systemctl mask "$unit" ;;
        masked-runtime) run_systemctl mask --runtime "$unit" ;;
        static|indirect|generated|transient) return 0 ;;
        *)
            echo "unsupported enablement state for $unit: $state" >&2
            return 47
            ;;
    esac
}

PREV_RERANK_ENABLED=""
PREV_RERANK_ACTIVE=""
PREV_LEGACY_ENABLED=""
PREV_LEGACY_ACTIVE=""
BACKUP_DIR=""
GUARD_CONFIG_EXISTED=0
MIGRATION_STARTED=0
DEPLOY_SUCCESS=0
CLEANUP_STARTED=0

rollback_runtime_state() {
    local rollback_failed=0
    if (( GUARD_CONFIG_EXISTED == 1 )); then
        /usr/bin/install -D -m 0644 \
            "$BACKUP_DIR/guard-config.toml" "$GUARD_CONFIG_PATH" || rollback_failed=1
    else
        rm -f -- "$GUARD_CONFIG_PATH" || rollback_failed=1
    fi
    run_systemctl stop "$RERANK_UNIT" || rollback_failed=1
    run_systemctl stop "$LEGACY_UNIT" || rollback_failed=1
    restore_unit_enablement "$RERANK_UNIT" "$PREV_RERANK_ENABLED" || rollback_failed=1
    restore_unit_enablement "$LEGACY_UNIT" "$PREV_LEGACY_ENABLED" || rollback_failed=1

    if [[ "$PREV_LEGACY_ACTIVE" == "active" ]]; then
        run_systemctl_start start "$LEGACY_UNIT" || rollback_failed=1
    elif [[ "$PREV_RERANK_ACTIVE" == "active" ]]; then
        run_systemctl_start start "$RERANK_UNIT" || rollback_failed=1
    fi
    return "$rollback_failed"
}

cleanup_failure() {
    local rc="$1" reason="$2" final_rc="$1"
    if (( final_rc == 0 )); then
        final_rc=49
    fi
    if (( CLEANUP_STARTED == 1 )); then
        return
    fi
    CLEANUP_STARTED=1
    trap - ERR INT TERM EXIT
    echo "DEPLOY_FAILED rc=$rc reason=$reason" >&2
    if (( MIGRATION_STARTED == 1 )); then
        if ! rollback_runtime_state; then
            echo "ROLLBACK_FAILED: inspect and restore the previous committed units" >&2
            final_rc=70
        fi
    fi
    run_systemctl status \
        "$AEON_UNIT" "$RERANK_UNIT" "$LEGACY_UNIT" "$GUARD_UNIT" \
        --no-pager -l || true
    exit "$final_rc"
}

cleanup_on_error() {
    local rc=$?
    cleanup_failure "$rc" ERR
}

cleanup_on_signal() {
    cleanup_failure "$2" "$1"
}

cleanup_on_exit() {
    local rc=$?
    if (( DEPLOY_SUCCESS == 0 && MIGRATION_STARTED == 1 )); then
        cleanup_failure "$rc" EXIT
    fi
}

trap cleanup_on_error ERR
trap 'cleanup_on_signal INT 130' INT
trap 'cleanup_on_signal TERM 143' TERM
trap cleanup_on_exit EXIT

PREV_RERANK_ENABLED="$(unit_enabled_state "$RERANK_UNIT")"
PREV_RERANK_ACTIVE="$(unit_active_state "$RERANK_UNIT")"
PREV_LEGACY_ENABLED="$(unit_enabled_state "$LEGACY_UNIT")"
PREV_LEGACY_ACTIVE="$(unit_active_state "$LEGACY_UNIT")"

read_mem_available_kib() {
    local key rest value
    while IFS=: read -r key rest; do
        if [[ "$key" == "MemAvailable" ]]; then
            read -r value _ <<< "$rest"
            printf '%s' "${value:-0}"
            return
        fi
    done < "${GB10_MEMINFO_PATH:-/proc/meminfo}"
    printf '0'
}

require_memory_headroom() {
    local available_kib
    available_kib="$(read_mem_available_kib)"
    if (( available_kib < MIN_AVAILABLE_GIB * 1024 * 1024 )); then
        echo "INSUFFICIENT_MEMORY available_kib=$available_kib minimum_gib=$MIN_AVAILABLE_GIB" >&2
        return 42
    fi
}

wait_for_url() {
    local url="$1" attempts="$2" label="$3" attempt
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    echo "${label}_READY_TIMEOUT attempts=$attempts" >&2
    return 43
}

verify_aeon_profile() {
    local command_json memory_limit expected_memory_limit
    if ! command_json="$(run_docker inspect "$AEON_CONTAINER" --format '{{json .Config.Cmd}}')"; then
        echo "AEON_INSPECT_FAILED field=command" >&2
        return 44
    fi
    if [[ "$command_json" != *"--kv-cache-memory-bytes\",\"${EXPECTED_AEON_KV_MIB}M"* ]]; then
        echo "AEON_KV_MISMATCH expected_mib=$EXPECTED_AEON_KV_MIB" >&2
        return 44
    fi
    if ! memory_limit="$(run_docker inspect "$AEON_CONTAINER" --format '{{.HostConfig.Memory}}')"; then
        echo "AEON_INSPECT_FAILED field=memory" >&2
        return 44
    fi
    expected_memory_limit=$((EXPECTED_AEON_MEMORY_GIB * 1024 * 1024 * 1024))
    if (( memory_limit != expected_memory_limit )); then
        echo "AEON_MEMORY_MISMATCH actual=$memory_limit expected=$expected_memory_limit" >&2
        return 44
    fi
}

run_systemctl is-active --quiet gb10-swap-guard.service
run_systemctl is-active --quiet "$GUARD_UNIT"
require_memory_headroom

if (( RESTART_AEON == 1 )); then
    echo "PHASE restart_aeon"
    run_systemctl_start restart "$AEON_UNIT"
else
    echo "PHASE verify_existing_aeon"
    run_systemctl is-active --quiet "$AEON_UNIT"
fi
wait_for_url "$AEON_URL/v1/models" "$AEON_READY_ATTEMPTS" AEON
verify_aeon_profile
require_memory_headroom

echo "PHASE switch_reranker"
BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gb10-querit-deploy.XXXXXX")"
if [[ -f "$GUARD_CONFIG_PATH" ]]; then
    GUARD_CONFIG_EXISTED=1
    /usr/bin/install -m 0644 "$GUARD_CONFIG_PATH" "$BACKUP_DIR/guard-config.toml"
fi
MIGRATION_STARTED=1
/usr/bin/install -D -m 0644 "$GUARD_CONFIG_SOURCE" "$GUARD_CONFIG_PATH"
run_systemctl disable --now "$LEGACY_UNIT"
run_systemctl reset-failed "$LEGACY_UNIT" || true
run_systemctl reset-failed "$RERANK_UNIT" || true
run_systemctl daemon-reload
run_systemctl_start start "$RERANK_UNIT"

wait_for_url "$RERANK_URL/v1/models" "$RERANK_READY_ATTEMPTS" RERANK
run_systemctl is-active --quiet "$RERANK_UNIT"
require_memory_headroom
echo "RERANK_READY"

RAW="$(curl -fsS --max-time 30 -H 'content-type: application/json' \
    -d '{"model":"qwen3-reranker-8b","query":"capital of France","documents":["Paris is the capital of France.","Bananas are yellow."],"top_n":2}' \
    "$RERANK_URL/v1/rerank")"
python3 -c 'import json,math,sys; d=json.loads(sys.argv[1]); r=d["results"]; assert r[0]["index"] == 0; assert all(math.isfinite(float(x["relevance_score"])) for x in r); print("RAW_RERANK_OK", r)' "$RAW"

GUARD="$(curl -fsS --max-time 30 -H 'content-type: application/json' \
    -d '{"model":"qwen3-reranker-8b","text_1":"capital of France","text_2":"Paris is the capital of France."}' \
    "$GUARD_SCORE_URL")"
python3 -c 'import json,math,sys; d=json.loads(sys.argv[1]); s=float(d["data"][0]["score"]); assert math.isfinite(s); print("GUARD_SCORE_OK", s)' "$GUARD"

run_systemctl enable "$RERANK_UNIT"
run_systemctl is-enabled --quiet "$RERANK_UNIT"
if run_systemctl is-enabled --quiet "$LEGACY_UNIT"; then
    echo "legacy reranker unexpectedly enabled" >&2
    exit 48
fi
run_systemctl is-active --quiet \
    vllm-embedding.service "$AEON_UNIT" "$RERANK_UNIT" "$GUARD_UNIT"

DEPLOY_SUCCESS=1
MIGRATION_STARTED=0
trap - ERR INT TERM EXIT
echo "DEPLOY_SUCCESS"
