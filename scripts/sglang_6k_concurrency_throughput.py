#!/usr/bin/env python3
"""Resumable SGLang text-backend prefill/decode throughput benchmark.

Stdlib only. Builds unique uncached prompts, runs deterministic concurrency
waves, and atomically checkpoints each completed wave for resume/observation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
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


class StreamProtocolError(ConnectionError):
    """The upstream emitted malformed or out-of-order SSE data."""


class StreamTruncatedError(ConnectionError):
    """The upstream ended before a complete SSE result was delivered."""


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
_IGNORED_CONFIG_SECTIONS = {"service", "server"}
_CHECKPOINT_SCHEMA_VERSION = 2
_LEGACY_CHECKPOINT_SCHEMA_VERSION = 1


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
    if not isinstance(config, dict) or set(config) != set(_ALLOWED_CONFIG):
        raise ValueError("config must contain exactly the client sections: run, execution, resources")
    for section, keys in _ALLOWED_CONFIG.items():
        if not isinstance(config[section], dict) or set(config[section]) != set(keys):
            raise ValueError(f"config section [{section}] has an invalid shape")
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
        unknown_sections = sorted(
            set(raw) - set(_ALLOWED_CONFIG) - _IGNORED_CONFIG_SECTIONS
        )
        if unknown_sections:
            raise ValueError(
                "unknown top-level config section(s): " + ", ".join(unknown_sections)
            )
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


def _remaining_deadline(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("request deadline exceeded")
    return remaining


def _set_response_io_timeout(response, timeout: float) -> None:
    candidate = response
    seen = set()
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            setter(timeout)
            return
        candidate = getattr(candidate, "fp", None) or getattr(candidate, "raw", None)
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        setter(timeout)


def _stream_chat(
    base_url: str,
    payload: dict,
    min_tokens: int,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
) -> dict:
    """POST a streaming chat request.

    ``request_timeout_s`` is a monotonic total-attempt deadline covering URL
    open and every SSE read.  The remaining deadline is also installed as the
    socket I/O timeout before each read, so trickle frames cannot extend it.
    """
    if (
        isinstance(request_timeout_s, bool)
        or not isinstance(request_timeout_s, (int, float))
        or not math.isfinite(request_timeout_s)
        or request_timeout_s <= 0
    ):
        raise ValueError("request_timeout_s must be a finite positive number")
    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    deadline = start + request_timeout_s
    ttft = None
    gen_ts = 0.0
    last_ts = start
    finish_reason = None
    usage = None
    saw_usage = False
    pending_usage = None
    saw_content_event = False
    saw_done = False
    with urllib.request.urlopen(req, timeout=_remaining_deadline(deadline)) as resp:
        iterator = iter(resp)
        while True:
            io_timeout = _remaining_deadline(deadline)
            _set_response_io_timeout(resp, io_timeout)
            try:
                raw = next(iterator)
            except StopIteration:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("request deadline exceeded during SSE read")
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise StreamProtocolError("stream contained invalid UTF-8") from exc
            if not line:
                continue
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            if saw_done:
                raise StreamProtocolError("stream contained data after [DONE]")
            if data == "[DONE]":
                if finish_reason is None or not saw_usage:
                    raise StreamProtocolError(
                        "stream [DONE] arrived before terminal usage event"
                    )
                saw_done = True
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as exc:
                raise StreamProtocolError("stream contained invalid JSON") from exc
            if not isinstance(chunk, dict):
                raise StreamProtocolError("stream event was not an object")
            if pending_usage is not None:
                raise StreamProtocolError("stream usage event preceded terminal finish event")
            if saw_usage:
                raise StreamProtocolError("stream contained data after usage event")
            choices = chunk.get("choices")
            has_non_null_usage = "usage" in chunk and chunk["usage"] is not None
            if has_non_null_usage:
                if choices != []:
                    raise StreamProtocolError(
                        "stream usage event must have an empty choices list"
                    )
                candidate_usage = chunk["usage"]
                if not isinstance(candidate_usage, dict):
                    raise StreamProtocolError("stream usage was not an object")
                prompt_tokens = candidate_usage.get("prompt_tokens")
                completion_tokens = candidate_usage.get("completion_tokens")
                if (
                    isinstance(prompt_tokens, bool)
                    or not isinstance(prompt_tokens, int)
                    or prompt_tokens < 0
                    or isinstance(completion_tokens, bool)
                    or not isinstance(completion_tokens, int)
                    or completion_tokens < 0
                ):
                    raise StreamProtocolError("stream usage token fields were invalid")
                usage = candidate_usage
                if finish_reason is None:
                    if not saw_content_event:
                        raise StreamProtocolError(
                            "stream usage event arrived before content"
                        )
                    pending_usage = candidate_usage
                else:
                    saw_usage = True
                continue
            if finish_reason is not None:
                raise StreamProtocolError("stream contained content after terminal event")
            if not isinstance(choices, list) or len(choices) != 1:
                raise StreamProtocolError("stream content event must have one choice")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise StreamProtocolError("stream choice was not an object")
            candidate_finish = choice.get("finish_reason")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise StreamProtocolError("stream delta was not an object")
            saw_content_event = True
            content = delta.get("content")
            now = time.monotonic()
            if content:
                if ttft is None:
                    ttft = now - start
                else:
                    gen_ts += now - last_ts
                last_ts = now
            if candidate_finish is not None:
                if not isinstance(candidate_finish, str) or not candidate_finish:
                    raise StreamProtocolError("stream finish reason was invalid")
                finish_reason = candidate_finish
    if not saw_done:
        raise StreamTruncatedError("stream ended before [DONE]")
    if not saw_usage or not isinstance(usage, dict):
        raise StreamTruncatedError("stream did not provide final usage")
    wall = time.monotonic() - start
    prompt_tokens = usage["prompt_tokens"]
    completion_tokens = usage["completion_tokens"]
    if not isinstance(finish_reason, str) or not finish_reason:
        raise StreamProtocolError("stream did not provide a finish reason")
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
        "n_fail_short": 1 if prompt_tokens < min_tokens else 0,
        "request_timeout_s": request_timeout_s,
    }


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, StreamProtocolError):
        return False
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
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
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
        "request_timeout_s": request_timeout_s,
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
            metric["request_timeout_s"] = request_timeout_s
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
                    request_timeout_s=request_timeout_s,
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
        "work_units": n,
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


def _open_artifact_parent(path: str | os.PathLike[str]) -> tuple[int, str]:
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
                raise
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


@dataclass(frozen=True)
class _ArtifactBinding:
    """A locked parent descriptor and immutable artifact leaf for one run."""

    display_path: str
    parent_fd: int
    leaf: str
    parent_identity: tuple[int, int]

    def __fspath__(self) -> str:
        return self.display_path

    @property
    def key(self) -> tuple[int, int, str]:
        return (*self.parent_identity, self.leaf)


def _binding_for(path: str | os.PathLike[str] | _ArtifactBinding) -> tuple[_ArtifactBinding, bool]:
    if isinstance(path, _ArtifactBinding):
        return path, False
    directory_fd, target_name = _open_artifact_parent(path)
    directory_stat = os.fstat(directory_fd)
    return (
        _ArtifactBinding(
            os.fsdecode(os.fspath(path)),
            directory_fd,
            target_name,
            (int(directory_stat.st_dev), int(directory_stat.st_ino)),
        ),
        True,
    )


def _artifact_exists(path: str | os.PathLike[str] | _ArtifactBinding) -> bool:
    binding, owned = _binding_for(path)
    try:
        try:
            os.stat(binding.leaf, dir_fd=binding.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    finally:
        if owned:
            os.close(binding.parent_fd)


def _validate_artifact_paths(*paths: str | os.PathLike[str] | _ArtifactBinding) -> None:
    bindings = []
    owned = []
    try:
        for path in paths:
            binding, is_owned = _binding_for(path)
            bindings.append(binding)
            if is_owned:
                owned.append(binding.parent_fd)
        keys = [binding.key for binding in bindings]
        if len(keys) != len(set(keys)):
            raise RuntimeError("duplicate artifact binding")
    finally:
        for descriptor in owned:
            os.close(descriptor)


def _atomic_write(path: str | os.PathLike[str] | _ArtifactBinding, content: str) -> None:
    binding, owned = _binding_for(path)
    directory_fd = binding.parent_fd
    target_name = binding.leaf
    temporary_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
    temporary_fd = None
    temporary_identity = None
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
        temporary_stat = os.fstat(temporary_fd)
        temporary_identity = (int(temporary_stat.st_dev), int(temporary_stat.st_ino))
        with os.fdopen(temporary_fd, "w", encoding="utf-8", closefd=False) as stream:
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
        if not renamed and temporary_fd is not None and temporary_identity is not None:
            try:
                current = os.stat(
                    temporary_name, dir_fd=directory_fd, follow_symlinks=False
                )
            except OSError:
                current = None
            if current is not None and (
                int(current.st_dev), int(current.st_ino)
            ) == temporary_identity:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except (FileNotFoundError, FileExistsError):
                    pass
        if temporary_fd is not None:
            os.close(temporary_fd)
        if owned:
            os.close(directory_fd)


def _atomic_write_json(
    path: str | os.PathLike[str] | _ArtifactBinding, value: dict
) -> None:
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_artifact_text(path: str | os.PathLike[str] | _ArtifactBinding) -> str:
    binding, owned = _binding_for(path)
    try:
        descriptor = os.open(
            binding.leaf,
            os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=binding.parent_fd,
        )
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                return stream.read()
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
    finally:
        if owned:
            os.close(binding.parent_fd)


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


_REQUIRED_WAVE_RESULT_FIELDS = {
    "wave",
    "concurrency",
    "work_units",
    "n_ok",
    "n_fail",
    "n_finish_length",
    "sum_completion",
    "wave_wall_s",
    "wave_observation_deadline_exceeded",
    "agg_decode_tok_s",
    "agg_prompt_tok_s",
    "requests",
    "config_epoch",
    "provider",
    "endpoint",
    "model",
    "max_tokens",
}
_REQUIRED_REQUEST_RESULT_FIELDS = {
    "ok",
    "attempts",
    "retry_exhausted",
    "ttft_s",
    "wall_s",
    "final_attempt_wall_s",
    "logical_wall_s",
    "retry_overhead_s",
    "retry_backoff_s",
    "failed_attempt_wall_s",
    "gen_s",
    "prompt_tokens",
    "completion_tokens",
    "decode_tok_s",
    "finish_reason",
    "n_fail_short",
    "request_timeout_s",
}
_REQUEST_FLOAT_FIELDS = (
    "ttft_s",
    "wall_s",
    "final_attempt_wall_s",
    "logical_wall_s",
    "retry_overhead_s",
    "retry_backoff_s",
    "failed_attempt_wall_s",
    "gen_s",
    "decode_tok_s",
    "request_timeout_s",
)
_METRIC_REL_TOL = 1e-6
_METRIC_ABS_TOL = 1e-9


def _metrics_close(actual: float, expected: float) -> bool:
    return math.isclose(
        actual,
        expected,
        rel_tol=_METRIC_REL_TOL,
        abs_tol=_METRIC_ABS_TOL,
    )


def _completed_wave_error(
    result: object,
    expected_requests: int,
    expected_wave: int | None = None,
    config: dict | None = None,
) -> str | None:
    if not isinstance(result, dict):
        return "wave result is not an object"
    missing = sorted(_REQUIRED_WAVE_RESULT_FIELDS - set(result))
    if missing:
        return "wave result is missing " + ", ".join(missing)
    unknown = sorted(set(result) - _REQUIRED_WAVE_RESULT_FIELDS)
    if unknown:
        return "wave result has unknown field(s): " + ", ".join(unknown)
    wave = result["wave"]
    if isinstance(wave, bool) or not isinstance(wave, int) or wave < 0:
        return "wave result wave ID is invalid"
    if expected_wave is not None and wave != expected_wave:
        return "wave result wave ID does not match the plan"
    concurrency = result["concurrency"]
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency != expected_requests
    ):
        return "wave result concurrency does not match the plan"
    if (
        isinstance(result["work_units"], bool)
        or not isinstance(result["work_units"], int)
        or result["work_units"] != expected_requests
    ):
        return "wave result work units do not match concurrency"
    for field in ("n_ok", "n_fail", "n_finish_length", "sum_completion"):
        value = result[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"wave result {field} is invalid"
    if result["n_ok"] != expected_requests or result["n_fail"] != 0:
        return "wave result counts do not match the completed request set"
    if not isinstance(result["wave_observation_deadline_exceeded"], bool):
        return "wave result observation deadline flag is invalid"
    for field in ("wave_wall_s", "agg_decode_tok_s", "agg_prompt_tok_s"):
        value = result[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return f"wave result {field} is invalid"
    if result["wave_wall_s"] <= 0:
        return "wave result wave wall must be positive"
    if (
        not isinstance(result["config_epoch"], str)
        or len(result["config_epoch"]) != 64
        or any(character not in "0123456789abcdef" for character in result["config_epoch"])
        or not isinstance(result["provider"], str)
        or not result["provider"]
        or not isinstance(result["endpoint"], str)
        or not result["endpoint"]
        or not isinstance(result["model"], str)
        or not result["model"]
    ):
        return "wave result provenance is invalid"
    max_tokens = result["max_tokens"]
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        return "wave result max_tokens provenance is invalid"
    if config is not None:
        execution = config["execution"]
        resources = config["resources"]
        wave_index = expected_wave if expected_wave is not None else wave
        expected_plan = execution["concurrencies"]
        if (
            wave_index >= len(expected_plan)
            or concurrency != expected_plan[wave_index]
            or result["config_epoch"] != config_epoch(config)
            or result["provider"] != config["run"]["provider"]
            or result["endpoint"] != config["run"]["endpoint"]
            or result["model"] != config["run"]["model_id"]
            or max_tokens != execution["max_tokens"]
        ):
            return "wave result provenance does not match its config epoch"
    requests = result["requests"]
    if not isinstance(requests, list) or len(requests) != expected_requests:
        return "wave did not return one result for every request"
    completion_total = 0
    prompt_total = 0
    finish_length_total = 0
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            return f"request {index} is not an object"
        missing = sorted(_REQUIRED_REQUEST_RESULT_FIELDS - set(request))
        if missing:
            return f"request {index} is missing " + ", ".join(missing)
        unknown = sorted(set(request) - _REQUIRED_REQUEST_RESULT_FIELDS)
        if unknown:
            return f"request {index} has unknown field(s): " + ", ".join(unknown)
        if request["ok"] is not True or request["retry_exhausted"] is not False:
            return f"request {index} did not complete successfully"
        attempts = request["attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            return f"request {index} attempts is invalid"
        if config is not None:
            resources = config["resources"]
            if attempts > resources["max_attempts"]:
                return f"request {index} attempts exceeds configured max_attempts"
            if request["request_timeout_s"] != resources["request_timeout_s"]:
                return f"request {index} request timeout does not match its config epoch"
        for field in _REQUEST_FLOAT_FIELDS:
            value = request[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                return f"request {index} metric {field} is invalid"
        for field in ("prompt_tokens", "completion_tokens", "n_fail_short"):
            value = request[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return f"request {index} metric {field} is invalid"
        if config is not None:
            execution = config["execution"]
            resources = config["resources"]
            if request["prompt_tokens"] < execution["min_prompt_tokens"]:
                return f"request {index} prompt tokens are below configured minimum"
            expected_backoff = sum(
                min(resources["backoff_max_s"], resources["backoff_initial_s"] * (2 ** retry))
                for retry in range(attempts - 1)
            )
            if not _metrics_close(request["retry_backoff_s"], expected_backoff):
                return f"request {index} retry backoff does not match its metrics"
        if request["ttft_s"] > request["wall_s"] + _METRIC_ABS_TOL:
            return f"request {index} TTFT exceeds wall time"
        if request["gen_s"] > request["wall_s"] - request["ttft_s"] + _METRIC_ABS_TOL:
            return f"request {index} generation time exceeds decode window"
        decode_window = request["wall_s"] - request["ttft_s"]
        expected_decode = (
            request["completion_tokens"] / decode_window if decode_window > 0 else 0.0
        )
        if not _metrics_close(request["decode_tok_s"], expected_decode):
            return f"request {index} decode rate does not match its metrics"
        if not _metrics_close(
            request["retry_overhead_s"],
            request["failed_attempt_wall_s"] + request["retry_backoff_s"],
        ):
            return f"request {index} retry overhead does not match its metrics"
        if not _metrics_close(
            request["logical_wall_s"],
            request["failed_attempt_wall_s"]
            + request["retry_backoff_s"]
            + request["final_attempt_wall_s"],
        ):
            return f"request {index} logical wall time does not match its metrics"
        if request["n_fail_short"] != 0:
            return f"request {index} was marked short"
        if not isinstance(request["finish_reason"], str) or not request["finish_reason"]:
            return f"request {index} finish reason is invalid"
        completion_total += request["completion_tokens"]
        prompt_total += request["prompt_tokens"]
        finish_length_total += request["finish_reason"] == "length"
    if result["sum_completion"] != completion_total:
        return "wave result completion total does not match request metrics"
    wave_wall = result["wave_wall_s"]
    expected_decode = completion_total / wave_wall
    expected_prompt = prompt_total / wave_wall
    if not _metrics_close(result["agg_decode_tok_s"], expected_decode):
        return "wave aggregate decode rate does not match request metrics"
    if not _metrics_close(result["agg_prompt_tok_s"], expected_prompt):
        return "wave aggregate prompt rate does not match request metrics"
    if result["n_finish_length"] != finish_length_total:
        return "wave result finish-length count does not match request metrics"
    return None


@contextmanager
def _exclusive_artifact_locks(
    out_path: Path,
    state_path: Path,
    progress_path: Path | None = None,
):
    artifacts = (out_path, state_path)
    if progress_path is not None:
        artifacts += (progress_path,)
    with ExitStack() as stack:
        pinned = []
        parent_fds = {}
        binding_keys = set()
        target_owners = {}
        for artifact in artifacts:
            directory_fd, target_name = _open_artifact_parent(artifact)
            directory_stat = os.fstat(directory_fd)
            directory_identity = (int(directory_stat.st_dev), int(directory_stat.st_ino))
            if directory_identity in parent_fds:
                os.close(directory_fd)
                directory_fd = parent_fds[directory_identity]
            else:
                parent_fds[directory_identity] = directory_fd
                stack.callback(os.close, directory_fd)
            display_path = os.fsdecode(os.fspath(artifact))
            binding = _ArtifactBinding(
                display_path, directory_fd, target_name, directory_identity
            )
            if binding.key in binding_keys:
                raise RuntimeError(f"duplicate artifact binding: {display_path}")
            binding_keys.add(binding.key)
            pinned.append(binding)
        for directory_identity in sorted(parent_fds):
            descriptor = parent_fds[directory_identity]
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise RuntimeError(
                        "refusing run: artifact parent directory is already owned"
                    ) from exc
                raise
            stack.callback(fcntl.flock, descriptor, fcntl.LOCK_UN)
        for binding in pinned:
            try:
                target_stat = os.stat(
                    binding.leaf, dir_fd=binding.parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(target_stat.st_mode):
                raise RuntimeError(f"refusing symlink artifact path: {binding.display_path}")
            target_identity = (int(target_stat.st_dev), int(target_stat.st_ino))
            prior = target_owners.get(target_identity)
            if prior is not None:
                raise RuntimeError(
                    f"artifact path collision/alias: {prior} and {binding.display_path}"
                )
            target_owners[target_identity] = binding.display_path
        yield tuple(pinned)


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


def _validate_epoch_config(epoch: object, config: object) -> dict:
    if (
        not isinstance(epoch, str)
        or len(epoch) != 64
        or any(character not in "0123456789abcdef" for character in epoch)
    ):
        raise RuntimeError("refusing resume: malformed config epoch key")
    try:
        normalized = _validate_config(json.loads(json.dumps(config)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("refusing resume: malformed stored config epoch shape") from exc
    if config_epoch(normalized) != epoch:
        raise RuntimeError("refusing resume: stored config epoch hash mismatch")
    return normalized


def _migrate_legacy_checkpoint(state: dict, config: dict | None) -> dict:
    """Normalize checkpoints written by the schema-1 producer."""
    legacy_required = {
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
    normalized_fields = {
        "planned_work_units",
        "completed_work_units",
        "active_wave",
        "active_config_epoch",
    }
    unknown = sorted(set(state) - legacy_required - normalized_fields - {"failure"})
    if unknown:
        raise RuntimeError(
            "refusing resume: malformed checkpoint unknown field(s): "
            + ", ".join(unknown)
        )
    missing = sorted(legacy_required - set(state))
    if missing:
        raise RuntimeError(
            "refusing resume: malformed checkpoint missing " + ", ".join(missing)
        )
    total = state["total"]
    planned_total = state["planned_total"]
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or isinstance(planned_total, bool)
        or not isinstance(planned_total, int)
        or planned_total != total
    ):
        raise RuntimeError("refusing resume: inconsistent legacy checkpoint plan totals")
    completed = state["completed_waves"]
    if (
        not isinstance(completed, list)
        or any(isinstance(wave, bool) or not isinstance(wave, int) for wave in completed)
        or len(set(completed)) != len(completed)
        or any(wave < 0 or wave >= planned_total for wave in completed)
    ):
        raise RuntimeError("refusing resume: malformed legacy checkpoint completed wave IDs")
    waves = state["waves"]
    if not isinstance(waves, list) or len(waves) != planned_total:
        raise RuntimeError("refusing resume: malformed legacy checkpoint wave results")
    planned_ids = state["planned_wave_ids"]
    if planned_ids != list(range(planned_total)):
        raise RuntimeError("refusing resume: malformed legacy checkpoint planned wave IDs")
    config_epochs = state["config_epochs"]
    if not isinstance(config_epochs, dict):
        raise RuntimeError("refusing resume: malformed legacy checkpoint config epochs")
    validated_epochs = {}
    for epoch, value in config_epochs.items():
        validated_epochs[epoch] = _validate_epoch_config(epoch, value)
    migrated = dict(state)
    migrated["config_epochs"] = dict(config_epochs)
    completed_set = set(completed)
    completed_work_units = 0
    for wave_id in completed_set:
        result = waves[wave_id]
        if not isinstance(result, dict):
            continue
        concurrency = result.get("concurrency")
        if isinstance(concurrency, int) and not isinstance(concurrency, bool) and concurrency > 0:
            result = dict(result)
            result.setdefault("work_units", concurrency)
            migrated["waves"][wave_id] = result
            completed_work_units += concurrency
        epoch_config = validated_epochs.get(result.get("config_epoch"))
        requests = result.get("requests")
        if epoch_config is not None and isinstance(requests, list):
            for index, request in enumerate(requests):
                if isinstance(request, dict):
                    request = dict(request)
                    request.setdefault(
                        "request_timeout_s",
                        epoch_config["resources"]["request_timeout_s"],
                    )
                    requests[index] = request
    current_config = None
    current_epoch = None
    if config is not None:
        current_config = _validate_config(json.loads(json.dumps(config)))
        current_epoch = config_epoch(current_config)
        if completed_set != set(range(planned_total)):
            migrated["config_epochs"][current_epoch] = current_config
            validated_epochs[current_epoch] = current_config
    planned_work_units = 0
    current_plan = (
        current_config["execution"]["concurrencies"] if current_config is not None else None
    )
    for wave_id in range(planned_total):
        result = migrated["waves"][wave_id]
        if wave_id in completed_set and isinstance(result, dict):
            value = result.get("concurrency")
        elif current_plan is not None and wave_id < len(current_plan):
            value = current_plan[wave_id]
        else:
            candidates = [
                epoch_config["execution"]["concurrencies"][wave_id]
                for epoch_config in validated_epochs.values()
                if wave_id < len(epoch_config["execution"]["concurrencies"])
            ]
            value = candidates[0] if candidates and len(set(candidates)) == 1 else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RuntimeError(
                f"refusing resume: cannot safely migrate legacy work plan for wave {wave_id}"
            )
        planned_work_units += value
    active_wave = next(
        (wave for wave in range(planned_total) if wave not in completed_set),
        None,
    )
    if active_wave is None:
        active_epoch = None
    elif current_epoch is not None:
        active_epoch = current_epoch
    elif len(validated_epochs) == 1:
        active_epoch = next(iter(validated_epochs))
    else:
        raise RuntimeError(
            "refusing resume: cannot safely migrate legacy active config epoch"
        )
    migrated["schema_version"] = _CHECKPOINT_SCHEMA_VERSION
    migrated["planned_work_units"] = planned_work_units
    migrated["completed_work_units"] = completed_work_units
    migrated["active_wave"] = active_wave
    migrated["active_config_epoch"] = active_epoch
    return migrated


def _validate_loaded_checkpoint(state: object, config: dict | None = None) -> dict:
    if not isinstance(state, dict):
        raise RuntimeError("refusing resume: malformed checkpoint root")
    schema_version = state.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise RuntimeError("refusing resume: unsupported checkpoint schema")
    if schema_version == _LEGACY_CHECKPOINT_SCHEMA_VERSION:
        state = _migrate_legacy_checkpoint(state, config)
    required = {
        "schema_version",
        "run_id",
        "started_at",
        "elapsed_s",
        "total",
        "planned_total",
        "planned_wave_ids",
        "planned_work_units",
        "completed_work_units",
        "active_wave",
        "active_config_epoch",
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
        or state["schema_version"] != _CHECKPOINT_SCHEMA_VERSION
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
    for field in ("planned_work_units", "completed_work_units"):
        value = state[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"refusing resume: malformed checkpoint {field}")
    if total > 0 and state["planned_work_units"] < 1:
        raise RuntimeError("refusing resume: malformed checkpoint planned work units")
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
    active_wave = state["active_wave"]
    if active_wave is not None and (
        isinstance(active_wave, bool)
        or not isinstance(active_wave, int)
        or active_wave < 0
        or active_wave >= planned_total
    ):
        raise RuntimeError("refusing resume: malformed checkpoint active wave")
    active_epoch = state["active_config_epoch"]
    if active_wave is None and active_epoch is not None:
        raise RuntimeError("refusing resume: inactive checkpoint has an active config epoch")
    if active_wave is not None and not isinstance(active_epoch, str):
        raise RuntimeError("refusing resume: active wave config epoch is missing")
    if active_wave is not None and active_wave in completed_set:
        raise RuntimeError("refusing resume: active wave is already completed")
    if len(completed_set) == planned_total and active_wave is not None:
        raise RuntimeError("refusing resume: complete checkpoint has an active wave")
    if len(completed_set) < planned_total and active_wave is None:
        raise RuntimeError("refusing resume: incomplete checkpoint has no active wave")
    if len(completed_set) == planned_total and state["completed_work_units"] < 1:
        raise RuntimeError("refusing resume: complete checkpoint has no work units")
    if state["completed_work_units"] > state["planned_work_units"]:
        raise RuntimeError("refusing resume: completed work exceeds planned work")
    for wave_id, result in enumerate(waves):
        if wave_id not in completed_set:
            if result is not None:
                raise RuntimeError("refusing resume: inconsistent checkpoint wave results")
            continue
        expected_requests = result.get("concurrency", -1) if isinstance(result, dict) else -1
        error = _completed_wave_error(result, expected_requests, wave_id)
        if error is not None:
            raise RuntimeError("refusing resume: malformed checkpoint " + error)
    config_epochs = state["config_epochs"]
    if not isinstance(config_epochs, dict):
        raise RuntimeError("refusing resume: malformed checkpoint config epochs")
    validated_epochs = {}
    for epoch, value in config_epochs.items():
        validated_epochs[epoch] = _validate_epoch_config(epoch, value)
    completed_work_units = 0
    for wave_id in completed_set:
        result = waves[wave_id]
        epoch = result["config_epoch"]
        epoch_config = validated_epochs.get(epoch)
        if epoch_config is None:
            raise RuntimeError("refusing resume: completed wave provenance epoch is absent")
        error = _completed_wave_error(
            result, result["concurrency"], wave_id, epoch_config
        )
        if error is not None:
            raise RuntimeError("refusing resume: malformed checkpoint " + error)
        expected_plan = epoch_config["execution"]["concurrencies"]
        if wave_id >= len(expected_plan) or result["concurrency"] != expected_plan[wave_id]:
            raise RuntimeError(
                f"refusing resume: completed wave {wave_id} provenance does not match its config epoch"
            )
        if (
            result["provider"] != epoch_config["run"]["provider"]
            or result["endpoint"] != epoch_config["run"]["endpoint"]
            or result["model"] != epoch_config["run"]["model_id"]
            or result["max_tokens"] != epoch_config["execution"]["max_tokens"]
        ):
            raise RuntimeError(
                f"refusing resume: completed wave {wave_id} provenance fields do not match its config epoch"
            )
        completed_work_units += result["work_units"]
    if completed_work_units != state["completed_work_units"]:
        raise RuntimeError("refusing resume: completed work units do not match wave results")
    if active_wave is not None and active_epoch not in validated_epochs:
        raise RuntimeError("refusing resume: active wave config epoch is absent")
    if active_wave is not None:
        active_plan = validated_epochs[active_epoch]["execution"]["concurrencies"]
        if active_wave >= len(active_plan):
            raise RuntimeError("refusing resume: active wave provenance is outside its config plan")
    if "failure" in state:
        failure = state["failure"]
        if (
            not isinstance(failure, dict)
            or not isinstance(failure.get("wave"), int)
            or isinstance(failure.get("wave"), bool)
            or not isinstance(failure.get("stage"), str)
            or not failure.get("stage")
            or not isinstance(failure.get("reason"), str)
            or not failure.get("reason")
            or not isinstance(failure.get("error_type"), str)
            or not isinstance(failure.get("error"), str)
            or ("result" in failure and not isinstance(failure["result"], dict))
        ):
            raise RuntimeError("refusing resume: malformed checkpoint failure record")
    return state


def _checkpoint(
    state: dict,
    state_path: Path | _ArtifactBinding,
    progress_path: Path | _ArtifactBinding,
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
    completed_waves = set(state["completed_waves"])
    completed_work_units = sum(
        state["waves"][wave]["work_units"]
        for wave in completed_waves
        if isinstance(state["waves"][wave], dict)
    )
    state["completed_work_units"] = completed_work_units
    state["active_wave"] = active_wave
    state["active_config_epoch"] = config_epoch_value if active_wave is not None else None
    state["elapsed_s"] = elapsed_s
    state["pid"] = pid
    state["start_time"] = start_time
    _atomic_write_json(state_path, state)
    planned_work_units = state["planned_work_units"]
    work_rate = completed_work_units / elapsed_s if elapsed_s > 0 else 0.0
    remaining_work_units = max(planned_work_units - completed_work_units, 0)
    eta_s = remaining_work_units / work_rate if work_rate > 0 else None
    active_work_unit = None
    if active_wave is not None:
        epoch_config = state["config_epochs"].get(config_epoch_value)
        if isinstance(epoch_config, dict):
            plan = epoch_config.get("execution", {}).get("concurrencies")
            if isinstance(plan, list) and active_wave < len(plan):
                active_work_unit = plan[active_wave]
    progress = {
        "completed": completed_work_units,
        "total": planned_work_units,
        "completed_waves": len(completed_waves),
        "total_waves": total,
        "completed_work_units": completed_work_units,
        "total_work_units": planned_work_units,
        "elapsed_s": round(elapsed_s, 6),
        "rate": work_rate,
        "work_rate": work_rate,
        "eta_s": eta_s,
        "active_wave": active_wave,
        "work_unit": active_work_unit,
        "config_epoch": config_epoch_value,
        "pid": pid,
        "start_time": start_time,
    }
    failure = state.get("failure")
    if isinstance(failure, dict):
        progress["failure_wave"] = failure.get("wave")
        progress["failure_stage"] = failure.get("stage")
        progress["failure_reason"] = failure.get("reason")
    _atomic_write(progress_path, _progress_yaml(progress))


def _new_state() -> dict:
    return {
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "started_at": time.time(),
        "elapsed_s": 0.0,
        "total": 0,
        "planned_total": 0,
        "planned_work_units": 0,
        "completed_work_units": 0,
        "active_wave": None,
        "active_config_epoch": None,
        "planned_wave_ids": [],
        "completed_waves": [],
        "waves": [],
        "config_epochs": {},
    }


def _load_state(path: Path | _ArtifactBinding, config: dict | None = None) -> dict:
    state = json.loads(_read_artifact_text(path))
    return _validate_loaded_checkpoint(state, config)


def _validate_loaded_plan(state: dict, concurrencies: list[int]) -> None:
    for wave in state["completed_waves"]:
        result = state["waves"][wave]
        if wave < len(concurrencies):
            expected = concurrencies[wave]
        else:
            epoch_config = state["config_epochs"].get(result["config_epoch"])
            historical = epoch_config.get("execution") if isinstance(epoch_config, dict) else None
            expected_plan = historical.get("concurrencies") if isinstance(historical, dict) else None
            if not isinstance(expected_plan, list) or wave >= len(expected_plan):
                raise RuntimeError(
                    f"refusing resume: no configured concurrency for completed wave {wave}"
                )
            expected = expected_plan[wave]
        if result["concurrency"] != expected:
            raise RuntimeError(
                f"refusing resume: completed wave {wave} concurrency does not match the plan"
            )


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
        for wave in range(planned_total):
            if wave in completed:
                if wave >= len(concurrencies):
                    continue
                result = state["waves"][wave] if wave < len(state["waves"]) else None
                expected = result.get("concurrency") if isinstance(result, dict) else None
                if expected is not None and concurrencies[wave] != expected:
                    raise ValueError(
                        f"config reload changed completed wave {wave}: "
                        f"expected concurrency {expected}, got {concurrencies[wave]}"
                    )
            elif wave >= len(concurrencies):
                raise ValueError(
                    f"config reload has no concurrency for incomplete wave {wave}"
                )
    planned_total = max(planned_total, total)
    state["planned_total"] = planned_total
    state["planned_wave_ids"] = list(range(planned_total))
    if len(state["waves"]) < planned_total:
        state["waves"].extend([None] * (planned_total - len(state["waves"])))
    state["total"] = planned_total
    if concurrencies is not None:
        planned_work_units = 0
        for wave in range(planned_total):
            result = state["waves"][wave]
            if wave in completed and isinstance(result, dict):
                planned_work_units += result["concurrency"]
            elif wave < len(concurrencies):
                planned_work_units += concurrencies[wave]
        state["planned_work_units"] = planned_work_units


def _next_active_wave(state: dict, total: int, start: int = 0) -> int | None:
    completed = set(state["completed_waves"])
    return next(
        (wave for wave in range(start, total) if wave not in completed),
        None,
    )


def backend_identity_preflight(config: dict) -> dict:
    """Fail closed unless the configured raw backend offers the required alias."""
    endpoint = config["run"]["endpoint"].rstrip("/") + "/models"
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(
            request, timeout=config["resources"]["request_timeout_s"]
        ) as response:
            status = response.getcode()
            body = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(f"backend identity preflight failed for {endpoint}: {exc}") from exc
    if status != 200:
        raise RuntimeError(
            f"backend identity preflight failed for {endpoint}: HTTP {status}"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"backend identity preflight returned invalid JSON: {endpoint}") from exc
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list) or any(
        not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]
        for model in models
    ):
        raise RuntimeError(f"backend identity preflight returned malformed model data: {endpoint}")
    offered = [model["id"] for model in models]
    required = config["run"]["model_id"]
    if required not in offered:
        raise RuntimeError(
            f"backend identity preflight rejected required alias {required!r}; "
            f"offered aliases: {', '.join(offered) or '<none>'}"
        )
    return {"status": "PASS", "endpoint": endpoint, "required_model": required, "offered_models": offered}


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
    p.add_argument("--out", help="path for JSON output")
    p.add_argument("--dry-run", action="store_true", help="build prompts only, no HTTP")
    p.add_argument(
        "--preflight",
        action="store_true",
        help="verify the configured raw backend model alias, then exit",
    )
    return p.parse_args(argv)


def _wave_acceptance_error(
    result: object, expected_requests: int, config: dict | None = None
) -> str | None:
    return _completed_wave_error(result, expected_requests, config=config)


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


def _prepare_fatal_state(
    state: dict,
    config: dict,
    failure: dict,
    *,
    active_wave: int,
    total: int,
    config_epoch_value: str,
    elapsed_s: float,
) -> dict:
    """Build truthful failure artifacts without invoking the checkpoint path."""
    state["failure"] = failure
    state["elapsed_s"] = max(0.0, elapsed_s)
    completed = set(state.get("completed_waves", []))
    total = max(total, int(state.get("total", 0)), len(state.get("waves", [])))
    state["total"] = total
    state["planned_total"] = total
    state["planned_wave_ids"] = list(range(total))
    state.setdefault("waves", []).extend(
        [None] * max(0, total - len(state.get("waves", [])))
    )
    state.setdefault("config_epochs", {})[config_epoch_value] = config
    state["active_wave"] = (
        active_wave if 0 <= active_wave < total and active_wave not in completed else None
    )
    state["active_config_epoch"] = (
        config_epoch_value if state["active_wave"] is not None else None
    )
    completed_work_units = 0
    planned_work_units = 0
    plan = config["execution"]["concurrencies"]
    for wave, result in enumerate(state["waves"][:total]):
        if wave in completed and isinstance(result, dict):
            completed_work_units += result.get("work_units", result.get("concurrency", 0))
            planned_work_units += result.get("work_units", result.get("concurrency", 0))
        elif wave < len(plan):
            planned_work_units += plan[wave]
    state["completed_work_units"] = completed_work_units
    state["planned_work_units"] = max(planned_work_units, completed_work_units)
    pid, start_time = _process_identity()
    state["pid"] = pid
    if start_time is not None:
        state["start_time"] = start_time
    work_rate = completed_work_units / elapsed_s if elapsed_s > 0 else 0.0
    return {
        "completed": completed_work_units,
        "total": state["planned_work_units"],
        "completed_waves": len(completed),
        "total_waves": total,
        "completed_work_units": completed_work_units,
        "total_work_units": state["planned_work_units"],
        "elapsed_s": round(max(0.0, elapsed_s), 6),
        "rate": work_rate,
        "work_rate": work_rate,
        "eta_s": (
            (state["planned_work_units"] - completed_work_units) / work_rate
            if work_rate > 0
            else None
        ),
        "active_wave": state["active_wave"],
        "work_unit": (
            plan[state["active_wave"]]
            if state["active_wave"] is not None and state["active_wave"] < len(plan)
            else None
        ),
        "config_epoch": config_epoch_value,
        "pid": pid,
        "start_time": state.get("start_time"),
        "failure_wave": failure["wave"],
        "failure_stage": failure["stage"],
        "failure_reason": failure["reason"],
    }


def _add_publication_note(original_error: BaseException, message: str) -> None:
    try:
        original_error.add_note(message)
    except BaseException:
        pass


def _invalidate_artifact(path: Path | _ArtifactBinding) -> None:
    """Remove an unreplaceable artifact from the locked operator directory."""
    binding, owned = _binding_for(path)
    try:
        try:
            os.unlink(binding.leaf, dir_fd=binding.parent_fd)
        except FileNotFoundError:
            return
        os.fsync(binding.parent_fd)
    finally:
        if owned:
            os.close(binding.parent_fd)


def _invalidate_prior_receipts(
    state: dict,
    *,
    resume: bool,
    out_path: Path | _ArtifactBinding,
    state_path: Path | _ArtifactBinding,
    progress_path: Path | _ArtifactBinding,
) -> None:
    """Clear receipts that could be mistaken for this run's terminal result."""
    _invalidate_artifact(out_path)
    if not resume or state.get("active_wave") is None:
        _invalidate_artifact(state_path)
    _invalidate_artifact(progress_path)


def _publish_incomplete_artifact(
    path: Path | _ArtifactBinding,
    content: str,
    label: str,
    original_error: BaseException,
) -> bool:
    """Publish once, retry once on the same artifact, then fail closed."""
    failures = []
    for attempt in (1, 2):
        try:
            _atomic_write(path, content)
            return True
        except BaseException as exc:
            failures.append(exc)
            _add_publication_note(
                original_error,
                f"INCOMPLETE {label} publication attempt {attempt} failed: {exc}",
            )
    try:
        _invalidate_artifact(path)
    except BaseException as exc:
        _add_publication_note(
            original_error,
            f"INCOMPLETE {label} invalidation failed after publication errors: {exc}",
        )
    else:
        _add_publication_note(
            original_error,
            f"INCOMPLETE {label} invalidated after {len(failures)} publication failures",
        )
    return False


def _publish_fatal_failure(
    state: dict,
    config: dict,
    out_path: Path | _ArtifactBinding,
    state_path: Path | _ArtifactBinding,
    progress_path: Path | _ArtifactBinding,
    *,
    runtime: dict,
    original_error: BaseException,
) -> None:
    """Best-effort failure publication that cannot recurse through _checkpoint."""
    error_text = str(original_error) or type(original_error).__name__
    failure = {
        "wave": int(runtime.get("wave", 0)),
        "stage": str(runtime.get("stage", "unknown")),
        "reason": error_text,
        "error_type": type(original_error).__name__,
        "error": error_text,
    }
    result = runtime.get("result")
    if isinstance(result, dict):
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            pass
        else:
            failure["result"] = result
    elapsed_s = float(runtime.get("elapsed_s", state.get("elapsed_s", 0.0)))
    progress = _prepare_fatal_state(
        state,
        config,
        failure,
        active_wave=failure["wave"],
        total=int(runtime.get("total", state.get("total", 0))),
        config_epoch_value=str(runtime.get("config_epoch", config_epoch(config))),
        elapsed_s=elapsed_s,
    )
    _publish_incomplete_artifact(
        state_path,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        "checkpoint",
        original_error,
    )
    _publish_incomplete_artifact(
        progress_path,
        _progress_yaml(progress),
        "progress",
        original_error,
    )
    try:
        report = _make_report(
            state,
            config,
            str(runtime.get("config_epoch", config_epoch(config))),
            status="INCOMPLETE",
            complete=False,
            failure=failure,
        )
        report_content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    except BaseException as exc:
        _add_publication_note(original_error, f"INCOMPLETE report construction failed: {exc}")
        try:
            _invalidate_artifact(out_path)
        except BaseException as invalidate_error:
            _add_publication_note(
                original_error,
                f"INCOMPLETE report invalidation failed after construction error: {invalidate_error}",
            )
    else:
        _publish_incomplete_artifact(
            out_path,
            report_content,
            "report",
            original_error,
        )


def _run(
    args: argparse.Namespace,
    config_path: Path | None,
    config: dict,
    out_path: Path | _ArtifactBinding,
    state_path: Path | _ArtifactBinding,
    progress_path: Path | _ArtifactBinding,
) -> int:
    runtime = {
        "state": None,
        "config": config,
        "config_epoch": config_epoch(config),
        "total": len(config["execution"]["concurrencies"]),
        "wave": 0,
        "stage": "state_initialization",
        "elapsed_s": 0.0,
        "initialized": False,
    }
    try:
        return _run_inner(
            args,
            config_path,
            config,
            out_path,
            state_path,
            progress_path,
            runtime,
        )
    except BaseException as exc:
        state = runtime.get("state")
        if state is not None and runtime.get("initialized", False):
            try:
                _publish_fatal_failure(
                    state,
                    runtime.get("config", config),
                    out_path,
                    state_path,
                    progress_path,
                    runtime=runtime,
                    original_error=exc,
                )
            except BaseException as publication_error:
                try:
                    exc.add_note(f"INCOMPLETE failure-fence publication failed: {publication_error}")
                except BaseException:
                    pass
                for label, artifact in (
                    ("report", out_path),
                    ("checkpoint", state_path),
                    ("progress", progress_path),
                ):
                    try:
                        _invalidate_artifact(artifact)
                    except BaseException as invalidate_error:
                        try:
                            exc.add_note(
                                f"INCOMPLETE {label} fallback invalidation failed: "
                                f"{invalidate_error}"
                            )
                        except BaseException:
                            pass
        raise


def _run_inner(
    args: argparse.Namespace,
    config_path: Path | None,
    config: dict,
    out_path: Path | _ArtifactBinding,
    state_path: Path | _ArtifactBinding,
    progress_path: Path | _ArtifactBinding,
    runtime: dict,
) -> int:
    runtime["config"] = config
    runtime["config_epoch"] = config_epoch(config)
    runtime["total"] = len(config["execution"]["concurrencies"])
    runtime["stage"] = "state_initialization"
    legacy_checkpoint = False
    if args.resume:
        if not _artifact_exists(state_path):
            raise FileNotFoundError(f"cannot resume without checkpoint: {state_path}")
        raw_state = json.loads(_read_artifact_text(state_path))
        legacy_checkpoint = raw_state.get("schema_version") == _LEGACY_CHECKPOINT_SCHEMA_VERSION
        state = _load_state(state_path, config)
        runtime["state"] = state
        active_wave = state["active_wave"]
        current_epoch = config_epoch(config)
        if active_wave is not None and state["active_config_epoch"] != current_epoch:
            raise RuntimeError(
                "refusing resume: active wave config epoch does not match current config"
            )
        _validate_loaded_plan(state, config["execution"]["concurrencies"])
    else:
        state = _new_state()
        runtime["state"] = state
    _ensure_state_capacity(
        state,
        len(config["execution"]["concurrencies"]),
        config["execution"]["concurrencies"],
    )
    runtime["initialized"] = True
    had_failure = "failure" in state
    state.pop("failure", None)
    _invalidate_prior_receipts(
        state,
        resume=args.resume,
        out_path=out_path,
        state_path=state_path,
        progress_path=progress_path,
    )
    base_elapsed = float(state.get("elapsed_s", 0.0))
    run_start = time.monotonic()
    last_epoch = config_epoch(config)
    runtime["total"] = state["total"]
    active = _next_active_wave(state, state["total"])
    runtime["wave"] = active if active is not None else max(state["total"] - 1, 0)
    runtime["config"] = config
    runtime["config_epoch"] = last_epoch
    preflighted_epochs = set()
    runtime["stage"] = "backend_preflight"
    backend_identity_preflight(config)
    preflighted_epochs.add(last_epoch)

    # Persist the active wave and its exact config epoch before any request.
    if active is not None:
        runtime["stage"] = "checkpoint_transition"
        runtime["elapsed_s"] = base_elapsed
        state["config_epochs"][last_epoch] = config
        _checkpoint(
            state,
            state_path,
            progress_path,
            elapsed_s=base_elapsed,
            active_wave=active,
            total=state["total"],
            config_epoch_value=last_epoch,
        )
    elif args.resume or legacy_checkpoint or had_failure:
        # Complete legacy checkpoints and terminal failure checkpoints both need
        # schema-2 state and progress persisted before the terminal report is
        # published.  Never publish COMPLETE while durable failure remains.
        runtime["stage"] = "checkpoint_transition"
        runtime["elapsed_s"] = base_elapsed
        state["config_epochs"][last_epoch] = config
        _checkpoint(
            state,
            state_path,
            progress_path,
            elapsed_s=base_elapsed,
            active_wave=None,
            total=state["total"],
            config_epoch_value=last_epoch,
        )

    wave = active
    while wave is not None:
        runtime["result"] = None
        runtime["wave"] = wave
        runtime["stage"] = "reload_or_plan_validation"
        candidate_config = resolve_config(config_path, args)
        candidate_epoch = config_epoch(candidate_config)
        config = candidate_config
        last_epoch = candidate_epoch
        runtime["config"] = config
        runtime["config_epoch"] = last_epoch
        if candidate_epoch not in preflighted_epochs:
            runtime["stage"] = "backend_preflight"
            backend_identity_preflight(config)
            preflighted_epochs.add(candidate_epoch)
        candidate_concurrencies = candidate_config["execution"]["concurrencies"]
        _ensure_state_capacity(
            state, len(candidate_concurrencies), candidate_concurrencies
        )
        concurrencies = candidate_concurrencies
        total = state["total"]
        state["config_epochs"][last_epoch] = config
        n = concurrencies[wave]
        resources = config["resources"]
        runtime["stage"] = "checkpoint_transition"
        runtime["elapsed_s"] = base_elapsed + (time.monotonic() - run_start)
        _checkpoint(
            state,
            state_path,
            progress_path,
            elapsed_s=base_elapsed + (time.monotonic() - run_start),
            active_wave=wave,
            total=total,
            config_epoch_value=last_epoch,
        )
        result = None
        acceptance_error = None
        try:
            runtime["stage"] = "run_wave"
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
            runtime["result"] = result
            runtime["stage"] = "result_processing"
            result.update(
                {
                    "wave": wave,
                    "work_units": n,
                    "config_epoch": last_epoch,
                    "provider": config["run"]["provider"],
                    "endpoint": config["run"]["endpoint"],
                    "model": config["run"]["model_id"],
                    "max_tokens": config["execution"]["max_tokens"],
                }
            )
            acceptance_error = _wave_acceptance_error(result, n, config)
            if acceptance_error is not None:
                runtime["stage"] = "wave_acceptance"
                raise RuntimeError(acceptance_error)
        except BaseException as exc:
            if acceptance_error is not None:
                raise RuntimeError(
                    f"benchmark incomplete at wave {wave}: {acceptance_error}"
                ) from exc
            raise
        state["waves"][wave] = result
        state["completed_waves"] = sorted(set(state["completed_waves"]) | {wave})
        wave = _next_active_wave(state, total, wave + 1)
        runtime["wave"] = wave if wave is not None else max(total - 1, 0)
        runtime["result"] = None
        elapsed = base_elapsed + (time.monotonic() - run_start)
        runtime["stage"] = "checkpoint_transition"
        runtime["elapsed_s"] = elapsed
        _checkpoint(
            state,
            state_path,
            progress_path,
            elapsed_s=elapsed,
            active_wave=wave,
            total=total,
            config_epoch_value=last_epoch,
        )

    runtime["stage"] = "report_publication"
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

    if args.preflight:
        print(json.dumps(backend_identity_preflight(config), indent=2, sort_keys=True))
        return 0
    if args.out is None:
        raise ValueError("--out is required unless --dry-run or --preflight is used")
    out_path = Path(args.out)
    state_path = Path(args.state) if args.state else Path(f"{args.out}.state.json")
    progress_path = Path(args.progress) if args.progress else Path(f"{args.out}.progress.yaml")
    with _exclusive_artifact_locks(out_path, state_path, progress_path) as bindings:
        return _run(args, config_path, config, *bindings)


if __name__ == "__main__":
    sys.exit(main())
