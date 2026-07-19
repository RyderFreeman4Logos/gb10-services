#!/usr/bin/env python3
"""Expose Querit vLLM scoring through the pinned DeepInfra reranker wire API."""

from __future__ import annotations

import argparse
import json
import math
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = [
    "AdapterError",
    "BackendResponse",
    "BoundedThreadingHTTPServer",
    "PublicRequest",
    "backend_request_bytes",
    "main",
    "parse_backend_response",
    "parse_public_request",
    "public_response_bytes",
    "valid_public_target",
]


PUBLIC_PATH = "/v1/inference/Qwen/Qwen3-Reranker-8B"
PUBLIC_VERSION = "5fa94080caafeaa45a15d11f969d7978e087a3db"
BACKEND_MODEL = "Querit/Querit-4B"
DEFAULT_INSTRUCTION = (
    "Given a web search query, retrieve relevant passages that answer the query"
)
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_CONCURRENCY = 4
MAX_CONCURRENCY = 64


class AdapterError(RuntimeError):
    """The public request or native vLLM response violates its pinned schema."""


@dataclass(frozen=True)
class PublicRequest:
    queries: tuple[str, ...]
    documents: tuple[str, ...]
    instruction: str | None
    service_tier: str | None


@dataclass(frozen=True)
class BackendResponse:
    scores: tuple[float, ...]
    input_tokens: int
    request_id: str


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that acquires admission before spawning a handler."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        max_concurrency: int,
    ) -> None:
        if not 1 <= max_concurrency <= MAX_CONCURRENCY:
            raise ValueError(
                f"max concurrency must be in [1, {MAX_CONCURRENCY}]"
            )
        self._handler_slots = threading.BoundedSemaphore(max_concurrency)
        super().__init__(server_address, request_handler_class)

    def process_request(self, request: Any, client_address: Any) -> None:
        self._handler_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _strict_object(body: bytes, label: str) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise ValueError(value)

    try:
        decoded = json.loads(body, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AdapterError(f"{label} is not strict JSON") from exc
    if not isinstance(decoded, dict):
        raise AdapterError(f"{label} must be an object")
    return decoded


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{label} must be non-empty text")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise AdapterError(f"{label} contains an unpaired surrogate")
    return value


def parse_public_request(body: bytes) -> PublicRequest:
    payload = _strict_object(body, "public request")
    required = {"documents", "queries"}
    allowed = required | {"instruction", "service_tier"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise AdapterError("public request fields do not match the pinned schema")
    if _json_bytes(payload) != body:
        raise AdapterError("public request is not the canonical byte serialization")
    queries = payload["queries"]
    documents = payload["documents"]
    if (
        not isinstance(queries, list)
        or not isinstance(documents, list)
        or not queries
        or len(queries) != len(documents)
        or len(queries) > 1024
    ):
        raise AdapterError("queries/documents cardinality must match in [1, 1024]")
    parsed_queries = tuple(_text(value, "query") for value in queries)
    parsed_documents = tuple(_text(value, "document") for value in documents)
    instruction = payload.get("instruction")
    service_tier = payload.get("service_tier")
    if instruction is not None:
        instruction = _text(instruction, "instruction")
    if service_tier is not None:
        service_tier = _text(service_tier, "service_tier")
    return PublicRequest(
        queries=parsed_queries,
        documents=parsed_documents,
        instruction=instruction,
        service_tier=service_tier,
    )


def backend_request_bytes(request: PublicRequest) -> bytes:
    payload: dict[str, object] = {
        "documents": list(request.documents),
        "instruction": request.instruction or DEFAULT_INSTRUCTION,
        "model": BACKEND_MODEL,
        "queries": list(request.queries),
        "use_activation": True,
    }
    return _json_bytes(payload)


def parse_backend_response(body: bytes, expected_count: int) -> BackendResponse:
    if not 1 <= expected_count <= 1024:
        raise ValueError("expected backend cardinality must be in [1, 1024]")
    payload = _strict_object(body, "vLLM score response")
    if set(payload) != {"created", "data", "id", "model", "object", "usage"}:
        raise AdapterError("vLLM score response fields are not exact")
    request_id = _text(payload["id"], "vLLM request id")
    if payload["object"] != "list" or payload["model"] != BACKEND_MODEL:
        raise AdapterError("vLLM response object or model differs from contract")
    created = payload["created"]
    if isinstance(created, bool) or not isinstance(created, int) or created < 0:
        raise AdapterError("vLLM created timestamp is invalid")
    data = payload["data"]
    if not isinstance(data, list) or len(data) != expected_count:
        raise AdapterError("vLLM score cardinality differs from public request")
    transformed: list[float] = []
    for expected_index, entry in enumerate(data):
        if not isinstance(entry, dict) or set(entry) != {"index", "object", "score"}:
            raise AdapterError("vLLM score entry fields are not exact")
        if entry["index"] != expected_index or entry["object"] != "score":
            raise AdapterError("vLLM score positions are not exact and ordered")
        value = entry["score"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise AdapterError("vLLM raw scores must be finite numbers")
        score = float(value)
        if not -1.0 <= score <= 1.0:
            raise AdapterError("vLLM raw scores must be in the tanh [-1, 1] domain")
        transformed.append((score + 1.0) / 2.0)
    usage = payload["usage"]
    if not isinstance(usage, dict) or set(usage) != {
        "completion_tokens",
        "prompt_tokens",
        "prompt_tokens_details",
        "total_tokens",
    }:
        raise AdapterError("vLLM usage fields are not exact")
    completion_tokens = usage["completion_tokens"]
    prompt_tokens = usage["prompt_tokens"]
    prompt_tokens_details = usage["prompt_tokens_details"]
    total_tokens = usage["total_tokens"]
    if (
        isinstance(completion_tokens, bool)
        or not isinstance(completion_tokens, int)
        or completion_tokens != 0
        or isinstance(prompt_tokens, bool)
        or not isinstance(prompt_tokens, int)
        or prompt_tokens < 0
        or prompt_tokens_details is not None
        or isinstance(total_tokens, bool)
        or not isinstance(total_tokens, int)
        or total_tokens != prompt_tokens
    ):
        raise AdapterError("vLLM token usage is invalid")
    return BackendResponse(tuple(transformed), prompt_tokens, request_id)


def public_response_bytes(response: BackendResponse) -> bytes:
    return _json_bytes(
        {
            "input_tokens": response.input_tokens,
            "request_id": response.request_id,
            "scores": list(response.scores),
        }
    )


def valid_public_target(path: str, query: str) -> bool:
    return path == PUBLIC_PATH and query == f"version={PUBLIC_VERSION}"


def _backend_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("backend URL must be a credential-free loopback HTTP origin")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/score", "", ""))


def _call_backend(url: str, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != HTTPStatus.OK:
                raise AdapterError(f"vLLM backend returned HTTP {response.status}")
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError("vLLM backend transport failed") from exc
    if len(response_body) > MAX_RESPONSE_BYTES:
        raise AdapterError("vLLM backend response exceeded the bounded read limit")
    return response_body


def _handler(backend_url: str, timeout: float) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "gb10-querit-adapter/1"
        sys_version = ""

        def do_POST(self) -> None:
            parsed_target = urllib.parse.urlsplit(self.path)
            if not valid_public_target(parsed_target.path, parsed_target.query):
                self._error(HTTPStatus.NOT_FOUND, "unsupported target")
                return
            content_type = self.headers.get_content_type()
            content_length = self.headers.get("Content-Length")
            try:
                length = int(content_length or "")
            except ValueError:
                length = -1
            if (
                content_type != "application/json"
                or not 0 < length <= MAX_REQUEST_BYTES
            ):
                self._error(HTTPStatus.BAD_REQUEST, "invalid content metadata")
                return
            previous_timeout = self.connection.gettimeout()
            body_timed_out = False
            try:
                self.connection.settimeout(REQUEST_BODY_TIMEOUT_SECONDS)
                body = self.rfile.read(length)
            except TimeoutError:
                body = b""
                body_timed_out = True
            finally:
                self.connection.settimeout(previous_timeout)
            if body_timed_out:
                self.close_connection = True
                self._error(HTTPStatus.REQUEST_TIMEOUT, "request body timed out")
                return
            if len(body) != length:
                self._error(HTTPStatus.BAD_REQUEST, "invalid request length")
                return
            try:
                public_request = parse_public_request(body)
                backend_response = _call_backend(
                    backend_url, backend_request_bytes(public_request), timeout
                )
                response = public_response_bytes(
                    parse_backend_response(
                        backend_response, len(public_request.queries)
                    )
                )
            except AdapterError:
                self._error(HTTPStatus.BAD_GATEWAY, "adapter contract failed closed")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response)

        def _error(self, status: HTTPStatus, message: str) -> None:
            response = _json_bytes({"error": message})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            super().log_message(format, *args)

    return Handler


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--backend-timeout", type=float, default=120.0)
    parser.add_argument(
        "--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if (
        not 1 <= args.listen_port <= 65535
        or not math.isfinite(args.backend_timeout)
        or args.backend_timeout <= 0
        or not 1 <= args.max_concurrency <= MAX_CONCURRENCY
    ):
        raise SystemExit(
            "listen port, backend timeout, and max concurrency are invalid"
        )
    backend_url = _backend_url(args.backend_url)
    server = BoundedThreadingHTTPServer(
        (args.listen_host, args.listen_port),
        _handler(backend_url, args.backend_timeout),
        max_concurrency=args.max_concurrency,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
