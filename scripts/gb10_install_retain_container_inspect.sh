#!/usr/bin/env bash
# Install the text-unit inspect retainer before any unit that calls it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -d -m 0755 /home/obj/.local/bin
install -m 0755 \
  "$ROOT/scripts/gb10_retain_container_inspect.sh" \
  /home/obj/.local/bin/gb10_retain_container_inspect.sh
