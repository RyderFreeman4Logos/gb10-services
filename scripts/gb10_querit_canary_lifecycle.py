#!/usr/bin/env python3
"""Installed entry point for transactional Querit canary lifecycle changes."""

import importlib
import sys


sys.path.insert(0, "/home/obj/.local/lib/gb10")
main = importlib.import_module("querit_canary_lifecycle").main


if __name__ == "__main__":
    raise SystemExit(main())
