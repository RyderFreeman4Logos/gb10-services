#!/usr/bin/env python3
"""Verify tracked systemd user units without sudo or host installation."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

__all__ = ["main"]


ROOT = Path(__file__).resolve().parents[1]
UNIT_SOURCE = ROOT / "systemd"
TARGET_ROOT = ROOT / "target"
EXEC_DIRECTIVES = (
    "ExecCondition=",
    "ExecStart=",
    "ExecStartPre=",
    "ExecStartPost=",
    "ExecStop=",
    "ExecStopPost=",
    "ExecReload=",
)
TARGET_PATTERN = re.compile(r"(?<![A-Za-z0-9_.@-])([A-Za-z0-9_.@-]+\.target)")
EXEC_MODIFIERS = "-+!:@"


def _rewrite_exec(line: str, stub: Path) -> str:
    key, value = line.split("=", 1)
    modifiers = value[: len(value) - len(value.lstrip(EXEC_MODIFIERS))]
    command = value[len(modifiers) :]
    if not command:
        return line
    parts = command.split(maxsplit=1)
    rewritten = f"{key}={modifiers}{stub}"
    return f"{rewritten} {parts[1]}" if len(parts) == 2 else rewritten


def main() -> int:
    units = sorted(UNIT_SOURCE.glob("*.service"))
    if not units:
        raise SystemExit("no tracked systemd user services found")

    TARGET_ROOT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="systemd-user-verify-", dir=TARGET_ROOT) as temp:
        fixture_root = Path(temp)
        unit_dir = fixture_root / "user"
        bin_dir = fixture_root / "bin"
        unit_dir.mkdir()
        bin_dir.mkdir()
        targets = {"basic.target", "default.target"}

        for source in units:
            text = source.read_text()
            targets.update(TARGET_PATTERN.findall(text))
            rewritten: list[str] = []
            for index, line in enumerate(text.splitlines()):
                if line.startswith(EXEC_DIRECTIVES):
                    stub = bin_dir / f"{source.stem}-{index}"
                    stub.write_text("#!/bin/sh\nexit 0\n")
                    stub.chmod(0o755)
                    line = _rewrite_exec(line, stub)
                rewritten.append(line)
            (unit_dir / source.name).write_text("\n".join(rewritten) + "\n")

        for target in targets:
            (unit_dir / target).write_text("[Unit]\n")

        environment = os.environ.copy()
        environment["SYSTEMD_UNIT_PATH"] = str(unit_dir)
        completed = subprocess.run(
            ["systemd-analyze", "--user", "verify", *(unit.name for unit in units)],
            check=False,
            env=environment,
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
