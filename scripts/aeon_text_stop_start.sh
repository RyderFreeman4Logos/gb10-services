#!/usr/bin/env bash
# Cleanly recycle only the AEON text unit for llm-guard-proxy local recovery.
# The proxy performs the bounded chat-completion readiness probe after this
# wrapper observes the systemd unit active; this wrapper never touches
# embedding or either reranker unit.
set -euo pipefail

readonly SYSTEMCTL="/usr/bin/systemctl"
readonly SLEEP="/usr/bin/sleep"
readonly ACTIVE_WAIT_SECS=30
readonly LIFECYCLE="${GB10_LIFECYCLE_BIN:-/home/obj/.local/bin/gb10_lifecycle.sh}"
readonly LIFECYCLE_ACTOR="llm-guard-proxy.local-recovery"
readonly LIFECYCLE_REASON="automatic-local-recovery"
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

select_text_unit() {
    local candidate state
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
        printf 'no active or enabled AEON text unit is available for recovery\n' >&2
    fi
    return 1
}

selected_unit="${GB10_TEXT_UNIT:-}"
if [[ -z "$selected_unit" ]]; then
    selected_unit="$(select_text_unit)"
fi
validate_text_unit "$selected_unit"
readonly UNIT="$selected_unit"

"$LIFECYCLE" stop --unit "$UNIT" \
    --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"
"$LIFECYCLE" start --unit "$UNIT" \
    --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"

deadline=$((SECONDS + ACTIVE_WAIT_SECS))
while (( SECONDS < deadline )); do
    if "${SYSTEMCTL}" --user is-active --quiet "${UNIT}"; then
        exit 0
    fi
    "${SLEEP}" 1
done

printf '%s did not become active within %ss\n' "${UNIT}" "${ACTIVE_WAIT_SECS}" >&2
exit 1
