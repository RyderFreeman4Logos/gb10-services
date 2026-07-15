from __future__ import annotations

import importlib.util
import math
import sys
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

import querit_score_contract as contract  # noqa: E402


class FakeTensor:
    def __init__(self, data: Any) -> None:
        self.data = data

    @property
    def shape(self) -> tuple[int, ...]:
        shape: list[int] = []
        value = self.data
        while isinstance(value, list):
            shape.append(len(value))
            value = value[0] if value else None
        return tuple(shape)

    @property
    def ndim(self) -> int:
        return len(self.shape)

    def to(self, _device: Any) -> FakeTensor:
        return self

    def detach(self) -> FakeTensor:
        return self

    def float(self) -> FakeTensor:
        return self

    def reshape(self, *_shape: int) -> FakeTensor:
        def flatten(value: Any) -> list[Any]:
            if not isinstance(value, list):
                return [value]
            flattened: list[Any] = []
            for item in value:
                flattened.extend(flatten(item))
            return flattened

        return FakeTensor(flatten(self.data))

    def tolist(self) -> Any:
        return self.data

    def __getitem__(self, key: int | tuple[int, int]) -> FakeTensor:
        if isinstance(key, tuple):
            row, column = key
            return FakeTensor(self.data[row][column])
        return FakeTensor(self.data[key])

    def __sub__(self, other: FakeTensor) -> FakeTensor:
        if not isinstance(self.data, list) and not isinstance(other.data, list):
            return FakeTensor(self.data - other.data)
        return FakeTensor(
            [left - right for left, right in zip(self.data, other.data, strict=True)]
        )


class FakeTokenizer:
    is_fast = True
    padding_side = "right"
    truncation_side = "right"
    pad_token_id = contract.POSTPROCESSOR_TOKEN_ID
    cls_token_id = None

    def __call__(
        self,
        texts: str | list[str],
        *,
        add_special_tokens: bool = True,
        padding: bool = False,
        truncation: bool = False,
        max_length: int | None = None,
        return_tensors: str | None = None,
    ) -> dict[str, Any]:
        if return_tensors is not None:
            raise AssertionError("server must use the pure packer before tensorization")
        singleton = isinstance(texts, str)
        values = [texts] if singleton else texts
        rows: list[list[int]] = []
        for value in values:
            row = [1 + (ord(character) % 17) for character in value]
            if add_special_tokens:
                row.append(contract.POSTPROCESSOR_TOKEN_ID)
            if truncation and max_length is not None and len(row) > max_length:
                row = row[: max_length - 1] + [contract.POSTPROCESSOR_TOKEN_ID]
            rows.append(row)
        width = max(len(row) for row in rows)
        masks = [[1] * len(row) + [0] * (width - len(row)) for row in rows]
        if padding:
            rows = [
                row + [contract.POSTPROCESSOR_TOKEN_ID] * (width - len(row))
                for row in rows
            ]
        else:
            masks = [[1] * len(row) for row in rows]
        if singleton:
            return {"input_ids": rows[0], "attention_mask": masks[0]}
        return {"input_ids": rows, "attention_mask": masks}


class FakeBackbone:
    def __call__(self, *, input_ids: FakeTensor, attention_mask: FakeTensor):
        hidden: list[list[list[float]]] = []
        for ids, mask in zip(input_ids.data, attention_mask.data, strict=True):
            running = 0.0
            row: list[list[float]] = []
            for token, attended in zip(ids, mask, strict=True):
                if attended:
                    running += float(token % 997) / 997.0
                    row.append([-running, running])
                else:
                    row.append([10_000.0, -10_000.0])
            hidden.append(row)
        return SimpleNamespace(last_hidden_state=FakeTensor(hidden))


class FakeHead:
    weight = SimpleNamespace(shape=(2, 2560))
    bias = SimpleNamespace(shape=(2,))

    def __call__(self, selected: FakeTensor) -> FakeTensor:
        return selected


class FakeModel:
    def __init__(self) -> None:
        self.model = FakeBackbone()
        self.head = FakeHead()

    def __call__(self, *, input_ids: FakeTensor, attention_mask: FakeTensor):
        scores: list[float] = []
        for ids, mask in zip(input_ids.data, attention_mask.data, strict=True):
            if mask[-1] == 0:
                scores.append(-1.0)
            else:
                scores.append(float(ids[-1] % 11) / 10.0)
        return {"score": FakeTensor(scores)}


class FakeFastAPI:
    def __init__(self, **_kwargs: Any) -> None:
        self.middleware: list[tuple[Any, dict[str, Any]]] = []

    def add_middleware(self, middleware: Any, **kwargs: Any) -> None:
        self.middleware.append((middleware, kwargs))

    def get(self, *_args: Any, **_kwargs: Any):
        return lambda function: function

    def post(self, *_args: Any, **_kwargs: Any):
        return lambda function: function


class FakeHTTPException(Exception):
    def __init__(self, status_code: int, detail: str, **_kwargs: Any) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def load_server() -> Any:
    torch = types.ModuleType("torch")
    setattr(torch, "inference_mode", lambda: (lambda function: function))
    setattr(torch, "bfloat16", object())
    setattr(torch, "float16", object())
    setattr(torch, "float32", object())
    setattr(torch, "cuda", SimpleNamespace(is_available=lambda: True))
    setattr(torch, "tensor", lambda data, **_kwargs: FakeTensor(data))
    setattr(torch, "stack", lambda rows: FakeTensor([row.data for row in rows]))
    setattr(torch, "softmax_calls", 0)

    def softmax(tensor: FakeTensor, dim: int = -1) -> FakeTensor:
        setattr(torch, "softmax_calls", getattr(torch, "softmax_calls") + 1)
        if dim != -1:
            raise AssertionError("unexpected softmax dimension")
        result = []
        for row in tensor.data:
            maximum = max(row)
            exponentials = [math.exp(value - maximum) for value in row]
            denominator = sum(exponentials)
            result.append([value / denominator for value in exponentials])
        return FakeTensor(result)

    setattr(torch, "softmax", softmax)
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
    spec = importlib.util.spec_from_file_location("querit_server_score_test", SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, fake_modules):
        spec.loader.exec_module(module)
    module.torch = torch
    module.STATE.model_id = "/models/querit"
    module.STATE.aliases = list(module.DEFAULT_ALIASES)
    module.STATE.tokenizer = FakeTokenizer()
    module.STATE.model = FakeModel()
    module.STATE.device = "cuda"
    module.STATE.max_batch = 8
    module.LOG.disabled = True
    return module


class ServerScoreContractTests(unittest.TestCase):
    def test_legacy_is_selected_by_default_and_exposed_in_metadata(self) -> None:
        module = load_server()
        self.assertEqual(module.STATE.score_contract, contract.LEGACY_PHYSICAL_LAST_V1)
        rows = module.list_models()["data"]
        self.assertEqual(len(rows), len(module.DEFAULT_ALIASES))
        self.assertEqual(
            {row["score_contract"] for row in rows},
            {contract.LEGACY_PHYSICAL_LAST_V1},
        )
        self.assertEqual(module.health()["score_contract"], contract.LEGACY_PHYSICAL_LAST_V1)

    def test_candidate_is_batch_and_permutation_invariant_through_real_path(self) -> None:
        module = load_server()
        module.STATE.score_contract = contract.CURRENT_PROMPT_TERMINAL_CLS_V1
        documents = ["a", "bb", "中文", "e\u0301", "é", "[CLS]", "\x00\r\n\t", "👩\u200d💻"]
        singleton = [module.score_pairs("query", [document])[0] for document in documents]
        batch = module.score_pairs("query", documents)
        self.assertEqual(batch, singleton)
        permutation = [7, 2, 5, 0, 6, 3, 1, 4]
        permuted = module.score_pairs("query", [documents[index] for index in permutation])
        self.assertEqual(permuted, [singleton[index] for index in permutation])
        self.assertTrue(all(math.isfinite(score) and -1 <= score <= 1 for score in batch))
        self.assertGreater(module.torch.softmax_calls, 0)

    def test_legacy_path_still_uses_opaque_model_score(self) -> None:
        module = load_server()
        module.STATE.score_contract = contract.LEGACY_PHYSICAL_LAST_V1
        scores = module.score_pairs("q", ["short", "a much longer document"])
        self.assertEqual(scores[0], -1.0)
        self.assertNotEqual(scores[1], -1.0)

    def test_surrogates_fail_with_400_before_score_pairs(self) -> None:
        module = load_server()
        called = False

        def fail_if_called(*_args: Any) -> list[float]:
            nonlocal called
            called = True
            raise AssertionError("tokenization was reached")

        module.score_pairs = fail_if_called
        for query, documents in (("bad\ud800", ["d"]), ("q", ["bad\udfff"])):
            with self.subTest(query=repr(query), documents=repr(documents)):
                with self.assertRaises(module.HTTPException) as raised:
                    module.rerank(
                        SimpleNamespace(
                            model=None,
                            query=query,
                            documents=documents,
                            top_n=1,
                        )
                    )
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(raised.exception.detail, "input contains invalid Unicode")
        self.assertFalse(called)

    def test_envelope_aliases_top_n_ties_and_near_ties_are_preserved(self) -> None:
        module = load_server()
        scores = [0.5, 0.5, 0.5000000000000001]
        module.score_pairs = lambda *_args: scores
        response = module.rerank(
            SimpleNamespace(
                model="Qwen/Qwen3-Reranker-8B",
                query="q",
                documents=["zero", "one", "two"],
                top_n=50,
            )
        )
        self.assertRegex(response["id"], r"^rerank-[0-9a-f]{32}$")
        self.assertEqual(response["model"], "Qwen/Qwen3-Reranker-8B")
        self.assertIs(response["results"], response["data"])
        self.assertEqual([row["index"] for row in response["results"]], [2, 0, 1])
        for row in response["results"]:
            self.assertEqual(row["index"], row["document_index"])
            self.assertEqual(row["score"], row["relevance_score"])

    def test_loaded_head_attestation_is_executed_for_hostile_reports(self) -> None:
        module = load_server()
        model = FakeModel()
        clean = {
            "missing_keys": [],
            "unexpected_keys": [],
            "mismatched_keys": [],
            "reinitialized_keys": [],
            "error_msgs": [],
        }
        module._attest_loaded_head(model, clean, {"head.weight", "head.bias"})
        for field, value in (
            ("missing_keys", ["head.weight"]),
            ("mismatched_keys", [("head.weight", (1,), (2, 2560))]),
            ("unexpected_keys", ["head.extra"]),
            ("reinitialized_keys", ["head.bias"]),
        ):
            with self.subTest(field=field):
                report = dict(clean)
                report[field] = value
                with self.assertRaises(contract.HeadAttestationError):
                    module._attest_loaded_head(
                        model, report, {"head.weight", "head.bias"}
                    )


if __name__ == "__main__":
    unittest.main()
