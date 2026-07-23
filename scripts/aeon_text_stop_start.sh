#!/usr/bin/env bash
# Cleanly recycle only the AEON text unit for llm-guard-proxy local recovery.
# The proxy performs the bounded chat-completion readiness probe after this
# wrapper observes the systemd unit active; this wrapper never touches
# embedding or either reranker unit.
set -euo pipefail

readonly UNIT="vllm-aeon-27b-dflash.service"
readonly SYSTEMCTL="/usr/bin/systemctl"
readonly TIMEOUT="/usr/bin/timeout"
readonly SLEEP="/usr/bin/sleep"
readonly STOP_TIMEOUT_SECS=90
readonly START_TIMEOUT_SECS=30
readonly ACTIVE_WAIT_SECS=30

"${TIMEOUT}" "${STOP_TIMEOUT_SECS}" "${SYSTEMCTL}" --user stop "${UNIT}"
"${TIMEOUT}" "${START_TIMEOUT_SECS}" "${SYSTEMCTL}" --user start "${UNIT}"

deadline=$((SECONDS + ACTIVE_WAIT_SECS))
while (( SECONDS < deadline )); do
    if "${SYSTEMCTL}" --user is-active --quiet "${UNIT}"; then
        exit 0
    fi
    "${SLEEP}" 1
done

printf '%s did not become active within %ss\n' "${UNIT}" "${ACTIVE_WAIT_SECS}" >&2
exit 1
