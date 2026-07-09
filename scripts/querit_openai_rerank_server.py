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
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoConfig, AutoModel, AutoTokenizer

LOG = logging.getLogger("querit_rerank")

DEFAULT_ALIASES = [
    "qwen3-reranker-8b",
    "Qwen/Qwen3-Reranker-8B",
    "Querit/Querit-4B",
]


def build_pair_prompt(query: str, document: str) -> str:
    # Cross-encoder pair format aligned with Qwen3-Reranker-style judges.
    return (
        "<|im_start|>system\n"
        "Judge whether the Document meets the requirements based on the Query "
        'and the Instruct provided. Note that the answer can only be "yes" or "no".'
        "<|im_end|>\n"
        "<|im_start|>user\n"
        "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n"
        f"<Query>: {query}\n"
        f"<Document>: {document}"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


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


STATE = AppState()
app = FastAPI(title="Querit-4B OpenAI-compatible rerank server")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
            }
        )
    return {"object": "list", "data": data}


@torch.inference_mode()
def score_pairs(query: str, documents: list[str]) -> list[float]:
    assert STATE.model is not None and STATE.tokenizer is not None
    scores: list[float] = []
    for i in range(0, len(documents), STATE.max_batch):
        batch_docs = documents[i : i + STATE.max_batch]
        texts = [build_pair_prompt(query, d) for d in batch_docs]
        enc = STATE.tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=STATE.max_model_len,
            return_tensors="pt",
        )
        enc = {k: v.to(STATE.device) for k, v in enc.items()}
        out = STATE.model(
            input_ids=enc["input_ids"],
            attention_mask=enc["attention_mask"],
        )
        if isinstance(out, dict) and "score" in out:
            batch_scores = out["score"].detach().float().reshape(-1).tolist()
        elif hasattr(out, "logits"):
            logits = out.logits
            if logits.ndim == 2 and logits.shape[-1] == 2:
                probs = torch.softmax(logits, dim=-1)
                weights = torch.tensor([-1.0, 1.0], device=probs.device)
                batch_scores = (probs * weights).sum(dim=-1).float().tolist()
            else:
                raise RuntimeError(f"unexpected model output shape: {tuple(logits.shape)}")
        else:
            raise RuntimeError(f"unexpected model output type: {type(out)}")
        scores.extend(float(x) for x in batch_scores)
    return scores


@app.post("/v1/rerank")
def rerank(req: RerankRequest) -> dict[str, Any]:
    model_name = req.model or STATE.aliases[0]
    if model_name not in STATE.aliases and model_name != STATE.model_id:
        raise HTTPException(status_code=404, detail=f"model not found: {model_name}")
    if not req.documents:
        raise HTTPException(status_code=400, detail="documents must be non-empty")

    try:
        scores = score_pairs(req.query, req.documents)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("rerank failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    indexed = list(enumerate(scores))
    indexed.sort(key=lambda x: x[1], reverse=True)
    top_n = req.top_n if req.top_n is not None else len(indexed)
    top_n = max(0, min(top_n, len(indexed)))
    results = [
        {
            "index": i,
            "document_index": i,
            "score": s,
            "relevance_score": s,
        }
        for i, s in indexed[:top_n]
    ]
    return {
        "id": f"rerank-{uuid.uuid4().hex}",
        "model": model_name,
        "results": results,
        "data": results,
    }


def _resolve_local_dir(model_path: str) -> str:
    p = Path(model_path)
    if (p / "config.json").exists():
        return str(p)
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(model_path, local_files_only=True)
    except Exception:
        return model_path


def load_model(model_path: str, dtype: str) -> None:
    LOG.info("loading tokenizer/model from %s", model_path)
    STATE.tokenizer = AutoTokenizer.from_pretrained(
        model_path, trust_remote_code=True, padding_side="right"
    )
    torch_dtype = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }[dtype]

    # Querit often ships architectures=["MLQwen3Model"] with class QueritModel
    # and no auto_map. Prefer remote-code AutoModel; fall back to explicit class.
    try:
        STATE.model = AutoModel.from_pretrained(
            model_path,
            trust_remote_code=True,
            torch_dtype=torch_dtype,
        )
    except Exception as auto_exc:  # noqa: BLE001
        LOG.warning("AutoModel failed (%s); trying explicit QueritModel import", auto_exc)
        local = _resolve_local_dir(model_path)
        mod_path = Path(local) / "modeling_querit_4b.py"
        if not mod_path.exists():
            raise RuntimeError(f"missing modeling_querit_4b.py under {local}") from auto_exc
        spec = importlib.util.spec_from_file_location("modeling_querit_4b", mod_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg = AutoConfig.from_pretrained(local, trust_remote_code=True)
        STATE.model = mod.QueritModel.from_pretrained(
            local, config=cfg, torch_dtype=torch_dtype, trust_remote_code=True
        )

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
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required for Querit rerank server")
    STATE.device = "cuda"

    load_model(args.model, args.dtype)
    LOG.info("serving aliases=%s max_model_len=%s", STATE.aliases, STATE.max_model_len)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
