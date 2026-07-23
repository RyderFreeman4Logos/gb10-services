#!/usr/bin/env bash
# Audited manual lifecycle control for GB10 model services.
#
# Lifecycle changes are explicitly serialized, require a content-free actor and
# reason, and are rejected while an investigation marker is active.  The audit
# log records the request before systemctl is called so failed calls retain
# attribution too.
set -euo pipefail
umask 077

readonly SYSTEMCTL_BIN="${GB10_LIFECYCLE_SYSTEMCTL:-/usr/bin/systemctl}"
readonly FLOCK_BIN="/usr/bin/flock"

usage() {
    cat >&2 <<'USAGE'
Usage:
  gb10_lifecycle.sh investigation-begin --actor ACTOR --reason REASON
  gb10_lifecycle.sh investigation-end --actor ACTOR --reason REASON
  gb10_lifecycle.sh stop|start --unit UNIT --actor ACTOR --reason REASON

Only tracked GB10 model units are accepted. `restart` is intentionally rejected:
perform separately audited stop and start operations instead.
USAGE
}

fail() {
    printf 'gb10_lifecycle: %s\n' "$*" >&2
    exit 1
}

readonly STATE_DIR="/home/obj/.local/state/gb10-lifecycle"
readonly AUDIT_LOG="${STATE_DIR}/lifecycle-audit.log"
readonly INVESTIGATION_LOCK="${STATE_DIR}/investigation.lock"
readonly MUTEX_LOCK="${STATE_DIR}/lifecycle.mutex"

ensure_state() {
    if [[ -L "$STATE_DIR" || ( -e "$STATE_DIR" && ! -d "$STATE_DIR" ) ]]; then
        fail "state directory is not a real directory"
    fi
    mkdir -p -m 0700 "$STATE_DIR"
    chmod 0700 "$STATE_DIR"

    if [[ -L "$AUDIT_LOG" || ( -e "$AUDIT_LOG" && ! -f "$AUDIT_LOG" ) ]]; then
        fail "audit log is not a regular file"
    fi
    touch "$AUDIT_LOG"
    chmod 0600 "$AUDIT_LOG"

    if [[ -L "$MUTEX_LOCK" || ( -e "$MUTEX_LOCK" && ! -f "$MUTEX_LOCK" ) ]]; then
        fail "lifecycle mutex is not a regular file"
    fi
    touch "$MUTEX_LOCK"
    chmod 0600 "$MUTEX_LOCK"
}

monotonic_seconds() {
    local monotonic _rest
    if read -r monotonic _rest < /proc/uptime; then
        printf '%s' "$monotonic"
    else
        printf 'unavailable'
    fi
}

audit() {
    local event="$1"
    shift
    printf '%s monotonic_seconds=%s uid=%s pid=%s event=%s %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$(monotonic_seconds)" \
        "$EUID" \
        "$$" \
        "$event" \
        "$*" >> "$AUDIT_LOG"
}

require_token() {
    local name="$1" value="$2"
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:@/+=,-]{0,159}$ ]] \
        || fail "$name must be a non-empty content-free token"
}

require_unit() {
    case "$1" in
        vllm-aeon-27b-dflash.service|vllm-embedding.service|vllm-querit-4b-reranker.service|vllm-qwen3-reranker-8b.service)
            ;;
        *)
            fail "unit is not an approved GB10 model service: $1"
            ;;
    esac
}

[[ $# -gt 0 ]] || {
    usage
    exit 1
}

ACTION="$1"
shift
ACTOR=""
REASON=""
UNIT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --actor)
            [[ $# -ge 2 ]] || fail "--actor requires a value"
            ACTOR="$2"
            shift 2
            ;;
        --reason)
            [[ $# -ge 2 ]] || fail "--reason requires a value"
            REASON="$2"
            shift 2
            ;;
        --unit)
            [[ $# -ge 2 ]] || fail "--unit requires a value"
            UNIT="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

case "$ACTION" in
    investigation-begin|investigation-end|stop|start|restart)
        ;;
    *)
        usage
        fail "unknown action: $ACTION"
        ;;
esac

require_token actor "$ACTOR"
require_token reason "$REASON"
case "$ACTION" in
    stop|start|restart)
        [[ -n "$UNIT" ]] || fail "$ACTION requires --unit"
        require_unit "$UNIT"
        ;;
    investigation-begin|investigation-end)
        [[ -z "$UNIT" ]] || fail "$ACTION does not accept --unit"
        ;;
esac

ensure_state
exec 9>"$MUTEX_LOCK"
if ! "$FLOCK_BIN" -n 9; then
    audit lock actor="$ACTOR" reason="$REASON" outcome=busy
    fail "another lifecycle operation holds the mutex"
fi

case "$ACTION" in
    investigation-begin)
        if [[ -e "$INVESTIGATION_LOCK" || -L "$INVESTIGATION_LOCK" ]]; then
            audit investigation-begin actor="$ACTOR" reason="$REASON" outcome=already-active
            fail "an investigation lock is already active"
        fi
        audit investigation-begin actor="$ACTOR" reason="$REASON" outcome=requested
        printf 'actor=%s\nreason=%s\ncreated_at=%s\n' \
            "$ACTOR" "$REASON" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$INVESTIGATION_LOCK"
        chmod 0600 "$INVESTIGATION_LOCK"
        audit investigation-begin actor="$ACTOR" reason="$REASON" outcome=created
        ;;
    investigation-end)
        if [[ -L "$INVESTIGATION_LOCK" || ! -f "$INVESTIGATION_LOCK" ]]; then
            audit investigation-end actor="$ACTOR" reason="$REASON" outcome=missing
            fail "there is no active investigation lock"
        fi
        audit investigation-end actor="$ACTOR" reason="$REASON" outcome=requested
        if rm -f -- "$INVESTIGATION_LOCK"; then
            :
        else
            status=$?
            audit investigation-end actor="$ACTOR" reason="$REASON" outcome=failure exit_status="$status"
            exit "$status"
        fi
        ;;
    restart)
        audit request action=restart unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=rejected
        fail "restart is forbidden; use separate audited stop and start operations"
        ;;
    stop)
        if [[ -e "$INVESTIGATION_LOCK" || -L "$INVESTIGATION_LOCK" ]]; then
            audit request action=stop unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=blocked investigation=active
            fail "active investigation lock blocks lifecycle operation"
        fi
        audit request action=stop unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=accepted
        if "$SYSTEMCTL_BIN" --user stop "$UNIT"; then
            audit result action=stop unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=success
        else
            status=$?
            audit result action=stop unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=failure exit_status="$status"
            exit "$status"
        fi
        ;;
    start)
        if [[ -e "$INVESTIGATION_LOCK" || -L "$INVESTIGATION_LOCK" ]]; then
            audit request action=start unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=blocked investigation=active
            fail "active investigation lock blocks lifecycle operation"
        fi
        audit request action=start unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=accepted
        if "$SYSTEMCTL_BIN" --user start --no-block "$UNIT"; then
            audit result action=start unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=submitted
        else
            status=$?
            audit result action=start unit="$UNIT" actor="$ACTOR" reason="$REASON" outcome=failure exit_status="$status"
            exit "$status"
        fi
        ;;
esac
