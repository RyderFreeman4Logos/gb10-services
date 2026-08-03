#!/usr/bin/env bash
# Cleanly recycle only the canonical AEON text unit for llm-guard-proxy local recovery.
# Profile selection is the installed aeon-dflash-profiles/active.env symlink.
set -euo pipefail

readonly SYSTEMCTL="/usr/bin/systemctl"
readonly SLEEP="/usr/bin/sleep"
readonly ACTIVE_WAIT_SECS=30
readonly LIFECYCLE="${GB10_LIFECYCLE_BIN:-/home/obj/.local/bin/gb10_lifecycle.sh}"
readonly LIFECYCLE_ACTOR="llm-guard-proxy.local-recovery"
readonly LIFECYCLE_REASON="automatic-local-recovery"
readonly UNIT="vllm-aeon-27b-dflash.service"

# The former HiKV unit name is a systemd alias, not a profile selector.
case "${GB10_TEXT_UNIT:-$UNIT}" in
    vllm-aeon-27b-dflash.service|vllm-aeon-27b-dflash-hikv.service)
        ;;
    *)
        printf 'unsupported AEON text unit: %s\n' "${GB10_TEXT_UNIT}" >&2
        exit 1
        ;;
esac

"$LIFECYCLE" stop --unit "$UNIT" \
    --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"
"$LIFECYCLE" start --unit "$UNIT" \
    --actor "$LIFECYCLE_ACTOR" --reason "$LIFECYCLE_REASON"

deadline=$((SECONDS + ACTIVE_WAIT_SECS))
while (( SECONDS < deadline )); do
    state="$("$SYSTEMCTL" --user show --property=ActiveState --value "$UNIT" 2>/dev/null || true)"
    case "$state" in
        active|activating|reloading)
            exit 0
            ;;
    esac
    "$SLEEP" 1
done

printf '%s did not accept start submission within %ss (ActiveState not active/activating/reloading)\n' \
    "$UNIT" "$ACTIVE_WAIT_SECS" >&2
exit 1
