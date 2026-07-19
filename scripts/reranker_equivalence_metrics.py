#!/usr/bin/env python3
"""Strict corpus loading and quality metrics for reranker equivalence."""

from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reranker_equivalence_wire import CorpusValidationError, canonical_payload


MAX_CORPUS_BYTES = 64 * 1024 * 1024
PROMPT_OVERHEAD_TOKENS_PER_PAIR = 256


@dataclass(frozen=True)
class Candidate:
    document_id: str
    document: str
    relevance: int
    source_language: str
    top_ranked_rank: int


@dataclass(frozen=True)
class QueryGroup:
    query_id: str
    query: str
    source_language: str
    candidates: tuple[Candidate, ...]


def _validate_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusValidationError(f"{label} must be non-empty text")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CorpusValidationError(f"{label} contains an unpaired surrogate")
    return value


def _read_regular(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CorpusValidationError("cannot open corpus as a regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 0
            or metadata.st_size > maximum
        ):
            raise CorpusValidationError("corpus is unsafe or oversized")
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum:
            raise CorpusValidationError("corpus is oversized")
        return bytes(payload)
    finally:
        os.close(descriptor)


def load_corpus(path: Path) -> list[QueryGroup]:
    """Load and strictly validate query groups from a public JSONL corpus."""

    raw = _read_regular(path, MAX_CORPUS_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusValidationError("corpus is not UTF-8") from exc
    groups: list[QueryGroup] = []
    identities: set[tuple[str, str]] = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            raise CorpusValidationError(f"blank corpus line at {line_number}")
        try:
            row = json.loads(
                line,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise CorpusValidationError(
                f"invalid JSON on corpus line {line_number}"
            ) from exc
        if not isinstance(row, dict) or set(row) != {
            "candidates",
            "query",
            "query_id",
            "source_language",
        }:
            raise CorpusValidationError(
                f"group fields are not exact on line {line_number}"
            )
        query_id = _validate_text(row["query_id"], "query_id")
        query = _validate_text(row["query"], "query")
        language = _validate_text(row["source_language"], "source_language")
        identity = (language, query_id)
        if identity in identities:
            raise CorpusValidationError("duplicate language/query identity")
        identities.add(identity)
        raw_candidates = row["candidates"]
        if not isinstance(raw_candidates, list) or len(raw_candidates) != 10:
            raise CorpusValidationError(
                "each query group must contain exactly 10 candidates"
            )
        candidates: list[Candidate] = []
        document_ids: set[str] = set()
        ranks: list[int] = []
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, dict) or set(raw_candidate) != {
                "document",
                "document_id",
                "relevance",
                "source_language",
                "top_ranked_rank",
            }:
                raise CorpusValidationError("candidate fields are not exact")
            document_id = _validate_text(raw_candidate["document_id"], "document_id")
            document = _validate_text(raw_candidate["document"], "document")
            candidate_language = _validate_text(
                raw_candidate["source_language"], "candidate source_language"
            )
            relevance = raw_candidate["relevance"]
            rank = raw_candidate["top_ranked_rank"]
            if (
                isinstance(relevance, bool)
                or not isinstance(relevance, int)
                or not 0 <= relevance <= 100
                or isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank <= 0
            ):
                raise CorpusValidationError(
                    "candidate relevance or top_ranked_rank is invalid"
                )
            if candidate_language != language:
                raise CorpusValidationError(
                    "candidate source language differs from query"
                )
            if document_id in document_ids:
                raise CorpusValidationError(
                    "query group contains duplicate document IDs"
                )
            document_ids.add(document_id)
            ranks.append(rank)
            candidates.append(
                Candidate(
                    document_id=document_id,
                    document=document,
                    relevance=relevance,
                    source_language=candidate_language,
                    top_ranked_rank=rank,
                )
            )
        if ranks != sorted(ranks) or len(set(ranks)) != len(ranks):
            raise CorpusValidationError(
                "candidate top_ranked ranks must be unique and ordered"
            )
        if not any(candidate.relevance > 0 for candidate in candidates):
            raise CorpusValidationError("query group has no qrels-positive candidate")
        if not any(candidate.relevance == 0 for candidate in candidates):
            raise CorpusValidationError("query group has no qrels-zero hard negative")
        groups.append(QueryGroup(query_id, query, language, tuple(candidates)))
    if not groups:
        raise CorpusValidationError("corpus contains no query groups")
    return groups


def estimate_input_tokens(
    groups: Sequence[QueryGroup], *, instruction: str | None = None
) -> int:
    """Conservatively upper-bound tokens by UTF-8 bytes plus prompt overhead."""

    instruction_bytes = len(instruction.encode("utf-8")) if instruction else 0
    total = 0
    for group in groups:
        query_bytes = len(group.query.encode("utf-8"))
        for candidate in group.candidates:
            total += (
                query_bytes
                + len(candidate.document.encode("utf-8"))
                + instruction_bytes
                + PROMPT_OVERHEAD_TOKENS_PER_PAIR
            )
    return total


def build_batches(
    groups: Sequence[QueryGroup],
    batch_size: int,
    *,
    instruction: str | None = None,
    service_tier: str | None = None,
) -> list[bytes]:
    if not 1 <= batch_size <= 1024:
        raise ValueError("batch size must be in [1, 1024]")
    pairs = [
        (group.query, candidate.document)
        for group in groups
        for candidate in group.candidates
    ]
    return [
        canonical_payload(
            [query for query, _document in pairs[offset : offset + batch_size]],
            [document for _query, document in pairs[offset : offset + batch_size]],
            instruction=instruction,
            service_tier=service_tier,
        )
        for offset in range(0, len(pairs), batch_size)
    ]


def rank_indices(scores: Sequence[float]) -> list[int]:
    """Rank descending with original candidate position as the stable tie-break."""

    if not scores or any(
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        for score in scores
    ):
        raise ValueError("scores must be a non-empty finite numeric sequence")
    return sorted(range(len(scores)), key=lambda index: (-float(scores[index]), index))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _quality_for_group(group: QueryGroup, scores: Sequence[float]) -> dict[str, float]:
    ranking = rank_indices(scores)[:10]
    relevances = [group.candidates[index].relevance for index in ranking]
    positive_ranks = [
        index + 1 for index, relevance in enumerate(relevances) if relevance > 0
    ]
    mrr = 1.0 / positive_ranks[0] if positive_ranks else 0.0
    dcg = sum(
        (2.0**relevance - 1.0) / math.log2(rank + 2)
        for rank, relevance in enumerate(relevances)
    )
    ideal = sorted(
        (candidate.relevance for candidate in group.candidates), reverse=True
    )[:10]
    idcg = sum(
        (2.0**relevance - 1.0) / math.log2(rank + 2)
        for rank, relevance in enumerate(ideal)
    )
    precision_sum = 0.0
    positives_seen = 0
    for rank, relevance in enumerate(relevances, 1):
        if relevance > 0:
            positives_seen += 1
            precision_sum += positives_seen / rank
    total_positives = sum(candidate.relevance > 0 for candidate in group.candidates)
    return {
        "map_at_10": precision_sum / min(total_positives, 10),
        "mrr_at_10": mrr,
        "ndcg_at_10": dcg / idcg if idcg else 0.0,
    }


def _score_domain(scores: Sequence[float]) -> dict[str, float | int]:
    mean = _mean(scores)
    return {
        "count": len(scores),
        "max": max(scores),
        "mean": mean,
        "min": min(scores),
        "standard_deviation": math.sqrt(
            _mean([(score - mean) ** 2 for score in scores])
        ),
    }


def compute_endpoint_metrics(
    groups: Sequence[QueryGroup], scores: Sequence[float]
) -> dict[str, object]:
    """Compute independent public-label quality metrics without a threshold."""

    expected = sum(len(group.candidates) for group in groups)
    if len(scores) != expected:
        raise ValueError("score count differs from corpus pair count")
    if any(not math.isfinite(float(score)) for score in scores):
        raise ValueError("score metrics require finite values")
    rows: list[tuple[str, dict[str, float]]] = []
    offset = 0
    for group in groups:
        width = len(group.candidates)
        rows.append(
            (
                group.source_language,
                _quality_for_group(group, scores[offset : offset + width]),
            )
        )
        offset += width

    def summarize(
        selected: Sequence[dict[str, float]],
    ) -> dict[str, float | int]:
        return {
            "groups": len(selected),
            "map_at_10": _mean([row["map_at_10"] for row in selected]),
            "mrr_at_10": _mean([row["mrr_at_10"] for row in selected]),
            "ndcg_at_10": _mean([row["ndcg_at_10"] for row in selected]),
        }

    languages = sorted({language for language, _row in rows})
    return {
        "aggregate": summarize([row for _language, row in rows]),
        "per_language": {
            language: summarize(
                [row for row_language, row in rows if row_language == language]
            )
            for language in languages
        },
        "score_domain": _score_domain([float(score) for score in scores]),
    }


def _spearman(left: Sequence[int], right: Sequence[int]) -> float:
    if len(left) != len(right) or set(left) != set(right):
        raise ValueError("rankings must contain the same candidates")
    if len(left) < 2:
        return 1.0
    right_positions = {candidate: rank for rank, candidate in enumerate(right)}
    distance = sum(
        (rank - right_positions[candidate]) ** 2 for rank, candidate in enumerate(left)
    )
    count = len(left)
    return 1.0 - 6.0 * distance / (count * (count * count - 1))


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean, right_mean = _mean(left), _mean(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_delta)
        * sum(value * value for value in right_delta)
    )
    if denominator == 0:
        return 1.0 if list(left) == list(right) else 0.0
    return (
        sum(a * b for a, b in zip(left_delta, right_delta, strict=True)) / denominator
    )


def compute_comparison_metrics(
    groups: Sequence[QueryGroup],
    cloud_scores: Sequence[float],
    local_scores: Sequence[float],
) -> dict[str, object]:
    """Compute rank agreement and calibration without score equality gates."""

    expected = sum(len(group.candidates) for group in groups)
    if len(cloud_scores) != expected or len(local_scores) != expected:
        raise ValueError("comparison score count differs from corpus")
    correlations: list[float] = []
    overlaps = {1: [], 3: [], 5: [], 10: []}
    offset = 0
    for group in groups:
        width = len(group.candidates)
        cloud_rank = rank_indices(cloud_scores[offset : offset + width])
        local_rank = rank_indices(local_scores[offset : offset + width])
        correlations.append(_spearman(cloud_rank, local_rank))
        for k in overlaps:
            top = min(k, width)
            overlaps[k].append(len(set(cloud_rank[:top]) & set(local_rank[:top])) / top)
        offset += width
    differences = [
        float(local) - float(cloud)
        for cloud, local in zip(cloud_scores, local_scores, strict=True)
    ]
    cloud_mean = _mean([float(value) for value in cloud_scores])
    local_mean = _mean([float(value) for value in local_scores])
    variance = sum((float(value) - cloud_mean) ** 2 for value in cloud_scores)
    covariance = sum(
        (float(cloud) - cloud_mean) * (float(local) - local_mean)
        for cloud, local in zip(cloud_scores, local_scores, strict=True)
    )
    slope = covariance / variance if variance else None
    intercept = local_mean - slope * cloud_mean if slope is not None else local_mean
    return {
        "rank_correlation": {
            "mean_spearman": _mean(correlations),
            "min_spearman": min(correlations),
        },
        "score_calibration": {
            "local_on_cloud_intercept": intercept,
            "local_on_cloud_slope": slope,
            "mean_absolute_error": _mean([abs(value) for value in differences]),
            "mean_difference_local_minus_cloud": _mean(differences),
            "paired_pearson": _pearson(
                [float(value) for value in cloud_scores],
                [float(value) for value in local_scores],
            ),
            "rmse": math.sqrt(_mean([value * value for value in differences])),
        },
        "top_k_overlap": {f"at_{k}": _mean(values) for k, values in overlaps.items()},
    }


__all__ = [
    "Candidate",
    "QueryGroup",
    "build_batches",
    "compute_comparison_metrics",
    "compute_endpoint_metrics",
    "estimate_input_tokens",
    "load_corpus",
    "rank_indices",
]
