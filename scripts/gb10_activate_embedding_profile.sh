#!/usr/bin/bash
# Canonical production entry point: no path, verifier, or command injection channel.
set -Eeuo pipefail
umask 077
if (( $# != 0 )); then
  echo "usage: gb10_activate_embedding_profile.sh" >&2
  exit 2
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
engine="$script_dir/gb10_embedding_activation.py"
expected_engine_sha256="c2e03fa7246e2bfd6e89ba222ce8b865f840f9208fa44e8bb56a411ce51087aa"
if [[ -L "$engine" || ! -f "$engine" ]]; then
  echo "embedding activation engine authority is unsafe" >&2
  exit 1
fi
engine_metadata="$(/usr/bin/stat --format='%u:%a:%h' -- "$engine")"
engine_sha256="$(/usr/bin/sha256sum -- "$engine")"
if [[ "$engine_metadata" != "$EUID:644:1" ||
      "${engine_sha256%% *}" != "$expected_engine_sha256" ]]; then
  echo "embedding activation engine authority differs" >&2
  exit 1
fi
exec /usr/bin/python3 -I -B -S "$engine"
