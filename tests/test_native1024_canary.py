"""Contract checks for the isolated native-dimension canary (no model download)."""
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class Native1024CanaryTests(unittest.TestCase):
    def test_request_contract(self):
        try:
            canary = importlib.import_module("native1024_canary")
        except ModuleNotFoundError:
            self.fail("native 1024 canary request contract is absent")
        valid = {"model": canary.MODEL, "input": "synthetic", "dimensions": 1024}
        self.assertEqual(canary.parse_request(valid), ["synthetic"])
        self.assertEqual(canary.parse_request({"model": canary.MODEL, "input": ["a", "b"]}), ["a", "b"])
        for changed in (
            {"dimensions": 4096}, {"dimensions": True}, {"dimensions": 1024.0},
            {"model": "qwen3-embedding-8b"}, {"input": []}, {"input": ["a"] * 3},
            {"input": [1]}, {"input": ""}, {"encoding_format": "base64"},
            {"unknown": "ignored?"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                canary.parse_request({**valid, **changed})

    def test_vector_contract(self):
        try:
            canary = importlib.import_module("native1024_canary")
        except ModuleNotFoundError:
            self.fail("native 1024 canary output contract is absent")
        canary.validate_vectors([[1.0] + [0.0] * 1023], 1)
        for vectors in ([], [[1.0] * 4096], [[0.0] * 1024], [[float("nan")] * 1024]):
            with self.subTest(length=len(vectors)), self.assertRaises(ValueError):
                canary.validate_vectors(vectors, 1)


if __name__ == "__main__":
    unittest.main()
