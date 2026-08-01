#!/usr/bin/env python3
"""Exercise Guard loop recovery with a release binary and an isolated fake upstream."""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

CALLER_MAX = 50_000
THINKING_BUDGET = 32_768
EXPECTED_FIRST_MAX = CALLER_MAX
FIXTURE_BODY_LIMIT = 1 << 20
FIXTURE_READ_TIMEOUT = 0.25
FIXTURE_STOP_TIMEOUT = 2.0
PROCESS_TERM_GRACE = 1.0
PROCESS_STOP_TIMEOUT = 4.0
PROCESS_POLL_INTERVAL = 0.02
SCAN_CHUNK_SIZE = 64 * 1024
SYS_PIDFD_SEND_SIGNAL = 424
SYS_PIDFD_OPEN = 434


class ProcessIdentity(NamedTuple):
    pid: int
    state: str
    pgrp: int
    session: int
    starttime: int
    pidfd: int | None = None


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.restype = ctypes.c_long


def _linux_syscall(number: int, *args) -> int:
    result = _LIBC.syscall(number, *args)
    if result == -1:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))
    return result


def _pidfd_open(pid: int) -> int:
    opener = getattr(os, "pidfd_open", None)
    if opener is not None:
        return opener(pid)
    return _linux_syscall(SYS_PIDFD_OPEN, pid, 0)


def _pidfd_send_signal(pidfd: int, signum: int) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is not None:
        sender(pidfd, signum)
        return
    _linux_syscall(
        SYS_PIDFD_SEND_SIGNAL,
        pidfd,
        signum,
        ctypes.c_void_p(),
        0,
    )


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
        self.handler_condition = threading.Condition(self.lock)
        self.active_handlers = 0
        self.chat_attempts: list[dict] = []
        self.errors: list[str] = []

    def record_error(self, code: str) -> None:
        with self.lock:
            self.errors.append(code)

    def handler_started(self) -> None:
        with self.handler_condition:
            self.active_handlers += 1

    def handler_finished(self) -> None:
        with self.handler_condition:
            self.active_handlers -= 1
            if self.active_handlers == 0:
                self.handler_condition.notify_all()

    def wait_for_handlers(self, timeout: float) -> bool:
        with self.handler_condition:
            return self.handler_condition.wait_for(
                lambda: self.active_handlers == 0,
                timeout=timeout,
            )

    def snapshot(self) -> tuple[list[dict], list[str]]:
        with self.lock:
            return [dict(attempt) for attempt in self.chat_attempts], list(self.errors)

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
            return attempt

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(FIXTURE_READ_TIMEOUT)

            def log_message(self, format: str, *args: object) -> None:
                return

            def send_json(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

            def do_GET(self) -> None:
                if self.path.endswith("/models"):
                    self.send_json(200, {"object": "list", "data": [{"id": "aeon-ultimate", "object": "model"}]})
                else:
                    self.send_json(404, {"error": {"message": "fixture route"}})

            def do_POST(self) -> None:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None or re.fullmatch(r"[0-9]+", raw_length) is None:
                    fixture.record_error("fixture_invalid_content_length")
                    self.close_connection = True
                    return
                size = int(raw_length)
                if size > FIXTURE_BODY_LIMIT:
                    fixture.record_error("fixture_invalid_content_length")
                    self.close_connection = True
                    return
                try:
                    raw = self.rfile.read(size)
                except TimeoutError:
                    fixture.record_error("fixture_body_timeout")
                    self.close_connection = True
                    return
                except OSError:
                    fixture.record_error("fixture_body_read_error")
                    self.close_connection = True
                    return
                if len(raw) != size:
                    fixture.record_error("fixture_short_body")
                    self.close_connection = True
                    return
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    fixture.record_error("malformed_upstream_request")
                    self.close_connection = True
                    return
                if self.path.endswith("/embeddings"):
                    values = body.get("input")
                    count = len(values) if isinstance(values, list) else 1
                    self.send_json(200, {"object": "list", "data": [{"object": "embedding", "index": i, "embedding": [1.0] + [0.0] * 255} for i in range(count)], "model": "fixture", "usage": {"prompt_tokens": 1, "total_tokens": 1}})
                    return
                if not self.path.endswith("/chat/completions") or not isinstance(body, dict):
                    fixture.record_error("unexpected_upstream_route")
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


class FixtureHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], fixture: Fixture):
        self.fixture = fixture
        super().__init__(server_address, handler)

    def process_request(self, request, client_address) -> None:
        self.fixture.handler_started()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.fixture.handler_finished()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.fixture.handler_finished()

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if not isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            self.fixture.record_error(f"handler_crash:{type(exc).__name__}")


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


def _read_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError):
        return None
    left = raw.find("(")
    right = raw.rfind(")")
    fields = raw[right + 1 :].split() if 0 < left < right else []
    if len(fields) < 20:
        raise OSError(errno.EIO, "invalid_proc_stat")
    parsed_pid = int(raw[:left].strip())
    if parsed_pid != pid:
        raise OSError(errno.ESTALE, "proc_pid_changed")
    return ProcessIdentity(
        pid=parsed_pid,
        state=fields[0],
        pgrp=int(fields[2]),
        session=int(fields[3]),
        starttime=int(fields[19]),
    )


def _same_process(left: ProcessIdentity | None, right: ProcessIdentity) -> bool:
    return left is not None and (
        left.pid,
        left.starttime,
        left.pgrp,
        left.session,
    ) == (right.pid, right.starttime, right.pgrp, right.session)


def capture_spawn_identity(pid: int) -> ProcessIdentity:
    before = _read_process_identity(pid)
    if before is None:
        raise ProcessLookupError(pid)
    pidfd = _pidfd_open(pid)
    after = _read_process_identity(pid)
    if not _same_process(after, before):
        os.close(pidfd)
        raise OSError(errno.ESTALE, "spawn_identity_changed")
    if before.pgrp != pid or before.session != pid:
        os.close(pidfd)
        raise RuntimeError("proxy_not_session_leader")
    return ProcessIdentity(
        pid=before.pid,
        state=before.state,
        pgrp=before.pgrp,
        session=before.session,
        starttime=before.starttime,
        pidfd=pidfd,
    )


def process_identity_is_live(pid: int) -> bool:
    current = _read_process_identity(pid)
    return current is not None and current.state not in {"X", "Z"}


def _child_exited(pid: int) -> bool:
    try:
        return (
            os.waitid(
                os.P_PID,
                pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            is not None
        )
    except ChildProcessError as exc:
        raise RuntimeError("proxy_identity_reaped") from exc


def _producer_sets(
    identity: ProcessIdentity,
) -> tuple[dict[int, ProcessIdentity], dict[int, ProcessIdentity]]:
    group: dict[int, ProcessIdentity] = {}
    session: dict[int, ProcessIdentity] = {}
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            current = _read_process_identity(int(entry.name))
            if current is None or current.state in {"X", "Z"}:
                continue
            if current.pgrp == identity.pgrp:
                group[current.pid] = current
            if current.session == identity.session:
                session[current.pid] = current
    return group, session


def _signal_group(identity: ProcessIdentity, signum: int) -> bool:
    if not _same_process(_read_process_identity(identity.pid), identity):
        return False
    try:
        os.killpg(identity.pgrp, signum)
        return True
    except ProcessLookupError:
        return False


def _signal_identity(identity: ProcessIdentity, signum: int) -> bool:
    try:
        pidfd = _pidfd_open(identity.pid)
    except ProcessLookupError:
        return False
    try:
        if not _same_process(_read_process_identity(identity.pid), identity):
            return False
        _pidfd_send_signal(pidfd, signum)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(pidfd)


def _wait_for_quiescence(
    identity: ProcessIdentity,
    deadline: float,
) -> tuple[bool, dict[int, ProcessIdentity], dict[int, ProcessIdentity]]:
    while True:
        root_exited = _child_exited(identity.pid)
        group, session = _producer_sets(identity)
        if root_exited and not session:
            return True, group, session
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, group, session
        time.sleep(min(PROCESS_POLL_INTERVAL, remaining))


def wait_port(port: int, proc: subprocess.Popen[bytes], deadline: float) -> None:
    while time.monotonic() < deadline:
        if _child_exited(proc.pid):
            fail("proxy_exit_before_ready")
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


def _stable_error(exc: Exception, fallback: str) -> dict[str, str]:
    code = fallback
    if isinstance(exc, OSError) and exc.errno is not None:
        code = errno.errorcode.get(exc.errno, fallback)
    elif isinstance(exc, RuntimeError) and re.fullmatch(r"[a-z0-9_:=-]+", str(exc)):
        code = str(exc)
    return {"class": type(exc).__name__, "code": code}


def stop(
    proc: subprocess.Popen[bytes] | None,
    identity: ProcessIdentity | None = None,
) -> dict[str, object]:
    cleanup: dict[str, object] = {
        "proxy_exited": proc is None,
        "unexpected_exit": False,
        "graceful_stop": False,
        "forced_kill": False,
        "term_sent": False,
        "kill_sent": False,
        "spawn_identity_captured": identity is not None,
        "group_quiesced": proc is None,
        "session_quiesced": proc is None,
        "residual_producer_count_before_kill": 0,
        "residual_producer_count_final": 0,
    }
    if proc is None:
        return cleanup

    deadline = time.monotonic() + PROCESS_STOP_TIMEOUT
    if identity is None:
        try:
            identity = capture_spawn_identity(proc.pid)
        except Exception as exc:
            cleanup["stop_error"] = _stable_error(exc, "spawn_identity_missing")
            return cleanup
    cleanup["spawn_identity"] = {
        "pid": identity.pid,
        "starttime": identity.starttime,
        "pgid": identity.pgrp,
        "session": identity.session,
    }

    group: dict[int, ProcessIdentity] = {}
    session_members: dict[int, ProcessIdentity] = {}
    quiesced = False
    try:
        cleanup["unexpected_exit"] = _child_exited(identity.pid)
        group, session_members = _producer_sets(identity)
        cleanup["term_sent"] = _signal_group(identity, signal.SIGTERM)
        for member in session_members.values():
            if member.pgrp != identity.pgrp:
                cleanup["term_sent"] = (
                    _signal_identity(member, signal.SIGTERM)
                    or cleanup["term_sent"]
                )
        term_deadline = min(deadline, time.monotonic() + PROCESS_TERM_GRACE)
        quiesced, group, session_members = _wait_for_quiescence(
            identity, term_deadline
        )
    except Exception as exc:
        cleanup["stop_error"] = _stable_error(exc, "proxy_sigterm_failed")

    if not quiesced:
        cleanup["forced_kill"] = True
        cleanup["residual_producer_count_before_kill"] = len(session_members)
        try:
            cleanup["kill_sent"] = _signal_group(identity, signal.SIGKILL)
            for member in session_members.values():
                cleanup["kill_sent"] = (
                    _signal_identity(member, signal.SIGKILL)
                    or cleanup["kill_sent"]
                )
        except Exception as exc:
            cleanup.setdefault(
                "stop_error", _stable_error(exc, "proxy_sigkill_failed")
            )
        try:
            quiesced, group, session_members = _wait_for_quiescence(
                identity, deadline
            )
        except Exception as exc:
            cleanup.setdefault(
                "stop_error", _stable_error(exc, "proxy_quiescence_failed")
            )

    cleanup["group_quiesced"] = quiesced and not group
    cleanup["session_quiesced"] = quiesced and not session_members
    returncode = None
    try:
        if _child_exited(identity.pid):
            returncode = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        else:
            cleanup.setdefault(
                "stop_error",
                {"class": "TimeoutExpired", "code": "proxy_reap_timeout"},
            )
    except Exception as exc:
        cleanup.setdefault("stop_error", _stable_error(exc, "proxy_reap_failed"))

    try:
        final_group, final_session = _producer_sets(identity)
        cleanup["group_quiesced"] = not final_group
        cleanup["session_quiesced"] = not final_session
        cleanup["residual_producer_count_final"] = len(final_session)
    except Exception as exc:
        cleanup["group_quiesced"] = False
        cleanup["session_quiesced"] = False
        cleanup.setdefault(
            "stop_error", _stable_error(exc, "proxy_final_quiescence_failed")
        )
    finally:
        if identity.pidfd is not None:
            os.close(identity.pidfd)

    cleanup["returncode"] = returncode
    cleanup["proxy_exited"] = returncode is not None
    cleanup["graceful_stop"] = (
        cleanup["proxy_exited"]
        and returncode == 0
        and not cleanup["unexpected_exit"]
        and not cleanup["forced_kill"]
        and cleanup["spawn_identity_captured"]
        and cleanup["group_quiesced"]
        and cleanup["session_quiesced"]
        and "stop_error" not in cleanup
    )
    return cleanup


def stop_fixture_server(
    server: FixtureHTTPServer | None,
    server_thread: threading.Thread | None,
    fixture: Fixture,
) -> dict[str, object]:
    deadline = time.monotonic() + FIXTURE_STOP_TIMEOUT
    cleanup_errors: list[dict[str, str]] = []
    cleanup: dict[str, object] = {
        "server_stopped": server is None,
        "shutdown_stopped": True,
        "unexpected_exit": False,
        "handlers_quiesced": False,
        "errors": cleanup_errors,
    }
    shutdown_thread = None
    shutdown_errors: list[dict[str, str]] = []
    if server is not None:
        cleanup["unexpected_exit"] = server_thread is None or not server_thread.is_alive()
        if server_thread is not None and server_thread.is_alive():
            def shutdown() -> None:
                try:
                    server.shutdown()
                except Exception as exc:
                    shutdown_errors.append(
                        _stable_error(exc, "fixture_shutdown_failed")
                    )

            shutdown_thread = threading.Thread(target=shutdown, daemon=True)
            shutdown_thread.start()
            shutdown_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if shutdown_thread.is_alive():
                cleanup_errors.append(
                    {"class": "TimeoutError", "code": "fixture_shutdown_timeout"}
                )
        try:
            server.server_close()
        except Exception as exc:
            cleanup_errors.append(_stable_error(exc, "fixture_close_failed"))
        if server_thread is not None:
            try:
                server_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            except RuntimeError as exc:
                cleanup_errors.append(_stable_error(exc, "fixture_join_failed"))
            if server_thread.is_alive():
                cleanup_errors.append(
                    {"class": "TimeoutError", "code": "fixture_join_timeout"}
                )
        if shutdown_thread is not None and shutdown_thread.is_alive():
            shutdown_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if shutdown_thread is not None and not shutdown_thread.is_alive():
            cleanup_errors.extend(shutdown_errors)
        cleanup["shutdown_stopped"] = (
            shutdown_thread is None or not shutdown_thread.is_alive()
        )
        cleanup["server_stopped"] = server_thread is None or not server_thread.is_alive()
    cleanup["handlers_quiesced"] = fixture.wait_for_handlers(
        timeout=max(0.0, deadline - time.monotonic())
    )
    if not cleanup["handlers_quiesced"]:
        cleanup_errors.append(
            {"class": "TimeoutError", "code": "fixture_handler_timeout"}
        )
    return cleanup


def _stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _require_stable_stat(
    before: os.stat_result,
    after: os.stat_result,
    code: str,
) -> None:
    if _stat_fingerprint(before) != _stat_fingerprint(after):
        raise OSError(errno.ESTALE, code)


def sensitive_marker_count(root: Path, markers: tuple[bytes, ...]) -> int:
    if not markers or any(not isinstance(marker, bytes) or not marker for marker in markers):
        raise ValueError("privacy_scan_invalid_marker")
    common_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_flags = common_flags | os.O_DIRECTORY
    file_flags = common_flags | os.O_NONBLOCK

    def scan_file(parent_fd: int, name: str, before: os.stat_result) -> int:
        fd = os.open(name, file_flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "privacy_scan_non_regular")
            _require_stable_stat(before, opened, "privacy_scan_file_replaced")
            remaining = opened.st_size
            tails = {marker: b"" for marker in markers}
            count = 0
            while remaining:
                chunk = os.read(fd, min(SCAN_CHUNK_SIZE, remaining))
                if not chunk:
                    raise OSError(errno.ESTALE, "privacy_scan_file_truncated")
                remaining -= len(chunk)
                for marker in markers:
                    combined = tails[marker] + chunk
                    count += combined.count(marker)
                    tails[marker] = (
                        combined[-(len(marker) - 1) :] if len(marker) > 1 else b""
                    )
            if os.read(fd, 1):
                raise OSError(errno.ESTALE, "privacy_scan_file_grew")
            _require_stable_stat(
                opened, os.fstat(fd), "privacy_scan_file_mutated"
            )
            parent_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            _require_stable_stat(
                opened, parent_after, "privacy_scan_file_name_replaced"
            )
            return count
        finally:
            os.close(fd)

    def scan_directory(fd: int, before: os.stat_result) -> int:
        total = 0
        with os.scandir(fd) as entries:
            for entry in entries:
                entry_before = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(entry_before.st_mode):
                    raise OSError(errno.ELOOP, "privacy_scan_symlink")
                if stat.S_ISDIR(entry_before.st_mode):
                    child_fd = os.open(entry.name, directory_flags, dir_fd=fd)
                    try:
                        opened = os.fstat(child_fd)
                        if not stat.S_ISDIR(opened.st_mode):
                            raise OSError(
                                errno.ENOTDIR, "privacy_scan_directory_replaced"
                            )
                        _require_stable_stat(
                            entry_before,
                            opened,
                            "privacy_scan_directory_replaced",
                        )
                        total += scan_directory(child_fd, opened)
                        _require_stable_stat(
                            opened,
                            os.fstat(child_fd),
                            "privacy_scan_directory_mutated",
                        )
                        parent_after = os.stat(
                            entry.name,
                            dir_fd=fd,
                            follow_symlinks=False,
                        )
                        _require_stable_stat(
                            opened,
                            parent_after,
                            "privacy_scan_directory_name_replaced",
                        )
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(entry_before.st_mode):
                    total += scan_file(fd, entry.name, entry_before)
                else:
                    raise OSError(errno.EINVAL, "privacy_scan_non_regular")
        _require_stable_stat(
            before, os.fstat(fd), "privacy_scan_directory_mutated"
        )
        return total

    root_fd = os.open(root, directory_flags)
    try:
        root_before = os.fstat(root_fd)
        if not stat.S_ISDIR(root_before.st_mode):
            raise OSError(errno.ENOTDIR, "privacy_scan_root_not_directory")
        total = scan_directory(root_fd, root_before)
        _require_stable_stat(
            root_before,
            os.stat(root, follow_symlinks=False),
            "privacy_scan_root_replaced",
        )
        return total
    finally:
        os.close(root_fd)


def acceptance_errors(summary: dict) -> list[str]:
    errors: list[str] = []
    if summary.get("execution_error") is not None:
        errors.append("execution_failed")
    errors.extend(summary.get("fixture_errors", []))

    attempts = summary.get("attempts", [])
    salvages = [attempt for attempt in attempts if attempt["salvage_material_present"]]
    fresh_attempts = [
        attempt
        for attempt in attempts
        if attempt["phase"] == "fresh" and not attempt["salvage_material_present"]
    ]
    if len(salvages) != 1:
        errors.append("salvage_count")
    if not fresh_attempts:
        errors.append("fresh_request_missing")
    for attempt in attempts:
        number = attempt["number"]
        if attempt["salvage_material_present"]:
            if attempt["phase"] != "positive":
                errors.append(f"salvage_phase:{number}")
            if attempt["thinking_budget"] != 0:
                errors.append(f"salvage_thinking_budget:{number}")
            if not attempt["private_prefix_present"]:
                errors.append(f"salvage_private_prefix:{number}")
            if attempt["loop_tail_present"]:
                errors.append(f"salvage_loop_tail:{number}")
            continue
        if attempt["private_prefix_present"] or attempt["loop_tail_present"]:
            prefix = (
                "fresh_request_replayed_private_material"
                if attempt["phase"] == "fresh"
                else "non_salvage_replayed_private_material"
            )
            errors.append(f"{prefix}:{number}")
        if (
            attempt["phase"] == "fresh"
            and attempt["thinking_budget"] != THINKING_BUDGET
        ):
            errors.append(f"fresh_thinking_budget:{number}")

    if not summary.get("positive_pass"):
        errors.append("positive_client_failed")
    if not summary.get("fresh_negative_pass"):
        errors.append("fresh_client_failed")

    process_cleanup = summary.get("process_cleanup", {})
    if process_cleanup.get("unexpected_exit"):
        errors.append("proxy_unexpected_exit")
    if process_cleanup.get("forced_kill"):
        errors.append("proxy_forced_kill")
    if not process_cleanup.get("spawn_identity_captured"):
        errors.append("proxy_spawn_identity_missing")
    if process_cleanup.get("residual_producer_count_before_kill"):
        errors.append("proxy_residual_producer_detected")
    if process_cleanup.get("residual_producer_count_final"):
        errors.append("proxy_residual_producer_final")
    if not process_cleanup.get("group_quiesced"):
        errors.append("proxy_group_not_quiesced")
    if not process_cleanup.get("session_quiesced"):
        errors.append("proxy_session_not_quiesced")
    if not process_cleanup.get("proxy_exited"):
        errors.append("proxy_not_exited")
    if not process_cleanup.get("graceful_stop") or process_cleanup.get("stop_error"):
        errors.append("proxy_not_gracefully_stopped")

    fixture_cleanup = summary.get("fixture_cleanup", {})
    if fixture_cleanup.get("unexpected_exit"):
        errors.append("fixture_server_unexpected_exit")
    if not fixture_cleanup.get("shutdown_stopped"):
        errors.append("fixture_shutdown_not_stopped")
    if not fixture_cleanup.get("server_stopped"):
        errors.append("fixture_server_not_stopped")
    if not fixture_cleanup.get("handlers_quiesced"):
        errors.append("fixture_handlers_not_quiesced")
    if fixture_cleanup.get("errors"):
        errors.append("fixture_cleanup_failed")

    port_cleanup = summary.get("port_cleanup", {})
    if not port_cleanup or not all(port_cleanup.values()):
        errors.append("ports_not_rebindable")
    if summary.get("scan_errors"):
        errors.append("privacy_scan_failed")
    elif summary.get("sensitive_marker_leak_count_all_fixture_files") != 0:
        errors.append("fixture_marker_leak")
    return errors


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
        "execution_error": None,
    }
    proc = None
    process_identity = None
    server = None
    server_thread = None
    try:
        config, hashes = isolated_config(candidate, root, fake_port, guard_port)
        summary.update(hashes)
        server = FixtureHTTPServer(("127.0.0.1", fake_port), fixture.handler(), fixture)
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        server_thread.start()
        env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"), "XDG_CACHE_HOME": str(root / "home" / ".cache"), "XDG_CONFIG_HOME": str(root / "home" / ".config"), "XDG_DATA_HOME": str(root / "home" / ".local" / "share"), "PATH": os.environ["PATH"]}
        with (root / "proxy.log").open("wb") as log:
            proc = subprocess.Popen([str(binary), "--config", str(config), "--guardian-runtime-dir", str(root / "guardian-runtime")], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            process_identity = capture_spawn_identity(proc.pid)
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
    except Exception as exc:
        summary["execution_error"] = _stable_error(exc, "execution_failed")

    summary["process_cleanup"] = stop(proc, process_identity)
    summary["fixture_cleanup"] = stop_fixture_server(server, server_thread, fixture)
    attempts, fixture_errors = fixture.snapshot()
    summary["attempts"] = attempts
    summary["fixture_errors"] = fixture_errors
    summary["salvage_count"] = sum(
        attempt["salvage_material_present"] for attempt in attempts
    )
    summary["fresh_request_count"] = sum(
        attempt["phase"] == "fresh" for attempt in attempts
    )

    positive = summary.get("positive_client")
    fresh = summary.get("fresh_client")
    summary["positive_pass"] = (
        isinstance(positive, dict)
        and positive["status"] == 200
        and positive["nonempty"]
        and positive["expected_final"]
        and positive["reasoning_marker_leak_count"] == 0
        and positive["private_prefix_marker_leak_count"] == 0
    )
    summary["fresh_negative_pass"] = (
        isinstance(fresh, dict)
        and fresh["status"] == 200
        and fresh["nonempty"]
        and fresh["expected_final"]
        and fresh["reasoning_marker_leak_count"] == 0
        and fresh["private_prefix_marker_leak_count"] == 0
    )
    summary["port_cleanup"] = {
        "guard_rebindable": _rebindable(guard_port),
        "fake_rebindable": _rebindable(fake_port),
    }

    lifecycle_final = (
        summary["process_cleanup"]["proxy_exited"]
        and summary["process_cleanup"]["group_quiesced"]
        and summary["process_cleanup"]["session_quiesced"]
        and summary["process_cleanup"]["residual_producer_count_final"] == 0
        and summary["fixture_cleanup"]["server_stopped"]
        and summary["fixture_cleanup"]["shutdown_stopped"]
        and summary["fixture_cleanup"]["handlers_quiesced"]
    )
    if lifecycle_final:
        try:
            summary["sensitive_marker_leak_count_all_fixture_files"] = sensitive_marker_count(
                root,
                tuple(marker.encode() for marker in sensitive_markers),
            )
            summary["scan_errors"] = 0
        except Exception as exc:
            summary["sensitive_marker_leak_count_all_fixture_files"] = None
            summary["scan_errors"] = 1
            summary["scan_error"] = _stable_error(exc, "privacy_scan_failed")
    else:
        summary["sensitive_marker_leak_count_all_fixture_files"] = None
        summary["scan_errors"] = 1
        summary["scan_error"] = {
            "class": "FixtureLifecycleError",
            "code": "fixture_not_final",
        }

    errors = acceptance_errors(summary)
    summary["acceptance_errors"] = errors
    summary["result"] = "PASS" if not errors else "FAIL"
    if errors:
        summary["error_code"] = errors[0]
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
