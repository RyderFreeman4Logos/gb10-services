#!/usr/bin/env python3
"""
vllm serve wrapper that applies hang_guard patches before serving.

Usage in Docker entrypoint:
  exec python3 /opt/hang_guard/aeon_vllm_wrapper.py serve "$MODEL_DIR" ...
  (replaces: exec vllm serve "$MODEL_DIR" ...)

CRITICAL: main() MUST be inside `if __name__ == "__main__":` guard.
vLLM V1 uses multiprocessing spawn for EngineCore — the child re-imports
this script, and module-level main() would recursively start another server.
"""

import sys


def _patch_reasoning_defaults():
    """No-op: AEON reasoning defaults are now caller-controlled.

    Previously this monkey-patched OpenAIServingChat to inject a default
    thinking budget (32768) and force enable_thinking=True on requests
    that did not specify reasoning_effort or thinking_token_budget.

    That global default caused tool-use / Hermes requests to be forced
    into thinking mode, triggering malformed-tool-call retry loops.
    Callers (guard-proxy, Hermes) now decide per-request whether to
    enable thinking; vLLM must faithfully forward what the caller sends.
    """
    return


# Apply hang guard BEFORE vllm initializes engine/workers
try:
    import aeon_hang_guard

    aeon_hang_guard.apply()
except Exception as e:
    print(f"[hang_guard] ERROR: failed to apply patches: {e}", file=sys.stderr)
    raise

try:
    _patch_reasoning_defaults()
except Exception as e:
    print(f"[reasoning_defaults] ERROR: failed to apply patches: {e}", file=sys.stderr)
    raise

if __name__ == "__main__":
    from vllm.scripts import main

    sys.exit(main())
