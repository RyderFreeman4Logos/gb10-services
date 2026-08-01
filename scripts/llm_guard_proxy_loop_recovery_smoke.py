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
CGROUP_ROOT = Path("/sys/fs/cgroup")


class ProcessIdentity(NamedTuple):
    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int
    starttime: int
    pidfd: int | None = None
    pidfd_errno: int | None = None


class ScopeFence(NamedTuple):
    unit: str
    control_group: str
    events_fd: int
    kill_fd: int


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
        current = _read_process_identity(supervisor.pid)
        if (
            current is None
            or not _same_process(current, supervisor.identity)
            or current.state in {"X", "Z"}
        ):
            fail("supervisor_exit_before_ready")
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


def _process_control_group(pid: int) -> str:
    lines = (Path("/proc") / str(pid) / "cgroup").read_text(encoding="ascii").splitlines()
    unified = [line.split("::", 1)[1] for line in lines if line.startswith("0::")]
    if len(unified) != 1 or not unified[0].startswith("/"):
        raise OSError(errno.EIO, "unified_cgroup_identity_invalid")
    return unified[0]


def _establish_scope_fence(
    control: socket.socket, unit: str, pid: int, deadline: float
) -> ScopeFence:
    receipt = _recv_supervisor_receipt(control, deadline)
    if receipt.get("kind") != "scope_ready" or receipt.get("pid") != pid:
        raise OSError(errno.ESTALE, "scope_worker_identity_invalid")
    control_group = _process_control_group(pid)
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
    try:
        kill_fd = os.open(
            cgroup_path / "cgroup.kill",
            os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except Exception:
        os.close(events_fd)
        raise
    scope = ScopeFence(unit, control_group, events_fd, kill_fd)
    try:
        populated, removed = _scope_population(events_fd)
        members = {
            int(member) for member in (cgroup_path / "cgroup.procs").read_text().split()
        }
        if removed or populated != 1 or pid not in members:
            raise OSError(errno.ESTALE, "scope_membership_invalid")
        return scope
    except Exception:
        _close_scope_fence(scope)
        raise


def _fence_supervisor_scope(
    cleanup: dict[str, object], scope: ScopeFence
) -> dict[str, object]:
    deadline = time.monotonic() + SUPERVISOR_EMERGENCY_TIMEOUT
    kill_sent = False
    populated_final: int | None = None
    removed = False
    error: dict[str, str] | None = None
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
        error = _stable_error(exc, "scope_fence_failed")
    finally:
        _close_scope_fence(scope)

    scope_quiesced = error is None and populated_final == 0
    cleanup.update(
        {
            "scope_fence_established": True,
            "scope_unit": scope.unit,
            "scope_control_group": scope.control_group,
            "scope_kill_all_sent": kill_sent,
            "scope_populated_final": populated_final,
            "scope_removed": removed,
            "scope_quiesced": scope_quiesced,
            "scope_error": error,
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
    supervisor_identity: ProcessIdentity | None = None
    try:
        scope = _establish_scope_fence(parent_control, unit, pid, deadline)
        supervisor_identity = capture_process_identity(pid)
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
            pid, supervisor_identity, parent_control, error
        )
        cleanup.update(scope_cleanup)
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
                pid, supervisor_identity, parent_control, error
            )
        else:
            cleanup_value = receipt.get("process_cleanup")
            cleanup = (
                cleanup_value if isinstance(cleanup_value, dict) else stop(None)
            )
            _finalize_supervisor_cleanup(cleanup, status, error)
            parent_control.close()
            _close_identity_pidfd(supervisor_identity)
        _fence_supervisor_scope(cleanup, scope)
        raise SupervisorStartError(
            error,
            cleanup,
            bool(receipt.get("subreaper_enabled")),
            bool(receipt.get("exclusive_supervisor")),
        )

    try:
        current_identity = _read_process_identity(pid)
        receipt_identity = receipt.get("supervisor_identity")
        if not _same_process(current_identity, supervisor_identity):
            raise OSError(errno.ESTALE, "supervisor_identity_changed")
        if not isinstance(receipt_identity, dict) or (
            receipt_identity.get("pid"), receipt_identity.get("starttime")
        ) != (supervisor_identity.pid, supervisor_identity.starttime):
            raise OSError(errno.ESTALE, "supervisor_identity_changed")
        if not receipt.get("exclusive_supervisor") or not receipt.get(
            "subreaper_enabled"
        ):
            raise RuntimeError("exclusive_supervisor_not_enabled")
    except Exception as exc:
        error = _stable_error(exc, "supervisor_identity_capture_failed")
        scope_cleanup = _fence_supervisor_scope({}, scope)
        cleanup = _emergency_supervisor_cleanup(
            pid,
            supervisor_identity,
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
        }
    )
    return SupervisorHandle(pid, supervisor_identity, parent_control, receipt, scope)


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


def acceptance_errors(summary: dict) -> list[str]:
    errors: list[str] = []
    if summary.get("execution_error") is not None:
        errors.append("execution_failed")
    if not summary.get("subreaper_enabled"):
        errors.append("child_subreaper_not_enabled")
    if not summary.get("exclusive_supervisor"):
        errors.append("exclusive_supervisor_missing")
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
        "subreaper_enabled": False,
        "exclusive_supervisor": False,
        "scan_limits": {
            "max_entries": SCAN_MAX_ENTRIES,
            "max_file_bytes": SCAN_MAX_FILE_BYTES,
            "max_total_bytes": SCAN_MAX_TOTAL_BYTES,
            "deadline_seconds": SCAN_TIMEOUT,
        },
        "scan_limits_enforced": False,
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
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        server_thread.start()
        wait_port(guard_port, supervisor, time.monotonic() + 20)
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
