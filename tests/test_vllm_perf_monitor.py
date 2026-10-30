"""Parser checks for the unprivileged vLLM perf sampler."""

from __future__ import annotations

import http.server
import importlib.util
import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vllm_perf_monitor.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "vllm_perf"


def load_monitor():
    spec = importlib.util.spec_from_file_location("vllm_perf_monitor", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_monitor()

    def test_defaults_are_raw_backends(self) -> None:
        got = dict(self.mod.DEFAULT_ENDPOINTS)
        self.assertEqual(got["emb"], "http://100.105.4.92:18012/metrics")
        self.assertEqual(got["aeon"], "http://100.105.4.92:18010/metrics")
        self.assertEqual(got["rerank"], "http://100.105.4.92:18013/metrics")

    def test_embedding_and_reranker_samples(self) -> None:
        emb = self.mod.metric_snapshot(self.mod.parse_metrics((FIXTURES / "emb_metrics.txt").read_text()))
        rerank = self.mod.metric_snapshot(self.mod.parse_metrics((FIXTURES / "rerank_metrics.txt").read_text()))
        self.assertEqual(emb["running"], 0.0)
        self.assertEqual(emb["waiting"], 0.0)
        self.assertEqual(emb["generation_tokens"], 0.0)
        self.assertEqual(emb["prompt_tokens"], 327279.0)
        self.assertEqual(emb["kv_cache_pct"], 0.0)
        self.assertEqual(rerank["running"], 0.0)
        self.assertEqual(rerank["generation_tokens"], 0.0)
        self.assertEqual(rerank["prompt_tokens"], 41.0)
        self.assertEqual(rerank["requests"], 2.0)

    def test_blank_guard_body_leaves_workload_empty(self) -> None:
        snap = self.mod.metric_snapshot(self.mod.parse_metrics((FIXTURES / "blank_metrics.txt").read_text()))
        self.assertIsNone(snap["running"])
        self.assertIsNone(snap["waiting"])
        self.assertIsNone(snap["generation_tokens"])
        self.assertIsNone(snap["kv_cache_pct"])

    def test_generation_delta(self) -> None:
        diff, reset = self.mod.delta(1655.0, 1600.0)
        self.assertEqual(diff, 55.0)
        self.assertFalse(reset)
        rate, _reset = self.mod.rate(1655.0, 1600.0, 30.0)
        self.assertAlmostEqual(rate, 55.0 / 30.0)

    def test_closed_endpoint_records_class_not_body(self) -> None:
        class Closed(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Closed)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/metrics"
            state = self.mod.ServiceState()
            row = self.mod.service_row("aeon", url, state, 1.0, 0.0)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(row[0], "0")
        self.assertEqual(row[-1], "closed")
        self.assertNotIn("http", row[-1])
        self.assertTrue(all(cell == "" for cell in row[2:-2]))

    def test_refused_endpoint_is_explicit(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        state = self.mod.ServiceState()
        row = self.mod.service_row("aeon", f"http://127.0.0.1:{port}/metrics", state, 1.0, 0.0)
        self.assertEqual(row[0], "0")
        self.assertEqual(row[-1], "refused")


if __name__ == "__main__":
    unittest.main()
