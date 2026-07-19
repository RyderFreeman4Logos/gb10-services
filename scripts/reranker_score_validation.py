#!/usr/bin/env python3
"""Shared strict numeric score validation for reranker evidence paths."""

from __future__ import annotations

import math


class ScoreValidationError(ValueError):
    """A score array cannot be accepted as equivalence evidence."""


def validate_scores(
    scores: object,
    expected: int,
    *,
    minimum: float,
    maximum: float,
    label: str,
) -> tuple[float, ...]:
    """Require exact cardinality and finite non-Boolean scores in a closed domain."""

    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        raise ValueError("expected score cardinality must be a positive integer")
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, (int, float))
        or not isinstance(maximum, (int, float))
        or not math.isfinite(float(minimum))
        or not math.isfinite(float(maximum))
        or minimum > maximum
    ):
        raise ValueError("score domain must be finite and ordered")
    if not isinstance(scores, list) or len(scores) != expected:
        raise ScoreValidationError(f"{label} cardinality mismatch")

    validated: list[float] = []
    for value in scores:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ScoreValidationError(f"{label} must contain finite non-Boolean numbers")
        score = float(value)
        if not float(minimum) <= score <= float(maximum):
            raise ScoreValidationError(
                f"{label} must stay within [{minimum}, {maximum}]"
            )
        validated.append(score)
    return tuple(validated)


__all__ = ["ScoreValidationError", "validate_scores"]
