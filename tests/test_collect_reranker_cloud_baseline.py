from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

collector = importlib.import_module("collect_reranker_cloud_baseline")


class _OversizedHTTPResponse:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.read_limit = -1

    def __enter__(self) -> _OversizedHTTPResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        self.read_limit = limit
        return b"x" * (collector.MAX_CLOUD_RESPONSE_BYTES + 1)


def _group(index: int) -> dict[str, object]:
    return {
        "query_id": f"query-{index}",
        "query": "q",
        "source_language": "en",
        "candidates": [
            {
                "document": "d",
                "document_id": f"document-{index}",
                "relevance": 1,
                "source_language": "en",
                "top_ranked_rank": 1,
            }
        ],
    }


def _write_corpus(path: Path, count: int) -> list[dict[str, object]]:
    groups = [_group(index) for index in range(count)]
    path.write_text(
        "".join(json.dumps(group, separators=(",", ":")) + "\n" for group in groups),
        encoding="utf-8",
    )
    return groups


def _baseline_row(group: dict[str, object], charged_tokens: int) -> dict[str, object]:
    request = collector.build_request(group, collector.DEFAULT_INSTRUCTION)
    return {
        "schema": "reranker-cloud-baseline-v1",
        "provider": "deepinfra",
        "model": "Qwen/Qwen3-Reranker-8B",
        "query_id": group["query_id"],
        "source_language": group["source_language"],
        "request_fingerprint": collector.request_fingerprint(group, request),
        "request_instruction": collector.DEFAULT_INSTRUCTION,
        "pair_count": 1,
        "candidate_document_ids": [f"document-{str(group['query_id']).removeprefix('query-')}"],
        "candidate_relevance": [1],
        "response": {"input_tokens": charged_tokens, "scores": [0.5]},
        "timing": {"http_status": 200},
        "charged_input_tokens": charged_tokens,
        "estimated_input_tokens": collector.estimate_tokens(request),
        "cumulative_cost_usd": charged_tokens / 1_000_000,
    }


def _run_main_capture(
    arguments: list[str],
    response: tuple[int, dict[str, object], dict[str, object]],
) -> tuple[int, MagicMock, str]:
    stdout = io.StringIO()
    with (
        patch.object(sys, "argv", ["collect_reranker_cloud_baseline.py", *arguments]),
        patch.dict(os.environ, {"DEEPINFRA_KEY": "unit-secret"}),
        patch.object(collector, "call_deepinfra", return_value=response) as cloud_call,
        redirect_stdout(stdout),
        redirect_stderr(io.StringIO()),
    ):
        result = collector.main()
    return result, cloud_call, stdout.getvalue()


def _run_main(
    arguments: list[str],
    response: tuple[int, dict[str, object], dict[str, object]],
) -> tuple[int, MagicMock]:
    result, cloud_call, _stdout = _run_main_capture(arguments, response)
    return result, cloud_call


class CloudCollectorSafetyTests(unittest.TestCase):
    def test_cloud_response_body_is_read_with_a_hard_limit(self) -> None:
        response = _OversizedHTTPResponse()
        with (
            patch.object(collector.urllib.request, "urlopen", return_value=response),
            self.assertRaisesRegex(RuntimeError, "response exceeded"),
        ):
            collector.call_deepinfra(
                "Qwen/Qwen3-Reranker-8B",
                {
                    "documents": ["document"],
                    "instruction": "rank",
                    "queries": ["query"],
                },
                "unit-secret",
                1,
            )
        self.assertEqual(response.read_limit, collector.MAX_CLOUD_RESPONSE_BYTES + 1)

    def test_estimate_includes_utf8_instruction_and_prompt_overhead_per_pair(self) -> None:
        request = {
            "queries": ["查询"],
            "documents": ["文"],
            "instruction": "rank",
        }
        self.assertEqual(collector.estimate_tokens(request), 6 + 3 + 4 + 256)

    def test_budget_preflights_all_chargeable_groups_before_first_paid_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            _write_corpus(corpus, 2)
            result, cloud_call = _run_main(
                [
                    "--corpus",
                    str(corpus),
                    "--output",
                    str(output),
                    "--price-per-mtok",
                    "1",
                    "--budget-usd",
                    "0.0006",
                    "--rate-delay-seconds",
                    "0",
                ],
                (200, {"input_tokens": 1, "scores": [0.5]}, {}),
            )
            self.assertEqual(result, 2)
            cloud_call.assert_not_called()
            self.assertFalse(output.exists())

    def test_request_intent_is_durable_before_paid_transport(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            groups = _write_corpus(corpus, 1)
            request = collector.build_request(groups[0], collector.DEFAULT_INSTRUCTION)
            fingerprint = collector.request_fingerprint(groups[0], request)

            def paid_call(*_args: object, **_kwargs: object) -> tuple[int, dict[str, object], dict[str, object]]:
                ledger = collector.intent_path(output)
                self.assertTrue(ledger.exists())
                rows = [json.loads(line) for line in ledger.read_text().splitlines()]
                self.assertEqual(rows[-1]["request_fingerprint"], fingerprint)
                self.assertTrue(fsync_call.called)
                return 200, {"input_tokens": 1, "scores": [0.5]}, {}

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "collect_reranker_cloud_baseline.py",
                        "--corpus",
                        str(corpus),
                        "--output",
                        str(output),
                        "--budget-usd",
                        "1",
                        "--rate-delay-seconds",
                        "0",
                    ],
                ),
                patch.dict(os.environ, {"DEEPINFRA_KEY": "unit-secret"}),
                patch.object(collector.os, "fsync", wraps=os.fsync) as fsync_call,
                patch.object(collector, "call_deepinfra", side_effect=paid_call),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(collector.main(), 0)

    def test_ambiguous_paid_transport_blocks_automatic_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            _write_corpus(corpus, 1)
            arguments = [
                "--corpus",
                str(corpus),
                "--output",
                str(output),
                "--budget-usd",
                "1",
                "--rate-delay-seconds",
                "0",
            ]
            with (
                patch.object(
                    sys,
                    "argv",
                    ["collect_reranker_cloud_baseline.py", *arguments],
                ),
                patch.dict(os.environ, {"DEEPINFRA_KEY": "unit-secret"}),
                patch.object(
                    collector, "call_deepinfra", side_effect=TimeoutError("ambiguous")
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(collector.main(), 1)
            self.assertTrue(collector.intent_path(output).exists())
            self.assertFalse(output.exists())

            result, cloud_call = _run_main(
                [*arguments, "--resume"],
                (200, {"input_tokens": 1, "scores": [0.5]}, {}),
            )
            self.assertEqual(result, 2)
            cloud_call.assert_not_called()

    def test_durable_provider_failures_remain_terminal_on_every_resume(self) -> None:
        failures = (
            (500, {"input_tokens": 7, "scores": [0.5]}),
            (200, {"input_tokens": "invalid", "scores": [0.5]}),
        )
        for status, body in failures:
            with self.subTest(status=status, body=body), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                corpus = root / "corpus.jsonl"
                output = root / "baseline.jsonl"
                _write_corpus(corpus, 1)
                arguments = [
                    "--corpus",
                    str(corpus),
                    "--output",
                    str(output),
                    "--budget-usd",
                    "1",
                    "--rate-delay-seconds",
                    "0",
                ]
                first_result, _first_call, first_stdout = _run_main_capture(
                    arguments,
                    (status, body, {"http_status": status}),
                )
                self.assertEqual(first_result, 1)
                self.assertEqual(json.loads(first_stdout)["status"], "FAILED")
                self.assertEqual(len(output.read_text().splitlines()), 1)

                for extra in ([], ["--dry-run"]):
                    result, cloud_call, stdout = _run_main_capture(
                        [*arguments, "--resume", *extra],
                        (200, {"input_tokens": 1, "scores": [0.5]}, {"http_status": 200}),
                    )
                    self.assertEqual(result, 1)
                    cloud_call.assert_not_called()
                    summary = json.loads(stdout)
                    self.assertEqual(summary["status"], "FAILED")
                    self.assertEqual(summary["groups_failed"], 1)
                self.assertEqual(len(output.read_text().splitlines()), 1)
                self.assertEqual(
                    len(collector.intent_path(output).read_text().splitlines()), 1
                )

    def test_malformed_corpus_jsonl_exits_cleanly_with_line_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            corpus.write_text('{"query_id":\n', encoding="utf-8")
            stderr = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "collect_reranker_cloud_baseline.py",
                        "--corpus",
                        str(corpus),
                        "--output",
                        str(output),
                        "--dry-run",
                    ],
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                result = collector.main()

            self.assertEqual(result, 2)
            self.assertIn("corpus JSONL row 1 is malformed", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_resume_rejects_malformed_output_instead_of_resending(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            _write_corpus(corpus, 1)
            output.write_text("{\n", encoding="utf-8")
            result, cloud_call = _run_main(
                [
                    "--corpus",
                    str(corpus),
                    "--output",
                    str(output),
                    "--resume",
                    "--budget-usd",
                    "1",
                    "--rate-delay-seconds",
                    "0",
                ],
                (200, {"input_tokens": 1, "scores": [0.5]}, {}),
            )
            self.assertEqual(result, 2)
            cloud_call.assert_not_called()

    def test_resume_rejects_intent_without_complete_response_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            groups = _write_corpus(corpus, 1)
            request = collector.build_request(groups[0], collector.DEFAULT_INSTRUCTION)
            fingerprint = collector.request_fingerprint(groups[0], request)
            collector.intent_path(output).write_text(
                json.dumps(
                    {
                        "schema": "reranker-cloud-request-intent-v1",
                        "request_fingerprint": fingerprint,
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            result, cloud_call = _run_main(
                [
                    "--corpus",
                    str(corpus),
                    "--output",
                    str(output),
                    "--resume",
                    "--budget-usd",
                    "1",
                    "--rate-delay-seconds",
                    "0",
                ],
                (200, {"input_tokens": 1, "scores": [0.5]}, {}),
            )
            self.assertEqual(result, 2)
            cloud_call.assert_not_called()

    def test_resume_charges_completed_rows_in_full_plan_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            output = root / "baseline.jsonl"
            groups = _write_corpus(corpus, 2)
            output.write_text(
                json.dumps(_baseline_row(groups[0], 400), separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            result, cloud_call = _run_main(
                [
                    "--corpus",
                    str(corpus),
                    "--output",
                    str(output),
                    "--resume",
                    "--price-per-mtok",
                    "1",
                    "--budget-usd",
                    "0.0006",
                    "--rate-delay-seconds",
                    "0",
                ],
                (200, {"input_tokens": 1, "scores": [0.5]}, {}),
            )
            self.assertEqual(result, 2)
            cloud_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
