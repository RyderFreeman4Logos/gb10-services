#!/usr/bin/env python3
"""Immutable reranker request identity, transport, evidence, and wire validation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


ENDPOINT_PATH = "/v1/inference/Qwen/Qwen3-Reranker-8B"
DEEPINFRA_MODEL_VERSION = "5fa94080caafeaa45a15d11f969d7978e087a3db"
PUBLIC_API_MODEL = "Qwen/Qwen3-Reranker-8B"
LOCAL_MODEL_REVISION = "7b796de30ad8dc772d6c46c75659c1341283a665"
PUBLIC_SCORE_MIN = 0.0
PUBLIC_SCORE_MAX = 1.0
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
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
    """A request has a ledger but no complete reusable response."""


class CacheMissError(EvidenceError):
    """Offline mode could not find a complete endpoint response."""


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
class EndpointSpec:
    provider: str
    model: str
    provider_model_version: str
    runner_transform: str
    public_api_version: str = DEEPINFRA_MODEL_VERSION
    auth_required: bool = True

    def __post_init__(self) -> None:
        for value, label in (
            (self.provider, "provider"),
            (self.model, "model"),
            (self.provider_model_version, "provider model version"),
            (self.runner_transform, "runner/transform identity"),
            (self.public_api_version, "public API version"),
        ):
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ValueError(f"{label} must be non-empty text without NUL")


CLOUD_ENDPOINT = EndpointSpec(
    provider="deepinfra",
    model=PUBLIC_API_MODEL,
    provider_model_version=DEEPINFRA_MODEL_VERSION,
    runner_transform="gb10-reranker-equivalence-v2+deepinfra-native-v1",
)
LOCAL_ENDPOINT = EndpointSpec(
    provider="gb10-querit-vllm-adapter",
    model="Querit/Querit-4B",
    provider_model_version=LOCAL_MODEL_REVISION,
    runner_transform="gb10-reranker-equivalence-v2+querit-tanh-to-public-v1",
    auth_required=False,
)


@dataclass(frozen=True)
class RequestIdentity:
    provider: str
    model: str
    provider_model_version: str
    runner_transform: str
    path_and_query: str
    request_body: bytes
    instruction: str | None
    service_tier: str | None
    auth_required: bool

    def record(self) -> dict[str, object]:
        return {
            "body_sha256": hashlib.sha256(self.request_body).hexdigest(),
            "instruction": self.instruction,
            "method": "POST",
            "model": self.model,
            "path_and_query": self.path_and_query,
            "provider": self.provider,
            "provider_model_version": self.provider_model_version,
            "request_body": self.request_body.decode("utf-8"),
            "runner_transform": self.runner_transform,
            "schema": "reranker-request-identity-v2",
            "service_tier": self.service_tier,
        }

    def canonical_bytes(self) -> bytes:
        return _json_bytes(self.record())


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


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        rendered = json.dumps(
            value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True
        )
        rendered += "\n"
    else:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return rendered.encode("utf-8")


def _validate_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusValidationError(f"{label} must be non-empty text")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CorpusValidationError(f"{label} contains an unpaired surrogate")
    return value


def canonical_payload(
    queries: Sequence[str],
    documents: Sequence[str],
    *,
    instruction: str | None = None,
    service_tier: str | None = None,
) -> bytes:
    """Return the canonical body sent byte-for-byte to both public endpoints."""

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


def _parse_strict_json_object(payload: bytes, label: str) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        value = json.loads(payload, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ResponseValidationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ResponseValidationError(f"{label} must be a JSON object")
    return value


def _canonical_payload_parts(body: bytes) -> tuple[int, str | None, str | None]:
    payload = _parse_strict_json_object(body, "canonical request")
    if set(payload) - {"queries", "documents", "instruction", "service_tier"}:
        raise ValueError("canonical payload fields are invalid")
    queries = payload.get("queries")
    documents = payload.get("documents")
    if (
        not isinstance(queries, list)
        or not isinstance(documents, list)
        or not queries
        or len(queries) != len(documents)
        or len(queries) > 1024
        or any(not isinstance(value, str) or not value for value in queries + documents)
    ):
        raise ValueError("canonical payload cardinality or text is invalid")
    instruction = payload.get("instruction")
    service_tier = payload.get("service_tier")
    if instruction is not None and (
        not isinstance(instruction, str) or not instruction
    ):
        raise ValueError("canonical instruction is invalid")
    if service_tier is not None and (
        not isinstance(service_tier, str) or not service_tier
    ):
        raise ValueError("canonical service tier is invalid")
    if _json_bytes(payload) != body:
        raise ValueError("request body is not the canonical byte serialization")
    return len(queries), instruction, service_tier


def request_identity(body: bytes, endpoint: EndpointSpec) -> RequestIdentity:
    _, instruction, service_tier = _canonical_payload_parts(body)
    path_and_query = (
        ENDPOINT_PATH
        + "?"
        + urllib.parse.urlencode((("version", endpoint.public_api_version),))
    )
    return RequestIdentity(
        provider=endpoint.provider,
        model=endpoint.model,
        provider_model_version=endpoint.provider_model_version,
        runner_transform=endpoint.runner_transform,
        path_and_query=path_and_query,
        request_body=body,
        instruction=instruction,
        service_tier=service_tier,
        auth_required=endpoint.auth_required,
    )


def canonical_request_hash(identity: RequestIdentity) -> str:
    digest = hashlib.sha256(b"reranker-request-cache-key-v2\0")
    digest.update(identity.canonical_bytes())
    return digest.hexdigest()


def request_ledger_bytes(identity: RequestIdentity) -> bytes:
    record = identity.record()
    record["request_hash"] = canonical_request_hash(identity)
    return _json_bytes(record, pretty=True)


def endpoint_url(base_url: str, identity: RequestIdentity) -> str:
    try:
        parsed = urllib.parse.urlsplit(base_url)
    except ValueError as exc:
        raise ValueError("endpoint base URL is malformed") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("endpoint base URL must contain only scheme and authority")
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            ENDPOINT_PATH,
            identity.path_and_query.partition("?")[2],
            "",
        )
    )


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
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum:
            raise EvidenceError(f"evidence file is oversized: {path.name}")
        return bytes(payload)
    finally:
        os.close(descriptor)


def sanitize_response_headers(
    headers: Mapping[str, str], *, secrets: Sequence[str] = ()
) -> dict[str, str]:
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
        raise AmbiguousTransportError(
            "endpoint response exceeded the bounded read limit"
        )
    return result


class EndpointEvidenceCache:
    """Durable evidence keyed by the complete immutable request identity."""

    def __init__(self, root: Path, *, transport: Transport | None = None) -> None:
        self.root = root
        self.transport = transport

    def _prepare_root(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self.root.lstat()
        except OSError as exc:
            raise EvidenceError("cannot prepare endpoint evidence root") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise EvidenceError("endpoint evidence root must be a real directory")

    @staticmethod
    def _require_request_directory(request_dir: Path) -> None:
        try:
            metadata = request_dir.lstat()
        except OSError as exc:
            raise CacheStateError("cannot inspect cached request directory") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CacheStateError("cached request path must be a real directory")

    def _load_complete(
        self, request_dir: Path, identity: RequestIdentity
    ) -> EvidenceResponse:
        request_hash = canonical_request_hash(identity)
        try:
            request_record = json.loads(
                _read_regular(request_dir / "request.json", 20 * 1024 * 1024)
            )
            expected_request = json.loads(request_ledger_bytes(identity))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CacheStateError("cached request ledger is malformed") from exc
        if request_record != expected_request:
            raise CacheStateError(
                "cached request ledger does not match request identity"
            )

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
            response_record["schema"] != "reranker-endpoint-response-v2"
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
            raise CacheStateError(
                "cached response metadata does not bind exact evidence"
            )
        return EvidenceResponse(
            status=status_value,
            headers=response_record["headers"],
            body=response_body,
            elapsed_ms=elapsed_value,
            request_hash=request_hash,
            from_cache=True,
        )

    def load(self, identity: RequestIdentity) -> EvidenceResponse:
        """Read complete evidence without preparing credentials, URLs, or transport."""

        request_hash = canonical_request_hash(identity)
        request_dir = self.root / request_hash
        if not request_dir.exists():
            raise CacheMissError(
                f"no cached {identity.provider} response for {request_hash}"
            )
        self._require_request_directory(request_dir)
        return self._load_complete(request_dir, identity)

    def fetch(
        self,
        identity: RequestIdentity,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
    ) -> EvidenceResponse:
        """Return cached evidence or send once after the ledger is durable."""

        if self.transport is None:
            raise ValueError("live fetch requires an explicit transport")
        if timeout <= 0:
            raise ValueError("positive timeout is required")
        if identity.auth_required and not api_key:
            raise ValueError("API key is required for this endpoint")
        url = endpoint_url(base_url, identity)
        request_hash = canonical_request_hash(identity)
        request_dir = self.root / request_hash
        if request_dir.exists():
            self._require_request_directory(request_dir)
            return self._load_complete(request_dir, identity)
        if api_key and api_key.encode("utf-8") in identity.request_body:
            raise EvidenceError("request body contains the configured API key")

        self._prepare_root()
        try:
            request_dir.mkdir(mode=0o700)
        except FileExistsError:
            self._require_request_directory(request_dir)
            return self._load_complete(request_dir, identity)
        _fsync_directory(self.root)
        _atomic_write(request_dir / "request.json", request_ledger_bytes(identity))

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "gb10-reranker-equivalence/2",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            result = self.transport(url, identity.request_body, headers, timeout)
        except AmbiguousTransportError as exc:
            message = str(exc)
            if api_key:
                message = message.replace(api_key, "<redacted>")
            attempt = {
                "error": message[:4096],
                "error_type": type(exc).__name__,
                "request_hash": request_hash,
                "schema": "reranker-endpoint-ambiguous-attempt-v2",
            }
            _atomic_write(
                request_dir / f"ambiguous-{time.time_ns()}.json",
                _json_bytes(attempt, pretty=True),
            )
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
            raise EvidenceError(
                "transport returned malformed bounded response evidence"
            )
        if api_key and api_key.encode("utf-8") in result.body:
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
            "schema": "reranker-endpoint-response-v2",
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
    """Validate the documented DeepInfra-native [0, 1] response contract."""

    if expected_count <= 0 or expected_count > 1024:
        raise ValueError("expected response cardinality must be in [1, 1024]")
    payload = _parse_strict_json_object(body, "response")
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
        score = float(value)
        if not PUBLIC_SCORE_MIN <= score <= PUBLIC_SCORE_MAX:
            raise ResponseValidationError(
                "response scores must be in public [0, 1] domain"
            )
        scores.append(score)
    input_tokens = payload["input_tokens"]
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
    ):
        raise ResponseValidationError(
            "response input_tokens must be a non-negative integer"
        )
    request_id = payload.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise ResponseValidationError("response request_id must be non-empty text")
    inference_status = payload.get("inference_status")
    if inference_status is not None and not isinstance(inference_status, (dict, str)):
        raise ResponseValidationError(
            "response inference_status must be an object or string"
        )
    return ValidatedResponse(
        scores=tuple(scores),
        input_tokens=input_tokens,
        request_id=request_id,
        inference_status=inference_status,
        present_fields=tuple(sorted(payload)),
    )


def load_cached_batches(
    identities: Sequence[RequestIdentity], *, cache: EndpointEvidenceCache
) -> list[ValidatedResponse]:
    validated: list[ValidatedResponse] = []
    for identity in identities:
        response = cache.load(identity)
        if not 200 <= response.status < 300:
            raise EndpointHttpError(
                f"cached {identity.provider} response for {response.request_hash} "
                f"has HTTP {response.status}"
            )
        expected, _, _ = _canonical_payload_parts(identity.request_body)
        validated.append(validate_response(response.body, expected))
    return validated


def fetch_endpoint_batches(
    identities: Sequence[RequestIdentity],
    *,
    cache: EndpointEvidenceCache,
    base_url: str,
    api_key: str,
    timeout: float,
) -> list[ValidatedResponse]:
    validated: list[ValidatedResponse] = []
    for identity in identities:
        response = cache.fetch(
            identity, base_url=base_url, api_key=api_key, timeout=timeout
        )
        if not 200 <= response.status < 300:
            raise EndpointHttpError(
                f"{identity.provider} response for {response.request_hash} "
                f"has HTTP {response.status}"
            )
        expected, _, _ = _canonical_payload_parts(identity.request_body)
        validated.append(validate_response(response.body, expected))
    return validated


def _estimated_cost(estimated_tokens: int, price_per_million: float) -> float:
    return estimated_tokens * price_per_million / 1_000_000


def _enforce_cost_cap(
    estimated_tokens: int,
    max_estimated_tokens: int,
    max_cost_usd: float,
    price_per_million: float,
) -> float:
    if (
        estimated_tokens < 0
        or max_estimated_tokens < 0
        or not math.isfinite(max_cost_usd)
        or max_cost_usd < 0
    ):
        raise ValueError("cost plan and caps must be finite and non-negative")
    estimated_cost = _estimated_cost(estimated_tokens, price_per_million)
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
    cache: EndpointEvidenceCache,
    base_url: str,
    api_key: str,
    timeout: float,
    estimated_tokens: int,
    max_estimated_tokens: int,
    max_cost_usd: float,
    cache_only: bool,
    price_per_million: float = 0.05,
) -> list[ValidatedResponse]:
    identities = [request_identity(payload, CLOUD_ENDPOINT) for payload in payloads]
    if cache_only:
        return load_cached_batches(identities, cache=cache)
    _enforce_cost_cap(
        estimated_tokens, max_estimated_tokens, max_cost_usd, price_per_million
    )
    return fetch_endpoint_batches(
        identities,
        cache=cache,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
    )


__all__ = [
    "AmbiguousTransportError",
    "CLOUD_ENDPOINT",
    "CacheMissError",
    "CacheStateError",
    "CorpusValidationError",
    "CostCapError",
    "DEEPINFRA_MODEL_VERSION",
    "ENDPOINT_PATH",
    "EndpointEvidenceCache",
    "EndpointHttpError",
    "EndpointSpec",
    "EquivalenceError",
    "EvidenceError",
    "EvidenceResponse",
    "HttpResult",
    "LOCAL_ENDPOINT",
    "RequestIdentity",
    "ResponseValidationError",
    "ValidatedResponse",
    "_atomic_write",
    "_enforce_cost_cap",
    "_estimated_cost",
    "_json_bytes",
    "_urllib_transport",
    "canonical_payload",
    "canonical_request_hash",
    "endpoint_url",
    "fetch_cloud_batches",
    "fetch_endpoint_batches",
    "load_cached_batches",
    "request_identity",
    "request_ledger_bytes",
    "sanitize_response_headers",
    "validate_response",
]
