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
expected_engine_sha256="19115f0844c7d320cc317301a652959b9a3e33789e5513df68765d3b5a7cd1ec"
if [[ -L "$engine" || ! -f "$engine" ]]; then
  echo "embedding activation engine authority is unsafe" >&2
  exit 1
fi
exec {engine_fd}<"$engine"
engine_metadata="$(/usr/bin/stat -L --format='%u:%a:%h:%d:%i:%s' -- "/proc/$$/fd/$engine_fd")"
path_identity="$(/usr/bin/stat --format='%d:%i:%s' -- "$engine")"
engine_sha256="$(/usr/bin/sha256sum -- "/proc/$$/fd/$engine_fd")"
if [[ "$engine_metadata" != "$EUID:644:1:$path_identity" ||
      "${engine_sha256%% *}" != "$expected_engine_sha256" ]]; then
  echo "embedding activation engine authority differs" >&2
  exit 1
fi
exec /usr/bin/python3 -I -B -S "/proc/self/fd/$engine_fd"
