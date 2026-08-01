#!/usr/bin/env python3
"""Exercise Guard loop recovery with a release binary and an isolated fake upstream."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CALLER_MAX = 50_000
THINKING_BUDGET = 32_768
EXPECTED_FIRST_MAX = CALLER_MAX


def fail(message: str) -> None:
    raise RuntimeError(message)


def require(condition: bool, code: str) -> None:
    if not condition:
        fail(code)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def budgets(body: dict) -> tuple[int | None, int | None]:
    thinking = body.get("thinking")
    canonical = thinking.get("budget_tokens") if isinstance(thinking, dict) and isinstance(thinking.get("budget_tokens"), int) else None
    native = body.get("thinking_token_budget") if isinstance(body.get("thinking_token_budget"), int) else None
    return canonical, native


def effective_budget(body: dict) -> int | None:
    canonical, native = budgets(body)
    return native if native is not None else canonical


def sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


class Fixture:
    def __init__(
        self,
        reasoning_marker: str,
        private_prefix_marker: str,
        fresh_input_marker: str,
        positive_output_marker: str,
        fresh_output_marker: str,
    ):
        self.reasoning_marker = reasoning_marker
        self.private_prefix_marker = private_prefix_marker
        self.fresh_input_marker = fresh_input_marker
        self.positive_output_marker = positive_output_marker
        self.fresh_output_marker = fresh_output_marker
        self.lock = threading.Lock()
        self.chat_attempts: list[dict] = []
        self.errors: list[str] = []
    def inspect_chat(self, body: dict) -> dict:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        messages = body.get("messages")
        phase = "fresh" if isinstance(messages, list) and any(isinstance(message, dict) and message.get("content") == self.fresh_input_marker for message in messages) else "positive"
        private_prefix_present = self.private_prefix_marker in text
        salvage_material = private_prefix_present and "Private bounded pre-loop reasoning notes" in text
        with self.lock:
            n = len(self.chat_attempts) + 1
            attempt = {
                "number": n,
                "phase": phase,
                "thinking_budget": effective_budget(body),
                "thinking_budget_canonical": budgets(body)[0],
                "thinking_budget_native": budgets(body)[1],
                "max_tokens": body.get("max_tokens"),
                "stream": body.get("stream"),
                "stream_usage": isinstance(body.get("stream_options"), dict)
                and body["stream_options"].get("include_usage") is True,
                "salvage_material_present": salvage_material,
                "private_prefix_present": private_prefix_present,
                "loop_tail_present": self.reasoning_marker in text,
            }
            self.chat_attempts.append(attempt)
            if n == 1:
                if effective_budget(body) != THINKING_BUDGET:
                    self.errors.append("first_thinking_budget")
                if body.get("max_tokens") != EXPECTED_FIRST_MAX:
                    self.errors.append("first_max_tokens")
                if body.get("stream") is not True or not attempt["stream_usage"]:
                    self.errors.append("first_shielded_sse_contract")
            if salvage_material:
                if effective_budget(body) != 0:
                    self.errors.append("salvage_thinking_budget")
                if body.get("max_tokens") != CALLER_MAX:
                    self.errors.append("salvage_answer_budget")
                if body.get("stream") is not True:
                    self.errors.append("salvage_not_sse")
                if attempt["loop_tail_present"]:
                    self.errors.append("salvage_replayed_loop_tail")
            return attempt

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def send_json(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path.endswith("/models"):
                    self.send_json(200, {"object": "list", "data": [{"id": "aeon-ultimate", "object": "model"}]})
                else:
                    self.send_json(404, {"error": {"message": "fixture route"}})

            def do_POST(self) -> None:
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(size))
                except Exception:
                    fixture.errors.append("malformed_upstream_request")
                    self.send_json(400, {"error": {"message": "malformed"}})
                    return
                if self.path.endswith("/embeddings"):
                    values = body.get("input")
                    count = len(values) if isinstance(values, list) else 1
                    self.send_json(200, {"object": "list", "data": [{"object": "embedding", "index": i, "embedding": [1.0] + [0.0] * 255} for i in range(count)], "model": "fixture", "usage": {"prompt_tokens": 1, "total_tokens": 1}})
                    return
                if not self.path.endswith("/chat/completions") or not isinstance(body, dict):
                    fixture.errors.append("unexpected_upstream_route")
                    self.send_json(404, {"error": {"message": "route"}})
                    return
                attempt = fixture.inspect_chat(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    if attempt["number"] == 1:
                        self.wfile.write(sse({"id": "fixture-1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"reasoning_content": f"{fixture.private_prefix_marker} derive the isolated invariant before answering\n"}, "finish_reason": None}]}))
                        self.wfile.flush()
                        repeated = fixture.reasoning_marker + " repeat loop line\n"
                        for _ in range(40):
                            self.wfile.write(sse({"id": "fixture-1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"reasoning_content": repeated}, "finish_reason": None}]}))
                            self.wfile.flush()
                    else:
                        text = fixture.positive_output_marker if attempt["salvage_material_present"] else fixture.fresh_output_marker
                        self.wfile.write(sse({"id": f"fixture-{attempt['number']}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}))
                        self.wfile.write(sse({"id": f"fixture-{attempt['number']}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
                        self.wfile.write(sse({"id": f"fixture-{attempt['number']}", "object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}))
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler


def isolated_config(candidate: Path, root: Path, fake_port: int, guard_port: int) -> tuple[Path, dict]:
    raw = candidate.read_text()
    parsed = tomllib.loads(raw)
    profiles = {p["name"]: p for p in parsed["upstreams"]}
    require(
        profiles["aeon-guard-max"]["loop_guard"]["on_reasoning_loop"]
        == "truncate_cot_then_answer",
        "candidate_recovery_mode",
    )
    require(
        profiles["aeon-legacy-bounded"]["loop_guard"]["on_reasoning_loop"]
        == "bounded_answer_from_cot",
        "legacy_recovery_mode",
    )
    raw_flags = {
        "observability.capture_raw_payloads": parsed["observability"]["capture_raw_payloads"],
        "evidence.include_raw_payloads": parsed["evidence"]["include_raw_payloads"],
        "evidence.shadow.paired_comparison.include_raw_input": parsed["evidence"]["shadow"]["paired_comparison"]["include_raw_input"],
        "evidence.shadow.paired_comparison.include_raw_output": parsed["evidence"]["shadow"]["paired_comparison"]["include_raw_output"],
        "evidence.shadow.paired_comparison.include_raw_reasoning": parsed["evidence"]["shadow"]["paired_comparison"]["include_raw_reasoning"],
    }
    require(not any(raw_flags.values()), "candidate_raw_capture_enabled")
    fake = f"http://127.0.0.1:{fake_port}/v1"
    raw = raw.replace("http://100.105.4.92:18010/v1", fake)
    raw = raw.replace("http://100.105.4.92:18012/v1", fake)
    raw = raw.replace("http://100.105.4.92:18013/v1", fake)
    raw = raw.replace("host = \"100.105.4.92\"", "host = \"127.0.0.1\"")
    raw = raw.replace("port = 18009", f"port = {free_port()}")
    # Keep the profile-pinned :18011 listener on the harness client port.
    raw = raw.replace("port = 18011", f"port = {guard_port}")
    for old in (18002, 18003, 18005, 18014, 18015):
        raw = raw.replace(f"port = {old}", f"port = {free_port()}")
    raw = raw.replace("/home/obj/.local/state/llm-guard-proxy/observability.sqlite3", str(root / "state" / "observability.sqlite3"))
    raw = raw.replace("/home/obj/.local/state/llm-guard-proxy-evidence/evidence.sqlite3", str(root / "state" / "evidence.sqlite3"))
    raw = raw.replace("/home/obj/.cache/llm-guard-proxy-evidence/blobs", str(root / "state" / "blobs"))
    # Fixture-only safety neutralization: do not cross into the next TOML table.
    for section in (
        "guardian",
        "upstream.local_recovery",
        "upstream.hot_restart",
        "upstreams.local_recovery",
        "upstreams.hot_restart",
    ):
        raw = re.sub(rf"(?ms)^(\[{re.escape(section)}\]\n(?:(?!^\[).)*?^enabled = )true$", r"\1false", raw)
    derived = tomllib.loads(raw)
    derived_profiles = {profile["name"]: profile for profile in derived["upstreams"]}
    selected_profile = derived_profiles["aeon-guard-max"]
    require(
        all(listener["bind_host"] == "127.0.0.1" for listener in derived["listeners"]),
        "fixture_non_loopback_listener",
    )
    require(
        all(endpoint["base_url"] == fake for endpoint in derived["upstreams"]),
        "fixture_nonlocal_upstream",
    )
    require(derived["guardian"]["enabled"] is False, "fixture_guardian_enabled")
    require(
        derived["upstream"]["local_recovery"]["enabled"] is False,
        "fixture_global_local_recovery_enabled",
    )
    require(
        derived["upstream"]["hot_restart"]["enabled"] is False,
        "fixture_global_hot_restart_enabled",
    )
    require(
        selected_profile["local_recovery"]["enabled"] is False,
        "fixture_local_recovery_enabled",
    )
    require(
        selected_profile["hot_restart"]["enabled"] is False,
        "fixture_hot_restart_enabled",
    )
    require(derived["shielding"] == parsed["shielding"], "fixture_changed_shielding")
    require(
        all(
            derived_profiles[name]["thinking"] == profile["thinking"]
            and {key: value for key, value in derived_profiles[name].get("loop_guard", {}).items() if key != "embedding"}
            == {key: value for key, value in profile.get("loop_guard", {}).items() if key != "embedding"}
            for name, profile in profiles.items()
        ),
        "fixture_changed_recovery_policy",
    )
    listener = next(item for item in derived["listeners"] if item["name"] == "aeon-guard-max")
    require(
        listener["port"] == guard_port
        and listener["upstream_profile"] == "aeon-guard-max",
        "fixture_listener_mismatch",
    )
    output = root / "candidate-isolated.toml"
    output.write_text(raw)
    return output, {
        "candidate_config_sha256": sha256(candidate),
        "isolated_config_sha256": sha256(output),
        "raw_flags": raw_flags,
    }


def wait_port(port: int, proc: subprocess.Popen[bytes], deadline: float) -> None:
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            fail(f"proxy_exit_before_ready={proc.returncode}")
        with socket.socket() as sock:
            sock.settimeout(0.15)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    fail("proxy_ready_timeout")


def client(port: int, input_marker: str, expected_output: str, reasoning_marker: str, private_prefix_marker: str) -> dict:
    body = {"model": "__listener_forced_aeon_guard_max__", "messages": [{"role": "user", "content": input_marker}], "max_tokens": CALLER_MAX, "stream": False}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(body, separators=(",", ":")).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read()
            try:
                data = json.loads(raw)
                response_format = "json"
            except json.JSONDecodeError:
                data = None
                response_format = "invalid"
                for event in raw.decode("utf-8", "replace").split("\n\n"):
                    if "event: final" not in event:
                        continue
                    encoded = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                    try:
                        data = json.loads(encoded)
                        response_format = "event_final"
                    except json.JSONDecodeError:
                        pass
                    break
            content = data.get("choices", [{}])[0].get("message", {}).get("content") if isinstance(data, dict) else None
            return {"status": response.status, "format": response_format, "nonempty": isinstance(content, str) and bool(content), "expected_final": content == expected_output, "reasoning_marker_leak_count": raw.count(reasoning_marker.encode()), "private_prefix_marker_leak_count": raw.count(private_prefix_marker.encode())}
    except urllib.error.HTTPError as err:
        raw = err.read()
        return {"status": err.code, "nonempty": False, "expected_final": False, "reasoning_marker_leak_count": raw.count(reasoning_marker.encode()), "private_prefix_marker_leak_count": raw.count(private_prefix_marker.encode())}


def stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=8)


def sensitive_marker_count(root: Path, markers: tuple[bytes, ...]) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                data = path.read_bytes()
                total += sum(data.count(marker) for marker in markers)
            except OSError:
                pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate-config", required=True, type=Path)
    ap.add_argument("--binary", required=True, type=Path)
    ap.add_argument("--root", required=True, type=Path)
    args = ap.parse_args()
    candidate = args.candidate_config.resolve(strict=True)
    binary = args.binary.resolve(strict=True)
    require(binary.is_file() and os.access(binary, os.X_OK), "binary_not_executable")
    root = args.root
    root.mkdir(mode=0o700, parents=False)
    os.chmod(root, 0o700)
    (root / "state").mkdir(mode=0o700)
    (root / "home").mkdir(mode=0o700)
    (root / "tmp").mkdir(mode=0o700)
    sensitive_markers = tuple("S" + secrets.token_hex(24) for _ in range(6))
    reasoning_marker, private_prefix_marker, positive_input_marker, fresh_input_marker, positive_output_marker, fresh_output_marker = sensitive_markers
    fixture = Fixture(
        reasoning_marker,
        private_prefix_marker,
        fresh_input_marker,
        positive_output_marker,
        fresh_output_marker,
    )
    fake_port, guard_port = free_port(), free_port()
    summary: dict = {
        "binary_path": str(binary),
        "binary_sha256": sha256(binary),
        "response_api_tested": False,
        "response_api_boundary": "unsupported",
        "fixture_root_mode": oct(root.stat().st_mode & 0o777),
        "ports": {"fake": fake_port, "guard": guard_port},
    }
    proc = None
    server = None
    try:
        config, hashes = isolated_config(candidate, root, fake_port, guard_port)
        summary.update(hashes)
        server = ThreadingHTTPServer(("127.0.0.1", fake_port), fixture.handler())
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"), "XDG_CACHE_HOME": str(root / "home" / ".cache"), "XDG_CONFIG_HOME": str(root / "home" / ".config"), "XDG_DATA_HOME": str(root / "home" / ".local" / "share"), "PATH": os.environ["PATH"]}
        with (root / "proxy.log").open("wb") as log:
            proc = subprocess.Popen([str(binary), "--config", str(config), "--guardian-runtime-dir", str(root / "guardian-runtime")], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            wait_port(guard_port, proc, time.monotonic() + 20)
            summary["candidate_full_toml_release_binary_parsed"] = True
            summary["positive_client"] = client(
                guard_port,
                positive_input_marker,
                positive_output_marker,
                reasoning_marker,
                private_prefix_marker,
            )
            summary["fresh_client"] = client(
                guard_port,
                fresh_input_marker,
                fresh_output_marker,
                reasoning_marker,
                private_prefix_marker,
            )
        salvages = [attempt for attempt in fixture.chat_attempts if attempt["salvage_material_present"]]
        fresh_attempts = [attempt for attempt in fixture.chat_attempts if attempt["phase"] == "fresh"]
        summary["salvage_count"] = len(salvages)
        summary["fresh_request_count"] = len(fresh_attempts)
        if len(salvages) != 1:
            fixture.errors.append("salvage_count")
        if not fresh_attempts:
            fixture.errors.append("fresh_request_missing")
        else:
            fresh = fresh_attempts[0]
            if fresh["private_prefix_present"] or fresh["salvage_material_present"] or fresh["loop_tail_present"]:
                fixture.errors.append("fresh_request_replayed_private_material")
            if fresh["thinking_budget"] != THINKING_BUDGET:
                fixture.errors.append("fresh_thinking_budget")
        summary["attempts"] = fixture.chat_attempts
        summary["fixture_errors"] = fixture.errors
        summary["positive_pass"] = summary["positive_client"]["status"] == 200 and summary["positive_client"]["nonempty"] and summary["positive_client"]["expected_final"] and summary["positive_client"]["reasoning_marker_leak_count"] == 0 and summary["positive_client"]["private_prefix_marker_leak_count"] == 0
        summary["fresh_negative_pass"] = summary["fresh_client"]["status"] == 200 and summary["fresh_client"]["nonempty"] and summary["fresh_client"]["expected_final"] and summary["fresh_client"]["reasoning_marker_leak_count"] == 0 and summary["fresh_client"]["private_prefix_marker_leak_count"] == 0
        if fixture.errors or not summary["positive_pass"] or not summary["fresh_negative_pass"]:
            fail("acceptance_assertion_failed")
        summary["result"] = "PASS"
    except Exception as exc:
        summary["result"] = "FAIL"
        summary["error_class"] = type(exc).__name__
        summary["error_code"] = str(exc)[:160]
    finally:
        stop(proc)
        if server:
            server.shutdown()
            server.server_close()
        time.sleep(0.15)
        summary["attempts"] = fixture.chat_attempts
        summary["fixture_errors"] = fixture.errors
        summary["process_cleanup"] = {"proxy_exited": proc is None or proc.poll() is not None}
        summary["port_cleanup"] = {"guard_rebindable": _rebindable(guard_port), "fake_rebindable": _rebindable(fake_port)}
        summary["sensitive_marker_leak_count_all_fixture_files"] = sensitive_marker_count(
            root,
            tuple(marker.encode() for marker in sensitive_markers),
        )
        if (
            summary["sensitive_marker_leak_count_all_fixture_files"]
            or not summary["process_cleanup"]["proxy_exited"]
            or not all(summary["port_cleanup"].values())
        ):
            summary["result"] = "FAIL"
            summary.setdefault("error_code", "cleanup_or_persistence_failed")
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["result"] == "PASS" else 1


def _rebindable(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


if __name__ == "__main__":
    raise SystemExit(main())
