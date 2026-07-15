#!/usr/bin/env python3
"""OpenAI-compatible /v1/models + /v1/rerank server for Querit/Querit-4B.

Querit is distributed as `library_name: transformers` with remote code
(`QueritModel` / `MLQwen3Model`). Current AEON vLLM images cannot safely load
the trained head; this server is the official high-compat path until native
vLLM support exists.

Public aliases intentionally match the production Qwen3-Reranker-8B contract so
mempal/verbatim need no config change:
  - qwen3-reranker-8b
  - Qwen/Qwen3-Reranker-8B
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import os
import stat
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoConfig, AutoTokenizer

from querit_score_contract import (
    CURRENT_PROMPT_TERMINAL_CLS_V1,
    LEGACY_PHYSICAL_LAST_V1,
    HeadAttestationError,
    attest_head_load,
    attest_tokenizer,
    pack_prompts,
    render_current_prompt,
    run_learned_head_path,
    scores_from_logits,
    stable_rank,
    validate_unicode_scalar_text,
)

Message = dict[str, Any]
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

LOG = logging.getLogger("querit_rerank")
INFERENCE_LOCK = threading.Lock()

MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 16
MAX_DOCUMENTS = 50
MAX_QUERY_CHARS = 8192
MAX_DOCUMENT_CHARS = 32768
MAX_TOTAL_DOCUMENT_CHARS = 262144
MAX_CHECKPOINT_INDEX_BYTES = 16 * 1024 * 1024

DEFAULT_ALIASES = [
    "qwen3-reranker-8b",
    "Qwen/Qwen3-Reranker-8B",
    "Querit/Querit-4B",
]


class RequestBodyLimitMiddleware:
    """Buffer at most one bounded request body before framework JSON parsing."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def _send_too_large(self, send: Send) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                content_length = int(value)
            except ValueError:
                break
            if content_length > self.max_body_bytes:
                await self._send_too_large(send)
                return
            break

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await self._send_too_large(send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_receive() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)


class RerankRequest(BaseModel):
    model: str | None = None
    query: str
    documents: list[str]
    top_n: int | None = Field(default=None, alias="top_n")

    model_config = {"populate_by_name": True}


class AppState:
    def __init__(self) -> None:
        self.model_id = ""
        self.aliases: list[str] = []
        self.max_model_len = 40960
        self.max_batch = 8
        self.device = "cuda"
        self.tokenizer = None
        self.model = None
        self.score_contract = LEGACY_PHYSICAL_LAST_V1


STATE = AppState()
app = FastAPI(title="Querit-4B OpenAI-compatible rerank server")
app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=MAX_REQUEST_BODY_BYTES)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "score_contract": STATE.score_contract}


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    now = int(time.time())
    data = []
    for name in STATE.aliases:
        data.append(
            {
                "id": name,
                "object": "model",
                "created": now,
                "owned_by": "querit",
                "root": STATE.model_id,
                "max_model_len": STATE.max_model_len,
                "score_contract": STATE.score_contract,
            }
        )
    return {"object": "list", "data": data}


def _tensorize_batch(packed: Any) -> tuple[Any, Any]:
    input_ids = torch.tensor(packed.input_ids).to(STATE.device)
    attention_mask = torch.tensor(packed.attention_mask).to(STATE.device)
    return input_ids, attention_mask


def _extract_legacy_scores(output: Any) -> list[float]:
    """Preserve the exact opaque output path selected by the legacy contract."""

    if isinstance(output, dict) and "score" in output:
        values = output["score"]
    elif hasattr(output, "score") and output.score is not None:
        values = output.score
    elif hasattr(output, "logits") and output.logits is not None:
        logits = output.logits
        return _tensor_scores_from_logits(logits)
    else:
        raise RuntimeError(f"unexpected model output type: {type(output)}")
    return [float(value) for value in values.detach().float().reshape(-1).tolist()]


def _tensor_scores_from_logits(logits: Any) -> list[float]:
    if logits.ndim != 2 or logits.shape[-1] != 2:
        raise RuntimeError(f"unexpected model output shape: {tuple(logits.shape)}")
    probabilities = torch.softmax(logits, dim=-1)
    score_tensor = torch.stack(
        [
            probabilities[row, 1] - probabilities[row, 0]
            for row in range(logits.shape[0])
        ]
    )
    scores = [
        float(value)
        for value in score_tensor.detach().float().reshape(-1).tolist()
    ]
    # Recompute from float32 logits as an independent finite/range assertion;
    # runtime scores remain the model-dtype torch softmax/subtraction above.
    scores_from_logits(logits.detach().float().tolist())
    if any(not math.isfinite(score) or not -1.0 <= score <= 1.0 for score in scores):
        raise RuntimeError("learned head returned an invalid score")
    return scores


def _gather_hidden_rows(hidden: Any, positions: list[int]) -> Any:
    return torch.stack([hidden[row, position] for row, position in enumerate(positions)])


@torch.inference_mode()
def score_pairs(query: str, documents: list[str]) -> list[float]:
    assert STATE.model is not None and STATE.tokenizer is not None
    scores: list[float] = []
    for i in range(0, len(documents), STATE.max_batch):
        batch_docs = documents[i : i + STATE.max_batch]
        prompts = [render_current_prompt(query, document) for document in batch_docs]
        packed = pack_prompts(
            STATE.tokenizer,
            prompts,
            STATE.score_contract,
            max_model_length=STATE.max_model_len,
        )
        input_ids, attention_mask = _tensorize_batch(packed)
        if STATE.score_contract == LEGACY_PHYSICAL_LAST_V1:
            output = STATE.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
            )
            batch_scores = _extract_legacy_scores(output)
        else:
            logits, _positions = run_learned_head_path(
                backbone=lambda **_ignored: STATE.model.model(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                ),
                head=STATE.model.head,
                gather_rows=_gather_hidden_rows,
                input_ids=packed.input_ids,
                attention_mask=packed.attention_mask,
                score_contract=STATE.score_contract,
            )
            batch_scores = _tensor_scores_from_logits(logits)
        scores.extend(batch_scores)
    return scores


@app.post("/v1/rerank")
def rerank(req: RerankRequest) -> dict[str, Any]:
    model_name = req.model or STATE.aliases[0]
    if model_name not in STATE.aliases and model_name != STATE.model_id:
        raise HTTPException(status_code=404, detail="model not found")
    if not req.query:
        raise HTTPException(status_code=400, detail="query must be non-empty")
    if len(req.query) > MAX_QUERY_CHARS:
        raise HTTPException(status_code=400, detail="query is too long")
    if not req.documents:
        raise HTTPException(status_code=400, detail="documents must be non-empty")
    if len(req.documents) > MAX_DOCUMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"documents must contain at most {MAX_DOCUMENTS} items",
        )
    if any(not document for document in req.documents):
        raise HTTPException(status_code=400, detail="documents must be non-empty strings")
    if any(len(document) > MAX_DOCUMENT_CHARS for document in req.documents):
        raise HTTPException(status_code=400, detail="document is too long")
    if sum(len(document) for document in req.documents) > MAX_TOTAL_DOCUMENT_CHARS:
        raise HTTPException(
            status_code=400, detail="aggregate document input is too long"
        )
    try:
        validate_unicode_scalar_text(req.query)
        for document in req.documents:
            validate_unicode_scalar_text(document)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="input contains invalid Unicode"
        ) from exc
    if req.top_n is not None and req.top_n <= 0:
        raise HTTPException(status_code=400, detail="top_n must be positive")
    if req.top_n is not None and req.top_n > MAX_DOCUMENTS:
        raise HTTPException(status_code=400, detail="top_n is too large")

    try:
        with INFERENCE_LOCK:
            scores = score_pairs(req.query, req.documents)
        if len(scores) != len(req.documents):
            raise RuntimeError("model returned an unexpected score count")
        if not all(math.isfinite(score) for score in scores):
            raise RuntimeError("model returned a non-finite score")
    except Exception as exc:  # noqa: BLE001
        LOG.exception("rerank failed")
        raise HTTPException(status_code=500, detail="rerank inference failed") from exc

    ranked = stable_rank(scores)
    top_n = req.top_n if req.top_n is not None else len(ranked)
    top_n = min(top_n, len(ranked))
    results = [
        {
            "index": i,
            "document_index": i,
            "score": scores[i],
            "relevance_score": scores[i],
        }
        for i in ranked[:top_n]
    ]
    return {
        "id": f"rerank-{uuid.uuid4().hex}",
        "model": model_name,
        "results": results,
        "data": results,
    }


def _resolve_local_dir(model_path: str) -> str:
    path = Path(model_path).resolve(strict=True)
    if not path.is_dir() or not (path / "config.json").is_file():
        raise RuntimeError(f"model path is not a local snapshot: {path}")
    return str(path)


def _read_bounded_regular_file(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"required local model file is not regular: {path.name}")
        if metadata.st_size > maximum_bytes:
            raise RuntimeError(f"required local model file is too large: {path.name}")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum_bytes:
            raise RuntimeError(f"required local model file is too large: {path.name}")
        return content
    finally:
        os.close(descriptor)


def _checkpoint_head_keys(local: str) -> set[str]:
    index_path = Path(local) / "model.safetensors.index.json"
    try:
        payload = json.loads(
            _read_bounded_regular_file(
                index_path, MAX_CHECKPOINT_INDEX_BYTES
            ).decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("could not read the local checkpoint index") from exc
    weight_map = payload.get("weight_map") if isinstance(payload, dict) else None
    if not isinstance(weight_map, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in weight_map.items()
    ):
        raise RuntimeError("local checkpoint index has an invalid weight map")
    return {key for key in weight_map if key == "head" or key.startswith("head.")}


def _attest_loaded_head(
    model: Any, loading_info: dict[str, Any], checkpoint_keys: set[str]
) -> None:
    head = getattr(model, "head", None)
    if head is None or getattr(head, "weight", None) is None:
        raise HeadAttestationError(
            f"loaded {type(model).__name__} without the learned Querit head"
        )
    if getattr(head, "bias", None) is None:
        raise HeadAttestationError("loaded Querit head has no bias")
    attest_head_load(
        checkpoint_keys=checkpoint_keys,
        loading_info=loading_info,
        weight_shape=tuple(head.weight.shape),
        bias_shape=tuple(head.bias.shape),
    )


def load_model(model_path: str, dtype: str) -> None:
    LOG.info("loading tokenizer/model from %s", model_path)
    local = _resolve_local_dir(model_path)
    STATE.tokenizer = AutoTokenizer.from_pretrained(
        local,
        trust_remote_code=True,
        local_files_only=True,
        padding_side="right",
    )
    attest_tokenizer(STATE.tokenizer)
    torch_dtype = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }[dtype]

    # Querit ships architectures=["MLQwen3Model"] with class QueritModel and
    # no auto_map. AutoModel.from_pretrained loads a plain Qwen3 backbone and
    # drops head.bias/head.weight as UNEXPECTED — always load QueritModel.
    mod_path = Path(local) / "modeling_querit_4b.py"
    if not mod_path.exists():
        raise RuntimeError(f"missing modeling_querit_4b.py under {local}")
    spec = importlib.util.spec_from_file_location("modeling_querit_4b", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = AutoConfig.from_pretrained(
        local, trust_remote_code=True, local_files_only=True
    )
    # QueritModel.__init__(use_lm_head=False) sets lm_head=None which breaks
    # transformers tied-weight loading. Keep lm_head for load, drop after.
    loaded = mod.QueritModel.from_pretrained(
        local,
        config=cfg,
        torch_dtype=torch_dtype,
        trust_remote_code=True,
        local_files_only=True,
        use_lm_head=True,
        output_loading_info=True,
    )
    if not isinstance(loaded, tuple) or len(loaded) != 2:
        raise RuntimeError("Transformers did not return strict model loading info")
    model, loading_info = loaded
    if not isinstance(loading_info, dict):
        raise RuntimeError("Transformers returned malformed model loading info")
    _attest_loaded_head(model, loading_info, _checkpoint_head_keys(local))
    STATE.model = model
    # Free unused generation head once weights are loaded.
    if hasattr(STATE.model, "lm_head") and STATE.model.lm_head is not None:
        STATE.model.lm_head = None

    STATE.model.eval()
    STATE.model.to(STATE.device)
    LOG.info(
        "model loaded on %s dtype=%s class=%s",
        STATE.device,
        dtype,
        type(STATE.model).__name__,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.environ.get("QUERIT_MODEL", "Querit/Querit-4B"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("QUERIT_PORT", "8000")))
    parser.add_argument(
        "--max-model-len",
        type=int,
        default=int(os.environ.get("QUERIT_MAX_MODEL_LEN", "40960")),
    )
    parser.add_argument(
        "--max-batch",
        type=int,
        default=int(os.environ.get("QUERIT_MAX_BATCH", "8")),
    )
    parser.add_argument("--dtype", default=os.environ.get("QUERIT_DTYPE", "bfloat16"))
    parser.add_argument(
        "--score-contract",
        choices=(LEGACY_PHYSICAL_LAST_V1, CURRENT_PROMPT_TERMINAL_CLS_V1),
        default=os.environ.get("QUERIT_SCORE_CONTRACT", LEGACY_PHYSICAL_LAST_V1),
        help="Explicit scoring contract; legacy remains the selected default",
    )
    parser.add_argument(
        "--served-model-name",
        nargs="+",
        default=None,
        help="Public aliases (default includes legacy qwen3-reranker-8b names)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    STATE.model_id = args.model
    STATE.aliases = args.served_model_name or DEFAULT_ALIASES
    STATE.max_model_len = args.max_model_len
    STATE.max_batch = args.max_batch
    STATE.score_contract = args.score_contract
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required for Querit rerank server")
    STATE.device = "cuda"

    load_model(args.model, args.dtype)
    LOG.info(
        "serving aliases=%s max_model_len=%s score_contract=%s",
        STATE.aliases,
        STATE.max_model_len,
        STATE.score_contract,
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        limit_concurrency=MAX_CONCURRENT_REQUESTS,
    )


if __name__ == "__main__":
    main()
