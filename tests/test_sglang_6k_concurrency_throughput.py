#!/usr/bin/env python3
"""Contracts for the 6k-input SGLang concurrency throughput harness."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

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

    def test_built_prompt_word_count_at_least_6000(self):
        nonce = self.mod.make_nonce(0, 0)
        prompt = self.mod.build_prompt(nonce, MIN_PROMPT_TOKENS)
        self.assertGreaterEqual(len(prompt.split()), MIN_PROMPT_TOKENS)

    def test_estimate_tokens_never_more_than_word_count(self):
        words = "aluminium " * 10
        self.assertLessEqual(self.mod.estimate_tokens(words), 10)

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

    def test_prompt_construction_does_not_reestimate_whole_prompt(self):
        with mock.patch.object(self.mod, "estimate_tokens", wraps=self.mod.estimate_tokens) as estimate:
            prompt = self.mod.build_prompt("wave-0-request-0", MIN_PROMPT_TOKENS)
        self.assertGreaterEqual(len(prompt.split()), MIN_PROMPT_TOKENS)
        self.assertLessEqual(estimate.call_count, 1)

    def test_reliability_defaults_and_retry_classification(self):
        defaults = self.mod.load_config(None)
        self.assertEqual(defaults["resources"]["max_attempts"], 5)
        self.assertGreaterEqual(defaults["resources"]["request_timeout_s"], 600)
        self.assertGreaterEqual(defaults["resources"]["wave_timeout_s"], 3600)

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "run.toml"
            config.write_text(
                "[resources]\nmax_attempts = 2\nbackoff_initial_s = 0.01\n"
                "backoff_max_s = 0.02\nrequest_timeout_s = 17\nwave_timeout_s = 23\n",
                encoding="utf-8",
            )
            loaded = self.mod.load_config(config)
            self.assertEqual(loaded["resources"]["max_attempts"], 2)
            self.assertEqual(loaded["resources"]["request_timeout_s"], 17)
            self.assertEqual(loaded["resources"]["wave_timeout_s"], 23)

        transient = urllib.error.HTTPError("http://e", 429, "busy", {}, None)
        response = {
            "ttft_s": 0.1, "wall_s": 0.2, "gen_s": 0.1,
            "prompt_tokens": MIN_PROMPT_TOKENS, "completion_tokens": 2,
            "decode_tok_s": 20.0, "finish_reason": "stop", "n_fail_short": 0,
        }
        with mock.patch.object(self.mod, "_stream_chat", side_effect=[transient, response]) as request:
            with mock.patch.object(self.mod.time, "sleep") as sleep:
                result = self.mod.run_request(
                    "http://e", {}, MIN_PROMPT_TOKENS,
                    max_attempts=2, backoff_initial_s=0.01, backoff_max_s=0.02,
                )
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertFalse(result["retry_exhausted"])
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.01)

        permanent = urllib.error.HTTPError("http://e", 400, "bad", {}, None)
        with mock.patch.object(self.mod, "_stream_chat", side_effect=permanent) as request:
            result = self.mod.run_request(
                "http://e", {}, MIN_PROMPT_TOKENS,
                max_attempts=5, backoff_initial_s=0.01, backoff_max_s=0.02,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(result["retry_exhausted"])
        self.assertEqual(request.call_count, 1)

        with mock.patch.object(self.mod, "_stream_chat", side_effect=transient) as request:
            result = self.mod.run_request(
                "http://e", {}, MIN_PROMPT_TOKENS,
                max_attempts=2, backoff_initial_s=0, backoff_max_s=0,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["retry_exhausted"])
        self.assertEqual(result["error_status"], 429)
        self.assertEqual(request.call_count, 2)

    def test_incomplete_stream_body_is_retried_but_permanent_4xx_is_not(self):
        response = {
            "ttft_s": 0.1, "wall_s": 0.2, "gen_s": 0.1,
            "prompt_tokens": MIN_PROMPT_TOKENS, "completion_tokens": 2,
            "decode_tok_s": 20.0, "finish_reason": "stop", "n_fail_short": 0,
        }
        incomplete = http.client.IncompleteRead(b"partial", 2)
        with mock.patch.object(
            self.mod, "_stream_chat", side_effect=[incomplete, response]
        ) as request:
            with mock.patch.object(self.mod.time, "sleep"):
                result = self.mod.run_request(
                    "http://e", {}, MIN_PROMPT_TOKENS,
                    max_attempts=2, backoff_initial_s=0.01, backoff_max_s=0.02,
                )
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertFalse(result["retry_exhausted"])
        self.assertEqual(request.call_count, 2)

        permanent = urllib.error.HTTPError("http://e", 400, "bad", {}, None)
        with mock.patch.object(self.mod, "_stream_chat", side_effect=permanent) as request:
            result = self.mod.run_request(
                "http://e", {}, MIN_PROMPT_TOKENS,
                max_attempts=5, backoff_initial_s=0, backoff_max_s=0,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(result["retry_exhausted"])
        self.assertEqual(request.call_count, 1)

    def test_wave_timeout_is_observational_and_waits_for_worker_completion(self):
        response = {
            "ttft_s": 0.1, "wall_s": 0.2, "gen_s": 0.1,
            "prompt_tokens": MIN_PROMPT_TOKENS, "completion_tokens": 2,
            "decode_tok_s": 20.0, "finish_reason": "stop", "n_fail_short": 0,
            "ok": True,
        }

        def slow_request(*args, **kwargs):
            time.sleep(0.02)
            return response

        with mock.patch.object(self.mod, "run_request", side_effect=slow_request):
            result = self.mod.run_wave(
                "http://e", DEFAULT_MODEL, 0, 1, MIN_PROMPT_TOKENS, 256,
                client_workers=1, request_timeout_s=1, wave_timeout_s=0.001,
            )
        self.assertTrue(result["wave_observation_deadline_exceeded"])
        self.assertEqual(result["n_ok"], 1)
        self.assertEqual(len(result["requests"]), 1)

    def test_checkpoint_progress_and_resume_skip_completed_waves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            state = root / "run.state.json"
            progress = root / "run.progress.yaml"
            out = root / "run.json"
            config.write_text(
                '[run]\nprovider = "p0"\nmodel_id = "m0"\nendpoint = "http://e0/v1"\n'
                "[execution]\nconcurrencies = [1, 2, 3]\nmax_tokens = 7\n"
                "[resources]\nclient_workers = 2\nrequest_timeout_s = 11\n",
                encoding="utf-8",
            )
            calls = []

            def interrupting_wave(base_url, model, wave, n, min_tokens, max_tokens, **kwargs):
                calls.append((base_url, model, wave, n, kwargs))
                if wave == 1:
                    raise RuntimeError("simulated interruption")
                return {
                    "concurrency": n,
                    "n_ok": 0,
                    "n_fail": n,
                    "sum_completion": 0,
                    "requests": [{
                        "ok": False,
                        "attempts": 5,
                        "retry_exhausted": True,
                        "error_status": 503,
                    }],
                }

            with mock.patch.object(self.mod, "run_wave", side_effect=interrupting_wave):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    )
            checkpoint = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["completed_waves"], [0])
            self.assertEqual(checkpoint["waves"][0]["requests"][0]["attempts"], 5)
            self.assertTrue(checkpoint["waves"][0]["requests"][0]["retry_exhausted"])
            progress_text = progress.read_text(encoding="utf-8")
            for field in ("completed:", "total:", "elapsed_s:", "rate:", "eta_s:",
                          "active_wave:", "config_epoch:", "pid:", "start_time:"):
                self.assertIn(field, progress_text)

            calls.clear()
            def completed_wave(base_url, model, wave, n, min_tokens, max_tokens, **kwargs):
                calls.append((base_url, model, wave, n, kwargs))
                return {"concurrency": n, "n_ok": n, "n_fail": 0, "sum_completion": n}

            with mock.patch.object(self.mod, "run_wave", side_effect=completed_wave):
                self.assertEqual(
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out), "--resume",
                        ]
                    ),
                    0,
                )
            self.assertEqual([call[2] for call in calls], [1, 2])
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(report["waves"]), 3)
            self.assertEqual([wave["concurrency"] for wave in report["waves"]], [1, 2, 3])

    def test_config_example_exists_and_readme_copies_it_before_launch(self):
        example = ROOT / "examples" / "sglang-6k-concurrency-throughput.toml"
        self.assertTrue(example.is_file())
        loaded = self.mod.load_config(example)
        self.assertEqual(loaded["execution"]["concurrencies"], EXPECTED_CONCURRENCIES)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        copy = 'cp "$repo_root/examples/sglang-6k-concurrency-throughput.toml" "$run_dir/run.toml"'
        self.assertIn(copy, readme)
        self.assertLess(readme.index(copy), readme.index('--config "$run_dir/run.toml"'))
        self.assertIn("refusing resume: saved benchmark owner is still live", readme)

    def test_exclusive_run_lock_refuses_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "run.lock"
            with self.mod._exclusive_run_lock(lock_path):
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    with self.mod._exclusive_run_lock(lock_path):
                        pass

    def test_exclusive_run_lock_is_held_over_main_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            state = root / "run.state.json"
            progress = root / "run.progress.yaml"
            out = root / "run.json"
            config.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\n[resources]\n",
                encoding="utf-8",
            )

            def run_while_locked(args, config_path, loaded, out_path, state_path, progress_path):
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    with self.mod._exclusive_run_lock(self.mod._run_lock_path(state_path)):
                        pass
                return 0

            with mock.patch.object(self.mod, "_run", side_effect=run_while_locked):
                self.assertEqual(
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    ),
                    0,
                )

    def test_plan_reload_rejects_changed_completed_wave_prefix(self):
        state = self.mod._new_state()
        self.mod._ensure_state_capacity(state, 2, [1, 2])
        state["waves"][0] = {"concurrency": 1}
        state["completed_waves"] = [0]
        with self.assertRaisesRegex(ValueError, "changed completed wave 0"):
            self.mod._ensure_state_capacity(state, 2, [9, 4])

    def test_plan_reload_refuses_to_drop_incomplete_wave_ids(self):
        state = self.mod._new_state()
        self.mod._ensure_state_capacity(state, 3)
        state["waves"][0] = {"concurrency": 1}
        state["completed_waves"] = [0]
        with self.assertRaisesRegex(ValueError, "incomplete planned wave"):
            self.mod._ensure_state_capacity(state, 1)
        self.assertEqual(state["total"], 3)

    def test_config_reload_changes_future_wave_and_config_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            state = root / "run.state.json"
            progress = root / "run.progress.yaml"
            out = root / "run.json"
            config.write_text(
                '[run]\nprovider = "p0"\nmodel_id = "m0"\nendpoint = "http://e0/v1"\n'
                "[execution]\nconcurrencies = [1, 2]\nmax_tokens = 7\n"
                "[resources]\nclient_workers = 2\nrequest_timeout_s = 11\n",
                encoding="utf-8",
            )
            seen = []

            def reload_wave(base_url, model, wave, n, min_tokens, max_tokens, **kwargs):
                seen.append((base_url, model, wave, n, kwargs))
                if wave == 0:
                    config.write_text(
                        '[run]\nprovider = "p1"\nmodel_id = "m1"\nendpoint = "http://e1/v1"\n'
                        "[execution]\nconcurrencies = [1, 4]\nmax_tokens = 9\n"
                        "[resources]\nclient_workers = 5\nrequest_timeout_s = 22\n",
                        encoding="utf-8",
                    )
                return {"concurrency": n, "n_ok": n, "n_fail": 0, "sum_completion": n}

            with mock.patch.object(self.mod, "run_wave", side_effect=reload_wave):
                self.assertEqual(
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    ),
                    0,
                )
            self.assertEqual(seen[0][0:4], ("http://e0/v1", "m0", 0, 1))
            self.assertEqual(seen[1][0:4], ("http://e1/v1", "m1", 1, 4))
            self.assertEqual(seen[1][4]["provider"], "p1")
            self.assertEqual(seen[1][4]["client_workers"], 5)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertNotEqual(report["waves"][0]["config_epoch"], report["waves"][1]["config_epoch"])
            self.assertEqual(len(report["config_epochs"]), 2)


if __name__ == "__main__":
    unittest.main()
