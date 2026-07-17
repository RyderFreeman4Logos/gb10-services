#!/usr/bin/env python3
"""Compare DeepInfra and local reranker endpoints on a pinned public corpus.

The cloud evidence cache is intentionally fail-closed. A request ledger is
durable before the network call; any ledger without a complete response is an
ambiguous paid request and is never resent automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


__all__ = [
    "AmbiguousTransportError",
    "CacheStateError",
    "CloudEvidenceCache",
    "CorpusValidationError",
    "CostCapError",
    "EvidenceError",
    "HttpResult",
    "ResponseValidationError",
    "canonical_payload",
    "canonical_request_hash",
    "compute_comparison_metrics",
    "compute_endpoint_metrics",
    "estimate_input_tokens",
    "fetch_cloud_batches",
    "load_corpus",
    "rank_indices",
    "sanitize_response_headers",
    "validate_response",
]


ENDPOINT_PATH = "/v1/inference/Qwen/Qwen3-Reranker-8B"
DEFAULT_CACHE_ROOT = Path(
    "/ssd/mirror-rootfs/home/obj/project/github/RyderFreeman4Logos/"
    "llm-guard-proxy/evaluation/deepinfra-qwen3-reranker-8b"
)
DEFAULT_CORPUS = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "reranker-equivalence"
    / "miracl-reranking-en-zh-dev.jsonl"
)
PRICE_USD_PER_MILLION_INPUT_TOKENS = 0.05
PROMPT_OVERHEAD_TOKENS_PER_PAIR = 256
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CORPUS_BYTES = 64 * 1024 * 1024
ALLOWED_RESPONSE_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "date",
        "retry-after",
        "server",
        "x-request-id",
        "x-deepinfra-request-id",
        "cf-ray",
    }
)


class EquivalenceError(RuntimeError):
    """The experiment cannot continue without violating its evidence contract."""


class EvidenceError(EquivalenceError):
    """Persistent evidence is unsafe, malformed, or incomplete."""


class CacheStateError(EvidenceError):
    """A paid request has a ledger but no complete reusable response."""


class CacheMissError(EvidenceError):
    """Cache-only mode could not find a complete cloud response."""


class AmbiguousTransportError(EquivalenceError):
    """A request may have reached the endpoint, so automatic retry is forbidden."""


class CostCapError(EquivalenceError):
    """The conservative cloud plan exceeds an explicit hard cap."""


class ResponseValidationError(EquivalenceError):
    """An endpoint response does not satisfy the public wire contract."""


class CorpusValidationError(EquivalenceError):
    """The public reranking corpus violates its committed selection contract."""


class EndpointHttpError(EquivalenceError):
    """An endpoint returned a cached or live non-success HTTP response."""


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


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: int


@dataclass(frozen=True)
class EvidenceResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: int
    request_hash: str
    from_cache: bool


@dataclass(frozen=True)
class ValidatedResponse:
    scores: tuple[float, ...]
    input_tokens: int
    request_id: str | None
    inference_status: object | None
    present_fields: tuple[str, ...]


Transport = Callable[[str, bytes, dict[str, str], float], HttpResult]


def _validate_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusValidationError(f"{label} must be non-empty text")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CorpusValidationError(f"{label} contains an unpaired surrogate")
    return value


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        rendered = json.dumps(
            value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True
        )
    else:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return (rendered + ("\n" if pretty else "")).encode("utf-8")


def canonical_payload(
    queries: Sequence[str],
    documents: Sequence[str],
    *,
    instruction: str | None = None,
    service_tier: str | None = None,
) -> bytes:
    """Return the one canonical request body sent byte-for-byte to both endpoints."""

    if not queries or len(queries) != len(documents) or len(queries) > 1024:
        raise ValueError("queries/documents must have equal length in [1, 1024]")
    payload: dict[str, object] = {
        "queries": [_validate_text(value, "query") for value in queries],
        "documents": [_validate_text(value, "document") for value in documents],
    }
    if instruction is not None:
        payload["instruction"] = _validate_text(instruction, "instruction")
    if service_tier is not None:
        payload["service_tier"] = _validate_text(service_tier, "service_tier")
    return _json_bytes(payload)


def canonical_request_hash(body: bytes) -> str:
    """Bind a cache key to the canonical method, public path, and exact body bytes."""

    digest = hashlib.sha256()
    digest.update(b"deepinfra-reranker-request-v1\0POST\0")
    digest.update(ENDPOINT_PATH.encode("ascii"))
    digest.update(b"\0")
    digest.update(body)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(descriptor, view[written:])
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_regular(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError(f"cannot open evidence file: {path.name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 0
            or metadata.st_size > maximum
        ):
            raise EvidenceError(f"evidence file is unsafe or oversized: {path.name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise EvidenceError(f"evidence file is oversized: {path.name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parse_json_object(payload: bytes, label: str) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        value = json.loads(payload, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ResponseValidationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ResponseValidationError(f"{label} must be a JSON object")
    return value


def sanitize_response_headers(
    headers: Mapping[str, str], *, secrets: Sequence[str] = ()
) -> dict[str, str]:
    """Retain a small diagnostic allowlist and redact secret-bearing values."""

    sanitized: dict[str, str] = {}
    active_secrets = tuple(secret for secret in secrets if secret)
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name not in ALLOWED_RESPONSE_HEADERS:
            continue
        value = str(raw_value).replace("\r", " ").replace("\n", " ")[:4096]
        if any(secret in value for secret in active_secrets):
            value = "<redacted>"
        sanitized[name] = value
    return dict(sorted(sanitized.items()))


def _urllib_transport(
    url: str, body: bytes, headers: dict[str, str], timeout: float
) -> HttpResult:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.monotonic_ns()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            result = HttpResult(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=response_body,
                elapsed_ms=(time.monotonic_ns() - started) // 1_000_000,
            )
    except urllib.error.HTTPError as exc:
        response_body = exc.read(MAX_RESPONSE_BYTES + 1)
        result = HttpResult(
            status=int(exc.code),
            headers=dict(exc.headers.items()) if exc.headers is not None else {},
            body=response_body,
            elapsed_ms=(time.monotonic_ns() - started) // 1_000_000,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AmbiguousTransportError(
            f"{type(exc).__name__}: transport ended without an authoritative response"
        ) from exc
    if len(result.body) > MAX_RESPONSE_BYTES:
        raise AmbiguousTransportError("endpoint response exceeded the bounded read limit")
    return result


class CloudEvidenceCache:
    """Durable cloud request/response evidence keyed only by canonical request bytes."""

    def __init__(self, root: Path, *, transport: Transport = _urllib_transport) -> None:
        self.root = root
        self.transport = transport

    def _prepare_root(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self.root.lstat()
        except OSError as exc:
            raise EvidenceError("cannot prepare cloud evidence root") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise EvidenceError("cloud evidence root must be a real directory")

    @staticmethod
    def _require_request_directory(request_dir: Path) -> None:
        try:
            metadata = request_dir.lstat()
        except OSError as exc:
            raise CacheStateError("cannot inspect cached request directory") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CacheStateError("cached request path must be a real directory")

    def _load_complete(self, request_dir: Path, body: bytes) -> EvidenceResponse:
        request_hash = canonical_request_hash(body)
        try:
            request_record = json.loads(_read_regular(request_dir / "request.json", 20 * 1024 * 1024))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CacheStateError("cached request ledger is malformed") from exc
        expected_request = {
            "method": "POST",
            "path": ENDPOINT_PATH,
            "request_body": body.decode("utf-8"),
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "request_hash": request_hash,
            "schema": "deepinfra-reranker-request-v1",
        }
        if request_record != expected_request:
            raise CacheStateError("cached request ledger does not match canonical bytes")

        response_path = request_dir / "response.json"
        body_path = request_dir / "response.body"
        if not response_path.exists() or not body_path.exists():
            raise CacheStateError(
                "request ledger has no complete response; automatic resend is forbidden"
            )
        response_body = _read_regular(body_path, MAX_RESPONSE_BYTES)
        try:
            response_record = json.loads(_read_regular(response_path, 1024 * 1024))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CacheStateError("cached response metadata is malformed") from exc
        required = {
            "body_sha256",
            "elapsed_ms",
            "headers",
            "request_hash",
            "schema",
            "status",
        }
        if not isinstance(response_record, dict) or set(response_record) != required:
            raise CacheStateError("cached response metadata fields are not exact")
        status_value = response_record["status"]
        elapsed_value = response_record["elapsed_ms"]
        if (
            response_record["schema"] != "deepinfra-reranker-response-v1"
            or response_record["request_hash"] != request_hash
            or response_record["body_sha256"]
            != hashlib.sha256(response_body).hexdigest()
            or isinstance(status_value, bool)
            or not isinstance(status_value, int)
            or not 100 <= status_value <= 599
            or isinstance(elapsed_value, bool)
            or not isinstance(elapsed_value, int)
            or elapsed_value < 0
            or not isinstance(response_record["headers"], dict)
        ):
            raise CacheStateError("cached response metadata does not bind exact evidence")
        return EvidenceResponse(
            status=status_value,
            headers=response_record["headers"],
            body=response_body,
            elapsed_ms=elapsed_value,
            request_hash=request_hash,
            from_cache=True,
        )

    def fetch(
        self,
        body: bytes,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
        cache_only: bool = False,
    ) -> EvidenceResponse:
        """Return cached evidence or send exactly once after a durable ledger commit."""

        if not base_url or timeout <= 0:
            raise ValueError("base URL and positive timeout are required")
        if not api_key and not cache_only:
            raise ValueError("API key is required when cloud network access is allowed")
        request_hash = canonical_request_hash(body)
        request_dir = self.root / request_hash
        if request_dir.exists():
            self._require_request_directory(request_dir)
            return self._load_complete(request_dir, body)
        if cache_only:
            raise CacheMissError(f"no cached cloud response for {request_hash}")
        if api_key.encode("utf-8") in body:
            raise EvidenceError("request body contains the configured API key")

        self._prepare_root()
        try:
            request_dir.mkdir(mode=0o700)
        except FileExistsError:
            self._require_request_directory(request_dir)
            return self._load_complete(request_dir, body)
        _fsync_directory(self.root)
        request_record = {
            "method": "POST",
            "path": ENDPOINT_PATH,
            "request_body": body.decode("utf-8"),
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "request_hash": request_hash,
            "schema": "deepinfra-reranker-request-v1",
        }
        _atomic_write(request_dir / "request.json", _json_bytes(request_record, pretty=True))

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "gb10-reranker-equivalence/1",
        }
        url = base_url.rstrip("/") + ENDPOINT_PATH
        try:
            result = self.transport(url, body, headers, timeout)
        except AmbiguousTransportError as exc:
            message = str(exc).replace(api_key, "<redacted>")[:4096]
            attempt = {
                "error": message,
                "error_type": type(exc).__name__,
                "request_hash": request_hash,
                "schema": "deepinfra-reranker-ambiguous-attempt-v1",
            }
            attempt_path = request_dir / f"ambiguous-{time.time_ns()}.json"
            _atomic_write(attempt_path, _json_bytes(attempt, pretty=True))
            raise
        if (
            isinstance(result.status, bool)
            or not isinstance(result.status, int)
            or not 100 <= result.status <= 599
            or isinstance(result.elapsed_ms, bool)
            or not isinstance(result.elapsed_ms, int)
            or result.elapsed_ms < 0
            or len(result.body) > MAX_RESPONSE_BYTES
        ):
            raise EvidenceError("transport returned malformed bounded response evidence")
        if api_key.encode("utf-8") in result.body:
            raise EvidenceError("response body contains the configured API key")
        sanitized_headers = sanitize_response_headers(
            result.headers, secrets=(api_key, f"Bearer {api_key}")
        )
        _atomic_write(request_dir / "response.body", result.body)
        response_record = {
            "body_sha256": hashlib.sha256(result.body).hexdigest(),
            "elapsed_ms": result.elapsed_ms,
            "headers": sanitized_headers,
            "request_hash": request_hash,
            "schema": "deepinfra-reranker-response-v1",
            "status": result.status,
        }
        _atomic_write(
            request_dir / "response.json", _json_bytes(response_record, pretty=True)
        )
        return EvidenceResponse(
            status=result.status,
            headers=sanitized_headers,
            body=result.body,
            elapsed_ms=result.elapsed_ms,
            request_hash=request_hash,
            from_cache=False,
        )


def validate_response(body: bytes, expected_count: int) -> ValidatedResponse:
    """Validate strict DeepInfra native response fields and finite score cardinality."""

    if expected_count <= 0 or expected_count > 1024:
        raise ValueError("expected response cardinality must be in [1, 1024]")
    payload = _parse_json_object(body, "response")
    required = {"scores", "input_tokens"}
    allowed = required | {"request_id", "inference_status"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise ResponseValidationError("response fields do not match the public schema")
    raw_scores = payload["scores"]
    if not isinstance(raw_scores, list) or len(raw_scores) != expected_count:
        raise ResponseValidationError("response score cardinality differs from request")
    scores: list[float] = []
    for value in raw_scores:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ResponseValidationError("response scores must be finite numbers")
        scores.append(float(value))
    input_tokens = payload["input_tokens"]
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
    ):
        raise ResponseValidationError("response input_tokens must be a non-negative integer")
    request_id = payload.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise ResponseValidationError("response request_id must be non-empty text")
    inference_status = payload.get("inference_status")
    if inference_status is not None and not isinstance(inference_status, (dict, str)):
        raise ResponseValidationError("response inference_status must be an object or string")
    return ValidatedResponse(
        scores=tuple(scores),
        input_tokens=input_tokens,
        request_id=request_id,
        inference_status=inference_status,
        present_fields=tuple(sorted(payload)),
    )


def _payload_cardinality(body: bytes) -> int:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canonical payload is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) - {
        "queries",
        "documents",
        "instruction",
        "service_tier",
    }:
        raise ValueError("canonical payload fields are invalid")
    queries = payload.get("queries")
    documents = payload.get("documents")
    if (
        not isinstance(queries, list)
        or not isinstance(documents, list)
        or not queries
        or len(queries) != len(documents)
        or len(queries) > 1024
    ):
        raise ValueError("canonical payload cardinality is invalid")
    return len(queries)


def _estimated_cost(estimated_tokens: int) -> float:
    return estimated_tokens * PRICE_USD_PER_MILLION_INPUT_TOKENS / 1_000_000


def _enforce_cost_cap(
    estimated_tokens: int, max_estimated_tokens: int, max_cost_usd: float
) -> float:
    if (
        estimated_tokens < 0
        or max_estimated_tokens < 0
        or not math.isfinite(max_cost_usd)
        or max_cost_usd < 0
    ):
        raise ValueError("cost plan and caps must be finite and non-negative")
    estimated_cost = _estimated_cost(estimated_tokens)
    if estimated_tokens > max_estimated_tokens or estimated_cost > max_cost_usd:
        raise CostCapError(
            "cloud plan exceeds hard cap: "
            f"estimated_tokens={estimated_tokens} max_tokens={max_estimated_tokens} "
            f"estimated_cost_usd={estimated_cost:.8f} max_cost_usd={max_cost_usd:.8f}"
        )
    return estimated_cost


def fetch_cloud_batches(
    payloads: Sequence[bytes],
    *,
    cache: CloudEvidenceCache,
    base_url: str,
    api_key: str,
    timeout: float,
    estimated_tokens: int,
    max_estimated_tokens: int,
    max_cost_usd: float,
    cache_only: bool,
) -> list[ValidatedResponse]:
    """Enforce the whole-plan cap before reading cache or sending the first request."""

    if not cache_only:
        _enforce_cost_cap(estimated_tokens, max_estimated_tokens, max_cost_usd)
    validated: list[ValidatedResponse] = []
    for payload in payloads:
        expected = _payload_cardinality(payload)
        response = cache.fetch(
            payload,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            cache_only=cache_only,
        )
        if not 200 <= response.status < 300:
            raise EndpointHttpError(
                f"cloud response for {response.request_hash} has HTTP {response.status}"
            )
        validated.append(validate_response(response.body, expected))
    return validated


def load_corpus(path: Path) -> list[QueryGroup]:
    """Load and strictly validate query groups from the committed public JSONL."""

    try:
        raw = _read_regular(path, MAX_CORPUS_BYTES)
    except EvidenceError as exc:
        raise CorpusValidationError(str(exc)) from exc
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
            row = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise CorpusValidationError(f"invalid JSON on corpus line {line_number}") from exc
        if not isinstance(row, dict) or set(row) != {
            "candidates",
            "query",
            "query_id",
            "source_language",
        }:
            raise CorpusValidationError(f"group fields are not exact on line {line_number}")
        query_id = _validate_text(row["query_id"], "query_id")
        query = _validate_text(row["query"], "query")
        language = _validate_text(row["source_language"], "source_language")
        identity = (language, query_id)
        if identity in identities:
            raise CorpusValidationError("duplicate language/query identity")
        identities.add(identity)
        raw_candidates = row["candidates"]
        if not isinstance(raw_candidates, list) or len(raw_candidates) != 10:
            raise CorpusValidationError("each query group must contain exactly 10 candidates")
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
                raise CorpusValidationError("candidate relevance or top_ranked_rank is invalid")
            if candidate_language != language:
                raise CorpusValidationError("candidate source language differs from query")
            if document_id in document_ids:
                raise CorpusValidationError("query group contains duplicate document IDs")
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
            raise CorpusValidationError("candidate top_ranked ranks must be unique and ordered")
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
    """Conservatively upper-bound tokens by UTF-8 bytes plus fixed prompt overhead."""

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


def _build_batches(
    groups: Sequence[QueryGroup],
    batch_size: int,
    *,
    instruction: str | None,
    service_tier: str | None,
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
    positive_ranks = [index + 1 for index, relevance in enumerate(relevances) if relevance > 0]
    mrr = 1.0 / positive_ranks[0] if positive_ranks else 0.0
    dcg = sum(
        (2.0**relevance - 1.0) / math.log2(rank + 2)
        for rank, relevance in enumerate(relevances)
    )
    ideal = sorted((candidate.relevance for candidate in group.candidates), reverse=True)[:10]
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
        "standard_deviation": math.sqrt(_mean([(score - mean) ** 2 for score in scores])),
    }


def compute_endpoint_metrics(
    groups: Sequence[QueryGroup], scores: Sequence[float]
) -> dict[str, object]:
    """Compute independent public-label quality metrics without a PASS threshold."""

    expected = sum(len(group.candidates) for group in groups)
    if len(scores) != expected:
        raise ValueError("score count differs from corpus pair count")
    if any(not math.isfinite(float(score)) for score in scores):
        raise ValueError("score metrics require finite values")
    rows: list[tuple[str, dict[str, float]]] = []
    offset = 0
    for group in groups:
        width = len(group.candidates)
        rows.append((group.source_language, _quality_for_group(group, scores[offset : offset + width])))
        offset += width

    def summarize(selected: Sequence[dict[str, float]]) -> dict[str, float | int]:
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
            language: summarize([row for row_language, row in rows if row_language == language])
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
    return sum(a * b for a, b in zip(left_delta, right_delta, strict=True)) / denominator


def compute_comparison_metrics(
    groups: Sequence[QueryGroup],
    cloud_scores: Sequence[float],
    local_scores: Sequence[float],
) -> dict[str, object]:
    """Compute rank agreement and calibration diagnostics without score equality gates."""

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


def _fetch_local_batches(
    payloads: Sequence[bytes],
    *,
    base_url: str,
    api_key: str | None,
    timeout: float,
) -> list[ValidatedResponse]:
    validated: list[ValidatedResponse] = []
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "gb10-reranker-equivalence/1",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for payload in payloads:
        result = _urllib_transport(
            base_url.rstrip("/") + ENDPOINT_PATH, payload, headers, timeout
        )
        if not 200 <= result.status < 300:
            raise EndpointHttpError(f"local endpoint returned HTTP {result.status}")
        validated.append(validate_response(result.body, _payload_cardinality(payload)))
    return validated


def _flatten_scores(responses: Sequence[ValidatedResponse]) -> list[float]:
    return [score for response in responses for score in response.scores]


def _field_sets(responses: Sequence[ValidatedResponse]) -> list[list[str]]:
    return [list(response.present_fields) for response in responses]


def _human_report(report: Mapping[str, object]) -> str:
    quality = report["quality"]
    comparison = report["cloud_vs_local"]
    cost = report["cost"]
    parity = report["api_schema_parity"]
    if not all(
        isinstance(section, dict) for section in (quality, comparison, cost, parity)
    ):
        raise ValueError("report sections are malformed")
    lines = [
        "RERANKER ENDPOINT EQUIVALENCE REPORT",
        f"groups={report['groups']} pairs={report['pairs']} languages={','.join(report['languages'])}",
        (
            "wire: path="
            f"{parity['endpoint_path']} byte_equivalent_requests="
            f"{str(parity['request_payloads_byte_equivalent']).lower()} "
            f"cloud_schema={str(parity['response_schema_valid']['cloud']).lower()} "
            f"local_schema={str(parity['response_schema_valid']['local']).lower()}"
        ),
        (
            f"cost: estimated_input_tokens_upper_bound={cost['estimated_input_tokens_upper_bound']} "
            f"estimated_cost_usd={cost['estimated_cost_usd']:.8f} "
            f"actual_input_tokens={cost['actual_input_tokens']} "
            f"actual_cost_usd={cost['actual_cost_usd']:.8f}"
        ),
    ]
    for endpoint in ("cloud", "local"):
        metrics = quality[endpoint]["aggregate"]
        domain = quality[endpoint]["score_domain"]
        lines.append(
            f"{endpoint}: MRR@10={metrics['mrr_at_10']:.6f} "
            f"nDCG@10={metrics['ndcg_at_10']:.6f} MAP@10={metrics['map_at_10']:.6f}"
        )
        lines.append(
            f"{endpoint} score domain: min={domain['min']:.8f} "
            f"max={domain['max']:.8f} mean={domain['mean']:.8f} "
            f"stddev={domain['standard_deviation']:.8f}"
        )
        for language, language_metrics in quality[endpoint]["per_language"].items():
            lines.append(
                f"  {endpoint}/{language}: MRR@10={language_metrics['mrr_at_10']:.6f} "
                f"nDCG@10={language_metrics['ndcg_at_10']:.6f} "
                f"MAP@10={language_metrics['map_at_10']:.6f}"
            )
    rank = comparison["rank_correlation"]
    overlap = comparison["top_k_overlap"]
    calibration = comparison["score_calibration"]
    lines.append(
        f"cloud-vs-local: mean_spearman={rank['mean_spearman']:.6f} "
        f"min_spearman={rank['min_spearman']:.6f} "
        f"top1={overlap['at_1']:.6f} top3={overlap['at_3']:.6f} "
        f"top5={overlap['at_5']:.6f} top10={overlap['at_10']:.6f}"
    )
    lines.append(
        f"calibration: paired_pearson={calibration['paired_pearson']:.6f} "
        f"MAE={calibration['mean_absolute_error']:.8f} "
        f"RMSE={calibration['rmse']:.8f} "
        f"mean_local_minus_cloud={calibration['mean_difference_local_minus_cloud']:.8f}"
    )
    lines.append("No quality PASS threshold is applied; endpoint quality is reported independently.")
    return "\n".join(lines) + "\n"


def _emit_report(report: dict[str, object], output_json: str, human_output: str | None) -> None:
    human = _human_report(report)
    print(human, file=sys.stderr, end="")
    if human_output is not None:
        target = Path(human_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, human.encode("utf-8"))
    encoded = _json_bytes(report, pretty=True)
    if output_json == "-":
        sys.stdout.buffer.write(encoded)
    else:
        target = Path(output_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, encoded)


def _emit_preview(
    preview: dict[str, object], output_json: str, human_output: str | None
) -> None:
    human = (
        "RERANKER CLOUD COST PREVIEW\n"
        f"pairs={preview['pairs']} batches={preview['batches']}\n"
        "conservative_estimated_input_tokens="
        f"{preview['estimated_input_tokens_upper_bound']} "
        f"estimated_cost_usd={preview['estimated_cost_usd']:.8f}\n"
        f"within_hard_cap={str(preview['within_hard_cap']).lower()}\n"
        "No endpoint request was sent.\n"
    )
    print(human, file=sys.stderr, end="")
    if human_output is not None:
        target = Path(human_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, human.encode("utf-8"))
    encoded = _json_bytes(preview, pretty=True)
    if output_json == "-":
        sys.stdout.buffer.write(encoded)
    else:
        target = Path(output_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, encoded)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--cloud-base-url", default="https://api.deepinfra.com")
    parser.add_argument("--cloud-api-key-env", default="DEEPINFRA_KEY")
    parser.add_argument("--local-base-url", default="http://100.105.4.92:18014")
    parser.add_argument("--local-api-key-env")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--instruction")
    parser.add_argument("--service-tier")
    parser.add_argument("--max-estimated-input-tokens", type=int, default=1_000_000)
    parser.add_argument("--max-cloud-cost-usd", type=float, default=0.05)
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help=(
            "forbid cloud network calls and API-key use; send-only cost caps do not "
            "apply, and the local endpoint is still probed"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-json", default="-")
    parser.add_argument("--human-output")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        groups = load_corpus(args.corpus)
        payloads = _build_batches(
            groups,
            args.batch_size,
            instruction=args.instruction,
            service_tier=args.service_tier,
        )
        estimated_tokens = estimate_input_tokens(groups, instruction=args.instruction)
        estimated_cost = _estimated_cost(estimated_tokens)
        within_cap = (
            estimated_tokens <= args.max_estimated_input_tokens
            and estimated_cost <= args.max_cloud_cost_usd
        )
        if args.dry_run:
            preview: dict[str, object] = {
                "batches": len(payloads),
                "estimated_cost_usd": estimated_cost,
                "estimated_input_tokens_upper_bound": estimated_tokens,
                "max_cloud_cost_usd": args.max_cloud_cost_usd,
                "max_estimated_input_tokens": args.max_estimated_input_tokens,
                "pairs": sum(len(group.candidates) for group in groups),
                "schema": "reranker-equivalence-cost-preview-v1",
                "within_hard_cap": within_cap,
            }
            _emit_preview(preview, args.output_json, args.human_output)
            return 0

        cloud_key = os.environ.get(args.cloud_api_key_env, "")
        if not cloud_key and not args.cache_only:
            raise EquivalenceError(
                f"cloud API key environment variable is unset: {args.cloud_api_key_env}"
            )
        local_key = (
            os.environ.get(args.local_api_key_env, "")
            if args.local_api_key_env
            else None
        )
        if args.local_api_key_env and not local_key:
            raise EquivalenceError(
                f"local API key environment variable is unset: {args.local_api_key_env}"
            )
        cloud = fetch_cloud_batches(
            payloads,
            cache=CloudEvidenceCache(args.cache_root),
            base_url=args.cloud_base_url,
            api_key=cloud_key,
            timeout=args.timeout,
            estimated_tokens=estimated_tokens,
            max_estimated_tokens=args.max_estimated_input_tokens,
            max_cost_usd=args.max_cloud_cost_usd,
            cache_only=args.cache_only,
        )
        local = _fetch_local_batches(
            payloads,
            base_url=args.local_base_url,
            api_key=local_key,
            timeout=args.timeout,
        )
        cloud_scores = _flatten_scores(cloud)
        local_scores = _flatten_scores(local)
        actual_tokens = sum(response.input_tokens for response in cloud)
        report: dict[str, object] = {
            "api_schema_parity": {
                "cloud_response_fields_by_batch": _field_sets(cloud),
                "endpoint_path": ENDPOINT_PATH,
                "local_response_fields_by_batch": _field_sets(local),
                "request_payload_sha256": [
                    hashlib.sha256(payload).hexdigest() for payload in payloads
                ],
                "request_payloads_byte_equivalent": True,
                "response_schema_valid": {"cloud": True, "local": True},
            },
            "cloud_vs_local": compute_comparison_metrics(
                groups, cloud_scores, local_scores
            ),
            "cost": {
                "actual_cost_usd": actual_tokens
                * PRICE_USD_PER_MILLION_INPUT_TOKENS
                / 1_000_000,
                "actual_input_tokens": actual_tokens,
                "estimated_cost_usd": estimated_cost,
                "estimated_input_tokens_upper_bound": estimated_tokens,
                "price_usd_per_million_input_tokens": PRICE_USD_PER_MILLION_INPUT_TOKENS,
            },
            "groups": len(groups),
            "languages": sorted({group.source_language for group in groups}),
            "pairs": len(cloud_scores),
            "quality": {
                "cloud": compute_endpoint_metrics(groups, cloud_scores),
                "local": compute_endpoint_metrics(groups, local_scores),
            },
            "schema": "reranker-endpoint-equivalence-report-v1",
        }
        _emit_report(report, args.output_json, args.human_output)
        return 0
    except EquivalenceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
