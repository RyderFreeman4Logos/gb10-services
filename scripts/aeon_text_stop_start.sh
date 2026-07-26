#!/usr/bin/env bash
# Cleanly recycle the currently-active AEON text unit for llm-guard-proxy
# local recovery.  The proxy performs the bounded chat-completion readiness
# probe after this wrapper observes the systemd unit active; this wrapper
# never touches embedding or either reranker unit.
#
# Variant-aware: detects which AEON text variant (dflash, hikv, etc.) is
# currently active and restarts THAT variant — not a hardcoded one.
# Falls back to the baseline dflash unit if no variant is active.
set -euo pipefail

readonly SYSTEMCTL="/usr/bin/systemctl"
readonly SLEEP="/usr/bin/sleep"
readonly ACTIVE_WAIT_SECS=30
readonly LIFECYCLE="${GB10_LIFECYCLE_BIN:-/home/obj/.local/bin/gb10_lifecycle.sh}"
readonly LIFECYCLE_ACTOR="llm-guard-proxy.local-recovery"
readonly LIFECYCLE_REASON="automatic-local-recovery"
readonly FALLBACK_UNIT="vllm-aeon-27b-dflash.service"

# Discover the currently active (or activating) AEON text variant.
discover_unit() {
    local candidate
    # List all AEON text units that are active or activating.
    while IFS= read -r candidate; do
        local state
        state="$("${SYSTEMCTL}" --user show -p ActiveState --value "${candidate}" 2>/dev/null || true)"
        case "${state}" in
            active|activating)
                echo "${candidate}"
                return 0
                ;;
        esac
    done < <("${SYSTEMCTL}" --user list-units --type=service \
                --all --no-legend --plain 'vllm-aeon-27b-*.service' 2>/dev/null \
              | awk '{print $1}')

    # No active variant found — use the fallback.
    echo "${FALLBACK_UNIT}"
}

UNIT="$(discover_unit)"
readonly UNIT

"${LIFECYCLE}" stop --unit "${UNIT}" \
    --actor "${LIFECYCLE_ACTOR}" --reason "${LIFECYCLE_REASON}"
"${LIFECYCLE}" start --unit "${UNIT}" \
    --actor "${LIFECYCLE_ACTOR}" --reason "${LIFECYCLE_REASON}"

deadline=$((SECONDS + ACTIVE_WAIT_SECS))
while (( SECONDS < deadline )); do
    if "${SYSTEMCTL}" --user is-active --quiet "${UNIT}"; then
        exit 0
    fi
    "${SLEEP}" 1
done

printf '%s did not become active within %ss\n' "${UNIT}" "${ACTIVE_WAIT_SECS}" >&2
exit 1
