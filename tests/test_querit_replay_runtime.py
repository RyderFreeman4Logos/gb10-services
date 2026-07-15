from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import querit_replay_runtime as runtime  # noqa: E402
import querit_score_contract as contract  # noqa: E402


CHECKPOINT_HEAD_KEYS = {"head.weight", "head.bias"}


def clean_loading_info() -> dict[str, list[Any]]:
    return {
        "missing_keys": [],
        "unexpected_keys": [],
        "mismatched_keys": [],
        "reinitialized_keys": [],
        "error_msgs": [],
    }


class FakeCuda:
    def __init__(self, events: list[str], *, available: bool = True) -> None:
        self.events = events
        self.available = available

    def is_available(self) -> bool:
        self.events.append("cuda_available")
        return self.available


class FakeModel:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.head: Any = SimpleNamespace(
            weight=SimpleNamespace(shape=(2, 2560)),
            bias=SimpleNamespace(shape=(2,)),
        )
        self.generation_head = object()
        self._lm_head: object | None = self.generation_head
        self.training = True
        self.to_calls: list[str] = []

    @property
    def lm_head(self) -> object | None:
        return self._lm_head

    @lm_head.setter
    def lm_head(self, value: object | None) -> None:
        self.events.append("drop_lm_head")
        self._lm_head = value

    def eval(self) -> FakeModel:
        self.events.append("eval")
        self.training = False
        return self

    def to(self, device: str) -> FakeModel:
        self.events.append(f"to:{device}")
        self.to_calls.append(device)
        return self


class FakePinnedModelClass:
    def __init__(
        self,
        events: list[str],
        loaded: object,
    ) -> None:
        self.events = events
        self.loaded = loaded
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def from_pretrained(self, *args: object, **kwargs: object) -> object:
        self.events.append("load")
        self.calls.append((args, kwargs))
        return self.loaded


class StrictSingleCudaLoaderTests(unittest.TestCase):
    def _load(
        self,
        model_class: FakePinnedModelClass,
        torch: Any,
    ) -> tuple[Any, dict[str, Any], str]:
        return runtime._load_strict_single_cuda_model(
            model_class=model_class,
            local=Path("/private/model"),
            config=self.config,
            torch=torch,
            torch_dtype=self.dtype,
            checkpoint_keys=CHECKPOINT_HEAD_KEYS,
        )

    def setUp(self) -> None:
        self.config = object()
        self.dtype = object()

    def test_exact_local_contract_attests_then_drops_and_moves_once(self) -> None:
        events: list[str] = []
        model = FakeModel(events)
        learned_head = model.head
        loading_info = clean_loading_info()
        model_class = FakePinnedModelClass(events, (model, loading_info))
        torch = SimpleNamespace(cuda=FakeCuda(events))

        def attest(**kwargs: object) -> None:
            events.append("attest")
            self.assertEqual(kwargs["checkpoint_keys"], CHECKPOINT_HEAD_KEYS)
            self.assertIs(kwargs["loading_info"], loading_info)
            self.assertEqual(kwargs["weight_shape"], (2, 2560))
            self.assertEqual(kwargs["bias_shape"], (2,))
            self.assertIs(model.lm_head, model.generation_head)

        with patch.object(runtime, "attest_head_load", side_effect=attest):
            loaded_model, loaded_info, device = self._load(model_class, torch)

        self.assertIs(loaded_model, model)
        self.assertIs(loaded_info, loading_info)
        self.assertEqual(device, "cuda")
        self.assertEqual(
            events,
            ["cuda_available", "load", "attest", "drop_lm_head", "eval", "to:cuda"],
        )
        self.assertEqual(len(model_class.calls), 1)
        args, kwargs = model_class.calls[0]
        self.assertEqual(args, ("/private/model",))
        self.assertEqual(
            kwargs,
            {
                "config": self.config,
                "torch_dtype": self.dtype,
                "local_files_only": True,
                "trust_remote_code": False,
                "use_lm_head": True,
                "output_loading_info": True,
            },
        )
        self.assertNotIn("device_map", kwargs)
        self.assertIs(model.head, learned_head)
        self.assertIsNone(model.lm_head)
        self.assertFalse(model.training)
        self.assertEqual(model.to_calls, ["cuda"])

    def test_cuda_unavailable_fails_before_model_load(self) -> None:
        events: list[str] = []
        model = FakeModel(events)
        model_class = FakePinnedModelClass(events, (model, clean_loading_info()))
        torch = SimpleNamespace(cuda=FakeCuda(events, available=False))

        with self.assertRaisesRegex(runtime.RuntimeLoadError, "CUDA"):
            self._load(model_class, torch)

        self.assertEqual(events, ["cuda_available"])
        self.assertIs(model.lm_head, model.generation_head)
        self.assertTrue(model.training)
        self.assertEqual(model.to_calls, [])

    def test_malformed_loader_result_fails_without_model_mutation(self) -> None:
        events: list[str] = []
        model = FakeModel(events)
        model_class = FakePinnedModelClass(events, model)
        torch = SimpleNamespace(cuda=FakeCuda(events))

        with self.assertRaisesRegex(runtime.RuntimeLoadError, "strict loading information"):
            self._load(model_class, torch)

        self.assertEqual(events, ["cuda_available", "load"])
        self.assertIs(model.lm_head, model.generation_head)
        self.assertTrue(model.training)
        self.assertEqual(model.to_calls, [])

    def test_malformed_loading_info_fails_before_head_attestation_or_move(self) -> None:
        events: list[str] = []
        model = FakeModel(events)
        model_class = FakePinnedModelClass(events, (model, []))
        torch = SimpleNamespace(cuda=FakeCuda(events))

        with patch.object(runtime, "attest_head_load") as attest:
            with self.assertRaisesRegex(runtime.RuntimeLoadError, "malformed model loading"):
                self._load(model_class, torch)

        attest.assert_not_called()
        self.assertEqual(events, ["cuda_available", "load"])
        self.assertIs(model.lm_head, model.generation_head)
        self.assertTrue(model.training)
        self.assertEqual(model.to_calls, [])

    def test_head_attestation_failure_blocks_drop_eval_and_cuda_move(self) -> None:
        events: list[str] = []
        model = FakeModel(events)
        loading_info = clean_loading_info()
        loading_info["missing_keys"] = ["head.weight"]
        model_class = FakePinnedModelClass(events, (model, loading_info))
        torch = SimpleNamespace(cuda=FakeCuda(events))

        with self.assertRaises(contract.HeadAttestationError):
            self._load(model_class, torch)

        self.assertEqual(events, ["cuda_available", "load"])
        self.assertIs(model.lm_head, model.generation_head)
        self.assertTrue(model.training)
        self.assertEqual(model.to_calls, [])

    def test_missing_learned_head_fails_closed_before_mutation(self) -> None:
        events: list[str] = []
        model = FakeModel(events)
        model.head = None
        model_class = FakePinnedModelClass(events, (model, clean_loading_info()))
        torch = SimpleNamespace(cuda=FakeCuda(events))

        with self.assertRaisesRegex(runtime.RuntimeLoadError, "learned Querit head"):
            self._load(model_class, torch)

        self.assertEqual(events, ["cuda_available", "load"])
        self.assertIs(model.lm_head, model.generation_head)
        self.assertTrue(model.training)
        self.assertEqual(model.to_calls, [])


if __name__ == "__main__":
    unittest.main()
