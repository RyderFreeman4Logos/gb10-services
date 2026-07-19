from __future__ import annotations

import importlib
import math
import sys
import unittest
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
