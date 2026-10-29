#!/usr/bin/env python3
"""User-level freeze black box. No root, no kmsg, no service mutation.

1 Hz MemAvailable + PSI + boot_id, fsync each row. A hard power-cut
should still leave the last healthy second. Kernel oops/Xid still need
dmesg_restrict=0; this only records the userspace corpse.

A slow collector (GPU, thermals, model gauges, D-state names) runs in a
child and must not stall the heartbeat. Hung children are killed, not
accumulated.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG_DIR = Path.home() / "log" / "freeze-blackbox"
INTERVAL = 1.0
SLOW_EVERY = 10.0
SLOW_TIMEOUT = 2.0
SLOW_RETENTION_DAYS = 21
HEADER = (
    "ts_local,boot_id,mono_s,uptime_s,load1,mem_avail_kb,mem_free_kb,swap_free_kb,"
    "psi_mem_some_avg10,psi_mem_full_avg10,psi_io_some_avg10,psi_cpu_some_avg10\n"
)
SLOW_HEADER = (
    "ts_local,boot_id,mono_s,tz0_c,gpu_temp_c,gpu_util_pct,gpu_sm_mhz,gpu_mem_mhz,"
    "gpu_throttle_active,gpu_hw_slowdown,gpu_sw_thermal,gpu_hw_thermal,"
    "guard_active,guard_queued,model_running,model_waiting,model_kv_pct,"
    "d_count,d_names\n"
)
ENDPOINTS = (
    ("guard", "http://100.105.4.92:18009/metrics"),
    ("model", "http://100.105.4.92:18010/metrics"),
    ("model", "http://100.105.4.92:18012/metrics"),
    ("model", "http://100.105.4.92:18013/metrics"),
)


def read_mem() -> dict[str, str]:
    want = {"MemAvailable", "MemFree", "SwapFree"}
    out = {k: "" for k in want}
    with Path("/proc/meminfo").open() as fh:
        for line in fh:
            name, rest = line.split(":", 1)
            if name in want:
                out[name] = rest.split()[0]
    return out


def psi_avg10(path: Path, prefix: str) -> str:
    try:
        text = path.read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith(prefix):
            for part in line.split():
                if part.startswith("avg10="):
                    return part.split("=", 1)[1]
    return ""


def monotonic_s() -> str:
    return f"{time.monotonic():.2f}"


def one_line(now: float) -> str:
    lt = time.localtime(now)
    mem = read_mem()
    return ",".join(
        [
            time.strftime("%Y-%m-%dT%H:%M:%S", lt),
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            monotonic_s(),
            Path("/proc/uptime").read_text().split()[0],
            Path("/proc/loadavg").read_text().split()[0],
            mem["MemAvailable"],
            mem["MemFree"],
            mem["SwapFree"],
            psi_avg10(Path("/proc/pressure/memory"), "some"),
            psi_avg10(Path("/proc/pressure/memory"), "full"),
            psi_avg10(Path("/proc/pressure/io"), "some"),
            psi_avg10(Path("/proc/pressure/cpu"), "some"),
        ]
    )


def open_day(path: Path, header: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    if os.fstat(fd).st_size == 0:
        os.write(fd, header.encode())
        os.fsync(fd)
    return fd


def write_last_ok(line: str) -> None:
    tmp = LOG_DIR / "last_ok.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        os.write(fd, (line + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.replace(LOG_DIR / "last_ok")


def _num(text: str, name: str) -> str:
    total = 0.0
    found = False
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        metric = line.split("{", 1)[0].split()[0]
        if metric != name:
            continue
        try:
            total += float(line.rsplit(None, 1)[-1])
        except ValueError:
            continue
        found = True
    if not found:
        return ""
    return str(int(total)) if total.is_integer() else f"{total:.4g}"


def _max_metric(text: str, names: tuple[str, ...]) -> str:
    best: float | None = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        metric = line.split("{", 1)[0].split()[0]
        if metric not in names:
            continue
        try:
            value = float(line.rsplit(None, 1)[-1])
        except ValueError:
            continue
        best = value if best is None else max(best, value)
    if best is None:
        return ""
    return str(int(best)) if best.is_integer() else f"{best:.4g}"


def fetch_text(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "gb10-freeze-blackbox/1"})
    with urllib.request.urlopen(req, timeout=0.6) as response:
        return response.read(256 * 1024).decode("utf-8", "replace")


def model_gauges() -> tuple[str, str, str, str, str]:
    guard_active = guard_queued = ""
    running = waiting = kv = ""
    for kind, url in ENDPOINTS:
        try:
            text = fetch_text(url)
        except Exception:
            continue
        if kind == "guard":
            guard_active = _num(text, "llm_guard_proxy_generation_active") or guard_active
            guard_queued = _num(text, "llm_guard_proxy_generation_queued") or guard_queued
            continue
        running = _num(text, "vllm:num_requests_running") or running
        waiting = _num(text, "vllm:num_requests_waiting") or waiting
        kv = _max_metric(text, ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")) or kv
    return guard_active, guard_queued, running, waiting, kv


def thermal_c() -> str:
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        raw = path.read_text().strip()
        return f"{int(raw) / 1000:.1f}"
    except (OSError, ValueError):
        return ""


def gpu_fields() -> list[str]:
    empty = ["", "", "", "", "", "", "", ""]
    # ponytail: one nvidia-smi per slow sample; parent kills the child at SLOW_TIMEOUT
    command = "nvidia-smi"
    try:
        proc = subprocess.run(
            [
                command,
                "--query-gpu=temperature.gpu,utilization.gpu,clocks.sm,clocks.mem,"
                "clocks_event_reasons.active,clocks_event_reasons.hw_slowdown,"
                "clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_thermal_slowdown",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return empty
    if proc.returncode != 0 or not proc.stdout.strip():
        return empty
    parts = [part.strip() for part in proc.stdout.splitlines()[0].split(",")]
    if len(parts) < 8:
        parts.extend([""] * (8 - len(parts)))
    return [part.replace(",", ";")[:32] for part in parts[:8]]


def d_state() -> tuple[str, str]:
    names: list[str] = []
    seen: set[str] = set()
    count = 0
    deadline = time.monotonic() + 0.3
    proc = Path("/proc")
    for entry in proc.iterdir():
        if time.monotonic() > deadline:
            break
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(errors="replace")
            end = stat.rfind(")")
            state = stat[end + 2 :].split()[0]
            if state != "D":
                continue
            count += 1
            comm = stat[stat.find("(") + 1 : end][:24]
            if comm in seen or len(names) >= 5:
                continue
            seen.add(comm)
            try:
                wchan = (entry / "wchan").read_text(errors="replace").strip()[:24]
            except OSError:
                wchan = "?"
            names.append(f"{comm}:{wchan}")
        except (OSError, IndexError, ValueError):
            continue
    return str(count), "|".join(names)


def slow_line(now: float) -> str:
    guard_active, guard_queued, running, waiting, kv = model_gauges()
    d_count, d_names = d_state()
    return ",".join(
        [
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            monotonic_s(),
            thermal_c(),
            *gpu_fields(),
            guard_active,
            guard_queued,
            running,
            waiting,
            kv,
            d_count,
            d_names,
        ]
    )


def prune_slow(today: str) -> None:
    cutoff = time.time() - SLOW_RETENTION_DAYS * 86400
    for path in LOG_DIR.glob("slow_*.csv"):
        if path.name == f"slow_{today}.csv":
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def slow_child() -> None:
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    now = time.time()
    path = LOG_DIR / time.strftime("slow_%Y-%m-%d.csv", time.localtime(now))
    fd = open_day(path, SLOW_HEADER)
    try:
        line = slow_line(now)
        os.write(fd, (line + "\n").encode())
        os.fsync(fd)
        prune_slow(time.strftime("%Y-%m-%d", time.localtime(now)))
    finally:
        os.close(fd)


def reap_slow(child: subprocess.Popen[bytes] | None) -> subprocess.Popen[bytes] | None:
    if child is None:
        return None
    if child.poll() is not None:
        return None
    if time.monotonic() - getattr(child, "started", 0.0) < SLOW_TIMEOUT:
        return child
    child.kill()
    try:
        child.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        return child
    return None


def spawn_slow(child: subprocess.Popen[bytes] | None) -> subprocess.Popen[bytes] | None:
    child = reap_slow(child)
    if child is not None:
        return child
    proc = subprocess.Popen(
        [sys.executable, "-I", __file__, "--slow-sample", str(LOG_DIR)],
        start_new_session=True,
    )
    proc.started = time.monotonic()  # type: ignore[attr-defined]
    return proc


def main() -> None:
    global LOG_DIR, SLOW_EVERY, SLOW_TIMEOUT
    args = sys.argv[1:]
    if args[:1] == ["--slow-sample"]:
        if len(args) == 2:
            LOG_DIR = Path(args[1])
        elif len(args) != 1:
            raise SystemExit("usage: freeze-blackbox.py --slow-sample [LOG_DIR]")
        slow_child()
        return
    if args[:1] == ["--test-only"]:
        if len(args) != 5:
            raise SystemExit("usage: freeze-blackbox.py --test-only LOG_DIR SLOW_EVERY SLOW_TIMEOUT NVIDIA")
        LOG_DIR = Path(args[1])
        SLOW_EVERY = float(args[2])
        SLOW_TIMEOUT = float(args[3])
        os.environ["PATH"] = str(Path(args[4]).parent) + os.pathsep + os.environ.get("PATH", "")
        if Path(args[4]).name != "nvidia-smi":
            raise SystemExit("test nvidia stub must be named nvidia-smi")
    elif args:
        raise SystemExit("usage: freeze-blackbox.py [--slow-sample]")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    current: Path | None = None
    fd = -1
    slow: subprocess.Popen[bytes] | None = None
    next_slow = 0.0
    while True:
        t0 = time.time()
        path = LOG_DIR / time.strftime("blackbox_%Y-%m-%d.csv", time.localtime(t0))
        if path != current:
            if fd >= 0:
                os.close(fd)
            fd = open_day(path, HEADER)
            current = path
        line = one_line(t0)
        os.write(fd, (line + "\n").encode())
        os.fsync(fd)
        write_last_ok(line)
        now_mono = time.monotonic()
        if now_mono >= next_slow:
            slow = spawn_slow(slow)
            next_slow = now_mono + SLOW_EVERY
        else:
            slow = reap_slow(slow)
        delay = INTERVAL - (time.time() - t0)
        if delay > 0:
            time.sleep(delay)


if __name__ == "__main__":
    main()
