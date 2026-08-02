from __future__ import annotations

import ctypes
import errno
import io
import importlib.util
import json
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "llm_guard_proxy_loop_recovery_smoke",
    ROOT / "scripts" / "llm_guard_proxy_loop_recovery_smoke.py",
)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def enable_test_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "test_subreaper_setup_failed")


def fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def start_direct_supervisor_worker() -> tuple[subprocess.Popen[bytes], socket.socket]:
    parent, child = socket.socketpair(
        socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "llm_guard_proxy_loop_recovery_smoke.py"),
            "--exclusive-supervisor-fd",
            str(child.fileno()),
        ],
        pass_fds=(child.fileno(),),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child.close()
    return proc, parent


def post_fixture(
    port: int,
    path: str,
    body: dict,
    *,
    probe: str | None = None,
) -> None:
    headers = {"Content-Type": "application/json"}
    if probe is not None:
        headers["x-llm-guard-proxy-probe"] = probe
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers=headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=2).read()
    except urllib.error.HTTPError:
        pass


def send_direct_launch(
    control: socket.socket,
    packet: dict[str, object],
    executable_fd: int | None = None,
) -> None:
    payload = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    ancillary = (
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, bytes(ctypes.c_int(executable_fd)))]
        if executable_fd is not None
        else []
    )
    sent = control.sendmsg([payload], ancillary)
    if sent != len(payload):
        raise RuntimeError("direct_supervisor_launch_truncated")


def finish_direct_supervisor(
    proc: subprocess.Popen[bytes], control: socket.socket, started: bool
) -> None:
    try:
        if started and proc.poll() is None:
            smoke._send_supervisor_packet(
                control, b"stop", time.monotonic() + 2
            )
            smoke._recv_supervisor_receipt(control, time.monotonic() + 4)
    finally:
        control.close()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def run_client_fixture(
    chunks: tuple[bytes, ...],
    *,
    content_type: str,
    chunk_delay: float = 0.0,
    **client_kwargs: object,
) -> tuple[dict, list[dict]]:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length))
            request["_fixture_path"] = self.path
            requests.append(request)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in chunks:
                time.sleep(chunk_delay)
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = smoke.client(
            server.server_port,
            "input",
            "expected",
            "reasoning-private-marker",
            "prefix-private-marker",
            **client_kwargs,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return result, requests


def write_forking_systemd_run_bridge(path: Path) -> None:
    real_systemd_run = smoke.SYSTEMD_RUN_BIN
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        f"real_systemd_run = {real_systemd_run!r}\n"
        "args = sys.argv[1:]\n"
        "if worker := os.environ.get('SCOPE_READY_TEST_WORKER'):\n"
        "    separator = args.index('--')\n"
        "    args[separator + 1:] = [\n"
        "        sys.executable, worker,\n"
        "        os.environ['SCOPE_READY_TEST_MARKER'],\n"
        "        os.environ['SCOPE_READY_TEST_PID'],\n"
        "    ]\n"
        "if marker := os.environ.get('SCOPE_WRAPPER_MARKER'):\n"
        "    Path(marker).write_text(str(os.getpid()), encoding='ascii')\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.closerange(3, os.sysconf('SC_OPEN_MAX'))\n"
        "    os.execv(real_systemd_run, [real_systemd_run, *args])\n"
        "if os.environ.get('SCOPE_WRAPPER_EXIT_EARLY'):\n"
        "    os._exit(0)\n"
        "_, status = os.waitpid(pid, 0)\n"
        "code = os.waitstatus_to_exitcode(status)\n"
        "os._exit(code if code >= 0 else 128 - code)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def write_systemctl_collection_bridge(path: Path) -> None:
    path.write_text(
        r'''#!/usr/bin/env python3
import os
import pathlib
import sys

unit = os.environ["SCOPE_COLLECT_EXPECTED_UNIT"]
if sys.argv[1:] != [
    "--user",
    "show",
    "--property=LoadState",
    "--value",
    "--",
    unit,
]:
    raise SystemExit(91)
mode = os.environ["SCOPE_COLLECT_MODE"]
if mode == "error":
    print("scope collection diagnostics must not enter receipts", file=sys.stderr)
    raise SystemExit(17)
if mode == "malformed":
    print(" not-found ")
    raise SystemExit(0)
counter = pathlib.Path(os.environ["SCOPE_COLLECT_COUNTER"])
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
print("not-found" if mode == "delayed" and count >= 3 else "loaded")
''',
        encoding="utf-8",
    )
    path.chmod(0o700)


def write_invalid_scope_ready_worker(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations
import json
import os
import socket
import sys
from pathlib import Path

control_group = next(
    line.split("::", 1)[1]
    for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    if line.startswith("0::")
)
Path(sys.argv[1]).write_text(
    json.dumps({"pid": os.getpid(), "control_group": control_group}),
    encoding="ascii",
)
control = socket.socket(fileno=0)
control.send(json.dumps({
    "kind": "scope_ready",
    "pid": json.loads(sys.argv[2]),
    "control_group": control_group,
}).encode())
try:
    while control.recv(4096):
        pass
except OSError:
    pass
""",
        encoding="utf-8",
    )


def attempt(
    number: int,
    phase: str,
    *,
    budget: int,
    salvage: bool = False,
    private: bool = False,
    loop: bool = False,
    endpoint: str = "chat_completions",
    role: str | None = None,
    admitted: int | None = None,
    upstream_first_event: int | None = None,
) -> dict:
    item = {
        "number": number,
        "endpoint": endpoint,
        "phase": phase,
        "role": role or ("salvage" if salvage else "primary"),
        "admitted_monotonic_ns": admitted if admitted is not None else number * 10,
        "thinking_budget": budget,
        "stream": True,
        "stream_usage": endpoint == "chat_completions",
        "salvage_material_present": salvage,
        "private_prefix_present": private,
        "loop_tail_present": loop,
    }
    if upstream_first_event is not None:
        item["upstream_first_event_monotonic_ns"] = upstream_first_event
    return item


def passing_summary() -> dict:
    tick_ns = smoke.PROBE_HEARTBEAT_INTERVAL_SECS * 1_000_000_000
    return {
        "offline_self_test": offline_receipt(),
        "binary_identity_stable": True,
        "attempts": [
            attempt(
                1,
                "shielded_hold",
                budget=smoke.THINKING_BUDGET,
                admitted=10,
                upstream_first_event=tick_ns + 10,
            ),
            attempt(
                2,
                "shielded_hold",
                budget=0,
                salvage=True,
                private=True,
                admitted=tick_ns + 20,
            ),
            attempt(
                3,
                "generic_committed_stream",
                endpoint="completions",
                budget=0,
                admitted=tick_ns + 40,
            ),
            attempt(
                4,
                "fresh",
                budget=smoke.THINKING_BUDGET,
                admitted=tick_ns + 60,
            ),
        ],
        "fixture_errors": [],
        "shielded_hold_pass": True,
        "fresh_negative_pass": True,
        "fresh_client": {
            "first_downstream_nonempty_monotonic_ns": tick_ns + 70,
        },
        "operational_contract": {
            "verified": True,
            "blocker": None,
            "arms": {
                "shielded_hold": {
                    "endpoint": "/v1/chat/completions",
                    "caller_stream": False,
                    "status": 200,
                    "content_type": "application/json",
                    "nonempty": True,
                    "valid_json": True,
                    "expected_final": True,
                    "first_downstream_nonempty_monotonic_ns": tick_ns + 30,
                    "downstream_prelude_detected": False,
                    "protocol_error": None,
                    "heartbeat_count": 0,
                    "reasoning_marker_leak_count": 0,
                    "private_prefix_marker_leak_count": 0,
                },
                "generic_committed_stream": {
                    "endpoint": "/v1/completions",
                    "caller_stream": True,
                    "status": 200,
                    "content_type": "text/event-stream",
                    "nonempty": True,
                    "expected_final": False,
                    "first_downstream_nonempty_monotonic_ns": tick_ns + 50,
                    "downstream_prelude_detected": True,
                    "protocol_valid_terminal_failure": True,
                    "protocol_error": None,
                    "heartbeat_count": 1,
                    "sse_error_event_count": 1,
                    "sse_done_count": 1,
                    "reasoning_marker_leak_count": 0,
                    "private_prefix_marker_leak_count": 0,
                },
            },
        },
        "execution_error": None,
        "subreaper_enabled": True,
        "exclusive_supervisor": True,
        "process_cleanup": {
            "exclusive_supervisor": True,
            "supervisor_exited": True,
            "supervisor_reaped": True,
            "candidate_ownership_quiesced": True,
            "scope_fence_established": True,
            "scope_populated_final": 0,
            "scope_removed": False,
            "scope_unit_collected": True,
            "scope_cgroup_collected": True,
            "scope_collected": True,
            "scope_collection_error": None,
            "scope_collection_duration_ms": 1,
            "scope_quiesced": True,
            "scope_error": None,
            "supervisor_error": None,
            "proxy_exited": True,
            "unexpected_exit": False,
            "graceful_stop": True,
            "forced_kill": False,
            "spawn_identity_captured": True,
            "pidfd_available": True,
            "ownership_quiesced": True,
            "group_quiesced": True,
            "session_quiesced": True,
            "residual_producer_count_before_kill": 0,
            "residual_producer_count_final": 0,
        },
        "fixture_cleanup": {
            "server_stopped": True,
            "shutdown_stopped": True,
            "handlers_quiesced": True,
        },
        "port_cleanup": {"guard_rebindable": True, "fake_rebindable": True},
        "sensitive_marker_leak_count_all_fixture_files": 0,
        "scan_errors": 0,
        "scan_limits_enforced": True,
    }


def offline_receipt() -> dict:
    return {
        "self_test": "post-await-no-replay",
        "status": "passed",
        "control": {
            "ordered_roles": ["business", "recovery_probe", "business"],
            "product_roles": ["business", "readiness_probe", "recovery_replay"],
            "attempt_count": 3,
            "fixture_rejected_count": 0,
            "request_claims": 1,
            "rejected_request_claims": 0,
            "recovery_replay_claims": 1,
            "rejected_recovery_replay_claims": 0,
            "rejected_physical_attempts": 0,
            "rejected_readiness_probes": 0,
            "business_count": 2,
            "probe_count": 1,
            "same_payload": True,
            "first_chunk_stall": True,
            "first_byte_wait_ms": 50,
            "client_observed_heartbeat": False,
            "done_observed": True,
            "terminal_error_observed": False,
            "eof_observed": True,
            "post_await_committed": False,
            "phases": {
                "pre_await_gate_ns": 1,
                "recovery_await_entered_ns": 2,
                "body_emitted_ns": 0,
                "client_ack_ns": 0,
                "recovery_await_completed_ns": 3,
                "control_replay_authorized_ns": 4,
                "post_await_committed_ns": 0,
            },
            "loopback_only": True,
            "cleanup_complete": True,
        },
        "committed": {
            "ordered_roles": ["business"],
            "product_roles": ["business"],
            "attempt_count": 1,
            "fixture_rejected_count": 0,
            "request_claims": 1,
            "rejected_request_claims": 0,
            "recovery_replay_claims": 0,
            "rejected_recovery_replay_claims": 0,
            "rejected_physical_attempts": 0,
            "rejected_readiness_probes": 0,
            "business_count": 1,
            "probe_count": 0,
            "same_payload": True,
            "first_chunk_stall": True,
            "first_byte_wait_ms": 50,
            "client_observed_heartbeat": True,
            "done_observed": False,
            "terminal_error_observed": True,
            "eof_observed": True,
            "post_await_committed": True,
            "phases": {
                "pre_await_gate_ns": 1,
                "recovery_await_entered_ns": 2,
                "body_emitted_ns": 3,
                "client_ack_ns": 4,
                "recovery_await_completed_ns": 5,
                "control_replay_authorized_ns": 0,
                "post_await_committed_ns": 6,
            },
            "loopback_only": True,
            "cleanup_complete": True,
        },
        "same_payload_across_arms": True,
    }


class GuardOfflineSelfTestTests(unittest.TestCase):
    def setUp(self) -> None:
        enable_test_subreaper()
        self.exclusive_supervisor_pid = smoke._EXCLUSIVE_SUPERVISOR_PID
        smoke._EXCLUSIVE_SUPERVISOR_PID = os.getpid()

    def tearDown(self) -> None:
        smoke._EXCLUSIVE_SUPERVISOR_PID = self.exclusive_supervisor_pid

    def write_self_test(
        self,
        path: Path,
        *,
        receipt: dict | None = None,
        body: str | None = None,
    ) -> None:
        if body is None:
            body = f"print({json.dumps(receipt or offline_receipt())!r})\n"
        path.write_text("#!/usr/bin/python3\n" + body, encoding="utf-8")
        path.chmod(0o700)

    def test_valid_receipt_runs_exact_command_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "guard"
            body = (
                "import os, sys\n"
                f"assert sys.argv[1:] == {list(smoke.OFFLINE_SELF_TEST_ARGV)!r}\n"
                "assert os.environ == {'LANG': 'C', 'LC_ALL': 'C', "
                "'PATH': '/usr/bin:/bin'}\n"
                f"print({json.dumps(offline_receipt())!r})\n"
            )
            self.write_self_test(binary, body=body)
            receipt, identity = smoke.run_offline_self_test(binary)
            digest = smoke.sha256(binary)
        self.assertEqual(receipt, offline_receipt())
        self.assertEqual(identity.sha256, digest)

    def test_complete_output_waits_for_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "guard"
            self.write_self_test(
                binary,
                body=(
                    "import os, time\n"
                    f"print({json.dumps(offline_receipt())!r}, flush=True)\n"
                    "os.close(1)\n"
                    "os.close(2)\n"
                    "time.sleep(0.1)\n"
                ),
            )
            started = time.monotonic()
            receipt, _ = smoke.run_offline_self_test(binary)
        self.assertGreaterEqual(time.monotonic() - started, 0.1)
        self.assertEqual(receipt, offline_receipt())

    def test_complete_output_timeout_reaps_all_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "guard"
            root_pid = root / "root.pid"
            child_pid = root / "child.pid"
            self.write_self_test(
                binary,
                body=(
                    "import os, signal, time\n"
                    f"open({str(root_pid)!r}, 'w').write(str(os.getpid()))\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "ready_r, ready_w = os.pipe()\n"
                    "if os.fork() == 0:\n"
                    "    os.close(ready_r)\n"
                    "    os.close(1)\n"
                    "    os.close(2)\n"
                    f"    open({str(child_pid)!r}, 'w').write(str(os.getpid()))\n"
                    "    os.write(ready_w, b'1')\n"
                    "    os.close(ready_w)\n"
                    "    time.sleep(30)\n"
                    "    os._exit(0)\n"
                    "os.close(ready_w)\n"
                    "os.read(ready_r, 1)\n"
                    "os.close(ready_r)\n"
                    f"print({json.dumps(offline_receipt())!r}, flush=True)\n"
                    "os.close(1)\n"
                    "os.close(2)\n"
                    "time.sleep(30)\n"
                ),
            )
            started = time.monotonic()
            with (
                patch.object(smoke, "OFFLINE_SELF_TEST_TIMEOUT", 0.1),
                patch.object(smoke, "PROCESS_TERM_GRACE", 0.05),
                patch.object(smoke, "PROCESS_STOP_TIMEOUT", 1.0),
                self.assertRaisesRegex(RuntimeError, "offline_self_test_timeout"),
            ):
                smoke.run_offline_self_test(binary)
            elapsed = time.monotonic() - started
            pids = (int(root_pid.read_text()), int(child_pid.read_text()))
            survived = [pid for pid in pids if smoke.process_identity_is_live(pid)]
            try:
                self.assertLess(elapsed, 1.5)
                self.assertEqual(survived, [])
                for pid in pids:
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(pid, os.WNOHANG)
            finally:
                for pid in survived:
                    os.kill(pid, signal.SIGKILL)

    def test_direct_supervisor_outside_scope_rejects_before_offline_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "offline-ran"
            binary = root / "candidate"
            self.write_self_test(
                binary,
                body=(
                    "import pathlib\n"
                    f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
                    f"print({json.dumps(offline_receipt())!r})\n"
                ),
            )
            proc, control = start_direct_supervisor_worker()
            executable_fd = os.open(binary, os.O_RDONLY | os.O_CLOEXEC)
            receipts = []
            try:
                send_direct_launch(
                    control,
                    {
                        "kind": "authorize",
                        "argv0": str(binary),
                        "test_failpoint": None,
                        "control_timeout": 5,
                    },
                    executable_fd,
                )
                while len(receipts) < 2:
                    try:
                        receipts.append(
                            smoke._recv_supervisor_receipt(
                                control, time.monotonic() + 1
                            )
                        )
                    except (EOFError, OSError, TimeoutError):
                        break
            finally:
                os.close(executable_fd)
                control.close()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            self.assertNotIn("scope_ready", {item.get("kind") for item in receipts})
            self.assertFalse(marker.exists())

    def test_runtime_launch_uses_sealed_bytes_after_in_place_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "candidate"
            legitimate = root / "legitimate"
            hostile = root / "hostile"
            source = root / "candidate.c"
            receipt_literal = json.dumps(
                json.dumps(offline_receipt(), separators=(",", ":"))
            )
            source.write_text(
                "#include <fcntl.h>\n"
                "#include <signal.h>\n"
                "#include <string.h>\n"
                "#include <unistd.h>\n"
                "static void stop(int signal_number) { _exit(0); }\n"
                "int main(int argc, char **argv) {\n"
                "  if (argc == 3 && !strcmp(argv[1], \"self-test\") && "
                "!strcmp(argv[2], \"post-await-no-replay\")) {\n"
                f"    static const char receipt[] = {receipt_literal};\n"
                "    write(1, receipt, sizeof(receipt) - 1);\n"
                "    write(1, \"\\n\", 1);\n"
                "    return 0;\n"
                "  }\n"
                "  int fd = open(argv[1], O_WRONLY | O_CREAT | O_TRUNC, 0600);\n"
                "  write(fd, \"ran\", 3);\n"
                "  close(fd);\n"
                "  signal(SIGTERM, stop);\n"
                "  for (;;) pause();\n"
                "}\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["/usr/bin/cc", "-O2", "-o", str(binary), str(source)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            authorization = smoke.authorize_candidate_supervisor(
                binary, str(binary)
            )
            hostile_bytes = (
                "#!/usr/bin/python3\n"
                "import pathlib, signal, time\n"
                f"pathlib.Path({str(hostile)!r}).write_text('ran')\n"
                "signal.signal(signal.SIGTERM, lambda *_: exit(0))\n"
                "while True: time.sleep(0.05)\n"
            ).encode()
            with binary.open("r+b", buffering=0) as output:
                output.truncate(0)
                output.write(hostile_bytes)
            supervisor = None
            try:
                supervisor = smoke.launch_candidate_supervisor(
                    authorization,
                    [str(binary), str(legitimate)],
                    root,
                    {"PATH": "/usr/bin:/bin"},
                    root / "candidate.log",
                )
                deadline = time.monotonic() + 1
                while (
                    not legitimate.exists()
                    and not hostile.exists()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
            finally:
                if supervisor is not None:
                    smoke.stop_candidate_supervisor(supervisor)
                elif not authorization.consumed:
                    smoke.stop_authorized_supervisor(
                        authorization,
                        {"class": "TestCleanup", "code": "test_cleanup"},
                    )
            self.assertTrue(legitimate.exists())
            self.assertFalse(hostile.exists())

    def test_sealed_executable_bytes_ignore_in_place_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "candidate"
            self.write_self_test(binary)
            expected = smoke.sha256(binary)
            fd = smoke._sealed_executable_fd(binary)
            try:
                with binary.open("r+b", buffering=0) as output:
                    output.truncate(0)
                    output.write(b"#!/bin/sh\nexit 99\n")
                smoke._require_sealed_executable(fd)
                self.assertEqual(smoke._executable_identity_fd(fd).sha256, expected)
                with self.assertRaises(OSError):
                    os.write(fd, b"hostile")
            finally:
                os.close(fd)

    def test_readiness_rejects_different_executable_listener_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            port = smoke.free_port()
            listener = root / "listener.py"
            listener.write_text(
                "import socket, sys, time\n"
                "with socket.socket() as sock:\n"
                "    sock.bind(('127.0.0.1', int(sys.argv[1])))\n"
                "    sock.listen()\n"
                "    time.sleep(30)\n",
                encoding="utf-8",
            )
            candidate = root / "candidate"
            candidate.write_text(
                "#!/bin/sh\n"
                f"/usr/bin/python3 {listener} {port} &\n"
                "wait\n",
                encoding="utf-8",
            )
            candidate.chmod(0o700)
            proc = subprocess.Popen([str(candidate)], start_new_session=True)
            parent, child = socket.socketpair()
            identity = smoke.capture_spawn_identity(proc.pid)
            expected = smoke.executable_identity(
                Path(f"/proc/{proc.pid}/exe"), nofollow=False
            )
            handle = smoke.SupervisorHandle(
                proc.pid,
                identity,
                parent,
                {
                    "candidate_identity": smoke._identity_receipt(identity),
                    "binary_identity": expected._asdict(),
                },
                smoke.ScopeFence("test.scope", "/test.scope", -1, -1, identity),
            )
            try:
                with self.assertRaises(RuntimeError):
                    smoke.wait_port(port, handle, time.monotonic() + 2)
            finally:
                child.close()
                parent.close()
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
                smoke._close_identity_pidfd(identity)

    def test_process_failures_stop_before_network(self) -> None:
        cases = {
            "nonzero": "raise SystemExit(7)\n",
            "signal": "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n",
            "timeout": "import time\ntime.sleep(10)\n",
            "multiline": "print('{}')\nprint('{}')\n",
            "non_json": "print('not-json')\n",
            "stderr": "import sys\nprint('diagnostic', file=sys.stderr)\n",
            "output_limit": f"print('x' * {smoke.OFFLINE_SELF_TEST_OUTPUT_LIMIT})\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, body in cases.items():
                binary = root / name
                self.write_self_test(binary, body=body)
                timeout = 0.05 if name == "timeout" else smoke.OFFLINE_SELF_TEST_TIMEOUT
                with (
                    self.subTest(name=name),
                    patch.object(smoke, "OFFLINE_SELF_TEST_TIMEOUT", timeout),
                    patch.object(smoke, "free_port", side_effect=AssertionError("network")),
                    self.assertRaises(RuntimeError) as caught,
                ):
                    smoke.run_offline_self_test(binary)
                self.assertNotIn("network", str(caught.exception))

            binary = root / "main-invalid"
            self.write_self_test(binary, body="print('not-json')\n")
            argv = [
                "smoke",
                "--candidate-config",
                str(ROOT / "config" / "llm-guard-proxy" / "config.toml"),
                "--binary",
                str(binary),
                "--root",
                str(root / "run"),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(smoke, "free_port") as free_port,
                redirect_stdout(io.StringIO()),
            ):
                result = smoke.main()
            self.assertEqual(result, 1)
            free_port.assert_not_called()

    def test_receipt_schema_and_f1_invariants_fail_closed(self) -> None:
        mutations = (
            ("unknown", lambda value: value.update(extra=True)),
            ("missing", lambda value: value.pop("status")),
            ("wrong_type", lambda value: value["control"].update(business_count=True)),
            ("control_b2", lambda value: value["control"].update(business_count=1)),
            ("control_probe", lambda value: value["control"].update(probe_count=0)),
            ("control_roles", lambda value: value["control"].update(product_roles=["business"])),
            ("committed_b2", lambda value: value["committed"].update(business_count=2)),
            ("committed_probe", lambda value: value["committed"].update(probe_count=1)),
            ("committed_replay", lambda value: value["committed"].update(recovery_replay_claims=1)),
            ("committed_salvage", lambda value: value["committed"].update(ordered_roles=["business", "salvage"])),
            ("committed_shadow", lambda value: value["committed"].update(product_roles=["business", "shadow"])),
            ("committed_failover", lambda value: value["committed"].update(ordered_roles=["business", "failover"])),
            ("committed_done", lambda value: value["committed"].update(done_observed=True)),
            ("committed_success", lambda value: value["committed"].update(terminal_error_observed=False)),
        )
        for name, mutate in mutations:
            receipt = offline_receipt()
            mutate(receipt)
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                smoke.validate_offline_self_test_receipt(receipt)

    def test_private_field_is_rejected_without_echo(self) -> None:
        receipt = offline_receipt()
        marker = "private-marker-must-not-echo"
        receipt["raw_prompt"] = marker
        with self.assertRaises(RuntimeError) as caught:
            smoke.validate_offline_self_test_receipt(receipt)
        self.assertNotIn(marker, str(caught.exception))

    def test_duplicate_json_members_are_rejected_recursively(self) -> None:
        raw = json.dumps(offline_receipt(), separators=(",", ":"))
        cases = {
            "top_level": raw.replace(
                '"status":"passed"',
                '"status":"failed","status":"passed"',
                1,
            ),
            "nested": raw.replace(
                '"business_count":2',
                '"business_count":1,"business_count":2',
                1,
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, payload in cases.items():
                binary = Path(tmp) / name
                self.write_self_test(binary, body=f"print({payload!r})\n")
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    smoke.run_offline_self_test(binary)

    def test_executable_replacement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "guard"
            replacement = root / "replacement"
            self.write_self_test(binary)
            _, identity = smoke.run_offline_self_test(binary)
            self.write_self_test(replacement, body="print('replacement')\n")
            os.replace(replacement, binary)
            with self.assertRaises(RuntimeError):
                smoke.require_executable_identity(binary, identity, "binary_identity_drift")
        summary = passing_summary()
        summary["binary_identity_stable"] = False
        self.assertIn("binary_identity_drift", smoke.acceptance_errors(summary))

    def test_failure_paths_reap_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, tail, timeout in (
                ("timeout", "import time\ntime.sleep(10)\n", 0.05),
                ("error", "print('not-json')\n", smoke.OFFLINE_SELF_TEST_TIMEOUT),
            ):
                pid_file = root / f"{name}.pid"
                binary = root / name
                body = (
                    "import os\n"
                    f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                    + tail
                )
                self.write_self_test(binary, body=body)
                with (
                    self.subTest(name=name),
                    patch.object(smoke, "OFFLINE_SELF_TEST_TIMEOUT", timeout),
                    self.assertRaises(RuntimeError),
                ):
                    smoke.run_offline_self_test(binary)
                pid = int(pid_file.read_text())
                with self.assertRaises(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)

    def test_offline_self_test_fences_setsid_pipe_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "guard"
            child_pid = root / "child.pid"
            body = (
                "import os, signal, time\n"
                "if os.fork() == 0:\n"
                "    os.setsid()\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"    open({str(child_pid)!r}, 'w').write(str(os.getpid()))\n"
                "    time.sleep(30)\n"
                f"print({json.dumps(offline_receipt())!r})\n"
            )
            self.write_self_test(binary, body=body)
            with (
                patch.object(smoke, "OFFLINE_SELF_TEST_TIMEOUT", 0.2),
                self.assertRaises(RuntimeError),
            ):
                smoke.run_offline_self_test(binary)
            pid = int(child_pid.read_text())
            survived = smoke.process_identity_is_live(pid)
            try:
                self.assertFalse(survived, "escaped offline descendant survived")
            finally:
                if survived:
                    os.kill(pid, signal.SIGKILL)


class LoopRecoveryFinalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fake_guard_tmp = tempfile.TemporaryDirectory()
        root = Path(cls.fake_guard_tmp.name)
        source = root / "fake_guard.c"
        cls.fake_guard = root / "fake_guard"
        receipt_literal = json.dumps(
            json.dumps(offline_receipt(), separators=(",", ":"))
        )
        source.write_text(
            "#include <Python.h>\n"
            "#include <string.h>\n"
            "#include <unistd.h>\n"
            "int main(int argc, char **argv) {\n"
            "  if (argc == 3 && !strcmp(argv[1], \"self-test\") && "
            "!strcmp(argv[2], \"post-await-no-replay\")) {\n"
            f"    static const char receipt[] = {receipt_literal};\n"
            "    write(1, receipt, sizeof(receipt) - 1);\n"
            "    write(1, \"\\n\", 1);\n"
            "    return 0;\n"
            "  }\n"
            "  return Py_BytesMain(argc, argv);\n"
            "}\n",
            encoding="utf-8",
        )
        flags = shlex.split(
            subprocess.check_output(
                ["/usr/bin/python3-config", "--embed", "--cflags", "--ldflags"],
                text=True,
            )
        )
        subprocess.run(
            ["/usr/bin/cc", str(source), "-o", str(cls.fake_guard), *flags],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fake_guard_tmp.cleanup()

    def test_final_acceptance_checks_every_attempt_privacy_and_budget(self) -> None:
        self.assertFalse(smoke.acceptance_errors(passing_summary()))

        for budget in (1024, smoke.THINKING_BUDGET):
            extra_shadow = passing_summary()
            extra_shadow["attempts"].append(
                attempt(5, "shielded_hold", budget=budget, role="shadow")
            )
            with self.subTest(extra_shadow_budget=budget):
                self.assertIn(
                    "shielded_hold_business_attempt_count",
                    smoke.acceptance_errors(extra_shadow),
                )

        for field in (
            "salvage_material_present",
            "private_prefix_present",
            "loop_tail_present",
        ):
            private_shadow = passing_summary()
            private_shadow["attempts"].append(
                attempt(
                    5,
                    "shielded_hold",
                    budget=smoke.THINKING_BUDGET,
                    role="shadow",
                )
            )
            private_shadow["attempts"][-1][field] = True
            with self.subTest(shadow_field=field):
                self.assertIn(
                    "non_salvage_replayed_private_material:5",
                    smoke.acceptance_errors(private_shadow),
                )

        for role in ("shadow", "recovery_probe", "primary", "unknown"):
            private_replay = passing_summary()
            replay = dict(private_replay["attempts"][1])
            replay.update(
                number=5,
                admitted_monotonic_ns=replay["admitted_monotonic_ns"] + 1,
                role=role,
            )
            private_replay["attempts"].append(replay)
            with self.subTest(private_salvage_role=role):
                self.assertIn(
                    "non_salvage_replayed_private_material:5",
                    smoke.acceptance_errors(private_replay),
                )

        for field, value, code in (
            ("phase", "fresh", "salvage_phase:2"),
            ("thinking_budget", 1, "salvage_thinking_budget:2"),
            (
                "salvage_material_present",
                False,
                "salvage_material_missing:2",
            ),
            ("private_prefix_present", False, "salvage_private_prefix:2"),
            ("loop_tail_present", True, "salvage_loop_tail:2"),
        ):
            invalid_salvage = passing_summary()
            invalid_salvage["attempts"][1][field] = value
            with self.subTest(field=field):
                self.assertIn(code, smoke.acceptance_errors(invalid_salvage))

    def test_final_acceptance_checks_late_errors_and_every_fresh_attempt(self) -> None:
        late_error = passing_summary()
        late_error["fixture_errors"].append("late_fixture_error")
        self.assertIn("late_fixture_error", smoke.acceptance_errors(late_error))

        late_fresh_shadow = passing_summary()
        late_fresh_shadow["attempts"].append(
            attempt(5, "fresh", budget=0, role="shadow")
        )
        self.assertIn(
            "fresh_business_attempt_count",
            smoke.acceptance_errors(late_fresh_shadow),
        )

        late_replay = passing_summary()
        late_replay["attempts"].append(
            attempt(
                5,
                "fresh",
                budget=smoke.THINKING_BUDGET,
                private=True,
                loop=True,
                role="shadow",
            )
        )
        self.assertIn(
            "fresh_request_replayed_private_material:5",
            smoke.acceptance_errors(late_replay),
        )

        wrong_fresh_budget = passing_summary()
        wrong_fresh_budget["attempts"].append(
            attempt(5, "fresh", budget=0)
        )
        self.assertIn(
            "fresh_thinking_budget:5",
            smoke.acceptance_errors(wrong_fresh_budget),
        )

        duplicate_salvage = passing_summary()
        duplicate_salvage["attempts"].append(
            attempt(5, "shielded_hold", budget=0, salvage=True, private=True)
        )
        self.assertIn("salvage_count", smoke.acceptance_errors(duplicate_salvage))

    def test_client_rejects_nonempty_prelude_before_stream_false_final(self) -> None:
        final = json.dumps(
            {"choices": [{"message": {"content": "expected"}}]},
            separators=(",", ":"),
        ).encode()
        final_event = b"event: final\ndata: " + final + b"\n\n"
        for name, chunks, content_type in (
            (
                "heartbeat",
                (b": heartbeat\n\n", final_event),
                "text/event-stream",
            ),
            (
                "sse-comment",
                (b": keepalive\n\n", final_event),
                "text/event-stream",
            ),
            ("json-whitespace", (b" \n", final), "application/json"),
        ):
            with self.subTest(name=name):
                result, requests = run_client_fixture(
                    chunks, content_type=content_type
                )
                self.assertEqual(len(requests), 1)
                self.assertFalse(requests[0]["stream"])
                self.assertFalse(result["expected_final"])
                self.assertTrue(result["downstream_prelude_detected"])
                self.assertIsInstance(
                    result["first_downstream_nonempty_monotonic_ns"], int
                )

    def test_final_acceptance_requires_two_arm_operational_contract(self) -> None:
        summary = passing_summary()
        summary["operational_contract"] = {
            "verified": False,
            "blocker": "shielded_hold_not_observed",
        }
        self.assertIn(
            "operational_contract_unverified",
            smoke.acceptance_errors(summary),
        )

    def test_operational_contract_rejects_post_commit_business_attempts(
        self,
    ) -> None:
        late_primary = passing_summary()
        shielded_first = late_primary["operational_contract"]["arms"][
            "shielded_hold"
        ]["first_downstream_nonempty_monotonic_ns"]
        late_primary["attempts"].append(
            attempt(
                5,
                "shielded_hold",
                budget=smoke.THINKING_BUDGET,
                admitted=shielded_first + 1,
            )
        )
        self.assertIn(
            "shielded_hold_post_commit_attempt:5",
            smoke.acceptance_errors(late_primary),
        )

        late_salvage = passing_summary()
        late_salvage["attempts"][1]["admitted_monotonic_ns"] = shielded_first + 1
        self.assertIn(
            "shielded_hold_salvage_after_first_byte",
            smoke.acceptance_errors(late_salvage),
        )

        for role in ("shadow", "unknown-future-role"):
            late_relabel = passing_summary()
            late_relabel["attempts"].append(
                attempt(
                    5,
                    "shielded_hold",
                    budget=smoke.THINKING_BUDGET,
                    role=role,
                    admitted=shielded_first + 1,
                )
            )
            with self.subTest(role=role):
                self.assertIn(
                    "shielded_hold_post_commit_attempt:5",
                    smoke.acceptance_errors(late_relabel),
                )

        generic_retry = passing_summary()
        generic_first = generic_retry["operational_contract"]["arms"][
            "generic_committed_stream"
        ]["first_downstream_nonempty_monotonic_ns"]
        generic_retry["attempts"].append(
            attempt(
                5,
                "generic_committed_stream",
                endpoint="completions",
                budget=0,
                admitted=generic_first + 1,
            )
        )
        errors = smoke.acceptance_errors(generic_retry)
        self.assertIn("generic_committed_stream_attempt_count", errors)
        self.assertIn("generic_committed_stream_post_commit_attempt:5", errors)

    def test_client_incrementally_frames_sse_and_bounds_input(self) -> None:
        final = json.dumps(
            {"choices": [{"message": {"content": "expected"}}]},
            separators=(",", ":"),
        ).encode()
        frame = b"event: final\r\ndata: " + final + b"\r\n\r\n"
        result, requests = run_client_fixture(
            tuple(bytes((byte,)) for byte in frame),
            content_type="text/event-stream",
        )
        self.assertEqual(len(requests), 1)
        self.assertTrue(result["expected_final"])
        self.assertFalse(result["downstream_prelude_detected"])

        error = (
            b": heartbeat\r\n\r\n"
            b"event: error\r\ndata: {\"error\":{\"code\":\"failed\"}}\r\n\r\n"
            b"data: [DONE]\r\n\r\n"
        )
        result, requests = run_client_fixture(
            tuple(bytes((byte,)) for byte in error),
            content_type="text/event-stream",
            stream=True,
        )
        self.assertTrue(requests[0]["stream"])
        self.assertTrue(result["protocol_valid_terminal_failure"])
        self.assertEqual(result["sse_error_event_count"], 1)
        self.assertEqual(result["sse_done_count"], 1)
        self.assertEqual(result["heartbeat_count"], 1)
        self.assertTrue(result["downstream_prelude_detected"])

        generic_result, generic_requests = run_client_fixture(
            (error,),
            content_type="text/event-stream",
            stream=True,
            endpoint="/v1/completions",
        )
        self.assertTrue(generic_result["protocol_valid_terminal_failure"])
        self.assertEqual(generic_requests[0]["_fixture_path"], "/v1/completions")
        self.assertEqual(generic_requests[0]["prompt"], "input")
        self.assertNotIn("messages", generic_requests[0])

        done = (
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
            b"data: [DONE]\n\n"
        )
        result, _ = run_client_fixture(
            tuple(bytes((byte,)) for byte in done),
            content_type="text/event-stream",
            stream=True,
        )
        self.assertIsNone(result["protocol_error"])
        self.assertEqual(result["sse_done_count"], 1)

        result, _ = run_client_fixture(
            (b"x" * 65,),
            content_type="application/json",
            max_response_bytes=64,
        )
        self.assertEqual(result["protocol_error"], "response_body_limit")

        result, _ = run_client_fixture(
            (final,),
            content_type="application/json",
            chunk_delay=0.1,
            deadline_seconds=0.05,
        )
        self.assertEqual(result["protocol_error"], "response_deadline")

    def test_client_treats_only_closed_response_socket_as_eof(self) -> None:
        final = json.dumps(
            {"choices": [{"message": {"content": "expected"}}]},
            separators=(",", ":"),
        ).encode()

        class Headers:
            @staticmethod
            def get_content_type() -> str:
                return "application/json"

        class ClosingResponse:
            status = 200
            headers = Headers()

            def __init__(self, response_closed: bool) -> None:
                self.sock = socket.socket()
                self.response_closed = response_closed
                self.read_calls = 0
                self.closed_after_nonempty = False

            def read1(self, _size: int) -> bytes:
                self.read_calls += 1
                self.sock.close()
                self.closed_after_nonempty = bool(final) and self.sock.fileno() == -1
                return final

            read = read1

            def isclosed(self) -> bool:
                return self.response_closed

            def close(self) -> None:
                self.sock.close()

        response = ClosingResponse(response_closed=True)
        with (
            patch.object(smoke.urllib.request, "urlopen", return_value=response),
            patch.object(smoke, "_response_socket", return_value=response.sock),
        ):
            result = smoke.client(1, "input", "expected", "reasoning", "private")
        self.assertTrue(response.closed_after_nonempty)
        self.assertEqual(response.read_calls, 1)
        self.assertTrue(result["expected_final"])

        incomplete = ClosingResponse(response_closed=False)
        with (
            patch.object(smoke.urllib.request, "urlopen", return_value=incomplete),
            patch.object(smoke, "_response_socket", return_value=incomplete.sock),
            self.assertRaises(OSError) as caught,
        ):
            smoke.client(1, "input", "expected", "reasoning", "private")
        self.assertTrue(incomplete.closed_after_nonempty)
        self.assertFalse(incomplete.response_closed)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_isolated_config_preserves_recovery_and_shortens_heartbeat(self) -> None:
        candidate = ROOT / "config" / "llm-guard-proxy" / "config.toml"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            derived_path, receipt = smoke.isolated_config(
                candidate,
                root,
                smoke.free_port(),
                smoke.free_port(),
            )
            derived = smoke.tomllib.loads(derived_path.read_text())
        profile = next(
            item for item in derived["upstreams"] if item["name"] == "aeon-guard-max"
        )
        self.assertEqual(derived["heartbeat"]["interval_secs"], 1)
        self.assertEqual(profile["thinking"]["budget_tokens"], smoke.THINKING_BUDGET)
        self.assertEqual(profile["thinking"]["max_tokens"], smoke.CALLER_MAX)
        self.assertEqual(
            profile["loop_guard"]["cot_salvage_prefix_max_bytes"], 32_768
        )
        self.assertEqual(receipt["isolated_heartbeat_interval_secs"], 1)

    def test_operational_acceptance_requires_valid_two_arm_protocols(self) -> None:
        shielded_prelude = passing_summary()
        shielded_prelude["operational_contract"]["arms"]["shielded_hold"].update(
            {"heartbeat_count": 1, "downstream_prelude_detected": True}
        )
        self.assertIn(
            "shielded_hold_downstream_prelude",
            smoke.acceptance_errors(shielded_prelude),
        )

        early_loop = passing_summary()
        early_loop["attempts"][0]["upstream_first_event_monotonic_ns"] = 100
        self.assertIn(
            "shielded_hold_not_delayed_across_heartbeat",
            smoke.acceptance_errors(early_loop),
        )

        generic_uncommitted = passing_summary()
        generic_uncommitted["operational_contract"]["arms"][
            "generic_committed_stream"
        ]["downstream_prelude_detected"] = False
        self.assertIn(
            "generic_committed_stream_prelude_missing",
            smoke.acceptance_errors(generic_uncommitted),
        )

        generic_success = passing_summary()
        generic_success["operational_contract"]["arms"][
            "generic_committed_stream"
        ]["protocol_valid_terminal_failure"] = False
        self.assertIn(
            "generic_committed_stream_terminal_failure_missing",
            smoke.acceptance_errors(generic_success),
        )

        recovery_probe = passing_summary()
        recovery_probe["attempts"].append(
            attempt(
                5,
                "shielded_hold",
                budget=smoke.THINKING_BUDGET,
                role="recovery_probe",
            )
        )
        self.assertIn(
            "shielded_hold_recovery_probe",
            smoke.acceptance_errors(recovery_probe),
        )

    def test_fixture_classifies_endpoint_phase_and_semantic_role(self) -> None:
        fixture = smoke.Fixture(
            "reason",
            "secret-marker",
            "fresh",
            "positive-output",
            "fresh-output",
        )

        def body(content: str, budget: int = smoke.THINKING_BUDGET) -> dict:
            return {
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": smoke.CALLER_MAX,
                "thinking": {"budget_tokens": budget},
                "messages": [{"role": "user", "content": content}],
            }

        primary = fixture.inspect_request("chat_completions", body("raw-chat-secret"))
        fresh = fixture.inspect_request("chat_completions", body("fresh"))
        salvage_body = body("raw-chat-secret", 0)
        salvage_body["messages"].append(
            {
                "role": "assistant",
                "content": "Private bounded pre-loop reasoning notes: secret-marker",
            }
        )
        salvage = fixture.inspect_request("chat_completions", salvage_body)
        shadow = fixture.inspect_request("chat_completions", body("raw-chat-secret"))
        generic = fixture.inspect_request(
            "completions", {"stream": True, "prompt": "raw-prompt-secret"}
        )
        probe = fixture.inspect_request(
            "chat_completions", smoke.READINESS_PROBE_BODY, recovery_probe=True
        )

        self.assertEqual(
            [
                (item["endpoint"], item["phase"], item["role"])
                for item in (primary, fresh, salvage, shadow, generic, probe)
            ],
            [
                ("chat_completions", "shielded_hold", "primary"),
                ("chat_completions", "fresh", "primary"),
                ("chat_completions", "shielded_hold", "salvage"),
                ("chat_completions", "shielded_hold", "shadow"),
                ("completions", "generic_committed_stream", "primary"),
                ("chat_completions", "shielded_hold", "recovery_probe"),
            ],
        )
        timestamps = [
            item["admitted_monotonic_ns"]
            for item in (primary, fresh, salvage, shadow, generic, probe)
        ]
        self.assertEqual(timestamps, sorted(timestamps))
        receipt = json.dumps(fixture.snapshot()[0])
        self.assertNotIn("secret-marker", receipt)
        self.assertNotIn("raw-chat-secret", receipt)
        self.assertNotIn("raw-prompt-secret", receipt)

    def test_fixture_records_actual_post_commit_sibling_traffic(self) -> None:
        fresh_body = {
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": smoke.CALLER_MAX,
            "thinking": {"budget_tokens": smoke.THINKING_BUDGET},
            "messages": [{"role": "user", "content": "fresh"}],
        }
        cases = (
            ("embeddings", "/v1/embeddings", {"input": "fresh"}, None),
            ("fresh_shadow", "/v1/chat/completions", fresh_body, None),
            ("unknown_role", "/v1/chat/completions", fresh_body, "future-role"),
            (
                "forged_recovery_probe",
                "/v1/chat/completions",
                fresh_body,
                "local-recovery",
            ),
        )
        for name, path, body, probe in cases:
            with self.subTest(name=name):
                fixture = smoke.Fixture(
                    "reason", "private", "fresh", "positive", "fresh-output"
                )
                if name != "embeddings":
                    fixture.inspect_request("chat_completions", fresh_body)
                server = smoke.FixtureHTTPServer(
                    ("127.0.0.1", 0), fixture.handler(), fixture
                )
                server.server_activate()
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    post_fixture(server.server_port, path, body, probe=probe)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
                attempts, _ = fixture.snapshot()
                self.assertTrue(attempts)
                summary = passing_summary()
                summary["attempts"].append(dict(attempts[-1], number=5))
                self.assertTrue(smoke.acceptance_errors(summary))

    def test_exact_readiness_probe_requires_full_semantic_tuple(self) -> None:
        fixture = smoke.Fixture(
            "reason", "private", "fresh", "positive", "fresh-output"
        )
        exact = {
            "model": "aeon-ultimate",
            "messages": [{"role": "user", "content": "1+1=?"}],
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": 1,
        }
        probe = fixture.inspect_request(
            "chat_completions", exact, recovery_probe=True
        )
        forged = fixture.inspect_request(
            "chat_completions",
            {"model": "aeon-ultimate", "messages": []},
            recovery_probe=True,
        )
        self.assertEqual(probe["role"], "recovery_probe")
        self.assertNotEqual(forged["role"], "recovery_probe")

    def test_fixture_listener_requires_explicit_activation(self) -> None:
        fixture = smoke.Fixture(
            "reason", "private", "fresh", "positive", "fresh-output"
        )
        server = smoke.FixtureHTTPServer(
            ("127.0.0.1", 0), fixture.handler(), fixture
        )
        try:
            with socket.socket() as client_sock:
                self.assertNotEqual(
                    client_sock.connect_ex(server.server_address), 0
                )
            server.server_activate()
            with socket.create_connection(server.server_address, timeout=1):
                pass
        finally:
            server.server_close()

    def test_fixture_cannot_serve_before_candidate_listener_attestation(self) -> None:
        fixture = smoke.Fixture(
            "reason", "private", "fresh", "positive", "fresh-output"
        )
        fake_port = smoke.free_port()
        guard_port = smoke.free_port()
        server = smoke.FixtureHTTPServer(
            ("127.0.0.1", fake_port), fixture.handler(), fixture
        )
        thread = None
        candidate = r"""
import json, os, signal, socket, sys, time, urllib.request
fake_port, guard_port = map(int, sys.argv[1:])
body = json.dumps({"model":"aeon-ultimate","messages":[]}).encode()
request = urllib.request.Request(
    f"http://127.0.0.1:{fake_port}/v1/chat/completions",
    data=body,
    headers={"Content-Type":"application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(request, timeout=0.5).read()
except Exception:
    pass
if os.fork() == 0:
    os.setsid()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", guard_port))
        listener.listen()
        while True:
            time.sleep(1)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
while True:
    time.sleep(1)
"""
        supervisor = None
        try:
            supervisor = smoke.start_candidate_supervisor(
                [sys.executable, "-c", candidate, str(fake_port), str(guard_port)],
                Path(self.fake_guard_tmp.name),
                os.environ.copy(),
                Path(self.fake_guard_tmp.name) / "candidate-listener.log",
                binary=self.fake_guard,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            with self.assertRaisesRegex(
                RuntimeError, "^candidate_listener_owner_mismatch$"
            ):
                smoke.wait_port(guard_port, supervisor, time.monotonic() + 2)
        finally:
            if supervisor is not None:
                smoke.stop_candidate_supervisor(supervisor)
            smoke.stop_fixture_server(server, thread, fixture)
        self.assertEqual(fixture.snapshot()[0], [])

    def test_final_snapshot_waits_for_handler_quiescence(self) -> None:
        fixture = smoke.Fixture("reason", "private", "fresh", "positive", "fresh-output")
        fixture.handler_started()
        release = threading.Event()
        completed = threading.Event()
        receipt: list[tuple[dict, list[str]]] = []

        def late_handler() -> None:
            release.wait()
            fixture.record_error("late_fixture_error")
            fixture.handler_finished()

        def finalize() -> None:
            cleanup = smoke.stop_fixture_server(None, None, fixture)
            _, errors = fixture.snapshot()
            receipt.append((cleanup, errors))
            completed.set()

        handler = threading.Thread(target=late_handler)
        finalizer = threading.Thread(target=finalize)
        handler.start()
        finalizer.start()
        self.assertFalse(completed.wait(timeout=0.05))
        release.set()
        handler.join(timeout=1)
        finalizer.join(timeout=1)
        self.assertFalse(handler.is_alive())
        self.assertFalse(finalizer.is_alive())
        self.assertTrue(receipt[0][0]["handlers_quiesced"])
        self.assertIn("late_fixture_error", receipt[0][1])

    def test_unexpected_exit_and_forced_kill_cannot_pass(self) -> None:
        for field in ("unexpected_exit", "forced_kill"):
            summary = passing_summary()
            summary["process_cleanup"][field] = True
            with self.subTest(field=field):
                self.assertTrue(smoke.acceptance_errors(summary))

    def test_exclusive_supervisor_receipts_are_required(self) -> None:
        for scope, field, code in (
            (None, "exclusive_supervisor", "exclusive_supervisor_missing"),
            ("process_cleanup", "exclusive_supervisor", "cleanup_supervisor_not_exclusive"),
            ("process_cleanup", "supervisor_exited", "supervisor_not_exited"),
            ("process_cleanup", "supervisor_reaped", "supervisor_not_reaped"),
            (
                "process_cleanup",
                "candidate_ownership_quiesced",
                "candidate_ownership_not_quiesced",
            ),
            (
                "process_cleanup",
                "scope_fence_established",
                "cleanup_scope_fence_missing",
            ),
            ("process_cleanup", "scope_quiesced", "cleanup_scope_not_quiesced"),
        ):
            summary = passing_summary()
            target = summary if scope is None else summary[scope]
            target[field] = False
            with self.subTest(field=field):
                self.assertIn(code, smoke.acceptance_errors(summary))

        summary = passing_summary()
        summary["process_cleanup"]["supervisor_error"] = {
            "class": "TimeoutError",
            "code": "supervisor_control_timeout",
        }
        self.assertIn("supervisor_cleanup_failed", smoke.acceptance_errors(summary))

        populated = passing_summary()
        populated["process_cleanup"]["scope_populated_final"] = 1
        self.assertIn("cleanup_scope_populated", smoke.acceptance_errors(populated))

        scope_error = passing_summary()
        scope_error["process_cleanup"]["scope_error"] = {
            "class": "TimeoutError",
            "code": "scope_quiescence_timeout",
        }
        self.assertIn("cleanup_scope_failed", smoke.acceptance_errors(scope_error))

    def test_final_acceptance_requires_scope_collection(self) -> None:
        not_collected = passing_summary()
        not_collected["process_cleanup"]["scope_collected"] = False
        self.assertIn(
            "cleanup_scope_not_collected",
            smoke.acceptance_errors(not_collected),
        )

        collection_error = passing_summary()
        collection_error["process_cleanup"]["scope_collection_error"] = {
            "class": "RuntimeError",
            "code": "scope_collect_systemctl_failed",
        }
        self.assertIn(
            "cleanup_scope_collection_failed",
            smoke.acceptance_errors(collection_error),
        )

    def test_scope_collection_probe_permission_error_is_stable(self) -> None:
        cleanup: dict[str, object] = {}
        unit = "guard-collect-permission.scope"
        control_group = f"/private/{unit}"
        denied = PermissionError(errno.EACCES, control_group)
        collected_state = subprocess.CompletedProcess(
            [],
            0,
            "not-found\n",
            "",
        )

        started = time.monotonic()
        with (
            patch.object(smoke.subprocess, "run", return_value=collected_state),
            patch.object(smoke, "_scope_cgroup_collected", side_effect=denied),
        ):
            result = smoke._collect_supervisor_scope(
                cleanup,
                unit,
                control_group,
            )
        elapsed = time.monotonic() - started

        self.assertIs(result, cleanup)
        self.assertEqual(cleanup["scope_unit"], unit)
        self.assertFalse(cleanup["scope_unit_collected"])
        self.assertIsNone(cleanup["scope_cgroup_collected"])
        self.assertFalse(cleanup["scope_collected"])
        self.assertEqual(
            cleanup["scope_collection_error"],
            {"class": "PermissionError", "code": "EACCES"},
        )
        duration_ms = cleanup["scope_collection_duration_ms"]
        self.assertIsInstance(duration_ms, int)
        assert isinstance(duration_ms, int)
        self.assertGreaterEqual(duration_ms, 0)
        self.assertLess(duration_ms, 500)
        self.assertLess(elapsed, 0.5)

    def test_scope_collection_bridge_is_bounded_exact_and_closes_fds_once(self) -> None:
        for mode, collected, error_code in (
            ("delayed", True, None),
            ("no_collect", False, "scope_collect_timeout"),
            ("error", False, "scope_collect_systemctl_failed"),
            ("malformed", False, "scope_collect_state_invalid"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bridge = root / "systemctl"
                counter = root / "counter"
                write_systemctl_collection_bridge(bridge)
                unit = f"guard-collect-{secrets.token_hex(12)}.scope"
                events = root / "cgroup.events"
                kill = root / "cgroup.kill"
                events.write_text("populated 0\n", encoding="ascii")
                kill.write_bytes(b"")
                scope = smoke.ScopeFence(
                    unit,
                    f"/test/{unit}",
                    os.open(events, os.O_RDONLY | os.O_CLOEXEC),
                    os.open(kill, os.O_WRONLY | os.O_CLOEXEC),
                    smoke.ProcessIdentity(
                        os.getpid(),
                        "R",
                        os.getppid(),
                        os.getpgrp(),
                        os.getsid(0),
                        1,
                    ),
                )
                env = {
                    "SCOPE_COLLECT_EXPECTED_UNIT": unit,
                    "SCOPE_COLLECT_COUNTER": str(counter),
                    "SCOPE_COLLECT_MODE": mode,
                }
                cleanup: dict[str, object] = {}
                started = time.monotonic()
                with (
                    patch.object(smoke, "SYSTEMCTL_BIN", str(bridge), create=True),
                    patch.object(smoke, "SCOPE_COLLECT_TIMEOUT", 0.12, create=True),
                    patch.dict(os.environ, env),
                    patch.object(
                        smoke,
                        "_close_scope_fence",
                        wraps=smoke._close_scope_fence,
                    ) as close_scope,
                ):
                    smoke._fence_supervisor_scope(cleanup, scope)
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 0.5)
                self.assertEqual(close_scope.call_count, 1)
                self.assertEqual(cleanup["scope_unit"], unit)
                self.assertFalse(cleanup["scope_removed"])
                self.assertEqual(cleanup["scope_collected"], collected)
                self.assertTrue(cleanup["scope_quiesced"])
                self.assertTrue(cleanup["candidate_ownership_quiesced"])
                if error_code is None:
                    self.assertIsNone(cleanup["scope_collection_error"])
                else:
                    collection_error = cleanup["scope_collection_error"]
                    self.assertIsInstance(collection_error, dict)
                    assert isinstance(collection_error, dict)
                    self.assertEqual(
                        collection_error["code"],
                        error_code,
                    )
                    self.assertNotIn("scope collection diagnostics", repr(cleanup))
                for fd in (scope.events_fd, scope.kill_fd):
                    with self.assertRaises(OSError) as closed:
                        os.fstat(fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_direct_stop_never_claims_unrelated_post_baseline_child(self) -> None:
        child = r"""
import os, signal, sys, time
ready = sys.argv[1]
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""
        unrelated_child = r"""
import os, signal, sys, time
ready, term = sys.argv[1:]
def terminated(*_):
    with open(term, "w", encoding="ascii") as output:
        output.write("term")
    os._exit(73)
signal.signal(signal.SIGTERM, terminated)
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_ready = root / "candidate.ready"
            unrelated_ready = root / "unrelated.ready"
            unrelated_term = root / "unrelated.term"
            baseline = smoke._capture_direct_children()
            proc = subprocess.Popen(
                [sys.executable, "-c", child, str(candidate_ready)],
                start_new_session=True,
            )
            identity = smoke.capture_spawn_identity(proc.pid)
            unrelated = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    unrelated_child,
                    str(unrelated_ready),
                    str(unrelated_term),
                ],
                start_new_session=True,
            )
            unrelated_identity = smoke.capture_spawn_identity(unrelated.pid)
            try:
                deadline = time.monotonic() + 2
                while (
                    not candidate_ready.exists() or not unrelated_ready.exists()
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(candidate_ready.exists())
                self.assertTrue(unrelated_ready.exists())

                cleanup = smoke.stop(proc, identity, baseline)
                current = smoke._read_process_identity(unrelated.pid)

                self.assertTrue(smoke._same_process(current, unrelated_identity))
                self.assertIsNone(unrelated.poll())
                self.assertFalse(unrelated_term.exists())
                self.assertTrue(cleanup["ownership_quiesced"])
                self.assertEqual(cleanup["residual_producer_count_final"], 0)
            finally:
                if proc.returncode is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except ChildProcessError:
                        pass
                if unrelated.returncode is None:
                    try:
                        os.killpg(unrelated.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        unrelated.wait(timeout=2)
                    except ChildProcessError:
                        pass
                    smoke._close_identity_pidfd(unrelated_identity)

    def test_systemd_run_distinct_wrapper_preserves_stdio_control(self) -> None:
        candidate = r"""
import os, signal, sys, time
ready = sys.argv[1]
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""

        before_fds = fd_count()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = root / "candidate.ready"
            bridge = root / "systemd-run"
            wrapper_marker = root / "wrapper.pid"
            write_forking_systemd_run_bridge(bridge)
            supervisor = None
            candidate_pid = None
            worker_pid = None
            scope_path = None
            with (
                patch.object(smoke, "SYSTEMD_RUN_BIN", str(bridge)),
                patch.dict(
                    os.environ,
                    {"SCOPE_WRAPPER_MARKER": str(wrapper_marker)},
                ),
            ):
                try:
                    supervisor = smoke.start_candidate_supervisor(
                        [sys.executable, "-c", candidate, str(ready)],
                        root,
                        os.environ.copy(),
                        root / "candidate.log",
                        binary=self.fake_guard,
                    )
                    candidate_pid = supervisor.started_receipt["candidate_identity"][
                        "pid"
                    ]
                    worker_pid = supervisor.scope.worker_identity.pid
                    scope_path = smoke.CGROUP_ROOT / supervisor.scope.control_group.removeprefix(
                        "/"
                    )
                    self.assertEqual(int(wrapper_marker.read_text()), supervisor.pid)
                    self.assertNotEqual(supervisor.pid, worker_pid)
                    self.assertEqual(
                        supervisor.started_receipt["scope_worker_pid"], worker_pid
                    )
                    self.assertEqual(
                        supervisor.started_receipt["scope_worker_starttime"],
                        supervisor.scope.worker_identity.starttime,
                    )
                    self.assertEqual(
                        supervisor.started_receipt["candidate_identity"]["ppid"],
                        worker_pid,
                    )
                    self.assertTrue(
                        smoke._same_process(
                            smoke._read_process_identity(worker_pid),
                            supervisor.scope.worker_identity,
                        )
                    )
                    members = {
                        int(pid)
                        for pid in (scope_path / "cgroup.procs").read_text().split()
                    }
                    self.assertIn(worker_pid, members)
                    self.assertIn(candidate_pid, members)
                    deadline = time.monotonic() + 2
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())

                    started = time.monotonic()
                    cleanup = smoke.stop_candidate_supervisor(supervisor)

                    self.assertLess(time.monotonic() - started, 2.0)
                    self.assertIsNone(cleanup["supervisor_error"])
                    self.assertTrue(cleanup["candidate_ownership_quiesced"])
                    self.assertTrue(cleanup["supervisor_exited"])
                    self.assertTrue(cleanup["supervisor_reaped"])
                    self.assertTrue(cleanup["scope_collected"])
                    self.assertIsNone(cleanup["scope_collection_error"])
                    self.assertFalse(smoke.process_identity_is_live(candidate_pid))
                    self.assertFalse(smoke.process_identity_is_live(worker_pid))
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(supervisor.pid, os.WNOHANG)
                    deadline = time.monotonic() + 2
                    while scope_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(scope_path.exists())
                finally:
                    if supervisor is not None and smoke.process_identity_is_live(
                        supervisor.pid
                    ):
                        smoke.stop_candidate_supervisor(supervisor)
                    if candidate_pid is not None and smoke.process_identity_is_live(
                        candidate_pid
                    ):
                        os.kill(candidate_pid, signal.SIGKILL)
        self.assertEqual(fd_count(), before_fds)

    def test_wait_port_follows_scope_worker_after_wrapper_exit(self) -> None:
        candidate = r"""
import os, signal, socket, sys, time
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
time.sleep(0.25)
with socket.socket() as listener:
    listener.bind(("127.0.0.1", int(sys.argv[1])))
    listener.listen()
    while True:
        time.sleep(1)
"""
        before_fds = fd_count()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = root / "systemd-run"
            write_forking_systemd_run_bridge(bridge)
            supervisor = None
            cleanup = None
            with (
                patch.object(smoke, "SYSTEMD_RUN_BIN", str(bridge)),
                patch.dict(os.environ, {"SCOPE_WRAPPER_EXIT_EARLY": "1"}),
            ):
                try:
                    port = smoke.free_port()
                    supervisor = smoke.start_candidate_supervisor(
                        [sys.executable, "-c", candidate, str(port)],
                        root,
                        os.environ.copy(),
                        root / "candidate.log",
                        binary=self.fake_guard,
                    )
                    wrapper = smoke._read_process_identity(supervisor.pid)
                    self.assertIsNotNone(wrapper)
                    assert wrapper is not None
                    self.assertEqual(wrapper.state, "Z")
                    self.assertNotEqual(
                        supervisor.pid, supervisor.scope.worker_identity.pid
                    )

                    smoke.wait_port(port, supervisor, time.monotonic() + 2)

                    worker = smoke._read_process_identity(
                        supervisor.scope.worker_identity.pid
                    )
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    self.assertTrue(
                        smoke._same_process(worker, supervisor.scope.worker_identity)
                    )
                    self.assertNotIn(worker.state, {"X", "Z"})
                finally:
                    if supervisor is not None:
                        started = time.monotonic()
                        cleanup = smoke.stop_candidate_supervisor(supervisor)
                        self.assertLess(time.monotonic() - started, 2.0)
            self.assertIsNotNone(cleanup)
            assert cleanup is not None
            self.assertTrue(cleanup["candidate_ownership_quiesced"])
            self.assertTrue(cleanup["supervisor_reaped"])
        self.assertEqual(fd_count(), before_fds)

    def test_wait_port_fails_when_scope_worker_exits_with_live_wrapper(self) -> None:
        before_fds = fd_count()
        wrapper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        wrapper_identity = smoke.capture_process_identity(wrapper.pid)
        worker_identity = smoke.capture_process_identity(worker.pid)
        control, peer = socket.socketpair()
        supervisor = smoke.SupervisorHandle(
            wrapper.pid,
            wrapper_identity,
            control,
            {},
            smoke.ScopeFence("test.scope", "/test.scope", -1, -1, worker_identity),
        )
        try:
            worker.kill()
            deadline = time.monotonic() + 2
            current_worker = smoke._read_process_identity(worker.pid)
            while (
                current_worker is not None
                and current_worker.state != "Z"
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                current_worker = smoke._read_process_identity(worker.pid)
            self.assertIsNotNone(current_worker)
            assert current_worker is not None
            self.assertEqual(current_worker.state, "Z")
            current_wrapper = smoke._read_process_identity(wrapper.pid)
            self.assertIsNotNone(current_wrapper)
            assert current_wrapper is not None
            self.assertTrue(smoke._same_process(current_wrapper, wrapper_identity))
            self.assertNotIn(current_wrapper.state, {"X", "Z"})

            started = time.monotonic()
            with self.assertRaisesRegex(
                RuntimeError, "^supervisor_exit_before_ready$"
            ):
                smoke.wait_port(
                    smoke.free_port(), supervisor, time.monotonic() + 1
                )
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            for proc in (worker, wrapper):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
            control.close()
            peer.close()
            smoke._close_identity_pidfd(worker_identity)
            smoke._close_identity_pidfd(wrapper_identity)
        self.assertEqual(fd_count(), before_fds)

    def test_invalid_scope_worker_pid_fails_closed_and_cleans_scope(self) -> None:
        before_fds = fd_count()
        for name, value, expected_code in (
            ("malformed", json.dumps("not-a-pid"), "scope_worker_identity_invalid"),
            ("bool", "true", "scope_worker_identity_invalid"),
            ("foreign", None, "scope_identity_invalid"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bridge = root / "systemd-run"
                worker = root / "invalid_worker.py"
                wrapper_marker = root / "wrapper.pid"
                worker_marker = root / "worker.json"
                write_forking_systemd_run_bridge(bridge)
                write_invalid_scope_ready_worker(worker)
                foreign = None
                if value is None:
                    foreign = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"]
                    )
                    value = str(foreign.pid)
                environment = {
                    "SCOPE_WRAPPER_MARKER": str(wrapper_marker),
                    "SCOPE_READY_TEST_WORKER": str(worker),
                    "SCOPE_READY_TEST_MARKER": str(worker_marker),
                    "SCOPE_READY_TEST_PID": value,
                }
                try:
                    with (
                        patch.object(smoke, "SYSTEMD_RUN_BIN", str(bridge)),
                        patch.dict(os.environ, environment),
                        self.assertRaises(smoke.SupervisorStartError) as caught,
                    ):
                        smoke.start_candidate_supervisor(
                            [sys.executable, "-c", "raise SystemExit(99)"],
                            root,
                            os.environ.copy(),
                            root / "candidate.log",
                            binary=self.fake_guard,
                        )
                    self.assertEqual(caught.exception.error["code"], "ESTALE")
                    self.assertIsInstance(caught.exception.__cause__, OSError)
                    self.assertEqual(caught.exception.__cause__.args[1], expected_code)
                    self.assertTrue(caught.exception.cleanup["supervisor_exited"])
                    self.assertTrue(caught.exception.cleanup["supervisor_reaped"])
                    self.assertTrue(caught.exception.cleanup["scope_collected"])
                    self.assertIsNone(
                        caught.exception.cleanup["scope_collection_error"]
                    )
                    wrapper_pid = int(wrapper_marker.read_text())
                    worker_info = json.loads(worker_marker.read_text())
                    worker_pid = worker_info["pid"]
                    scope_path = smoke.CGROUP_ROOT / worker_info[
                        "control_group"
                    ].removeprefix("/")
                    deadline = time.monotonic() + 2
                    while (
                        smoke._read_process_identity(worker_pid) is not None
                        or scope_path.exists()
                    ) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIsNone(smoke._read_process_identity(wrapper_pid))
                    self.assertIsNone(smoke._read_process_identity(worker_pid))
                    self.assertFalse(scope_path.exists())
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(wrapper_pid, os.WNOHANG)
                    if foreign is not None:
                        self.assertIsNone(foreign.poll())
                finally:
                    if foreign is not None:
                        foreign.terminate()
                        foreign.wait(timeout=2)
        self.assertEqual(fd_count(), before_fds)

    def test_parent_scope_fence_kills_candidate_after_supervisor_sigkill(self) -> None:
        candidate = r"""
import os, signal, sys, time
ready = sys.argv[1]
producer = os.fork()
if producer == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
with open(ready, "w", encoding="ascii") as output:
    output.write(f"{os.getpid()} {producer}")
    output.flush()
    os.fsync(output.fileno())
while True:
    time.sleep(1)
"""
        unrelated_child = r"""
import os, signal, sys, time
ready, term = sys.argv[1:]
signal.signal(signal.SIGTERM, lambda *_: (open(term, "w").write("term"), os._exit(73)))
open(ready, "w").write("ready")
while True:
    time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = root / "candidate.ready"
            unrelated_ready = root / "unrelated.ready"
            unrelated_term = root / "unrelated.term"
            supervisor = smoke.start_candidate_supervisor(
                [sys.executable, "-c", candidate, str(ready)],
                root,
                os.environ.copy(),
                root / "candidate.log",
                binary=self.fake_guard,
            )
            unrelated = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    unrelated_child,
                    str(unrelated_ready),
                    str(unrelated_term),
                ],
                start_new_session=True,
            )
            candidate_pids: list[int] = []
            try:
                deadline = time.monotonic() + 2
                while (
                    not ready.exists() or not unrelated_ready.exists()
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                self.assertTrue(unrelated_ready.exists())
                candidate_pids = [int(pid) for pid in ready.read_text().split()]

                os.kill(supervisor.pid, signal.SIGKILL)
                started = time.monotonic()
                cleanup = smoke._finish_candidate_supervisor(supervisor, None)

                self.assertLess(time.monotonic() - started, 2.0)
                self.assertTrue(supervisor.started_receipt["scope_fence_established"])
                self.assertTrue(cleanup["scope_fence_established"])
                self.assertTrue(cleanup["scope_kill_all_sent"])
                self.assertEqual(cleanup["scope_populated_final"], 0)
                self.assertTrue(cleanup["scope_quiesced"])
                self.assertTrue(cleanup["scope_collected"])
                self.assertIsNone(cleanup["scope_collection_error"])
                self.assertTrue(cleanup["candidate_ownership_quiesced"])
                self.assertTrue(cleanup["supervisor_reaped"])
                self.assertTrue(cleanup["forced_kill"])
                for pid in candidate_pids:
                    self.assertFalse(smoke.process_identity_is_live(pid))
                self.assertIsNone(unrelated.poll())
                self.assertFalse(unrelated_term.exists())
            finally:
                if smoke.process_identity_is_live(supervisor.pid):
                    try:
                        smoke.stop_candidate_supervisor(supervisor)
                    except OSError:
                        pass
                for pid in candidate_pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if unrelated.poll() is None:
                    os.killpg(unrelated.pid, signal.SIGKILL)
                    unrelated.wait(timeout=2)

    def test_parent_scope_fence_collects_worker_killed_during_offline_test(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "offline.pids"
            binary = root / "candidate"
            binary.write_text(
                "#!/usr/bin/python3\n"
                "import os, signal, time\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                "    os.setsid()\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    time.sleep(30)\n"
                f"open({str(marker)!r}, 'w').write(f'{{os.getpid()}} {{child}}')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            binary.chmod(0o700)
            runner_code = (
                "import importlib.util, json, pathlib, sys\n"
                f"spec=importlib.util.spec_from_file_location('smoke', {str(ROOT / 'scripts' / 'llm_guard_proxy_loop_recovery_smoke.py')!r})\n"
                "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
                "try:\n"
                " module.authorize_candidate_supervisor(pathlib.Path(sys.argv[1]), sys.argv[1])\n"
                "except module.SupervisorStartError as exc:\n"
                " print(json.dumps(exc.cleanup, separators=(',', ':')))\n"
            )
            runner = subprocess.Popen(
                [sys.executable, "-c", runner_code, str(binary)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            offline_pids: list[int] = []
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                offline_pids = [int(value) for value in marker.read_text().split()]
                worker = smoke._read_process_identity(offline_pids[0])
                self.assertIsNotNone(worker)
                assert worker is not None
                worker_pid = worker.ppid
                scope_group = smoke._process_control_group(worker_pid)
                scope_path = smoke.CGROUP_ROOT / scope_group.removeprefix("/")
                os.kill(worker_pid, signal.SIGKILL)
                stdout, _ = runner.communicate(timeout=10)
                cleanup = json.loads(stdout)
                self.assertTrue(cleanup["scope_quiesced"])
                self.assertTrue(cleanup["scope_collected"])
                self.assertFalse(scope_path.exists())
                for pid in offline_pids:
                    self.assertFalse(smoke.process_identity_is_live(pid))
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait(timeout=2)
                if runner.stdout is not None:
                    runner.stdout.close()
                for pid in offline_pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_supervisor_control_failures_cleanup_candidate_bounded(self) -> None:
        candidate = r"""
import os, signal, sys, time
ready = sys.argv[1]
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""
        for mode, expected in (
            ("eof", "supervisor_parent_eof"),
            ("timeout", "supervisor_control_timeout"),
            ("invalid", "supervisor_invalid_control"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ready = root / "candidate.ready"
                timeout = 0.5 if mode == "timeout" else smoke.SUPERVISOR_CONTROL_TIMEOUT
                with patch.object(smoke, "SUPERVISOR_CONTROL_TIMEOUT", timeout):
                    handle = smoke.start_candidate_supervisor(
                        [sys.executable, "-c", candidate, str(ready)],
                        root,
                        os.environ.copy(),
                        root / "candidate.log",
                        binary=self.fake_guard,
                    )
                    deadline = time.monotonic() + 2
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    started = time.monotonic()
                    if mode == "eof":
                        handle.control.shutdown(socket.SHUT_WR)
                        cleanup = smoke._finish_candidate_supervisor(handle, None)
                    elif mode == "timeout":
                        cleanup = smoke._finish_candidate_supervisor(handle, None)
                    else:
                        cleanup = smoke._finish_candidate_supervisor(handle, b"invalid")
                    elapsed = time.monotonic() - started

                self.assertLess(elapsed, 2.0)
                self.assertEqual(cleanup["supervisor_error"]["code"], expected)
                self.assertTrue(cleanup["proxy_exited"])
                self.assertTrue(cleanup["ownership_quiesced"])
                self.assertTrue(cleanup["candidate_ownership_quiesced"])
                self.assertEqual(cleanup["residual_producer_count_final"], 0)
                self.assertTrue(cleanup["supervisor_exited"])
                self.assertTrue(cleanup["supervisor_reaped"])
                self.assertTrue(cleanup["scope_collected"])
                self.assertIsNone(cleanup["scope_collection_error"])

    def test_stop_reports_unexpected_exit_and_graceful_stop(self) -> None:
        child = r"""
import os, signal, sys, time
mode, ready = sys.argv[1:]
if mode == "graceful":
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(ready, "w", encoding="ascii") as receipt:
    receipt.write("ready")
    receipt.flush()
    os.fsync(receipt.fileno())
if mode == "unexpected":
    os._exit(17)
while True:
    time.sleep(1)
"""
        for mode in ("unexpected", "graceful"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                ready = Path(tmp) / "ready"
                proc = subprocess.Popen(
                    [sys.executable, "-c", child, mode, str(ready)],
                    start_new_session=True,
                )
                identity = smoke.capture_spawn_identity(proc.pid)
                try:
                    deadline = time.monotonic() + 2
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    if mode == "unexpected":
                        while (
                            smoke.process_identity_is_live(proc.pid)
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)

                    cleanup = smoke.stop(proc, identity)
                    self.assertTrue(cleanup["proxy_exited"])
                    self.assertTrue(cleanup["spawn_identity_captured"])
                    self.assertTrue(cleanup["pidfd_available"])
                    self.assertTrue(cleanup["ownership_quiesced"])
                    self.assertEqual(
                        cleanup["residual_producer_count_before_kill"], 0
                    )
                    self.assertEqual(cleanup["residual_producer_count_final"], 0)
                    self.assertTrue(cleanup["group_quiesced"])
                    self.assertTrue(cleanup["session_quiesced"])
                    self.assertFalse(cleanup["forced_kill"])
                    if mode == "unexpected":
                        self.assertTrue(cleanup["unexpected_exit"])
                        self.assertEqual(cleanup["returncode"], 17)
                        self.assertFalse(cleanup["graceful_stop"])
                    else:
                        self.assertFalse(cleanup["unexpected_exit"])
                        self.assertEqual(cleanup["returncode"], 0)
                        self.assertTrue(cleanup["graceful_stop"])
                finally:
                    if proc.returncode is None:
                        smoke._signal_group(identity, signal.SIGKILL)
                        proc.wait(timeout=2)

    def test_stop_uses_linux_pidfd_syscalls_when_python_omits_wrappers(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        native_pidfd_open = smoke.os.pidfd_open
        native_pidfd_send_signal = smoke.signal.pidfd_send_signal
        identity = None
        try:
            with (
                patch.object(smoke.os, "pidfd_open", None),
                patch.object(smoke.signal, "pidfd_send_signal", None),
            ):
                identity = smoke.capture_spawn_identity(proc.pid)
                cleanup = smoke.stop(proc, identity)
            self.assertTrue(cleanup["spawn_identity_captured"])
            self.assertTrue(cleanup["pidfd_available"])
            self.assertTrue(cleanup["ownership_quiesced"])
            self.assertEqual(cleanup["residual_producer_count_before_kill"], 0)
            self.assertEqual(cleanup["residual_producer_count_final"], 0)
            self.assertFalse(cleanup["graceful_stop"])
            self.assertFalse(cleanup["forced_kill"])
            self.assertEqual(cleanup["returncode"], -signal.SIGTERM)
            self.assertTrue(cleanup["group_quiesced"])
            self.assertTrue(cleanup["session_quiesced"])
        finally:
            if proc.returncode is None:
                pidfd = native_pidfd_open(proc.pid)
                try:
                    native_pidfd_send_signal(pidfd, signal.SIGKILL)
                finally:
                    os.close(pidfd)
                proc.wait(timeout=2)

    def test_stop_kills_same_session_residual_before_reaping_leader(self) -> None:
        child_ready = r"""
import os, signal, sys, time
ready = sys.argv[1]
reader, writer = os.pipe()
child = os.fork()
if child == 0:
    os.close(reader)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(writer, b"1")
    os.close(writer)
    while True:
        time.sleep(1)
os.close(writer)
os.read(reader, 1)
os.close(reader)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
receipt_path = ready + ".tmp"
with open(receipt_path, "w", encoding="ascii") as receipt:
    receipt.write(str(child))
    receipt.flush()
    os.fsync(receipt.fileno())
os.replace(receipt_path, ready)
while True:
    time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = root / "ready"
            supervisor = smoke.start_candidate_supervisor(
                [sys.executable, "-c", child_ready, str(ready)],
                root,
                os.environ.copy(),
                root / "candidate.log",
                binary=self.fake_guard,
            )
            try:
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                child_pid = int(ready.read_text())

                cleanup = smoke.stop_candidate_supervisor(supervisor)
                self.assertGreaterEqual(
                    cleanup["residual_producer_count_before_kill"], 1
                )
                self.assertTrue(cleanup["forced_kill"])
                self.assertFalse(cleanup["graceful_stop"])
                self.assertTrue(cleanup["group_quiesced"])
                self.assertTrue(cleanup["session_quiesced"])
                self.assertTrue(cleanup["ownership_quiesced"])
                self.assertEqual(cleanup["residual_producer_count_final"], 0)
                self.assertFalse(smoke.process_identity_is_live(child_pid))

                summary = passing_summary()
                summary["process_cleanup"] = cleanup
                self.assertIn("proxy_forced_kill", smoke.acceptance_errors(summary))
            finally:
                if smoke.process_identity_is_live(supervisor.pid):
                    try:
                        smoke.stop_candidate_supervisor(supervisor)
                    except OSError:
                        self.fail("exclusive supervisor did not terminate")

    def test_subreaper_setup_failure_prevents_candidate_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            marker = base / "spawned"
            binary = base / "candidate"
            binary.write_text(
                "#!/usr/bin/python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                f"if sys.argv[1:] == {list(smoke.OFFLINE_SELF_TEST_ARGV)!r}:\n"
                f"    print({json.dumps(offline_receipt())!r})\n"
                "    raise SystemExit(0)\n"
                f"Path({str(marker)!r}).write_text('spawned')\n"
            )
            binary.chmod(0o700)
            output = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "smoke",
                        "--candidate-config",
                        str(ROOT / "config" / "llm-guard-proxy" / "config.toml"),
                        "--binary",
                        str(binary),
                        "--root",
                        str(base / "run"),
                    ],
                ),
                patch.object(
                    smoke,
                    "_SUPERVISOR_TEST_FAILPOINT",
                    "subreaper:EPERM",
                ),
                redirect_stdout(output),
            ):
                result = smoke.main()

            self.assertEqual(result, 1)
            self.assertFalse(marker.exists())
            summary = json.loads(output.getvalue())
            self.assertFalse(summary["subreaper_enabled"])
            self.assertEqual(summary["execution_error"]["code"], "EPERM")

    def test_exclusive_supervisor_owns_escaped_candidate_not_unrelated_child(self) -> None:
        child = r"""
import os, signal, sys, time
mode, ready, root_ready, trigger, late = sys.argv[1:]

def producer():
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    receipt = ready + ".tmp"
    with open(receipt, "w", encoding="ascii") as output:
        output.write(str(os.getpid()))
        output.flush()
        os.fsync(output.fileno())
    os.replace(receipt, ready)
    while not os.path.exists(trigger):
        time.sleep(0.01)
    with open(late, "wb") as output:
        output.write(b"post-scan-private-marker")
        output.flush()
        os.fsync(output.fileno())
    while True:
        time.sleep(1)

if mode == "setsid":
    spawned = os.fork()
    if spawned == 0:
        producer()
else:
    middle = os.fork()
    if middle == 0:
        spawned = os.fork()
        if spawned == 0:
            producer()
        os._exit(0)
    os.waitpid(middle, 0)

while not os.path.exists(ready):
    time.sleep(0.01)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
with open(root_ready, "w", encoding="ascii") as output:
    output.write("ready")
    output.flush()
    os.fsync(output.fileno())
while True:
    time.sleep(1)
"""
        unrelated_child = r"""
import os, signal, sys, time
ready, term = sys.argv[1:]
def terminated(*_):
    with open(term, "w", encoding="ascii") as output:
        output.write("term")
    os._exit(73)
signal.signal(signal.SIGTERM, terminated)
with open(ready, "w", encoding="ascii") as output:
    output.write("ready")
while True:
    time.sleep(1)
"""
        for mode, trial in (
            (mode, trial)
            for mode in ("setsid", "double-fork")
            for trial in range(3)
        ):
            with self.subTest(mode=mode, trial=trial), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ready = root / "producer.pid"
                root_ready = root / "root.ready"
                trigger = root / "post-scan.trigger"
                late = root / "late-evidence"
                unrelated_ready = root / "unrelated.ready"
                unrelated_term = root / "unrelated.term"
                baseline = smoke._capture_direct_children()
                supervisor = smoke.start_candidate_supervisor(
                    [
                        sys.executable,
                        "-c",
                        child,
                        mode,
                        str(ready),
                        str(root_ready),
                        str(trigger),
                        str(late),
                    ],
                    root,
                    os.environ.copy(),
                    root / "candidate.log",
                    binary=self.fake_guard,
                )
                unrelated = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        unrelated_child,
                        str(unrelated_ready),
                        str(unrelated_term),
                    ],
                    start_new_session=True,
                )
                unrelated_identity = smoke.capture_spawn_identity(unrelated.pid)
                producer_pid = None
                try:
                    deadline = time.monotonic() + 2
                    while (
                        not root_ready.exists() or not unrelated_ready.exists()
                    ) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(root_ready.exists())
                    self.assertTrue(unrelated_ready.exists())
                    self.assertNotIn(unrelated.pid, baseline)
                    producer_pid = int(ready.read_text())

                    cleanup = smoke.stop_candidate_supervisor(supervisor)
                    unrelated_current = smoke._read_process_identity(unrelated.pid)
                    scan_before = smoke.sensitive_marker_count(
                        root, (b"post-scan-private-marker",)
                    )
                    trigger.write_bytes(b"continue")
                    late_deadline = time.monotonic() + 0.3
                    while not late.exists() and time.monotonic() < late_deadline:
                        time.sleep(0.01)
                    late_write = late.exists()
                    producer_live = smoke.process_identity_is_live(producer_pid)

                    self.assertEqual(scan_before, 0)
                    self.assertFalse(late_write)
                    self.assertFalse(producer_live)
                    self.assertTrue(cleanup["exclusive_supervisor"])
                    self.assertTrue(cleanup["supervisor_exited"])
                    self.assertTrue(cleanup["supervisor_reaped"])
                    self.assertTrue(cleanup["candidate_ownership_quiesced"])
                    self.assertTrue(cleanup["group_quiesced"])
                    self.assertTrue(cleanup["session_quiesced"])
                    self.assertTrue(
                        smoke._same_process(unrelated_current, unrelated_identity)
                    )
                    self.assertIsNone(unrelated.poll())
                    self.assertFalse(unrelated_term.exists())
                    self.assertTrue(cleanup["forced_kill"])
                    self.assertGreaterEqual(
                        cleanup["residual_producer_count_before_kill"], 1
                    )
                    self.assertTrue(cleanup["ownership_quiesced"])
                    self.assertEqual(cleanup["residual_producer_count_final"], 0)
                    summary = passing_summary()
                    summary["process_cleanup"] = cleanup
                    self.assertIn(
                        "proxy_forced_kill", smoke.acceptance_errors(summary)
                    )
                finally:
                    if smoke.process_identity_is_live(supervisor.pid):
                        try:
                            smoke.stop_candidate_supervisor(supervisor)
                        except OSError:
                            self.fail("exclusive supervisor did not terminate")
                    if unrelated.returncode is None:
                        try:
                            os.killpg(unrelated.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            unrelated.wait(timeout=2)
                        except ChildProcessError:
                            pass
                    smoke._close_identity_pidfd(unrelated_identity)
                    if producer_pid is not None:
                        try:
                            os.kill(producer_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            os.waitpid(producer_pid, 0)
                        except ChildProcessError:
                            pass

    def test_pidfd_capture_failures_still_reap_owned_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            binary = base / "candidate"
            binary.write_text(
                "#!/usr/bin/python3\n"
                "import json, sys, time\n"
                f"if sys.argv[1:] == {list(smoke.OFFLINE_SELF_TEST_ARGV)!r}:\n"
                f"    print({json.dumps(offline_receipt())!r})\n"
                "    raise SystemExit(0)\n"
                "time.sleep(60)\n"
            )
            binary.chmod(0o700)
            for code in (errno.EMFILE, errno.EPERM):
                with self.subTest(code=errno.errorcode[code]):
                    output = io.StringIO()
                    root = base / f"run-{errno.errorcode[code]}"
                    argv = [
                        "smoke",
                        "--candidate-config",
                        str(ROOT / "config" / "llm-guard-proxy" / "config.toml"),
                        "--binary",
                        str(binary),
                        "--root",
                        str(root),
                    ]
                    before_fds = fd_count()
                    with (
                        patch.object(sys, "argv", argv),
                        patch.object(
                            smoke,
                            "_SUPERVISOR_TEST_FAILPOINT",
                            f"pidfd:{errno.errorcode[code]}",
                        ),
                        redirect_stdout(output),
                    ):
                        result = smoke.main()

                    summary = json.loads(output.getvalue())
                    cleanup = summary["process_cleanup"]
                    self.assertEqual(result, 1)
                    spawned_pid = cleanup["spawn_identity"]["pid"]
                    self.assertFalse(smoke.process_identity_is_live(spawned_pid))
                    self.assertEqual(summary["result"], "FAIL")
                    self.assertEqual(
                        summary["execution_error"]["code"], errno.errorcode[code]
                    )
                    self.assertTrue(cleanup["proxy_exited"])
                    self.assertTrue(cleanup["spawn_identity_captured"])
                    self.assertFalse(cleanup["pidfd_available"])
                    self.assertTrue(cleanup["ownership_quiesced"])
                    self.assertEqual(
                        cleanup["residual_producer_count_before_kill"], 0
                    )
                    self.assertEqual(cleanup["residual_producer_count_final"], 0)
                    self.assertEqual(
                        cleanup["stop_error"]["code"], errno.errorcode[code]
                    )
                    self.assertTrue(all(summary["port_cleanup"].values()))
                    self.assertEqual(fd_count(), before_fds)

    def test_marker_scan_fails_closed_on_walk_read_and_symlink_errors(self) -> None:
        marker = (b"private-marker",)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence.sqlite3"
            evidence.write_bytes(b"safe metadata")
            with patch.object(
                smoke.os,
                "read",
                side_effect=PermissionError(errno.EACCES, "denied"),
            ):
                with self.assertRaises(PermissionError):
                    smoke.sensitive_marker_count(root, marker)

            evidence.write_bytes(
                b"x" * (smoke.SCAN_CHUNK_SIZE - 3) + marker[0] + b"safe"
            )
            stats: dict[str, int | float | bool] = {}
            self.assertEqual(smoke.sensitive_marker_count(root, marker, stats), 1)
            self.assertTrue(stats["completed"])
            self.assertLessEqual(stats["entry_count"], smoke.SCAN_MAX_ENTRIES)
            self.assertLessEqual(stats["logical_bytes"], smoke.SCAN_MAX_TOTAL_BYTES)
            self.assertLessEqual(stats["largest_file_bytes"], smoke.SCAN_MAX_FILE_BYTES)
            self.assertLess(stats["elapsed_milliseconds"], smoke.SCAN_TIMEOUT * 1_000)

            not_directory = root / "not-a-directory"
            not_directory.write_bytes(b"safe metadata")
            with self.assertRaises(OSError):
                smoke.sensitive_marker_count(not_directory, marker)

            symlink = root / "evidence-link"
            symlink.symlink_to(evidence)
            with self.assertRaises(OSError):
                smoke.sensitive_marker_count(root, marker)

        summary = passing_summary()
        summary["scan_errors"] = 1
        self.assertIn("privacy_scan_failed", smoke.acceptance_errors(summary))

    def test_marker_scan_rejects_sparse_oversize_before_reading(self) -> None:
        marker = (b"private-marker",)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sparse = root / "sparse-evidence"
            with sparse.open("wb") as output:
                output.truncate(1 << 40)
            before_fds = fd_count()
            started = time.monotonic()
            with (
                patch.object(
                    smoke.os,
                    "read",
                    side_effect=AssertionError("oversized file was read"),
                ),
                self.assertRaises(OSError) as raised,
            ):
                smoke.sensitive_marker_count(root, marker)
            elapsed = time.monotonic() - started

            self.assertEqual(raised.exception.errno, errno.EFBIG)
            self.assertLess(elapsed, 0.5)
            self.assertEqual(fd_count(), before_fds)
            self.assertNotIn(sparse.name, str(raised.exception))
            self.assertNotIn(marker[0].decode(), str(raised.exception))

    def test_marker_scan_rejects_total_logical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").write_bytes(b"a" * 40)
            (root / "second").write_bytes(b"b" * 40)
            before_fds = fd_count()
            with (
                patch.object(smoke, "SCAN_MAX_FILE_BYTES", 64),
                patch.object(smoke, "SCAN_MAX_TOTAL_BYTES", 64),
                self.assertRaises(OSError) as raised,
            ):
                smoke.sensitive_marker_count(root, (b"private-marker",))

            self.assertEqual(raised.exception.errno, errno.EFBIG)
            self.assertEqual(fd_count(), before_fds)
            self.assertNotIn("first", str(raised.exception))
            self.assertNotIn("second", str(raised.exception))

    def test_marker_scan_rejects_excess_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(smoke.SCAN_MAX_ENTRIES + 1):
                (root / f"entry-{number}").touch()
            before_fds = fd_count()
            with self.assertRaises(OSError) as raised:
                smoke.sensitive_marker_count(root, (b"private-marker",))

            self.assertEqual(raised.exception.errno, errno.E2BIG)
            self.assertEqual(fd_count(), before_fds)
            self.assertNotIn("entry-", str(raised.exception))

    def test_marker_scan_enforces_absolute_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "evidence").write_bytes(b"safe")
            before_fds = fd_count()
            ticks = iter((10.0, 10.0 + smoke.SCAN_TIMEOUT + 1.0))
            with (
                patch.object(smoke.time, "monotonic", side_effect=lambda: next(ticks)),
                self.assertRaises(OSError) as raised,
            ):
                smoke.sensitive_marker_count(root, (b"private-marker",))

            self.assertEqual(raised.exception.errno, errno.ETIMEDOUT)
            self.assertEqual(fd_count(), before_fds)

    def test_marker_scan_fails_closed_on_replacement_races(self) -> None:
        marker = (b"private-marker",)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            root.mkdir()
            (root / "safe").write_bytes(b"safe")
            moved_root = base / "root.saved"
            outside = base / "outside"
            outside.mkdir()
            (outside / "evidence").write_bytes(marker[0])
            real_scandir = smoke.os.scandir
            changed = False

            def replace_root(path):
                nonlocal changed
                if not changed:
                    changed = True
                    root.rename(moved_root)
                    root.symlink_to(outside, target_is_directory=True)
                return real_scandir(path)

            failed_closed = False
            try:
                with patch.object(smoke.os, "scandir", side_effect=replace_root):
                    smoke.sensitive_marker_count(root, marker)
            except OSError:
                failed_closed = True
            finally:
                if changed:
                    root.unlink(missing_ok=True)
                    moved_root.rename(root)
            self.assertTrue(failed_closed)

        class RacingEntry:
            def __init__(self, entry, mutate) -> None:
                self._entry = entry
                self._mutate = mutate

            def stat(self, *args, **kwargs):
                result = self._entry.stat(*args, **kwargs)
                self._mutate(self._entry.name)
                return result

            def __getattr__(self, name):
                return getattr(self._entry, name)

        class RacingEntries:
            def __init__(self, entries, mutate, restore) -> None:
                self._entries = entries
                self._mutate = mutate
                self._restore = restore

            def __enter__(self):
                self._entries.__enter__()
                return self

            def __exit__(self, *args):
                self._restore()
                return self._entries.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                try:
                    entry = next(self._entries)
                except StopIteration:
                    self._restore()
                    raise
                return RacingEntry(entry, self._mutate)

        def run_race(*, replacement: str) -> tuple[bool, int | None, bool]:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root = base / "root"
                root.mkdir()
                target = root / "evidence"
                backup = base / "evidence.saved"
                outside = base / "outside"
                if replacement == "symlink":
                    target.write_bytes(b"safe")
                    outside.write_bytes(marker[0])
                else:
                    target.write_bytes(marker[0])
                    outside.write_bytes(b"safe")
                changed = False

                def mutate(name: str) -> None:
                    nonlocal changed
                    if changed or name != target.name:
                        return
                    changed = True
                    target.rename(backup)
                    if replacement == "symlink":
                        target.symlink_to(outside)
                    else:
                        target.write_bytes(b"safe")

                def restore() -> None:
                    if not changed or not backup.exists():
                        return
                    target.unlink(missing_ok=True)
                    backup.rename(target)

                real_scandir = smoke.os.scandir

                def racing_scandir(path):
                    return RacingEntries(real_scandir(path), mutate, restore)

                failed_closed = False
                count = None
                try:
                    with patch.object(smoke.os, "scandir", side_effect=racing_scandir):
                        count = smoke.sensitive_marker_count(root, marker)
                except OSError:
                    failed_closed = True
                finally:
                    restore()
                return failed_closed, count, marker[0] in target.read_bytes()

        followed_symlink, _, _ = run_race(replacement="symlink")
        self.assertTrue(followed_symlink)

        failed_closed, count, marker_restored = run_race(replacement="regular")
        self.assertTrue(marker_restored)
        self.assertTrue(failed_closed or count == 1)

    def test_fixture_close_is_bounded_for_partial_request_body(self) -> None:
        fixture = smoke.Fixture(
            "reason", "private", "fresh", "positive", "fresh-output"
        )
        server = smoke.FixtureHTTPServer(
            ("127.0.0.1", 0), fixture.handler(), fixture
        )
        server.server_activate()
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        server_thread.start()
        for length in ("1e3", str(smoke.FIXTURE_BODY_LIMIT + 1)):
            with socket.create_connection(server.server_address, timeout=1) as rejected:
                rejected.sendall(
                    (
                        "POST /v1/chat/completions HTTP/1.1\r\n"
                        "Host: fixture\r\n"
                        f"Content-Length: {length}\r\n\r\n"
                    ).encode("ascii")
                )
        error_deadline = time.monotonic() + 1
        while time.monotonic() < error_deadline:
            _, errors = fixture.snapshot()
            if errors.count("fixture_invalid_content_length") == 2:
                break
            time.sleep(0.01)

        client_sock = socket.create_connection(server.server_address, timeout=1)
        client_sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: fixture\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 100\r\n\r\n{"
        )
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with fixture.lock:
                if fixture.active_handlers == 1:
                    break
            time.sleep(0.01)
        with fixture.lock:
            self.assertEqual(fixture.active_handlers, 1)

        receipt: list[dict] = []
        done = threading.Event()

        def close_fixture() -> None:
            receipt.append(smoke.stop_fixture_server(server, server_thread, fixture))
            done.set()

        stopper = threading.Thread(target=close_fixture, daemon=True)
        started = time.monotonic()
        stopper.start()
        bounded = done.wait(timeout=1.5)
        elapsed = time.monotonic() - started
        client_sock.close()
        done.wait(timeout=2)
        stopper.join(timeout=2)

        self.assertTrue(bounded)
        self.assertLess(elapsed, 1.5)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(server_thread.is_alive())
        self.assertTrue(receipt[0]["shutdown_stopped"])
        attempts, errors = fixture.snapshot()
        self.assertEqual(len(attempts), 3)
        self.assertTrue(
            all(
                attempt["phase"] == "unknown"
                and attempt["role"] == "unknown"
                and isinstance(attempt["admitted_monotonic_ns"], int)
                for attempt in attempts
            )
        )
        self.assertEqual(errors.count("fixture_invalid_content_length"), 2)
        self.assertIn("fixture_body_timeout", errors)
        self.assertTrue(receipt[0]["handlers_quiesced"])

        summary = passing_summary()
        summary["fixture_cleanup"] = receipt[0]
        summary["fixture_errors"] = errors
        self.assertTrue(smoke.acceptance_errors(summary))


if __name__ == "__main__":
    unittest.main()
