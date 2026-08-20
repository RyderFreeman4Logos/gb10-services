#!/usr/bin/env python3
"""Resumable SGLang text-backend prefill/decode throughput benchmark.

Stdlib only. Builds unique uncached prompts, runs deterministic concurrency
waves, and atomically checkpoints each completed wave for resume/observation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import ExitStack, contextmanager
import errno
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid

DEFAULT_BASE_URL = "http://100.105.4.92:18010/v1"
DEFAULT_MODEL = "abliterated-qwen-latest-27b-nvfp4"
DEFAULT_PROVIDER = "openai"
DEFAULT_MIN_PROMPT_TOKENS = 6000
DEFAULT_MAX_TOKENS = 256
DEFAULT_CONCURRENCIES = [1, 2, 4, 6, 8]
DEFAULT_CLIENT_WORKERS = 8
DEFAULT_REQUEST_TIMEOUT_S = 600.0
DEFAULT_WAVE_TIMEOUT_S = 3600.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_INITIAL_S = 1.0
DEFAULT_BACKOFF_MAX_S = 30.0

FILLER_WORD = "aluminium"


# Only benchmark-client settings are admitted; service/server sections are not
# read or hot-reloaded by this harness.
_ALLOWED_CONFIG = {
    "run": ("provider", "model_id", "endpoint"),
    "execution": ("concurrencies", "min_prompt_tokens", "max_tokens"),
    "resources": (
        "client_workers",
        "request_timeout_s",
        "wave_timeout_s",
        "max_attempts",
        "backoff_initial_s",
        "backoff_max_s",
    ),
}


def _default_config() -> dict:
    return {
        "run": {
            "provider": DEFAULT_PROVIDER,
            "model_id": DEFAULT_MODEL,
            "endpoint": DEFAULT_BASE_URL,
        },
        "execution": {
            "concurrencies": list(DEFAULT_CONCURRENCIES),
            "min_prompt_tokens": DEFAULT_MIN_PROMPT_TOKENS,
            "max_tokens": DEFAULT_MAX_TOKENS,
        },
        "resources": {
            "client_workers": DEFAULT_CLIENT_WORKERS,
            "request_timeout_s": DEFAULT_REQUEST_TIMEOUT_S,
            "wave_timeout_s": DEFAULT_WAVE_TIMEOUT_S,
            "max_attempts": DEFAULT_MAX_ATTEMPTS,
            "backoff_initial_s": DEFAULT_BACKOFF_INITIAL_S,
            "backoff_max_s": DEFAULT_BACKOFF_MAX_S,
        },
    }


def _validate_config(config: dict) -> dict:
    run = config["run"]
    execution = config["execution"]
    resources = config["resources"]
    for key in ("provider", "model_id", "endpoint"):
        if not isinstance(run[key], str) or not run[key]:
            raise ValueError(f"run.{key} must be a non-empty string")
    concurrencies = execution["concurrencies"]
    if (
        not isinstance(concurrencies, list)
        or not concurrencies
        or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in concurrencies)
    ):
        raise ValueError("execution.concurrencies must be a non-empty list of positive integers")
    for key in ("min_prompt_tokens", "max_tokens"):
        if isinstance(execution[key], bool) or not isinstance(execution[key], int) or execution[key] < 1:
            raise ValueError(f"execution.{key} must be a positive integer")
    if isinstance(resources["client_workers"], bool) or not isinstance(resources["client_workers"], int) or resources["client_workers"] < 1:
        raise ValueError("resources.client_workers must be a positive integer")
    if resources["client_workers"] < max(concurrencies):
        raise ValueError(
            "resources.client_workers must be >= requested wave concurrency"
        )
    if isinstance(resources["max_attempts"], bool) or not isinstance(resources["max_attempts"], int) or resources["max_attempts"] < 1:
        raise ValueError("resources.max_attempts must be a positive integer")
    for key in ("request_timeout_s", "wave_timeout_s", "backoff_initial_s", "backoff_max_s"):
        value = resources[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"resources.{key} must be a finite non-negative number")
    if resources["request_timeout_s"] == 0 or resources["wave_timeout_s"] == 0:
        raise ValueError("request and wave timeouts must be greater than zero")
    if resources["backoff_max_s"] < resources["backoff_initial_s"]:
        raise ValueError("resources.backoff_max_s must be >= backoff_initial_s")
    return config


def load_config(path: str | os.PathLike[str] | None) -> dict:
    """Load the adjacent TOML client config, applying safe defaults."""
    config = _default_config()
    if path is not None:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        for section, keys in _ALLOWED_CONFIG.items():
            values = raw.get(section, {})
            if not isinstance(values, dict):
                raise ValueError(f"config section [{section}] must be a table")
            unknown = sorted(set(values) - set(keys))
            if unknown:
                raise ValueError(
                    f"unknown client config key(s) in [{section}]: {', '.join(unknown)}"
                )
            for key in keys:
                if key in values:
                    config[section][key] = values[key]
    return _validate_config(config)


def config_epoch(config: dict) -> str:
    normalized = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    if args.base_url is not None:
        config["run"]["endpoint"] = args.base_url
    if args.model is not None:
        config["run"]["model_id"] = args.model
    if args.min_prompt_tokens is not None:
        config["execution"]["min_prompt_tokens"] = args.min_prompt_tokens
    if args.max_tokens is not None:
        config["execution"]["max_tokens"] = args.max_tokens
    if args.concurrencies is not None:
        config["execution"]["concurrencies"] = [
            int(value) for value in args.concurrencies.split(",") if value.strip()
        ]
    for key in ("client_workers", "max_attempts"):
        value = getattr(args, key)
        if value is not None:
            config["resources"][key] = value
    for key in ("request_timeout_s", "wave_timeout_s", "backoff_initial_s", "backoff_max_s"):
        value = getattr(args, key)
        if value is not None:
            config["resources"][key] = value
    return _validate_config(config)


def resolve_config(path: str | os.PathLike[str] | None, args: argparse.Namespace) -> dict:
    config = load_config(path)
    # An explicit TOML file is the hot-reload source. CLI flags retain the old
    # behavior when no config file is supplied.
    return config if path is not None else _apply_cli_overrides(config, args)


def estimate_tokens(text: str) -> int:
    # Conservative local estimate: whitespace-separated words, capping at
    # 1 token per word (Qwen counts ~1 token/word). Never 4-chars/token.
    return max(1, len(text.split()))


def make_nonce(wave: int, index: int) -> str:
    return f"{wave}-{index}-{uuid.uuid4()}-{time.time_ns()}"


def _grow_to_min_tokens(nonce: str, min_tokens: int) -> str:
    """Build filler once at the required size instead of re-estimating it."""
    prefix = f"{nonce}\n<|startoftext|>\n"
    suffix = f"\nPlease echo this nonce at the very end: {nonce}\n"
    # prefix/suffix contain a bounded number of words, so this arithmetic is
    # O(n) in the output size and never copies an expanding whole prompt.
    fixed_tokens = estimate_tokens(prefix + suffix)
    filler_count = max(16, min_tokens - fixed_tokens)
    body = (FILLER_WORD + " ") * filler_count
    prompt = prefix + body + suffix
    # The estimate is intentionally conservative; account for its exact fixed
    # word count without a second whole-string scan.
    shortfall = min_tokens - (fixed_tokens + filler_count)
    if shortfall > 0:
        body += (FILLER_WORD + " ") * shortfall
        prompt = prefix + body + suffix
    return prompt


def build_prompt(nonce: str, min_tokens: int) -> str:
    return _grow_to_min_tokens(nonce, min_tokens)


def build_payload(model: str, nonce: str, min_tokens: int, max_tokens: int) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(nonce, min_tokens)}],
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _stream_chat(
    base_url: str,
    payload: dict,
    min_tokens: int,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
) -> dict:
    """POST a streaming chat request, returning SSE usage and throughput."""
    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    ttft = None
    gen_ts = 0.0
    last_ts = start
    finish_reason = None
    usage = None
    with urllib.request.urlopen(req, timeout=request_timeout_s) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            now = time.monotonic()
            if usage is None and chunk.get("usage"):
                usage = chunk["usage"]
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                if ttft is None:
                    ttft = now - start
                else:
                    gen_ts += now - last_ts
                last_ts = now
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
            elif choice.get("finish_reason") and usage is not None:
                finish_reason = choice["finish_reason"]
    wall = time.monotonic() - start
    prompt_tokens = (usage or {}).get("prompt_tokens", 0)
    completion_tokens = (usage or {}).get("completion_tokens", 0)
    if ttft is None:
        ttft = wall
        gen_ts = 0.0
    decode_tok_s = completion_tokens / (wall - ttft) if (wall - ttft) > 0 else 0.0
    return {
        "ttft_s": ttft,
        "wall_s": wall,
        "gen_s": gen_ts,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "decode_tok_s": decode_tok_s,
        "finish_reason": finish_reason,
        "n_fail_short": 1 if (prompt_tokens and prompt_tokens < min_tokens) else 0,
    }


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    return isinstance(
        exc,
        (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, ConnectionError),
    )


def _failure_metric(
    exc: BaseException,
    attempts: int,
    retry_exhausted: bool,
    *,
    logical_wall_s: float = 0.0,
    retry_backoff_s: float = 0.0,
    failed_attempt_wall_s: float = 0.0,
) -> dict:
    status = getattr(exc, "code", None)
    retry_overhead_s = failed_attempt_wall_s + retry_backoff_s
    return {
        "ok": False,
        "attempts": attempts,
        "retry_exhausted": retry_exhausted,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "error_status": status if isinstance(status, int) else None,
        "ttft_s": 0.0,
        "wall_s": 0.0,
        "final_attempt_wall_s": 0.0,
        "logical_wall_s": logical_wall_s,
        "retry_overhead_s": retry_overhead_s,
        "retry_backoff_s": retry_backoff_s,
        "failed_attempt_wall_s": failed_attempt_wall_s,
        "gen_s": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "decode_tok_s": 0.0,
        "finish_reason": None,
        "n_fail_short": 0,
    }


def run_request(
    base_url: str,
    payload: dict,
    min_tokens: int,
    *,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S,
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
) -> dict:
    """Run one request with bounded retries for transient failures only."""
    retry_backoff_s = 0.0
    failed_attempt_wall_s = 0.0
    for attempt in range(1, max_attempts + 1):
        attempt_start = time.monotonic()
        try:
            metric = dict(_stream_chat(base_url, payload, min_tokens, request_timeout_s))
            attempt_wall_s = max(0.0, time.monotonic() - attempt_start)
            final_attempt_wall_s = attempt_wall_s or float(metric.get("wall_s", 0.0))
            metric["attempts"] = attempt
            metric["retry_exhausted"] = False
            metric["ok"] = False if metric["n_fail_short"] else True
            metric["final_attempt_wall_s"] = final_attempt_wall_s
            metric["logical_wall_s"] = failed_attempt_wall_s + retry_backoff_s + final_attempt_wall_s
            metric["retry_backoff_s"] = retry_backoff_s
            metric["failed_attempt_wall_s"] = failed_attempt_wall_s
            metric["retry_overhead_s"] = failed_attempt_wall_s + retry_backoff_s
            return metric
        except Exception as exc:  # network/HTTP errors are classified below
            failed_attempt_wall_s += max(0.0, time.monotonic() - attempt_start)
            transient = _is_transient(exc)
            if not transient or attempt >= max_attempts:
                return _failure_metric(
                    exc,
                    attempt,
                    transient and attempt >= max_attempts,
                    logical_wall_s=failed_attempt_wall_s + retry_backoff_s,
                    retry_backoff_s=retry_backoff_s,
                    failed_attempt_wall_s=failed_attempt_wall_s,
                )
            delay = min(backoff_max_s, backoff_initial_s * (2 ** (attempt - 1)))
            retry_backoff_s += delay
            if delay > 0:
                time.sleep(delay)
    raise AssertionError("unreachable retry loop")


def run_wave(
    base_url: str,
    model: str,
    wave: int,
    n: int,
    min_tokens: int,
    max_tokens: int,
    *,
    provider: str = DEFAULT_PROVIDER,
    client_workers: int | None = None,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    wave_timeout_s: float = DEFAULT_WAVE_TIMEOUT_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S,
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
) -> dict:
    """Run a wave and join workers before returning its complete metrics.

    ``wave_timeout_s`` is a non-hard observational deadline. Python cannot
    force-stop a running ``ThreadPoolExecutor`` worker, so the deadline only
    records that the wave exceeded its observation window; each request still
    uses its own timeout and retry policy, and the wave waits for those workers.
    """
    del provider  # retained in the wave/report metadata; endpoint is the transport.
    payloads = [
        build_payload(model, make_nonce(wave, i), min_tokens, max_tokens)
        for i in range(n)
    ]
    wave_start = time.monotonic()
    workers = max(1, min(n, client_workers or n))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = [
        executor.submit(
            run_request,
            base_url,
            payload,
            min_tokens,
            request_timeout_s=request_timeout_s,
            max_attempts=max_attempts,
            backoff_initial_s=backoff_initial_s,
            backoff_max_s=backoff_max_s,
        )
        for payload in payloads
    ]
    observation_deadline_exceeded = False
    try:
        _done, pending = concurrent.futures.wait(futures, timeout=wave_timeout_s)
        observation_deadline_exceeded = bool(pending)
        # A running thread cannot be cancelled safely. Waiting here preserves
        # complete per-request metrics and lets request_timeout_s bound I/O.
        results = [future.result() for future in futures]
    finally:
        executor.shutdown(wait=True)
    wave_wall = time.monotonic() - wave_start
    ok = [result for result in results if result["ok"]]
    n_ok = len(ok)
    n_fail = len(results) - n_ok
    sum_completion = sum(result["completion_tokens"] for result in ok)
    sum_prompt = sum(result["prompt_tokens"] for result in ok)
    return {
        "concurrency": n,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_finish_length": sum(result.get("finish_reason") == "length" for result in results),
        "sum_completion": sum_completion,
        "wave_wall_s": wave_wall,
        "wave_observation_deadline_exceeded": observation_deadline_exceeded,
        "agg_decode_tok_s": sum_completion / wave_wall if wave_wall > 0 else 0.0,
        "agg_prompt_tok_s": sum_prompt / wave_wall if wave_wall > 0 else 0.0,
        "requests": results,
    }


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _artifact_path_parts(path: str | os.PathLike[str]) -> tuple[int, list[str]]:
    raw = os.fspath(path)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not raw or "\x00" in raw:
        raise RuntimeError(f"refusing invalid artifact path: {path!r}")
    parsed = Path(raw)
    parts = list(parsed.parts)
    if parsed.is_absolute():
        anchor = parts.pop(0)
        base_fd = os.open(anchor, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    else:
        base_fd = os.open(".", os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    if not parts or parts[-1] in (".", ".."):
        os.close(base_fd)
        raise RuntimeError(f"refusing invalid artifact path: {path!r}")
    return base_fd, parts


def _open_artifact_parent(
    path: str | os.PathLike[str], *, create: bool = True
) -> tuple[int, str]:
    """Open the final artifact parent without following any path symlink."""
    directory_fd, parts = _artifact_path_parts(path)
    try:
        for component in parts[:-1]:
            if component == ".":
                continue
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(
                    component,
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    try:
                        component_stat = os.stat(
                            component, dir_fd=directory_fd, follow_symlinks=False
                        )
                    except OSError:
                        component_stat = None
                    if component_stat is not None and stat.S_ISLNK(component_stat.st_mode):
                        raise RuntimeError(
                            f"refusing symlink artifact parent component: {component}"
                        ) from exc
                raise
            os.close(directory_fd)
            directory_fd = child_fd

        target_name = parts[-1]
        try:
            target_stat = os.stat(
                target_name, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(target_stat.st_mode):
                raise RuntimeError(f"refusing symlink artifact path: {path}")
        return directory_fd, target_name
    except BaseException:
        os.close(directory_fd)
        raise


def _validate_artifact_paths(*paths: str | os.PathLike[str]) -> None:
    for path in paths:
        directory_fd, _target_name = _open_artifact_parent(path)
        os.close(directory_fd)


def _atomic_write(path: str | os.PathLike[str], content: str) -> None:
    directory_fd, target_name = _open_artifact_parent(path)
    temporary_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
    temporary_fd = None
    renamed = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _O_NOFOLLOW
            | _O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as stream:
            temporary_fd = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        renamed = True
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if not renamed:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _atomic_write_json(path: str | os.PathLike[str], value: dict) -> None:
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _yaml_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _progress_yaml(progress: dict) -> str:
    return "".join(f"{key}: {_yaml_scalar(value)}\n" for key, value in progress.items())


def _read_process_start_time(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat.rsplit(")", 1)[1].split()
        return int(fields[19])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError) as exc:
        raise RuntimeError(f"refusing resume: malformed /proc/{pid}/stat") from exc


def _process_identity() -> tuple[int, int | None]:
    pid = os.getpid()
    try:
        return pid, _read_process_start_time(pid)
    except RuntimeError:
        return pid, None


@contextmanager
def _exclusive_run_lock(path: str | os.PathLike[str]):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError(f"refusing run: {lock_path} is already owned") from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _artifact_lock_identities(path: str | os.PathLike[str]) -> set[tuple]:
    directory_fd, target_name = _open_artifact_parent(path)
    try:
        raw = os.fspath(path)
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        resolved = Path(os.path.abspath(raw))
        identities: set[tuple] = {("path", str(resolved))}
        try:
            target_stat = os.stat(
                target_name, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(target_stat.st_mode):
                raise RuntimeError(f"refusing symlink artifact path: {path}")
            identities.add(("inode", int(target_stat.st_dev), int(target_stat.st_ino)))
        return identities
    finally:
        os.close(directory_fd)


def _artifact_lock_path(identity: tuple) -> Path:
    digest = hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return Path(tempfile.gettempdir()) / "aeon-bench-artifact-locks" / f"{digest}.lock"


@contextmanager
def _exclusive_artifact_locks(
    out_path: Path,
    state_path: Path,
    progress_path: Path | None = None,
):
    artifacts = (out_path, state_path)
    if progress_path is not None:
        artifacts += (progress_path,)
    _validate_artifact_paths(*artifacts)
    identities = set()
    owners = {}
    for artifact in artifacts:
        for identity in _artifact_lock_identities(artifact):
            prior = owners.get(identity)
            if prior is not None:
                raise RuntimeError(
                    f"artifact path collision/alias: {prior} and {artifact}"
                )
            owners[identity] = artifact
            identities.add(identity)
    lock_paths = [_artifact_lock_path(identity) for identity in sorted(identities)]
    with ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(_exclusive_run_lock(lock_path))
        yield


def _run_lock_path(state_path: Path) -> Path:
    resolved = state_path.resolve(strict=False)
    return _artifact_lock_path(("path", str(resolved)))


def _validate_checkpoint_owner(state: dict) -> None:
    owner_fields = ("pid", "start_time")
    present = [field in state for field in owner_fields]
    if not all(present):
        raise RuntimeError("refusing resume: malformed checkpoint owner identity")
    pid = state["pid"]
    start_time = state["start_time"]
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid < 1
        or isinstance(start_time, bool)
        or not isinstance(start_time, int)
        or start_time < 1
    ):
        raise RuntimeError("refusing resume: malformed checkpoint owner identity")
    live_start_time = _read_process_start_time(pid)
    if live_start_time is None or live_start_time != start_time:
        return
    current_pid, current_start_time = _process_identity()
    if (pid, start_time) != (current_pid, current_start_time):
        raise RuntimeError("refusing resume: saved benchmark owner is still live")


def _validate_loaded_checkpoint(state: object) -> dict:
    if not isinstance(state, dict):
        raise RuntimeError("refusing resume: malformed checkpoint root")
    required = {
        "schema_version",
        "run_id",
        "started_at",
        "elapsed_s",
        "total",
        "planned_total",
        "planned_wave_ids",
        "completed_waves",
        "waves",
        "config_epochs",
        "pid",
        "start_time",
    }
    allowed = required | {"failure"}
    unknown = sorted(set(state) - allowed)
    if unknown:
        raise RuntimeError(
            "refusing resume: malformed checkpoint unknown field(s): "
            + ", ".join(unknown)
        )
    missing = sorted(required - set(state))
    if missing:
        raise RuntimeError(
            "refusing resume: malformed checkpoint missing " + ", ".join(missing)
        )
    if (
        isinstance(state["schema_version"], bool)
        or not isinstance(state["schema_version"], int)
        or state["schema_version"] != 1
    ):
        raise RuntimeError("refusing resume: unsupported checkpoint schema")
    _validate_checkpoint_owner(state)
    run_id = state["run_id"]
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("refusing resume: malformed checkpoint run identity")
    for field in ("started_at", "elapsed_s"):
        value = state[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise RuntimeError(f"refusing resume: malformed checkpoint {field}")
    for field in ("total", "planned_total"):
        value = state[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"refusing resume: malformed checkpoint {field}")
    total = state["total"]
    planned_total = state["planned_total"]
    if total != planned_total:
        raise RuntimeError("refusing resume: inconsistent checkpoint plan totals")
    planned_ids = state["planned_wave_ids"]
    if planned_ids != list(range(planned_total)):
        raise RuntimeError("refusing resume: malformed checkpoint planned wave IDs")
    completed = state["completed_waves"]
    if (
        not isinstance(completed, list)
        or any(isinstance(wave, bool) or not isinstance(wave, int) for wave in completed)
        or len(set(completed)) != len(completed)
        or any(wave < 0 or wave >= planned_total for wave in completed)
    ):
        raise RuntimeError("refusing resume: malformed checkpoint completed wave IDs")
    waves = state["waves"]
    if not isinstance(waves, list) or len(waves) != planned_total:
        raise RuntimeError("refusing resume: malformed checkpoint wave results")
    completed_set = set(completed)
    required_result_fields = {
        "wave",
        "concurrency",
        "n_ok",
        "n_fail",
        "sum_completion",
        "requests",
    }
    for wave_id, result in enumerate(waves):
        if wave_id not in completed_set:
            if result is not None:
                raise RuntimeError("refusing resume: inconsistent checkpoint wave results")
            continue
        if not isinstance(result, dict) or not required_result_fields.issubset(result):
            raise RuntimeError("refusing resume: malformed checkpoint wave result")
        if (
            isinstance(result["wave"], bool)
            or not isinstance(result["wave"], int)
            or result["wave"] != wave_id
        ):
            raise RuntimeError("refusing resume: inconsistent checkpoint wave result ID")
        concurrency = result["concurrency"]
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise RuntimeError("refusing resume: malformed checkpoint wave concurrency")
        for field in ("n_ok", "n_fail", "sum_completion"):
            value = result[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(f"refusing resume: malformed checkpoint wave {field}")
        if not isinstance(result["requests"], list) or len(result["requests"]) != concurrency:
            raise RuntimeError("refusing resume: malformed checkpoint request results")
        if any(
            not isinstance(request, dict) or not isinstance(request.get("ok"), bool)
            for request in result["requests"]
        ):
            raise RuntimeError("refusing resume: malformed checkpoint request result")
    config_epochs = state["config_epochs"]
    if (
        not isinstance(config_epochs, dict)
        or any(not isinstance(epoch, str) or not isinstance(value, dict)
               for epoch, value in config_epochs.items())
    ):
        raise RuntimeError("refusing resume: malformed checkpoint config epochs")
    if "failure" in state:
        failure = state["failure"]
        if (
            not isinstance(failure, dict)
            or not isinstance(failure.get("wave"), int)
            or isinstance(failure.get("wave"), bool)
            or not isinstance(failure.get("reason"), str)
            or not isinstance(failure.get("result"), dict)
        ):
            raise RuntimeError("refusing resume: malformed checkpoint failure record")
    return state


def _checkpoint(
    state: dict,
    state_path: Path,
    progress_path: Path,
    *,
    elapsed_s: float,
    active_wave: int | None,
    total: int,
    config_epoch_value: str,
) -> None:
    _validate_artifact_paths(state_path, progress_path)
    pid, start_time = _process_identity()
    if start_time is None:
        raise RuntimeError("refusing checkpoint: unavailable owner start time")
    state["elapsed_s"] = elapsed_s
    state["pid"] = pid
    state["start_time"] = start_time
    _atomic_write_json(state_path, state)
    completed = len(state["completed_waves"])
    rate = completed / elapsed_s if elapsed_s > 0 else 0.0
    remaining = max(total - completed, 0)
    eta_s = remaining / rate if rate > 0 else None
    _atomic_write(
        progress_path,
        _progress_yaml(
            {
                "completed": completed,
                "total": total,
                "elapsed_s": round(elapsed_s, 6),
                "rate": rate,
                "eta_s": eta_s,
                "active_wave": active_wave,
                "config_epoch": config_epoch_value,
                "pid": pid,
                "start_time": start_time,
            }
        ),
    )


def _new_state() -> dict:
    return {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "started_at": time.time(),
        "elapsed_s": 0.0,
        "total": 0,
        "planned_total": 0,
        "planned_wave_ids": [],
        "completed_waves": [],
        "waves": [],
        "config_epochs": {},
    }


def _load_state(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        state = json.load(stream)
    return _validate_loaded_checkpoint(state)


def _ensure_state_capacity(
    state: dict,
    total: int,
    concurrencies: list[int] | None = None,
) -> None:
    completed = {int(wave) for wave in state["completed_waves"]}
    planned_total = max(
        int(state.get("planned_total", 0)),
        int(state.get("total", 0)),
        len(state["waves"]),
        max(completed, default=-1) + 1,
    )
    if planned_total == 0:
        planned_total = total
    dropped = [
        wave for wave in range(planned_total)
        if wave not in completed and wave >= total
    ]
    if dropped:
        ids = ", ".join(str(wave) for wave in dropped)
        raise ValueError(f"config reload would drop incomplete planned wave IDs: {ids}")
    if concurrencies is not None:
        for wave in sorted(completed):
            if wave >= len(concurrencies):
                continue
            result = state["waves"][wave] if wave < len(state["waves"]) else None
            expected = result.get("concurrency") if isinstance(result, dict) else None
            if expected is not None and concurrencies[wave] != expected:
                raise ValueError(
                    f"config reload changed completed wave {wave}: "
                    f"expected concurrency {expected}, got {concurrencies[wave]}"
                )
    planned_total = max(planned_total, total)
    state["planned_total"] = planned_total
    state["planned_wave_ids"] = list(range(planned_total))
    if len(state["waves"]) < planned_total:
        state["waves"].extend([None] * (planned_total - len(state["waves"])))
    state["total"] = planned_total


def _next_active_wave(state: dict, total: int, start: int = 0) -> int | None:
    completed = set(state["completed_waves"])
    return next(
        (wave for wave in range(start, total) if wave not in completed),
        None,
    )


def parse_args(argv):
    p = argparse.ArgumentParser(description="6k-input SGLang concurrency throughput harness")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--min-prompt-tokens", type=int, default=DEFAULT_MIN_PROMPT_TOKENS)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument(
        "--concurrencies",
        default=",".join(str(c) for c in DEFAULT_CONCURRENCIES),
        help="comma-separated concurrency levels",
    )
    p.add_argument("--client-workers", type=int, default=None)
    p.add_argument("--request-timeout-s", type=float, default=None)
    p.add_argument(
        "--wave-timeout-s",
        type=float,
        default=None,
        help="observational wave deadline; does not cancel running workers",
    )
    p.add_argument("--max-attempts", type=int, default=None)
    p.add_argument("--backoff-initial-s", type=float, default=None)
    p.add_argument("--backoff-max-s", type=float, default=None)
    p.add_argument("--config", help="adjacent TOML benchmark-client config")
    p.add_argument("--state", help="JSON checkpoint path")
    p.add_argument("--progress", help="YAML progress sidecar path")
    p.add_argument("--resume", action="store_true", help="resume incomplete waves from --state")
    p.add_argument("--out", required=True, help="path for JSON output")
    p.add_argument("--dry-run", action="store_true", help="build prompts only, no HTTP")
    return p.parse_args(argv)


def _wave_acceptance_error(result: object, expected_requests: int) -> str | None:
    if not isinstance(result, dict):
        return "wave result is not an object"
    requests = result.get("requests")
    if not isinstance(requests, list) or len(requests) != expected_requests:
        return "wave did not return one result for every request"
    concurrency = result.get("concurrency")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency != expected_requests
    ):
        return "wave result concurrency does not match the plan"
    n_ok = result.get("n_ok")
    n_fail = result.get("n_fail")
    if (
        isinstance(n_ok, bool)
        or not isinstance(n_ok, int)
        or isinstance(n_fail, bool)
        or not isinstance(n_fail, int)
        or n_ok != expected_requests
        or n_fail != 0
    ):
        return "wave contains request failures"
    for index, request in enumerate(requests):
        if not isinstance(request, dict) or request.get("ok") is not True:
            return f"request {index} did not complete successfully"
        if any(
            field in request and request[field] not in (None, "")
            for field in ("error", "error_type", "error_status")
        ):
            return f"request {index} contains an unexpected error"
    return None


def _make_report(
    state: dict,
    config: dict,
    last_epoch: str,
    *,
    status: str,
    complete: bool,
    failure: dict | None = None,
) -> dict:
    waves = [result for result in state["waves"] if result is not None]
    last_executed = waves[-1] if waves else None
    report = {
        "run_id": state["run_id"],
        "base_url": last_executed["endpoint"] if last_executed else config["run"]["endpoint"],
        "endpoint": last_executed["endpoint"] if last_executed else config["run"]["endpoint"],
        "provider": last_executed["provider"] if last_executed else config["run"]["provider"],
        "model": last_executed["model"] if last_executed else config["run"]["model_id"],
        "config_epoch": last_executed["config_epoch"] if last_executed else last_epoch,
        "status": status,
        "complete": complete,
        "waves": waves,
        "config_epochs": state["config_epochs"],
    }
    if failure is not None:
        report["failure"] = failure
    return report


def _run(
    args: argparse.Namespace,
    config_path: Path | None,
    config: dict,
    out_path: Path,
    state_path: Path,
    progress_path: Path,
) -> int:
    if args.resume:
        if not state_path.exists():
            raise FileNotFoundError(f"cannot resume without checkpoint: {state_path}")
        state = _load_state(state_path)
    else:
        state = _new_state()
    _ensure_state_capacity(
        state,
        len(config["execution"]["concurrencies"]),
        config["execution"]["concurrencies"],
    )
    state.pop("failure", None)
    base_elapsed = float(state.get("elapsed_s", 0.0))
    run_start = time.monotonic()
    last_epoch = config_epoch(config)

    # Progress is written before the first wave as well as after every complete
    # wave, so a detached launch is observable before its first response.
    _checkpoint(
        state,
        state_path,
        progress_path,
        elapsed_s=base_elapsed,
        active_wave=_next_active_wave(state, state["total"]),
        total=state["total"],
        config_epoch_value=last_epoch,
    )

    wave = 0
    while True:
        # This is the sole reload point: config changes affect only future waves.
        config = resolve_config(config_path, args)
        last_epoch = config_epoch(config)
        concurrencies = config["execution"]["concurrencies"]
        _ensure_state_capacity(state, len(concurrencies), concurrencies)
        total = state["total"]
        if wave >= total:
            break
        if wave in state["completed_waves"]:
            wave += 1
            _checkpoint(
                state,
                state_path,
                progress_path,
                elapsed_s=base_elapsed + (time.monotonic() - run_start),
                active_wave=_next_active_wave(state, total, wave),
                total=total,
                config_epoch_value=last_epoch,
            )
            continue
        state["config_epochs"][last_epoch] = config
        n = concurrencies[wave]
        resources = config["resources"]
        _checkpoint(
            state,
            state_path,
            progress_path,
            elapsed_s=base_elapsed + (time.monotonic() - run_start),
            active_wave=wave,
            total=total,
            config_epoch_value=last_epoch,
        )
        result = run_wave(
            config["run"]["endpoint"],
            config["run"]["model_id"],
            wave,
            n,
            config["execution"]["min_prompt_tokens"],
            config["execution"]["max_tokens"],
            provider=config["run"]["provider"],
            client_workers=resources["client_workers"],
            request_timeout_s=resources["request_timeout_s"],
            wave_timeout_s=resources["wave_timeout_s"],
            max_attempts=resources["max_attempts"],
            backoff_initial_s=resources["backoff_initial_s"],
            backoff_max_s=resources["backoff_max_s"],
        )
        result.update(
            {
                "wave": wave,
                "config_epoch": last_epoch,
                "provider": config["run"]["provider"],
                "endpoint": config["run"]["endpoint"],
                "model": config["run"]["model_id"],
                "max_tokens": config["execution"]["max_tokens"],
            }
        )
        acceptance_error = _wave_acceptance_error(result, n)
        if acceptance_error is not None:
            failure = {
                "wave": wave,
                "reason": acceptance_error,
                "result": result,
            }
            state["failure"] = failure
            _checkpoint(
                state,
                state_path,
                progress_path,
                elapsed_s=base_elapsed + (time.monotonic() - run_start),
                active_wave=wave,
                total=total,
                config_epoch_value=last_epoch,
            )
            _atomic_write_json(
                out_path,
                _make_report(
                    state,
                    config,
                    last_epoch,
                    status="INCOMPLETE",
                    complete=False,
                    failure=failure,
                ),
            )
            raise RuntimeError(
                f"benchmark incomplete at wave {wave}: {acceptance_error}"
            )
        state["waves"][wave] = result
        state["completed_waves"] = sorted(set(state["completed_waves"]) | {wave})
        next_wave = wave + 1
        elapsed = base_elapsed + (time.monotonic() - run_start)
        _checkpoint(
            state,
            state_path,
            progress_path,
            elapsed_s=elapsed,
            active_wave=_next_active_wave(state, total, next_wave),
            total=total,
            config_epoch_value=last_epoch,
        )
        wave = next_wave

    report = _make_report(
        state,
        config,
        last_epoch,
        status="COMPLETE",
        complete=True,
    )
    _atomic_write_json(out_path, report)
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config) if args.config else None
    config = resolve_config(config_path, args)
    if args.dry_run:
        nonce = make_nonce(0, 0)
        sample = build_prompt(nonce, config["execution"]["min_prompt_tokens"])
        print(
            json.dumps(
                {
                    "concurrencies": config["execution"]["concurrencies"],
                    "model": config["run"]["model_id"],
                    "base_url": config["run"]["endpoint"],
                    "provider": config["run"]["provider"],
                    "min_prompt_tokens": config["execution"]["min_prompt_tokens"],
                    "max_tokens": config["execution"]["max_tokens"],
                    "sample_prompt": sample,
                    "config_epoch": config_epoch(config),
                },
                indent=2,
            )
        )
        return 0

    out_path = Path(args.out)
    state_path = Path(args.state) if args.state else Path(f"{args.out}.state.json")
    progress_path = Path(args.progress) if args.progress else Path(f"{args.out}.progress.yaml")
    with _exclusive_artifact_locks(out_path, state_path, progress_path):
        return _run(args, config_path, config, out_path, state_path, progress_path)


if __name__ == "__main__":
    sys.exit(main())
