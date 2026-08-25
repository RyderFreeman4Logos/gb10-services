#!/usr/bin/env python3
"""Raw SGLang 212k-context thinking probe (operator-run only)."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Iterable
import urllib.request
import uuid

THINKING_BUDGET = 32768
MAX_TOKENS = 50000
TARGET_TOKENS = 212144
TOKEN_TOLERANCE = 4243
DEFAULT_ENDPOINT = "http://100.105.4.92:18010/v1/chat/completions"
DEFAULT_MODEL = "abliterated-qwen-latest-27b-nvfp4"
DEFAULT_ARTICLES = Path("/home/obj/project/github/RyderFreeman4Logos/drafts/articles/articles")

_SUMMARY_INSTRUCTION = "请用中文总结以下文章集合的核心观点、主要论据与分歧。不要逐段翻译；请给出结构化、简洁但完整的总结。"


def require_target_tokens(tokens: int) -> None:
    if not TARGET_TOKENS - TOKEN_TOLERANCE <= tokens <= TARGET_TOKENS + TOKEN_TOLERANCE:
        raise ValueError(f"token estimate {tokens} is undersize or oversize for {TARGET_TOKENS}")


def concatenate_files(paths: list[Path], destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as stream:
            os.chmod(temporary, 0o600)
            for path in paths:
                stream.write(path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _token_estimates(paths: list[Path]) -> dict[Path, int]:
    if not paths:
        raise ValueError("no article files found")
    try:
        completed = subprocess.run(
            ["csa", "tokuin", "estimate", "--json", *map(str, paths)],
            check=True,
            capture_output=True,
            text=True,
        )
        report = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RuntimeError("csa tokuin estimate failed") from exc
    entries = report.get("files") if isinstance(report, dict) else None
    if not isinstance(entries, list):
        entries = [report]
    estimates: dict[Path, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("csa tokuin estimate returned an invalid entry")
        file_name = entry.get("file")
        tokens = entry.get("tokens")
        if not isinstance(file_name, str) or isinstance(tokens, bool) or not isinstance(tokens, int):
            raise RuntimeError("csa tokuin estimate omitted a file token count")
        estimates[Path(file_name).resolve()] = tokens
    if set(estimates) != {path.resolve() for path in paths}:
        raise RuntimeError("csa tokuin estimate did not cover every article file")
    return estimates


def _choose_files(paths: list[Path], estimates: dict[Path, int]) -> list[Path]:
    lower = TARGET_TOKENS - TOKEN_TOLERANCE
    upper = TARGET_TOKENS + TOKEN_TOLERANCE
    selected: list[Path] = []
    total = 0
    for path in paths:
        tokens = estimates[path.resolve()]
        candidate = total + tokens
        if candidate > upper:
            continue
        if total < lower or abs(candidate - TARGET_TOKENS) < abs(total - TARGET_TOKENS):
            selected.append(path)
            total = candidate
    require_target_tokens(total)
    return selected


def build_corpus(articles: Path, destination: Path) -> dict:
    paths = sorted(path for path in articles.rglob("*") if path.is_file())
    estimates = _token_estimates(paths)
    selected = _choose_files(paths, estimates)
    concatenate_files(selected, destination)
    final_estimate = _token_estimates([destination])[destination.resolve()]
    require_target_tokens(final_estimate)
    try:
        destination.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("selected corpus is not UTF-8 text") from exc
    return {
        "corpus_path": str(destination),
        "estimated_tokens": final_estimate,
        "source_file_count": len(selected),
        "corpus_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }


def build_user_content(nonce: str, corpus: bytes) -> str:
    return nonce + "\n" + _SUMMARY_INSTRUCTION + "\n" + corpus.decode("utf-8")


def build_payload(model: str, nonce: str, corpus: str | bytes) -> dict:
    content = build_user_content(nonce, corpus) if isinstance(corpus, bytes) else nonce + "\n" + _SUMMARY_INSTRUCTION + "\n" + corpus
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": True},
        "reasoning_effort": "medium",
        "thinking_token_budget": THINKING_BUDGET,
        "max_tokens": MAX_TOKENS,
    }


def build_payloads(model: str, corpus: str | bytes) -> list[dict]:
    return [build_payload(model, uuid.uuid4().hex, corpus) for _ in range(3)]


def _usage_token_fields(usage: object) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    fields: dict[str, int] = {}
    stack = [("", usage)]
    while stack:
        prefix, value = stack.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                stack.append((name, child))
        elif "token" in prefix.lower() and isinstance(value, int) and not isinstance(value, bool):
            fields[prefix] = value
    return fields


def classify_sse(lines: Iterable[bytes]) -> dict:
    """Reduce an SSE stream to receipt-safe termination metadata."""
    finish_reason = None
    saw_done = False
    saw_reasoning = False
    saw_visible = False
    usage_tokens: dict[str, int] = {}
    for raw_line in lines:
        if not raw_line.startswith(b"data:"):
            continue
        data = raw_line[5:].strip()
        if data == b"[DONE]":
            saw_done = True
            continue
        try:
            event = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid SSE JSON") from exc
        if not isinstance(event, dict):
            raise ValueError("SSE event was not an object")
        usage_tokens.update(_usage_token_fields(event.get("usage")))
        choices = event.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            candidate = choice.get("finish_reason")
            if isinstance(candidate, str) and candidate:
                finish_reason = candidate
            delta = choice.get("delta")
            if isinstance(delta, dict):
                saw_reasoning |= "reasoning_content" in delta or "thinking" in delta
                saw_visible |= "content" in delta
    if not saw_done:
        termination = "missing_eos"
    elif finish_reason == "length":
        termination = "length"
    elif finish_reason == "stop":
        termination = "stop"
    else:
        termination = "unknown"
    return {
        "finish_reason": finish_reason,
        "termination": termination,
        "eos_received": saw_done,
        "thinking_stop": termination == "stop" and saw_reasoning and not saw_visible,
        "usage_tokens": usage_tokens,
    }


_FORBIDDEN_ARTIFACT_KEYS = {
    "messages", "content", "headers", "authorization", "api_key", "sse",
    "prompt", "prompt_text", "completion", "completion_text",
}


def _require_metadata_only(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_ARTIFACT_KEYS:
                raise ValueError(f"refusing artifact field: {key}")
            _require_metadata_only(child)
    elif isinstance(value, list):
        for child in value:
            _require_metadata_only(child)


def write_artifact(path: Path, value: dict) -> None:
    """Atomically write receipt metadata without any request or response body."""
    _require_metadata_only(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _nonce_from_payload(payload: dict) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("payload did not contain one user message")
    message = messages[0]
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("payload user message was invalid")
    return message["content"].split("\n", 1)[0]


def dry_run_metadata(
    payloads: list[dict], corpus_tokens: int, corpus: bytes, endpoint: str = DEFAULT_ENDPOINT
) -> dict:
    request_rows = []
    for payload in payloads:
        nonce = _nonce_from_payload(payload)
        request_rows.append({
            "nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
            "estimated_corpus_tokens": corpus_tokens,
            "estimated_input_tokens": corpus_tokens + len(nonce) + len(_SUMMARY_INSTRUCTION),
            "max_tokens": payload["max_tokens"],
            "temperature": payload["temperature"],
            "top_p": payload["top_p"],
            "top_k": payload["top_k"],
            "reasoning_effort": payload["reasoning_effort"],
            "thinking_token_budget": payload["thinking_token_budget"],
            "enable_thinking": payload["chat_template_kwargs"]["enable_thinking"],
        })
    return {
        "schema_version": 1,
        "endpoint": endpoint,
        "model": payloads[0]["model"] if payloads else None,
        "request_count": len(request_rows),
        "corpus_sha256": hashlib.sha256(corpus).hexdigest(),
        "requests": request_rows,
    }


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _progress_yaml(state: dict) -> str:
    request_rows = state.get("requests", [])
    completed = sum(row.get("status") != "queued" for row in request_rows)
    return (
        f"phase: {state['phase']}\n"
        f"pid: {state['pid']}\n"
        f"completed_requests: {completed}\n"
        f"total_requests: {len(request_rows)}\n"
        f"snapshots: {len(state['host_snapshots'])}\n"
    )


def _default_run_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return Path("/tmp") / f"sglang-212k-thinking-{stamp}-{uuid.uuid4().hex[:8]}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--build-corpus", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--dry-run-payload", action="store_true")
    parser.add_argument("--articles", type=Path, default=DEFAULT_ARTICLES)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--request-timeout-s", type=float, default=14400.0)
    parser.add_argument("--sample-interval-s", type=float, default=60.0)
    parser.add_argument("--server-pid", type=int)
    parser.add_argument("--cgroup-path", type=Path)
    args = parser.parse_args(argv)
    if args.request_timeout_s <= 0 or args.sample_interval_s <= 0:
        parser.error("timeouts and sample interval must be positive")
    if not args.build_corpus and not args.preflight and args.corpus is None:
        parser.error("--corpus is required for a dry run or request run")
    return args


def _process_start_time(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return int(fields[19])
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def _parse_key_value_file(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, int] = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            try:
                values[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                pass
    return values


def _memory_snapshot() -> dict:
    meminfo = _parse_key_value_file(Path("/proc/meminfo"))
    snapshot = {
        "mem_available_kib": meminfo.get("MemAvailable"),
        "swap_free_kib": meminfo.get("SwapFree"),
        "swap_total_kib": meminfo.get("SwapTotal"),
    }
    for line in Path("/proc/pressure/memory").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key in {"avg10", "avg60", "avg300"}:
                try:
                    snapshot[f"memory_psi_{kind}_{key}"] = float(value)
                except ValueError:
                    pass
    return snapshot


def _cgroup_events(path: Path | None) -> dict[str, int]:
    if path is None or not path.is_dir():
        return {}
    values = _parse_key_value_file(path / "memory.events")
    return {
        "oom": values.get("oom", 0),
        "oom_kill": values.get("oom_kill", 0),
    }


def _process_cgroup(pid: int) -> Path | None:
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines():
            hierarchy, _, relative = line.partition("::")
            if hierarchy == "0" and relative.startswith("/"):
                return Path("/sys/fs/cgroup") / relative.lstrip("/")
    except OSError:
        pass
    return None


def _process_snapshot(pid: int, cgroup_path: Path | None = None) -> dict:
    start_time = _process_start_time(pid)
    if start_time is None:
        return {"pid": pid, "alive": False}
    return {
        "pid": pid,
        "start_time": start_time,
        "alive": True,
        "cgroup_memory_events": _cgroup_events(cgroup_path or _process_cgroup(pid)),
    }


def _find_sglang_pids() -> list[int]:
    pids = []
    uid = os.getuid()
    for candidate in Path("/proc").iterdir():
        if not candidate.name.isdigit():
            continue
        try:
            if candidate.stat().st_uid != uid:
                continue
            command = (candidate / "cmdline").read_bytes().lower()
        except OSError:
            continue
        if b"sglang" in command and b"18010" in command:
            pids.append(int(candidate.name))
    return sorted(pids)


def host_snapshot(server_pids: list[int], cgroup_path: Path | None) -> dict:
    snapshot = {
        "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **_memory_snapshot(),
        "server_processes": [_process_snapshot(pid) for pid in server_pids],
        "monitored_cgroup_memory_events": _cgroup_events(cgroup_path),
    }
    return snapshot


def _sse_has_generated_token(raw_line: bytes) -> bool:
    if not raw_line.startswith(b"data:"):
        return False
    data = raw_line[5:].strip()
    if not data or data == b"[DONE]":
        return False
    try:
        event = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(event, dict) or not isinstance(event.get("choices"), list):
        return False
    for choice in event["choices"]:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict):
            continue
        if any(isinstance(delta.get(key), str) and delta[key] for key in ("content", "reasoning_content")):
            return True
    return False


def stream_chat(endpoint: str, payload: dict, timeout_s: float) -> dict:
    start = time.monotonic()
    ttft_s = None
    try:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            def timed_lines():
                nonlocal ttft_s
                for raw_line in response:
                    if ttft_s is None and _sse_has_generated_token(raw_line):
                        ttft_s = time.monotonic() - start
                    yield raw_line

            termination = classify_sse(timed_lines())
    except Exception as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "ttft_s": ttft_s,
            "wall_s": time.monotonic() - start,
        }
    usage_tokens = termination.pop("usage_tokens")
    wall_s = time.monotonic() - start
    return {
        "status": "complete",
        "ttft_s": wall_s if ttft_s is None else ttft_s,
        "wall_s": wall_s,
        "prompt_tokens": usage_tokens.get("prompt_tokens"),
        "completion_tokens": usage_tokens.get("completion_tokens"),
        "usage_tokens": usage_tokens,
        **termination,
    }


def _models_endpoint(endpoint: str) -> str:
    suffix = "/chat/completions"
    if not endpoint.endswith(suffix):
        raise ValueError("endpoint must end with /v1/chat/completions")
    return endpoint[:-len(suffix)] + "/models"


def preflight(endpoint: str) -> dict:
    models_endpoint = _models_endpoint(endpoint)
    request = urllib.request.Request(models_endpoint, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            body = json.loads(response.read())
    except Exception as exc:
        raise RuntimeError(f"models preflight failed: {type(exc).__name__}") from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("models preflight returned an invalid response")
    offered = sorted(item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str))
    return {"models_endpoint": models_endpoint, "offered_models": offered}


def _summary(state: dict) -> dict:
    rows = state["requests"]
    termination_counts: dict[str, int] = {}
    for row in rows:
        category = row.get("termination") or row.get("status", "unknown")
        termination_counts[category] = termination_counts.get(category, 0) + 1
    snapshots = state["host_snapshots"]
    numeric = lambda key: [row[key] for row in snapshots if isinstance(row.get(key), (int, float))]
    first_events = snapshots[0].get("monitored_cgroup_memory_events", {})
    last_events = snapshots[-1].get("monitored_cgroup_memory_events", {})
    cgroup_delta = {
        key: last_events.get(key, 0) - first_events.get(key, 0)
        for key in ("oom", "oom_kill")
    } if first_events and last_events else None
    initial = {row["pid"] for row in snapshots[0]["server_processes"] if row.get("alive")}
    final_alive = {row["pid"] for row in snapshots[-1]["server_processes"] if row.get("alive")}
    return {
        "request_count": len(rows),
        "termination_counts": termination_counts,
        "thinking_stop_count": sum(bool(row.get("thinking_stop")) for row in rows),
        "min_mem_available_kib": min(numeric("mem_available_kib"), default=None),
        "min_swap_free_kib": min(numeric("swap_free_kib"), default=None),
        "max_memory_psi_some_avg10": max(numeric("memory_psi_some_avg10"), default=None),
        "max_memory_psi_full_avg10": max(numeric("memory_psi_full_avg10"), default=None),
        "monitored_cgroup_oom_delta": cgroup_delta,
        "observed_server_process_death": bool(initial - final_alive),
    }


def _ensure_external_run_path(path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    try:
        path.resolve().relative_to(checkout)
    except ValueError:
        return
    raise ValueError("run artifacts must be outside this checkout")


def run_probe(args: argparse.Namespace, run_dir: Path, corpus: bytes, corpus_tokens: int) -> dict:
    payloads = build_payloads(args.model, corpus)
    metadata = dry_run_metadata(payloads, corpus_tokens, corpus, args.endpoint)
    pid = os.getpid()
    state = {
        "schema_version": 1,
        "phase": "running",
        "pid": pid,
        "start_time": _process_start_time(pid),
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": args.endpoint,
        "model": args.model,
        "corpus_sha256": metadata["corpus_sha256"],
        "estimated_corpus_tokens": corpus_tokens,
        "requests": [{**row, "status": "queued"} for row in metadata["requests"]],
        "host_snapshots": [],
    }
    server_pids = [args.server_pid] if args.server_pid is not None else _find_sglang_pids()
    state["observed_server_pids"] = server_pids
    monitor_cgroup = args.cgroup_path or Path("/sys/fs/cgroup")
    state["monitored_cgroup_scope"] = "explicit" if args.cgroup_path else "host_root"
    state_path = run_dir / "state.json"
    progress_path = run_dir / "progress.yaml"
    if state_path.exists() or progress_path.exists():
        raise RuntimeError("refusing to overwrite an existing probe run")
    lock = threading.Lock()

    def checkpoint_locked() -> None:
        state["updated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_artifact(state_path, state)
        _write_text_atomic(progress_path, _progress_yaml(state))

    with lock:
        state["host_snapshots"].append(host_snapshot(server_pids, monitor_cgroup))
        checkpoint_locked()

    stop_monitor = threading.Event()

    def monitor() -> None:
        while not stop_monitor.wait(args.sample_interval_s):
            with lock:
                state["host_snapshots"].append(host_snapshot(server_pids, monitor_cgroup))
                checkpoint_locked()

    monitor_thread = threading.Thread(target=monitor, name="host-snapshot", daemon=True)
    monitor_thread.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(stream_chat, args.endpoint, payload, args.request_timeout_s): index
                for index, payload in enumerate(payloads)
            }
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"status": "error", "error_type": type(exc).__name__}
                with lock:
                    state["requests"][index].update(result)
                    checkpoint_locked()
    finally:
        stop_monitor.set()
        monitor_thread.join()
        with lock:
            state["host_snapshots"].append(host_snapshot(server_pids, monitor_cgroup))
            state["phase"] = "complete"
            state["aggregate"] = _summary(state)
            checkpoint_locked()
    return state


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.preflight:
        print(json.dumps(preflight(args.endpoint), sort_keys=True))
        return 0
    run_dir = args.run_dir or _default_run_dir()
    _ensure_external_run_path(run_dir)
    if args.build_corpus:
        destination = args.corpus or run_dir / "corpus.txt"
        _ensure_external_run_path(destination)
        print(json.dumps(build_corpus(args.articles, destination), sort_keys=True))
        return 0
    assert args.corpus is not None
    corpus = args.corpus.read_bytes()
    corpus_tokens = _token_estimates([args.corpus])[args.corpus.resolve()]
    if args.dry_run_payload:
        metadata = dry_run_metadata(build_payloads(args.model, corpus), corpus_tokens, corpus, args.endpoint)
        receipt = run_dir / "dry-run-payload.json"
        write_artifact(receipt, metadata)
        print(json.dumps({"dry_run_receipt": str(receipt), "request_count": 3}, sort_keys=True))
        return 0
    require_target_tokens(corpus_tokens)
    state = run_probe(args, run_dir, corpus, corpus_tokens)
    print(json.dumps({"run_dir": str(run_dir), "phase": state["phase"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
