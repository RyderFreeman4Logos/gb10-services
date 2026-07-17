#!/usr/bin/env python3
"""Installed public-wire warmup entry point for the Querit canary."""

import importlib
import sys


sys.path.insert(0, "/home/obj/.local/lib/gb10")
lifecycle = importlib.import_module("querit_canary_lifecycle")


if __name__ == "__main__":
    lifecycle.SystemHost().warm()
