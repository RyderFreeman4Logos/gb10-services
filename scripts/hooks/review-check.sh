#!/usr/bin/env bash
# Bind a passing CSA review to the exact single branch update Git will push.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
updates_file="$(mktemp "${TMPDIR:-/tmp}/gb10-pre-push-updates.XXXXXXXX")"
trap 'rm -f -- "$updates_file"' EXIT
trap 'exit 1' HUP INT TERM

while IFS= read -r update || [[ -n "$update" ]]; do
  printf '%s\n' "$update" >> "$updates_file"
done

mapfile -t updates < "$updates_file"
if [[ "${#updates[@]}" -ne 1 || -z "${updates[0]}" ]]; then
  echo "ERROR: pre-push review requires exactly one non-empty ref update." >&2
  exit 1
fi

local_sha="$("$script_dir/branch-protection.sh" "$updates_file")"
if [[ -z "$local_sha" || "$local_sha" != "$(git rev-parse --verify HEAD^{commit})" ]]; then
  echo "ERROR: branch gate did not attest the checked-out HEAD." >&2
  exit 1
fi

if ! command -v csa >/dev/null 2>&1; then
  echo "ERROR: csa is required for the pre-push review gate." >&2
  exit 1
fi

main_sha="$(git rev-parse --verify main^{commit})" || {
  echo "ERROR: local main is required to bind the full-diff review range." >&2
  exit 1
}
if csa review --check-verdict --range main...HEAD; then
  if [[ "$local_sha" != "$(git rev-parse --verify HEAD^{commit})" \
        || "$main_sha" != "$(git rev-parse --verify main^{commit})" ]]; then
    echo "ERROR: reviewed HEAD or main changed while the gate was running." >&2
    exit 1
  fi
  echo "pre-push: passing full-diff review verified for HEAD ${local_sha}."
  exit 0
fi

cat >&2 <<GATE_BLOCKED
<!-- CSA:REVIEW_GATE_BLOCKED head_sha="${local_sha}" range="main...HEAD" -->
Push blocked: no passing review is bound to the exact checked-out HEAD and range.
Run: csa review --range main...HEAD --sa-mode true
Wait for PASS, then retry this exact single-ref push.
<!-- /CSA:REVIEW_GATE_BLOCKED -->
GATE_BLOCKED
exit 1
