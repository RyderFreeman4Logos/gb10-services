#!/usr/bin/env python3
"""Small bounded process runner used by the Guard rebuild."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = ["ScopePolicy", "command", "remaining", "scoped_command"]


@dataclass(frozen=True)
class ScopePolicy:
    phase: str
    memory_high: int
    memory_max: int
    tasks_max: int
    cpu_percent: int
    fsize_bytes: int
    min_mem_available: int
    runtime_seconds: int
    proc_root: object | None = None
    cgroup_root: object | None = None

    def contract(self) -> dict[str, object]:
        return {
            "cpu_percent": self.cpu_percent,
            "fsize_bytes": self.fsize_bytes,
            "memory_high": self.memory_high,
            "memory_max": self.memory_max,
            "min_mem_available": self.min_mem_available,
            "phase": self.phase,
            "runtime_seconds": self.runtime_seconds,
            "tasks_max": self.tasks_max,
        }


def remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2.0)


def command(
    arguments: Sequence[str],
    *,
    timeout: float,
    deadline: float | None = None,
    cwd: os.PathLike[str] | str | None = None,
    env: Mapping[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    max_output_bytes: int = 192 * 1024 * 1024,
    cleanup_reserve: float = 2.0,
) -> str:
    del cleanup_reserve
    if not arguments or timeout <= 0 or max_output_bytes <= 0:
        raise RuntimeError("invalid bounded command")
    budget = timeout if deadline is None else min(timeout, remaining(deadline) or 0.0)
    if budget <= 0:
        raise RuntimeError("command deadline exhausted")
    try:
        process = subprocess.Popen(
            list(arguments),
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=tuple(pass_fds),
        )
    except OSError as error:
        raise RuntimeError(f"command spawn failed: {arguments[0]}") from error

    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    output = bytearray()
    started = time.monotonic()
    try:
        while selector.get_map():
            left = budget - (time.monotonic() - started)
            if deadline is not None:
                left = min(left, remaining(deadline) or 0.0)
            if left <= 0:
                _kill_group(process)
                raise RuntimeError("command deadline exhausted")
            for key, _ in selector.select(left):
                chunk = key.fileobj.read1(65536)
                if chunk:
                    output.extend(chunk)
                    if len(output) > max_output_bytes:
                        _kill_group(process)
                        raise RuntimeError("command output exceeded bound")
                else:
                    selector.unregister(key.fileobj)
            if process.poll() is not None and not selector.get_map():
                break
        status = process.wait(timeout=max(0.1, min(2.0, budget)))
    except BaseException:
        _kill_group(process)
        selector.close()
        raise
    finally:
        selector.close()
    if status != 0:
        detail = bytes(output).decode("utf-8", "replace").splitlines()
        suffix = f": {detail[-1][:512]}" if detail else ""
        raise RuntimeError(f"command failed ({status}): {arguments[0]}{suffix}")
    try:
        return bytes(output).decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"command stdout is not valid UTF-8: {arguments[0]}") from error


def scoped_command(
    arguments: Sequence[str],
    policy: ScopePolicy,
    *,
    systemd_run: str,
    systemctl: str | None = None,
    nice: str | None = None,
    ionice: str | None = None,
    prlimit: str | None = None,
    timeout: float,
    deadline: float | None = None,
    env: Mapping[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    max_output_bytes: int = 192 * 1024 * 1024,
    cleanup_reserve: float = 2.0,
) -> str:
    del systemctl, nice, ionice, prlimit
    if not arguments or "--unshare-user" in arguments or "--unshare-all" in arguments:
        raise RuntimeError("invalid bounded scope command")
    token = f"{os.getpid()}-{time.monotonic_ns()}"
    properties = (
        f"MemoryHigh={policy.memory_high}",
        f"MemoryMax={policy.memory_max}",
        "MemorySwapMax=0",
        f"TasksMax={policy.tasks_max}",
        f"CPUQuota={policy.cpu_percent}%",
        "CPUQuotaPeriodSec=100ms",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "OOMPolicy=kill",
        f"RuntimeMaxSec={policy.runtime_seconds}",
        f"LimitFSIZE={policy.fsize_bytes}",
    )
    launcher = [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=llm-guard-rebuild-{policy.phase}-{token}.scope",
    ]
    for value in properties:
        launcher.extend(("--property", value))
    launcher.extend(("--", *arguments))
    return command(
        launcher,
        timeout=timeout,
        deadline=deadline,
        env=env,
        pass_fds=pass_fds,
        max_output_bytes=max_output_bytes,
        cleanup_reserve=cleanup_reserve,
    )
