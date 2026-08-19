#!/usr/bin/env python3
"""Contracts for the 6k-input SGLang concurrency throughput harness."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sglang_6k_concurrency_throughput.py"
MIN_PROMPT_TOKENS = 6000

DEFAULT_BASE_URL = "http://100.105.4.92:18010/v1"
DEFAULT_MODEL = "abliterated-qwen-latest-27b-nvfp4"

EXPECTED_CONCURRENCIES = [1, 2, 4, 6, 8]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sglang_6k_concurrency_throughput_under_test", SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SglangConcurrencyThroughputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_default_concurrency_list(self):
        self.assertEqual(self.mod.DEFAULT_CONCURRENCIES, EXPECTED_CONCURRENCIES)

    def test_two_prompts_have_different_first_line_nonces(self):
        n1 = self.mod.make_nonce(0, 0)
        n2 = self.mod.make_nonce(0, 1)
        self.assertNotEqual(n1, n2)
        p1 = self.mod.build_prompt(n1, MIN_PROMPT_TOKENS)
        p2 = self.mod.build_prompt(n2, MIN_PROMPT_TOKENS)
        first1 = p1.splitlines()[0]
        first2 = p2.splitlines()[0]
        self.assertIn(n1, first1)
        self.assertIn(n2, first2)
        self.assertNotEqual(first1, first2)

    def test_estimated_prompt_tokens_at_least_6000(self):
        nonce = self.mod.make_nonce(0, 0)
        prompt = self.mod.build_prompt(nonce, MIN_PROMPT_TOKENS)
        self.assertGreaterEqual(self.mod.estimate_tokens(prompt), MIN_PROMPT_TOKENS)

    def test_dry_run_exit_zero_and_json_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "dry.json")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--dry-run", "--out", out],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["concurrencies"], EXPECTED_CONCURRENCIES)
            self.assertIn("model", data)
            self.assertIn("base_url", data)
            self.assertIn("min_prompt_tokens", data)
            prompt = data["sample_prompt"]
            self.assertGreaterEqual(
                self.mod.estimate_tokens(prompt), MIN_PROMPT_TOKENS
            )
            self.assertIn("-", prompt.splitlines()[0])

    def test_request_payload_shape_and_thinking_off(self):
        nonce = self.mod.make_nonce(0, 0)
        payload = self.mod.build_payload(
            DEFAULT_MODEL, nonce, MIN_PROMPT_TOKENS, max_tokens=256
        )
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertFalse(payload["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(payload["temperature"], 1.0)
        self.assertEqual(payload["top_p"], 0.95)
        self.assertEqual(payload["top_k"], 20)
        self.assertEqual(payload["min_p"], 0.0)
        self.assertEqual(payload["presence_penalty"], 0.0)
        self.assertEqual(payload["repetition_penalty"], 1.0)


if __name__ == "__main__":
    unittest.main()
