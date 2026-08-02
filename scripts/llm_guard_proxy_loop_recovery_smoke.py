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
import selectors
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
CLIENT_RESPONSE_LIMIT = 4 * 1024 * 1024
CLIENT_DEADLINE = 45.0
CLIENT_READ_CHUNK = 16 * 1024
PROBE_HEARTBEAT_INTERVAL_SECS = 1
PROCESS_TERM_GRACE = 1.0
PROCESS_STOP_TIMEOUT = 4.0
PROCESS_POLL_INTERVAL = 0.02
SUPERVISOR_START_TIMEOUT = 5.0
SUPERVISOR_CONTROL_TIMEOUT = 180.0
SUPERVISOR_CLEANUP_TIMEOUT = PROCESS_STOP_TIMEOUT + 2.0
SUPERVISOR_EMERGENCY_TIMEOUT = PROCESS_STOP_TIMEOUT * 2 + 2.0
SUPERVISOR_MESSAGE_LIMIT = 16 * 1024
SCAN_CHUNK_SIZE = 64 * 1024
SCAN_MAX_ENTRIES = 2_048
SCAN_MAX_FILE_BYTES = 64 * 1024 * 1024
SCAN_MAX_TOTAL_BYTES = 64 * 1024 * 1024
SCAN_TIMEOUT = 6.0
SYS_PIDFD_SEND_SIGNAL = 424
SYS_PIDFD_OPEN = 434
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
SYSTEMD_RUN_BIN = "/usr/bin/systemd-run"
SYSTEMCTL_BIN = "/usr/bin/systemctl"
SCOPE_COLLECT_TIMEOUT = 2.0
CGROUP_ROOT = Path("/sys/fs/cgroup")
OFFLINE_SELF_TEST_TIMEOUT = 10.0
OFFLINE_SELF_TEST_OUTPUT_LIMIT = 16 * 1024
OFFLINE_SELF_TEST_ARGV = ("self-test", "post-await-no-replay")


class ProcessIdentity(NamedTuple):
    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int
    starttime: int
    pidfd: int | None = None
    pidfd_errno: int | None = None


class ExecutableIdentity(NamedTuple):
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


class ScopeFence(NamedTuple):
    unit: str
    control_group: str
    events_fd: int
    kill_fd: int
    worker_identity: ProcessIdentity


class SupervisorHandle(NamedTuple):
    pid: int
    identity: ProcessIdentity
    control: socket.socket
    started_receipt: dict[str, object]
    scope: ScopeFence


class SupervisorStartError(RuntimeError):
    def __init__(
        self,
        error: dict[str, str],
        cleanup: dict[str, object],
        subreaper_enabled: bool,
        exclusive_supervisor: bool,
    ) -> None:
        super().__init__(error["code"])
        self.error = error
        self.cleanup = cleanup
        self.subreaper_enabled = subreaper_enabled
        self.exclusive_supervisor = exclusive_supervisor


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.restype = ctypes.c_long
_LIBC.prctl.restype = ctypes.c_int
_EXCLUSIVE_SUPERVISOR_PID: int | None = None
_SUPERVISOR_TEST_FAILPOINT: str | None = None


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


def _enable_child_subreaper() -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "child_subreaper_unsupported")
    if _LIBC.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        value = ctypes.get_errno()
        raise OSError(value, "child_subreaper_setup_failed")
    enabled = ctypes.c_int()
    if _LIBC.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(enabled), 0, 0, 0) != 0:
        value = ctypes.get_errno()
        raise OSError(value, "child_subreaper_verify_failed")
    if enabled.value != 1:
        raise OSError(errno.EIO, "child_subreaper_not_enabled")


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


def _executable_identity_fd(fd: int) -> ExecutableIdentity:
    before = os.fstat(fd)
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    for block in iter(lambda: os.read(fd, 1024 * 1024), b""):
        digest.update(block)
    after = os.fstat(fd)
    _require_stable_stat(before, after, "binary_identity_drift")
    return ExecutableIdentity(*_stat_fingerprint(after), digest.hexdigest())


def executable_identity(path: Path, *, nofollow: bool = True) -> ExecutableIdentity:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if nofollow:
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        return _executable_identity_fd(fd)
    finally:
        os.close(fd)


def require_executable_identity(
    path: Path,
    expected: ExecutableIdentity,
    code: str,
    *,
    nofollow: bool = True,
) -> ExecutableIdentity:
    try:
        current = executable_identity(path, nofollow=nofollow)
    except Exception:
        fail(code)
    require(current == expected, code)
    return current


_OFFLINE_PHASE_FIELDS = frozenset(
    {
        "pre_await_gate_ns",
        "recovery_await_entered_ns",
        "body_emitted_ns",
        "client_ack_ns",
        "recovery_await_completed_ns",
        "control_replay_authorized_ns",
        "post_await_committed_ns",
    }
)
_OFFLINE_ARM_FIELDS = frozenset(
    {
        "ordered_roles",
        "product_roles",
        "attempt_count",
        "fixture_rejected_count",
        "request_claims",
        "rejected_request_claims",
        "recovery_replay_claims",
        "rejected_recovery_replay_claims",
        "rejected_physical_attempts",
        "rejected_readiness_probes",
        "business_count",
        "probe_count",
        "same_payload",
        "first_chunk_stall",
        "first_byte_wait_ms",
        "client_observed_heartbeat",
        "done_observed",
        "terminal_error_observed",
        "eof_observed",
        "post_await_committed",
        "phases",
        "loopback_only",
        "cleanup_complete",
    }
)


def _exact_fields(value: object, fields: frozenset[str], code: str) -> dict:
    require(type(value) is dict and value.keys() == fields, code)
    assert isinstance(value, dict)
    return value


def _validate_offline_arm(arm: object, *, committed: bool) -> None:
    arm = _exact_fields(arm, _OFFLINE_ARM_FIELDS, "offline_self_test_arm_schema")
    phases = _exact_fields(
        arm["phases"], _OFFLINE_PHASE_FIELDS, "offline_self_test_phase_schema"
    )
    int_fields = _OFFLINE_PHASE_FIELDS | {
        "attempt_count",
        "fixture_rejected_count",
        "request_claims",
        "rejected_request_claims",
        "recovery_replay_claims",
        "rejected_recovery_replay_claims",
        "rejected_physical_attempts",
        "rejected_readiness_probes",
        "business_count",
        "probe_count",
        "first_byte_wait_ms",
    }
    require(
        all(
            type((phases if field in phases else arm)[field]) is int
            for field in int_fields
        ),
        "offline_self_test_integer_type",
    )
    require(
        all(
            type(arm[field]) is bool
            for field in (
                "same_payload",
                "first_chunk_stall",
                "client_observed_heartbeat",
                "done_observed",
                "terminal_error_observed",
                "eof_observed",
                "post_await_committed",
                "loopback_only",
                "cleanup_complete",
            )
        ),
        "offline_self_test_boolean_type",
    )
    require(type(arm["ordered_roles"]) is list, "offline_self_test_role_type")
    require(type(arm["product_roles"]) is list, "offline_self_test_role_type")
    zero_counters = (
        "fixture_rejected_count",
        "rejected_request_claims",
        "rejected_recovery_replay_claims",
        "rejected_physical_attempts",
        "rejected_readiness_probes",
    )
    require(
        all(arm[field] == 0 for field in zero_counters),
        "offline_self_test_rejection_counter",
    )
    require(
        arm["request_claims"] == 1
        and arm["same_payload"] is True
        and arm["first_chunk_stall"] is True
        and arm["first_byte_wait_ms"] > 0
        and arm["eof_observed"] is True
        and arm["loopback_only"] is True
        and arm["cleanup_complete"] is True,
        "offline_self_test_common_invariant",
    )
    pre = phases["pre_await_gate_ns"]
    entered = phases["recovery_await_entered_ns"]
    completed = phases["recovery_await_completed_ns"]
    if not committed:
        require(
            arm["ordered_roles"] == ["business", "recovery_probe", "business"]
            and arm["product_roles"]
            == ["business", "readiness_probe", "recovery_replay"]
            and arm["attempt_count"] == 3
            and arm["business_count"] == 2
            and arm["probe_count"] == 1
            and arm["recovery_replay_claims"] == 1
            and arm["client_observed_heartbeat"] is False
            and arm["done_observed"] is True
            and arm["terminal_error_observed"] is False
            and arm["post_await_committed"] is False
            and phases["body_emitted_ns"] == 0
            and phases["client_ack_ns"] == 0
            and 0 < pre < entered < completed
            < phases["control_replay_authorized_ns"]
            and phases["post_await_committed_ns"] == 0,
            "offline_self_test_control_invariant",
        )
        return
    require(
        arm["ordered_roles"] == ["business"]
        and arm["product_roles"] == ["business"]
        and arm["attempt_count"] == 1
        and arm["business_count"] == 1
        and arm["probe_count"] == 0
        and arm["recovery_replay_claims"] == 0
        and arm["client_observed_heartbeat"] is True
        and arm["done_observed"] is False
        and arm["terminal_error_observed"] is True
        and arm["post_await_committed"] is True
        and 0 < pre < entered < phases["body_emitted_ns"]
        < phases["client_ack_ns"] < completed
        < phases["post_await_committed_ns"]
        and phases["control_replay_authorized_ns"] == 0,
        "offline_self_test_committed_invariant",
    )


def validate_offline_self_test_receipt(receipt: object) -> None:
    receipt = _exact_fields(
        receipt,
        frozenset(
            {"self_test", "status", "control", "committed", "same_payload_across_arms"}
        ),
        "offline_self_test_receipt_schema",
    )
    require(
        receipt["self_test"] == "post-await-no-replay"
        and receipt["status"] == "passed"
        and receipt["same_payload_across_arms"] is True,
        "offline_self_test_receipt_status",
    )
    _validate_offline_arm(receipt["control"], committed=False)
    _validate_offline_arm(receipt["committed"], committed=True)


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=PROCESS_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=PROCESS_STOP_TIMEOUT)


def run_offline_self_test(binary: Path) -> tuple[dict, ExecutableIdentity]:
    fd = os.open(binary, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    proc: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    streams: tuple[object, ...] = ()
    try:
        identity = _executable_identity_fd(fd)
        proc = subprocess.Popen(
            [str(binary), *OFFLINE_SELF_TEST_ARGV],
            executable=f"/proc/self/fd/{fd}",
            pass_fds=(fd,),
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        streams = (proc.stdout, proc.stderr)
        buffers = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + OFFLINE_SELF_TEST_TIMEOUT
        while selector.get_map():
            remaining = deadline - time.monotonic()
            require(remaining > 0, "offline_self_test_timeout")
            events = selector.select(remaining)
            require(bool(events), "offline_self_test_timeout")
            for key, _ in events:
                output = buffers[key.fd]
                chunk = os.read(
                    key.fd,
                    min(4096, OFFLINE_SELF_TEST_OUTPUT_LIMIT + 1 - len(output)),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                require(
                    len(output) <= OFFLINE_SELF_TEST_OUTPUT_LIMIT,
                    "offline_self_test_output_limit",
                )
        remaining = deadline - time.monotonic()
        require(remaining > 0, "offline_self_test_timeout")
        returncode = proc.wait(timeout=remaining)
        require(returncode >= 0, "offline_self_test_signal")
        require(returncode == 0, "offline_self_test_nonzero")
        stdout, stderr = (bytes(buffers[stream.fileno()]) for stream in streams)
        require(stderr == b"", "offline_self_test_stderr")
        require(
            stdout.endswith(b"\n") and stdout.count(b"\n") == 1,
            "offline_self_test_stdout_contract",
        )
        try:
            receipt = json.loads(stdout[:-1])
        except (UnicodeDecodeError, json.JSONDecodeError):
            fail("offline_self_test_json")
        validate_offline_self_test_receipt(receipt)
        assert isinstance(receipt, dict)
        return receipt, identity
    finally:
        os.close(fd)
        selector.close()
        if proc is not None:
            _kill_and_reap(proc)
        for stream in streams:
            stream.close()


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
        shielded_output_marker: str,
        fresh_output_marker: str,
    ):
        self.reasoning_marker = reasoning_marker
        self.private_prefix_marker = private_prefix_marker
        self.fresh_input_marker = fresh_input_marker
        self.shielded_output_marker = shielded_output_marker
        self.fresh_output_marker = fresh_output_marker
        self.lock = threading.Lock()
        self.handler_condition = threading.Condition(self.lock)
        self.active_handlers = 0
        self.attempts: list[dict] = []
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
            return [dict(attempt) for attempt in self.attempts], list(self.errors)

    def record_attempt_event(self, number: int, field: str) -> None:
        with self.lock:
            self.attempts[number - 1][field] = time.monotonic_ns()

    def inspect_request(
        self,
        endpoint: str,
        body: dict,
        *,
        recovery_probe: bool = False,
    ) -> dict:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        messages = body.get("messages")
        message_contents = {
            message.get("content")
            for message in messages
            if isinstance(messages, list)
            and isinstance(message, dict)
            and isinstance(message.get("content"), str)
        } if isinstance(messages, list) else set()
        phase = (
            "generic_committed_stream"
            if endpoint == "completions"
            else "shielded_hold"
        )
        if endpoint == "chat_completions" and self.fresh_input_marker in message_contents:
            phase = "fresh"
        private_prefix_present = self.private_prefix_marker in text
        salvage_material = private_prefix_present and "Private bounded pre-loop reasoning notes" in text
        with self.lock:
            n = len(self.attempts) + 1
            phase_has_primary = any(
                item["phase"] == phase and item["role"] == "primary"
                for item in self.attempts
            )
            phase_has_salvage = any(
                item["phase"] == phase and item["role"] == "salvage"
                for item in self.attempts
            )
            role = (
                "recovery_probe"
                if recovery_probe
                else "primary"
                if endpoint == "completions"
                else "salvage"
                if salvage_material and not phase_has_salvage
                else "shadow"
                if phase_has_primary
                else "primary"
            )
            budget = effective_budget(body)
            attempt = {
                "number": n,
                "endpoint": endpoint,
                "phase": phase,
                "role": role,
                "admitted_monotonic_ns": time.monotonic_ns(),
                "thinking_budget": budget,
                "thinking_budget_canonical": budgets(body)[0],
                "thinking_budget_native": budgets(body)[1],
                "max_tokens": body.get("max_tokens"),
                "stream": body.get("stream") is True,
                "stream_usage": isinstance(body.get("stream_options"), dict)
                and body["stream_options"].get("include_usage") is True,
                "salvage_material_present": salvage_material,
                "private_prefix_present": private_prefix_present,
                "loop_tail_present": self.reasoning_marker in text,
                "variant": (
                    "cot-salvage"
                    if salvage_material
                    else "no-thinking"
                    if budget == 0
                    else "max-thinking"
                    if budget == THINKING_BUDGET
                    else "bounded-thinking"
                ),
            }
            self.attempts.append(attempt)
            if endpoint == "chat_completions" and role == "primary":
                if effective_budget(body) != THINKING_BUDGET:
                    self.errors.append(f"primary_thinking_budget:{phase}")
                if body.get("max_tokens") != EXPECTED_FIRST_MAX:
                    self.errors.append(f"primary_max_tokens:{phase}")
                if body.get("stream") is not True or not attempt["stream_usage"]:
                    self.errors.append(f"primary_shielded_sse_contract:{phase}")
            if endpoint == "completions" and body.get("stream") is not True:
                self.errors.append("generic_upstream_not_streaming")
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
                endpoint = (
                    "chat_completions"
                    if self.path.endswith("/chat/completions")
                    else "completions"
                    if self.path.endswith("/completions")
                    else None
                )
                if endpoint is None or not isinstance(body, dict):
                    fixture.record_error("unexpected_upstream_route")
                    self.send_json(404, {"error": {"message": "route"}})
                    return
                attempt = fixture.inspect_request(
                    endpoint,
                    body,
                    recovery_probe=self.headers.get("x-llm-guard-proxy-probe")
                    == "local-recovery",
                )
                if attempt["role"] == "recovery_probe":
                    self.send_json(
                        200,
                        {
                            "choices": [
                                {"message": {"role": "assistant", "content": "ready"}}
                            ]
                        },
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    if endpoint == "completions":
                        error = json.dumps(
                            {
                                "error": {
                                    "message": "fixture generic terminal failure",
                                    "type": "fixture_generic_terminal_failure",
                                    "code": "fixture_generic_terminal_failure",
                                }
                            },
                            separators=(",", ":"),
                        ).encode()
                        self.wfile.write(b": heartbeat fixture generic\n\n")
                        self.wfile.flush()
                        time.sleep(0.05)
                        self.wfile.write(b"event: error\ndata: " + error + b"\n\n")
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    elif (
                        attempt["role"] == "primary"
                        and attempt["phase"] == "shielded_hold"
                    ):
                        time.sleep(PROBE_HEARTBEAT_INTERVAL_SECS + 0.15)
                        fixture.record_attempt_event(
                            attempt["number"],
                            "upstream_first_event_monotonic_ns",
                        )
                        self.wfile.write(sse({"id": f"fixture-{attempt['number']}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"reasoning_content": f"{fixture.private_prefix_marker} derive the isolated invariant before answering\n"}, "finish_reason": None}]}))
                        self.wfile.flush()
                        repeated = fixture.reasoning_marker + " repeat loop line\n"
                        for _ in range(40):
                            self.wfile.write(sse({"id": f"fixture-{attempt['number']}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"reasoning_content": repeated}, "finish_reason": None}]}))
                            self.wfile.flush()
                    else:
                        text = fixture.shielded_output_marker if attempt["phase"] == "shielded_hold" and attempt["salvage_material_present"] else fixture.fresh_output_marker
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
    raw, heartbeat_replacements = re.subn(
        r"(?ms)^(\[heartbeat\]\n(?:(?!^\[).)*?^interval_secs = )\d+$",
        rf"\g<1>{PROBE_HEARTBEAT_INTERVAL_SECS}",
        raw,
        count=1,
    )
    require(heartbeat_replacements == 1, "fixture_heartbeat_interval_missing")
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
    require(
        derived["heartbeat"]["mode"] == "sse"
        and derived["heartbeat"]["interval_secs"] == PROBE_HEARTBEAT_INTERVAL_SECS,
        "fixture_heartbeat_not_enabled",
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
        "isolated_heartbeat_interval_secs": PROBE_HEARTBEAT_INTERVAL_SECS,
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
        ppid=int(fields[1]),
        pgrp=int(fields[2]),
        session=int(fields[3]),
        starttime=int(fields[19]),
    )


def _same_process(left: ProcessIdentity | None, right: ProcessIdentity) -> bool:
    return left is not None and (left.pid, left.starttime) == (
        right.pid,
        right.starttime,
    )


def capture_process_identity(pid: int) -> ProcessIdentity:
    before = _read_process_identity(pid)
    if before is None:
        raise ProcessLookupError(pid)
    pidfd = None
    pidfd_errno = None
    try:
        pidfd = _pidfd_open(pid)
    except OSError as exc:
        pidfd_errno = exc.errno or errno.EIO
    try:
        after = _read_process_identity(pid)
    except Exception:
        if pidfd is not None:
            os.close(pidfd)
        raise
    if not _same_process(after, before):
        if pidfd is not None:
            os.close(pidfd)
        raise OSError(errno.ESTALE, "spawn_identity_changed")
    assert after is not None
    return ProcessIdentity(
        pid=after.pid,
        state=after.state,
        ppid=after.ppid,
        pgrp=after.pgrp,
        session=after.session,
        starttime=after.starttime,
        pidfd=pidfd,
        pidfd_errno=pidfd_errno,
    )


def capture_spawn_identity(pid: int) -> ProcessIdentity:
    identity = capture_process_identity(pid)
    if identity.pgrp != pid or identity.session != pid:
        if identity.pidfd is not None:
            os.close(identity.pidfd)
        raise RuntimeError("proxy_not_session_leader")
    return identity


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


def _process_table() -> dict[int, ProcessIdentity]:
    processes: dict[int, ProcessIdentity] = {}
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            current = _read_process_identity(int(entry.name))
            if current is not None:
                processes[current.pid] = current
    return processes


def _capture_direct_children() -> dict[int, ProcessIdentity]:
    parent = os.getpid()
    return {
        pid: identity
        for pid, identity in _process_table().items()
        if identity.ppid == parent
    }


def _owned_processes(
    root: ProcessIdentity | None,
    known: dict[int, ProcessIdentity],
    *,
    exclusive_supervisor: bool,
) -> dict[int, ProcessIdentity]:
    if exclusive_supervisor and _EXCLUSIVE_SUPERVISOR_PID != os.getpid():
        raise RuntimeError("exclusive_supervisor_scope_required")
    table = _process_table()
    owned: dict[int, ProcessIdentity] = {}
    for pid, remembered in known.items():
        current = table.get(pid)
        if _same_process(current, remembered):
            assert current is not None
            owned[pid] = current
    if root is not None:
        current_root = table.get(root.pid)
        if _same_process(current_root, root):
            assert current_root is not None
            owned[root.pid] = current_root

    if exclusive_supervisor:
        parent = os.getpid()
        for pid, current in table.items():
            if current.ppid == parent:
                owned[pid] = current

    changed = True
    while changed:
        changed = False
        for pid, current in table.items():
            if pid not in owned and current.ppid in owned:
                owned[pid] = current
                changed = True

    for pid, current in owned.items():
        remembered = known.get(pid)
        if remembered is not None and remembered.pidfd is not None:
            current = current._replace(
                pidfd=remembered.pidfd,
                pidfd_errno=remembered.pidfd_errno,
            )
            owned[pid] = current
        known[pid] = current
    return owned


def _reap_adopted(
    owned: dict[int, ProcessIdentity], root_pid: int | None
) -> None:
    parent = os.getpid()
    for identity in owned.values():
        if (
            (root_pid is not None and identity.pid == root_pid)
            or identity.ppid != parent
            or identity.state != "Z"
        ):
            continue
        current = _read_process_identity(identity.pid)
        if not _same_process(current, identity):
            continue
        assert current is not None
        if current.ppid != parent:
            continue
        try:
            os.waitpid(identity.pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _producer_sets(
    identity: ProcessIdentity,
) -> tuple[dict[int, ProcessIdentity], dict[int, ProcessIdentity]]:
    group: dict[int, ProcessIdentity] = {}
    session: dict[int, ProcessIdentity] = {}
    for current in _process_table().values():
        if current.state in {"X", "Z"}:
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
    close_pidfd = False
    try:
        if identity.pidfd is not None:
            pidfd = identity.pidfd
        else:
            pidfd = _pidfd_open(identity.pid)
            close_pidfd = True
    except ProcessLookupError:
        return False
    except OSError:
        current = _read_process_identity(identity.pid)
        if not _same_process(current, identity):
            return False
        assert current is not None
        if current.ppid != os.getpid():
            raise
        try:
            os.kill(identity.pid, signum)
        except ProcessLookupError:
            return False
        after = _read_process_identity(identity.pid)
        if after is not None and not _same_process(after, identity):
            raise OSError(errno.ESTALE, "owned_process_identity_changed")
        return True
    try:
        if not _same_process(_read_process_identity(identity.pid), identity):
            return False
        _pidfd_send_signal(pidfd, signum)
        return True
    except ProcessLookupError:
        return False
    finally:
        if close_pidfd:
            os.close(pidfd)


def _wait_for_quiescence(
    identity: ProcessIdentity,
    known: dict[int, ProcessIdentity],
    deadline: float,
    signum: int,
    signalled: set[tuple[int, int]],
    *,
    exclusive_supervisor: bool,
) -> tuple[
    bool,
    dict[int, ProcessIdentity],
    dict[int, ProcessIdentity],
    dict[int, ProcessIdentity],
    bool,
    Exception | None,
]:
    sent = False
    signal_error = None
    while True:
        owned = _owned_processes(
            identity, known, exclusive_supervisor=exclusive_supervisor
        )
        _reap_adopted(owned, identity.pid)
        owned = _owned_processes(
            identity, known, exclusive_supervisor=exclusive_supervisor
        )
        for member in owned.values():
            key = (member.pid, member.starttime)
            if member.state in {"X", "Z"} or key in signalled:
                continue
            try:
                member_sent = _signal_identity(member, signum)
                sent = member_sent or sent
                if member_sent:
                    signalled.add(key)
            except OSError as exc:
                signal_error = signal_error or exc
        root_exited = _child_exited(identity.pid)
        group, session = _producer_sets(identity)
        live_owned = {
            pid: member
            for pid, member in owned.items()
            if member.state not in {"X", "Z"}
        }
        if root_exited and not live_owned:
            return True, group, session, owned, sent, signal_error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, group, session, owned, sent, signal_error
        time.sleep(min(PROCESS_POLL_INTERVAL, remaining))


def wait_port(port: int, supervisor: SupervisorHandle, deadline: float) -> None:
    while time.monotonic() < deadline:
        current = _read_process_identity(supervisor.scope.worker_identity.pid)
        if (
            current is None
            or not _same_process(current, supervisor.scope.worker_identity)
            or current.state in {"X", "Z"}
        ):
            fail("supervisor_exit_before_ready")
        with socket.socket() as sock:
            sock.settimeout(0.15)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    fail("proxy_ready_timeout")


class _SSEFramer:
    """Incremental, chunk-agnostic SSE framing with content-free receipts."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.event_type: bytes | None = None
        self.data_lines: list[bytes] = []
        self.event_count = 0
        self.comment_count = 0
        self.heartbeat_count = 0
        self.final_count = 0
        self.error_count = 0
        self.done_count = 0
        self.prelude = False
        self.protocol_error: str | None = None
        self.final_payload: dict | None = None
        self.terminal_failure = False
        self.content_parts: list[str] = []
        self.terminal_seen = False

    def _fail(self, code: str) -> None:
        if self.protocol_error is None:
            self.protocol_error = code

    def _dispatch(self) -> None:
        if self.event_type is None and not self.data_lines:
            return
        event_type = self.event_type or b"message"
        data = b"\n".join(self.data_lines)
        self.event_type = None
        self.data_lines = []
        if self.terminal_seen:
            if (
                self.error_count == 1
                and self.done_count == 0
                and event_type == b"message"
                and data == b"[DONE]"
            ):
                self.event_count += 1
                self.done_count = 1
                return
            self._fail("sse_after_terminal")
            return
        prior_event = self.event_count > 0 or self.comment_count > 0 or self.prelude
        self.event_count += 1
        if data == b"[DONE]":
            self.done_count += 1
            self.terminal_seen = True
            self.prelude = True
            return
        try:
            payload = json.loads(data) if data else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._fail("sse_invalid_json")
            return
        if event_type == b"final":
            self.final_count += 1
            if prior_event:
                self.prelude = True
            if not isinstance(payload, dict):
                self._fail("sse_invalid_final")
                return
            self.final_payload = payload
            self.terminal_seen = True
            return
        if event_type == b"error" or (
            isinstance(payload, dict) and isinstance(payload.get("error"), dict)
        ):
            self.error_count += 1
            self.terminal_failure = isinstance(payload, dict)
            self.terminal_seen = True
            return
        self.prelude = True
        if not isinstance(payload, dict):
            self._fail("sse_invalid_event")
            return
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                for container_name in ("delta", "message"):
                    container = choice.get(container_name)
                    content = container.get("content") if isinstance(container, dict) else None
                    if isinstance(content, str):
                        self.content_parts.append(content)

    def _line(self, line: bytes) -> None:
        if line.startswith(b":"):
            if self.terminal_seen:
                self._fail("sse_after_terminal")
            self.comment_count += 1
            if line[1:].strip().startswith(b"heartbeat"):
                self.heartbeat_count += 1
            self.prelude = True
            return
        if not line:
            if self.event_type is not None or self.data_lines:
                self._dispatch()
            elif self.event_count == 0 and self.comment_count == 0:
                self.prelude = True
            return
        if self.terminal_seen:
            if (
                self.error_count == 1
                and self.done_count == 0
                and not self.data_lines
                and line in {b"data: [DONE]", b"data:[DONE]"}
            ):
                self.data_lines.append(b"[DONE]")
                return
            self._fail("sse_after_terminal")
            return
        field, separator, value = line.partition(b":")
        if not separator:
            return
        if value.startswith(b" "):
            value = value[1:]
        if field == b"event":
            self.event_type = value
        elif field == b"data":
            self.data_lines.append(value)

    def feed(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        while True:
            cr = self.buffer.find(b"\r")
            lf = self.buffer.find(b"\n")
            positions = [position for position in (cr, lf) if position >= 0]
            if not positions:
                return
            end = min(positions)
            if self.buffer[end] == 13 and end + 1 == len(self.buffer):
                return
            width = 2 if self.buffer[end : end + 2] == b"\r\n" else 1
            line = bytes(self.buffer[:end])
            del self.buffer[: end + width]
            self._line(line)

    def finish(self) -> None:
        if self.buffer:
            line = bytes(self.buffer)
            if line.endswith(b"\r"):
                line = line[:-1]
            self.buffer.clear()
            self._line(line)
        if self.event_type is not None or self.data_lines:
            self._dispatch()


def _response_socket(response) -> socket.socket | None:
    current = response
    seen: set[int] = set()
    for _ in range(6):
        if isinstance(current, socket.socket):
            return current
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        for attribute in ("_sock", "raw", "fp"):
            nested = getattr(current, attribute, None)
            if nested is not None and id(nested) not in seen:
                current = nested
                break
        else:
            return None
    return current if isinstance(current, socket.socket) else None


def client(
    port: int,
    input_marker: str,
    expected_output: str,
    reasoning_marker: str,
    private_prefix_marker: str,
    *,
    stream: bool = False,
    endpoint: str = "/v1/chat/completions",
    max_response_bytes: int = CLIENT_RESPONSE_LIMIT,
    deadline_seconds: float = CLIENT_DEADLINE,
) -> dict:
    require(
        endpoint in {"/v1/chat/completions", "/v1/completions"},
        "unsupported_client_endpoint",
    )
    body = {
        "model": "__listener_forced_aeon_guard_max__",
        "max_tokens": CALLER_MAX,
        "stream": stream,
    }
    if endpoint == "/v1/completions":
        body["prompt"] = input_marker
    else:
        body["messages"] = [{"role": "user", "content": input_marker}]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{endpoint}", data=json.dumps(body, separators=(",", ":")).encode(), headers={"Content-Type": "application/json"}, method="POST")
    started_ns = time.monotonic_ns()
    deadline = time.monotonic() + deadline_seconds
    try:
        response = urllib.request.urlopen(req, timeout=deadline_seconds)
    except urllib.error.HTTPError as err:
        response = err
    status = int(response.status or 0)
    content_type = response.headers.get_content_type()
    raw = bytearray()
    framer = _SSEFramer()
    first_byte_ns: int | None = None
    protocol_error: str | None = None
    try:
        sock = _response_socket(response)
        if sock is None:
            protocol_error = "client_deadline_socket_missing"
        while protocol_error is None and sock is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                protocol_error = "response_deadline"
                break
            try:
                sock.settimeout(remaining)
            except OSError:
                if sock.fileno() < 0 and response.isclosed():
                    break
                raise
            reader = getattr(response, "read1", response.read)
            chunk = reader(min(CLIENT_READ_CHUNK, max_response_bytes + 1 - len(raw)))
            if not chunk:
                break
            if first_byte_ns is None:
                first_byte_ns = time.monotonic_ns()
            raw.extend(chunk)
            if len(raw) > max_response_bytes:
                protocol_error = "response_body_limit"
                break
            if content_type == "text/event-stream":
                framer.feed(chunk)
    except (TimeoutError, socket.timeout):
        protocol_error = "response_deadline"
    finally:
        response.close()
    if content_type == "text/event-stream" and protocol_error is None:
        framer.finish()
        protocol_error = framer.protocol_error

    payload: dict | None = None
    response_format = "invalid"
    downstream_prelude = False
    if content_type == "text/event-stream":
        payload = framer.final_payload
        response_format = "event_final" if payload is not None else "sse"
        downstream_prelude = framer.prelude
    elif protocol_error is None:
        downstream_prelude = bool(raw) and raw[:1] != b"{"
        try:
            decoded = json.loads(raw)
            payload = decoded if isinstance(decoded, dict) else None
            response_format = "json" if payload is not None else "invalid"
        except (json.JSONDecodeError, UnicodeDecodeError):
            protocol_error = "invalid_json_response"

    choices = payload.get("choices") if isinstance(payload, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    http_error = status >= 400 and isinstance(payload, dict) and isinstance(payload.get("error"), dict)
    terminal_failure = protocol_error is None and (
        (
            framer.terminal_failure
            and framer.error_count == 1
            and framer.done_count == 1
        )
        or http_error
    )
    expected_final = (
        not stream
        and not downstream_prelude
        and protocol_error is None
        and content == expected_output
    )
    finished_ns = time.monotonic_ns()
    return {
        "endpoint": endpoint,
        "caller_stream": stream,
        "status": status,
        "content_type": content_type,
        "format": response_format,
        "nonempty": bool(raw),
        "valid_json": response_format == "json" and payload is not None,
        "expected_final": expected_final,
        "protocol_valid_terminal_failure": terminal_failure,
        "protocol_error": protocol_error,
        "downstream_prelude_detected": downstream_prelude,
        "first_downstream_nonempty_monotonic_ns": first_byte_ns,
        "request_started_monotonic_ns": started_ns,
        "request_finished_monotonic_ns": finished_ns,
        "duration_ms": (finished_ns - started_ns) // 1_000_000,
        "response_bytes": len(raw),
        "sse_event_count": framer.event_count,
        "sse_comment_count": framer.comment_count,
        "heartbeat_count": framer.heartbeat_count,
        "sse_final_event_count": framer.final_count,
        "sse_error_event_count": framer.error_count,
        "sse_done_count": framer.done_count,
        "reasoning_marker_leak_count": raw.count(reasoning_marker.encode()),
        "private_prefix_marker_leak_count": raw.count(private_prefix_marker.encode()),
    }


def _stable_error(exc: Exception, fallback: str) -> dict[str, str]:
    code = fallback
    if isinstance(exc, OSError) and exc.errno is not None:
        code = errno.errorcode.get(exc.errno, fallback)
    elif isinstance(exc, RuntimeError) and re.fullmatch(r"[a-z0-9_:=-]+", str(exc)):
        code = str(exc)
    return {"class": type(exc).__name__, "code": code}


def _stop_uncaptured_exclusive(
    proc: subprocess.Popen[bytes],
    cleanup: dict[str, object],
    deadline: float,
) -> dict[str, object]:
    known: dict[int, ProcessIdentity] = {}
    cleanup["unexpected_exit"] = proc.poll() is not None
    quiesced = False
    for signum, phase_deadline in (
        (
            signal.SIGTERM,
            min(deadline, time.monotonic() + PROCESS_TERM_GRACE),
        ),
        (signal.SIGKILL, deadline),
    ):
        if signum == signal.SIGKILL and quiesced:
            break
        if signum == signal.SIGKILL:
            cleanup["forced_kill"] = True
        signalled: set[tuple[int, int]] = set()
        while True:
            owned = _owned_processes(
                None, known, exclusive_supervisor=True
            )
            _reap_adopted(owned, proc.pid)
            proc.poll()
            owned = _owned_processes(
                None, known, exclusive_supervisor=True
            )
            live = [
                member
                for member in owned.values()
                if member.state not in {"X", "Z"}
            ]
            for member in live:
                key = (member.pid, member.starttime)
                if key in signalled:
                    continue
                if _signal_identity(member, signum):
                    signalled.add(key)
                    cleanup["term_sent" if signum == signal.SIGTERM else "kill_sent"] = True
            if proc.returncode is not None and not live:
                quiesced = True
                break
            remaining = phase_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(PROCESS_POLL_INTERVAL, remaining))
        if signum == signal.SIGTERM and not quiesced:
            cleanup["residual_producer_count_before_kill"] = len(live)

    final_owned: dict[int, ProcessIdentity] = {}
    try:
        while True:
            final_owned = _owned_processes(
                None, known, exclusive_supervisor=True
            )
            _reap_adopted(final_owned, proc.pid)
            proc.poll()
            final_owned = _owned_processes(
                None, known, exclusive_supervisor=True
            )
            if not final_owned or time.monotonic() >= deadline:
                break
            time.sleep(
                min(PROCESS_POLL_INTERVAL, max(0.0, deadline - time.monotonic()))
            )
    except Exception as exc:
        cleanup.setdefault(
            "cleanup_error", _stable_error(exc, "uncaptured_cleanup_failed")
        )
    cleanup["returncode"] = proc.returncode
    cleanup["proxy_exited"] = proc.returncode is not None
    cleanup["ownership_quiesced"] = not final_owned
    cleanup["group_quiesced"] = not final_owned
    cleanup["session_quiesced"] = not final_owned
    cleanup["residual_producer_count_final"] = sum(
        member.state not in {"X", "Z"} for member in final_owned.values()
    )
    if cleanup["residual_producer_count_before_kill"] is None and not final_owned:
        cleanup["residual_producer_count_before_kill"] = 0
    return cleanup


def stop(
    proc: subprocess.Popen[bytes] | None,
    identity: ProcessIdentity | None = None,
    baseline_children: dict[int, ProcessIdentity] | None = None,
    *,
    exclusive_supervisor: bool = False,
) -> dict[str, object]:
    if exclusive_supervisor and _EXCLUSIVE_SUPERVISOR_PID != os.getpid():
        raise RuntimeError("exclusive_supervisor_scope_required")
    cleanup: dict[str, object] = {
        "proxy_exited": proc is None,
        "unexpected_exit": False,
        "graceful_stop": False,
        "forced_kill": False,
        "term_sent": False,
        "kill_sent": False,
        "spawn_identity_captured": identity is not None,
        "pidfd_available": identity is not None and identity.pidfd is not None,
        "ownership_quiesced": proc is None,
        "group_quiesced": proc is None,
        "session_quiesced": proc is None,
        "residual_producer_count_before_kill": 0 if proc is None else None,
        "residual_producer_count_final": 0 if proc is None else None,
    }
    if proc is None:
        return cleanup

    deadline = time.monotonic() + PROCESS_STOP_TIMEOUT
    if identity is None:
        try:
            identity = capture_spawn_identity(proc.pid)
        except Exception as exc:
            cleanup["stop_error"] = _stable_error(exc, "spawn_identity_missing")
            if exclusive_supervisor:
                return _stop_uncaptured_exclusive(proc, cleanup, deadline)
            cleanup["unexpected_exit"] = proc.poll() is not None
            try:
                if proc.returncode is None:
                    proc.terminate()
                    cleanup["term_sent"] = True
                    try:
                        proc.wait(timeout=min(PROCESS_TERM_GRACE, PROCESS_STOP_TIMEOUT))
                    except subprocess.TimeoutExpired:
                        cleanup["forced_kill"] = True
                        proc.kill()
                        cleanup["kill_sent"] = True
                        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except Exception as cleanup_exc:
                cleanup.setdefault(
                    "cleanup_error",
                    _stable_error(cleanup_exc, "direct_child_cleanup_failed"),
                )
            cleanup["returncode"] = proc.returncode
            cleanup["proxy_exited"] = proc.returncode is not None
            return cleanup
    cleanup["spawn_identity_captured"] = True
    cleanup["pidfd_available"] = identity.pidfd is not None
    if identity.pidfd_errno is not None:
        cleanup["stop_error"] = _stable_error(
            OSError(identity.pidfd_errno, os.strerror(identity.pidfd_errno)),
            "pidfd_unavailable",
        )
    cleanup["spawn_identity"] = {
        "pid": identity.pid,
        "ppid": identity.ppid,
        "starttime": identity.starttime,
        "pgid": identity.pgrp,
        "session": identity.session,
    }

    # Generic callers may pass the former baseline argument, but it no longer
    # grants ownership of later direct children. Only the isolated supervisor
    # may claim all of its direct/adopted children.
    del baseline_children
    known = {identity.pid: identity}
    group: dict[int, ProcessIdentity] = {}
    session_members: dict[int, ProcessIdentity] = {}
    owned: dict[int, ProcessIdentity] = {}
    quiesced = False
    ownership_scan_ok = False
    try:
        cleanup["unexpected_exit"] = _child_exited(identity.pid)
        group, session_members = _producer_sets(identity)
        cleanup["term_sent"] = _signal_group(identity, signal.SIGTERM)
        term_deadline = min(deadline, time.monotonic() + PROCESS_TERM_GRACE)
        quiesced, group, session_members, owned, sent, signal_error = (
            _wait_for_quiescence(
                identity,
                known,
                term_deadline,
                signal.SIGTERM,
                set(),
                exclusive_supervisor=exclusive_supervisor,
            )
        )
        ownership_scan_ok = True
        cleanup["term_sent"] = sent or cleanup["term_sent"]
        if signal_error is not None:
            cleanup.setdefault(
                "stop_error", _stable_error(signal_error, "proxy_sigterm_failed")
            )
        if quiesced:
            cleanup["residual_producer_count_before_kill"] = 0
    except Exception as exc:
        cleanup.setdefault("stop_error", _stable_error(exc, "proxy_sigterm_failed"))

    if not quiesced:
        cleanup["forced_kill"] = True
        if ownership_scan_ok:
            cleanup["residual_producer_count_before_kill"] = sum(
                member.state not in {"X", "Z"} for member in owned.values()
            )
        try:
            cleanup["kill_sent"] = _signal_group(identity, signal.SIGKILL)
        except Exception as exc:
            cleanup.setdefault(
                "stop_error", _stable_error(exc, "proxy_sigkill_failed")
            )
        try:
            quiesced, group, session_members, owned, sent, signal_error = (
                _wait_for_quiescence(
                    identity,
                    known,
                    deadline,
                    signal.SIGKILL,
                    set(),
                    exclusive_supervisor=exclusive_supervisor,
                )
            )
            ownership_scan_ok = True
            cleanup["kill_sent"] = sent or cleanup["kill_sent"]
            if signal_error is not None:
                cleanup.setdefault(
                    "stop_error", _stable_error(signal_error, "proxy_sigkill_failed")
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

    final_owned: dict[int, ProcessIdentity] = owned
    try:
        while True:
            final_owned = _owned_processes(
                identity, known, exclusive_supervisor=exclusive_supervisor
            )
            _reap_adopted(final_owned, identity.pid)
            final_owned = _owned_processes(
                identity, known, exclusive_supervisor=exclusive_supervisor
            )
            if not final_owned or time.monotonic() >= deadline:
                break
            time.sleep(
                min(PROCESS_POLL_INTERVAL, max(0.0, deadline - time.monotonic()))
            )
        final_group, final_session = _producer_sets(identity)
        cleanup["group_quiesced"] = not final_group
        cleanup["session_quiesced"] = not final_session
        cleanup["ownership_quiesced"] = not final_owned
        cleanup["residual_producer_count_final"] = sum(
            member.state not in {"X", "Z"} for member in final_owned.values()
        )
    except Exception as exc:
        cleanup["ownership_quiesced"] = False
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
        and cleanup["pidfd_available"]
        and cleanup["ownership_quiesced"]
        and cleanup["group_quiesced"]
        and cleanup["session_quiesced"]
        and "stop_error" not in cleanup
    )
    return cleanup


def _identity_receipt(identity: ProcessIdentity) -> dict[str, int]:
    return {
        "pid": identity.pid,
        "ppid": identity.ppid,
        "starttime": identity.starttime,
        "pgid": identity.pgrp,
        "session": identity.session,
    }


def _send_supervisor_packet(
    control: socket.socket,
    packet: bytes | dict[str, object],
    deadline: float,
) -> None:
    payload = (
        packet
        if isinstance(packet, bytes)
        else json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    )
    if not payload or len(payload) > SUPERVISOR_MESSAGE_LIMIT:
        raise OSError(errno.E2BIG, "supervisor_message_invalid")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("supervisor_send_timeout")
    control.settimeout(remaining)
    if control.send(payload) != len(payload):
        raise OSError(errno.EIO, "supervisor_message_truncated")


def _recv_supervisor_receipt(
    control: socket.socket, deadline: float
) -> dict[str, object]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("supervisor_receipt_timeout")
    control.settimeout(remaining)
    payload = control.recv(SUPERVISOR_MESSAGE_LIMIT + 1)
    if not payload:
        raise EOFError("supervisor_receipt_eof")
    if len(payload) > SUPERVISOR_MESSAGE_LIMIT:
        raise OSError(errno.E2BIG, "supervisor_receipt_too_large")
    receipt = json.loads(payload)
    if not isinstance(receipt, dict):
        raise ValueError("supervisor_receipt_invalid")
    return receipt


def _scope_population(events_fd: int) -> tuple[int, bool]:
    try:
        payload = os.pread(events_fd, 4096, 0).decode("ascii")
    except OSError as exc:
        if exc.errno in {errno.ENODEV, errno.ENOENT}:
            return 0, True
        raise
    values = dict(line.split() for line in payload.splitlines())
    if values.get("populated") not in {"0", "1"}:
        raise OSError(errno.EIO, "scope_events_invalid")
    return int(values["populated"]), False


def _close_scope_fence(scope: ScopeFence) -> None:
    for fd in (scope.events_fd, scope.kill_fd):
        try:
            os.close(fd)
        except OSError:
            pass
    _close_identity_pidfd(scope.worker_identity)


def _scope_cgroup_collected(control_group: str | None) -> bool | None:
    if control_group is None:
        return None
    return not (CGROUP_ROOT / control_group.removeprefix("/")).exists()


def _wait_scope_collected(
    unit: str,
    control_group: str | None,
    deadline: float,
) -> tuple[bool, bool | None]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("scope_collect_timeout")
        try:
            result = subprocess.run(
                [
                    SYSTEMCTL_BIN,
                    "--user",
                    "show",
                    "--property=LoadState",
                    "--value",
                    "--",
                    unit,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("scope_collect_timeout") from exc
        if result.returncode != 0:
            raise RuntimeError("scope_collect_systemctl_failed")
        state = tuple(result.stdout.splitlines())
        if state not in {("loaded",), ("not-found",)}:
            raise RuntimeError("scope_collect_state_invalid")
        cgroup_collected = _scope_cgroup_collected(control_group)
        if state == ("not-found",) and cgroup_collected is not False:
            return True, cgroup_collected
        time.sleep(min(PROCESS_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))


def _collect_supervisor_scope(
    cleanup: dict[str, object],
    unit: str,
    control_group: str | None,
) -> dict[str, object]:
    started_ns = time.monotonic_ns()
    unit_collected = False
    cgroup_collected: bool | None = None
    error: dict[str, str] | None = None
    try:
        unit_collected, cgroup_collected = _wait_scope_collected(
            unit,
            control_group,
            time.monotonic() + SCOPE_COLLECT_TIMEOUT,
        )
    except Exception as exc:
        error = _stable_error(exc, "scope_collection_failed")
    finished_ns = time.monotonic_ns()
    cleanup.update(
        {
            "scope_unit": unit,
            "scope_unit_collected": unit_collected,
            "scope_cgroup_collected": cgroup_collected,
            "scope_collected": unit_collected and cgroup_collected is not False,
            "scope_collection_error": error,
            "scope_collection_duration_ms": (finished_ns - started_ns) // 1_000_000,
        }
    )
    return cleanup


def _process_control_group(pid: int) -> str:
    lines = (Path("/proc") / str(pid) / "cgroup").read_text(encoding="ascii").splitlines()
    unified = [line.split("::", 1)[1] for line in lines if line.startswith("0::")]
    if len(unified) != 1 or not unified[0].startswith("/"):
        raise OSError(errno.EIO, "unified_cgroup_identity_invalid")
    return unified[0]


def _establish_scope_fence(
    control: socket.socket, unit: str, _wrapper_pid: int, deadline: float
) -> ScopeFence:
    receipt = _recv_supervisor_receipt(control, deadline)
    worker_pid = receipt.get("pid")
    if (
        receipt.get("kind") != "scope_ready"
        or type(worker_pid) is not int
        or worker_pid <= 1
    ):
        raise OSError(errno.ESTALE, "scope_worker_identity_invalid")
    worker_identity = capture_process_identity(worker_pid)
    events_fd: int | None = None
    kill_fd: int | None = None
    try:
        control_group = _process_control_group(worker_pid)
        parts = Path(control_group).parts
        if ".." in parts or not parts or parts[-1] != unit:
            raise OSError(errno.ESTALE, "scope_identity_invalid")
        cgroup_path = CGROUP_ROOT / control_group.removeprefix("/")
        if not stat.S_ISDIR(cgroup_path.lstat().st_mode):
            raise OSError(errno.ENOTDIR, "scope_cgroup_invalid")
        events_fd = os.open(
            cgroup_path / "cgroup.events",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        kill_fd = os.open(
            cgroup_path / "cgroup.kill",
            os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        populated, removed = _scope_population(events_fd)
        members = {
            int(member) for member in (cgroup_path / "cgroup.procs").read_text().split()
        }
        if removed or populated != 1 or worker_pid not in members:
            raise OSError(errno.ESTALE, "scope_membership_invalid")
        if (
            _process_control_group(worker_pid) != control_group
            or not _same_process(_read_process_identity(worker_pid), worker_identity)
        ):
            raise OSError(errno.ESTALE, "scope_worker_identity_changed")
        return ScopeFence(
            unit,
            control_group,
            events_fd,
            kill_fd,
            worker_identity,
        )
    except Exception:
        for fd in (events_fd, kill_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        _close_identity_pidfd(worker_identity)
        raise


def _fence_supervisor_scope(
    cleanup: dict[str, object], scope: ScopeFence
) -> dict[str, object]:
    deadline = time.monotonic() + SUPERVISOR_EMERGENCY_TIMEOUT
    kill_sent = False
    populated_final: int | None = None
    removed = False
    fence_error: dict[str, str] | None = None
    try:
        populated, removed = _scope_population(scope.events_fd)
        cleanup["scope_populated_initial"] = populated
        if populated:
            try:
                if os.write(scope.kill_fd, b"1") != 1:
                    raise OSError(errno.EIO, "scope_kill_truncated")
                kill_sent = True
            except OSError as exc:
                if exc.errno not in {errno.ENODEV, errno.ENOENT}:
                    raise
        while True:
            populated_final, removed = _scope_population(scope.events_fd)
            if populated_final == 0:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("scope_quiescence_timeout")
            time.sleep(PROCESS_POLL_INTERVAL)
    except Exception as exc:
        fence_error = _stable_error(exc, "scope_fence_failed")
    finally:
        _close_scope_fence(scope)
    _collect_supervisor_scope(cleanup, scope.unit, scope.control_group)

    scope_quiesced = fence_error is None and populated_final == 0
    cleanup.update(
        {
            "scope_fence_established": True,
            "scope_unit": scope.unit,
            "scope_control_group": scope.control_group,
            "scope_worker_pid": scope.worker_identity.pid,
            "scope_worker_starttime": scope.worker_identity.starttime,
            "scope_kill_all_sent": kill_sent,
            "scope_populated_final": populated_final,
            "scope_removed": removed,
            "scope_quiesced": scope_quiesced,
            "scope_error": fence_error,
        }
    )
    if scope_quiesced:
        cleanup.update(
            {
                "proxy_exited": True,
                "ownership_quiesced": True,
                "group_quiesced": True,
                "session_quiesced": True,
                "residual_producer_count_final": 0,
                "candidate_ownership_quiesced": True,
            }
        )
    else:
        cleanup["candidate_ownership_quiesced"] = False
    if kill_sent:
        cleanup["forced_kill"] = True
        cleanup["graceful_stop"] = False
    return cleanup


def _wait_supervisor(pid: int, deadline: float) -> int | None:
    while True:
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if waited == pid:
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(PROCESS_POLL_INTERVAL, remaining))


def _close_identity_pidfd(identity: ProcessIdentity | None) -> None:
    if identity is not None and identity.pidfd is not None:
        try:
            os.close(identity.pidfd)
        except OSError:
            pass


def _finalize_supervisor_cleanup(
    cleanup: dict[str, object],
    status: int | None,
    error: dict[str, str] | None = None,
) -> dict[str, object]:
    cleanup["exclusive_supervisor"] = bool(cleanup.get("exclusive_supervisor"))
    cleanup["supervisor_exited"] = status is not None
    cleanup["supervisor_reaped"] = status is not None
    cleanup["supervisor_exitcode"] = (
        os.waitstatus_to_exitcode(status) if status is not None else None
    )
    cleanup["candidate_ownership_quiesced"] = bool(
        cleanup.get("proxy_exited")
        and cleanup.get("ownership_quiesced")
        and cleanup.get("residual_producer_count_final") == 0
    )
    if error is not None and not cleanup.get("supervisor_error"):
        cleanup["supervisor_error"] = error
    if (
        cleanup["supervisor_exitcode"] not in (None, 0)
        and not cleanup.get("supervisor_error")
    ):
        cleanup["supervisor_error"] = {
            "class": "RuntimeError",
            "code": "supervisor_exit_nonzero",
        }
    cleanup.setdefault("supervisor_error", None)
    return cleanup


def _descendant_processes(
    root: ProcessIdentity, table: dict[int, ProcessIdentity]
) -> dict[int, ProcessIdentity]:
    current_root = table.get(root.pid)
    if not _same_process(current_root, root):
        return {}
    descendants: dict[int, ProcessIdentity] = {}
    changed = True
    while changed:
        changed = False
        parents = {root.pid, *descendants}
        for pid, current in table.items():
            if pid not in descendants and current.ppid in parents:
                descendants[pid] = current
                changed = True
    return descendants


def _signal_external_identity(
    identity: ProcessIdentity,
    signum: int,
    owner: ProcessIdentity,
) -> bool:
    close_pidfd = False
    try:
        if identity.pidfd is not None:
            pidfd = identity.pidfd
        else:
            pidfd = _pidfd_open(identity.pid)
            close_pidfd = True
    except ProcessLookupError:
        return False
    except OSError:
        table = _process_table()
        current = table.get(identity.pid)
        if not _same_process(current, identity):
            return False
        if identity.pid != owner.pid and identity.pid not in _descendant_processes(
            owner, table
        ):
            return False
        os.kill(identity.pid, signum)
        return True
    try:
        if not _same_process(_read_process_identity(identity.pid), identity):
            return False
        _pidfd_send_signal(pidfd, signum)
        return True
    except ProcessLookupError:
        return False
    finally:
        if close_pidfd:
            os.close(pidfd)


def _emergency_supervisor_cleanup(
    pid: int,
    identity: ProcessIdentity | None,
    control: socket.socket,
    error: dict[str, str],
) -> dict[str, object]:
    deadline = time.monotonic() + SUPERVISOR_EMERGENCY_TIMEOUT
    try:
        control.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    try:
        receipt = _recv_supervisor_receipt(
            control,
            min(deadline, time.monotonic() + SUPERVISOR_CLEANUP_TIMEOUT),
        )
    except Exception:
        receipt = None
    late_cleanup: dict[str, object] | None = None
    cleanup_value = receipt.get("process_cleanup") if isinstance(receipt, dict) else None
    if isinstance(cleanup_value, dict):
        late_cleanup = cleanup_value
        status = _wait_supervisor(
            pid, min(deadline, time.monotonic() + PROCESS_TERM_GRACE)
        )
        if status is not None:
            cleanup = _finalize_supervisor_cleanup(late_cleanup, status, error)
            control.close()
            _close_identity_pidfd(identity)
            return cleanup

    if identity is None:
        try:
            identity = capture_process_identity(pid)
        except Exception:
            identity = None
    if identity is not None:
        try:
            _signal_external_identity(identity, signal.SIGTERM, identity)
        except OSError:
            pass
    status = _wait_supervisor(
        pid, min(deadline, time.monotonic() + PROCESS_TERM_GRACE)
    )

    known: dict[int, ProcessIdentity] = {}
    if status is None and identity is not None:
        try:
            _signal_external_identity(identity, signal.SIGSTOP, identity)
        except OSError:
            pass
        stable = 0
        previous: set[tuple[int, int]] = set()
        while stable < 2 and time.monotonic() < deadline:
            descendants = _descendant_processes(identity, _process_table())
            for member in descendants.values():
                known[member.pid] = member
                if member.state not in {"X", "Z"}:
                    try:
                        _signal_external_identity(member, signal.SIGSTOP, identity)
                    except OSError:
                        pass
            current = {(item.pid, item.starttime) for item in descendants.values()}
            stable = stable + 1 if current == previous else 0
            previous = current
            time.sleep(PROCESS_POLL_INTERVAL)
        for member in known.values():
            if member.state not in {"X", "Z"}:
                try:
                    _signal_external_identity(member, signal.SIGKILL, identity)
                except OSError:
                    pass
        try:
            _signal_external_identity(identity, signal.SIGKILL, identity)
        except OSError:
            pass
        status = _wait_supervisor(pid, deadline)

    while time.monotonic() < deadline:
        remaining = [
            member
            for member in known.values()
            if _same_process(_read_process_identity(member.pid), member)
        ]
        if not remaining:
            break
        time.sleep(PROCESS_POLL_INTERVAL)
    else:
        remaining = list(known.values())

    control.close()
    _close_identity_pidfd(identity)
    cleanup = late_cleanup or stop(None)
    cleanup.update(
        {
            "exclusive_supervisor": True,
            "ownership_quiesced": not remaining,
            "group_quiesced": not remaining,
            "session_quiesced": not remaining,
            "residual_producer_count_final": len(remaining),
        }
    )
    return _finalize_supervisor_cleanup(cleanup, status, error)


def _exclusive_supervisor_worker(control_fd: int) -> int:
    try:
        control = socket.socket(fileno=control_fd)
        control.set_inheritable(False)
    except OSError:
        return 2
    try:
        _send_supervisor_packet(
            control,
            {"kind": "scope_ready", "pid": os.getpid()},
            time.monotonic() + SUPERVISOR_START_TIMEOUT,
        )
        launch = _recv_supervisor_receipt(
            control, time.monotonic() + SUPERVISOR_START_TIMEOUT
        )
        argv = launch.get("argv")
        cwd = launch.get("cwd")
        env = launch.get("env")
        log_path = launch.get("log_path")
        test_failpoint = launch.get("test_failpoint")
        control_timeout = launch.get("control_timeout")
        if (
            launch.get("kind") != "launch"
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(value, str) for value in argv)
            or not isinstance(cwd, str)
            or not isinstance(env, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            )
            or not isinstance(log_path, str)
            or test_failpoint
            not in {None, "subreaper:EPERM", "pidfd:EMFILE", "pidfd:EPERM"}
            or not isinstance(control_timeout, (int, float))
            or not 0 < control_timeout <= 3600
        ):
            raise ValueError("supervisor_launch_invalid")
    except Exception as exc:
        error = _stable_error(exc, "supervisor_launch_failed")
        try:
            _send_supervisor_packet(
                control,
                {
                    "kind": "startup_error",
                    "exclusive_supervisor": False,
                    "subreaper_enabled": False,
                    "supervisor_error": error,
                    "process_cleanup": stop(None),
                },
                time.monotonic() + SUPERVISOR_CLEANUP_TIMEOUT,
            )
        except Exception:
            pass
        control.close()
        return 2
    if isinstance(test_failpoint, str):
        target, code_name = test_failpoint.split(":", 1)
        code = getattr(errno, code_name)

        def injected_failure(*_args) -> None:
            raise OSError(code, os.strerror(code))

        globals()[
            "_enable_child_subreaper" if target == "subreaper" else "_pidfd_open"
        ] = injected_failure
    globals()["SUPERVISOR_CONTROL_TIMEOUT"] = float(control_timeout)
    _exclusive_supervisor_main(
        control,
        argv,
        Path(cwd),
        env,
        Path(log_path),
        (),
    )
    return 1


def _exclusive_supervisor_main(
    control: socket.socket,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    inherited_fds: tuple[int, ...],
) -> None:
    global _EXCLUSIVE_SUPERVISOR_PID
    proc: subprocess.Popen[bytes] | None = None
    identity: ProcessIdentity | None = None
    log = None
    started = False
    subreaper_enabled = False
    supervisor_error: dict[str, str] | None = None
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    try:
        for fd in inherited_fds:
            if fd != control.fileno():
                try:
                    os.close(fd)
                except OSError:
                    pass
        os.setsid()
        _enable_child_subreaper()
        subreaper_enabled = True
        _EXCLUSIVE_SUPERVISOR_PID = os.getpid()
        signal.signal(signal.SIGTERM, request_stop)
        log = log_path.open("wb")
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        identity = capture_spawn_identity(proc.pid)
        if identity.pidfd_errno is not None:
            raise OSError(identity.pidfd_errno, os.strerror(identity.pidfd_errno))
        supervisor_identity = _read_process_identity(os.getpid())
        if supervisor_identity is None:
            raise ProcessLookupError(os.getpid())
        _send_supervisor_packet(
            control,
            {
                "kind": "started",
                "exclusive_supervisor": True,
                "subreaper_enabled": True,
                "candidate_identity": _identity_receipt(identity),
                "pidfd_available": True,
                "supervisor_identity": _identity_receipt(supervisor_identity),
            },
            time.monotonic() + SUPERVISOR_START_TIMEOUT,
        )
        started = True
        control_deadline = time.monotonic() + SUPERVISOR_CONTROL_TIMEOUT
        while True:
            if stop_requested:
                supervisor_error = {
                    "class": "SignalExit",
                    "code": "supervisor_parent_signal",
                }
                break
            if _child_exited(identity.pid):
                supervisor_error = {
                    "class": "RuntimeError",
                    "code": "proxy_exit_before_stop",
                }
                break
            remaining = control_deadline - time.monotonic()
            if remaining <= 0:
                supervisor_error = {
                    "class": "TimeoutError",
                    "code": "supervisor_control_timeout",
                }
                break
            control.settimeout(min(PROCESS_POLL_INTERVAL, remaining))
            try:
                command = control.recv(SUPERVISOR_MESSAGE_LIMIT + 1)
            except socket.timeout:
                continue
            except (ConnectionError, OSError):
                command = b""
            if not command:
                supervisor_error = {
                    "class": "EOFError",
                    "code": "supervisor_parent_eof",
                }
            elif command != b"stop":
                supervisor_error = {
                    "class": "ValueError",
                    "code": "supervisor_invalid_control",
                }
            break
    except Exception as exc:
        supervisor_error = _stable_error(exc, "supervisor_start_failed")
    finally:
        cleanup = stop(
            proc,
            identity,
            exclusive_supervisor=subreaper_enabled,
        )
        cleanup["exclusive_supervisor"] = subreaper_enabled
        cleanup["candidate_ownership_quiesced"] = bool(
            cleanup.get("proxy_exited")
            and cleanup.get("ownership_quiesced")
            and cleanup.get("residual_producer_count_final") == 0
        )
        cleanup["supervisor_error"] = supervisor_error
        receipt = {
            "kind": "cleanup" if started else "startup_failed",
            "exclusive_supervisor": subreaper_enabled,
            "subreaper_enabled": subreaper_enabled,
            "supervisor_error": supervisor_error,
            "process_cleanup": cleanup,
        }
        try:
            _send_supervisor_packet(
                control,
                receipt,
                time.monotonic() + SUPERVISOR_CLEANUP_TIMEOUT,
            )
        except Exception:
            pass
        if log is not None:
            log.close()
        control.close()
        os._exit(0 if cleanup["candidate_ownership_quiesced"] else 1)


def start_candidate_supervisor(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    inherited_fds: tuple[int, ...] = (),
) -> SupervisorHandle:
    if threading.active_count() != 1:
        raise RuntimeError("supervisor_fork_parent_multithreaded")
    if not os.access(SYSTEMD_RUN_BIN, os.X_OK):
        raise RuntimeError("systemd_user_scope_unavailable")
    unit = f"llm-guard-loop-recovery-{os.getpid()}-{secrets.token_hex(8)}.scope"
    script = Path(__file__).resolve(strict=True)
    parent_control, child_control = socket.socketpair(
        socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC
    )
    try:
        pid = os.fork()
    except Exception:
        parent_control.close()
        child_control.close()
        raise
    if pid == 0:
        parent_control.close()
        for fd in inherited_fds:
            if fd != child_control.fileno():
                try:
                    os.close(fd)
                except OSError:
                    pass
        control_fd = child_control.fileno()
        for stdio_fd in (0, 1):
            if control_fd != stdio_fd:
                os.dup2(control_fd, stdio_fd)
            os.set_inheritable(stdio_fd, True)
        if control_fd not in (0, 1):
            child_control.close()
        try:
            os.execv(
                SYSTEMD_RUN_BIN,
                [
                    SYSTEMD_RUN_BIN,
                    "--user",
                    "--scope",
                    "--quiet",
                    "--collect",
                    f"--unit={unit}",
                    "--property=KillMode=control-group",
                    "--property=SendSIGKILL=yes",
                    "--",
                    sys.executable,
                    str(script),
                    "--exclusive-supervisor-fd",
                    "0",
                ],
            )
        except Exception:
            os._exit(127)

    child_control.close()
    deadline = time.monotonic() + SUPERVISOR_START_TIMEOUT
    scope: ScopeFence | None = None
    wrapper_identity: ProcessIdentity | None = None
    try:
        scope = _establish_scope_fence(parent_control, unit, pid, deadline)
        wrapper_identity = capture_process_identity(pid)
        _send_supervisor_packet(
            parent_control,
            {
                "kind": "launch",
                "argv": argv,
                "cwd": str(cwd),
                "env": env,
                "log_path": str(log_path),
                "test_failpoint": _SUPERVISOR_TEST_FAILPOINT,
                "control_timeout": SUPERVISOR_CONTROL_TIMEOUT,
            },
            deadline,
        )
        receipt = _recv_supervisor_receipt(parent_control, deadline)
    except Exception as exc:
        error = _stable_error(exc, "supervisor_start_receipt_failed")
        scope_cleanup: dict[str, object] = {}
        if scope is not None:
            _fence_supervisor_scope(scope_cleanup, scope)
        cleanup = _emergency_supervisor_cleanup(
            pid, wrapper_identity, parent_control, error
        )
        cleanup.update(scope_cleanup)
        if scope is None:
            _collect_supervisor_scope(cleanup, unit, None)
        raise SupervisorStartError(error, cleanup, False, False) from exc

    if receipt.get("kind") != "started":
        status = _wait_supervisor(
            pid, time.monotonic() + SUPERVISOR_CLEANUP_TIMEOUT
        )
        error_value = receipt.get("supervisor_error")
        error = (
            error_value
            if isinstance(error_value, dict)
            else {"class": "RuntimeError", "code": "supervisor_start_failed"}
        )
        if status is None:
            cleanup = _emergency_supervisor_cleanup(
                pid, wrapper_identity, parent_control, error
            )
        else:
            cleanup_value = receipt.get("process_cleanup")
            cleanup = (
                cleanup_value if isinstance(cleanup_value, dict) else stop(None)
            )
            _finalize_supervisor_cleanup(cleanup, status, error)
            parent_control.close()
            _close_identity_pidfd(wrapper_identity)
        _fence_supervisor_scope(cleanup, scope)
        raise SupervisorStartError(
            error,
            cleanup,
            bool(receipt.get("subreaper_enabled")),
            bool(receipt.get("exclusive_supervisor")),
        )

    assert scope is not None and wrapper_identity is not None
    try:
        current_wrapper = _read_process_identity(pid)
        current_worker = _read_process_identity(scope.worker_identity.pid)
        receipt_identity = receipt.get("supervisor_identity")
        candidate_identity = receipt.get("candidate_identity")
        if not _same_process(current_wrapper, wrapper_identity):
            raise OSError(errno.ESTALE, "scope_wrapper_identity_changed")
        if not _same_process(current_worker, scope.worker_identity):
            raise OSError(errno.ESTALE, "scope_worker_identity_changed")
        if not isinstance(receipt_identity, dict) or (
            receipt_identity.get("pid"), receipt_identity.get("starttime")
        ) != (scope.worker_identity.pid, scope.worker_identity.starttime):
            raise OSError(errno.ESTALE, "scope_worker_identity_changed")
        if not isinstance(candidate_identity, dict) or any(
            type(candidate_identity.get(field)) is not int
            for field in ("pid", "ppid", "starttime")
        ):
            raise OSError(errno.ESTALE, "candidate_lineage_invalid")
        candidate_pid = candidate_identity["pid"]
        current_candidate = _read_process_identity(candidate_pid)
        if current_candidate is None or (
            current_candidate.pid,
            current_candidate.ppid,
            current_candidate.starttime,
        ) != (
            candidate_pid,
            scope.worker_identity.pid,
            candidate_identity["starttime"],
        ):
            raise OSError(errno.ESTALE, "candidate_lineage_invalid")
        if _process_control_group(candidate_pid) != scope.control_group:
            raise OSError(errno.ESTALE, "candidate_scope_identity_invalid")
        if not receipt.get("exclusive_supervisor") or not receipt.get(
            "subreaper_enabled"
        ):
            raise RuntimeError("exclusive_supervisor_not_enabled")
    except Exception as exc:
        error = _stable_error(exc, "supervisor_identity_capture_failed")
        scope_cleanup = _fence_supervisor_scope({}, scope)
        cleanup = _emergency_supervisor_cleanup(
            pid,
            wrapper_identity,
            parent_control,
            error,
        )
        cleanup.update(scope_cleanup)
        raise SupervisorStartError(
            error,
            cleanup,
            bool(receipt.get("subreaper_enabled")),
            bool(receipt.get("exclusive_supervisor")),
        ) from exc
    receipt.update(
        {
            "scope_fence_established": True,
            "scope_unit": scope.unit,
            "scope_control_group": scope.control_group,
            "scope_worker_pid": scope.worker_identity.pid,
            "scope_worker_starttime": scope.worker_identity.starttime,
        }
    )
    return SupervisorHandle(pid, wrapper_identity, parent_control, receipt, scope)


def _finish_candidate_supervisor(
    handle: SupervisorHandle, command: bytes | None
) -> dict[str, object]:
    deadline = time.monotonic() + SUPERVISOR_CLEANUP_TIMEOUT
    control_error: dict[str, str] | None = None
    if command is not None:
        try:
            _send_supervisor_packet(handle.control, command, deadline)
        except Exception as exc:
            control_error = _stable_error(exc, "supervisor_control_send_failed")
            try:
                handle.control.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    try:
        receipt = _recv_supervisor_receipt(handle.control, deadline)
        cleanup_value = receipt.get("process_cleanup")
        if receipt.get("kind") != "cleanup" or not isinstance(
            cleanup_value, dict
        ):
            raise ValueError("supervisor_cleanup_receipt_invalid")
        status = _wait_supervisor(handle.pid, deadline)
        if status is None:
            raise TimeoutError("supervisor_exit_timeout")
        cleanup = _finalize_supervisor_cleanup(
            cleanup_value, status, control_error
        )
        handle.control.close()
        _close_identity_pidfd(handle.identity)
        return _fence_supervisor_scope(cleanup, handle.scope)
    except Exception as exc:
        error = control_error or _stable_error(
            exc, "supervisor_cleanup_receipt_failed"
        )
        scope_cleanup = _fence_supervisor_scope({}, handle.scope)
        cleanup = _emergency_supervisor_cleanup(
            handle.pid, handle.identity, handle.control, error
        )
        cleanup.update(scope_cleanup)
        return cleanup


def stop_candidate_supervisor(handle: SupervisorHandle) -> dict[str, object]:
    return _finish_candidate_supervisor(handle, b"stop")


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


def sensitive_marker_count(
    root: Path,
    markers: tuple[bytes, ...],
    stats: dict[str, int | float | bool] | None = None,
) -> int:
    if not markers or any(not isinstance(marker, bytes) or not marker for marker in markers):
        raise ValueError("privacy_scan_invalid_marker")
    started = time.monotonic()
    deadline = started + SCAN_TIMEOUT
    entry_count = 0
    logical_bytes = 0
    largest_file_bytes = 0
    if stats is not None:
        stats.clear()
        stats.update(
            {
                "completed": False,
                "entry_count": 0,
                "logical_bytes": 0,
                "largest_file_bytes": 0,
                "max_entries": SCAN_MAX_ENTRIES,
                "max_file_bytes": SCAN_MAX_FILE_BYTES,
                "max_total_bytes": SCAN_MAX_TOTAL_BYTES,
                "deadline_seconds": SCAN_TIMEOUT,
            }
        )
    common_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_flags = common_flags | os.O_DIRECTORY
    file_flags = common_flags | os.O_NONBLOCK

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise OSError(errno.ETIMEDOUT, "privacy_scan_deadline")

    def count_entry() -> None:
        nonlocal entry_count
        check_deadline()
        entry_count += 1
        if stats is not None:
            stats["entry_count"] = entry_count
        if entry_count > SCAN_MAX_ENTRIES:
            raise OSError(errno.E2BIG, "privacy_scan_entry_limit")

    def scan_file(parent_fd: int, name: str, before: os.stat_result) -> int:
        nonlocal largest_file_bytes, logical_bytes
        check_deadline()
        fd = os.open(name, file_flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "privacy_scan_non_regular")
            _require_stable_stat(before, opened, "privacy_scan_file_replaced")
            if opened.st_size > SCAN_MAX_FILE_BYTES:
                raise OSError(errno.EFBIG, "privacy_scan_file_limit")
            if logical_bytes > SCAN_MAX_TOTAL_BYTES - opened.st_size:
                raise OSError(errno.EFBIG, "privacy_scan_total_limit")
            logical_bytes += opened.st_size
            largest_file_bytes = max(largest_file_bytes, opened.st_size)
            if stats is not None:
                stats["logical_bytes"] = logical_bytes
                stats["largest_file_bytes"] = largest_file_bytes
            remaining = opened.st_size
            tails = {marker: b"" for marker in markers}
            count = 0
            while remaining:
                check_deadline()
                chunk = os.read(fd, min(SCAN_CHUNK_SIZE, remaining))
                check_deadline()
                if not chunk:
                    raise OSError(errno.ESTALE, "privacy_scan_file_truncated")
                remaining -= len(chunk)
                for marker in markers:
                    combined = tails[marker] + chunk
                    count += combined.count(marker)
                    tails[marker] = (
                        combined[-(len(marker) - 1) :] if len(marker) > 1 else b""
                    )
            check_deadline()
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
        check_deadline()
        with os.scandir(fd) as entries:
            for entry in entries:
                count_entry()
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

    check_deadline()
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
        finished = time.monotonic()
        if finished >= deadline:
            raise OSError(errno.ETIMEDOUT, "privacy_scan_deadline")
        if stats is not None:
            stats["completed"] = True
            stats["elapsed_milliseconds"] = int(
                (finished - started) * 1_000
            )
        return total
    finally:
        os.close(root_fd)


def _operational_contract_evidence_errors(summary: dict) -> list[str]:
    errors: list[str] = []
    attempts = summary.get("attempts", [])
    for attempt in attempts:
        timestamp = attempt.get("admitted_monotonic_ns")
        if not isinstance(timestamp, int):
            errors.append(f"attempt_timestamp_missing:{attempt.get('number', 0)}")

    contract = summary.get("operational_contract", {})
    arms = contract.get("arms", {}) if isinstance(contract, dict) else {}
    business_roles = {"primary", "salvage", "recovery_probe"}

    shielded = arms.get("shielded_hold")
    if not isinstance(shielded, dict):
        errors.append("shielded_hold_receipt_missing")
        shielded = {}
    shielded_attempts = [
        attempt for attempt in attempts if attempt.get("phase") == "shielded_hold"
    ]
    shielded_business = [
        attempt
        for attempt in shielded_attempts
        if attempt.get("role") in business_roles
    ]
    primaries = [
        attempt for attempt in shielded_business if attempt.get("role") == "primary"
    ]
    salvages = [
        attempt for attempt in shielded_business if attempt.get("role") == "salvage"
    ]
    recovery_probes = [
        attempt
        for attempt in shielded_business
        if attempt.get("role") == "recovery_probe"
    ]
    if len(primaries) != 1:
        errors.append("shielded_hold_primary_count")
    if len(salvages) != 1:
        errors.append("shielded_hold_salvage_count")
    if len(shielded_business) != 2:
        errors.append("shielded_hold_business_attempt_count")
    if recovery_probes:
        errors.append("shielded_hold_recovery_probe")

    shielded_first = shielded.get("first_downstream_nonempty_monotonic_ns")
    if not isinstance(shielded_first, int):
        errors.append("shielded_hold_first_byte_missing")
    else:
        for attempt in shielded_business:
            admitted = attempt.get("admitted_monotonic_ns")
            if not isinstance(admitted, int) or admitted >= shielded_first:
                errors.append(
                    f"shielded_hold_post_commit_attempt:{attempt.get('number', 0)}"
                )
        if salvages and (
            not isinstance(salvages[0].get("admitted_monotonic_ns"), int)
            or salvages[0]["admitted_monotonic_ns"] >= shielded_first
        ):
            errors.append("shielded_hold_salvage_after_first_byte")

    if primaries:
        primary = primaries[0]
        admitted = primary.get("admitted_monotonic_ns")
        first_event = primary.get("upstream_first_event_monotonic_ns")
        if (
            not isinstance(admitted, int)
            or not isinstance(first_event, int)
            or first_event - admitted
            < PROBE_HEARTBEAT_INTERVAL_SECS * 1_000_000_000
        ):
            errors.append("shielded_hold_not_delayed_across_heartbeat")
        if primary.get("endpoint") != "chat_completions" or primary.get("stream") is not True:
            errors.append("shielded_hold_upstream_stream_contract")
        if primary.get("stream_usage") is not True:
            errors.append("shielded_hold_upstream_usage_contract")
        if primary.get("thinking_budget") != THINKING_BUDGET:
            errors.append("shielded_hold_primary_thinking_budget")
    if salvages:
        salvage = salvages[0]
        if salvage.get("endpoint") != "chat_completions" or salvage.get("stream") is not True:
            errors.append("shielded_hold_salvage_stream_contract")
        if (
            salvage.get("thinking_budget") != 0
            or salvage.get("private_prefix_present") is not True
            or salvage.get("loop_tail_present") is not False
        ):
            errors.append("shielded_hold_salvage_contract")

    if (
        shielded.get("endpoint") != "/v1/chat/completions"
        or shielded.get("caller_stream") is not False
    ):
        errors.append("shielded_hold_caller_contract")
    if (
        shielded.get("status") != 200
        or shielded.get("content_type") != "application/json"
        or shielded.get("valid_json") is not True
        or shielded.get("expected_final") is not True
        or shielded.get("nonempty") is not True
    ):
        errors.append("shielded_hold_json_success_missing")
    if (
        shielded.get("heartbeat_count") != 0
        or shielded.get("downstream_prelude_detected") is not False
    ):
        errors.append("shielded_hold_downstream_prelude")
    if shielded.get("protocol_error") is not None:
        errors.append("shielded_hold_protocol_error")
    if any(
        shielded.get(field, 0) != 0
        for field in (
            "reasoning_marker_leak_count",
            "private_prefix_marker_leak_count",
        )
    ):
        errors.append("shielded_hold_marker_leak")

    generic = arms.get("generic_committed_stream")
    if not isinstance(generic, dict):
        errors.append("generic_committed_stream_receipt_missing")
        generic = {}
    generic_attempts = [
        attempt
        for attempt in attempts
        if attempt.get("phase") == "generic_committed_stream"
        and attempt.get("role") in business_roles
    ]
    if len(generic_attempts) != 1 or any(
        attempt.get("role") != "primary" for attempt in generic_attempts
    ):
        errors.append("generic_committed_stream_attempt_count")
    if any(attempt.get("role") == "recovery_probe" for attempt in generic_attempts):
        errors.append("generic_committed_stream_recovery_probe")
    generic_first = generic.get("first_downstream_nonempty_monotonic_ns")
    if not isinstance(generic_first, int):
        errors.append("generic_committed_stream_first_byte_missing")
    else:
        for attempt in generic_attempts:
            admitted = attempt.get("admitted_monotonic_ns")
            if not isinstance(admitted, int) or admitted >= generic_first:
                errors.append(
                    "generic_committed_stream_post_commit_attempt:"
                    f"{attempt.get('number', 0)}"
                )
    if generic_attempts and (
        generic_attempts[0].get("endpoint") != "completions"
        or generic_attempts[0].get("stream") is not True
    ):
        errors.append("generic_committed_stream_upstream_contract")
    if (
        generic.get("endpoint") != "/v1/completions"
        or generic.get("caller_stream") is not True
        or generic.get("status") != 200
        or generic.get("content_type") != "text/event-stream"
    ):
        errors.append("generic_committed_stream_caller_contract")
    if (
        generic.get("first_downstream_nonempty_monotonic_ns") is None
        or generic.get("downstream_prelude_detected") is not True
        or generic.get("heartbeat_count", 0) < 1
    ):
        errors.append("generic_committed_stream_prelude_missing")
    if generic.get("protocol_error") is not None:
        errors.append("generic_committed_stream_protocol_error")
    if (
        generic.get("protocol_valid_terminal_failure") is not True
        or generic.get("sse_error_event_count") != 1
        or generic.get("sse_done_count") != 1
    ):
        errors.append("generic_committed_stream_terminal_failure_missing")
    if any(
        generic.get(field, 0) != 0
        for field in (
            "reasoning_marker_leak_count",
            "private_prefix_marker_leak_count",
        )
    ):
        errors.append("generic_committed_stream_marker_leak")
    return errors


def acceptance_errors(summary: dict) -> list[str]:
    errors: list[str] = []
    try:
        validate_offline_self_test_receipt(summary.get("offline_self_test"))
    except RuntimeError:
        errors.append("offline_self_test_failed")
    if not summary.get("binary_identity_stable"):
        errors.append("binary_identity_drift")
    if summary.get("execution_error") is not None:
        errors.append("execution_failed")
    if not summary.get("subreaper_enabled"):
        errors.append("child_subreaper_not_enabled")
    if not summary.get("exclusive_supervisor"):
        errors.append("exclusive_supervisor_missing")
    errors.extend(summary.get("fixture_errors", []))

    attempts = summary.get("attempts", [])
    salvages = [
        attempt
        for attempt in attempts
        if attempt.get("phase") == "shielded_hold"
        and attempt.get("role") == "salvage"
    ]
    fresh_attempts = [
        attempt
        for attempt in attempts
        if attempt["phase"] == "fresh"
        and attempt.get("role") == "primary"
        and not attempt["salvage_material_present"]
    ]
    if len(salvages) != 1:
        errors.append("salvage_count")
    if not fresh_attempts:
        errors.append("fresh_request_missing")
    for attempt in attempts:
        number = attempt["number"]
        if attempt.get("role") == "salvage":
            if attempt["phase"] != "shielded_hold":
                errors.append(f"salvage_phase:{number}")
            if not attempt["salvage_material_present"]:
                errors.append(f"salvage_material_missing:{number}")
            if attempt["thinking_budget"] != 0:
                errors.append(f"salvage_thinking_budget:{number}")
            if not attempt["private_prefix_present"]:
                errors.append(f"salvage_private_prefix:{number}")
            if attempt["loop_tail_present"]:
                errors.append(f"salvage_loop_tail:{number}")
            continue
        if any(
            attempt[field]
            for field in (
                "salvage_material_present",
                "private_prefix_present",
                "loop_tail_present",
            )
        ):
            prefix = (
                "fresh_request_replayed_private_material"
                if attempt["phase"] == "fresh"
                else "non_salvage_replayed_private_material"
            )
            errors.append(f"{prefix}:{number}")
        if (
            attempt["phase"] == "fresh"
            and attempt.get("role") == "primary"
            and attempt["thinking_budget"] != THINKING_BUDGET
        ):
            errors.append(f"fresh_thinking_budget:{number}")

    if not summary.get("shielded_hold_pass"):
        errors.append("shielded_hold_client_failed")
    if not summary.get("fresh_negative_pass"):
        errors.append("fresh_client_failed")
    contract = summary.get("operational_contract", {})
    if contract.get("verified") is not True:
        errors.append("operational_contract_unverified")
    errors.extend(_operational_contract_evidence_errors(summary))

    process_cleanup = summary.get("process_cleanup", {})
    if not process_cleanup.get("exclusive_supervisor"):
        errors.append("cleanup_supervisor_not_exclusive")
    if not process_cleanup.get("supervisor_exited"):
        errors.append("supervisor_not_exited")
    if not process_cleanup.get("supervisor_reaped"):
        errors.append("supervisor_not_reaped")
    if not process_cleanup.get("candidate_ownership_quiesced"):
        errors.append("candidate_ownership_not_quiesced")
    if not process_cleanup.get("scope_fence_established"):
        errors.append("cleanup_scope_fence_missing")
    if process_cleanup.get("scope_populated_final") != 0:
        errors.append("cleanup_scope_populated")
    if (
        process_cleanup.get("scope_collected") is not True
        or process_cleanup.get("scope_unit_collected") is not True
        or process_cleanup.get("scope_cgroup_collected") is not True
    ):
        errors.append("cleanup_scope_not_collected")
    if process_cleanup.get("scope_collection_error"):
        errors.append("cleanup_scope_collection_failed")
    if not process_cleanup.get("scope_quiesced"):
        errors.append("cleanup_scope_not_quiesced")
    if process_cleanup.get("scope_error"):
        errors.append("cleanup_scope_failed")
    if process_cleanup.get("supervisor_error"):
        errors.append("supervisor_cleanup_failed")
    if process_cleanup.get("unexpected_exit"):
        errors.append("proxy_unexpected_exit")
    if process_cleanup.get("forced_kill"):
        errors.append("proxy_forced_kill")
    if not process_cleanup.get("spawn_identity_captured"):
        errors.append("proxy_spawn_identity_missing")
    if not process_cleanup.get("pidfd_available"):
        errors.append("proxy_pidfd_unavailable")
    if not process_cleanup.get("ownership_quiesced"):
        errors.append("proxy_ownership_not_quiesced")
    if process_cleanup.get("residual_producer_count_before_kill") != 0:
        errors.append("proxy_residual_producer_detected")
    if process_cleanup.get("residual_producer_count_final") != 0:
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
    if not summary.get("scan_limits_enforced"):
        errors.append("privacy_scan_limits_unverified")
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
    offline_self_test, binary_identity = run_offline_self_test(binary)
    require_executable_identity(binary, binary_identity, "binary_identity_drift")
    root = args.root
    root.mkdir(mode=0o700, parents=False)
    os.chmod(root, 0o700)
    (root / "state").mkdir(mode=0o700)
    (root / "home").mkdir(mode=0o700)
    (root / "tmp").mkdir(mode=0o700)
    sensitive_markers = tuple("S" + secrets.token_hex(24) for _ in range(6))
    reasoning_marker, private_prefix_marker, shielded_input_marker, fresh_input_marker, shielded_output_marker, fresh_output_marker = sensitive_markers
    generic_input_marker = shielded_input_marker
    fixture = Fixture(
        reasoning_marker,
        private_prefix_marker,
        fresh_input_marker,
        shielded_output_marker,
        fresh_output_marker,
    )
    fake_port, guard_port = free_port(), free_port()
    summary: dict = {
        "binary_path": str(binary),
        "binary_sha256": binary_identity.sha256,
        "offline_self_test": offline_self_test,
        "runtime_binary_sha256": None,
        "final_binary_sha256": None,
        "binary_identity_stable": False,
        "response_api_tested": False,
        "response_api_boundary": "unsupported",
        "fixture_root_mode": oct(root.stat().st_mode & 0o777),
        "ports": {"fake": fake_port, "guard": guard_port},
        "execution_error": None,
        "subreaper_enabled": False,
        "exclusive_supervisor": False,
        "scan_limits": {
            "max_entries": SCAN_MAX_ENTRIES,
            "max_file_bytes": SCAN_MAX_FILE_BYTES,
            "max_total_bytes": SCAN_MAX_TOTAL_BYTES,
            "deadline_seconds": SCAN_TIMEOUT,
        },
        "scan_limits_enforced": False,
        "operational_contract": {
            "verified": False,
            "arms": {},
            "blocker": "BLOCKER:not_executed",
        },
    }
    supervisor = None
    process_cleanup = None
    server = None
    server_thread = None
    try:
        config, hashes = isolated_config(candidate, root, fake_port, guard_port)
        summary.update(hashes)
        server = FixtureHTTPServer(("127.0.0.1", fake_port), fixture.handler(), fixture)
        env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"), "XDG_CACHE_HOME": str(root / "home" / ".cache"), "XDG_CONFIG_HOME": str(root / "home" / ".config"), "XDG_DATA_HOME": str(root / "home" / ".local" / "share"), "PATH": os.environ["PATH"]}
        supervisor = start_candidate_supervisor(
            [
                str(binary),
                "--config",
                str(config),
                "--guardian-runtime-dir",
                str(root / "guardian-runtime"),
            ],
            root,
            env,
            root / "proxy.log",
            (server.fileno(),),
        )
        summary["subreaper_enabled"] = bool(
            supervisor.started_receipt["subreaper_enabled"]
        )
        summary["exclusive_supervisor"] = bool(
            supervisor.started_receipt["exclusive_supervisor"]
        )
        candidate_identity = supervisor.started_receipt["candidate_identity"]
        runtime_identity = require_executable_identity(
            Path(f"/proc/{candidate_identity['pid']}/exe"),
            binary_identity,
            "runtime_binary_identity_drift",
            nofollow=False,
        )
        summary["runtime_binary_sha256"] = runtime_identity.sha256
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        server_thread.start()
        wait_port(guard_port, supervisor, time.monotonic() + 20)
        summary["candidate_full_toml_release_binary_parsed"] = True
        summary["operational_contract"]["arms"]["shielded_hold"] = client(
            guard_port,
            shielded_input_marker,
            shielded_output_marker,
            reasoning_marker,
            private_prefix_marker,
        )
        summary["operational_contract"]["arms"][
            "generic_committed_stream"
        ] = client(
            guard_port,
            generic_input_marker,
            "",
            reasoning_marker,
            private_prefix_marker,
            stream=True,
            endpoint="/v1/completions",
        )
        summary["fresh_client"] = client(
            guard_port,
            fresh_input_marker,
            fresh_output_marker,
            reasoning_marker,
            private_prefix_marker,
        )
    except SupervisorStartError as exc:
        summary["execution_error"] = exc.error
        summary["subreaper_enabled"] = exc.subreaper_enabled
        summary["exclusive_supervisor"] = exc.exclusive_supervisor
        process_cleanup = exc.cleanup
    except Exception as exc:
        summary["execution_error"] = _stable_error(exc, "execution_failed")

    if process_cleanup is None:
        if supervisor is not None:
            process_cleanup = stop_candidate_supervisor(supervisor)
        else:
            process_cleanup = stop(None)
            process_cleanup.update(
                {
                    "exclusive_supervisor": False,
                    "supervisor_exited": True,
                    "supervisor_reaped": True,
                    "candidate_ownership_quiesced": True,
                    "supervisor_error": summary["execution_error"],
                }
            )
    summary["process_cleanup"] = process_cleanup
    summary["fixture_cleanup"] = stop_fixture_server(server, server_thread, fixture)
    try:
        final_identity = require_executable_identity(
            binary, binary_identity, "final_binary_identity_drift"
        )
        summary["final_binary_sha256"] = final_identity.sha256
        summary["binary_identity_stable"] = (
            summary["runtime_binary_sha256"] == binary_identity.sha256
        )
    except RuntimeError as exc:
        if summary["execution_error"] is None:
            summary["execution_error"] = _stable_error(
                exc, "final_binary_identity_drift"
            )
    attempts, fixture_errors = fixture.snapshot()
    summary["attempts"] = attempts
    summary["fixture_errors"] = fixture_errors
    summary["salvage_count"] = sum(
        attempt.get("phase") == "shielded_hold"
        and attempt.get("role") == "salvage"
        for attempt in attempts
    )
    summary["shielded_hold_request_count"] = sum(
        attempt.get("phase") == "shielded_hold"
        and attempt.get("role") in {"primary", "salvage", "recovery_probe"}
        for attempt in attempts
    )
    summary["fresh_request_count"] = sum(
        attempt.get("phase") == "fresh" and attempt.get("role") == "primary"
        for attempt in attempts
    )
    summary["generic_committed_stream_request_count"] = sum(
        attempt.get("phase") == "generic_committed_stream"
        and attempt.get("role") in {"primary", "salvage", "recovery_probe"}
        for attempt in attempts
    )
    operational_errors = _operational_contract_evidence_errors(summary)
    summary["operational_contract"]["verified"] = not operational_errors
    summary["operational_contract"]["blocker"] = (
        None if not operational_errors else f"BLOCKER:{operational_errors[0]}"
    )

    shielded = summary["operational_contract"]["arms"].get("shielded_hold")
    fresh = summary.get("fresh_client")
    summary["shielded_hold_pass"] = (
        isinstance(shielded, dict)
        and shielded["status"] == 200
        and shielded["nonempty"]
        and shielded["valid_json"]
        and shielded["expected_final"]
        and shielded["heartbeat_count"] == 0
        and not shielded["downstream_prelude_detected"]
        and shielded["reasoning_marker_leak_count"] == 0
        and shielded["private_prefix_marker_leak_count"] == 0
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
        summary["subreaper_enabled"]
        and summary["exclusive_supervisor"]
        and summary["process_cleanup"]["exclusive_supervisor"]
        and summary["process_cleanup"]["supervisor_exited"]
        and summary["process_cleanup"]["supervisor_reaped"]
        and summary["process_cleanup"]["candidate_ownership_quiesced"]
        and not summary["process_cleanup"]["supervisor_error"]
        and summary["process_cleanup"]["proxy_exited"]
        and summary["process_cleanup"]["ownership_quiesced"]
        and summary["process_cleanup"]["group_quiesced"]
        and summary["process_cleanup"]["session_quiesced"]
        and summary["process_cleanup"]["residual_producer_count_final"] == 0
        and summary["fixture_cleanup"]["server_stopped"]
        and summary["fixture_cleanup"]["shutdown_stopped"]
        and summary["fixture_cleanup"]["handlers_quiesced"]
    )
    if lifecycle_final:
        scan_stats: dict[str, int | float | bool] = {}
        summary["scan_stats"] = scan_stats
        try:
            summary["sensitive_marker_leak_count_all_fixture_files"] = sensitive_marker_count(
                root,
                tuple(marker.encode() for marker in sensitive_markers),
                scan_stats,
            )
            summary["scan_errors"] = 0
            summary["scan_limits_enforced"] = bool(
                scan_stats.get("completed")
                and scan_stats.get("entry_count", SCAN_MAX_ENTRIES + 1)
                <= SCAN_MAX_ENTRIES
                and scan_stats.get("logical_bytes", SCAN_MAX_TOTAL_BYTES + 1)
                <= SCAN_MAX_TOTAL_BYTES
                and scan_stats.get("largest_file_bytes", SCAN_MAX_FILE_BYTES + 1)
                <= SCAN_MAX_FILE_BYTES
                and scan_stats.get("elapsed_milliseconds", SCAN_TIMEOUT * 1_000 + 1)
                < SCAN_TIMEOUT * 1_000
            )
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
    if len(sys.argv) == 3 and sys.argv[1] == "--exclusive-supervisor-fd":
        raise SystemExit(_exclusive_supervisor_worker(int(sys.argv[2])))
    raise SystemExit(main())
