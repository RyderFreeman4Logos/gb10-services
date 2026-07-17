#!/usr/bin/env bash
# Validate the exact ref update captured from Git's pre-push standard input.
set -euo pipefail

if [[ "$#" -ne 1 || ! -f "$1" ]]; then
  echo "ERROR: branch-protection requires the captured pre-push update file." >&2
  exit 1
fi

mapfile -t updates < "$1"
if [[ "${#updates[@]}" -ne 1 || -z "${updates[0]}" ]]; then
  echo "ERROR: pre-push requires exactly one non-empty ref update." >&2
  exit 1
fi

read -r local_ref local_sha remote_ref remote_sha extra <<< "${updates[0]}"
if [[ -n "${extra:-}" || -z "${local_ref:-}" || -z "${local_sha:-}" \
      || -z "${remote_ref:-}" || -z "${remote_sha:-}" ]]; then
  echo "ERROR: malformed pre-push ref update." >&2
  exit 1
fi

if [[ "$local_sha" =~ ^0+$ ]]; then
  echo "ERROR: ref deletion is not authorized by this push gate." >&2
  exit 1
fi

checked_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)" || {
  echo "ERROR: pre-push review requires a checked-out branch." >&2
  exit 1
}
checked_ref="refs/heads/${checked_branch}"
checked_sha="$(git rev-parse --verify HEAD^{commit})"
if [[ "$local_ref" != "$checked_ref" || "$local_sha" != "$checked_sha" ]]; then
  echo "ERROR: pushed ref/SHA does not match the checked-out branch and HEAD." >&2
  exit 1
fi

for protected in main dev master; do
  if [[ "$checked_ref" == "refs/heads/${protected}" \
        || "$remote_ref" == "refs/heads/${protected}" ]]; then
    echo "ERROR: protected remote ref or checked-out branch: ${protected}." >&2
    exit 1
  fi
done

if [[ "$remote_ref" != refs/heads/* ]]; then
  echo "ERROR: only one branch ref may pass the reviewed push gate." >&2
  exit 1
fi

printf '%s\n' "$local_sha"
