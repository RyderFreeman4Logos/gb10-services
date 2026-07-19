#!/usr/bin/env python3
"""Run the sealed aggregate-only legacy-18013 versus canary-18014 experiment.

This runner deliberately uses each service's native HTTP contract.  It retains
queries, documents, endpoint URLs, and response bytes only long enough to
normalize a single request; the public receipt is aggregate-only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import platform
import random
import re
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from reranker_equivalence_metrics import QueryGroup, compute_endpoint_metrics, load_corpus, rank_indices
from reranker_equivalence_wire import CorpusValidationError, DEEPINFRA_MODEL_VERSION, ENDPOINT_PATH, ResponseValidationError, canonical_payload, validate_response
from querit_deepinfra_adapter import PUBLIC_PATH as ADAPTER_PUBLIC_PATH
from querit_deepinfra_adapter import PUBLIC_VERSION as ADAPTER_PUBLIC_VERSION


__all__ = [
    "Attempt",
    "HarnessError",
    "HttpResponse",
    "NormalizationError",
    "PlanError",
    "PlannedAttempt",
    "ReceiptError",
    "SplitGroup",
    "aggregate_results",
    "assert_receipt_private",
    "auxiliary_schedule",
    "build_receipt",
    "canonical_json_bytes",
    "dry_run_receipt",
    "execute_schedule",
    "exit_code_for",
    "load_plan",
    "main",
    "main_schedule",
    "normalize_candidate_response",
    "normalize_legacy_response",
    "paired_bootstrap_lower_bound",
    "run",
    "schedule_sha256",
    "split_groups",
    "urllib_transport",
    "validate_corpus",
    "validate_plan",
    "validate_receipt",
    "warm_schedule",
    "write_owner_only_receipt",
]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "data" / "reranker-equivalence" / "miracl-reranking-en-zh-dev.jsonl"
DEFAULT_PLAN = ROOT / "data" / "reranker-equivalence" / "querit-legacy-canary-equivalence-plan-v2.json"
LEGACY_PATH = "/v1/rerank"
DEFAULT_LEGACY_URL = "http://127.0.0.1:18013"
DEFAULT_CANDIDATE_URL = "http://127.0.0.1:18014"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
EXIT_PASS = 0
EXIT_FAIL = 2
EXIT_INCONCLUSIVE = 3


class HarnessError(RuntimeError):
    """A non-sensitive validation or experiment failure."""


class PlanError(HarnessError):
    """The sealed source plan has drifted or is malformed."""


class ReceiptError(HarnessError):
    """A receipt would be unsafe or malformed."""


class NormalizationError(HarnessError):
    """A native response cannot be used as aggregate evidence."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


Transport = Callable[[str, str, bytes, float], HttpResponse]


@dataclass(frozen=True)
class SplitGroup:
    group: QueryGroup
    corpus_position: int
    identity_digest: str


@dataclass(frozen=True)
class PlannedAttempt:
    phase: str
    endpoint: str
    group: SplitGroup
    concurrency: int
    ordinal: int


@dataclass(frozen=True)
class Attempt:
    plan: PlannedAttempt
    duration_seconds: float
    failure_class: str | None
    scores: tuple[float, ...] | None
    started_seconds: float = 0.0
    finished_seconds: float = 0.0


def canonical_json_bytes(value: object) -> bytes:
    """Serialize strict JSON deterministically without exposing non-finite data."""

    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise HarnessError("value is not canonical JSON") from exc


def _strict_object(body: bytes, label: str) -> dict[str, object]:
    def duplicate_rejector(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError("non-finite constant")

    try:
        value = json.loads(
            body,
            object_pairs_hook=duplicate_rejector,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise NormalizationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise NormalizationError(f"{label} must be an object")
    return value


def _require_exact_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise PlanError(f"{label} fields are not exact")
    return value


def _require_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PlanError(f"{label} is invalid")
    return value


def _require_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{label} is invalid")
    number = float(value)
    if not math.isfinite(number):
        raise PlanError(f"{label} is not finite")
    return number


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PlanError(f"{label} is not a lowercase SHA-256")
    return value


def validate_plan(plan: object) -> dict[str, object]:
    """Reject unknown fields and malformed threshold boundaries in the sealed plan."""

    root = _require_exact_keys(
        plan,
        {
            "bootstrap",
            "corpus",
            "endpoints",
            "privacy",
            "schedule",
            "schema",
            "split",
            "thresholds",
            "verdict",
        },
        "plan",
    )
    if root["schema"] != "querit-legacy-canary-equivalence-plan-v2":
        raise PlanError("plan schema differs from the sealed version")

    corpus = _require_exact_keys(
        root["corpus"],
        {
            "candidates_per_group",
            "groups",
            "groups_per_language",
            "languages",
            "path",
            "sha256",
        },
        "corpus",
    )
    if (
        corpus["path"] != "data/reranker-equivalence/miracl-reranking-en-zh-dev.jsonl"
        or corpus["languages"] != ["en", "zh"]
        or _require_int(corpus["groups"], "corpus groups", minimum=1) != 200
        or _require_int(corpus["groups_per_language"], "groups per language", minimum=1)
        != 100
        or _require_int(corpus["candidates_per_group"], "candidates per group", minimum=1)
        != 10
    ):
        raise PlanError("corpus identity or cardinality differs from the sealed plan")
    _require_sha256(corpus["sha256"], "corpus SHA-256")

    split = _require_exact_keys(
        root["split"],
        {"calibration_per_language", "evaluation_per_language", "identity_domain", "identity_fields"},
        "split",
    )
    if (
        split["identity_domain"] != "reranker-legacy-canary-v2"
        or split["identity_fields"] != ["source_language", "query_id"]
        or _require_int(split["calibration_per_language"], "calibration size", minimum=1)
        != 20
        or _require_int(split["evaluation_per_language"], "evaluation size", minimum=1)
        != 80
    ):
        raise PlanError("split differs from the sealed plan")

    schedule = _require_exact_keys(
        root["schedule"],
        {
            "cold_attempts_per_endpoint",
            "excluded_warmups_per_endpoint",
            "main_attempts_per_endpoint",
            "retries",
            "timeout_seconds",
            "warm_calibration_groups_per_endpoint",
            "warm_concurrencies",
        },
        "schedule",
    )
    if (
        _require_int(schedule["cold_attempts_per_endpoint"], "cold attempts") != 1
        or _require_int(schedule["excluded_warmups_per_endpoint"], "warmups") != 1
        or _require_int(schedule["main_attempts_per_endpoint"], "main attempts") != 200
        or _require_int(schedule["retries"], "retries") != 0
        or _require_number(schedule["timeout_seconds"], "timeout") != 120.0
        or _require_int(schedule["warm_calibration_groups_per_endpoint"], "warm groups")
        != 40
        or schedule["warm_concurrencies"] != [1, 2, 4, 8]
    ):
        raise PlanError("operational schedule differs from the sealed plan")

    bootstrap = _require_exact_keys(
        root["bootstrap"], {"confidence", "method", "resamples", "seed"}, "bootstrap"
    )
    if (
        _require_number(bootstrap["confidence"], "bootstrap confidence") != 0.95
        or bootstrap["method"] != "paired_group_mean_one_sided_lower_percentile"
        or _require_int(bootstrap["resamples"], "bootstrap resamples", minimum=1)
        != 10_000
        or _require_int(bootstrap["seed"], "bootstrap seed", minimum=0) != 1_801_318_014
    ):
        raise PlanError("bootstrap settings differ from the sealed plan")

    endpoints = _require_exact_keys(root["endpoints"], {"candidate", "legacy"}, "endpoints")
    legacy = _require_exact_keys(endpoints["legacy"], {"path", "score_domain"}, "legacy endpoint")
    candidate = _require_exact_keys(
        endpoints["candidate"], {"model", "path", "public_version", "score_domain"}, "candidate endpoint"
    )
    if (
        legacy["path"] != LEGACY_PATH
        or legacy["score_domain"] != [-1.0, 1.0]
        or candidate["path"] != ENDPOINT_PATH
        or candidate["path"] != ADAPTER_PUBLIC_PATH
        or candidate["model"] != "Qwen/Qwen3-Reranker-8B"
        or candidate["public_version"] != DEEPINFRA_MODEL_VERSION
        or candidate["public_version"] != ADAPTER_PUBLIC_VERSION
        or candidate["score_domain"] != [0.0, 1.0]
    ):
        raise PlanError("endpoint contract differs from committed source")

    thresholds = _require_exact_keys(
        root["thresholds"], {"latency", "qrels_quality_noninferiority", "ranking"}, "thresholds"
    )
    ranking = _require_exact_keys(thresholds["ranking"], {"overall", "per_language"}, "ranking thresholds")
    for name, expected in (
        ("overall", {"mean_spearman_min": 0.90, "top1_agreement_min": 0.90, "top3_overlap_min": 0.85}),
        ("per_language", {"mean_spearman_min": 0.85, "top1_agreement_min": 0.85, "top3_overlap_min": 0.80}),
    ):
        section = _require_exact_keys(ranking[name], set(expected), f"ranking {name}")
        if any(_require_number(section[key], f"ranking {name} {key}") != value for key, value in expected.items()):
            raise PlanError("ranking threshold differs from the sealed plan")
    qrels = _require_exact_keys(
        thresholds["qrels_quality_noninferiority"],
        {"overall_lower_bound_min", "per_language_lower_bound_min"},
        "qrels thresholds",
    )
    if (
        _require_number(qrels["overall_lower_bound_min"], "overall qrels bound") != -0.02
        or _require_number(qrels["per_language_lower_bound_min"], "language qrels bound") != -0.04
    ):
        raise PlanError("qrels threshold differs from the sealed plan")
    latency = _require_exact_keys(
        thresholds["latency"],
        {"candidate_legacy_p95_ratio_max", "candidate_p99_less_than_timeout"},
        "latency thresholds",
    )
    ratios = _require_exact_keys(latency["candidate_legacy_p95_ratio_max"], {"1", "2"}, "latency ratio thresholds")
    if (
        _require_number(ratios["1"], "concurrency-one ratio") != 2.0
        or _require_number(ratios["2"], "concurrency-two ratio") != 2.0
        or latency["candidate_p99_less_than_timeout"] is not True
    ):
        raise PlanError("latency threshold differs from the sealed plan")

    verdict = _require_exact_keys(
        root["verdict"],
        {
            "behavioral_pass_requires",
            "canary_pass_requires",
            "score_interchangeability",
            "wire_drop_in_compatibility",
        },
        "verdict",
    )
    if (
        verdict["behavioral_pass_requires"]
        != ["native_api_availability", "ranking_behavior", "qrels_quality_noninferiority", "reliability"]
        or verdict["canary_pass_requires"] != ["behavioral_usability", "latency_usability"]
        or verdict["wire_drop_in_compatibility"] != "NOT_CLAIMED_CONTRACTS_DIFFER"
        or verdict["score_interchangeability"] != "NOT_CLAIMED_EXPECTED_NONIDENTICAL"
    ):
        raise PlanError("verdict composition differs from the sealed plan")

    privacy = _require_exact_keys(root["privacy"], {"forbidden_key_fragments", "receipt_schema"}, "privacy")
    if (
        privacy["receipt_schema"] != "querit-legacy-canary-equivalence-receipt-v2"
        or privacy["forbidden_key_fragments"]
        != ["case", "credential", "document", "endpoint", "environment", "header", "host", "ip", "path", "payload", "query", "response", "url"]
    ):
        raise PlanError("privacy schema differs from the sealed plan")
    return root


def load_plan(path: Path = DEFAULT_PLAN) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanError("sealed plan cannot be read") from exc
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise PlanError("sealed plan must have exactly one terminal newline")
    plan = _strict_object(raw[:-1], "sealed plan")
    if canonical_json_bytes(plan) != raw[:-1]:
        raise PlanError("sealed plan is not canonical JSON")
    return validate_plan(plan), hashlib.sha256(raw).hexdigest()


def _digest_identity(language: str, query_id: str, domain: str) -> str:
    if not language or not query_id:
        raise HarnessError("corpus identity cannot be empty")
    return hashlib.sha256(
        (domain + "\0" + language + "\0" + query_id).encode("utf-8")
    ).hexdigest()


def validate_corpus(path: Path, plan: Mapping[str, object]) -> list[QueryGroup]:
    try:
        raw = path.read_bytes()
        groups = load_corpus(path)
    except (OSError, ValueError, CorpusValidationError) as exc:
        raise HarnessError("committed corpus cannot be loaded") from exc
    corpus = plan["corpus"]
    assert isinstance(corpus, dict)
    if hashlib.sha256(raw).hexdigest() != corpus["sha256"]:
        raise HarnessError("committed corpus hash differs from the sealed plan")
    languages = corpus["languages"]
    assert isinstance(languages, list)
    if len(groups) != corpus["groups"]:
        raise HarnessError("committed corpus group count differs from the sealed plan")
    by_language = Counter(group.source_language for group in groups)
    if dict(sorted(by_language.items())) != {language: corpus["groups_per_language"] for language in languages}:
        raise HarnessError("committed corpus language counts differ from the sealed plan")
    if any(len(group.candidates) != corpus["candidates_per_group"] for group in groups):
        raise HarnessError("committed corpus candidate count differs from the sealed plan")
    # Bare query IDs intentionally are not unique across language partitions.
    if len({(group.source_language, group.query_id) for group in groups}) != len(groups):
        raise HarnessError("committed corpus language/query identities are not unique")
    return groups


def split_groups(groups: Sequence[QueryGroup], plan: Mapping[str, object]) -> tuple[list[SplitGroup], list[SplitGroup]]:
    split = plan["split"]
    corpus = plan["corpus"]
    assert isinstance(split, dict) and isinstance(corpus, dict)
    per_language: dict[str, list[SplitGroup]] = defaultdict(list)
    for position, group in enumerate(groups):
        digest = _digest_identity(group.source_language, group.query_id, str(split["identity_domain"]))
        per_language[group.source_language].append(SplitGroup(group, position, digest))
    calibration: list[SplitGroup] = []
    evaluation: list[SplitGroup] = []
    for language in corpus["languages"]:
        assert isinstance(language, str)
        ordered = sorted(per_language[language], key=lambda item: (item.identity_digest, item.corpus_position))
        cut = int(split["calibration_per_language"])
        if len(ordered) != cut + int(split["evaluation_per_language"]):
            raise HarnessError("corpus split cardinality differs from the sealed plan")
        calibration.extend(ordered[:cut])
        evaluation.extend(ordered[cut:])
    return calibration, evaluation


def _endpoint_order(ordinal: int) -> tuple[str, str]:
    return ("legacy", "candidate") if ordinal % 2 == 0 else ("candidate", "legacy")


def main_schedule(groups: Sequence[SplitGroup]) -> list[PlannedAttempt]:
    schedule: list[PlannedAttempt] = []
    for ordinal, group in enumerate(groups):
        for endpoint in _endpoint_order(ordinal):
            schedule.append(PlannedAttempt("main", endpoint, group, 1, ordinal))
    return schedule


def warm_schedule(groups: Sequence[SplitGroup], concurrency: int) -> list[PlannedAttempt]:
    if concurrency not in {1, 2, 4, 8}:
        raise HarnessError("warm concurrency is not sealed")
    schedule: list[PlannedAttempt] = []
    for ordinal, group in enumerate(groups):
        for endpoint in _endpoint_order(ordinal):
            schedule.append(PlannedAttempt("warm", endpoint, group, concurrency, ordinal))
    return schedule


def auxiliary_schedule(calibration: Sequence[SplitGroup]) -> list[PlannedAttempt]:
    if len(calibration) != 40:
        raise HarnessError("sealed calibration schedule is incomplete")
    cold = calibration[0]
    warmup = calibration[-1]
    return [
        PlannedAttempt("cold", endpoint, cold, 1, ordinal)
        for ordinal, endpoint in enumerate(_endpoint_order(0))
    ] + [
        PlannedAttempt("warmup", endpoint, warmup, 1, ordinal)
        for ordinal, endpoint in enumerate(_endpoint_order(1))
    ]


def schedule_sha256(schedule: Sequence[PlannedAttempt]) -> str:
    # The digest binds identities in memory without disclosing per-case identity hashes.
    serial = [
        {
            "concurrency": row.concurrency,
            "endpoint": row.endpoint,
            "group_digest": row.group.identity_digest,
            "ordinal": row.ordinal,
            "phase": row.phase,
        }
        for row in schedule
    ]
    return hashlib.sha256(b"querit-legacy-canary-schedule-v2\0" + canonical_json_bytes(serial)).hexdigest()


def _legacy_request(group: QueryGroup) -> bytes:
    return canonical_json_bytes(
        {"documents": [candidate.document for candidate in group.candidates], "query": group.query, "top_n": 10}
    )


def _candidate_request(group: QueryGroup) -> bytes:
    return canonical_payload(
        [group.query] * len(group.candidates),
        [candidate.document for candidate in group.candidates],
    )


def normalize_legacy_response(body: bytes, expected_count: int = 10) -> tuple[float, ...]:
    """Accept only aliases emitted by the committed legacy server and restore input order."""

    payload = _strict_object(body, "legacy response")
    allowed_top = {"id", "model", "results", "data"}
    if not set(payload).issubset(allowed_top) or not ({"results", "data"} & set(payload)):
        raise NormalizationError("legacy response fields are unsupported")
    for field in ("id", "model"):
        if field in payload and (not isinstance(payload[field], str) or not payload[field]):
            raise NormalizationError("legacy response metadata is unsupported")

    def parse_rows(rows: object) -> tuple[float, ...]:
        if not isinstance(rows, list) or len(rows) != expected_count:
            raise NormalizationError("legacy response cardinality differs from request")
        restored: list[float | None] = [None] * expected_count
        for row in rows:
            if not isinstance(row, dict) or not row or not set(row).issubset(
                {"index", "document_index", "score", "relevance_score"}
            ):
                raise NormalizationError("legacy result shape is unsupported")
            indices = [row[name] for name in ("index", "document_index") if name in row]
            scores = [row[name] for name in ("score", "relevance_score") if name in row]
            if not indices or not scores or any(
                isinstance(value, bool) or not isinstance(value, int) for value in indices
            ):
                raise NormalizationError("legacy result index is invalid")
            if len(set(indices)) != 1 or not 0 <= indices[0] < expected_count:
                raise NormalizationError("legacy result index is missing, duplicate, or out of range")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in scores
            ):
                raise NormalizationError("legacy result score is non-finite")
            normalized = float(scores[0])
            if len({float(value) for value in scores}) != 1 or not -1.0 <= normalized <= 1.0:
                raise NormalizationError("legacy result score is outside the signed domain")
            index = indices[0]
            if restored[index] is not None:
                raise NormalizationError("legacy result index is duplicated")
            restored[index] = normalized
        if any(value is None for value in restored):
            raise NormalizationError("legacy response is missing an input position")
        return tuple(float(value) for value in restored)

    parsed = [parse_rows(payload[name]) for name in ("results", "data") if name in payload]
    if len(parsed) == 2 and parsed[0] != parsed[1]:
        raise NormalizationError("legacy response aliases disagree")
    return parsed[0]


def normalize_candidate_response(body: bytes, expected_count: int = 10) -> tuple[float, ...]:
    """Validate public positional scores and normalize [0, 1] to signed space."""

    _strict_object(body, "candidate response")
    try:
        response = validate_response(body, expected_count)
    except (ResponseValidationError, ValueError) as exc:
        raise NormalizationError(str(exc)) from exc
    return tuple(2.0 * score - 1.0 for score in response.scores)


def _safe_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise HarnessError("endpoint URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HarnessError("endpoint URL is invalid")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _url_for(base_url: str, path: str, query: str = "") -> str:
    parsed = urllib.parse.urlsplit(_safe_base_url(base_url))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def urllib_transport(base_url: str, path: str, body: bytes, timeout: float) -> HttpResponse:
    """Use no authorization and retain raw bytes only until normalizing the attempt."""

    query = ""
    if path == ENDPOINT_PATH:
        query = urllib.parse.urlencode((("version", DEEPINFRA_MODEL_VERSION),))
    request = urllib.request.Request(
        _url_for(base_url, path, query),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - explicit operator URL
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise OSError("response limit exceeded")
            return HttpResponse(int(response.status), data)
    except urllib.error.HTTPError as exc:
        data = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            data = b""
        return HttpResponse(int(exc.code), data)


def _attempt(plan: PlannedAttempt, *, legacy_url: str, candidate_url: str, timeout: float, transport: Transport) -> Attempt:
    group = plan.group.group
    started = time.monotonic()
    try:
        if plan.endpoint == "legacy":
            response = transport(legacy_url, LEGACY_PATH, _legacy_request(group), timeout)
            normalizer = normalize_legacy_response
        elif plan.endpoint == "candidate":
            response = transport(candidate_url, ENDPOINT_PATH, _candidate_request(group), timeout)
            normalizer = normalize_candidate_response
        else:  # pragma: no cover - constructed schedules are exhaustive
            raise HarnessError("unknown planned endpoint")
        elapsed = time.monotonic() - started
        if not 200 <= response.status < 300:
            return Attempt(plan, elapsed, "http", None, started, time.monotonic())
        try:
            return Attempt(plan, elapsed, None, normalizer(response.body, len(group.candidates)), started, time.monotonic())
        except NormalizationError as exc:
            message = str(exc).lower()
            if "cardinality" in message:
                failure = "cardinality"
            elif "index" in message or "position" in message or "missing" in message or "duplicate" in message:
                failure = "index"
            elif "non-finite" in message or "finite" in message:
                failure = "nonfinite"
            elif "domain" in message or "outside" in message:
                failure = "domain"
            else:
                failure = "schema"
            return Attempt(plan, elapsed, failure, None, started, time.monotonic())
    except (TimeoutError, socket.timeout):
        finished = time.monotonic()
        return Attempt(plan, finished - started, "timeout", None, started, finished)
    except urllib.error.URLError as exc:
        finished = time.monotonic()
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return Attempt(plan, finished - started, "timeout", None, started, finished)
        return Attempt(plan, finished - started, "transport", None, started, finished)
    except OSError:
        finished = time.monotonic()
        return Attempt(plan, finished - started, "transport", None, started, finished)
    except Exception:  # noqa: BLE001 - never disclose exception or request content
        finished = time.monotonic()
        return Attempt(plan, finished - started, "transport", None, started, finished)


def execute_schedule(
    schedule: Sequence[PlannedAttempt],
    *,
    concurrency: int,
    legacy_url: str,
    candidate_url: str,
    timeout: float,
    transport: Transport,
) -> list[Attempt]:
    if concurrency < 1:
        raise HarnessError("concurrency must be positive")
    invoke = lambda row: _attempt(  # noqa: E731 - keeps executor mapping private and local
        row,
        legacy_url=legacy_url,
        candidate_url=candidate_url,
        timeout=timeout,
        transport=transport,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        return list(executor.map(invoke, schedule))


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _spearman(left: Sequence[int], right: Sequence[int]) -> float:
    if len(left) != len(right) or set(left) != set(right):
        raise HarnessError("rankings are incomparable")
    count = len(left)
    if count < 2:
        return 1.0
    positions = {value: index for index, value in enumerate(right)}
    distance = sum((index - positions[value]) ** 2 for index, value in enumerate(left))
    return 1.0 - 6.0 * distance / (count * (count * count - 1))


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean = _mean(left)
    right_mean = _mean(right)
    assert left_mean is not None and right_mean is not None
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    if denominator == 0:
        return 1.0 if list(left) == list(right) else 0.0
    return sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)) / denominator


def _attempt_key(attempt: Attempt) -> tuple[str, int, str, str, str]:
    group = attempt.plan.group.group
    return (
        attempt.plan.phase,
        attempt.plan.concurrency,
        group.source_language,
        group.query_id,
        attempt.plan.endpoint,
    )


def paired_bootstrap_lower_bound(deltas: Sequence[float], *, seed: int, resamples: int) -> dict[str, object]:
    if not deltas:
        return {"groups": 0, "lower_bound": None, "mean_delta": None, "resamples": resamples}
    if any(not math.isfinite(value) for value in deltas):
        raise HarnessError("bootstrap values must be finite")
    generator = random.Random(seed)
    count = len(deltas)
    means = sorted(
        sum(deltas[generator.randrange(count)] for _ in range(count)) / count
        for _ in range(resamples)
    )
    return {
        "groups": count,
        "lower_bound": means[math.ceil(0.05 * resamples) - 1],
        "mean_delta": sum(deltas) / count,
        "resamples": resamples,
    }


def _wilson(failures: int, attempts: int) -> dict[str, float | int]:
    if attempts <= 0 or not 0 <= failures <= attempts:
        raise HarnessError("Wilson inputs are invalid")
    z = 1.959963984540054
    rate = failures / attempts
    denominator = 1.0 + z * z / attempts
    centre = (rate + z * z / (2.0 * attempts)) / denominator
    spread = z * math.sqrt(rate * (1.0 - rate) / attempts + z * z / (4.0 * attempts * attempts)) / denominator
    return {"attempts": attempts, "failures": failures, "lower": max(0.0, centre - spread), "rate": rate, "upper": min(1.0, centre + spread)}


def _group_quality(group: QueryGroup, scores: Sequence[float]) -> dict[str, float]:
    metric = compute_endpoint_metrics([group], scores)["aggregate"]
    assert isinstance(metric, dict)
    return {name: float(metric[name]) for name in ("mrr_at_10", "ndcg_at_10", "map_at_10")}


def _verdict_for_ranking(metrics: Mapping[str, object], complete: bool, thresholds: Mapping[str, object]) -> str:
    if not complete:
        return INCONCLUSIVE
    overall = metrics["overall"]
    languages = metrics["per_language"]
    assert isinstance(overall, dict) and isinstance(languages, dict)
    overall_thresholds = thresholds["overall"]
    language_thresholds = thresholds["per_language"]
    assert isinstance(overall_thresholds, dict) and isinstance(language_thresholds, dict)
    checks = [
        float(overall["top1_agreement"]) >= float(overall_thresholds["top1_agreement_min"]),
        float(overall["mean_spearman"]) >= float(overall_thresholds["mean_spearman_min"]),
        float(overall["top3_overlap"]) >= float(overall_thresholds["top3_overlap_min"]),
    ]
    for metrics_for_language in languages.values():
        assert isinstance(metrics_for_language, dict)
        checks.extend(
            [
                float(metrics_for_language["top1_agreement"]) >= float(language_thresholds["top1_agreement_min"]),
                float(metrics_for_language["mean_spearman"]) >= float(language_thresholds["mean_spearman_min"]),
                float(metrics_for_language["top3_overlap"]) >= float(language_thresholds["top3_overlap_min"]),
            ]
        )
    return PASS if all(checks) else FAIL


def _verdict_for_qrels(metrics: Mapping[str, object], complete: bool, thresholds: Mapping[str, object]) -> str:
    if not complete:
        return INCONCLUSIVE
    overall_threshold = float(thresholds["overall_lower_bound_min"])
    language_threshold = float(thresholds["per_language_lower_bound_min"])
    checks: list[bool] = []
    for scope, values in metrics.items():
        assert isinstance(values, dict)
        required = overall_threshold if scope == "overall" else language_threshold
        for metric in ("mrr_at_10", "ndcg_at_10", "map_at_10"):
            row = values[metric]
            assert isinstance(row, dict)
            lower_bound = row["lower_bound"]
            checks.append(lower_bound is not None and float(lower_bound) >= required)
    return PASS if all(checks) else FAIL


def _compose(required: Iterable[str], verdicts: Mapping[str, str]) -> str:
    values = [verdicts[name] for name in required]
    if all(value == PASS for value in values):
        return PASS
    if any(value == FAIL for value in values):
        return FAIL
    return INCONCLUSIVE


def aggregate_results(
    attempts: Sequence[Attempt],
    *,
    main_groups: Sequence[SplitGroup],
    evaluation: Sequence[SplitGroup],
    plan: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """Aggregate all evidence without exposing a per-group result or identity."""

    schedule = plan["schedule"]
    thresholds = plan["thresholds"]
    bootstrap = plan["bootstrap"]
    verdict_plan = plan["verdict"]
    assert isinstance(schedule, dict) and isinstance(thresholds, dict) and isinstance(bootstrap, dict) and isinstance(verdict_plan, dict)
    indexed = {_attempt_key(attempt): attempt for attempt in attempts}
    expected_main = len(main_groups) * 2
    expected_warm = 40 * 2 * len(schedule["warm_concurrencies"])
    expected_total = expected_main + expected_warm + 4
    complete_schedule = len(attempts) == expected_total and len(indexed) == len(attempts)

    def failure_summary(selected: Sequence[Attempt]) -> dict[str, object]:
        failures = Counter(attempt.failure_class for attempt in selected if attempt.failure_class is not None)
        return {
            "classes": dict(sorted(failures.items())),
            "wilson_failure_rate": _wilson(sum(failures.values()), len(selected)),
        }

    failure_rows: dict[str, object] = {}
    for endpoint in ("legacy", "candidate"):
        endpoint_attempts = [attempt for attempt in attempts if attempt.plan.endpoint == endpoint]
        failure_rows[endpoint] = {
            "all": failure_summary(endpoint_attempts),
            "cold": failure_summary([attempt for attempt in endpoint_attempts if attempt.plan.phase == "cold"]),
            "main": failure_summary([attempt for attempt in endpoint_attempts if attempt.plan.phase == "main"]),
            "warm": {
                str(concurrency): failure_summary(
                    [
                        attempt
                        for attempt in endpoint_attempts
                        if attempt.plan.phase == "warm" and attempt.plan.concurrency == concurrency
                    ]
                )
                for concurrency in schedule["warm_concurrencies"]
            },
            "warmup": failure_summary([attempt for attempt in endpoint_attempts if attempt.plan.phase == "warmup"]),
        }

    def main_attempt(group: SplitGroup, endpoint: str) -> Attempt | None:
        return indexed.get(("main", 1, group.group.source_language, group.group.query_id, endpoint))

    complete_evaluation = complete_schedule and all(
        (legacy := main_attempt(group, "legacy")) is not None
        and (candidate := main_attempt(group, "candidate")) is not None
        and legacy.failure_class is None
        and candidate.failure_class is None
        and legacy.scores is not None
        and candidate.scores is not None
        for group in evaluation
    )

    ranking_values: dict[str, list[dict[str, float]]] = defaultdict(list)
    score_legacy: list[float] = []
    score_candidate: list[float] = []
    qrels_deltas: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for group in evaluation:
        legacy = main_attempt(group, "legacy")
        candidate = main_attempt(group, "candidate")
        if legacy is None or candidate is None or legacy.scores is None or candidate.scores is None:
            continue
        legacy_rank = rank_indices(legacy.scores)
        candidate_rank = rank_indices(candidate.scores)
        row = {
            "mean_spearman": _spearman(legacy_rank, candidate_rank),
            "top1_agreement": float(legacy_rank[0] == candidate_rank[0]),
            "top3_overlap": len(set(legacy_rank[:3]) & set(candidate_rank[:3])) / 3.0,
            "top5_overlap": len(set(legacy_rank[:5]) & set(candidate_rank[:5])) / 5.0,
            "top10_overlap": 1.0,
        }
        ranking_values["overall"].append(row)
        ranking_values[group.group.source_language].append(row)
        score_legacy.extend(legacy.scores)
        score_candidate.extend(candidate.scores)
        legacy_quality = _group_quality(group.group, legacy.scores)
        candidate_quality = _group_quality(group.group, candidate.scores)
        for metric in legacy_quality:
            delta = candidate_quality[metric] - legacy_quality[metric]
            qrels_deltas["overall"][metric].append(delta)
            qrels_deltas[group.group.source_language][metric].append(delta)

    def summarize_ranking(rows: Sequence[Mapping[str, float]]) -> dict[str, float | int]:
        return {"groups": len(rows), **{name: float(_mean([row[name] for row in rows]) or 0.0) for name in ("top1_agreement", "mean_spearman", "top3_overlap", "top5_overlap", "top10_overlap")}}

    ranking_metrics: dict[str, object] = {
        "overall": summarize_ranking(ranking_values["overall"]),
        "per_language": {language: summarize_ranking(ranking_values[language]) for language in ("en", "zh")},
    }
    score_metrics: dict[str, object]
    if complete_evaluation:
        differences = [candidate - legacy for legacy, candidate in zip(score_legacy, score_candidate, strict=True)]
        legacy_mean = float(_mean(score_legacy) or 0.0)
        candidate_mean = float(_mean(score_candidate) or 0.0)
        variance = sum((value - legacy_mean) ** 2 for value in score_legacy)
        covariance = sum(
            (legacy - legacy_mean) * (candidate - candidate_mean)
            for legacy, candidate in zip(score_legacy, score_candidate, strict=True)
        )
        slope = covariance / variance if variance else None
        score_metrics = {
            "batch_permutation_delta": "NOT_MEASURED_BY_SEALED_SCHEDULE",
            "candidate_signed_domain": {"max": max(score_candidate), "min": min(score_candidate)},
            "intercept": candidate_mean - slope * legacy_mean if slope is not None else candidate_mean,
            "legacy_signed_domain": {"max": max(score_legacy), "min": min(score_legacy)},
            "mae": float(_mean([abs(value) for value in differences]) or 0.0),
            "pearson": _pearson(score_legacy, score_candidate),
            "repeat_jitter": "NOT_MEASURED_BY_SEALED_SCHEDULE",
            "rmse": math.sqrt(float(_mean([value * value for value in differences]) or 0.0)),
            "slope": slope,
            "spearman": ranking_metrics["overall"]["mean_spearman"],
        }
    else:
        score_metrics = {"status": "INCOMPLETE"}

    qrels_metrics: dict[str, object] = {}
    for scope in ("overall", "en", "zh"):
        qrels_metrics["overall" if scope == "overall" else scope] = {
            metric: paired_bootstrap_lower_bound(
                qrels_deltas[scope][metric],
                seed=int(bootstrap["seed"]),
                resamples=int(bootstrap["resamples"]),
            )
            for metric in ("mrr_at_10", "ndcg_at_10", "map_at_10")
        }
    endpoint_qrels: dict[str, object] = (
        {
            "legacy": compute_endpoint_metrics([row.group for row in evaluation], score_legacy),
            "candidate": compute_endpoint_metrics([row.group for row in evaluation], score_candidate),
        }
        if complete_evaluation
        else {"status": "INCOMPLETE"}
    )

    latency: dict[str, object] = {"cold": {}, "warm": {}}
    for phase in ("cold",):
        for endpoint in ("legacy", "candidate"):
            durations = [attempt.duration_seconds for attempt in attempts if attempt.plan.phase == phase and attempt.plan.endpoint == endpoint]
            latency[phase][endpoint] = {"attempts": len(durations), "p50": _percentile(durations, 0.50), "p95": _percentile(durations, 0.95), "p99": _percentile(durations, 0.99)}
    warm_rows: dict[str, object] = {}
    for concurrency in schedule["warm_concurrencies"]:
        row: dict[str, object] = {}
        for endpoint in ("legacy", "candidate"):
            selected = [attempt for attempt in attempts if attempt.plan.phase == "warm" and attempt.plan.concurrency == concurrency and attempt.plan.endpoint == endpoint]
            durations = [attempt.duration_seconds for attempt in selected]
            starts = [attempt.started_seconds for attempt in selected]
            finishes = [attempt.finished_seconds for attempt in selected]
            elapsed = (
                max(finishes) - min(starts)
                if starts and all(finish > start for start, finish in zip(starts, finishes, strict=True))
                else sum(durations)
            )
            row[endpoint] = {
                "attempts": len(selected),
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
                "p99": _percentile(durations, 0.99),
                "pairs_per_second": (10.0 * len(selected) / elapsed) if elapsed > 0 else None,
            }
        legacy_p95 = row["legacy"]["p95"]
        candidate_p95 = row["candidate"]["p95"]
        row["candidate_legacy_p95_ratio"] = candidate_p95 / legacy_p95 if legacy_p95 not in (None, 0.0) and candidate_p95 is not None else None
        warm_rows[str(concurrency)] = row
    latency["warm"] = warm_rows

    main_failures = [attempt for attempt in attempts if attempt.plan.phase == "main" and attempt.failure_class is not None]
    reliability_complete = complete_schedule and all(
        len([attempt for attempt in attempts if attempt.plan.phase == "warm" and attempt.plan.concurrency == concurrency and attempt.plan.endpoint == endpoint]) == 40
        for concurrency in schedule["warm_concurrencies"]
        for endpoint in ("legacy", "candidate")
    )
    reliability_checks: list[bool] = [not main_failures]
    paired_failure_difference: dict[str, object] = {}
    for concurrency in schedule["warm_concurrencies"]:
        legacy_warm = [attempt for attempt in attempts if attempt.plan.phase == "warm" and attempt.plan.concurrency == concurrency and attempt.plan.endpoint == "legacy"]
        candidate_warm = [attempt for attempt in attempts if attempt.plan.phase == "warm" and attempt.plan.concurrency == concurrency and attempt.plan.endpoint == "candidate"]
        legacy_failures = sum(attempt.failure_class is not None for attempt in legacy_warm)
        candidate_failures = sum(attempt.failure_class is not None for attempt in candidate_warm)
        candidate_hard_failures = sum(
            attempt.failure_class in {"schema", "cardinality", "index", "nonfinite", "domain"}
            for attempt in candidate_warm
        )
        if concurrency in (1, 2):
            reliability_checks.append(candidate_failures == 0 and candidate_hard_failures == 0)
        else:
            reliability_checks.append(candidate_failures <= legacy_failures and candidate_hard_failures == 0)
        candidate_by_identity = {(attempt.plan.group.group.source_language, attempt.plan.group.group.query_id): attempt for attempt in candidate_warm}
        paired = [
            float(candidate_by_identity[(attempt.plan.group.group.source_language, attempt.plan.group.group.query_id)].failure_class is not None) - float(attempt.failure_class is not None)
            for attempt in legacy_warm
            if (attempt.plan.group.group.source_language, attempt.plan.group.group.query_id) in candidate_by_identity
        ]
        paired_failure_difference[str(concurrency)] = {"pairs": len(paired), "candidate_minus_legacy": _mean(paired)}
    reliability_verdict = PASS if reliability_complete and all(reliability_checks) else (FAIL if reliability_complete else INCONCLUSIVE)

    native_verdict = PASS if complete_schedule and all(attempt.failure_class is None for attempt in attempts) else FAIL
    ranking_verdict = _verdict_for_ranking(ranking_metrics, complete_evaluation, thresholds["ranking"])
    qrels_verdict = _verdict_for_qrels(qrels_metrics, complete_evaluation, thresholds["qrels_quality_noninferiority"])
    latency_complete = reliability_complete and all(attempt.failure_class is None for attempt in attempts if attempt.plan.phase == "warm")
    latency_checks: list[bool] = []
    ratios = thresholds["latency"]["candidate_legacy_p95_ratio_max"]
    assert isinstance(ratios, dict)
    for concurrency in ("1", "2"):
        ratio = warm_rows[concurrency]["candidate_legacy_p95_ratio"]
        latency_checks.append(ratio is not None and float(ratio) <= float(ratios[concurrency]))
    candidate_p99 = warm_rows["1"]["candidate"]["p99"]
    latency_checks.append(candidate_p99 is not None and math.isfinite(float(candidate_p99)) and float(candidate_p99) < float(schedule["timeout_seconds"]))
    latency_verdict = PASS if latency_complete and all(latency_checks) else (FAIL if latency_complete else INCONCLUSIVE)

    verdicts = {
        "native_api_availability": native_verdict,
        "wire_drop_in_compatibility": str(verdict_plan["wire_drop_in_compatibility"]),
        "score_interchangeability": str(verdict_plan["score_interchangeability"]),
        "ranking_behavior": ranking_verdict,
        "qrels_quality_noninferiority": qrels_verdict,
        "reliability": reliability_verdict,
        "latency_usability": latency_verdict,
    }
    verdicts["behavioral_usability"] = _compose(verdict_plan["behavioral_pass_requires"], verdicts)
    verdicts["canary_operational_suitability"] = _compose(verdict_plan["canary_pass_requires"], verdicts)

    return (
        {
            "failure_classes": failure_rows,
            "latency": latency,
            "paired_failure_difference": paired_failure_difference,
            "qrels_quality": {
                "native_aggregates": endpoint_qrels,
                "paired_lower_bounds": qrels_metrics,
            },
            "ranking": ranking_metrics,
            "score_report": score_metrics,
        },
        verdicts,
    )


FORBIDDEN_KEY_FRAGMENTS = frozenset(
    {"case", "credential", "document", "endpoint", "environment", "header", "host", "ip", "path", "payload", "query", "response", "url"}
)
_IP_RE = re.compile(r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}(?![0-9])")
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+(?::[0-9]+)?$")


def assert_receipt_private(value: object) -> None:
    """Reject direct and indirect leakage before serializing any receipt."""

    if isinstance(value, str):
        lowered = value.lower()
        if "/" in value or "\\" in value or "://" in value or _IP_RE.search(value) or _HOST_RE.fullmatch(value) or value.lower() == "localhost" or "bearer " in lowered or re.match(r"^[A-Z_][A-Z0-9_]*=", value):
            raise ReceiptError("receipt contains a forbidden value class")
    elif isinstance(value, list):
        for item in value:
            assert_receipt_private(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or any(fragment in key.lower() for fragment in FORBIDDEN_KEY_FRAGMENTS):
                raise ReceiptError("receipt contains a forbidden field")
            assert_receipt_private(item)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ReceiptError("receipt contains an unsupported value")


def validate_receipt(receipt: object) -> dict[str, object]:
    expected = {"corpus", "identities", "metrics", "schedule", "schema", "thresholds", "verdicts"}
    if not isinstance(receipt, dict) or set(receipt) != expected:
        raise ReceiptError("receipt fields are not exact")
    assert_receipt_private(receipt)
    if receipt["schema"] != "querit-legacy-canary-equivalence-receipt-v2":
        raise ReceiptError("receipt schema is invalid")
    return receipt


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise HarnessError("source identity cannot be read") from exc


def _identity_hash(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + canonical_json_bytes(value)).hexdigest()


def build_receipt(
    *,
    plan: Mapping[str, object],
    plan_hash: str,
    corpus: Sequence[QueryGroup],
    schedules: Mapping[str, Sequence[PlannedAttempt]],
    metrics: Mapping[str, object],
    verdicts: Mapping[str, str],
    legacy_url: str,
    candidate_url: str,
) -> dict[str, object]:
    source_files = [
        Path(__file__),
        ROOT / "scripts" / "reranker_equivalence_metrics.py",
        ROOT / "scripts" / "reranker_equivalence_wire.py",
        ROOT / "scripts" / "querit_deepinfra_adapter.py",
        ROOT / "scripts" / "querit_openai_rerank_server.py",
    ]
    corpus_plan = plan["corpus"]
    schedule_plan = plan["schedule"]
    assert isinstance(corpus_plan, dict) and isinstance(schedule_plan, dict)
    receipt: dict[str, object] = {
        "corpus": {
            "groups": len(corpus),
            "languages": dict(sorted(Counter(group.source_language for group in corpus).items())),
            "pairs": sum(len(group.candidates) for group in corpus),
            "sha256": corpus_plan["sha256"],
        },
        "identities": {
            "candidate_identity_sha256": _identity_hash(b"querit-candidate-url-v2\0", _safe_base_url(candidate_url)),
            "legacy_identity_sha256": _identity_hash(b"querit-legacy-url-v2\0", _safe_base_url(legacy_url)),
            "plan_sha256": plan_hash,
            "runner_sha256": _sha256_file(Path(__file__)),
            "runtime_sha256": _identity_hash(
                b"querit-runtime-v2\0",
                {"implementation": platform.python_implementation(), "version": platform.python_version()},
            ),
            "source_sha256": _identity_hash(
                b"querit-source-v2\0",
                {path.name: _sha256_file(path) for path in source_files},
            ),
        },
        "metrics": dict(metrics),
        "schedule": {
            "attempts": {name: len(rows) for name, rows in schedules.items()},
            "hashes": {name: schedule_sha256(rows) for name, rows in schedules.items()},
            "retries": schedule_plan["retries"],
            "timeout_seconds": schedule_plan["timeout_seconds"],
        },
        "schema": "querit-legacy-canary-equivalence-receipt-v2",
        "thresholds": plan["thresholds"],
        "verdicts": dict(verdicts),
    }
    return validate_receipt(receipt)


def write_owner_only_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    """Atomically write one receipt without changing the process umask."""

    validated = validate_receipt(dict(receipt))
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ReceiptError("receipt parent is unsafe")
        os.chmod(parent, 0o700)
        temporary = parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            payload = canonical_json_bytes(validated) + b"\n"
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            temporary.unlink()  # type: ignore[has-type]
        except (FileNotFoundError, UnboundLocalError):
            pass
        raise ReceiptError("receipt cannot be written atomically") from exc


def dry_run_receipt(plan: Mapping[str, object], plan_hash: str, corpus: Sequence[QueryGroup]) -> dict[str, object]:
    calibration, evaluation = split_groups(corpus, plan)
    schedules: dict[str, list[PlannedAttempt]] = {"main": main_schedule([*calibration, *evaluation]), "auxiliary": auxiliary_schedule(calibration)}
    for concurrency in plan["schedule"]["warm_concurrencies"]:  # type: ignore[index]
        schedules[f"warm_{concurrency}"] = warm_schedule(calibration, int(concurrency))
    return {
        "corpus": {"groups": len(corpus), "pairs": sum(len(group.candidates) for group in corpus), "sha256": plan["corpus"]["sha256"]},  # type: ignore[index]
        "plan_sha256": plan_hash,
        "schedule": {"attempts": {name: len(rows) for name, rows in schedules.items()}, "hashes": {name: schedule_sha256(rows) for name, rows in schedules.items()}},
        "schema": "querit-legacy-canary-equivalence-dry-run-v2",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--legacy-url", default=DEFAULT_LEGACY_URL)
    parser.add_argument("--candidate-url", default=DEFAULT_CANDIDATE_URL)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def exit_code_for(verdicts: Mapping[str, str], attempts: Sequence[Attempt]) -> int:
    if verdicts["canary_operational_suitability"] == PASS:
        return EXIT_PASS
    if any(attempt.failure_class in {"timeout", "transport"} for attempt in attempts) or INCONCLUSIVE in verdicts.values():
        return EXIT_INCONCLUSIVE
    return EXIT_FAIL


def run(
    args: argparse.Namespace,
    *,
    transport: Transport = urllib_transport,
) -> tuple[int, dict[str, object]]:
    plan, plan_hash = load_plan(args.plan)
    corpus = validate_corpus(args.corpus, plan)
    if args.dry_run:
        return EXIT_PASS, dry_run_receipt(plan, plan_hash, corpus)
    legacy_url = _safe_base_url(args.legacy_url)
    candidate_url = _safe_base_url(args.candidate_url)
    calibration, evaluation = split_groups(corpus, plan)
    main = main_schedule([*calibration, *evaluation])
    auxiliary = auxiliary_schedule(calibration)
    attempts = execute_schedule(auxiliary, concurrency=1, legacy_url=legacy_url, candidate_url=candidate_url, timeout=float(plan["schedule"]["timeout_seconds"]), transport=transport)  # type: ignore[index]
    attempts.extend(execute_schedule(main, concurrency=1, legacy_url=legacy_url, candidate_url=candidate_url, timeout=float(plan["schedule"]["timeout_seconds"]), transport=transport))  # type: ignore[index]
    schedules: dict[str, Sequence[PlannedAttempt]] = {"auxiliary": auxiliary, "main": main}
    for concurrency in plan["schedule"]["warm_concurrencies"]:  # type: ignore[index]
        warm = warm_schedule(calibration, int(concurrency))
        schedules[f"warm_{concurrency}"] = warm
        attempts.extend(execute_schedule(warm, concurrency=int(concurrency), legacy_url=legacy_url, candidate_url=candidate_url, timeout=float(plan["schedule"]["timeout_seconds"]), transport=transport))  # type: ignore[index]
    metrics, verdicts = aggregate_results(attempts, main_groups=[*calibration, *evaluation], evaluation=evaluation, plan=plan)
    receipt = build_receipt(plan=plan, plan_hash=plan_hash, corpus=corpus, schedules=schedules, metrics=metrics, verdicts=verdicts, legacy_url=legacy_url, candidate_url=candidate_url)
    return exit_code_for(verdicts, attempts), receipt


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        exit_code, receipt = run(args)
        if args.dry_run:
            sys.stdout.buffer.write(canonical_json_bytes(receipt) + b"\n")
        elif args.output is not None:
            write_owner_only_receipt(args.output, receipt)
        else:
            sys.stdout.buffer.write(canonical_json_bytes(receipt) + b"\n")
        return exit_code
    except HarnessError:
        print("direct equivalence harness failed without emitting evidence", file=sys.stderr)
        return EXIT_INCONCLUSIVE


if __name__ == "__main__":
    raise SystemExit(main())
