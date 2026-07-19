from __future__ import annotations

import importlib
import io
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
