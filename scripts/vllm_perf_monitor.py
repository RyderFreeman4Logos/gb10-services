#!/usr/bin/env python3
"""Low-write-rate vLLM Prometheus performance sampler.

Writes one CSV row every VLLM_PERF_INTERVAL_SECONDS (default: 30s) with
counter deltas/rates for the GB10 embedding, AEON text, and reranker services.
The script is intentionally conservative about disk wear and fill risk:

- one append per interval, no fsync;
- retention by age and total byte cap;
- per-current-file byte cap;
- stop writing CSV rows when free space drops below a configured floor;
- journal output only on startup and at most hourly for repeated write skips.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import re
import signal
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

MetricRows = dict[str, list[tuple[dict[str, str], float]]]

# Raw vLLM publishes. 18002/18009/18003 are Guard listeners and do not
# expose engine gauges, so polling them leaves workload columns blank.
DEFAULT_ENDPOINTS = (
    ("emb", "http://100.105.4.92:18012/metrics"),
    ("aeon", "http://100.105.4.92:18010/metrics"),
    ("rerank", "http://100.105.4.92:18013/metrics"),
)

METRIC_RE = re.compile(r"^([^#{}\s]+)(?:\{([^}]*)\})?\s+([-+0-9.eE]+)\s*$")
LABEL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=\"((?:\\.|[^\"\\])*)\"")
DATE_RE = re.compile(r"^vllm_perf_\d{4}-\d{2}-\d{2}\.csv$")
STOP = False

COUNTERS = {
    "prompt_tokens": "vllm:prompt_tokens_total",
    "generation_tokens": "vllm:generation_tokens_total",
    "requests": "vllm:request_success_total",
    "request_errors": "vllm:request_success_total",
    "ttft_count": "vllm:time_to_first_token_seconds_count",
    "ttft_sum": "vllm:time_to_first_token_seconds_sum",
    "tpot_count": "vllm:request_time_per_output_token_seconds_count",
    "tpot_sum": "vllm:request_time_per_output_token_seconds_sum",
    "itl_count": "vllm:inter_token_latency_seconds_count",
    "itl_sum": "vllm:inter_token_latency_seconds_sum",
    "e2e_count": "vllm:e2e_request_latency_seconds_count",
    "e2e_sum": "vllm:e2e_request_latency_seconds_sum",
    "preemptions": "vllm:num_preemptions_total",
    "prefix_queries": "vllm:prefix_cache_queries_total",
    "prefix_hits": "vllm:prefix_cache_hits_total",
    "spec_draft": "vllm:spec_decode_num_draft_tokens_total",
    "spec_accepted": "vllm:spec_decode_num_accepted_tokens_total",
}

GAUGES = {
    "running": "vllm:num_requests_running",
    "waiting": "vllm:num_requests_waiting",
    "swapped": "vllm:num_requests_swapped",
    "kv_cache_pct": "vllm:kv_cache_usage_perc",
}

BASE_COLUMNS = ["timestamp", "interval_s", "log_skipped", "skip_reason"]
SERVICE_COLUMNS = [
    "up",
    "elapsed_s",
    "running",
    "waiting",
    "swapped",
    "kv_cache_pct",
    "prompt_tok_s",
    "generation_tok_s",
    "req_s",
    "error_delta",
    "ttft_avg_s",
    "tpot_avg_s",
    "itl_avg_s",
    "e2e_avg_s",
    "preemptions_delta",
    "prefix_hit_rate",
    "spec_draft_tok_s",
    "spec_accepted_tok_s",
    "spec_accept_rate",
    "counter_reset",
    "err",
]


def _handle_signal(signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"WARN invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"WARN invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def parse_endpoints(raw: str | None) -> list[tuple[str, str]]:
    if not raw:
        return list(DEFAULT_ENDPOINTS)
    endpoints: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"endpoint entry must be name=url, got {item!r}")
        name, url = item.split("=", 1)
        name = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
        url = url.strip()
        if not name or not url.startswith(("http://", "https://")):
            raise ValueError(f"invalid endpoint entry {item!r}")
        endpoints.append((name, url))
    if not endpoints:
        raise ValueError("no valid endpoints")
    return endpoints


def parse_labels(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    labels: dict[str, str] = {}
    for match in LABEL_RE.finditer(raw):
        labels[match.group(1)] = match.group(2).replace(r"\"", '"').replace(r"\\", "\\")
    return labels


def parse_metrics(text: str) -> MetricRows:
    rows: MetricRows = {}
    for line in text.splitlines():
        match = METRIC_RE.match(line)
        if not match:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        rows.setdefault(match.group(1), []).append((parse_labels(match.group(2)), value))
    return rows


def fetch_metrics(url: str, timeout: float) -> MetricRows:
    # ponytail: urlopen timeout covers the blocking read; a stuck DNS lookup
    # still sits in getaddrinfo. Upgrade path is a bounded resolver thread.
    req = urllib.request.Request(url, headers={"User-Agent": "gb10-vllm-perf-monitor/1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read(4 * 1024 * 1024)
    return parse_metrics(body.decode("utf-8", "replace"))


def sum_metric(rows: MetricRows, name: str, selector: Callable[[dict[str, str]], bool] | None = None) -> float | None:
    values = rows.get(name)
    if not values:
        return None
    total = 0.0
    found = False
    for labels, value in values:
        if selector is not None and not selector(labels):
            continue
        total += value
        found = True
    return total if found else None


def max_metric(rows: MetricRows, names: Iterable[str]) -> float | None:
    vals: list[float] = []
    for name in names:
        vals.extend(value for _labels, value in rows.get(name, []))
    return max(vals) if vals else None


def metric_snapshot(rows: MetricRows) -> dict[str, float | None]:
    snap: dict[str, float | None] = {}
    for key, metric in COUNTERS.items():
        if key == "request_errors":
            snap[key] = sum_metric(
                rows,
                metric,
                lambda labels: labels.get("finished_reason") == "error",
            )
        else:
            snap[key] = sum_metric(rows, metric)
    for key, metric in GAUGES.items():
        if key == "kv_cache_pct":
            snap[key] = max_metric(rows, ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"))
        else:
            snap[key] = sum_metric(rows, metric)
    return snap


def error_class(exc: BaseException) -> str:
    """Content-free failure class. Never includes the exception text."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, ConnectionResetError):
        return "closed"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return "timeout"
        if isinstance(reason, ConnectionResetError):
            return "closed"
        if isinstance(reason, ConnectionRefusedError):
            return "refused"
        return "url_error"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in {104, 54}:
        return "closed"
    return type(exc).__name__


def short_error(exc: BaseException) -> str:
    return error_class(exc)


def delta(curr: float | None, prev: float | None) -> tuple[float | None, bool]:
    if curr is None or prev is None:
        return None, False
    if curr < prev:
        return None, True
    return curr - prev, False


def rate(curr: float | None, prev: float | None, elapsed: float) -> tuple[float | None, bool]:
    diff, reset = delta(curr, prev)
    if diff is None or elapsed <= 0:
        return None, reset
    return diff / elapsed, reset


def avg_delta(sum_curr: float | None, sum_prev: float | None, count_curr: float | None, count_prev: float | None) -> tuple[float | None, bool]:
    sum_diff, sum_reset = delta(sum_curr, sum_prev)
    count_diff, count_reset = delta(count_curr, count_prev)
    if sum_diff is None or count_diff is None or count_diff <= 0:
        return None, sum_reset or count_reset
    return sum_diff / count_diff, False


def fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        return f"{value:.6g}"
    return str(value)


def csv_header(endpoint_names: list[str]) -> list[str]:
    header = list(BASE_COLUMNS)
    for name in endpoint_names:
        header.extend(f"{name}_{col}" for col in SERVICE_COLUMNS)
    return header


def current_log_path(log_dir: Path, now: dt.datetime) -> Path:
    return log_dir / f"vllm_perf_{now.date().isoformat()}.csv"


def ensure_log_file(path: Path, header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header_line = ",".join(header)
    if path.exists():
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                first = f.readline().rstrip("\n\r")
            if first == header_line:
                return
            backup = path.with_name(f"{path.stem}.pre-v1.{dt.datetime.now().strftime('%H%M%S')}.csv")
            path.rename(backup)
            print(f"WARN rotated incompatible existing log to {backup}", file=sys.stderr)
        except OSError as exc:
            print(f"WARN could not inspect existing log {path}: {exc}", file=sys.stderr)
    with path.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(header)


def iter_log_files(log_dir: Path) -> list[Path]:
    return sorted(p for p in log_dir.glob("vllm_perf_*.csv") if DATE_RE.match(p.name))


def prune_logs(log_dir: Path, retention_days: int, max_total_bytes: int, current: Path) -> None:
    now = time.time()
    files = iter_log_files(log_dir)
    if retention_days > 0:
        cutoff = now - retention_days * 86400
        for path in files:
            if path == current:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                print(f"WARN could not prune old log {path}: {exc}", file=sys.stderr)
    files = iter_log_files(log_dir)
    total = 0
    sizes: list[tuple[float, Path, int]] = []
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        total += st.st_size
        sizes.append((st.st_mtime, path, st.st_size))
    if max_total_bytes <= 0:
        return
    for _mtime, path, size in sorted(sizes):
        if total <= max_total_bytes:
            break
        if path == current:
            continue
        try:
            path.unlink()
            total -= size
        except OSError as exc:
            print(f"WARN could not prune capped log {path}: {exc}", file=sys.stderr)


def write_allowed(log_dir: Path, current: Path, max_current_bytes: int, max_total_bytes: int, min_free_bytes: int) -> tuple[bool, str]:
    try:
        usage = shutil.disk_usage(log_dir)
        if usage.free < min_free_bytes:
            return False, f"free_below_floor:{usage.free}<{min_free_bytes}"
    except OSError as exc:
        return False, f"disk_usage_error:{short_error(exc)}"
    try:
        if current.exists() and max_current_bytes > 0 and current.stat().st_size > max_current_bytes:
            return False, f"current_file_cap:{current.stat().st_size}>{max_current_bytes}"
    except OSError as exc:
        return False, f"current_stat_error:{short_error(exc)}"
    if max_total_bytes > 0:
        total = 0
        for path in iter_log_files(log_dir):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        if total > max_total_bytes:
            return False, f"total_cap:{total}>{max_total_bytes}"
    return True, ""


@dataclass
class ServiceState:
    last_snapshot: dict[str, float | None] | None = None
    last_time: float | None = None


def service_row(name: str, url: str, state: ServiceState, timeout: float, now_monotonic: float) -> list[str]:
    try:
        snap = metric_snapshot(fetch_metrics(url, timeout))
    except Exception as exc:  # endpoint down should not kill monitor
        return [
            "0", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "0", short_error(exc)
        ]

    prev = state.last_snapshot
    prev_time = state.last_time
    elapsed = (now_monotonic - prev_time) if prev_time is not None else None
    reset = False

    def prev_value(key: str) -> float | None:
        return None if prev is None else prev.get(key)

    def rate_value(key: str) -> float | None:
        nonlocal reset
        if elapsed is None:
            return None
        value, did_reset = rate(snap.get(key), prev_value(key), elapsed)
        reset = reset or did_reset
        return value

    def delta_value(key: str) -> float | None:
        nonlocal reset
        value, did_reset = delta(snap.get(key), prev_value(key))
        reset = reset or did_reset
        return value

    def avg_value(sum_key: str, count_key: str) -> float | None:
        nonlocal reset
        value, did_reset = avg_delta(snap.get(sum_key), prev_value(sum_key), snap.get(count_key), prev_value(count_key))
        reset = reset or did_reset
        return value

    prefix_queries_delta = delta_value("prefix_queries")
    prefix_hits_delta = delta_value("prefix_hits")
    prefix_hit_rate = None
    if prefix_queries_delta and prefix_queries_delta > 0 and prefix_hits_delta is not None:
        prefix_hit_rate = prefix_hits_delta / prefix_queries_delta

    spec_draft_rate = rate_value("spec_draft")
    spec_accepted_rate = rate_value("spec_accepted")
    spec_draft_delta = delta_value("spec_draft")
    spec_accepted_delta = delta_value("spec_accepted")
    spec_accept_rate = None
    if spec_draft_delta and spec_draft_delta > 0 and spec_accepted_delta is not None:
        spec_accept_rate = spec_accepted_delta / spec_draft_delta

    row = [
        "1",
        fmt(elapsed),
        fmt(snap.get("running")),
        fmt(snap.get("waiting")),
        fmt(snap.get("swapped")),
        fmt(snap.get("kv_cache_pct")),
        fmt(rate_value("prompt_tokens")),
        fmt(rate_value("generation_tokens")),
        fmt(rate_value("requests")),
        fmt(delta_value("request_errors")),
        fmt(avg_value("ttft_sum", "ttft_count")),
        fmt(avg_value("tpot_sum", "tpot_count")),
        fmt(avg_value("itl_sum", "itl_count")),
        fmt(avg_value("e2e_sum", "e2e_count")),
        fmt(delta_value("preemptions")),
        fmt(prefix_hit_rate),
        fmt(spec_draft_rate),
        fmt(spec_accepted_rate),
        fmt(spec_accept_rate),
        "1" if reset else "0",
        "",
    ]
    state.last_snapshot = snap
    state.last_time = now_monotonic
    return row


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    interval = max(5.0, env_float("VLLM_PERF_INTERVAL_SECONDS", 30.0))
    timeout = max(0.5, env_float("VLLM_PERF_TIMEOUT_SECONDS", 2.5))
    retention_days = max(1, env_int("VLLM_PERF_RETENTION_DAYS", 14))
    max_total_bytes = max(1, env_int("VLLM_PERF_MAX_TOTAL_BYTES", 5 * 1024 * 1024 * 1024))
    max_current_bytes = max(1, env_int("VLLM_PERF_MAX_CURRENT_BYTES", 32 * 1024 * 1024))
    min_free_bytes = max(0, env_int("VLLM_PERF_MIN_FREE_BYTES", 5 * 1024 * 1024 * 1024))
    log_dir = Path(os.path.expanduser(os.getenv("VLLM_PERF_LOG_DIR", "~/log"))).resolve()
    endpoints = parse_endpoints(os.getenv("VLLM_PERF_ENDPOINTS"))
    header = csv_header([name for name, _url in endpoints])
    states = {name: ServiceState() for name, _url in endpoints}
    last_prune = 0.0
    last_skip_warn = 0.0

    print(
        "vllm_perf_monitor started "
        f"interval={interval}s retention_days={retention_days} "
        f"max_total_bytes={max_total_bytes} max_current_bytes={max_current_bytes} "
        f"min_free_bytes={min_free_bytes} endpoints={','.join(name for name, _ in endpoints)}",
        flush=True,
    )

    while not STOP:
        loop_started = time.monotonic()
        now = dt.datetime.now(dt.timezone.utc).astimezone()
        log_path = current_log_path(log_dir, now)

        if loop_started - last_prune > 3600 or last_prune == 0:
            prune_logs(log_dir, retention_days, max_total_bytes, log_path)
            last_prune = loop_started

        try:
            ensure_log_file(log_path, header)
        except OSError as exc:
            allowed = False
            skip_reason = f"ensure_log_error:{short_error(exc)}"
        else:
            allowed, skip_reason = write_allowed(log_dir, log_path, max_current_bytes, max_total_bytes, min_free_bytes)

        row = [now.isoformat(timespec="seconds"), fmt(interval), "0" if allowed else "1", skip_reason]
        for name, url in endpoints:
            row.extend(service_row(name, url, states[name], timeout, loop_started))

        if allowed:
            try:
                with log_path.open("a", encoding="utf-8", newline="") as f:
                    csv.writer(f).writerow(row)
            except OSError as exc:
                now_mono = time.monotonic()
                if now_mono - last_skip_warn > 3600:
                    print(f"WARN CSV write failed: {short_error(exc)}", file=sys.stderr, flush=True)
                    last_skip_warn = now_mono
        else:
            now_mono = time.monotonic()
            if now_mono - last_skip_warn > 3600:
                print(f"WARN skipping CSV writes: {skip_reason}", file=sys.stderr, flush=True)
                last_skip_warn = now_mono

        sleep_for = interval - (time.monotonic() - loop_started)
        if sleep_for > 0:
            time.sleep(sleep_for)

    print("vllm_perf_monitor stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
