#!/usr/bin/env python3
"""Installed entry point for the source-controlled Querit canary owner."""

import importlib
import sys


sys.path.insert(0, "/home/obj/.local/lib/gb10")
main = importlib.import_module("querit_canary_deployment").main


if __name__ == "__main__":
    raise SystemExit(main())
