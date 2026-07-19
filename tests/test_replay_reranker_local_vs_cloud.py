from __future__ import annotations

import importlib
import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

replay = importlib.import_module("replay_reranker_local_vs_cloud")
collector = importlib.import_module("collect_reranker_cloud_baseline")


def _evidence_fixture(root: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    candidates = [
        {"document_id": f"d{i}", "document": f"document {i}", "relevance": i == 0}
        for i in range(5)
    ]
    group = {
        "query_id": "q1",
        "source_language": "en",
        "query": "query",
        "candidates": candidates,
    }
    instruction = collector.DEFAULT_INSTRUCTION
    request = collector.build_request(group, instruction)
    row: dict[str, object] = {
        "schema": collector.LEGACY_BASELINE_SCHEMA,
        "provider": collector.DEFAULT_PROVIDER,
        "model": collector.DEFAULT_MODEL,
        "query_id": "q1",
        "source_language": "en",
        "request_fingerprint": collector.request_fingerprint(
            group,
            request,
            baseline_schema=collector.LEGACY_BASELINE_SCHEMA,
        ),
        "request_instruction": instruction,
        "pair_count": 5,
        "candidate_document_ids": [f"d{i}" for i in range(5)],
        "candidate_relevance": [True, False, False, False, False],
        "response": {"scores": [0.9, 0.7, 0.5, 0.3, 0.1], "input_tokens": 5},
        "timing": {"http_status": 200},
        "charged_input_tokens": 5,
        "estimated_input_tokens": collector.estimate_tokens(request),
        "cumulative_cost_usd": 0.1,
    }
    corpus = root / "corpus.jsonl"
    baseline = root / "baseline.jsonl"
    output = root / "receipt.json"
    corpus.write_bytes(replay.canonical_json(group) + b"\n")
    baseline.write_bytes(replay.canonical_json(row) + b"\n")
    return corpus, baseline, output, row


class _OversizedHTTPResponse:
    status = 200

    def __init__(self) -> None:
        self.read_limit = -1

    def __enter__(self) -> _OversizedHTTPResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        self.read_limit = limit
        return b"x" * (replay.MAX_LOCAL_RESPONSE_BYTES + 1)


class RankingMetricTests(unittest.TestCase):
    def test_committed_legacy_baseline_binds_every_exact_corpus_request(self) -> None:
        data = ROOT / "data" / "reranker-equivalence"
        corpus_rows, _corpus_hash = replay.load_jsonl(
            data / "miracl-reranking-en-zh-dev.jsonl"
        )
        baseline_rows, _baseline_hash = replay.load_jsonl(
            data / "cloud-baseline-deepinfra-qwen3-reranker-8b.jsonl"
        )
        corpus_index = replay.load_corpus_index(corpus_rows)
        self.assertEqual(len(baseline_rows), 200)
        for row in baseline_rows:
            replay.validate_baseline_identity(
                row,
                corpus_index[(row["query_id"], row["source_language"])],
                provider=collector.DEFAULT_PROVIDER,
                model=collector.DEFAULT_MODEL,
            )

    def test_versioned_fingerprints_bind_legacy_and_current_experiment_identity(
        self,
    ) -> None:
        group = {
            "query_id": "q1",
            "source_language": "en",
            "query": "query",
            "candidates": [{"document": "document"}],
        }
        request = collector.build_request(group, collector.DEFAULT_INSTRUCTION)
        legacy_identity = {
            "query_id": "q1",
            "source_language": "en",
            "request": request,
        }
        legacy = hashlib.sha256(collector.canonical_json(legacy_identity)).hexdigest()
        self.assertEqual(
            collector.request_fingerprint(
                group,
                request,
                baseline_schema=collector.LEGACY_BASELINE_SCHEMA,
            ),
            legacy,
        )
        current = collector.request_fingerprint(
            group, request, baseline_schema=collector.CURRENT_BASELINE_SCHEMA
        )
        self.assertNotEqual(current, legacy)
        self.assertNotEqual(
            current,
            collector.request_fingerprint(
                group,
                request,
                provider="other-provider",
                baseline_schema=collector.CURRENT_BASELINE_SCHEMA,
            ),
        )
        self.assertNotEqual(
            current,
            collector.request_fingerprint(
                group,
                request,
                model="other-model",
                baseline_schema=collector.CURRENT_BASELINE_SCHEMA,
            ),
        )

    def test_replay_rejects_identity_mutations_before_local_request(self) -> None:
        mutations = {
            "schema": "unknown-schema",
            "provider": "other-provider",
            "model": "other-model",
            "source_language": "zh",
            "request_fingerprint": "0" * 64,
            "request_instruction": "different instruction",
            "candidate_document_ids": ["wrong"] * 5,
            "candidate_relevance": [False] * 5,
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw_tmp:
                corpus, baseline, output, row = _evidence_fixture(Path(raw_tmp))
                row[field] = value
                baseline.write_bytes(replay.canonical_json(row) + b"\n")
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "replay_reranker_local_vs_cloud.py",
                            "--baseline",
                            str(baseline),
                            "--corpus",
                            str(corpus),
                            "--output",
                            str(output),
                            "--local-url",
                            "http://127.0.0.1:18014",
                            "--rate-delay-seconds",
                            "0",
                        ],
                    ),
                    patch.object(replay, "call_local") as local_call,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    result = replay.main()
                self.assertEqual(result, 2)
                local_call.assert_not_called()
                self.assertFalse(output.exists())

    def test_receipt_binds_exact_baseline_and_corpus_content_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            corpus, baseline, output, _row = _evidence_fixture(Path(raw_tmp))
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "replay_reranker_local_vs_cloud.py",
                        "--baseline",
                        str(baseline),
                        "--corpus",
                        str(corpus),
                        "--output",
                        str(output),
                        "--local-url",
                        "http://127.0.0.1:18014",
                        "--rate-delay-seconds",
                        "0",
                    ],
                ),
                patch.object(
                    replay,
                    "call_local",
                    return_value=(
                        200,
                        {
                            "data": [
                                {"index": index, "score": score}
                                for index, score in enumerate(
                                    (0.8, 0.4, 0.0, -0.4, -0.8)
                                )
                            ]
                        },
                    ),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = replay.main()
            self.assertEqual(result, 0)
            receipt = json.loads(output.read_text())
            self.assertEqual(
                receipt["baseline_sha256"], hashlib.sha256(baseline.read_bytes()).hexdigest()
            )
            self.assertEqual(
                receipt["corpus_sha256"], hashlib.sha256(corpus.read_bytes()).hexdigest()
            )

    def test_nonfinite_values_cannot_be_serialized_as_json_evidence(self) -> None:
        with self.assertRaises(ValueError):
            replay.canonical_json({"metric": float("nan")})

    def test_empty_baseline_or_corpus_fails_before_requests_and_outputs(self) -> None:
        cases = (("", ""), ("", json.dumps({"query_id": "q1"})), ("{}\n", ""))
        for baseline_text, corpus_text in cases:
            with (
                self.subTest(baseline=baseline_text, corpus=corpus_text),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                baseline = root / "baseline.jsonl"
                corpus = root / "corpus.jsonl"
                output = root / "receipt.json"
                baseline.write_text(baseline_text)
                corpus.write_text(corpus_text)
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "replay_reranker_local_vs_cloud.py",
                            "--baseline",
                            str(baseline),
                            "--corpus",
                            str(corpus),
                            "--output",
                            str(output),
                            "--local-url",
                            "http://127.0.0.1:18014",
                        ],
                    ),
                    patch.object(replay, "call_local") as local_call,
                    patch("sys.stdout", new_callable=io.StringIO) as stdout,
                    patch("sys.stderr", new_callable=io.StringIO),
                ):
                    result = replay.main()

                self.assertEqual(result, 2)
                local_call.assert_not_called()
                self.assertEqual(stdout.getvalue(), "")
                self.assertFalse(output.exists())
                self.assertFalse(output.with_suffix(".groups.jsonl").exists())

    def test_zero_valid_comparisons_cannot_write_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            baseline = root / "baseline.jsonl"
            corpus = root / "corpus.jsonl"
            output = root / "receipt.json"
            baseline.write_text(json.dumps({"query_id": "missing"}) + "\n")
            corpus.write_text(json.dumps({"query_id": "q1"}) + "\n")
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "replay_reranker_local_vs_cloud.py",
                        "--baseline",
                        str(baseline),
                        "--corpus",
                        str(corpus),
                        "--output",
                        str(output),
                        "--local-url",
                        "http://127.0.0.1:18014",
                    ],
                ),
                patch.object(replay, "call_local") as local_call,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                result = replay.main()

            self.assertEqual(result, 2)
            local_call.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(output.exists())

    def test_score_normalizers_reject_boolean_indices_scores_and_domain_drift(self) -> None:
        invalid_vllm = (
            {"data": [{"index": False, "score": 0.0}]},
            {"data": [{"index": 0, "score": True}]},
            {"data": [{"index": 0, "score": -1.000001}]},
            {"data": [{"index": 0, "score": 1.000001}]},
        )
        for body in invalid_vllm:
            with self.subTest(contract="vllm", body=body), self.assertRaises(ValueError):
                replay.normalize_vllm_score(body, 1)

        invalid_public = ([True], [-0.000001], [1.000001])
        for scores in invalid_public:
            with self.subTest(contract="deepinfra", scores=scores), self.assertRaises(
                ValueError
            ):
                replay.normalize_deepinfra({"scores": scores}, 1)
            with self.subTest(contract="cloud", scores=scores), self.assertRaises(
                ValueError
            ):
                replay.extract_cloud_scores({"response": {"scores": scores}}, 1)

    def test_structurally_invalid_corpus_rows_fail_without_tracebacks_or_requests(self) -> None:
        invalid_candidates = (None, [{}], [])
        for candidates in invalid_candidates:
            with (
                self.subTest(candidates=candidates),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                corpus = root / "corpus.jsonl"
                baseline = root / "baseline.jsonl"
                output = root / "replay.json"
                corpus.write_text(
                    json.dumps(
                        {"query_id": "q1", "query": "q", "candidates": candidates}
                    )
                    + "\n",
                    encoding="utf-8",
                )
                baseline.write_text(
                    json.dumps(
                        {"query_id": "q1", "response": {"scores": [0.5]}}
                    )
                    + "\n",
                    encoding="utf-8",
                )
                stderr = io.StringIO()
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "replay_reranker_local_vs_cloud.py",
                            "--corpus",
                            str(corpus),
                            "--baseline",
                            str(baseline),
                            "--local-url",
                            "http://127.0.0.1:18013",
                            "--output",
                            str(output),
                        ],
                    ),
                    patch.object(replay, "call_local") as local_call,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(stderr),
                ):
                    result = replay.main()

                self.assertNotEqual(result, 0)
                self.assertNotIn("Traceback", stderr.getvalue())
                local_call.assert_not_called()

    def test_local_response_body_is_read_with_a_hard_limit(self) -> None:
        response = _OversizedHTTPResponse()
        with (
            patch.object(replay.urllib.request, "urlopen", return_value=response),
            self.assertRaisesRegex(RuntimeError, "response exceeded"),
        ):
            replay.call_local(
                "http://127.0.0.1:18013",
                "/score",
                "Querit/Querit-4B",
                {"model": "Querit/Querit-4B", "text_1": ["q"], "text_2": ["d"]},
                1,
            )
        self.assertEqual(response.read_limit, replay.MAX_LOCAL_RESPONSE_BYTES + 1)

    def test_malformed_input_jsonl_exits_cleanly_with_line_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            baseline = root / "baseline.jsonl"
            output = root / "replay.jsonl"
            valid_corpus = '{"query_id":"q1"}\n'
            valid_baseline = '{}\n'

            for malformed_flag in ("--corpus", "--baseline"):
                with self.subTest(malformed_flag=malformed_flag):
                    corpus.write_text(
                        '{"query_id":\n' if malformed_flag == "--corpus" else valid_corpus,
                        encoding="utf-8",
                    )
                    baseline.write_text(
                        '{"response":\n'
                        if malformed_flag == "--baseline"
                        else valid_baseline,
                        encoding="utf-8",
                    )
                    stderr = io.StringIO()
                    with (
                        patch.object(
                            sys,
                            "argv",
                            [
                                "replay_reranker_local_vs_cloud.py",
                                "--corpus",
                                str(corpus),
                                "--baseline",
                                str(baseline),
                                "--local-url",
                                "http://127.0.0.1:18013",
                                "--output",
                                str(output),
                            ],
                        ),
                        patch.object(replay, "call_local") as local_call,
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(stderr),
                    ):
                        result = replay.main()

                    self.assertEqual(result, 2)
                    self.assertIn("JSONL row 1 is malformed", stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())
                    local_call.assert_not_called()

    def test_top1_compares_top_ranked_document_indices(self) -> None:
        self.assertTrue(replay.top1_agreement((0.0, 3.0, 2.0), (2.0, 3.0, 0.0)))
        self.assertFalse(replay.top1_agreement((0.0, 3.0, 2.0), (0.0, 2.0, 3.0)))

    def test_top_k_overlap_compares_ranked_document_indices(self) -> None:
        self.assertEqual(
            replay.top_k_overlap((0.0, 2.0, 1.0, 3.0), (0.0, 2.0, 3.0, 1.0), 2),
            0.5,
        )

    def test_top_k_overlap_retains_invalid_k_contract(self) -> None:
        self.assertTrue(math.isnan(replay.top_k_overlap((1.0,), (1.0,), 0)))
        self.assertTrue(math.isnan(replay.top_k_overlap((1.0,), (1.0,), 2)))


if __name__ == "__main__":
    unittest.main()
