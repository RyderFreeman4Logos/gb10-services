from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

try:
    import querit_checkpoint_convert as converter
except ModuleNotFoundError:
    converter = None  # type: ignore[assignment]


BFLOAT16 = object()
TEMPLATE_PATH = ROOT / "config" / "querit" / "querit-rerank.jinja"
EXPECTED_TEMPLATE = '''{%- set q = (
    messages
    | selectattr("role", "eq", "query")
    | first
).content -%}
{%- set d = (
    messages
    | selectattr("role", "eq", "document")
    | first
).content -%}
{{- "<|im_start|>system\\n"
    ~ "Judge whether the Document meets the requirements based on the Query "
    ~ "and the Instruct provided. Note that the answer can only be \\"yes\\" or \\"no\\"."
    ~ "<|im_end|>\\n"
    ~ "<|im_start|>user\\n"
    ~ "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\\n"
    ~ "<Query>: " ~ q ~ "\\n"
    ~ "<Document>: " ~ d
    ~ "<|im_end|>\\n"
    ~ "<|im_start|>assistant\\n"
-}}
'''


class FakeTensor:
    def __init__(self, data: list[Any], *, dtype: object = BFLOAT16) -> None:
        self.data = data
        self.dtype = dtype

    @property
    def shape(self) -> tuple[int, ...]:
        if self.data and isinstance(self.data[0], list):
            return (len(self.data), len(self.data[0]))
        return (len(self.data),)

    def __getitem__(self, item: slice) -> "FakeTensor":
        return FakeTensor(self.data[item], dtype=self.dtype)

    def __sub__(self, other: "FakeTensor") -> "FakeTensor":
        if self.data and isinstance(self.data[0], list):
            rows = [
                [left - right for left, right in zip(left_row, right_row, strict=True)]
                for left_row, right_row in zip(self.data, other.data, strict=True)
            ]
            return FakeTensor(rows, dtype=self.dtype)
        return FakeTensor(
            [left - right for left, right in zip(self.data, other.data, strict=True)],
            dtype=self.dtype,
        )

    def __truediv__(self, divisor: int) -> "FakeTensor":
        if self.data and isinstance(self.data[0], list):
            return FakeTensor(
                [[value / divisor for value in row] for row in self.data],
                dtype=self.dtype,
            )
        return FakeTensor([value / divisor for value in self.data], dtype=self.dtype)

    def contiguous(self) -> "FakeTensor":
        return self


class PromotingFakeTensor(FakeTensor):
    def __getitem__(self, item: slice) -> "PromotingFakeTensor":
        return PromotingFakeTensor(self.data[item], dtype=self.dtype)

    def __sub__(self, other: "FakeTensor") -> "PromotingFakeTensor":
        result = super().__sub__(other)
        return PromotingFakeTensor(result.data, dtype=self.dtype)

    def __truediv__(self, divisor: int) -> "FakeTensor":
        result = super().__truediv__(divisor)
        return FakeTensor(result.data, dtype=object())


class HeadRewriteTests(unittest.TestCase):
    def test_two_class_bfloat16_head_becomes_scalar_tanh_head(self) -> None:
        self.assertIsNotNone(converter, "conversion module is missing")
        self.assertTrue(hasattr(converter, "rewrite_head_state"))
        state = {
            "model.weight": FakeTensor([[99.0, 98.0]]),
            "head.weight": FakeTensor([[2.0] * 2560, [10.0] * 2560]),
            "head.bias": FakeTensor([4.0, 12.0]),
        }

        converted = converter.rewrite_head_state(  # type: ignore[union-attr]
            state, bfloat16_dtype=BFLOAT16
        )

        self.assertEqual(set(converted), {"model.weight", "score.weight", "score.bias"})
        self.assertIs(converted["model.weight"], state["model.weight"])
        self.assertEqual(converted["score.weight"].shape, (1, 2560))
        self.assertEqual(converted["score.weight"].data, [[4.0] * 2560])
        self.assertEqual(converted["score.bias"].shape, (1,))
        self.assertEqual(converted["score.bias"].data, [4.0])
        self.assertIs(converted["score.weight"].dtype, BFLOAT16)
        self.assertIs(converted["score.bias"].dtype, BFLOAT16)
        self.assertIn("head.weight", state, "source mapping must not be mutated")
        self.assertIn("head.bias", state, "source mapping must not be mutated")

    def test_arithmetic_must_retain_bfloat16_output(self) -> None:
        state = {
            "head.weight": PromotingFakeTensor(
                [[2.0] * 2560, [10.0] * 2560]
            ),
            "head.bias": PromotingFakeTensor([4.0, 12.0]),
        }

        with self.assertRaisesRegex(ValueError, "score dtype"):
            converter.rewrite_head_state(  # type: ignore[union-attr]
                state, bfloat16_dtype=BFLOAT16
            )

    def test_head_contract_mismatches_fail_closed(self) -> None:
        self.assertIsNotNone(converter, "conversion module is missing")
        valid_weight = FakeTensor([[2.0] * 2560, [10.0] * 2560])
        valid_bias = FakeTensor([4.0, 12.0])
        cases = {
            "weight shape": {
                "head.weight": FakeTensor([[2.0], [10.0]]),
                "head.bias": valid_bias,
            },
            "bias shape": {
                "head.weight": valid_weight,
                "head.bias": FakeTensor([4.0]),
            },
            "weight dtype": {
                "head.weight": FakeTensor(
                    [[2.0] * 2560, [10.0] * 2560], dtype=object()
                ),
                "head.bias": valid_bias,
            },
            "bias dtype": {
                "head.weight": valid_weight,
                "head.bias": FakeTensor([4.0, 12.0], dtype=object()),
            },
            "pre-existing score": {
                "head.weight": valid_weight,
                "head.bias": valid_bias,
                "score.weight": FakeTensor([[1.0] * 2560]),
            },
        }
        for label, state in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, label):
                    converter.rewrite_head_state(  # type: ignore[union-attr]
                        state, bfloat16_dtype=BFLOAT16
                    )


class MetadataRewriteTests(unittest.TestCase):
    def test_config_enables_scalar_bfloat16_tanh_sequence_classification(self) -> None:
        source = {
            "architectures": ["MLQwen3Model"],
            "hidden_size": 2560,
            "model_type": "qwen3",
            "num_labels": 2,
            "torch_dtype": "bfloat16",
        }

        self.assertTrue(hasattr(converter, "rewrite_config"))
        converted = converter.rewrite_config(source)  # type: ignore[union-attr]

        self.assertEqual(converted["architectures"], ["Qwen3ForSequenceClassification"])
        self.assertEqual(converted["num_labels"], 1)
        self.assertEqual(converted["head_dtype"], "model")
        self.assertEqual(
            converted["sbert_ce_default_activation_function"],
            "torch.nn.modules.activation.Tanh",
        )
        self.assertEqual(converted["hidden_size"], 2560)
        self.assertEqual(converted["model_type"], "qwen3")
        self.assertEqual(converted["torch_dtype"], "bfloat16")
        self.assertNotIn("problem_type", converted)
        self.assertNotIn("head_dtype", source)

    def test_weight_index_replaces_only_head_keys_and_preserves_metadata(self) -> None:
        source = {
            "metadata": {"total_size": 123456},
            "weight_map": {
                "model.embed_tokens.weight": "model-00001-of-00002.safetensors",
                "head.bias": "model-00002-of-00002.safetensors",
                "head.weight": "model-00002-of-00002.safetensors",
                "model.norm.weight": "model-00002-of-00002.safetensors",
            },
        }

        self.assertTrue(hasattr(converter, "rewrite_weight_index"))
        converted = converter.rewrite_weight_index(  # type: ignore[union-attr]
            source, shard_name="model-00002-of-00002.safetensors"
        )

        self.assertEqual(converted["metadata"], source["metadata"])
        self.assertEqual(
            list(converted["weight_map"]),
            [
                "model.embed_tokens.weight",
                "score.bias",
                "score.weight",
                "model.norm.weight",
            ],
        )
        self.assertEqual(
            converted["weight_map"]["score.weight"],
            "model-00002-of-00002.safetensors",
        )
        self.assertNotIn("head.weight", converted["weight_map"])
        self.assertIn("head.weight", source["weight_map"])

    def test_weight_index_rejects_wrong_shard_or_existing_score_keys(self) -> None:
        cases = {
            "head.weight shard": {
                "head.weight": "model-00001-of-00002.safetensors",
                "head.bias": "model-00002-of-00002.safetensors",
            },
            "pre-existing score": {
                "head.weight": "model-00002-of-00002.safetensors",
                "head.bias": "model-00002-of-00002.safetensors",
                "score.weight": "model-00002-of-00002.safetensors",
            },
        }
        for label, weight_map in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, label):
                    converter.rewrite_weight_index(  # type: ignore[union-attr]
                        {"weight_map": weight_map},
                        shard_name="model-00002-of-00002.safetensors",
                    )


class TemplateContractTests(unittest.TestCase):
    def test_tracked_jinja_template_bytes_are_frozen(self) -> None:
        self.assertTrue(TEMPLATE_PATH.is_file(), "tracked Querit template is missing")
        self.assertEqual(TEMPLATE_PATH.read_text(), EXPECTED_TEMPLATE)


class SnapshotConversionTests(unittest.TestCase):
    def test_snapshot_conversion_updates_all_artifacts_in_place(self) -> None:
        self.assertTrue(hasattr(converter, "convert_snapshot"))
        with tempfile.TemporaryDirectory() as raw_tmp:
            snapshot = Path(raw_tmp)
            shard = snapshot / "model-00002-of-00002.safetensors"
            shard.write_bytes(b"original-shard")
            (snapshot / "model.safetensors.index.json").write_text(
                json.dumps(
                    {
                        "metadata": {"total_size": 123456},
                        "weight_map": {
                            "model.weight": "model-00001-of-00002.safetensors",
                            "head.weight": shard.name,
                            "head.bias": shard.name,
                        },
                    }
                )
            )
            (snapshot / "config.json").write_text(
                json.dumps(
                    {
                        "architectures": ["MLQwen3Model"],
                        "hidden_size": 2560,
                        "num_labels": 2,
                    }
                )
            )
            state = {
                "model.norm.weight": FakeTensor([99.0]),
                "head.weight": FakeTensor([[2.0] * 2560, [10.0] * 2560]),
                "head.bias": FakeTensor([4.0, 12.0]),
            }
            calls: list[tuple[Any, ...]] = []

            def load_shard(path: Path) -> tuple[dict[str, FakeTensor], dict[str, str]]:
                calls.append(("load", path))
                return state, {"format": "pt"}

            def save_shard(
                tensors: dict[str, FakeTensor],
                path: Path,
                metadata: dict[str, str] | None,
            ) -> None:
                calls.append(("save", tensors, path, metadata))
                path.write_bytes(b"converted-shard")

            result = converter.convert_snapshot(  # type: ignore[union-attr]
                snapshot,
                template_path=TEMPLATE_PATH,
                load_shard=load_shard,
                save_shard=save_shard,
                bfloat16_dtype=BFLOAT16,
            )

            self.assertEqual(shard.read_bytes(), b"converted-shard")
            self.assertEqual(calls[0], ("load", shard))
            self.assertEqual(calls[1][0], "save")
            self.assertNotEqual(calls[1][2], shard)
            self.assertEqual(calls[1][3], {"format": "pt"})
            saved_state = calls[1][1]
            self.assertNotIn("head.weight", saved_state)
            self.assertEqual(saved_state["score.weight"].shape, (1, 2560))

            index = json.loads((snapshot / "model.safetensors.index.json").read_text())
            self.assertEqual(index["weight_map"]["score.weight"], shard.name)
            self.assertNotIn("head.weight", index["weight_map"])
            config = json.loads((snapshot / "config.json").read_text())
            self.assertEqual(config["architectures"], ["Qwen3ForSequenceClassification"])
            self.assertEqual(config["num_labels"], 1)
            self.assertEqual(config["head_dtype"], "model")
            self.assertNotIn("problem_type", config)
            self.assertEqual(
                (snapshot / "querit-rerank.jinja").read_text(), EXPECTED_TEMPLATE
            )
            self.assertEqual(result["snapshot"], str(snapshot.resolve()))
            self.assertEqual(result["score_weight_shape"], [1, 2560])
            manifest_path = snapshot / "querit-vllm-artifact-manifest.json"
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(result["artifact_manifest"], manifest_path.name)
            sys.path.insert(0, str(ROOT / "scripts"))
            import querit_vllm_artifact

            verified = querit_vllm_artifact.verify_manifest(snapshot)
            self.assertEqual(verified["source_revision"], converter.SOURCE_REVISION)



if __name__ == "__main__":
    unittest.main()
