from __future__ import annotations

import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

collector = importlib.import_module("collect_reranker_cloud_baseline")


def _group() -> dict[str, object]:
    return {
        "query_id": "query-0",
        "query": "q",
        "source_language": "en",
        "candidates": [
            {
                "document": "d",
                "document_id": "document-0",
                "relevance": 1,
                "source_language": "en",
                "top_ranked_rank": 1,
            }
        ],
    }


def _write_corpus(path: Path) -> dict[str, object]:
    group = _group()
    path.write_text(json.dumps(group) + "\n", encoding="utf-8")
    return group


def _arguments(corpus: Path, output: Path) -> list[str]:
    return [
        "--corpus",
        str(corpus),
        "--output",
        str(output),
        "--budget-usd",
        "1",
        "--rate-delay-seconds",
        "0",
    ]


def _baseline_row(group: dict[str, object]) -> dict[str, object]:
    request = collector.build_request(group, collector.DEFAULT_INSTRUCTION)
    return {
        "schema": collector.CURRENT_BASELINE_SCHEMA,
        "provider": collector.DEFAULT_PROVIDER,
        "model": collector.DEFAULT_MODEL,
        "query_id": group["query_id"],
        "source_language": group["source_language"],
        "request_fingerprint": collector.request_fingerprint(group, request),
        "request_instruction": collector.DEFAULT_INSTRUCTION,
        "pair_count": 1,
        "candidate_document_ids": ["document-0"],
        "candidate_relevance": [1],
        "response": {"input_tokens": 1, "scores": [0.5]},
        "timing": {"http_status": 200},
        "charged_input_tokens": 1,
        "estimated_input_tokens": collector.estimate_tokens(request),
        "cumulative_cost_usd": 0.000001,
    }


class CloudCollectorArtifactSecurityTests(unittest.TestCase):
    def test_echoed_bearer_key_is_rejected_before_response_persistence(self) -> None:
        secret = "fixture-bearer-key"
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            _write_corpus(corpus)
            response = MagicMock()
            response.status = 200
            response.read.return_value = json.dumps(
                {
                    "scores": [0.5],
                    "input_tokens": 1,
                    "echoed_authorization": f"Bearer {secret}",
                }
            ).encode()
            response.__enter__.return_value = response
            response.__exit__.return_value = None
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    ["collect_reranker_cloud_baseline.py", *_arguments(corpus, output)],
                ),
                patch.dict(os.environ, {"DEEPINFRA_KEY": secret}),
                patch.object(collector.urllib.request, "urlopen", return_value=response),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = collector.main()

            self.assertEqual(result, 1)
            self.assertFalse(output.exists())
            self.assertIn("response contains the configured bearer key", stderr.getvalue())
            artifact_bytes = b"".join(
                path.read_bytes() for path in root.iterdir() if path.is_file()
            )
            self.assertNotIn(secret.encode(), artifact_bytes)
            self.assertNotIn(secret, stdout.getvalue())
            self.assertNotIn(secret, stderr.getvalue())

    def test_committable_response_uses_an_explicit_field_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            _write_corpus(corpus)
            body = {
                "input_tokens": 1,
                "scores": [0.5],
                "request_id": "provider-request-id",
                "inference_status": {"status": "succeeded"},
                "provider_extension": "must-not-persist",
            }
            with (
                patch.object(
                    sys,
                    "argv",
                    ["collect_reranker_cloud_baseline.py", *_arguments(corpus, output)],
                ),
                patch.dict(os.environ, {"DEEPINFRA_KEY": "unit-secret"}),
                patch.object(
                    collector,
                    "call_deepinfra",
                    return_value=(200, body, {"http_status": 200}),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = collector.main()

            self.assertEqual(result, 0)
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                row["response"],
                {
                    "inference_status": {"status": "succeeded"},
                    "input_tokens": 1,
                    "request_id": "provider-request-id",
                    "scores": [0.5],
                },
            )

    def test_durable_append_repairs_existing_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            output = Path(raw_tmp) / "baseline.jsonl"
            output.write_text("", encoding="utf-8")
            output.chmod(0o644)

            collector._append_durable(output, {"safe": True})

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_resume_repairs_existing_baseline_permissions_without_appending(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            group = _write_corpus(corpus)
            output.write_text(
                json.dumps(_baseline_row(group)) + "\n", encoding="utf-8"
            )
            output.chmod(0o644)
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "collect_reranker_cloud_baseline.py",
                        *_arguments(corpus, output),
                        "--resume",
                        "--dry-run",
                    ],
                ),
                patch.dict(os.environ, {"DEEPINFRA_KEY": "unit-secret"}),
                patch.object(collector, "call_deepinfra") as cloud_call,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = collector.main()

            self.assertEqual(result, 0)
            cloud_call.assert_not_called()
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
