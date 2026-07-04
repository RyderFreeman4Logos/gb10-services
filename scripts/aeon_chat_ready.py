#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


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


def probe(url: str, model: str, timeout: float) -> tuple[bool, str, float]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "1+1=?"}],
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "gb10-aeon-ready/1"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(1024 * 1024).decode("utf-8", "replace")
            elapsed = time.monotonic() - started
            data = _parse_chat_payload(raw)
            choices = data.get("choices") or []
            usage = data.get("usage") or {}
            if response.status == 200 and choices:
                return True, f"ready status=200 elapsed={elapsed:.3f}s completion_tokens={usage.get('completion_tokens')}", elapsed
            return False, f"bad_response status={response.status} elapsed={elapsed:.3f}s choices={len(choices)}", elapsed
    except Exception as exc:
        elapsed = time.monotonic() - started
        msg = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")[:200]
        return False, f"not_ready elapsed={elapsed:.3f}s error={msg}", elapsed


def probe_models(url: str, timeout: float) -> tuple[bool, str, float]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "gb10-aeon-ready/1"},
        method="GET",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            response.read(1024 * 1024)
            elapsed = time.monotonic() - started
            if response.status == 200:
                return True, f"models_ready status=200 elapsed={elapsed:.3f}s", elapsed
            return False, f"models_bad_response status={response.status} elapsed={elapsed:.3f}s", elapsed
    except Exception as exc:
        elapsed = time.monotonic() - started
        msg = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")[:200]
        return False, f"models_not_ready elapsed={elapsed:.3f}s error={msg}", elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for AEON chat completion readiness.")
    parser.add_argument("--url", default="http://100.105.4.92:18009/v1/chat/completions")
    parser.add_argument("--models-url", default="http://100.105.4.92:18009/v1/models")
    parser.add_argument("--model", default="aeon-ultimate")
    parser.add_argument("--models-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=6.0)
    parser.add_argument("--long-timeout-seconds", type=float, default=0.0)
    parser.add_argument("--deadline-seconds", type=float, default=840.0)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    args = parser.parse_args()

    deadline = time.monotonic() + args.deadline_seconds
    attempts = 0
    last = "not attempted"
    while time.monotonic() < deadline:
        attempts += 1
        models_timeout = min(args.models_timeout_seconds, max(0.5, deadline - time.monotonic()))
        models_ok, models_message, _models_elapsed = probe_models(args.models_url, models_timeout)
        last = models_message
        if not models_ok:
            if attempts == 1 or attempts % 12 == 0:
                print(f"aeon_models_wait attempts={attempts} {models_message}", file=sys.stderr, flush=True)
            time.sleep(max(0.5, args.interval_seconds))
            continue

        timeout = min(args.timeout_seconds, max(0.5, deadline - time.monotonic()))
        ok, message, elapsed = probe(args.url, args.model, timeout)
        last = message
        if ok:
            print(f"aeon_chat_ready attempts={attempts} {message}", flush=True)
            return 0
        if attempts == 1 or attempts % 12 == 0:
            print(f"aeon_chat_wait attempts={attempts} {message}", file=sys.stderr, flush=True)

        remaining = deadline - time.monotonic()
        if (
            args.long_timeout_seconds > args.timeout_seconds
            and elapsed >= timeout * 0.8
            and remaining > args.timeout_seconds + 1.0
        ):
            attempts += 1
            long_timeout = min(args.long_timeout_seconds, max(0.5, remaining))
            print(
                f"aeon_chat_long_wait attempts={attempts} timeout={long_timeout:.3f}s after={message}",
                file=sys.stderr,
                flush=True,
            )
            ok, message, _elapsed = probe(args.url, args.model, long_timeout)
            last = message
            if ok:
                print(f"aeon_chat_ready attempts={attempts} {message}", flush=True)
                return 0
            print(f"aeon_chat_long_not_ready attempts={attempts} {message}", file=sys.stderr, flush=True)

        time.sleep(max(0.5, args.interval_seconds))
    print(f"aeon_chat_not_ready attempts={attempts} last={last}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
