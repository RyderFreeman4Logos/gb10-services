from __future__ import annotations

import importlib
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

replay = importlib.import_module("replay_reranker_local_vs_cloud")


class RankingMetricTests(unittest.TestCase):
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
