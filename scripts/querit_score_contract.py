#!/usr/bin/env python3
"""Pure Querit prompt, packing, selection, head, and ranking contracts.

This module deliberately performs no filesystem, network, HTTP, framework, or model
loading.  Callers supply a tokenizer and the backbone/head operations.  The two
named contracts are explicit because the candidate is not publisher-equivalent:
the publisher's exact title/content serializer has not been released.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = [
    "CLS_TOKEN_ID",
    "CLS_TOKEN_TEXT",
    "CURRENT_PROMPT_TERMINAL_CLS_V1",
    "HeadAttestationError",
    "LEGACY_PHYSICAL_LAST_V1",
    "MAX_MODEL_LENGTH",
    "NonFiniteScoreError",
    "POSTPROCESSOR_TOKEN_ID",
    "PackedBatch",
    "ScoreContractError",
    "attest_head_load",
    "attest_tokenizer",
    "pack_prompts",
    "render_current_prompt",
    "run_learned_head_path",
    "scores_from_logits",
    "selected_positions",
    "stable_rank",
    "validate_unicode_scalar_text",
]

LEGACY_PHYSICAL_LAST_V1 = "legacy-physical-last-v1"
CURRENT_PROMPT_TERMINAL_CLS_V1 = "current-prompt-terminal-cls-v1"
SCORE_CONTRACTS = frozenset(
    {LEGACY_PHYSICAL_LAST_V1, CURRENT_PROMPT_TERMINAL_CLS_V1}
)
MAX_MODEL_LENGTH = 40_960
POSTPROCESSOR_TOKEN_ID = 151_643
CLS_TOKEN_ID = 151_665
CLS_TOKEN_TEXT = "[CLS]"
EXPECTED_HEAD_KEYS = frozenset({"head.weight", "head.bias"})
EXPECTED_HEAD_WEIGHT_SHAPE = (2, 2560)
EXPECTED_HEAD_BIAS_SHAPE = (2,)


class ScoreContractError(ValueError):
    """Raised when a named score contract or tokenizer invariant is violated."""


class NonFiniteScoreError(ScoreContractError):
    """Raised when logits, probabilities, or final scores are not finite."""


class HeadAttestationError(ScoreContractError):
    """Raised when checkpoint loading does not prove the learned head is exact."""


@dataclass(frozen=True)
class PackedBatch:
    """A framework-independent right-padded tokenizer batch."""

    input_ids: list[list[int]]
    attention_mask: list[list[int]]
    unpadded_lengths: list[int]
    score_contract: str

    @property
    def width(self) -> int:
        return len(self.input_ids[0]) if self.input_ids else 0


def validate_unicode_scalar_text(value: str) -> None:
    """Reject lone surrogate code points before UTF-8 or tokenization."""

    if not isinstance(value, str):
        raise TypeError("Querit input must be text")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("Querit input contains an unpaired Unicode surrogate")


def render_current_prompt(query: str, document: str) -> str:
    """Render the byte-exact legacy prompt without claiming publisher semantics."""

    validate_unicode_scalar_text(query)
    validate_unicode_scalar_text(document)
    return (
        "<|im_start|>system\n"
        "Judge whether the Document meets the requirements based on the Query "
        'and the Instruct provided. Note that the answer can only be "yes" or "no".'
        "<|im_end|>\n"
        "<|im_start|>user\n"
        "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n"
        f"<Query>: {query}\n"
        f"<Document>: {document}"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def _require_contract(score_contract: str) -> None:
    if score_contract not in SCORE_CONTRACTS:
        raise ScoreContractError(f"unknown Querit score contract: {score_contract}")


def attest_tokenizer(tokenizer: Any) -> None:
    """Attest the pinned fast-tokenizer assumptions needed by both contracts."""

    expected = {
        "is_fast": True,
        "padding_side": "right",
        "truncation_side": "right",
        "pad_token_id": POSTPROCESSOR_TOKEN_ID,
        "cls_token_id": None,
    }
    mismatches = [
        f"{name}={getattr(tokenizer, name, '<missing>')!r}"
        for name, wanted in expected.items()
        if getattr(tokenizer, name, object()) != wanted
    ]
    if mismatches:
        raise ScoreContractError(
            "pinned Querit tokenizer assumptions failed: " + ", ".join(mismatches)
        )


def _integer_row(value: Any, field: str) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ScoreContractError(f"tokenizer {field} row is not a sequence")
    row: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ScoreContractError(f"tokenizer {field} contains a non-integer")
        row.append(item)
    return row


def _batch_rows(value: Any, expected_rows: int, field: str) -> list[list[int]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ScoreContractError(f"tokenizer {field} is not a sequence")
    if expected_rows == 1 and (not value or isinstance(value[0], int)):
        return [_integer_row(value, field)]
    rows = [_integer_row(row, field) for row in value]
    if len(rows) != expected_rows:
        raise ScoreContractError(
            f"tokenizer {field} returned {len(rows)} rows, expected {expected_rows}"
        )
    return rows


def _validate_unpadded_row(ids: list[int], mask: list[int], maximum: int) -> None:
    if not ids or len(ids) != len(mask):
        raise ScoreContractError("tokenizer IDs/mask lengths are invalid")
    if len(ids) > maximum:
        raise ScoreContractError("tokenizer exceeded the declared maximum length")
    if any(bit != 1 for bit in mask):
        raise ScoreContractError("independently tokenized row contains padding")
    if ids[-1] != POSTPROCESSOR_TOKEN_ID:
        raise ScoreContractError(
            "pinned fast tokenizer did not retain terminal postprocessor token 151643"
        )


def _right_pad(
    rows: list[list[int]], masks: list[list[int]]
) -> tuple[list[list[int]], list[list[int]], list[int]]:
    lengths = [len(row) for row in rows]
    width = max(lengths, default=0)
    padded_ids: list[list[int]] = []
    padded_masks: list[list[int]] = []
    for ids, mask in zip(rows, masks, strict=True):
        padding = width - len(ids)
        padded_ids.append(ids + [POSTPROCESSOR_TOKEN_ID] * padding)
        padded_masks.append(mask + [0] * padding)
    return padded_ids, padded_masks, lengths


def pack_prompts(
    tokenizer: Any,
    prompts: Sequence[str],
    score_contract: str,
    *,
    max_model_length: int = MAX_MODEL_LENGTH,
) -> PackedBatch:
    """Pack prompts according to a named legacy or terminal-CLS contract."""

    _require_contract(score_contract)
    attest_tokenizer(tokenizer)
    if not prompts:
        raise ScoreContractError("at least one prompt is required")
    if not 2 <= max_model_length <= MAX_MODEL_LENGTH:
        raise ScoreContractError("Querit maximum length must be in [2, 40960]")
    for prompt in prompts:
        validate_unicode_scalar_text(prompt)

    if score_contract == LEGACY_PHYSICAL_LAST_V1:
        encoded = tokenizer(
            list(prompts),
            add_special_tokens=True,
            padding=True,
            truncation=True,
            max_length=max_model_length,
        )
        ids = _batch_rows(encoded.get("input_ids"), len(prompts), "input_ids")
        masks = _batch_rows(
            encoded.get("attention_mask"), len(prompts), "attention_mask"
        )
        if any(len(row) != len(mask) for row, mask in zip(ids, masks, strict=True)):
            raise ScoreContractError("legacy tokenizer IDs/mask lengths differ")
        if any(len(row) > max_model_length for row in ids):
            raise ScoreContractError("legacy tokenizer exceeded maximum length")
        lengths = [sum(mask) for mask in masks]
        if any(length <= 0 for length in lengths):
            raise ScoreContractError("legacy tokenizer returned an empty row")
        return PackedBatch(ids, masks, lengths, score_contract)

    rows: list[list[int]] = []
    masks: list[list[int]] = []
    reserved_maximum = max_model_length - 1
    for prompt in prompts:
        encoded = tokenizer(
            prompt,
            add_special_tokens=True,
            padding=False,
            truncation=True,
            max_length=reserved_maximum,
        )
        ids = _batch_rows(encoded.get("input_ids"), 1, "input_ids")[0]
        mask = _batch_rows(encoded.get("attention_mask"), 1, "attention_mask")[0]
        _validate_unpadded_row(ids, mask, reserved_maximum)
        rows.append(ids + [CLS_TOKEN_ID])
        masks.append(mask + [1])
    padded_ids, padded_masks, lengths = _right_pad(rows, masks)
    return PackedBatch(
        padded_ids,
        padded_masks,
        lengths,
        CURRENT_PROMPT_TERMINAL_CLS_V1,
    )


def selected_positions(
    attention_mask: Sequence[Sequence[int]], score_contract: str
) -> list[int]:
    """Return physical-last positions for legacy or last-real positions for candidate."""

    _require_contract(score_contract)
    if not attention_mask:
        raise ScoreContractError("attention mask must contain rows")
    width = len(attention_mask[0])
    if width == 0 or any(len(row) != width for row in attention_mask):
        raise ScoreContractError("attention mask must be a non-empty rectangle")
    positions: list[int] = []
    for row in attention_mask:
        if any(bit not in (0, 1) for bit in row):
            raise ScoreContractError("attention mask must contain only zero and one")
        if score_contract == LEGACY_PHYSICAL_LAST_V1:
            positions.append(width - 1)
            continue
        real_length = sum(row)
        if real_length <= 0 or row[:real_length] != [1] * real_length:
            raise ScoreContractError("candidate attention mask is not right padded")
        if row[real_length:] != [0] * (width - real_length):
            raise ScoreContractError("candidate attention mask is not right padded")
        positions.append(real_length - 1)
    return positions


T = TypeVar("T")
U = TypeVar("U")


def run_learned_head_path(
    *,
    backbone: Callable[..., Any],
    head: Callable[[Any], T],
    gather_rows: Callable[[Any, Sequence[int]], Any],
    input_ids: Any,
    attention_mask: Sequence[Sequence[int]],
    score_contract: str,
) -> tuple[T, list[int]]:
    """Execute backbone, explicit position gather, then the learned classifier head."""

    positions = selected_positions(attention_mask, score_contract)
    outputs = backbone(input_ids=input_ids, attention_mask=attention_mask)
    hidden = getattr(outputs, "last_hidden_state", None)
    if hidden is None and isinstance(outputs, Mapping):
        hidden = outputs.get("last_hidden_state")
    if hidden is None:
        raise ScoreContractError("backbone did not return last_hidden_state")
    selected = gather_rows(hidden, positions)
    return head(selected), positions


def _score_pair(zero: float, one: float) -> float:
    if not math.isfinite(zero) or not math.isfinite(one):
        raise NonFiniteScoreError("Querit logits must be finite")
    scale = max(zero, one)
    exp_zero = math.exp(zero - scale)
    exp_one = math.exp(one - scale)
    denominator = exp_zero + exp_one
    p_zero = exp_zero / denominator
    p_one = exp_one / denominator
    score = p_one - p_zero
    if not all(math.isfinite(value) for value in (p_zero, p_one, score)):
        raise NonFiniteScoreError("Querit probability or score is non-finite")
    if not -1.0 <= score <= 1.0:
        raise ScoreContractError("Querit p1-p0 score is outside [-1, 1]")
    return score


def scores_from_logits(logits: Iterable[Sequence[float]]) -> list[float]:
    """Apply stable two-class softmax followed by the exact p1-p0 formula."""

    scores: list[float] = []
    for row in logits:
        if len(row) != 2:
            raise ScoreContractError("Querit learned head must return exactly two logits")
        scores.append(_score_pair(float(row[0]), float(row[1])))
    return scores


def stable_rank(scores: Sequence[float]) -> list[int]:
    """Rank descending without rounding; Python stability preserves exact ties."""

    if any(not math.isfinite(float(score)) for score in scores):
        raise NonFiniteScoreError("cannot rank a non-finite Querit score")
    return sorted(range(len(scores)), key=lambda index: float(scores[index]), reverse=True)


def _head_related(value: Any) -> bool:
    if isinstance(value, str):
        return "head." in value or value == "head"
    if isinstance(value, Mapping):
        return any(_head_related(item) for item in value.items())
    if isinstance(value, Iterable):
        return any(_head_related(item) for item in value)
    return False


def attest_head_load(
    *,
    checkpoint_keys: Iterable[str],
    loading_info: Mapping[str, Any],
    weight_shape: Sequence[int],
    bias_shape: Sequence[int],
) -> None:
    """Fail closed unless the exact learned head was consumed with exact shapes."""

    actual_head_keys = frozenset(
        key for key in checkpoint_keys if key == "head" or key.startswith("head.")
    )
    if actual_head_keys != EXPECTED_HEAD_KEYS:
        raise HeadAttestationError(
            f"checkpoint head keys are not exact: {sorted(actual_head_keys)!r}"
        )
    if tuple(weight_shape) != EXPECTED_HEAD_WEIGHT_SHAPE:
        raise HeadAttestationError(
            f"head.weight shape is {tuple(weight_shape)!r}, expected {EXPECTED_HEAD_WEIGHT_SHAPE!r}"
        )
    if tuple(bias_shape) != EXPECTED_HEAD_BIAS_SHAPE:
        raise HeadAttestationError(
            f"head.bias shape is {tuple(bias_shape)!r}, expected {EXPECTED_HEAD_BIAS_SHAPE!r}"
        )
    report_fields = (
        "missing_keys",
        "unexpected_keys",
        "mismatched_keys",
        "reinitialized_keys",
        "error_msgs",
    )
    failures = [
        field for field in report_fields if _head_related(loading_info.get(field, []))
    ]
    if failures:
        raise HeadAttestationError(
            "head load report is not clean for: " + ", ".join(failures)
        )
