#!/usr/bin/bash
# Verify reviews and seal a receipt for the complete immutable pre-push update set.
set -euo pipefail
export LC_ALL=C
umask 077

CSA_EXECUTABLE="/home/obj/.local/bin/csa"
GIT=/usr/bin/git
CMP=/usr/bin/cmp
SHA256SUM=/usr/bin/sha256sum

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
temporary_receipt=
cleanup() {
  /usr/bin/rm -f -- "$updates_file" "$plan_before" "$plan_after" "$candidate_receipt"
  if [[ -n "$temporary_receipt" ]]; then
    /usr/bin/rm -f -- "$temporary_receipt"
  fi
  /usr/bin/rmdir -- "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

while IFS= read -r update || [[ -n "$update" ]]; do
  printf '%s\n' "$update" >> "$updates_file"
done
[[ -f "$updates_file" ]] || : > "$updates_file"

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
if [[ -e "$receipt_dir" ]]; then
  [[ -d "$receipt_dir" && ! -L "$receipt_dir" ]] \
    || die "pre-push receipt directory is unsafe."
else
  /usr/bin/mkdir -m 700 -- "$receipt_dir"
fi
read -r directory_owner directory_mode directory_links directory_kind < <(
  /usr/bin/stat -Lc '%u %a %h %F' -- "$receipt_dir"
)
[[ "$directory_owner" == "$(/usr/bin/id -u)" \
    && "$directory_links" == 2 && "$directory_kind" == directory \
    && "$directory_mode" == 700 ]] \
  || die "pre-push receipt directory ownership or mode is unsafe."

receipt_output=$("$SHA256SUM" -- "$candidate_receipt")
receipt_sha=${receipt_output%% *}
receipt_path="${receipt_dir}/${receipt_sha}.receipt"
if [[ -e "$receipt_path" ]]; then
  [[ -f "$receipt_path" && ! -L "$receipt_path" ]] \
    || die "existing pre-push receipt is unsafe."
  read -r receipt_owner receipt_mode receipt_links receipt_kind < <(
    /usr/bin/stat -Lc '%u %a %h %F' -- "$receipt_path"
  )
  [[ "$receipt_owner" == "$(/usr/bin/id -u)" && "$receipt_mode" == 600 \
      && "$receipt_links" == 1 && "$receipt_kind" == "regular file" ]] \
    || die "existing pre-push receipt metadata is unsafe."
  "$CMP" -s -- "$candidate_receipt" "$receipt_path" \
    || die "existing pre-push receipt is stale or tampered."
fi

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

if [[ ! -e "$receipt_path" ]]; then
  temporary_receipt="${receipt_dir}/.${receipt_sha}.$$"
  /usr/bin/install -m 600 -- "$candidate_receipt" "$temporary_receipt"
  /usr/bin/mv -- "$temporary_receipt" "$receipt_path"
  temporary_receipt=
fi
"$CMP" -s -- "$candidate_receipt" "$receipt_path" \
  || die "pre-push receipt verification failed after creation."

printf 'pre-push: verified %d review range(s), %d ref update(s), receipt %s.\n' \
  "${#review_ranges[@]}" "$(/usr/bin/awk -F '\t' '$1 == "update" { count++ } END { print count + 0 }' "$plan_before")" \
  "$receipt_sha"
