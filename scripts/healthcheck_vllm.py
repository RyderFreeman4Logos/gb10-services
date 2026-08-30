#!/usr/bin/env python3
"""vLLM service health check — verifies actual inference, not just /v1/models.

Configuration priority: CLI args > .env file > built-in defaults.

Usage:
  healthcheck_vllm.py                  # check all 3 services
  healthcheck_vllm.py chat             # check chat only (uses defaults)
  healthcheck_vllm.py chat --url http://localhost:8000 --model my-model
  healthcheck_vllm.py all --wait 300   # poll until all ready, max 5min
  healthcheck_vllm.py embedding --url http://10.0.0.1:8000 --model bge-m3
  healthcheck_vllm.py token-activity --url http://100.105.4.92:18010 --window 1m

.env format (place in project root or CWD):
  HEALTHCHECK_CHAT_URL=http://100.105.4.92:18009
  HEALTHCHECK_CHAT_MODEL=abliterated-qwen-latest-27b-none
  HEALTHCHECK_CHAT_MAX_TOKENS=16
  HEALTHCHECK_EMBEDDING_URL=http://100.105.4.92:18002
  HEALTHCHECK_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B
  HEALTHCHECK_RERANKER_URL=http://100.105.4.92:18003
  HEALTHCHECK_RERANKER_MODEL=qwen3-reranker-8b
  HEALTHCHECK_TOKEN_ACTIVITY_URL=http://100.105.4.92:18010
  HEALTHCHECK_TIMEOUT=60
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import re
import sys
import urllib.request
import urllib.error
import time
from pathlib import Path

BUILTIN_DEFAULTS: dict[str, dict[str, str | int]] = {
    "chat": {
        "url": "http://100.105.4.92:18009",
        "model": "abliterated-qwen-latest-27b-none",
        "max_tokens": 16,
    },
    "embedding": {
        "url": "http://100.105.4.92:18002",
        "model": "Qwen/Qwen3-Embedding-8B",
    },
    "reranker": {
        "url": "http://100.105.4.92:18003",
        "model": "qwen3-reranker-8b",
    },
    "token-activity": {
        "url": "http://100.105.4.92:18010",
    },
}


def _load_dotenv() -> dict[str, str]:
    for candidate in [Path(".env"), Path(__file__).resolve().parent.parent / ".env"]:
        if candidate.is_file():
            env = {}
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
            return env
    return {}


def _resolve(mode: str, field: str, cli_val: str | None) -> str:
    if cli_val is not None:
        return cli_val
    dotenv = _load_dotenv()
    env_key = f"HEALTHCHECK_{mode.upper().replace('-', '_')}_{field.upper()}"
    if env_key in dotenv:
        return dotenv[env_key]
    if env_key in os.environ:
        return os.environ[env_key]
    return str(BUILTIN_DEFAULTS.get(mode, {}).get(field, ""))


def _resolve_int(mode: str, field: str, cli_val: int | None, fallback: int) -> int:
    if cli_val is not None:
        return cli_val
    dotenv = _load_dotenv()
    env_key = f"HEALTHCHECK_{mode.upper().replace('-', '_')}_{field.upper()}"
    raw = dotenv.get(env_key) or os.environ.get(env_key)
    if raw is not None:
        return int(raw)
    default = BUILTIN_DEFAULTS.get(mode, {}).get(field)
    return int(default) if default is not None else fallback


def _parse_chat_payload(raw: str) -> dict:
    stripped = raw.lstrip()
    if not stripped.startswith("event:") and "\nevent:" not in raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("chat response is not a JSON object")
        return parsed

    for event in raw.split("\n\n"):
        lines = event.splitlines()
        if not any(line.strip() == "event: final" for line in lines):
            continue
        data = "\n".join(
            line.removeprefix("data:").lstrip()
            for line in lines
            if line.startswith("data:")
        )
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("final SSE payload is not a JSON object")
        return parsed
    raise ValueError("missing final SSE event")


_GENERATION_TOKENS_TOTAL = "vllm:generation_tokens_total"
_PROMETHEUS_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_GENERATION_TOKENS_LINE = re.compile(
    rf"^{re.escape(_GENERATION_TOKENS_TOTAL)}"
    rf"(?:\{{[^{{}}]*\}})?\s+({_PROMETHEUS_NUMBER})"
    rf"(?:\s+{_PROMETHEUS_NUMBER})?\s*$"
)


class MetricsError(ValueError):
    """Raised when the output-token metric cannot be trusted."""


def parse_duration(raw: str) -> int:
    """Parse a positive integer duration such as ``1m`` or ``2m``."""
    match = re.fullmatch(r"([1-9]\d*)([smhd])", raw.strip())
    if match is None:
        raise ValueError("duration must be a positive integer followed by s, m, h, or d")
    value = int(match.group(1))
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def parse_generation_tokens_total(metrics: str) -> float:
    """Sum all vLLM generation-token counter series in Prometheus text."""
    total = 0.0
    found = False
    for line_number, raw_line in enumerate(metrics.splitlines(), 1):
        line = raw_line.strip()
        if line.startswith(_GENERATION_TOKENS_TOTAL + "_created"):
            continue
        if not line.startswith(_GENERATION_TOKENS_TOTAL):
            continue
        match = _GENERATION_TOKENS_LINE.fullmatch(line)
        if match is None:
            raise MetricsError(f"malformed {_GENERATION_TOKENS_TOTAL} at line {line_number}")
        value = float(match.group(1))
        if not math.isfinite(value) or value < 0:
            raise MetricsError(f"invalid {_GENERATION_TOKENS_TOTAL} at line {line_number}")
        total += value
        found = True
    if not found:
        raise MetricsError(f"missing {_GENERATION_TOKENS_TOTAL}")
    return total


def _fetch_metrics(base_url: str, timeout: int) -> str:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/metrics", timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:
        raise MetricsError(f"metrics request failed: {exc}") from exc


def check_output_token_activity(
    base_url: str,
    window: int,
    timeout: int,
    *,
    fetch_metrics=None,
    sleep_fn=None,
) -> int:
    """Return 0 for counter growth, 1 for a stall, and 2 for bad metrics/I/O."""
    fetch = _fetch_metrics if fetch_metrics is None else fetch_metrics
    sleep = time.sleep if sleep_fn is None else sleep_fn
    try:
        if window <= 0:
            raise MetricsError("window must be greater than zero")
        before = parse_generation_tokens_total(fetch(base_url, timeout))
        sleep(window)
        after = parse_generation_tokens_total(fetch(base_url, timeout))
        delta = after - before
        if delta < 0:
            raise MetricsError(f"counter decreased from {before:g} to {after:g}")
    except Exception as exc:
        print(f"FAIL token-activity error={exc}")
        return 2
    if delta > 0:
        print(f"OK  token-activity window={window}s before={before:g} after={after:g} delta={delta:g}")
        return 0
    print(f"STALL token-activity window={window}s counter={after:g} delta=0")
    return 1


def check_chat(base_url: str, model: str, max_tokens: int, timeout: int) -> bool:
    url = f"{base_url}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _parse_chat_payload(resp.read().decode("utf-8", "replace"))
            content = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("completion_tokens", 0)
            print(f"OK  model={model}  tokens={tokens}  reply={content[:80]!r}")
            return True
    except Exception as e:
        print(f"FAIL  model={model}  error={e}")
        return False


def check_embedding(base_url: str, model: str, timeout: int) -> bool:
    url = f"{base_url}/v1/embeddings"
    payload = {
        "model": model,
        "input": "health check probe",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            dim = len(data["data"][0]["embedding"])
            print(f"OK  model={model}  embedding_dim={dim}")
            return True
    except Exception as e:
        print(f"FAIL  model={model}  error={e}")
        return False


def check_reranker(base_url: str, model: str, timeout: int) -> bool:
    url = f"{base_url}/v1/score"
    payload = {
        "model": model,
        "text_1": "What is machine learning?",
        "text_2": "Machine learning is a subset of artificial intelligence.",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            score = data["data"][0]["score"]
            print(f"OK  model={model}  score={score:.4f}")
            return True
    except Exception as e:
        print(f"FAIL  model={model}  error={e}")
        return False


def _run_single(mode: str, url: str, model: str, max_tokens: int, timeout: int) -> bool:
    if mode == "chat":
        return check_chat(url, model, max_tokens, timeout)
    elif mode == "embedding":
        return check_embedding(url, model, timeout)
    elif mode == "reranker":
        return check_reranker(url, model, timeout)
    return False


def check_all(cli_timeout: int | None) -> bool:
    futures = []
    modes = ("embedding", "chat", "reranker")
    with ThreadPoolExecutor(max_workers=len(modes)) as executor:
        for mode in modes:
            url = _resolve(mode, "url", None)
            model = _resolve(mode, "model", None)
            timeout = _resolve_int(mode, "timeout", cli_timeout, 60)
            max_tokens = _resolve_int(mode, "max_tokens", None, 16)
            futures.append(
                (mode, executor.submit(_run_single, mode, url, model, max_tokens, timeout))
            )

    all_ok = True
    for mode, future in futures:
        try:
            ok = future.result()
        except Exception as e:
            print(f"FAIL  mode={mode}  error={e}")
            ok = False
        if not ok:
            all_ok = False
    return all_ok


def main():
    parser = argparse.ArgumentParser(
        description="vLLM service health check (chat/embedding/reranker/token-activity)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Config priority: CLI args > .env > built-in defaults.\n"
               "See module docstring for .env variable names.",
    )
    parser.add_argument("mode", nargs="?", default="all",
                        choices=["chat", "embedding", "reranker", "all", "token-activity"])
    parser.add_argument("--url", help="Base URL (e.g. http://host:port)")
    parser.add_argument("--model", help="Model name to query")
    parser.add_argument("--max-tokens", type=int,
                        help="Max tokens for chat mode (default: 16)")
    parser.add_argument("--timeout", type=int,
                        help="Request timeout in seconds (default: 60)")
    parser.add_argument("--wait", type=int, default=0,
                        help="Poll until ready, max N seconds (0=one-shot)")
    parser.add_argument("--interval", type=int, default=5,
                        help="Poll interval when --wait is set (default: 5)")
    parser.add_argument("--window", type=parse_duration, default=60,
                        help="Metrics sampling window for token-activity (default: 1m)")
    args = parser.parse_args()

    if args.mode == "all":
        if args.wait:
            deadline = time.time() + args.wait
            while time.time() < deadline:
                if check_all(args.timeout):
                    sys.exit(0)
                remaining = int(deadline - time.time())
                print(f"  retrying in {args.interval}s... ({remaining}s left)")
                time.sleep(args.interval)
            print("TIMEOUT waiting for all services")
            sys.exit(1)
        sys.exit(0 if check_all(args.timeout) else 1)

    if args.mode == "token-activity":
        url = _resolve(args.mode, "url", args.url)
        timeout = _resolve_int(args.mode, "timeout", args.timeout, 60)
        sys.exit(check_output_token_activity(url, args.window, timeout))

    url = _resolve(args.mode, "url", args.url)
    model = _resolve(args.mode, "model", args.model)
    timeout = _resolve_int(args.mode, "timeout", args.timeout, 60)
    max_tokens = _resolve_int(args.mode, "max_tokens", args.max_tokens, 16)

    def run_check() -> bool:
        return _run_single(args.mode, url, model, max_tokens, timeout)

    if args.wait:
        deadline = time.time() + args.wait
        while time.time() < deadline:
            if run_check():
                sys.exit(0)
            remaining = int(deadline - time.time())
            print(f"  retrying in {args.interval}s... ({remaining}s left)")
            time.sleep(args.interval)
        print("TIMEOUT")
        sys.exit(1)

    sys.exit(0 if run_check() else 1)


if __name__ == "__main__":
    main()
