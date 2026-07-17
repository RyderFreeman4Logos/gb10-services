#!/usr/bin/env -S uv run --python 3.11 --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pyarrow==20.0.0"]
# ///
"""Materialize the pinned MIRACLReranking English/Chinese equivalence corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as parquet


__all__ = ["main", "prepare_corpus"]


DATASET = "mteb/MIRACLReranking"
REVISION = "ab6f54eff185a84bc1f6ab96b56bc7df87433228"
LICENSE = "CC-BY-SA-4.0"
GROUPS_PER_LANGUAGE = 100
CANDIDATES_PER_GROUP = 10
LANGUAGES = ("en", "zh")


@dataclass(frozen=True)
class SourceFile:
    path: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return (
            f"https://huggingface.co/datasets/{DATASET}/resolve/"
            f"{REVISION}/{self.path}?download=true"
        )


SOURCES = (
    SourceFile(
        "en-corpus/dev-00000-of-00001.parquet",
        "cf0679ffc34fd67a14c68554b446618c353e7f0a8c3cb6ce1c31b14f9b765416",
        30_024_859,
    ),
    SourceFile(
        "en-qrels/dev-00000-of-00001.parquet",
        "925bacac253aa59680f308c2779367bc31e8189e569df5519880e623b489c7e4",
        500_585,
    ),
    SourceFile(
        "en-queries/dev-00000-of-00001.parquet",
        "79265c85080b101de7abd590cc6aecbafa14cf0001fa778f7c2eeac1928c7734",
        28_887,
    ),
    SourceFile(
        "en-top_ranked/dev-00000-of-00001.parquet",
        "f549fd8a3746546f37095083a9ce006f7348e09fab4da2b471b57703cf538c01",
        410_185,
    ),
    SourceFile(
        "zh-corpus/dev-00000-of-00001.parquet",
        "c0fdd6d7b6b7ca30dddc3bb2fe761433fc507c7eb9d99e502985faef5bc8d1a6",
        10_998_069,
    ),
    SourceFile(
        "zh-qrels/dev-00000-of-00001.parquet",
        "23d7986206a286f23683da48d42771bee8c61cb6e7e3cce8586625bb0f64999f",
        249_243,
    ),
    SourceFile(
        "zh-queries/dev-00000-of-00001.parquet",
        "f5c75b9f96531886dc906097d8451f508921c22850076be5820d4f51d8b6b7f2",
        12_509,
    ),
    SourceFile(
        "zh-top_ranked/dev-00000-of-00001.parquet",
        "023a96094f80d889f8fe026449c3fb528cb276ef8e463da7b4f0335eb8be2ecb",
        238_646,
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _download(source: SourceFile, cache_root: Path) -> Path:
    destination = cache_root / source.path
    if destination.exists():
        if destination.stat().st_size != source.size or _sha256(destination) != source.sha256:
            raise RuntimeError(f"cached source differs from pinned identity: {source.path}")
        return destination
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.download")
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(source.url, timeout=120) as response, temporary.open(
            "xb"
        ) as sink:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > source.size:
                    raise RuntimeError(f"source exceeds pinned size: {source.path}")
                digest.update(chunk)
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        if total != source.size or digest.hexdigest() != source.sha256:
            raise RuntimeError(f"downloaded source differs from pinned identity: {source.path}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _rows(source: Path) -> list[dict[str, Any]]:
    return parquet.read_table(source).to_pylist()


def _selection_key(language: str, query_id: str) -> bytes:
    return hashlib.sha256(
        f"miracl-reranker-equivalence-v1\0{REVISION}\0{language}\0{query_id}".encode()
    ).digest()


def _language_rows(language: str, cache_root: Path) -> list[dict[str, object]]:
    by_suffix = {
        source.path.split("/", 1)[0].removeprefix(f"{language}-"): cache_root
        / source.path
        for source in SOURCES
        if source.path.startswith(f"{language}-")
    }
    corpus_rows = _rows(by_suffix["corpus"])
    query_rows = _rows(by_suffix["queries"])
    qrel_rows = _rows(by_suffix["qrels"])
    ranked_rows = _rows(by_suffix["top_ranked"])

    documents = {row["_id"]: row["text"] for row in corpus_rows}
    queries = {row["_id"]: row["text"] for row in query_rows}
    relevance: dict[str, dict[str, int]] = {}
    for row in qrel_rows:
        relevance.setdefault(row["query-id"], {})[row["corpus-id"]] = int(row["score"])
    top_ranked = {row["query-id"]: list(row["corpus-ids"]) for row in ranked_rows}

    valid: list[dict[str, object]] = []
    for query_id, query in queries.items():
        ranked = top_ranked.get(query_id)
        labels = relevance.get(query_id)
        if not isinstance(query, str) or not query or not ranked or not labels:
            continue
        positive = next(
            (
                (rank, document_id)
                for rank, document_id in enumerate(ranked, 1)
                if labels.get(document_id, 0) > 0
                and isinstance(documents.get(document_id), str)
                and documents[document_id]
            ),
            None,
        )
        negatives = [
            (rank, document_id)
            for rank, document_id in enumerate(ranked, 1)
            if labels.get(document_id) == 0
            and isinstance(documents.get(document_id), str)
            and documents[document_id]
        ][: CANDIDATES_PER_GROUP - 1]
        if positive is None or len(negatives) != CANDIDATES_PER_GROUP - 1:
            continue
        selected = sorted([positive, *negatives])
        valid.append(
            {
                "candidates": [
                    {
                        "document": documents[document_id],
                        "document_id": document_id,
                        "relevance": labels[document_id],
                        "source_language": language,
                        "top_ranked_rank": rank,
                    }
                    for rank, document_id in selected
                ],
                "query": query,
                "query_id": query_id,
                "source_language": language,
            }
        )
    valid.sort(key=lambda row: _selection_key(language, str(row["query_id"])))
    if len(valid) < GROUPS_PER_LANGUAGE:
        raise RuntimeError(
            f"{language} has only {len(valid)} valid public query groups; "
            f"need {GROUPS_PER_LANGUAGE}"
        )
    return valid[:GROUPS_PER_LANGUAGE]


def prepare_corpus(cache_root: Path, output_root: Path) -> None:
    """Download, attest, select, and atomically write the committed corpus."""

    for source in SOURCES:
        _download(source, cache_root)
    rows = [
        row
        for language in LANGUAGES
        for row in _language_rows(language, cache_root)
    ]
    corpus = b"".join(
        (
            json.dumps(
                row,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        for row in rows
    )
    corpus_name = "miracl-reranking-en-zh-dev.jsonl"
    _atomic_write(output_root / corpus_name, corpus)
    metadata = {
        "candidates_per_group": CANDIDATES_PER_GROUP,
        "citations": [
            {
                "doi": "10.1162/tacl_a_00595",
                "title": "MIRACL: A Multilingual Retrieval Dataset Covering 18 Diverse Languages",
            },
            {
                "doi": "10.48550/arXiv.2502.13595",
                "title": "MMTEB: Massive Multilingual Text Embedding Benchmark",
            },
            {
                "doi": "10.48550/ARXIV.2210.07316",
                "title": "MTEB: Massive Text Embedding Benchmark",
            },
        ],
        "corpus_file": corpus_name,
        "corpus_sha256": hashlib.sha256(corpus).hexdigest(),
        "dataset": DATASET,
        "dataset_url": f"https://huggingface.co/datasets/{DATASET}/tree/{REVISION}",
        "groups_per_language": GROUPS_PER_LANGUAGE,
        "languages": list(LANGUAGES),
        "license": LICENSE,
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "revision": REVISION,
        "source_card_url": (
            f"https://huggingface.co/datasets/{DATASET}/blob/{REVISION}/README.md"
        ),
        "selection": {
            "candidate_rule": (
                "For each valid query, take the first qrels-positive candidate and "
                "the first nine qrels-zero hard negatives from top_ranked, then "
                "restore top_ranked order. No text is generated or rewritten."
            ),
            "query_rule": (
                "Sort valid query groups by SHA-256 of the fixed selection domain, "
                "revision, language, and query_id; take the first 100 per language."
            ),
            "selection_domain": "miracl-reranker-equivalence-v1",
        },
        "source_files": [
            {
                "path": source.path,
                "sha256": source.sha256,
                "size": source.size,
                "url": source.url,
            }
            for source in SOURCES
        ],
        "split": "dev",
    }
    _atomic_write(
        output_root / "metadata.json",
        (
            json.dumps(
                metadata,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cache", type=Path, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "data"
        / "reranker-equivalence",
    )
    args = parser.parse_args()
    prepare_corpus(args.source_cache, args.output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
