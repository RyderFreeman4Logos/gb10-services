from __future__ import annotations

import ast
import asyncio
import importlib.util
import math
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SERVER = SCRIPTS / "querit_openai_rerank_server.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class QueritServerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SERVER.read_text()
        ast.parse(self.source)

    def test_bounds_backend_admission_and_parallel_gpu_inference(self) -> None:
        self.assertIn("MAX_CONCURRENT_INFERENCE = 2", self.source)
        self.assertIn(
            "INFERENCE_SEMAPHORE = threading.BoundedSemaphore("
            "MAX_CONCURRENT_INFERENCE)",
            self.source,
        )
        self.assertIn("with INFERENCE_SEMAPHORE:", self.source)
        self.assertNotIn("INFERENCE_SEMAPHORE.acquire(blocking=False)", self.source)
        self.assertNotIn("rerank inference busy", self.source)

    def test_bounds_body_documents_and_input_sizes(self) -> None:
        for contract in (
            "MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024",
            "MAX_CONCURRENT_REQUESTS = 16",
            "limit_concurrency=MAX_CONCURRENT_REQUESTS",
            "MAX_DOCUMENTS = 10000",
            "MAX_QUERY_CHARS = 131072",
            "MAX_DOCUMENT_CHARS = 131072",
            "MAX_TOTAL_DOCUMENT_CHARS = 10 * 1024 * 1024",
            "RequestBodyLimitMiddleware",
            "app.add_middleware",
        ):
            self.assertIn(contract, self.source)
        self.assertIn("documents must contain at most", self.source)
        self.assertIn("top_n must be positive", self.source)

    def test_rejects_non_finite_scores(self) -> None:
        self.assertIn("math.isfinite", self.source)
        self.assertIn("model returned a non-finite score", self.source)

    def test_errors_are_logged_but_not_disclosed(self) -> None:
        self.assertIn('detail="rerank inference failed"', self.source)
        self.assertNotIn("detail=str(exc)", self.source)
        self.assertNotIn("model not found: {model_name}", self.source)

    def test_model_load_is_strictly_local(self) -> None:
        self.assertGreaterEqual(self.source.count("local_files_only=True"), 3)
        self.assertNotIn("snapshot_download", self.source)

    def load_with_fake_dependencies(self) -> Any:
        class FakeFastAPI:
            def __init__(self, **_kwargs):
                self.middleware: list[tuple[Any, dict[str, Any]]] = []

            def add_middleware(self, middleware: Any, **kwargs: Any) -> None:
                self.middleware.append((middleware, kwargs))

            def get(self, *_args, **_kwargs):
                return lambda function: function

            def post(self, *_args, **_kwargs):
                return lambda function: function

        class FakeHTTPException(Exception):
            def __init__(
                self,
                status_code: int,
                detail: str,
                headers: dict[str, str] | None = None,
            ):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail
                self.headers = headers

        torch = types.ModuleType("torch")
        setattr(torch, "inference_mode", lambda: (lambda function: function))
        setattr(torch, "bfloat16", object())
        setattr(torch, "float16", object())
        setattr(torch, "float32", object())
        setattr(torch, "cuda", SimpleNamespace(is_available=lambda: True))
        fastapi = types.ModuleType("fastapi")
        setattr(fastapi, "FastAPI", FakeFastAPI)
        setattr(fastapi, "HTTPException", FakeHTTPException)
        pydantic = types.ModuleType("pydantic")
        setattr(pydantic, "BaseModel", type("BaseModel", (), {}))
        setattr(pydantic, "Field", lambda default=None, **_kwargs: default)
        transformers = types.ModuleType("transformers")
        setattr(transformers, "AutoConfig", object())
        setattr(transformers, "AutoTokenizer", object())
        uvicorn = types.ModuleType("uvicorn")
        setattr(uvicorn, "run", lambda *_args, **_kwargs: None)
        starlette = types.ModuleType("starlette")
        starlette_types = types.ModuleType("starlette.types")
        for name in ("ASGIApp", "Message", "Receive", "Scope", "Send"):
            setattr(starlette_types, name, Any)
        fake_modules = {
            "torch": torch,
            "fastapi": fastapi,
            "pydantic": pydantic,
            "transformers": transformers,
            "uvicorn": uvicorn,
            "starlette": starlette,
            "starlette.types": starlette_types,
        }
        spec = importlib.util.spec_from_file_location("querit_server_under_test", SERVER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, fake_modules):
            spec.loader.exec_module(module)
        module.STATE.model_id = "/models/querit"
        module.STATE.aliases = ["qwen3-reranker-8b"]
        module.LOG.disabled = True
        return module

    def test_body_limit_rejects_before_downstream_parse(self) -> None:
        module = self.load_with_fake_dependencies()
        downstream_called = False
        sent: list[dict[str, Any]] = []
        incoming = [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"67890", "more_body": False},
        ]

        async def downstream(_scope, _receive, _send) -> None:
            nonlocal downstream_called
            downstream_called = True

        async def receive() -> dict[str, Any]:
            return incoming.pop(0)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        middleware = module.RequestBodyLimitMiddleware(
            downstream, max_body_bytes=8
        )
        asyncio.run(
            middleware(
                {"type": "http", "headers": []},
                receive,
                send,
            )
        )
        self.assertFalse(downstream_called)
        self.assertEqual(sent[0]["status"], 413)

    def test_request_bounds_and_client_safe_errors_execute(self) -> None:
        module = self.load_with_fake_dependencies()
        with self.assertRaises(module.HTTPException) as too_many:
            module.rerank(
                SimpleNamespace(
                    model=None,
                    query="q",
                    documents=["d"] * (module.MAX_DOCUMENTS + 1),
                    top_n=1,
                )
            )
        self.assertEqual(too_many.exception.status_code, 400)

        setattr(module, "score_pairs", lambda *_args: [math.nan])
        with self.assertRaises(module.HTTPException) as non_finite:
            module.rerank(
                SimpleNamespace(model=None, query="q", documents=["d"], top_n=1)
            )
        self.assertEqual(non_finite.exception.status_code, 500)
        self.assertEqual(non_finite.exception.detail, "rerank inference failed")

    def test_inference_allows_two_in_flight_and_bounds_third(self) -> None:
        module = self.load_with_fake_dependencies()
        first_entered = threading.Event()
        first_release = threading.Event()
        second_entered = threading.Event()
        second_release = threading.Event()
        third_entered = threading.Event()
        errors: list[BaseException] = []
        results: list[Any] = []

        def fake_score(query, _documents):
            if query == "first":
                first_entered.set()
                if not first_release.wait(timeout=1):
                    raise RuntimeError("test inference release timed out")
            elif query == "second":
                second_entered.set()
                if not second_release.wait(timeout=1):
                    raise RuntimeError("test inference release timed out")
            else:
                third_entered.set()
            return [1.0]

        setattr(module, "score_pairs", fake_score)

        def request(query: str) -> None:
            try:
                results.append(
                    module.rerank(
                        SimpleNamespace(
                            model=None, query=query, documents=["d"], top_n=1
                        )
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        first = threading.Thread(target=request, args=("first",))
        second = threading.Thread(target=request, args=("second",))
        third = threading.Thread(target=request, args=("third",))
        first.start()
        self.assertTrue(first_entered.wait(timeout=1))
        second.start()
        second_ran_in_parallel = second_entered.wait(timeout=0.1)
        third.start()
        third_exceeded_bound = third_entered.wait(timeout=0.05)
        first_release.set()
        second_release.set()
        first.join(timeout=1)
        second.join(timeout=1)
        third.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(third.is_alive())
        self.assertTrue(second_ran_in_parallel)
        self.assertFalse(third_exceeded_bound)
        self.assertTrue(third_entered.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
