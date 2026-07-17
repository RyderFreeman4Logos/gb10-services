#!/usr/bin/bash
# Canonicalize every ref transition supplied by Git's pre-push hook.
set -euo pipefail
export LC_ALL=C

GIT=/usr/bin/git
SHA256SUM=/usr/bin/sha256sum
SORT=/usr/bin/sort

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

hash_text() {
  local output
  output=$(printf '%s' "$1" | "$SHA256SUM")
  printf '%s' "${output%% *}"
}

resolve_commit() {
  local object_sha=$1
  "$GIT" rev-parse --verify "${object_sha}^{commit}" 2>/dev/null \
    || die "pushed object does not peel to a commit: ${object_sha}."
}

resolve_tree() {
  local commit_sha=$1
  "$GIT" rev-parse --verify "${commit_sha}^{tree}" 2>/dev/null \
    || die "commit tree cannot be resolved: ${commit_sha}."
}

if [[ "$#" -ne 3 ]]; then
  die "branch-protection requires updates plus the exact remote name and location."
fi
updates_file=$1
remote_name=$2
remote_location=$3
[[ -f "$updates_file" && ! -L "$updates_file" ]] \
  || die "captured pre-push updates are missing or unsafe."
[[ "$remote_name" =~ ^[A-Za-z0-9._-]+$ ]] \
  || die "remote name is malformed."
[[ -n "$remote_location" && "$remote_location" != *$'\n'* ]] \
  || die "remote location is malformed."

configured_location=$("$GIT" remote get-url --push -- "$remote_name" 2>/dev/null) \
  || die "remote identity is not configured."
[[ "$configured_location" == "$remote_location" ]] \
  || die "remote location differs from the configured push URL."

object_format=$("$GIT" rev-parse --show-object-format)
case "$object_format" in
  sha1) sha_length=40 ;;
  sha256) sha_length=64 ;;
  *) die "repository object format is unsupported." ;;
esac
printf -v zero_sha '%*s' "$sha_length" ''
zero_sha=${zero_sha// /0}
sha_pattern="^[0-9a-f]{${sha_length}}$"

mapfile -t updates < "$updates_file"
(( ${#updates[@]} > 0 && ${#updates[@]} <= 1024 )) \
  || die "pre-push update set must contain between 1 and 1024 refs."

declare -A seen_remote_refs=()
declare -a plan_rows=()
for update in "${updates[@]}"; do
  read -r -a fields <<< "$update"
  (( ${#fields[@]} == 4 )) || die "malformed pre-push ref update."
  local_ref=${fields[0]}
  local_sha=${fields[1]}
  remote_ref=${fields[2]}
  remote_old_sha=${fields[3]}
  [[ "$local_sha" =~ $sha_pattern && "$remote_old_sha" =~ $sha_pattern ]] \
    || die "pre-push update contains a malformed object ID."
  "$GIT" check-ref-format "$remote_ref" >/dev/null 2>&1 \
    || die "remote ref is malformed: ${remote_ref}."
  case "$remote_ref" in
    refs/heads/*|refs/tags/*) ;;
    *) die "only branch and tag remote refs are supported." ;;
  esac
  [[ -z "${seen_remote_refs[$remote_ref]+present}" ]] \
    || die "pre-push update set repeats remote ref: ${remote_ref}."
  seen_remote_refs[$remote_ref]=1

  for protected in main dev master; do
    [[ "$remote_ref" != "refs/heads/${protected}" ]] \
      || die "protected remote ref cannot be updated or deleted: ${remote_ref}."
    [[ "$local_ref" != "refs/heads/${protected}" ]] \
      || die "protected local ref cannot be pushed: ${local_ref}."
  done

  if [[ "$local_sha" == "$zero_sha" ]]; then
    [[ "$local_ref" == "(delete)" && "$remote_old_sha" != "$zero_sha" ]] \
      || die "deletion update is malformed or has no remote object."
    "$GIT" cat-file -e "${remote_old_sha}^{object}" 2>/dev/null \
      || die "deleted remote object is unavailable locally; fetch before pushing."
    base_commit=$(resolve_commit "$remote_old_sha")
    base_tree=$(resolve_tree "$base_commit")
    base_ref=-
    if [[ "$remote_ref" == refs/heads/* ]]; then
      base_ref="refs/remotes/${remote_name}/${remote_ref#refs/heads/}"
      tracked_old=$("$GIT" rev-parse --verify "${base_ref}^{object}" 2>/dev/null) \
        || die "remote-tracking base is missing for deletion: ${base_ref}."
      [[ "$tracked_old" == "$remote_old_sha" ]] \
        || die "remote-tracking base is stale for deletion: ${base_ref}."
    fi
    plan_rows+=("update\t${remote_ref}\t${local_ref}\t${local_sha}\t-\t-\t${remote_old_sha}\t${base_commit}\t${base_tree}\t-\tdelete\t${base_ref}")
    continue
  fi

  [[ "$local_ref" != "(delete)" ]] || die "non-deletion update has a deletion ref."
  "$GIT" check-ref-format "$local_ref" >/dev/null 2>&1 \
    || die "local ref is malformed: ${local_ref}."
  case "$local_ref" in
    refs/heads/*|refs/tags/*) ;;
    *) die "only branch and tag local refs are supported." ;;
  esac
  observed_local=$("$GIT" rev-parse --verify "${local_ref}^{object}" 2>/dev/null) \
    || die "local ref does not resolve: ${local_ref}."
  [[ "$observed_local" == "$local_sha" ]] \
    || die "local ref changed from the SHA supplied by Git: ${local_ref}."
  local_commit=$(resolve_commit "$local_sha")
  local_tree=$(resolve_tree "$local_commit")

  if [[ "$remote_old_sha" == "$zero_sha" ]]; then
    base_ref=$("$GIT" symbolic-ref --quiet "refs/remotes/${remote_name}/HEAD" 2>/dev/null) \
      || base_ref="refs/remotes/${remote_name}/main"
    [[ "$base_ref" == "refs/remotes/${remote_name}/"* ]] \
      || die "remote default branch identity is unsafe."
    base_commit=$("$GIT" rev-parse --verify "${base_ref}^{commit}" 2>/dev/null) \
      || die "new ref requires a fetched remote default-branch base."
    base_tree=$(resolve_tree "$base_commit")
    transition=new
  else
    "$GIT" cat-file -e "${remote_old_sha}^{object}" 2>/dev/null \
      || die "remote old object is unavailable locally; fetch before pushing."
    base_commit=$(resolve_commit "$remote_old_sha")
    base_tree=$(resolve_tree "$base_commit")
    base_ref=-
    if [[ "$remote_ref" == refs/heads/* ]]; then
      base_ref="refs/remotes/${remote_name}/${remote_ref#refs/heads/}"
      tracked_old=$("$GIT" rev-parse --verify "${base_ref}^{object}" 2>/dev/null) \
        || die "remote-tracking base is missing: ${base_ref}."
      [[ "$tracked_old" == "$remote_old_sha" ]] \
        || die "remote-tracking base is stale: ${base_ref}."
    fi
    if "$GIT" merge-base --is-ancestor "$base_commit" "$local_commit"; then
      transition=fast-forward
    else
      merge_status=$?
      (( merge_status == 1 )) || die "cannot classify the remote ref transition."
      transition=force
    fi
  fi

  [[ "$base_commit" != "$local_commit" && "$base_tree" != "$local_tree" ]] \
    || die "empty commit or tree transition has no reviewable evidence."
  review_range="${base_commit}..${local_commit}"
  plan_rows+=("update\t${remote_ref}\t${local_ref}\t${local_sha}\t${local_commit}\t${local_tree}\t${remote_old_sha}\t${base_commit}\t${base_tree}\t${review_range}\t${transition}\t${base_ref}")
done

printf 'schema\tgb10-pre-push-plan-v1\n'
printf 'remote\t%s\t%s\t%s\t%s\n' \
  "$remote_name" "$(hash_text "$remote_location")" \
  "$(hash_text "$configured_location")" "$object_format"
printf '%b\n' "${plan_rows[@]}" | "$SORT" -t $'\t' -k2,2 -k3,3
