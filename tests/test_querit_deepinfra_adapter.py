from __future__ import annotations

import importlib
import http.client
import json
import math
import re
import socket
import sys
import threading
import time
import tracemalloc
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

adapter = importlib.import_module("querit_deepinfra_adapter")


def _public_request() -> bytes:
    return json.dumps(
        {
            "documents": ["d1", "d2", "d3"],
            "instruction": "rank faithfully",
            "queries": ["q1", "q2", "q3"],
            "service_tier": "priority",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _backend_response(scores: list[float]) -> bytes:
    return json.dumps(
        {
            "id": "score-request-test",
            "object": "list",
            "created": 1,
            "model": "Querit/Querit-4B",
            "data": [
                {"index": index, "object": "score", "score": score}
                for index, score in enumerate(scores)
            ],
            "usage": {
                "prompt_tokens": 123,
                "total_tokens": 123,
                "completion_tokens": 0,
                "prompt_tokens_details": None,
            },
        },
        allow_nan=True,
        separators=(",", ":"),
    ).encode()


class QueritDeepinfraAdapterTests(unittest.TestCase):
    def test_unpaired_surrogate_is_a_bounded_adapter_error(self) -> None:
        body = b'{"documents":["\\ud800"],"queries":["q"]}'
        with self.assertRaisesRegex(adapter.AdapterError, "unpaired surrogate"):
            adapter.parse_public_request(body)

    def test_default_peak_request_memory_fits_adapter_unit_envelope(self) -> None:
        self.assertLessEqual(adapter.MAX_REQUEST_BYTES, 8 * 1024 * 1024)
        self.assertEqual(adapter.DEFAULT_MAX_CONCURRENCY, 1)

        document = "d" * (adapter.MAX_REQUEST_BYTES - 1024)
        body = json.dumps(
            {"documents": [document], "queries": ["q"]},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self.assertGreaterEqual(len(body), adapter.MAX_REQUEST_BYTES - 2048)
        self.assertLessEqual(len(body), adapter.MAX_REQUEST_BYTES)

        tracemalloc.start()
        try:
            request = adapter.parse_public_request(body)
            backend_body = adapter.backend_request_bytes(request)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(backend_body)

        unit = (ROOT / "systemd" / "vllm-querit-4b-canary.service").read_text()
        memory_match = re.search(r"^MemoryMax=(\d+)M$", unit, re.MULTILINE)
        self.assertIsNotNone(memory_match)
        assert memory_match is not None
        memory_max = int(memory_match.group(1)) * 1024 * 1024
        interpreter_and_server_reserve = 128 * 1024 * 1024
        measured_envelope = len(body) + peak + interpreter_and_server_reserve
        self.assertLess(measured_envelope, memory_max)

    def test_handler_threads_and_large_body_allocations_are_bounded(self) -> None:
        handler = adapter._handler("http://unused.invalid/v1/score", 0.5)
        server = adapter.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), handler, max_concurrency=2
        )
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        release = threading.Event()
        two_entered = threading.Event()
        lock = threading.Lock()
        active = 0
        max_active = 0
        entered = 0
        statuses: list[int] = []

        def blocked_backend(*_args: object, **_kwargs: object) -> bytes:
            nonlocal active, max_active, entered
            with lock:
                active += 1
                entered += 1
                max_active = max(max_active, active)
                if entered == 2:
                    two_entered.set()
            self.assertTrue(release.wait(timeout=2))
            with lock:
                active -= 1
            return _backend_response([-1.0, 0.0, 1.0])

        def request() -> None:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=2
            )
            body = _public_request()
            try:
                connection.request(
                    "POST",
                    "/v1/inference/Qwen/Qwen3-Reranker-8B"
                    "?version=5fa94080caafeaa45a15d11f969d7978e087a3db",
                    body=body,
                    headers={
                        "Content-Length": str(len(body)),
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                response.read()
                statuses.append(response.status)
            finally:
                connection.close()

        clients = [threading.Thread(target=request) for _ in range(3)]
        try:
            with patch.object(adapter, "_call_backend", side_effect=blocked_backend):
                for client in clients:
                    client.start()
                self.assertTrue(two_entered.wait(timeout=1))
                time.sleep(0.1)
                with lock:
                    self.assertEqual(entered, 2)
                    self.assertEqual(max_active, 2)
                release.set()
                for client in clients:
                    client.join(timeout=2)
                    self.assertFalse(client.is_alive())
            self.assertEqual(statuses, [200, 200, 200])
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)

    def test_loopback_exact_content_length_request_completes(self) -> None:
        handler = adapter._handler("http://unused.invalid/v1/score", 0.5)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=0.5
        )
        body = _public_request()
        try:
            with patch.object(
                adapter,
                "_call_backend",
                return_value=_backend_response([-1.0, 0.0, 1.0]),
            ):
                connection.request(
                    "POST",
                    "/v1/inference/Qwen/Qwen3-Reranker-8B"
                    "?version=5fa94080caafeaa45a15d11f969d7978e087a3db",
                    body=body,
                    headers={
                        "Content-Length": str(len(body)),
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["scores"], [0.0, 0.5, 1.0])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_partial_request_body_times_out_fail_closed(self) -> None:
        handler = adapter._handler("http://unused.invalid/v1/score", 0.5)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = socket.create_connection(
            ("127.0.0.1", server.server_port), timeout=1
        )
        client.settimeout(0.5)
        body = _public_request()
        request = (
            "POST /v1/inference/Qwen/Qwen3-Reranker-8B"
            "?version=5fa94080caafeaa45a15d11f969d7978e087a3db HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        ).encode() + body[:8]
        try:
            with patch.object(
                adapter, "REQUEST_BODY_TIMEOUT_SECONDS", 0.05, create=True
            ):
                client.sendall(request)
                response = client.recv(4096)
            self.assertTrue(response.startswith(b"HTTP/1.0 408 "), response)
            self.assertNotIn(body[:8], response)
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_partial_request_line_or_header_cannot_monopolize_the_only_slot(
        self,
    ) -> None:
        partials = (
            b"POST /v1/inference",
            b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Stall:",
        )
        for partial in partials:
            with self.subTest(partial=partial):
                handler = adapter._handler("http://unused.invalid/v1/score", 0.5)
                with patch.object(adapter, "REQUEST_BODY_TIMEOUT_SECONDS", 0.05):
                    server = adapter.BoundedThreadingHTTPServer(
                        ("127.0.0.1", 0), handler, max_concurrency=1
                    )
                    server_thread = threading.Thread(
                        target=server.serve_forever, daemon=True
                    )
                    server_thread.start()
                    stalled = socket.create_connection(
                        ("127.0.0.1", server.server_port), timeout=1
                    )
                    completed = threading.Event()
                    status: list[int] = []

                    def valid_request() -> None:
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", server.server_port, timeout=1
                        )
                        try:
                            connection.request("GET", "/")
                            response = connection.getresponse()
                            response.read()
                            status.append(response.status)
                        finally:
                            connection.close()
                            completed.set()

                    client_thread = threading.Thread(target=valid_request)
                    try:
                        stalled.sendall(partial)
                        time.sleep(0.02)
                        client_thread.start()
                        self.assertTrue(completed.wait(timeout=0.5))
                        self.assertEqual(status, [501])
                    finally:
                        stalled.close()
                        client_thread.join(timeout=1)
                        server.shutdown()
                        server.server_close()
                        server_thread.join(timeout=1)

    def test_exact_public_request_maps_to_vllm_score_and_back(self) -> None:
        request = adapter.parse_public_request(_public_request())
        self.assertEqual(
            adapter.backend_request_bytes(request),
            json.dumps(
                {
                    "documents": ["d1", "d2", "d3"],
                    "instruction": "rank faithfully",
                    "model": "Querit/Querit-4B",
                    "queries": ["q1", "q2", "q3"],
                    "use_activation": True,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        )
        response = json.loads(
            adapter.public_response_bytes(
                adapter.parse_backend_response(_backend_response([-1.0, 0.0, 1.0]), 3)
            )
        )
        self.assertEqual(response["scores"], [0.0, 0.5, 1.0])
        self.assertEqual(response["input_tokens"], 123)
        self.assertEqual(response["request_id"], "score-request-test")

    def test_default_and_custom_instruction_reach_the_tracked_chat_template(self) -> None:
        request = adapter.parse_public_request(
            json.dumps(
                {"documents": ["document"], "queries": ["query"]},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        default_backend = json.loads(adapter.backend_request_bytes(request))
        self.assertEqual(default_backend["instruction"], adapter.DEFAULT_INSTRUCTION)

        custom = adapter.parse_public_request(_public_request())
        custom_backend = json.loads(adapter.backend_request_bytes(custom))
        self.assertEqual(custom_backend["instruction"], "rank faithfully")

        template = (ROOT / "config" / "querit" / "querit-rerank.jinja").read_text()
        self.assertIn('~ "<Instruct>: " ~ instruction ~ "\\n"', template)
        self.assertNotIn(
            "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\\n",
            template,
        )

    def test_real_vllm_025_score_response_usage_shape_is_accepted(self) -> None:
        response = adapter.parse_backend_response(_backend_response([0.25]), 1)
        self.assertEqual(response.input_tokens, 123)
        self.assertEqual(response.scores, (0.625,))

    def test_internal_tanh_domain_never_clamps_outside_evidence(self) -> None:
        for score in (
            math.nextafter(-1.0, -math.inf),
            -1.0 - 2.0**-23,
            1.0 + 2.0**-23,
            math.nextafter(1.0, math.inf),
        ):
            with self.subTest(score=score), self.assertRaises(adapter.AdapterError):
                adapter.parse_backend_response(_backend_response([score]), 1)

        self.assertEqual(
            adapter.parse_backend_response(_backend_response([-1.0, 1.0]), 2).scores,
            (0.0, 1.0),
        )

    def test_public_path_and_version_query_are_exact(self) -> None:
        self.assertTrue(
            adapter.valid_public_target(
                "/v1/inference/Qwen/Qwen3-Reranker-8B",
                "version=5fa94080caafeaa45a15d11f969d7978e087a3db",
            )
        )
        for path, query in (
            ("/v1/score", ""),
            ("/v1/inference/Qwen/Qwen3-Reranker-8B", ""),
            ("/v1/inference/Qwen/Qwen3-Reranker-8B", "version=stale"),
            (
                "/v1/inference/Qwen/Qwen3-Reranker-8B",
                "version=5fa94080caafeaa45a15d11f969d7978e087a3db&extra=1",
            ),
        ):
            with self.subTest(path=path, query=query):
                self.assertFalse(adapter.valid_public_target(path, query))

    def test_request_and_backend_response_fail_closed_on_schema_or_domain_drift(
        self,
    ) -> None:
        public = json.loads(_public_request())
        invalid_requests = []
        for mutation in (
            lambda row: row["documents"].pop(),
            lambda row: row.update({"extra": True}),
            lambda row: row.update({"queries": ["", "q2", "q3"]}),
        ):
            changed = json.loads(json.dumps(public))
            mutation(changed)
            invalid_requests.append(json.dumps(changed).encode())
        invalid_requests.append(json.dumps(public, indent=2, sort_keys=True).encode())
        for body in invalid_requests:
            with self.subTest(body=body), self.assertRaises(adapter.AdapterError):
                adapter.parse_public_request(body)

        invalid_backend = (
            _backend_response([-1.000001, 0.0, 1.0]),
            _backend_response([-1.0, 0.0, 1.000001]),
            _backend_response([math.nan, 0.0, 1.0]),
            _backend_response([0.0, 1.0]),
            _backend_response([0.0, 0.5, 1.0]).replace(b'"index":1', b'"index":0'),
            _backend_response([0.0, 0.5, 1.0]).replace(
                b'"prompt_tokens":123', b'"prompt_tokens":true'
            ),
            _backend_response([0.0, 0.5, 1.0]).replace(
                b',"completion_tokens":0,"prompt_tokens_details":null', b""
            ),
            _backend_response([0.0, 0.5, 1.0]).replace(
                b'"completion_tokens":0', b'"completion_tokens":1'
            ),
            _backend_response([0.0, 0.5, 1.0]).replace(
                b'"prompt_tokens_details":null', b'"prompt_tokens_details":{}'
            ),
            _backend_response([0.0, 0.5, 1.0]).replace(
                b'"total_tokens":123', b'"total_tokens":124'
            ),
        )
        for body in invalid_backend:
            with self.subTest(body=body), self.assertRaises(adapter.AdapterError):
                adapter.parse_backend_response(body, 3)


if __name__ == "__main__":
    unittest.main()
