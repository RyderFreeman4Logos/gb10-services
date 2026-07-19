#!/usr/bin/bash
# Verify reviews and seal a receipt for the complete immutable pre-push update set.
set -euo pipefail
export LC_ALL=C
umask 077

CSA_EXECUTABLE="/home/obj/.local/bin/csa"
GIT=/usr/bin/git
CMP=/usr/bin/cmp
SHA256SUM=/usr/bin/sha256sum
PYTHON=/usr/bin/python3
MAX_UPDATES_BYTES=1048576
MAX_UPDATE_LINE_BYTES=16384

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

if [[ "$#" -ne 2 || -z "$1" || -z "$2" ]]; then
  die "pre-push review requires Git's exact remote name and remote location."
fi
remote_name=$1
remote_location=$2
script_dir=$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && /usr/bin/pwd -P)

temporary_dir=$(/usr/bin/mktemp -d /tmp/gb10-pre-push-review.XXXXXXXX)
updates_file="${temporary_dir}/updates"
plan_before="${temporary_dir}/plan-before"
plan_after="${temporary_dir}/plan-after"
candidate_receipt="${temporary_dir}/candidate-receipt"
cleanup() {
  /usr/bin/rm -f -- "$updates_file" "$plan_before" "$plan_after" "$candidate_receipt"
  /usr/bin/rmdir -- "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

: > "$updates_file"
captured_bytes=0
while IFS= read -r update || [[ -n "$update" ]]; do
  (( ${#update} <= MAX_UPDATE_LINE_BYTES )) \
    || die "pre-push update line exceeds the capture bound."
  (( captured_bytes += ${#update} + 1 ))
  (( captured_bytes <= MAX_UPDATES_BYTES )) \
    || die "pre-push update set exceeds the capture bound."
  printf '%s\n' "$update" >> "$updates_file"
done

if ! "$script_dir/branch-protection.sh" \
    "$updates_file" "$remote_name" "$remote_location" > "$plan_before"; then
  die "pre-push update set or remote identity failed validation."
fi

attest_csa() {
  [[ "$CSA_EXECUTABLE" == /* && -f "$CSA_EXECUTABLE" \
      && -x "$CSA_EXECUTABLE" && ! -L "$CSA_EXECUTABLE" ]] \
    || die "the fixed CSA executable is missing or unsafe."
  [[ "$(/usr/bin/readlink -f -- "$CSA_EXECUTABLE")" == "$CSA_EXECUTABLE" ]] \
    || die "the fixed CSA executable path contains a symlink."
  local owner mode links device inode kind current_uid
  read -r owner mode links device inode kind < <(
    /usr/bin/stat -Lc '%u %a %h %d %i %F' -- "$CSA_EXECUTABLE"
  )
  current_uid=$(/usr/bin/id -u)
  [[ "$owner" == "$current_uid" && "$links" == 1 && "$kind" == "regular file" ]] \
    || die "the fixed CSA executable identity is unsafe."
  (( (8#$mode & 8#022) == 0 )) \
    || die "the fixed CSA executable is group/world writable."
  local sha_output
  sha_output=$("$SHA256SUM" -- "$CSA_EXECUTABLE")
  printf '%s:%s:%s' "${sha_output%% *}" "$device" "$inode"
}

csa_identity_before=$(attest_csa)
declare -A seen_ranges=()
declare -a review_ranges=()
while IFS=$'\t' read -r kind _remote_ref _local_ref _local_sha \
    _local_commit _local_tree _remote_old _base_commit _base_tree \
    review_range _transition _base_ref; do
  if [[ "$kind" == update && "$review_range" != - \
        && -z "${seen_ranges[$review_range]+present}" ]]; then
    seen_ranges[$review_range]=1
    review_ranges+=("$review_range")
  fi
done < "$plan_before"

{
  printf 'schema\tgb10-pre-push-receipt-v1\n'
  printf 'csa\t%s\t%s\n' "$CSA_EXECUTABLE" "$csa_identity_before"
  /usr/bin/cat -- "$plan_before"
  for review_range in "${review_ranges[@]}"; do
    printf 'review\t%s\tpass-required\n' "$review_range"
  done
} > "$candidate_receipt"

git_dir=$("$GIT" rev-parse --absolute-git-dir)
receipt_dir="${git_dir}/gb10-pre-push-receipts"
receipt_output=$("$SHA256SUM" -- "$candidate_receipt")
receipt_sha=${receipt_output%% *}
"$PYTHON" "$script_dir/receipt-store.py" \
  verify "$receipt_dir" "$candidate_receipt" "$receipt_sha" \
  || die "pre-push receipt state failed validation."

for review_range in "${review_ranges[@]}"; do
  if ! "$CSA_EXECUTABLE" review --check-verdict --range "$review_range"; then
    cat >&2 <<GATE_BLOCKED
<!-- CSA:REVIEW_GATE_BLOCKED range="${review_range}" receipt_sha256="${receipt_sha}" -->
Push blocked: no passing review is bound to this exact immutable commit range.
Run: ${CSA_EXECUTABLE} review --range ${review_range} --sa-mode true
Wait for PASS, then retry the unchanged complete push update set.
<!-- /CSA:REVIEW_GATE_BLOCKED -->
GATE_BLOCKED
    exit 1
  fi
done

if ! "$script_dir/branch-protection.sh" \
    "$updates_file" "$remote_name" "$remote_location" > "$plan_after"; then
  die "ref, tree, base, or remote identity changed while reviews were checked."
fi
"$CMP" -s -- "$plan_before" "$plan_after" \
  || die "ref, tree, base, update order, or remote identity changed while reviews were checked."
csa_identity_after=$(attest_csa)
[[ "$csa_identity_after" == "$csa_identity_before" ]] \
  || die "fixed CSA executable changed while reviews were checked."

"$PYTHON" "$script_dir/receipt-store.py" \
  publish "$receipt_dir" "$candidate_receipt" "$receipt_sha" \
  || die "pre-push receipt publication failed."

printf 'pre-push: verified %d review range(s), %d ref update(s), receipt %s.\n' \
  "${#review_ranges[@]}" "$(/usr/bin/awk -F '\t' '$1 == "update" { count++ } END { print count + 0 }' "$plan_before")" \
  "$receipt_sha"
