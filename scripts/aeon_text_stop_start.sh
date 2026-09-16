#!/usr/bin/env bash
# Cleanly recycle only the canonical AEON text unit for llm-guard-proxy local recovery.
# Recycles the canonical Ultimate :18010 owner; legacy 27B active.env is not a selector.
set -euo pipefail

readonly SYSTEMCTL="/usr/bin/systemctl"
readonly DOCKER="/usr/bin/docker"
readonly STAT="/usr/bin/stat"
readonly ID="/usr/bin/id"
readonly SLEEP="/usr/bin/sleep"
readonly ACTIVE_WAIT_SECS=30
readonly LIFECYCLE="${GB10_LIFECYCLE_BIN:-/home/obj/.local/bin/gb10_lifecycle.sh}"
readonly NO_SWAP_VERIFIER="/home/obj/.local/bin/gb10_verify_vllm_no_swap.sh"
readonly LIFECYCLE_ACTOR="llm-guard-proxy.local-recovery"
readonly LIFECYCLE_REASON="automatic-local-recovery"
readonly UNIT="vllm-aeon-ultimate-uncensored-nvfp4.service"
readonly UNIT_PATH="/home/obj/.config/systemd/user/$UNIT"
readonly CONTAINER="vllm-aeon-ultimate-uncensored-nvfp4"
readonly CIDFILE="/run/user/1001/gb10-memory-guardian/aeon-text.cid"
readonly DOCKER_HOST_VALUE="unix:///run/user/1001/docker.sock"

fail() {
    printf 'aeon_text_stop_start: %s\n' "$*" >&2
    exit 1
}

unit_snapshot() {
    local output active_state="" sub_state="" main_pid=""
    output="$("$SYSTEMCTL" --user show --property=ActiveState \
        --property=SubState --property=MainPID "$UNIT")" \
        || fail "cannot read canonical unit state"
    while IFS='=' read -r key value; do
        case "$key" in
            ActiveState)
                [[ -z "$active_state" ]] || fail "duplicate ActiveState"
                active_state="$value"
                ;;
            SubState)
                [[ -z "$sub_state" ]] || fail "duplicate SubState"
                sub_state="$value"
                ;;
            MainPID)
                [[ -z "$main_pid" ]] || fail "duplicate MainPID"
                main_pid="$value"
                ;;
            *)
                fail "unexpected canonical unit property: $key"
                ;;
        esac
    done <<< "$output"
    [[ -n "$active_state" && -n "$sub_state" && "$main_pid" =~ ^[0-9]+$ ]] \
        || fail "canonical unit state is incomplete or malformed"
    printf '%s %s %s\n' "$active_state" "$sub_state" "$main_pid"
}

private_cid() {
    [[ -f "$CIDFILE" && ! -L "$CIDFILE" ]] \
        || fail "private CID authority is not a regular non-symlink file"
    local metadata owner group mode links size cid
    metadata="$("$STAT" -c '%u %g %a %h %s' -- "$CIDFILE")" \
        || fail "cannot stat private CID authority"
    read -r owner group mode links size <<< "$metadata"
    [[ "$owner" == "$("$ID" -u)" && "$group" == "$("$ID" -g)" \
        && "$mode" == 600 && "$links" == 1 && ( "$size" == 64 || "$size" == 65 ) ]] \
        || fail "private CID authority owner, mode, link count, or size is unsafe"
    cid="$(<"$CIDFILE")"
    [[ "$cid" =~ ^[0-9a-f]{64}$ ]] \
        || fail "private CID authority is not one full container ID"
    printf '%s\n' "$cid"
}

join_existing_start() {
    local snapshot_before="$1" active_state sub_state main_pid
    local cid_before cid_after identity docker_id docker_name running extra snapshot_after
    read -r active_state sub_state main_pid <<< "$snapshot_before"
    (( main_pid > 1 )) || fail "activating canonical unit has no live MainPID"
    cid_before="$(private_cid)"

    /usr/bin/env -i HOME=/home/obj PATH=/usr/bin:/bin LC_ALL=C \
        DOCKER_HOST="$DOCKER_HOST_VALUE" /usr/bin/bash --noprofile --norc \
        "$NO_SWAP_VERIFIER" --unit "$UNIT_PATH" --container "$CONTAINER" \
        || fail "activating canonical generation failed strict verification"
    identity="$(/usr/bin/env -i HOME=/home/obj PATH=/usr/bin:/bin LC_ALL=C \
        DOCKER_HOST="$DOCKER_HOST_VALUE" "$DOCKER" inspect --type container \
        --format '{{.Id}} {{.Name}} {{.State.Running}}' "$cid_before")" \
        || fail "cannot inspect private CID generation"
    read -r docker_id docker_name running extra <<< "$identity"
    [[ -z "$extra" && "$docker_id" == "$cid_before" \
        && "$docker_name" == "/$CONTAINER" && "$running" == true ]] \
        || fail "private CID does not identify the expected running container"

    snapshot_after="$(unit_snapshot)"
    cid_after="$(private_cid)"
    local after_active after_sub after_pid
    read -r after_active after_sub after_pid <<< "$snapshot_after"
    [[ "$after_pid" == "$main_pid" && "$cid_after" == "$cid_before" ]] \
        || fail "canonical unit or private CID generation changed during verification"
    [[ ( "$after_active" == activating && "$after_sub" == start-post ) \
        || ( "$after_active" == active && "$after_sub" == running ) ]] \
        || fail "canonical unit left its verified startup generation"
    printf 'aeon_text_stop_start: joined existing canonical startup cid=%s\n' "$cid_before"
}

# GB10_TEXT_UNIT is an allowlist only. HiKV remains an alias name, not a selector.
# Recovery always recycles the current :18010 owner (UNIT), never the alias.
case "${GB10_TEXT_UNIT:-$UNIT}" in
    vllm-aeon-ultimate-uncensored-nvfp4.service|vllm-aeon-27b-dflash.service|vllm-aeon-27b-dflash-hikv.service)
        ;;
    *)
        printf 'unsupported AEON text unit: %s\n' "${GB10_TEXT_UNIT:-$UNIT}" >&2
        exit 1
        ;;
esac

snapshot="$(unit_snapshot)"
read -r active_state sub_state _main_pid <<< "$snapshot"
if [[ "$active_state" == activating && "$sub_state" == start-post ]]; then
    join_existing_start "$snapshot"
    exit 0
fi

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
