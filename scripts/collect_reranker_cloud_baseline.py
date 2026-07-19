#!/usr/bin/env python3
"""Collect a reproducible cloud-baseline reranker equivalence dataset.

This script sends the fixed MIRACLReranking (en+zh) corpus to a cloud reranker
API (DeepInfra Qwen3-Reranker-8B by default) using its native inference contract,
stores each raw response alongside the request fingerprint, and tracks token
budget so the run is reproducible and auditable.

The output is a companion baseline file under ``data/reranker-equivalence/`` so
that a locally-deployed vLLM Querit-4B (behind llm-guard-proxy) can later replay
the identical requests and the two response sets can be compared deterministically.

Usage::

    DEEPINFRA_KEY=... python3 scripts/collect_reranker_cloud_baseline.py \\
        --provider deepinfra \\
        --model Qwen/Qwen3-Reranker-8B \\
        --corpus data/reranker-equivalence/miracl-reranking-en-zh-dev.jsonl \\
        --output data/reranker-equivalence/cloud-baseline-deepinfra-qwen3-reranker-8b.jsonl \\
        --budget-usd 3.00

The script never prints credentials and writes only privacy-safe metadata
(group id, language, token counts, scores, timings, request fingerprints).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reranker_equivalence_metrics import PROMPT_OVERHEAD_TOKENS_PER_PAIR
from reranker_score_validation import ScoreValidationError, validate_scores

__all__ = [
    "build_request",
    "estimate_tokens",
    "intent_path",
    "main",
    "request_fingerprint",
]

DEFAULT_PRICE_PER_MTOK = 0.05
MAX_CLOUD_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
DEFAULT_PROVIDER = "deepinfra"
DEFAULT_MODEL = "Qwen/Qwen3-Reranker-8B"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = REPO_ROOT / "data" / "reranker-equivalence" / "miracl-reranking-en-zh-dev.jsonl"


class CollectionStateError(RuntimeError):
    """The durable paid-request ledger cannot be resumed safely."""


@dataclass(frozen=True)
class ResumeState:
    completed: frozenset[str]
    charged_input_tokens: int
    terminal_failures: frozenset[str]


@dataclass(frozen=True)
class ExpectedPlanRow:
    provider: str
    model: str
    query_id: object
    source_language: object
    request_instruction: str
    pair_count: int
    candidate_document_ids: tuple[object, ...]
    candidate_relevance: tuple[object, ...]
    estimated_input_tokens: int


def _corpus_text(value: object, label: str, line_number: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise CollectionStateError(
            f"corpus JSONL row {line_number} has invalid {label}"
        )
    return value


def load_corpus(path: Path) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(
                    line,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise CollectionStateError(
                    f"corpus JSONL row {line_number} is malformed"
                ) from exc
            if not isinstance(record, dict):
                raise CollectionStateError(
                    f"corpus JSONL row {line_number} is not an object"
                )
            if "query" not in record or "candidates" not in record:
                raise CollectionStateError(
                    f"corpus JSONL row {line_number} is missing query/candidates"
                )
            _corpus_text(record["query"], "query", line_number)
            candidates = record["candidates"]
            if not isinstance(candidates, list) or not candidates:
                raise CollectionStateError(
                    f"corpus JSONL row {line_number} has invalid candidates"
                )
            for candidate in candidates:
                if not isinstance(candidate, dict) or "document" not in candidate:
                    raise CollectionStateError(
                        f"corpus JSONL row {line_number} has invalid candidate structure"
                    )
                _corpus_text(candidate["document"], "candidate document", line_number)
            groups.append(record)
    return groups


def build_request(group: dict[str, Any], instruction: str) -> dict[str, Any]:
    query = group["query"]
    candidates = group["candidates"]
    documents = [c["document"] for c in candidates]
    return {
        "queries": [query] * len(documents),
        "documents": documents,
        "instruction": instruction,
    }


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def request_fingerprint(
    group: dict[str, Any],
    request_body: dict[str, Any],
    *,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL,
) -> str:
    identity = {
        "schema": "reranker-cloud-request-v1",
        "provider": provider,
        "model": model,
        "query_id": group.get("query_id"),
        "source_language": group.get("source_language"),
        "request": request_body,
    }
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def estimate_tokens(request_body: dict[str, Any]) -> int:
    """Return the shared conservative UTF-8 byte upper bound for one request."""

    queries = request_body.get("queries")
    documents = request_body.get("documents")
    instruction = request_body.get("instruction")
    if (
        not isinstance(queries, list)
        or not isinstance(documents, list)
        or len(queries) != len(documents)
        or not queries
        or not isinstance(instruction, str)
        or any(not isinstance(value, str) for value in [*queries, *documents])
    ):
        raise ValueError("request body is invalid for token estimation")
    instruction_bytes = len(instruction.encode("utf-8"))
    return sum(
        len(query.encode("utf-8"))
        + len(document.encode("utf-8"))
        + instruction_bytes
        + PROMPT_OVERHEAD_TOKENS_PER_PAIR
        for query, document in zip(queries, documents, strict=True)
    )


def intent_path(output: Path) -> Path:
    """Return the append-only paid-request intent ledger beside the baseline."""

    return output.with_name(output.name + ".intents.jsonl")


def _lock_path(output: Path) -> Path:
    return output.with_name(output.name + ".lock")


def _acquire_output_lock(output: Path) -> int:
    """Own the output/intent pair before inspecting or spending against it."""

    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _lock_path(output)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CollectionStateError("cannot open output ownership lock") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CollectionStateError("output ownership lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise CollectionStateError(
                    "another collector owns this output and intent ledger"
                ) from exc
            raise CollectionStateError("cannot acquire output ownership lock") from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_durable(path: Path, record: dict[str, Any]) -> None:
    """Append one canonical JSONL row and make it durable before returning."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existed = path.exists()
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    payload = canonical_json(record) + b"\n"
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("durable JSONL append made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if not existed:
        _fsync_directory(path.parent)


def _load_jsonl_strict(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CollectionStateError(f"cannot read {label}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line:
            raise CollectionStateError(f"{label} contains a blank or partial row")
        try:
            row = json.loads(
                line,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise CollectionStateError(
                f"{label} row {line_number} is malformed or partial"
            ) from exc
        if not isinstance(row, dict):
            raise CollectionStateError(f"{label} row {line_number} is not an object")
        rows.append(row)
    return rows


def _load_resume_state(
    output: Path, planned: dict[str, ExpectedPlanRow]
) -> ResumeState:
    completed: set[str] = set()
    terminal_failures: set[str] = set()
    charged_input_tokens = 0
    baseline_fields = {
        "candidate_document_ids",
        "candidate_relevance",
        "charged_input_tokens",
        "cumulative_cost_usd",
        "estimated_input_tokens",
        "model",
        "pair_count",
        "provider",
        "query_id",
        "request_fingerprint",
        "request_instruction",
        "response",
        "schema",
        "source_language",
        "timing",
    }
    for row in _load_jsonl_strict(output, "cloud baseline"):
        fingerprint = row.get("request_fingerprint")
        charged = row.get("charged_input_tokens")
        estimated = row.get("estimated_input_tokens")
        pair_count = row.get("pair_count")
        cumulative_cost = row.get("cumulative_cost_usd")
        if (
            set(row) != baseline_fields
            or row.get("schema") != "reranker-cloud-baseline-v1"
            or not isinstance(fingerprint, str)
            or fingerprint not in planned
            or fingerprint in completed
            or isinstance(charged, bool)
            or not isinstance(charged, int)
            or charged < 0
            or isinstance(estimated, bool)
            or not isinstance(estimated, int)
            or estimated <= 0
            or isinstance(pair_count, bool)
            or not isinstance(pair_count, int)
            or pair_count <= 0
            or isinstance(cumulative_cost, bool)
            or not isinstance(cumulative_cost, (int, float))
            or not math.isfinite(cumulative_cost)
            or cumulative_cost < 0
            or not isinstance(row.get("provider"), str)
            or not isinstance(row.get("model"), str)
            or not isinstance(row.get("query_id"), str)
            or not isinstance(row.get("source_language"), str)
            or not isinstance(row.get("request_instruction"), str)
            or not isinstance(row.get("candidate_document_ids"), list)
            or not isinstance(row.get("candidate_relevance"), list)
            or not isinstance(row.get("response"), dict)
            or not isinstance(row.get("timing"), dict)
        ):
            raise CollectionStateError("cloud baseline resume row is inconsistent")
        expected = planned[fingerprint]
        if (
            row["provider"] != expected.provider
            or row["model"] != expected.model
            or row["query_id"] != expected.query_id
            or row["source_language"] != expected.source_language
            or row["request_instruction"] != expected.request_instruction
            or row["pair_count"] != expected.pair_count
            or row["candidate_document_ids"] != list(expected.candidate_document_ids)
            or row["candidate_relevance"] != list(expected.candidate_relevance)
            or row["estimated_input_tokens"] != expected.estimated_input_tokens
        ):
            raise CollectionStateError(
                "cloud baseline resume row does not match the requested experiment"
            )
        timing = row["timing"]
        response = row["response"]
        http_status = timing.get("http_status")
        reported_tokens = response.get("input_tokens")
        if (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or not 100 <= http_status <= 599
        ):
            raise CollectionStateError("cloud baseline HTTP status is invalid")
        token_count_invalid = reported_tokens is not None and (
            isinstance(reported_tokens, bool)
            or not isinstance(reported_tokens, int)
            or reported_tokens < 0
        )
        score_payload_invalid = False
        if http_status == 200:
            try:
                validate_scores(
                    response.get("scores"),
                    pair_count,
                    minimum=0.0,
                    maximum=1.0,
                    label="DeepInfra response scores",
                )
            except ScoreValidationError:
                score_payload_invalid = True
        completed.add(fingerprint)
        if http_status != 200 or token_count_invalid or score_payload_invalid:
            terminal_failures.add(fingerprint)
        charged_input_tokens += charged

    intents: set[str] = set()
    for row in _load_jsonl_strict(intent_path(output), "cloud request intent ledger"):
        fingerprint = row.get("request_fingerprint")
        expected = planned.get(fingerprint) if isinstance(fingerprint, str) else None
        estimated_cost = row.get("estimated_cost_usd")
        if (
            set(row)
            != {
                "estimated_cost_usd",
                "estimated_input_tokens",
                "model",
                "provider",
                "query_id",
                "request_fingerprint",
                "schema",
            }
            or row.get("schema") != "reranker-cloud-request-intent-v1"
            or not isinstance(fingerprint, str)
            or expected is None
            or fingerprint in intents
            or row.get("provider") != expected.provider
            or row.get("model") != expected.model
            or row.get("query_id") != expected.query_id
            or row.get("estimated_input_tokens") != expected.estimated_input_tokens
            or isinstance(estimated_cost, bool)
            or not isinstance(estimated_cost, (int, float))
            or not math.isfinite(estimated_cost)
            or estimated_cost < 0
        ):
            raise CollectionStateError("cloud request intent row is inconsistent")
        intents.add(fingerprint)
    unresolved = intents - completed
    if unresolved:
        raise CollectionStateError(
            "paid request intent has no complete response row; automatic resend is forbidden"
        )
    return ResumeState(
        frozenset(completed), charged_input_tokens, frozenset(terminal_failures)
    )


def call_deepinfra(
    model: str,
    request_body: dict[str, Any],
    api_key: str,
    timeout: float,
    endpoint: str = "https://api.deepinfra.com/v1/inference",
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    url = f"{endpoint}/{model}"
    payload = canonical_json(request_body)
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_CLOUD_RESPONSE_BYTES + 1)
            if len(raw) > MAX_CLOUD_RESPONSE_BYTES:
                raise RuntimeError("cloud response exceeded bounded read limit")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_CLOUD_RESPONSE_BYTES + 1)
        if len(raw) > MAX_CLOUD_RESPONSE_BYTES:
            raise RuntimeError("cloud error response exceeded bounded read limit") from exc
        status = exc.code
    elapsed = time.monotonic() - started
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        body = {"_raw": raw.decode("utf-8", errors="replace")}
    timing = {
        "http_status": status,
        "elapsed_seconds": round(elapsed, 4),
        "response_bytes": len(raw),
    }
    return status, body, timing


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=[DEFAULT_PROVIDER])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    parser.add_argument("--budget-usd", type=float, default=3.00)
    parser.add_argument("--price-per-mtok", type=float, default=DEFAULT_PRICE_PER_MTOK)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--rate-delay-seconds", type=float, default=0.5)
    parser.add_argument("--max-groups", type=int, default=0, help="0 = all groups")
    parser.add_argument("--resume", action="store_true", help="skip groups already in output")
    parser.add_argument("--dry-run", action="store_true", help="build requests, estimate cost, do not call API")
    return parser.parse_args()


def _run_owned(args: argparse.Namespace) -> int:

    if (
        not math.isfinite(args.budget_usd)
        or args.budget_usd < 0
        or not math.isfinite(args.price_per_mtok)
        or args.price_per_mtok <= 0
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
        or not math.isfinite(args.rate_delay_seconds)
        or args.rate_delay_seconds < 0
        or args.max_groups < 0
    ):
        print("ERROR: limits must be finite and non-negative", file=sys.stderr)
        return 2

    try:
        groups = load_corpus(args.corpus)
        if args.max_groups > 0:
            groups = groups[: args.max_groups]
        if not groups:
            raise CollectionStateError("corpus must contain at least one query group")
        plans: list[tuple[dict[str, Any], dict[str, Any], str, int]] = []
        planned: dict[str, ExpectedPlanRow] = {}
        for group in groups:
            request_body = build_request(group, args.instruction)
            fingerprint = request_fingerprint(
                group,
                request_body,
                provider=args.provider,
                model=args.model,
            )
            if fingerprint in planned:
                raise CollectionStateError("cloud plan contains duplicate requests")
            estimated_tokens = estimate_tokens(request_body)
            planned[fingerprint] = ExpectedPlanRow(
                provider=args.provider,
                model=args.model,
                query_id=group.get("query_id"),
                source_language=group.get("source_language"),
                request_instruction=args.instruction,
                pair_count=len(request_body["queries"]),
                candidate_document_ids=tuple(
                    candidate.get("document_id") for candidate in group["candidates"]
                ),
                candidate_relevance=tuple(
                    candidate.get("relevance") for candidate in group["candidates"]
                ),
                estimated_input_tokens=estimated_tokens,
            )
            plans.append((group, request_body, fingerprint, estimated_tokens))

        if args.resume:
            resume = _load_resume_state(args.output, planned)
        else:
            if args.output.exists() or intent_path(args.output).exists():
                raise CollectionStateError(
                    "output or request intent ledger already exists; use --resume"
                )
            resume = ResumeState(frozenset(), 0, frozenset())
    except (
        CollectionStateError,
        KeyError,
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    remaining = [plan for plan in plans if plan[2] not in resume.completed]
    projected_input_tokens = resume.charged_input_tokens + sum(
        estimated for _group, _request, _fingerprint, estimated in remaining
    )
    projected_cost = projected_input_tokens / 1_000_000 * args.price_per_mtok
    if projected_cost > args.budget_usd:
        print(
            "ERROR: full chargeable cloud plan exceeds hard budget before first "
            f"request (${projected_cost:.6f} > ${args.budget_usd:.6f})",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        failed = len(resume.terminal_failures)
        print(
            json.dumps(
                {
                    "status": "FAILED" if failed else "DRY_RUN",
                    "provider": args.provider,
                    "model": args.model,
                    "groups_total": len(groups),
                    "groups_completed": len(remaining),
                    "groups_skipped": len(resume.completed),
                    "groups_failed": failed,
                    "total_input_tokens": projected_input_tokens,
                    "total_cost_usd": round(projected_cost, 6),
                    "budget_usd": args.budget_usd,
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 1 if failed else 0

    api_key = os.environ.get("DEEPINFRA_KEY", "")
    if not api_key:
        print("ERROR: DEEPINFRA_KEY is not set", file=sys.stderr)
        return 2

    total_input_tokens = resume.charged_input_tokens
    completed = 0
    skipped = len(resume.completed)
    failed = len(resume.terminal_failures)

    for index, (group, request_body, fingerprint, est_tokens) in enumerate(plans, 1):
        if fingerprint in resume.completed:
            continue
        intent = {
            "schema": "reranker-cloud-request-intent-v1",
            "provider": args.provider,
            "model": args.model,
            "query_id": group.get("query_id"),
            "request_fingerprint": fingerprint,
            "estimated_input_tokens": est_tokens,
            "estimated_cost_usd": round(
                est_tokens / 1_000_000 * args.price_per_mtok, 8
            ),
        }
        try:
            _append_durable(intent_path(args.output), intent)
        except OSError as exc:
            print(f"ERROR: cannot persist paid request intent: {exc}", file=sys.stderr)
            return 1

        try:
            status, body, timing = call_deepinfra(
                args.model, request_body, api_key, args.timeout
            )
        except Exception as exc:
            failed += 1
            print(
                f"ERROR group {index} {group.get('query_id')}: {exc}; "
                "request intent is ambiguous and resume is blocked",
                file=sys.stderr,
            )
            break

        reported_tokens = body.get("input_tokens") if isinstance(body, dict) else None
        token_count_invalid = reported_tokens is not None and (
            isinstance(reported_tokens, bool)
            or not isinstance(reported_tokens, int)
            or reported_tokens < 0
        )
        score_payload_invalid = False
        if status == 200:
            try:
                validate_scores(
                    body.get("scores") if isinstance(body, dict) else None,
                    len(request_body["queries"]),
                    minimum=0.0,
                    maximum=1.0,
                    label="DeepInfra response scores",
                )
            except ScoreValidationError:
                score_payload_invalid = True
        charged_tokens = (
            reported_tokens
            if isinstance(reported_tokens, int)
            and not isinstance(reported_tokens, bool)
            and reported_tokens > 0
            else est_tokens
        )
        total_input_tokens += charged_tokens
        total_cost = total_input_tokens / 1_000_000 * args.price_per_mtok

        record = {
            "schema": "reranker-cloud-baseline-v1",
            "provider": args.provider,
            "model": args.model,
            "query_id": group.get("query_id"),
            "source_language": group.get("source_language"),
            "request_fingerprint": fingerprint,
            "request_instruction": args.instruction,
            "pair_count": len(request_body["queries"]),
            "candidate_document_ids": [c.get("document_id") for c in group["candidates"]],
            "candidate_relevance": [c.get("relevance") for c in group["candidates"]],
            "response": body,
            "timing": timing,
            "charged_input_tokens": charged_tokens,
            "estimated_input_tokens": est_tokens,
            "cumulative_cost_usd": round(total_cost, 6),
        }
        try:
            _append_durable(args.output, record)
        except OSError as exc:
            failed += 1
            print(
                f"ERROR: paid response could not be persisted: {exc}; "
                "request intent is ambiguous and resume is blocked",
                file=sys.stderr,
            )
            break
        if status == 200 and not token_count_invalid and not score_payload_invalid:
            completed += 1

        remaining_estimate = sum(
            pending_estimate
            for _pending_group, _pending_request, pending_fingerprint, pending_estimate in plans[index:]
            if pending_fingerprint not in resume.completed
        )
        if (
            (total_input_tokens + remaining_estimate)
            / 1_000_000
            * args.price_per_mtok
            > args.budget_usd
        ):
            failed += 1
            print(
                "ERROR: reported token charge invalidated the remaining hard-budget "
                "plan; stopping before another paid request",
                file=sys.stderr,
            )
            break
        if status != 200 or token_count_invalid or score_payload_invalid:
            failed += 1
            print(
                f"WARN group {index} {group.get('query_id')} http={status} "
                f"cost=${total_cost:.6f}; stopping after durable response evidence",
                file=sys.stderr,
            )
            break

        if args.rate_delay_seconds > 0:
            time.sleep(args.rate_delay_seconds)

        if index % 10 == 0:
            print(
                f"progress {index}/{len(groups)} completed={completed} "
                f"skipped={skipped} failed={failed} cost=${total_cost:.6f} "
                f"tokens={total_input_tokens}",
                file=sys.stderr,
            )

    total_cost = total_input_tokens / 1_000_000 * args.price_per_mtok
    print(
        json.dumps(
            {
                "status": "FAILED" if failed else "DONE",
                "provider": args.provider,
                "model": args.model,
                "groups_total": len(groups),
                "groups_completed": completed,
                "groups_skipped": skipped,
                "groups_failed": failed,
                "total_input_tokens": total_input_tokens,
                "total_cost_usd": round(total_cost, 6),
                "budget_usd": args.budget_usd,
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0 if failed == 0 and completed + skipped == len(groups) else 1


def main() -> int:
    args = _parse_args()
    try:
        lock_descriptor = _acquire_output_lock(args.output)
    except CollectionStateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        return _run_owned(args)
    finally:
        os.close(lock_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
