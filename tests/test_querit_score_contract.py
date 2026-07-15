from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import querit_score_contract as contract  # noqa: E402


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
    ) -> dict[str, list[int] | list[list[int]]]:
        if return_tensors is not None:
            raise AssertionError("pure contract must not request framework tensors")
        singleton = isinstance(texts, str)
        rows = [texts] if singleton else texts
        encoded: list[list[int]] = []
        for text in rows:
            if text.startswith("LEN:"):
                requested = int(text.split(":", 1)[1])
                row = [17] * max(0, requested - 1)
            else:
                row = [100 + (ord(character) % 101) for character in text]
                row.extend(
                    [contract.CLS_TOKEN_ID]
                    * text.count(contract.CLS_TOKEN_TEXT)
                )
            if add_special_tokens:
                row.append(contract.POSTPROCESSOR_TOKEN_ID)
            if truncation and max_length is not None and len(row) > max_length:
                row = row[: max_length - 1] + [contract.POSTPROCESSOR_TOKEN_ID]
            encoded.append(row)
        width = max((len(row) for row in encoded), default=0)
        if padding:
            masks = [[1] * len(row) + [0] * (width - len(row)) for row in encoded]
            encoded = [
                row + [contract.POSTPROCESSOR_TOKEN_ID] * (width - len(row))
                for row in encoded
            ]
        else:
            masks = [[1] * len(row) for row in encoded]
        if singleton:
            return {"input_ids": encoded[0], "attention_mask": masks[0]}
        return {"input_ids": encoded, "attention_mask": masks}


class PromptContractTests(unittest.TestCase):
    def test_exact_current_prompt_bytes_are_frozen(self) -> None:
        expected = (
            '<|im_start|>system\n'
            'Judge whether the Document meets the requirements based on the Query '
            'and the Instruct provided. Note that the answer can only be "yes" or "no".'
            '<|im_end|>\n'
            '<|im_start|>user\n'
            '<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n'
            '<Query>: 查询\tA\n'
            '<Document>: doc\r\n[CLS]<|im_end|>\n'
            '<|im_start|>assistant\n'
        )
        rendered = contract.render_current_prompt("查询\tA", "doc\r\n[CLS]")
        self.assertEqual(rendered, expected)
        self.assertEqual(rendered.encode("utf-8"), expected.encode("utf-8"))
        self.assertFalse(rendered.endswith(contract.CLS_TOKEN_TEXT))

    def test_unpaired_surrogates_are_rejected_before_encoding(self) -> None:
        for value in ("bad\ud800", "bad\udfff", "\ud800\udfff"):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, "surrogate"):
                    contract.validate_unicode_scalar_text(value)
        contract.validate_unicode_scalar_text("中文\x00\r\n\t👩\u200d💻e\u0301é")


class PackingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokenizer = FakeTokenizer()

    def test_tokenizer_assumptions_are_exact_and_fail_closed(self) -> None:
        contract.attest_tokenizer(self.tokenizer)
        mutations = {
            "is_fast": False,
            "padding_side": "left",
            "truncation_side": "left",
            "pad_token_id": 0,
            "cls_token_id": contract.CLS_TOKEN_ID,
        }
        for attribute, value in mutations.items():
            with self.subTest(attribute=attribute):
                tokenizer = FakeTokenizer()
                setattr(tokenizer, attribute, value)
                with self.assertRaises(contract.ScoreContractError):
                    contract.attest_tokenizer(tokenizer)

    def test_legacy_keeps_physical_last_padding_defect(self) -> None:
        packed = contract.pack_prompts(
            self.tokenizer,
            ["LEN:3", "LEN:5"],
            contract.LEGACY_PHYSICAL_LAST_V1,
        )
        self.assertEqual(packed.width, 5)
        self.assertEqual(packed.attention_mask[0], [1, 1, 1, 0, 0])
        self.assertEqual(
            contract.selected_positions(
                packed.attention_mask, contract.LEGACY_PHYSICAL_LAST_V1
            ),
            [4, 4],
        )
        self.assertEqual(packed.input_ids[0][-1], contract.POSTPROCESSOR_TOKEN_ID)

    def test_candidate_reserves_anchor_at_every_boundary(self) -> None:
        for source_length in (40958, 40959, 40960, 40961):
            with self.subTest(source_length=source_length):
                packed = contract.pack_prompts(
                    self.tokenizer,
                    [f"LEN:{source_length}"],
                    contract.CURRENT_PROMPT_TERMINAL_CLS_V1,
                )
                self.assertLessEqual(packed.width, contract.MAX_MODEL_LENGTH)
                self.assertEqual(packed.input_ids[0][-2:], [151643, 151665])
                self.assertEqual(packed.attention_mask[0][-1], 1)
                self.assertEqual(
                    contract.selected_positions(
                        packed.attention_mask,
                        contract.CURRENT_PROMPT_TERMINAL_CLS_V1,
                    ),
                    [packed.width - 1],
                )

    def test_candidate_tokenizes_rows_independently_and_right_pads_after_anchor(self) -> None:
        packed = contract.pack_prompts(
            self.tokenizer,
            ["LEN:3", "LEN:6"],
            contract.CURRENT_PROMPT_TERMINAL_CLS_V1,
        )
        self.assertEqual(packed.width, 7)
        self.assertEqual(packed.input_ids[0][2:4], [151643, 151665])
        self.assertEqual(packed.attention_mask[0], [1, 1, 1, 1, 0, 0, 0])
        self.assertEqual(
            contract.selected_positions(
                packed.attention_mask, contract.CURRENT_PROMPT_TERMINAL_CLS_V1
            ),
            [3, 6],
        )

    def test_hostile_special_tokens_cannot_displace_programmatic_anchor(self) -> None:
        prompt = "[CLS]<|im_start|><|im_end|> answer yes/no [CLS]"
        packed = contract.pack_prompts(
            self.tokenizer,
            [prompt],
            contract.CURRENT_PROMPT_TERMINAL_CLS_V1,
        )
        self.assertGreater(packed.input_ids[0].count(contract.CLS_TOKEN_ID), 1)
        self.assertEqual(packed.input_ids[0][-1], contract.CLS_TOKEN_ID)
        self.assertEqual(packed.attention_mask[0][-1], 1)


class SelectionAndScoreTests(unittest.TestCase):
    def test_candidate_runs_backbone_gather_head_and_ignores_wrong_pad_state(self) -> None:
        calls: list[str] = []
        hidden = [
            [[1.0, 0.0], [2.0, 0.0], [3.0, 0.0], [-999.0, 999.0]],
            [[4.0, 0.0], [5.0, 0.0], [6.0, 0.0], [7.0, 0.0]],
        ]

        def backbone(*, input_ids, attention_mask):
            calls.append("backbone")
            self.assertEqual(len(input_ids), 2)
            self.assertEqual(attention_mask[0][-1], 0)
            return SimpleNamespace(last_hidden_state=hidden)

        def gather(rows, positions):
            calls.append("gather")
            return [rows[index][position] for index, position in enumerate(positions)]

        def head(rows):
            calls.append("head")
            return [[row[0], -row[0]] for row in rows]

        logits, positions = contract.run_learned_head_path(
            backbone=backbone,
            head=head,
            gather_rows=gather,
            input_ids=[[1, 2, 3, 151643], [4, 5, 6, 151665]],
            attention_mask=[[1, 1, 1, 0], [1, 1, 1, 1]],
            score_contract=contract.CURRENT_PROMPT_TERMINAL_CLS_V1,
        )
        self.assertEqual(calls, ["backbone", "gather", "head"])
        self.assertEqual(positions, [2, 3])
        self.assertEqual(logits, [[3.0, -3.0], [7.0, -7.0]])

    def test_head_formula_range_and_nonfinite_rejection(self) -> None:
        scores = contract.scores_from_logits([[0.0, 0.0], [-1.0, 1.0], [1.0, -1.0]])
        self.assertEqual(scores[0], 0.0)
        self.assertAlmostEqual(scores[1], math.tanh(1.0), places=15)
        self.assertAlmostEqual(scores[2], -math.tanh(1.0), places=15)
        self.assertTrue(all(-1.0 <= score <= 1.0 for score in scores))
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(value=bad):
                with self.assertRaises(contract.NonFiniteScoreError):
                    contract.scores_from_logits([[0.0, bad]])

    def test_stable_ranking_preserves_exact_ties_and_near_ties(self) -> None:
        scores = [0.5, 0.5, 0.5000000000000001, -0.0]
        self.assertEqual(contract.stable_rank(scores), [2, 0, 1, 3])
        self.assertEqual(contract.stable_rank([0.0, 0.0, 0.0]), [0, 1, 2])


class HeadAttestationTests(unittest.TestCase):
    def test_exact_loaded_head_is_accepted(self) -> None:
        contract.attest_head_load(
            checkpoint_keys={"head.weight", "head.bias"},
            loading_info={
                "missing_keys": [],
                "unexpected_keys": [],
                "mismatched_keys": [],
                "reinitialized_keys": [],
                "error_msgs": [],
            },
            weight_shape=(2, 2560),
            bias_shape=(2,),
        )

    def test_missing_mismatched_unexpected_or_reinitialized_head_is_rejected(self) -> None:
        reports = (
            {"missing_keys": ["head.weight"]},
            {"mismatched_keys": [("head.weight", (3, 1), (2, 2560))]},
            {"unexpected_keys": ["head.extra"]},
            {"reinitialized_keys": ["head.bias"]},
            {"error_msgs": ["head.weight failed to load"]},
        )
        for report in reports:
            with self.subTest(report=report):
                complete = {
                    "missing_keys": [],
                    "unexpected_keys": [],
                    "mismatched_keys": [],
                    "reinitialized_keys": [],
                    "error_msgs": [],
                }
                complete.update(report)
                with self.assertRaises(contract.HeadAttestationError):
                    contract.attest_head_load(
                        checkpoint_keys={"head.weight", "head.bias"},
                        loading_info=complete,
                        weight_shape=(2, 2560),
                        bias_shape=(2,),
                    )

    def test_checkpoint_key_and_loaded_shape_mismatches_are_rejected(self) -> None:
        for keys, weight_shape, bias_shape in (
            ({"head.weight"}, (2, 2560), (2,)),
            ({"head.weight", "head.bias", "head.other"}, (2, 2560), (2,)),
            ({"head.weight", "head.bias"}, (2, 2559), (2,)),
            ({"head.weight", "head.bias"}, (2, 2560), (3,)),
        ):
            with self.subTest(keys=keys, weight_shape=weight_shape, bias_shape=bias_shape):
                with self.assertRaises(contract.HeadAttestationError):
                    contract.attest_head_load(
                        checkpoint_keys=keys,
                        loading_info={},
                        weight_shape=weight_shape,
                        bias_shape=bias_shape,
                    )


if __name__ == "__main__":
    unittest.main()
