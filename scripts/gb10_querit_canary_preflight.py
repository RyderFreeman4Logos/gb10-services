#!/usr/bin/env python3
"""Fail-closed systemd ExecCondition for lifecycle-authorized canary starts."""

import importlib
import sys


__all__: list[str] = []


sys.path.insert(0, "/home/obj/.local/lib/gb10")
lifecycle = importlib.import_module("querit_canary_lifecycle")


if __name__ == "__main__":
    try:
        raise SystemExit(lifecycle.main(["preflight"]))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(255) from exc
