#!/usr/bin/env python3
"""Measures SGLang text-backend prefill/decode throughput at >=6k prompt tokens.

Stdlib only. Builds unique uncached prompts (nonce first line), runs waves of
concurrency 1/2/4/6/8 sequentially, streams responses to get real TTFT, and
writes per-request + per-wave metrics as JSON.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_BASE_URL = "http://100.105.4.92:18010/v1"
DEFAULT_MODEL = "abliterated-qwen-latest-27b-nvfp4"
DEFAULT_MIN_PROMPT_TOKENS = 6000
DEFAULT_MAX_TOKENS = 256
DEFAULT_CONCURRENCIES = [1, 2, 4, 6, 8]

FILLER_WORD = "aluminium"


def estimate_tokens(text: str) -> int:
    # Conservative local estimate: whitespace-separated words, capping at
    # 1 token per word (Qwen counts ~1 token/word). Never 4-chars/token.
    return max(1, len(text.split()))


def make_nonce(wave: int, index: int) -> str:
    return f"{wave}-{index}-{uuid.uuid4()}-{time.time_ns()}"


def _grow_to_min_tokens(nonce: str, min_tokens: int) -> str:
    """Deterministic filler grown until the local estimate is >= min_tokens."""
    word = FILLER_WORD + " "
    tokens = estimate_tokens(
        f"{nonce}\n<|startoftext|>\n{word * 16}\nPlease echo this nonce at the very end: {nonce}\n"
    )
    count = 1
    while tokens < min_tokens:
        count += 1
        tokens = estimate_tokens(
            f"{nonce}\n<|startoftext|>\n{word * count}\nPlease echo this nonce at the very end: {nonce}\n"
        )
    body = word * count
    return f"{nonce}\n<|startoftext|>\n{body}\nPlease echo this nonce at the very end: {nonce}\n"


def build_prompt(nonce: str, min_tokens: int) -> str:
    return _grow_to_min_tokens(nonce, min_tokens)


def build_payload(
    model: str, nonce: str, min_tokens: int, max_tokens: int
) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "user", "content": build_prompt(nonce, min_tokens)}
        ],
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


def _stream_chat(base_url: str, payload: dict, min_tokens: int) -> dict:
    """POST a streaming chat request, return parsed SSE usage + throughput."""
    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    ttft = None
    first_chunk_ts = None
    gen_ts = 0.0
    last_ts = start
    finish_reason = None
    usage = None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):]
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
                    first_chunk_ts = now
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


def run_request(base_url: str, payload: dict, min_tokens: int) -> dict:
    metric = _stream_chat(base_url, payload, min_tokens)
    metric["ok"] = False if metric["n_fail_short"] else True
    return metric


def run_wave(base_url: str, model: str, wave: int, n: int, min_tokens: int, max_tokens: int) -> dict:
    payloads = [
        build_payload(model, make_nonce(wave, i), min_tokens, max_tokens)
        for i in range(n)
    ]
    wave_start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(lambda p: run_request(base_url, p, min_tokens), payloads))
    wave_wall = time.monotonic() - wave_start
    ok = [r for r in results if r["ok"]]
    n_ok = len(ok)
    n_fail = len(results) - n_ok
    sum_completion = sum(r["completion_tokens"] for r in ok)
    sum_prompt = sum(r["prompt_tokens"] for r in ok)
    return {
        "concurrency": n,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "sum_completion": sum_completion,
        "wave_wall_s": wave_wall,
        "agg_decode_tok_s": sum_completion / wave_wall if wave_wall > 0 else 0.0,
        "agg_prompt_tok_s": sum_prompt / wave_wall if wave_wall > 0 else 0.0,
        "requests": results,
    }


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="6k-input SGLang concurrency throughput harness"
    )
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--min-prompt-tokens", type=int, default=DEFAULT_MIN_PROMPT_TOKENS)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument(
        "--concurrencies",
        default=",".join(str(c) for c in DEFAULT_CONCURRENCIES),
        help="comma-separated concurrency levels",
    )
    p.add_argument("--out", required=True, help="path for JSON output")
    p.add_argument("--dry-run", action="store_true", help="build prompts only, no HTTP")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    concurrencies = [int(c) for c in args.concurrencies.split(",") if c.strip()]
    if args.dry_run:
        nonce = make_nonce(0, 0)
        sample = build_prompt(nonce, args.min_prompt_tokens)
        print(
            json.dumps(
                {
                    "concurrencies": concurrencies,
                    "model": args.model,
                    "base_url": args.base_url,
                    "min_prompt_tokens": args.min_prompt_tokens,
                    "max_tokens": args.max_tokens,
                    "sample_prompt": sample,
                },
                indent=2,
            )
        )
        return 0
    report = {"base_url": args.base_url, "model": args.model, "waves": []}
    for wave, n in enumerate(concurrencies):
        report["waves"].append(
            run_wave(args.base_url, args.model, wave, n, args.min_prompt_tokens, args.max_tokens)
        )
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
