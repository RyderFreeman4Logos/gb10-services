#!/usr/bin/env python3
"""Opt-in, loopback-only CPU canary; never changes the shared embedding route."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import signal
import socket
import time
from types import FrameType
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

__all__ = ["MODEL", "REVISION", "parse_request", "validate_vectors"]
MODEL = "multilingual-e5-large-instruct-1024"
REVISION = "274baa43b0e13e37fafa6428dbc7938e62e5c439"
WEIGHTS_SHA256 = "dd6b6e4f52db0a7aff83a13d10e6c5342ef9f6ab799bad3221f4b35ef390fa85"


def parse_request(payload: object) -> list[str]:
    """Accept only explicit native-1024 float embeddings, at most two texts."""
    if not isinstance(payload, dict) or set(payload) - {"model", "input", "dimensions", "encoding_format"}:
        raise ValueError("unsupported request fields")
    if payload.get("model") != MODEL:
        raise ValueError("wrong model")
    if "dimensions" in payload and (type(payload["dimensions"]) is not int or payload["dimensions"] != 1024):
        raise ValueError("only native dimension 1024 is supported")
    if payload.get("encoding_format", "float") != "float":
        raise ValueError("only float encoding is supported")
    texts = payload.get("input")
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list) or not 1 <= len(texts) <= 2 or any(not isinstance(t, str) or not t.strip() for t in texts):
        raise ValueError("input must contain one or two nonempty strings")
    return texts


def validate_vectors(vectors: list[list[float]], count: int) -> None:
    """Reject wrong shapes, nonfinite outputs and non-unit norms; never slice."""
    if len(vectors) != count or any(len(v) != 1024 or not all(math.isfinite(x) for x in v) or abs(math.sqrt(sum(x*x for x in v)) - 1) > 1e-5 for v in vectors):
        raise ValueError("native vector contract failed")


def _available() -> int:
    return int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))) * 1024


def _request_timeout(signum: int, frame: FrameType | None) -> None:
    raise TimeoutError("absolute canary request deadline")


class _BoundedHTTPServer(HTTPServer):
    deadline: float

    def finish_request(self, request: object, client_address: tuple[str, int]) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("canary requires a TCP socket")
        request.settimeout(5)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, min(30, self.deadline - time.monotonic())))
        try:
            super().finish_request(request, client_address)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


def main() -> None:
    """Serve a pinned, offline model for a bounded synthetic-only canary window."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18016)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--container-publish-loopback", action="store_true", help="bind container interface; requires Docker -p 127.0.0.1:18016:18016, never host networking")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1 <= args.seconds <= 900:
        parser.error("invalid port or lifetime")
    path = args.model_path
    if path.name != REVISION or _available() < 11 * 1024**3:
        raise RuntimeError("immutable model pin or 11 GiB startup admission failed")
    config = json.loads((path / "config.json").read_text())
    if config.get("hidden_size") != 1024 or config.get("architectures") != ["XLMRobertaModel"]:
        raise RuntimeError("not the native 1024 XLM-R checkpoint")
    with (path / "model.safetensors").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != WEIGHTS_SHA256:
        raise RuntimeError("weights hash mismatch")
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(2)
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(path, local_files_only=True, trust_remote_code=False, dtype=torch.float32).eval()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            pass  # Synthetic-only canary: do not log request bodies or paths.

        def reply(self, status: int, data: object) -> None:
            body = json.dumps(data, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.reply(200, {"status": "ok", "model": MODEL, "revision": REVISION})
            elif self.path == "/v1/models":
                self.reply(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "intfloat", "revision": REVISION, "embedding_dimension": 1024, "max_model_len": 512}]})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/v1/embeddings":
                self.reply(404, {"error": "not found"})
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("chunked bodies are unsupported")
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 32768:
                    raise ValueError("body must be 1..32768 bytes")
                self.connection.settimeout(5)
                texts = parse_request(json.loads(self.rfile.read(length)))
                batch = tokenizer(texts, padding=True, truncation=False, return_tensors="pt")
                if batch["input_ids"].shape[1] > 512:
                    raise ValueError("input exceeds native 512-token limit; no silent truncation")
                if _available() < 6 * 1024**3:
                    self.reply(503, {"error": "6 GiB host headroom floor"})
                    return
                with torch.inference_mode():
                    hidden = model(**batch).last_hidden_state
                    hidden = hidden.masked_fill(~batch["attention_mask"][..., None].bool(), 0.0)
                    pooled = hidden.sum(1) / batch["attention_mask"].sum(1)[..., None]
                    vectors = torch.nn.functional.normalize(pooled, p=2, dim=1).tolist()
                validate_vectors(vectors, len(texts))
                self.reply(200, {"object": "list", "model": MODEL, "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)], "usage": {"prompt_tokens": int(batch["attention_mask"].sum()), "total_tokens": int(batch["attention_mask"].sum())}})
            except (ValueError, UnicodeDecodeError, TimeoutError):
                self.reply(400, {"error": "invalid request or native vector contract"})

    # ponytail: single synchronous canary worker; use a production engine after corpus acceptance.
    signal.signal(signal.SIGALRM, _request_timeout)
    with _BoundedHTTPServer(("0.0.0.0" if args.container_publish_loopback else "127.0.0.1", args.port), Handler) as server:
        server.timeout = 1
        deadline = server.deadline = time.monotonic() + args.seconds
        print(json.dumps({"ready": True, "model": MODEL, "revision": REVISION, "weights_sha256": digest}), flush=True)
        while time.monotonic() < deadline and _available() >= 6 * 1024**3:
            server.handle_request()


if __name__ == "__main__":
    main()
