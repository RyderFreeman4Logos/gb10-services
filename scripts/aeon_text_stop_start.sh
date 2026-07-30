#!/usr/bin/env bash
# Cleanly recycle only the AEON text unit for llm-guard-proxy local recovery.
# The proxy performs the bounded chat-completion readiness probe after this
# wrapper observes a successful start submission. Type=simple text units can
# remain activating for the full cold-start window while ExecStartPost readiness
# runs, so this wrapper treats a successfully submitted activating/reloading
# state as restart-command success and never waits for HTTP readiness itself.
# This wrapper never touches embedding or either reranker unit.
set -euo pipefail

readonly SYSTEMCTL="/usr/bin/systemctl"
readonly SLEEP="/usr/bin/sleep"
readonly MKDIR="/usr/bin/mkdir"
readonly MKTEMP="/usr/bin/mktemp"
readonly MV="/usr/bin/mv"
readonly ACTIVE_WAIT_SECS=30
readonly LIFECYCLE="${GB10_LIFECYCLE_BIN:-/home/obj/.local/bin/gb10_lifecycle.sh}"
readonly LIFECYCLE_ACTOR="llm-guard-proxy.local-recovery"
readonly LIFECYCLE_REASON="automatic-local-recovery"
readonly SELECTED_TEXT_UNIT_FILE="${GB10_SELECTED_TEXT_UNIT_FILE:-/home/obj/.local/state/gb10-lifecycle/selected-text-unit}"
readonly -a TEXT_UNITS=(
    vllm-aeon-27b-dflash.service
    vllm-aeon-27b-dflash-hikv.service
)

validate_text_unit() {
    case "$1" in
        vllm-aeon-27b-dflash.service|vllm-aeon-27b-dflash-hikv.service)
            ;;
        *)
            printf 'unsupported AEON text unit: %s\n' "$1" >&2
            return 1
            ;;
    esac
}

read_persisted_text_unit() {
    local candidate=""
    if [[ -r "$SELECTED_TEXT_UNIT_FILE" ]]; then
        candidate="$(<"$SELECTED_TEXT_UNIT_FILE")"
        candidate="${candidate//$'\r'/}"
        candidate="${candidate//$'\n'/}"
        if [[ -n "$candidate" ]] && validate_text_unit "$candidate" 2>/dev/null; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi
    return 1
}

persist_selected_text_unit() {
    local unit="$1"
    local parent name tmp
    parent="$(dirname -- "$SELECTED_TEXT_UNIT_FILE")"
    name="${SELECTED_TEXT_UNIT_FILE##*/}"
    "$MKDIR" -p -- "$parent"
    tmp="$("$MKTEMP" -- "${parent}/.${name}.XXXXXXXXXX")"
    if ! printf '%s\n' "$unit" >"$tmp"; then
        rm -f -- "$tmp"
        return 1
    fi
    "$MV" -f -- "$tmp" "$SELECTED_TEXT_UNIT_FILE"
}

select_text_unit() {
    local candidate state persisted
    local -a matching=()

    for candidate in "${TEXT_UNITS[@]}"; do
        state="$("$SYSTEMCTL" --user show --property=ActiveState --value "$candidate" 2>/dev/null || true)"
        case "$state" in
            active|activating|reloading)
                matching+=("$candidate")
                ;;
        esac
    done
    if (( ${#matching[@]} == 1 )); then
        printf '%s\n' "${matching[0]}"
        return 0
    fi
    if (( ${#matching[@]} > 1 )); then
        printf 'ambiguous active AEON text units: %s\n' "${matching[*]}" >&2
        return 1
    fi

    # Prefer the last explicitly selected profile over "enabled" fallback so a
    # failed/inactive HIGH-KV unit does not silently recycle to baseline.
    if persisted="$(read_persisted_text_unit)"; then
        printf '%s\n' "$persisted"
        return 0
    fi

    for candidate in "${TEXT_UNITS[@]}"; do
        state="$("$SYSTEMCTL" --user is-enabled "$candidate" 2>/dev/null || true)"
        case "$state" in
            enabled|enabled-runtime|linked|linked-runtime)
                matching+=("$candidate")
                ;;
        esac
    done
    if (( ${#matching[@]} == 1 )); then
        printf '%s\n' "${matching[0]}"
        return 0
    fi
    if (( ${#matching[@]} > 1 )); then
        printf 'ambiguous enabled AEON text units: %s\n' "${matching[*]}" >&2
    else
        printf 'no active, persisted, or enabled AEON text unit is available for recovery\n' >&2
    fi
    return 1
}

unit_start_submitted() {
    local state
    state="$("$SYSTEMCTL" --user show --property=ActiveState --value "${UNIT}" 2>/dev/null || true)"
    case "$state" in
        active|activating|reloading)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

selected_unit="${GB10_TEXT_UNIT:-}"
if [[ -z "$selected_unit" ]]; then
    selected_unit="$(select_text_unit)"
fi
validate_text_unit "$selected_unit"
readonly UNIT="$selected_unit"
persist_selected_text_unit "$UNIT"

"$LIFECYCLE" stop --unit "$UNIT" \
    --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"
"$LIFECYCLE" start --unit "$UNIT" \
    --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"

deadline=$((SECONDS + ACTIVE_WAIT_SECS))
while (( SECONDS < deadline )); do
    # Accept activating/reloading as restart-command success. Guard's HTTP
    # readiness deadline covers the multi-minute HIGH-KV cold start.
    if unit_start_submitted; then
        exit 0
    fi
    "${SLEEP}" 1
done

printf '%s did not accept start submission within %ss (ActiveState not active/activating/reloading)\n' \
    "${UNIT}" "${ACTIVE_WAIT_SECS}" >&2
exit 1
