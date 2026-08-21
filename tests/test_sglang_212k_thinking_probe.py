#!/usr/bin/env python3
"""Contracts for the raw SGLang 212k thinking probe."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sglang_212k_thinking_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sglang_212k_thinking_probe_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Sglang212kThinkingProbeTest(unittest.TestCase):
    def test_payload_has_requested_thinking_contract(self):
        if not SCRIPT.exists():
            self.fail("new probe script is missing")
        mod = _load_module()
        payload = mod.build_payload("model", "nonce-at-least-sixteen", "article bytes")
        self.assertTrue(payload["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(payload["reasoning_effort"], "medium")
        self.assertEqual(payload["thinking_token_budget"], 32768)
        self.assertEqual(payload["max_tokens"], 50000)

    def test_three_payloads_have_distinct_nonce_prefixes(self):
        mod = _load_module()
        payloads = mod.build_payloads("model", "article bytes")
        prefixes = [payload["messages"][0]["content"].splitlines()[0] for payload in payloads]
        self.assertEqual(len(payloads), 3)
        self.assertEqual(len(set(prefixes)), 3)
        self.assertTrue(all(prefix.isprintable() and len(prefix) >= 16 for prefix in prefixes))

    def test_corpus_builder_preserves_order_prefixes_nonce_and_rejects_undersize(self):
        mod = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a.txt"
            second = root / "b.txt"
            corpus = root / "corpus.bin"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            mod.concatenate_files([first, second], corpus)
            self.assertEqual(corpus.read_bytes(), b"firstsecond")
            content = mod.build_user_content("nonce-at-least-sixteen", corpus.read_bytes())
            self.assertTrue(content.startswith("nonce-at-least-sixteen\n"))
            self.assertTrue(content.endswith("firstsecond"))
        with self.assertRaisesRegex(ValueError, "undersize"):
            mod.require_target_tokens(mod.TARGET_TOKENS - mod.TOKEN_TOLERANCE - 1)

    def test_finish_classifier_distinguishes_stop_length_and_missing_eos(self):
        mod = _load_module()

        def event(payload):
            return b"data: " + json.dumps(payload).encode() + b"\n\n"

        usage = {"prompt_tokens": 212144, "completion_tokens": 7}
        stop = mod.classify_sse([
            event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            event({"choices": [], "usage": usage}),
            b"data: [DONE]\n\n",
        ])
        length = mod.classify_sse([
            event({"choices": [{"delta": {}, "finish_reason": "length"}]}),
            event({"choices": [], "usage": usage}),
            b"data: [DONE]\n\n",
        ])
        missing_eos = mod.classify_sse([
            event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            event({"choices": [], "usage": usage}),
        ])
        self.assertEqual(stop["termination"], "stop")
        self.assertEqual(length["termination"], "length")
        self.assertEqual(missing_eos["termination"], "missing_eos")

    def test_artifact_writer_refuses_messages_and_content(self):
        mod = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "receipt.json"
            mod.write_artifact(artifact, {"nonce_sha256": "0" * 64})
            self.assertIn("nonce_sha256", artifact.read_text())
            with self.assertRaisesRegex(ValueError, "messages"):
                mod.write_artifact(artifact, {"messages": []})
            with self.assertRaisesRegex(ValueError, "content"):
                mod.write_artifact(artifact, {"nested": {"content": "x"}})

    def test_dry_run_writes_only_payload_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus.txt"
            run_dir = root / "run"
            corpus.write_bytes(b"tiny synthetic article")
            proc = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--dry-run-payload",
                    "--corpus", str(corpus), "--run-dir", str(run_dir),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = (run_dir / "dry-run-payload.json").read_text()
            self.assertNotIn("messages", receipt)
            self.assertNotIn("content", receipt)
            self.assertNotIn("tiny synthetic article", receipt)
            metadata = json.loads(receipt)
            self.assertEqual(len(metadata["requests"]), 3)
            self.assertTrue(all("nonce_sha256" in row for row in metadata["requests"]))


if __name__ == "__main__":
    unittest.main()
