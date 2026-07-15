#!/usr/bin/env python3
"""Evidence-derived validation for the fixed Querit dual-replay plan."""

from __future__ import annotations

import math
import statistics
import struct
from collections import defaultdict
from typing import Any, Mapping, Sequence

from querit_replay_plan import (
    corpus_definition_sha256,
    corpus_definitions,
    replay_schedule,
    schedule_sha256,
)
from querit_replay_schema import (
    CANDIDATE_CONTRACT,
    LEGACY_CONTRACT,
    MAX_ERROR_COUNT,
    REQUIRED_PASS_GATES,
    canonical_json_bytes,
    sha256_bytes,
)


class EvidenceError(ValueError):
    """Persisted evidence disagrees with the fixed plan or claimed gates."""


def _cell(row: Mapping[str, Any], name: str) -> float:
    aliases = {
        "logit_0": ("logits", 0),
        "logit_1": ("logits", 1),
        "probability_0": ("probabilities", 0),
        "probability_1": ("probabilities", 1),
    }
    try:
        if name in aliases:
            field, index = aliases[name]
            value = row[field][index]
        else:
            value = row[name]
        decoded = struct.unpack(">f", bytes.fromhex(value["f32_be"]))[0]
        declared = float(value["value"])
        if not math.isfinite(declared) or declared != decoded:
            raise ValueError("float cell declaration differs from canonical bytes")
        return decoded
    except (IndexError, KeyError, TypeError, ValueError, struct.error) as exc:
        raise EvidenceError(f"invalid {name} float cell") from exc


def _pairwise_rank_preserved(
    baseline: Sequence[float], observed: Sequence[float], tolerance: float
) -> bool:
    for left in range(len(baseline)):
        for right in range(left + 1, len(baseline)):
            gap = baseline[left] - baseline[right]
            if abs(gap) <= 2 * tolerance:
                continue
            observed_gap = observed[left] - observed[right]
            if (gap > 0 and observed_gap <= 0) or (gap < 0 and observed_gap >= 0):
                return False
    return True


def _tolerance(jitter: float, batch_delta: float, scalar: str) -> float:
    floor, cap = {
        "logits": (1e-5, 5e-2),
        "probabilities": (1e-6, 5e-3),
        "score": (1e-6, 1e-2),
    }[scalar]
    value = max(floor, 8 * jitter, 4 * batch_delta)
    if value > cap:
        raise EvidenceError(f"calibrated {scalar} tolerance exceeds cap")
    return value


def calibrate_tolerances(
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float]]:
    rows = [
        row
        for row in observations
        if row["track"] == CANDIDATE_CONTRACT and row["phase"] == "w4-calibration"
    ]
    result: dict[str, dict[str, float]] = {}
    for scalar, fields in (
        ("logits", ("logit_0", "logit_1")),
        ("probabilities", ("probability_0", "probability_1")),
        ("score", ("native_score",)),
    ):
        jitter = 0.0
        batch_delta = 0.0
        for case_id in ("W01", "W02", "W03", "W04"):
            singleton = [row for row in rows if row["case_id"] == case_id and row["batch_size"] == 1]
            batched = [row for row in rows if row["case_id"] == case_id and row["batch_size"] == 4]
            if len(singleton) != 5 or len(batched) != 5:
                raise EvidenceError("candidate calibration matrix is incomplete")
            for field in fields:
                single_values = [_cell(row, field) for row in singleton]
                batch_values = [_cell(row, field) for row in batched]
                single_median = statistics.median(single_values)
                batch_median = statistics.median(batch_values)
                jitter = max(
                    jitter,
                    max(abs(value - single_median) for value in single_values),
                    max(abs(value - batch_median) for value in batch_values),
                )
                batch_delta = max(batch_delta, abs(single_median - batch_median))
        floor, cap = {
            "logits": (1e-5, 5e-2),
            "probabilities": (1e-6, 5e-3),
            "score": (1e-6, 1e-2),
        }[scalar]
        result[scalar] = {
            "batch_delta": batch_delta,
            "cap": cap,
            "floor": floor,
            "jitter": jitter,
            "value": _tolerance(jitter, batch_delta, scalar),
        }
    return result


def _validate_corpus(
    manifest: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]
) -> None:
    definitions = {case.case_id: case for case in corpus_definitions()}
    if manifest["corpus_definition_sha256"] != corpus_definition_sha256():
        raise EvidenceError("fixed corpus definition hash changed")
    for row in cases:
        case_id = row["case_id"]
        if case_id not in definitions:
            raise EvidenceError("case ID is outside the fixed corpus")
        expected = definitions[case_id]
        if row["group"] != expected.group or row["query"] != expected.query:
            raise EvidenceError(f"fixed case identity changed: {case_id}")
        if expected.group == "L":
            if row["target_prepack_tokens"] != expected.target_prepack_tokens:
                raise EvidenceError(f"boundary target changed: {case_id}")
        elif (
            row["document"] != expected.document
            or row["target_prepack_tokens"] is not None
        ):
            raise EvidenceError(f"fixed case content changed: {case_id}")
    actual_hash = sha256_bytes(
        canonical_json_bytes(list(cases)), domain=b"querit-replay-corpus-v1\0"
    )
    if actual_hash != manifest["corpus_sha256"]:
        raise EvidenceError("corpus hash does not match cases")


def _validate_schedule(
    manifest: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]
) -> None:
    schedule = replay_schedule()
    if manifest["schedule_sha256"] != schedule_sha256(schedule):
        raise EvidenceError("fixed schedule hash changed")
    offset = 0
    for batch in schedule:
        rows = observations[offset : offset + len(batch.case_ids)]
        if len(rows) != len(batch.case_ids):
            raise EvidenceError("schedule ended early")
        for position, (row, case_id) in enumerate(zip(rows, batch.case_ids, strict=True)):
            expected = {
                "batch_id": batch.batch_id,
                "batch_size": len(batch.case_ids),
                "case_id": case_id,
                "observation_id": f"qro-{offset + position:04d}",
                "permutation": batch.permutation,
                "phase": batch.phase,
                "repetition": batch.repetition,
                "row_position": position,
                "schedule_id": batch.batch_id,
                "track": batch.track,
            }
            if any(row.get(key) != value for key, value in expected.items()):
                raise EvidenceError(f"observation disagrees with schedule: {batch.batch_id}")
        offset += len(rows)
    if offset != len(observations):
        raise EvidenceError("schedule has trailing observations")


def _head_related(value: Any) -> bool:
    if isinstance(value, str):
        return value == "head" or value.startswith("head.")
    if isinstance(value, Mapping):
        return any(_head_related(item) for item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_head_related(item) for item in value)
    return False


def validate_replay_evidence(
    manifest: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    encodings: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Recompute fixed plan, tolerances, formulas, and every selection gate."""

    _validate_corpus(manifest, cases)
    _validate_schedule(manifest, observations)
    calibrated = calibrate_tolerances(observations)
    if manifest["tolerances"] != calibrated:
        raise EvidenceError("persisted tolerances were not derived from calibration evidence")
    tolerances = {name: row["value"] for name, row in calibrated.items()}
    gates = {name: "PASS" for name in REQUIRED_PASS_GATES}
    errors: list[dict[str, str]] = []

    def fail(gate: str, detail: str) -> None:
        gates[gate] = "FAIL"
        if len(errors) < MAX_ERROR_COUNT:
            errors.append({"error": detail, "gate": gate})

    report = manifest.get("classifier_load_report", {})
    report_fields = (
        "missing_keys",
        "unexpected_keys",
        "mismatched_keys",
        "reinitialized_keys",
        "error_msgs",
    )
    if any(field not in report or _head_related(report[field]) for field in report_fields):
        fail("head_attestation", "classifier head load report is incomplete or hostile")

    encoding_map = {(row["case_id"], row["track"]): row for row in encodings}
    for row in cases:
        if row["group"] == "L":
            target = row["target_prepack_tokens"]
            for track in (LEGACY_CONTRACT, CANDIDATE_CONTRACT):
                if encoding_map[(row["case_id"], track)]["pre_truncation_token_count"] != target:
                    fail("boundary_exact", f"boundary mismatch: {row['case_id']}:{track}")

    candidate_rows = [row for row in observations if row["track"] == CANDIDATE_CONTRACT]
    baselines: dict[str, dict[str, float]] = {}
    fields = (
        ("logit_0", "logits"),
        ("logit_1", "logits"),
        ("probability_0", "probabilities"),
        ("probability_1", "probabilities"),
        ("native_score", "score"),
    )
    for row in candidate_rows:
        if row["batch_size"] == 1:
            baselines.setdefault(
                str(row["case_id"]), {field: _cell(row, field) for field, _ in fields}
            )
    for row in observations:
        logit_0, logit_1 = _cell(row, "logit_0"), _cell(row, "logit_1")
        maximum = max(logit_0, logit_1)
        exp_0, exp_1 = math.exp(logit_0 - maximum), math.exp(logit_1 - maximum)
        expected_p0, expected_p1 = exp_0 / (exp_0 + exp_1), exp_1 / (exp_0 + exp_1)
        p0, p1 = _cell(row, "probability_0"), _cell(row, "probability_1")
        native, recomputed = _cell(row, "native_score"), _cell(row, "recomputed_score")
        if (
            abs(p0 - expected_p0) > tolerances["probabilities"]
            or abs(p1 - expected_p1) > tolerances["probabilities"]
            or abs(native - (p1 - p0)) > tolerances["score"]
            or abs(recomputed - (p1 - p0)) > tolerances["score"]
        ):
            fail("native_formula", f"formula mismatch: {row['observation_id']}")
        if row["track"] == LEGACY_CONTRACT and abs(
            _cell(row, "legacy_opaque_score") - native
        ) > tolerances["score"]:
            fail("legacy_direct_parity", f"legacy parity drift: {row['observation_id']}")

    for row in candidate_rows:
        baseline = baselines[str(row["case_id"])]
        drifted = {
            scalar
            for field, scalar in fields
            if abs(_cell(row, field) - baseline[field]) > tolerances[scalar]
        }
        if row["phase"] == "w4-calibration" and row["batch_size"] == 1 and drifted:
            fail("candidate_repeat_invariance", f"candidate repeat drift: {row['observation_id']}")
        if row["batch_size"] > 1 and drifted:
            fail("candidate_batch_invariance", f"candidate batch drift: {row['observation_id']}")
        if row["phase"] in ("w4-all-permutations", "b8-mixed-permutations") and drifted:
            fail("candidate_permutation_invariance", f"candidate permutation drift: {row['observation_id']}")

    for track in (LEGACY_CONTRACT, CANDIDATE_CONTRACT):
        if encoding_map[("B07", track)]["input_ids_sha256"] != encoding_map[("B08", track)]["input_ids_sha256"]:
            fail("duplicate_equality", f"duplicate token hashes differ: {track}")
    if any(
        abs(baselines["B07"][field] - baselines["B08"][field]) > tolerances[scalar]
        for field, scalar in fields
    ):
        fail("duplicate_equality", "duplicate singleton outputs differ")

    by_batch: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in candidate_rows:
        by_batch[str(row["batch_id"])].append(row)
    for batch_id, rows in by_batch.items():
        baseline_scores = [baselines[str(row["case_id"])]["native_score"] for row in rows]
        observed_scores = [_cell(row, "native_score") for row in rows]
        if not _pairwise_rank_preserved(baseline_scores, observed_scores, tolerances["score"]):
            fail("rank_preservation", f"rank reversal: {batch_id}")
        duplicate = {str(row["case_id"]): row for row in rows if row["case_id"] in ("B07", "B08")}
        if set(duplicate) == {"B07", "B08"} and any(
            abs(_cell(duplicate["B07"], field) - _cell(duplicate["B08"], field)) > tolerances[scalar]
            for field, scalar in fields
        ):
            fail("duplicate_equality", f"duplicate batch outputs differ: {batch_id}")
    return gates, errors
