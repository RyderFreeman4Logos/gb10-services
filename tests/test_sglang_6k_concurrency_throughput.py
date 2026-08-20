#!/usr/bin/env python3
"""Contracts for the 6k-input SGLang concurrency throughput harness."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
from pathlib import Path
import stat
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


def _completed_request(**overrides):
    request = {
        "ok": True,
        "attempts": 1,
        "retry_exhausted": False,
        "ttft_s": 0.1,
        "wall_s": 0.2,
        "final_attempt_wall_s": 0.2,
        "logical_wall_s": 0.2,
        "retry_overhead_s": 0.0,
        "retry_backoff_s": 0.0,
        "failed_attempt_wall_s": 0.0,
        "gen_s": 0.1,
        "prompt_tokens": MIN_PROMPT_TOKENS,
        "completion_tokens": 2,
        "decode_tok_s": 20.0,
        "finish_reason": "stop",
        "n_fail_short": 0,
        "request_timeout_s": 600.0,
    }
    request.update(overrides)
    return request


def _completed_wave(concurrency, **overrides):
    request_timeout_s = overrides.pop("request_timeout_s", None)
    result = {
        "concurrency": concurrency,
        "work_units": concurrency,
        "n_ok": concurrency,
        "n_fail": 0,
        "n_finish_length": 0,
        "sum_completion": concurrency * 2,
        "wave_wall_s": 0.2,
        "wave_observation_deadline_exceeded": False,
        "agg_decode_tok_s": float(concurrency * 2) / 0.2,
        "agg_prompt_tok_s": float(concurrency * MIN_PROMPT_TOKENS) / 0.2,
        "requests": [_completed_request() for _ in range(concurrency)],
    }
    if request_timeout_s is not None:
        for request in result["requests"]:
            request["request_timeout_s"] = request_timeout_s
    result.update(overrides)
    return result


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

    def test_backend_identity_preflight_requires_configured_alias_on_raw_models_route(self):
        config = self.mod.load_config(None)
        config["run"]["endpoint"] = "http://raw.example/v1"
        config["run"]["model_id"] = "required-alias"

        class Response:
            def __init__(self, body, status=200):
                self.body = body
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def getcode(self):
                return self.status

            def read(self):
                return self.body

        with mock.patch.object(
            self.mod.urllib.request,
            "urlopen",
            return_value=Response(b'{"data":[{"id":"required-alias"}]}'),
        ) as urlopen:
            result = self.mod.backend_identity_preflight(config)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["endpoint"], "http://raw.example/v1/models")
        self.assertEqual(result["offered_models"], ["required-alias"])
        self.assertEqual(urlopen.call_args.args[0].full_url, "http://raw.example/v1/models")

        with mock.patch.object(
            self.mod.urllib.request,
            "urlopen",
            return_value=Response(b'{"data":[{"id":"different-alias"}]}'),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected required alias"):
                self.mod.backend_identity_preflight(config)

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

    def test_retry_metrics_account_for_failed_attempt_and_backoff_time(self):
        response = {
            "ttft_s": 0.1, "wall_s": 0.2, "gen_s": 0.1,
            "prompt_tokens": MIN_PROMPT_TOKENS, "completion_tokens": 2,
            "decode_tok_s": 20.0, "finish_reason": "stop", "n_fail_short": 0,
        }
        transient = urllib.error.HTTPError("http://e", 429, "busy", {}, None)
        clock = [0.0]

        def monotonic():
            return clock[0]

        def failed_attempt(*args, **kwargs):
            clock[0] += 0.4
            raise transient

        def final_attempt(*args, **kwargs):
            clock[0] += 0.2
            return response

        def sleep(delay):
            clock[0] += delay

        attempts = [failed_attempt, final_attempt]

        def stream(*args, **kwargs):
            return attempts.pop(0)(*args, **kwargs)

        with mock.patch.object(self.mod, "_stream_chat", side_effect=stream):
            with mock.patch.object(self.mod.time, "monotonic", side_effect=monotonic):
                with mock.patch.object(self.mod.time, "sleep", side_effect=sleep):
                    result = self.mod.run_request(
                        "http://e", {}, MIN_PROMPT_TOKENS,
                        max_attempts=2, backoff_initial_s=0.1, backoff_max_s=0.1,
                    )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["wall_s"], 0.2)
        self.assertAlmostEqual(result["ttft_s"], 0.1)
        self.assertAlmostEqual(result["decode_tok_s"], 20.0)
        self.assertAlmostEqual(result["failed_attempt_wall_s"], 0.4)
        self.assertAlmostEqual(result["retry_backoff_s"], 0.1)
        self.assertAlmostEqual(result["retry_overhead_s"], 0.5)
        self.assertAlmostEqual(result["logical_wall_s"], 0.7)
        self.assertAlmostEqual(
            result["logical_wall_s"],
            result["failed_attempt_wall_s"]
            + result["retry_backoff_s"]
            + result["final_attempt_wall_s"],
        )

    def test_config_rejects_workers_below_requested_wave_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "run.toml"
            config.write_text(
                "[execution]\nconcurrencies = [1, 3]\n"
                "[resources]\nclient_workers = 2\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "client_workers.*wave concurrency"):
                self.mod.load_config(config)

    def test_config_rejects_unknown_client_keys_but_ignores_service_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "run.toml"
            config.write_text(
                "[execution]\nconcurrencies = [1]\nunknown_client_key = true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown.*execution.*unknown_client_key"):
                self.mod.load_config(config)
            config.write_text(
                "[execution]\nconcurrencies = [1]\n\n[service]\nunknown_service_key = true\n",
                encoding="utf-8",
            )
            self.assertEqual(self.mod.load_config(config)["execution"]["concurrencies"], [1])

    def test_config_rejects_unknown_top_level_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "run.toml"
            config.write_text(
                "[execution]\nconcurrencies = [1]\n\n[mystery]\nvalue = true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown top-level config section.*mystery"):
                self.mod.load_config(config)

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

    def test_stream_chat_requires_done_usage_and_finish_reason(self):
        class Response:
            def __init__(self, lines):
                self.lines = lines

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def __iter__(self):
                return iter(self.lines)

        valid = [
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":6000,"completion_tokens":1}}\n',
            b"data: [DONE]\n",
        ]
        with mock.patch.object(
            self.mod.urllib.request, "urlopen", return_value=Response(valid)
        ):
            result = self.mod._stream_chat("http://e/v1", {}, MIN_PROMPT_TOKENS)
        self.assertEqual(result["finish_reason"], "stop")
        self.assertEqual(result["prompt_tokens"], MIN_PROMPT_TOKENS)
        self.assertEqual(result["completion_tokens"], 1)

        invalid_streams = (
            valid[:-1],
            [valid[0], valid[1], valid[3]],
            [valid[0], valid[2], valid[3]],
            [
                b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":6000,"completion_tokens":true}}\n',
                b"data: [DONE]\n",
            ],
        )
        for lines in invalid_streams:
            with self.subTest(lines=lines), mock.patch.object(
                self.mod.urllib.request, "urlopen", return_value=Response(lines)
            ):
                with self.assertRaises(ConnectionError):
                    self.mod._stream_chat("http://e/v1", {}, MIN_PROMPT_TOKENS)

    def test_stream_chat_rejects_early_duplicate_and_post_usage_events(self):
        class Response:
            def __init__(self, lines):
                self.lines = lines

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def __iter__(self):
                return iter(self.lines)

        content = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n'
        terminal = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
        usage = b'data: {"choices":[],"usage":{"prompt_tokens":6000,"completion_tokens":1}}\n'
        done = b"data: [DONE]\n"
        invalid_streams = (
            [content, usage, terminal, done],
            [content, terminal, usage, usage, done],
            [content, terminal, usage, content, done],
            [content, done, terminal, usage],
        )
        for lines in invalid_streams:
            with self.subTest(lines=lines), mock.patch.object(
                self.mod.urllib.request, "urlopen", return_value=Response(lines)
            ):
                with self.assertRaises(ConnectionError):
                    self.mod._stream_chat("http://e/v1", {}, MIN_PROMPT_TOKENS)

    def test_request_timeout_is_total_attempt_deadline_for_trickle_stream(self):
        clock = [0.0]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def __iter__(self):
                for line in (
                    b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n',
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
                ):
                    clock[0] += 0.75
                    yield line

        with mock.patch.object(self.mod.urllib.request, "urlopen", return_value=Response()):
            with mock.patch.object(self.mod.time, "monotonic", side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(TimeoutError, "deadline"):
                    self.mod._stream_chat(
                        "http://e/v1", {}, MIN_PROMPT_TOKENS, request_timeout_s=1.0
                    )

    def test_clean_eof_stream_is_retryable_and_never_complete(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n',
                        b'data: {"choices":[],"usage":{"prompt_tokens":6000,"completion_tokens":1}}\n',
                    ]
                )

        with mock.patch.object(self.mod.urllib.request, "urlopen", return_value=Response()):
            with mock.patch.object(self.mod.time, "sleep"):
                result = self.mod.run_request(
                    "http://e/v1", {}, MIN_PROMPT_TOKENS,
                    max_attempts=2, backoff_initial_s=0, backoff_max_s=0,
                )
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["retry_exhausted"])

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
                "[resources]\nclient_workers = 3\nrequest_timeout_s = 11\n",
                encoding="utf-8",
            )
            calls = []

            def interrupting_wave(base_url, model, wave, n, min_tokens, max_tokens, **kwargs):
                calls.append((base_url, model, wave, n, kwargs))
                if wave == 1:
                    raise RuntimeError("simulated interruption")
                return _completed_wave(
                    n, request_timeout_s=kwargs.get("request_timeout_s", 600.0)
                )

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
            self.assertEqual(checkpoint["waves"][0]["requests"][0]["attempts"], 1)
            self.assertFalse(checkpoint["waves"][0]["requests"][0]["retry_exhausted"])
            progress_text = progress.read_text(encoding="utf-8")
            for field in ("completed:", "total:", "elapsed_s:", "rate:", "eta_s:",
                          "active_wave:", "config_epoch:", "pid:", "start_time:"):
                self.assertIn(field, progress_text)

            calls.clear()
            progress_before_resume_wave = []

            def completed_wave(base_url, model, wave, n, min_tokens, max_tokens, **kwargs):
                calls.append((base_url, model, wave, n, kwargs))
                return _completed_wave(
                    n, request_timeout_s=kwargs.get("request_timeout_s", 600.0)
                )

            def completed_wave_and_capture(*args, **kwargs):
                progress_before_resume_wave.append(progress.read_text(encoding="utf-8"))
                return completed_wave(*args, **kwargs)

            with mock.patch.object(self.mod, "run_wave", side_effect=completed_wave_and_capture):
                self.assertEqual(
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out), "--resume",
                        ]
                    ),
                    0,
                )
            self.assertIn("active_wave: 1", progress_before_resume_wave[0])
            self.assertEqual([call[2] for call in calls], [1, 2])
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(report["waves"]), 3)
            self.assertEqual([wave["concurrency"] for wave in report["waves"]], [1, 2, 3])

    def test_schema1_checkpoint_migrates_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "run.toml"
            state_path = root / "run.state.json"
            progress_path = root / "run.progress.yaml"
            out_path = root / "run.json"
            config_path.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1, 2]\nmax_tokens = 7\n"
                "[resources]\nclient_workers = 2\nrequest_timeout_s = 11\n",
                encoding="utf-8",
            )
            config = self.mod.load_config(config_path)
            epoch = self.mod.config_epoch(config)
            completed = _completed_wave(
                1,
                wave=0,
                config_epoch=epoch,
                provider="p",
                endpoint="http://e/v1",
                model="m",
                max_tokens=7,
            )
            for request in completed["requests"]:
                request.pop("request_timeout_s")
            completed.pop("work_units")
            legacy = {
                "schema_version": 1,
                "run_id": "legacy-run",
                "started_at": 1.0,
                "elapsed_s": 0.5,
                "total": 2,
                "planned_total": 2,
                "planned_wave_ids": [0, 1],
                "completed_waves": [0],
                "waves": [completed, None],
                "config_epochs": {epoch: config},
                "pid": 999999,
                "start_time": 1,
            }
            state_path.write_text(json.dumps(legacy), encoding="utf-8")

            calls = []

            def complete_remaining(*args, **kwargs):
                calls.append((args, kwargs))
                return _completed_wave(
                    2, request_timeout_s=kwargs.get("request_timeout_s", 600.0)
                )

            with mock.patch.object(self.mod, "run_wave", side_effect=complete_remaining):
                self.assertEqual(
                    self.mod.main(
                        [
                            "--config", str(config_path), "--state", str(state_path),
                            "--progress", str(progress_path), "--out", str(out_path), "--resume",
                        ]
                    ),
                    0,
                )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][2:4], (1, 2))
            migrated = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["completed_waves"], [0, 1])
            self.assertEqual(migrated["completed_work_units"], 3)
            self.assertEqual(migrated["planned_work_units"], 3)
            self.assertIsNone(migrated["active_wave"])
            self.assertIsNone(migrated["active_config_epoch"])
            self.assertEqual(migrated["waves"][0]["work_units"], 1)
            self.assertEqual(
                migrated["waves"][0]["requests"][0]["request_timeout_s"], 11
            )

    def test_completed_wave_metrics_must_match_referenced_config_epoch(self):
        cases = {
            "prompt_floor": lambda request: request.update(prompt_tokens=MIN_PROMPT_TOKENS - 1),
            "request_timeout": lambda request: request.update(request_timeout_s=12.0),
            "max_attempts": lambda request: request.update(attempts=3),
            "retry_backoff": lambda request: request.update(retry_backoff_s=0.1),
        }
        for case, forge in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_path = root / "run.toml"
                state_path = root / "run.state.json"
                progress_path = root / "run.progress.yaml"
                out_path = root / "run.json"
                config_path.write_text(
                    '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                    "[execution]\nconcurrencies = [1]\nmax_tokens = 7\n"
                    "[resources]\nmax_attempts = 2\nrequest_timeout_s = 11\n"
                    "backoff_initial_s = 0.1\nbackoff_max_s = 0.2\n",
                    encoding="utf-8",
                )
                def forged_wave(*args, **kwargs):
                    result = _completed_wave(1)
                    forge(result["requests"][0])
                    return result

                with mock.patch.object(self.mod, "run_wave", side_effect=forged_wave):
                    with self.assertRaisesRegex(RuntimeError, "benchmark incomplete"):
                        self.mod.main(
                            [
                                "--config", str(config_path), "--state", str(state_path),
                                "--progress", str(progress_path), "--out", str(out_path),
                            ]
                        )
                report = json.loads(out_path.read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "INCOMPLETE")
                self.assertFalse(report["complete"])

    def test_config_example_exists_and_readme_copies_it_before_launch(self):
        example = ROOT / "examples" / "sglang-6k-concurrency-throughput.toml"
        self.assertTrue(example.is_file())
        loaded = self.mod.load_config(example)
        self.assertEqual(loaded["execution"]["concurrencies"], EXPECTED_CONCURRENCIES)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        copy = 'cp "$repo_root/examples/sglang-6k-concurrency-throughput.toml" "$run_dir/run.toml"'
        self.assertIn(copy, readme)
        self.assertLess(readme.index(copy), readme.index('--config "$run_dir/run.toml"', readme.index(copy)))
        self.assertIn("refusing resume: saved benchmark owner is still live", readme)
        resume = readme[readme.index("--resume") :]
        self.assertIn("pid=$!", resume)
        self.assertIn('start_time=$(awk', resume)
        self.assertIn('pid_start_tmp="$run_dir/.run.pid-start.', resume)
        self.assertIn('>"$pid_start_tmp"', resume)
        self.assertIn('mv -f -- "$pid_start_tmp" "$run_dir/run.pid-start"', resume)

    def test_artifact_parent_lock_refuses_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.mod._exclusive_artifact_locks(
                root / "run.json", root / "run.state.json"
            ):
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    with self.mod._exclusive_artifact_locks(
                        root / "other.json", root / "other.state.json"
                    ):
                        pass

    def test_artifact_locks_reject_duplicate_absent_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "absent.json"
            with self.assertRaisesRegex(RuntimeError, "duplicate artifact"):
                with self.mod._exclusive_artifact_locks(target, target):
                    pass

    def test_artifact_io_reuses_locked_parent_bindings_for_full_run(self):
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
            with mock.patch.object(self.mod, "run_wave", return_value=_completed_wave(1)):
                with mock.patch.object(
                    self.mod,
                    "_open_artifact_parent",
                    wraps=self.mod._open_artifact_parent,
                ) as open_parent:
                    self.assertEqual(
                        self.mod.main(
                            [
                                "--config", str(config), "--state", str(state),
                                "--progress", str(progress), "--out", str(out),
                            ]
                        ),
                        0,
                    )
            self.assertEqual(open_parent.call_count, 3)

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
                    with self.mod._exclusive_artifact_locks(
                        out_path, state_path, progress_path
                    ):
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

    def test_artifact_locks_refuse_same_output_with_different_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run.json"
            state_a = root / "a.state.json"
            state_b = root / "b.state.json"
            with self.mod._exclusive_artifact_locks(out, state_a):
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    with self.mod._exclusive_artifact_locks(out, state_b):
                        pass

    def test_artifact_locks_refuse_symlink_and_hardlink_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run.json"
            hardlink = root / "hardlink.json"
            symlink = root / "symlink.json"
            out.write_text("{}\n", encoding="utf-8")
            os.link(out, hardlink)
            symlink.symlink_to(out)
            state_a = root / "a.state.json"
            state_b = root / "b.state.json"
            with self.mod._exclusive_artifact_locks(out, state_a):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    with self.mod._exclusive_artifact_locks(symlink, state_b):
                        pass
                broken = root / "broken.json"
                broken.symlink_to(root / "missing.json")
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    with self.mod._exclusive_artifact_locks(broken, state_b):
                        pass
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    with self.mod._exclusive_artifact_locks(hardlink, state_b):
                        pass

    def test_artifact_locks_reject_progress_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run.json"
            state = root / "run.state.json"
            progress = root / "run.progress.yaml"
            out.write_text("{}\n", encoding="utf-8")
            os.link(out, progress)
            with self.assertRaisesRegex(RuntimeError, "collision|alias"):
                with self.mod._exclusive_artifact_locks(out, state, progress):
                    pass

    def test_artifact_locks_use_parent_fds_and_serialize_same_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run.json"
            state_a = root / "a.state.json"
            out_b = root / "other.json"
            state_b = root / "b.state.json"
            real_flock = self.mod.fcntl.flock
            locked_modes = []

            def record_directory_flock(fd, operation):
                if operation & self.mod.fcntl.LOCK_EX:
                    locked_modes.append(stat.S_ISDIR(os.fstat(fd).st_mode))
                return real_flock(fd, operation)

            with mock.patch.object(self.mod.fcntl, "flock", side_effect=record_directory_flock):
                with self.mod._exclusive_artifact_locks(out, state_a):
                    with self.assertRaisesRegex(RuntimeError, "already owned"):
                        with self.mod._exclusive_artifact_locks(out_b, state_b):
                            pass
            self.assertTrue(locked_modes)
            self.assertTrue(all(locked_modes))

    def test_artifact_parent_directories_must_preexist(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "missing" / "run.json"
            with self.assertRaises(FileNotFoundError):
                self.mod._atomic_write(target, "must not create parents\n")
            self.assertFalse(target.parent.exists())

    def test_atomic_write_preserves_colliding_temp_after_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "run.json"
            collision = root / ".run.json.collision.tmp"
            collision.write_text("keep collision\n", encoding="utf-8")
            fake_uuid = mock.Mock(hex="collision")
            with mock.patch.object(self.mod.uuid, "uuid4", return_value=fake_uuid):
                with self.assertRaises(FileExistsError):
                    self.mod._atomic_write(target, "new content\n")
            self.assertEqual(collision.read_text(encoding="utf-8"), "keep collision\n")

    def test_atomic_write_preserves_replaced_temp_after_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "run.json"

            def replace_with_colliding_basename(source, destination, *args, **kwargs):
                directory_fd = kwargs["src_dir_fd"]
                self.mod.os.unlink(source, dir_fd=directory_fd)
                (root / source).write_text("replacement\n", encoding="utf-8")
                raise FileExistsError("replacement won the basename")

            with mock.patch.object(
                self.mod.os, "replace", side_effect=replace_with_colliding_basename
            ):
                with self.assertRaises(FileExistsError):
                    self.mod._atomic_write(target, "new content\n")
            leftovers = list(root.glob(".run.json.*.tmp"))
            self.assertEqual(len(leftovers), 1)
            self.assertEqual(leftovers[0].read_text(encoding="utf-8"), "replacement\n")

    def test_atomic_publication_rejects_symlinked_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_parent = root / "real-parent"
            real_parent.mkdir()
            symlink_parent = root / "symlink-parent"
            symlink_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                self.mod._atomic_write(symlink_parent / "run.json", "safe\n")
            self.assertFalse((real_parent / "run.json").exists())

    def test_atomic_publication_uses_pinned_parent_after_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent"
            parent.mkdir()
            moved = root / "moved"
            target = parent / "run.json"
            replace = self.mod.os.replace

            def rename_parent_then_replace(source, destination, *args, **kwargs):
                parent.rename(moved)
                parent.mkdir()
                return replace(source, destination, *args, **kwargs)

            with mock.patch.object(
                self.mod.os, "replace", side_effect=rename_parent_then_replace
            ):
                try:
                    self.mod._atomic_write(target, "pinned\n")
                except FileNotFoundError as exc:
                    self.fail(f"publication lost its parent pin: {exc}")
            self.assertEqual((moved / "run.json").read_text(encoding="utf-8"), "pinned\n")
            self.assertFalse((parent / "run.json").exists())

    def test_atomic_publication_does_not_ignore_parent_fsync_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run.json"
            fsync_calls = 0
            real_fsync = self.mod.os.fsync

            def fail_parent_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("parent fsync failed")
                return real_fsync(fd)

            with mock.patch.object(self.mod.os, "fsync", side_effect=fail_parent_fsync):
                with self.assertRaisesRegex(OSError, "parent fsync failed"):
                    self.mod._atomic_write(target, "must fail closed\n")

    def test_atomic_publication_fails_when_parent_open_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run.json"
            real_open = self.mod.os.open
            directory_flag = getattr(self.mod.os, "O_DIRECTORY", 0)

            def fail_directory_open(path, flags, *args, **kwargs):
                if flags & directory_flag:
                    raise OSError("parent open failed")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(self.mod.os, "open", side_effect=fail_directory_open):
                with self.assertRaisesRegex(OSError, "parent open failed"):
                    self.mod._atomic_write(target, "must fail closed\n")

    def test_main_rejects_symlink_artifacts_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            config.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\n[resources]\n",
                encoding="utf-8",
            )
            artifacts = {
                "run.json": root / "run.json",
                "run.state.json": root / "run.state.json",
                "run.progress.yaml": root / "run.progress.yaml",
            }
            for artifact_name, artifact in artifacts.items():
                artifact.symlink_to(root / f"missing-{artifact_name}")
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(artifacts["run.state.json"]),
                            "--progress", str(artifacts["run.progress.yaml"]),
                            "--out", str(artifacts["run.json"]),
                        ]
                    )
                self.assertTrue(artifact.is_symlink())
                artifact.unlink()

    def test_resume_rejects_live_different_owner_before_rewriting_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            state_path = root / "run.state.json"
            progress_path = root / "run.progress.yaml"
            out_path = root / "run.json"
            config.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\n[resources]\n",
                encoding="utf-8",
            )
            checkpoint = self.mod._new_state()
            self.mod._ensure_state_capacity(checkpoint, 1, [1])
            checkpoint["completed_waves"] = [0]
            checkpoint["waves"][0] = {"concurrency": 1}
            owner = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            try:
                stat = Path(f"/proc/{owner.pid}/stat").read_text(encoding="utf-8")
                start_time = int(stat.rsplit(")", 1)[1].split()[19])
                checkpoint["pid"] = owner.pid
                checkpoint["start_time"] = start_time
                state_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                progress_path.write_text("keep progress\n", encoding="utf-8")
                out_path.write_text("keep output\n", encoding="utf-8")
                before = {
                    path: path.read_bytes()
                    for path in (state_path, progress_path, out_path)
                }
                with self.assertRaisesRegex(RuntimeError, "saved benchmark owner is still live"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state_path),
                            "--progress", str(progress_path), "--out", str(out_path),
                            "--resume",
                        ]
                    )
                for path, content in before.items():
                    self.assertEqual(path.read_bytes(), content)
            finally:
                owner.terminate()
                owner.wait()

    def test_resume_rejects_malformed_present_owner_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            state_path = root / "run.state.json"
            progress_path = root / "run.progress.yaml"
            out_path = root / "run.json"
            config.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\n[resources]\n",
                encoding="utf-8",
            )
            checkpoint = self.mod._new_state()
            self.mod._ensure_state_capacity(checkpoint, 1, [1])
            checkpoint["pid"] = os.getpid()
            checkpoint["start_time"] = "not-an-integer"
            state_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            progress_path.write_text("keep progress\n", encoding="utf-8")
            out_path.write_text("keep output\n", encoding="utf-8")
            before = {
                path: path.read_bytes()
                for path in (state_path, progress_path, out_path)
            }
            with self.assertRaisesRegex(RuntimeError, "malformed checkpoint owner identity"):
                self.mod.main(
                    [
                        "--config", str(config), "--state", str(state_path),
                        "--progress", str(progress_path), "--out", str(out_path),
                        "--resume",
                    ]
                )
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_resume_rejects_ownerless_or_malformed_schema1_before_mutation(self):
        cases = ("ownerless", "duplicate-completed", "out-of-range", "bad-plan", "bad-config")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "run.toml"
                state_path = root / "run.state.json"
                progress_path = root / "run.progress.yaml"
                out_path = root / "run.json"
                config.write_text(
                    '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                    "[execution]\nconcurrencies = [1]\n[resources]\n",
                    encoding="utf-8",
                )
                checkpoint = self.mod._new_state()
                self.mod._ensure_state_capacity(checkpoint, 1, [1])
                checkpoint["waves"][0] = {"concurrency": 1}
                checkpoint["completed_waves"] = [0]
                if case != "ownerless":
                    checkpoint["pid"], checkpoint["start_time"] = self.mod._process_identity()
                if case == "duplicate-completed":
                    checkpoint["completed_waves"] = [0, 0]
                elif case == "out-of-range":
                    checkpoint["completed_waves"] = [1]
                elif case == "bad-plan":
                    checkpoint["planned_wave_ids"] = [0, 0]
                elif case == "bad-config":
                    checkpoint["config_epochs"] = []
                state_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                with mock.patch.object(
                    self.mod, "_ensure_state_capacity", wraps=self.mod._ensure_state_capacity
                ) as capacity:
                    with mock.patch.object(
                        self.mod, "_checkpoint", wraps=self.mod._checkpoint
                    ) as checkpoint_writer:
                        with mock.patch.object(self.mod, "run_wave") as benchmark:
                            with self.assertRaises(Exception) as caught:
                                self.mod.main(
                                    [
                                        "--config", str(config), "--state", str(state_path),
                                        "--progress", str(progress_path), "--out", str(out_path),
                                        "--resume",
                                    ]
                                )
                self.assertRegex(str(caught.exception), "refusing resume")
                capacity.assert_not_called()
                checkpoint_writer.assert_not_called()
                benchmark.assert_not_called()

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

    def test_resume_rejects_changed_active_config_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            state = root / "run.state.json"
            progress = root / "run.progress.yaml"
            out = root / "run.json"
            config.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m0"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\n[resources]\n",
                encoding="utf-8",
            )
            with mock.patch.object(self.mod, "run_wave", side_effect=RuntimeError("stop")):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    )
            config.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m1"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\n[resources]\n",
                encoding="utf-8",
            )
            with mock.patch.object(self.mod, "run_wave") as run_wave:
                with self.assertRaisesRegex(RuntimeError, "active wave config epoch"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out), "--resume",
                        ]
                    )
            run_wave.assert_not_called()

    def test_resume_strictly_recomputes_epoch_and_wave_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "run.toml"
            state_path = root / "run.state.json"
            progress_path = root / "run.progress.yaml"
            out_path = root / "run.json"
            config_path.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\nmax_tokens = 7\n[resources]\n",
                encoding="utf-8",
            )
            config = self.mod.load_config(config_path)
            epoch = "0" * 64
            result = _completed_wave(
                1,
                wave=0,
                config_epoch=epoch,
                provider="p",
                endpoint="http://e/v1",
                model="m",
                max_tokens=7,
            )
            state = self.mod._new_state()
            self.mod._ensure_state_capacity(state, 1, [1])
            state["waves"][0] = result
            state["completed_waves"] = [0]
            state["completed_work_units"] = 1
            state["config_epochs"] = {epoch: config}
            state["pid"], state["start_time"] = (999999, 1)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            progress_path.write_text("keep\n", encoding="utf-8")
            out_path.write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "config epoch|provenance"):
                self.mod.main(
                    [
                        "--config", str(config_path), "--state", str(state_path),
                        "--progress", str(progress_path), "--out", str(out_path), "--resume",
                    ]
                )

    def test_loaded_completed_wave_schema_is_strict_and_rejected_before_mutation(self):
        cases = {
            "counts": lambda result: result.update(n_ok=0),
            "metric_type": lambda result: result["requests"][0].update(prompt_tokens="6000"),
            "missing_provenance": lambda result: result.pop("config_epoch"),
            "missing_finish_reason": lambda result: result["requests"][0].update(finish_reason=None),
        }
        for case, forge in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "run.toml"
                state_path = root / "run.state.json"
                progress_path = root / "run.progress.yaml"
                out_path = root / "run.json"
                config.write_text(
                    '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                    "[execution]\nconcurrencies = [1]\nmax_tokens = 7\n[resources]\n",
                    encoding="utf-8",
                )
                state = self.mod._new_state()
                self.mod._ensure_state_capacity(state, 1, [1])
                epoch = self.mod.config_epoch(self.mod.load_config(config))
                result = _completed_wave(
                    1,
                    wave=0,
                    config_epoch=epoch,
                    provider="p",
                    endpoint="http://e/v1",
                    model="m",
                    max_tokens=7,
                )
                forge(result)
                state["waves"][0] = result
                state["completed_waves"] = [0]
                state["pid"], state["start_time"] = (999999, 1)
                state_path.write_text(json.dumps(state), encoding="utf-8")
                progress_path.write_text("keep progress\n", encoding="utf-8")
                out_path.write_text("keep output\n", encoding="utf-8")
                before = {
                    path: path.read_bytes()
                    for path in (state_path, progress_path, out_path)
                }
                with mock.patch.object(self.mod, "run_wave") as benchmark:
                    with mock.patch.object(self.mod, "_checkpoint") as checkpoint:
                        with self.assertRaisesRegex(RuntimeError, "refusing resume"):
                            self.mod.main(
                                [
                                    "--config", str(config), "--state", str(state_path),
                                    "--progress", str(progress_path), "--out", str(out_path),
                                    "--resume",
                                ]
                            )
                benchmark.assert_not_called()
                checkpoint.assert_not_called()
                for path, content in before.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_reload_plan_failure_publishes_incomplete_report_and_failure_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            state = root / "run.state.json"
            progress = root / "run.progress.yaml"
            out = root / "run.json"

            def write_config(concurrencies):
                config.write_text(
                    '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                    f"[execution]\nconcurrencies = {concurrencies}\nmax_tokens = 7\n"
                    "[resources]\nclient_workers = 4\n",
                    encoding="utf-8",
                )

            write_config([1, 2])

            def reload_with_invalid_completed_prefix(*args, **kwargs):
                write_config([3, 4])
                return _completed_wave(1)

            with mock.patch.object(
                self.mod, "run_wave", side_effect=reload_with_invalid_completed_prefix
            ):
                with self.assertRaisesRegex(ValueError, "changed completed wave"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertFalse(report["complete"])
            self.assertEqual(report["failure"]["wave"], 1)
            checkpoint = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["completed_waves"], [0])
            self.assertEqual(checkpoint["failure"]["wave"], 1)
            progress_text = progress.read_text(encoding="utf-8")
            self.assertIn("active_wave: 1", progress_text)
            self.assertIn("failure_wave: 1", progress_text)

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
                return _completed_wave(
                    n, request_timeout_s=kwargs.get("request_timeout_s", 600.0)
                )

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

    def test_terminal_report_uses_last_executed_config_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "run.toml"
            state = root / "run.state.json"
            progress = root / "run.progress.yaml"
            out = root / "run.json"

            def write_config(provider, model, endpoint):
                config.write_text(
                    f'[run]\nprovider = "{provider}"\nmodel_id = "{model}"\n'
                    f'endpoint = "{endpoint}"\n'
                    "[execution]\nconcurrencies = [1, 2]\nmax_tokens = 7\n"
                    "[resources]\nclient_workers = 2\n",
                    encoding="utf-8",
                )

            write_config("p0", "m0", "http://e0/v1")

            def reload_after_final_wave(base_url, model, wave, n, min_tokens, max_tokens, **kwargs):
                if wave == 0:
                    write_config("p1", "m1", "http://e1/v1")
                else:
                    write_config("p2", "m2", "http://e2/v1")
                return _completed_wave(n)

            with mock.patch.object(self.mod, "run_wave", side_effect=reload_after_final_wave):
                self.assertEqual(
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    ),
                    0,
                )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["model"], "m1")
            self.assertEqual(report["base_url"], "http://e1/v1")
            self.assertEqual(report["endpoint"], "http://e1/v1")
            self.assertEqual(report["config_epoch"], report["waves"][-1]["config_epoch"])

    def test_final_wave_config_mutation_is_not_reloaded_into_completion(self):
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

            def mutate_after_final(*args, **kwargs):
                config.write_text(
                    "[execution]\nconcurrencies = [1]\nunknown = true\n",
                    encoding="utf-8",
                )
                return _completed_wave(1)

            with mock.patch.object(self.mod, "run_wave", side_effect=mutate_after_final):
                self.assertEqual(
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    ),
                    0,
                )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "COMPLETE")
            self.assertTrue(report["complete"])

    def test_progress_uses_request_work_units_for_rate_and_eta(self):
        state = self.mod._new_state()
        plan_config = self.mod.load_config(None)
        plan_config["execution"]["concurrencies"] = [1, 8]
        epoch = self.mod.config_epoch(plan_config)
        self.mod._ensure_state_capacity(state, 2, [1, 8])
        state["waves"][0] = _completed_wave(
            1,
            wave=0,
            config_epoch=epoch,
            provider="p",
            endpoint="http://e/v1",
            model="m",
            max_tokens=7,
        )
        state["completed_waves"] = [0]
        state["config_epochs"][epoch] = plan_config
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            progress_path = root / "progress.yaml"
            self.mod._checkpoint(
                state,
                state_path,
                progress_path,
                elapsed_s=1.0,
                active_wave=1,
                total=2,
                config_epoch_value=epoch,
            )
            progress = progress_path.read_text(encoding="utf-8")
            self.assertIn("completed_work_units: 1", progress)
            self.assertIn("total_work_units: 9", progress)
            self.assertIn("work_rate: 1.0", progress)
            self.assertIn("eta_s: 8.0", progress)
            self.assertIn("work_unit: 8", progress)

    def test_request_errors_produce_incomplete_report_and_nonzero_result(self):
        cases = (
            {
                "n_ok": 0,
                "n_fail": 1,
                "requests": [{"ok": False, "error_type": "HTTPError", "error": "busy"}],
            },
            {
                "n_ok": 1,
                "n_fail": 0,
                "requests": [{"ok": True, "error": "unexpected server error"}],
            },
        )
        for failed_result in cases:
            with self.subTest(failed_result=failed_result), tempfile.TemporaryDirectory() as tmp:
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
                result = {
                    "concurrency": 1,
                    "sum_completion": 0,
                    **failed_result,
                }
                with mock.patch.object(self.mod, "run_wave", return_value=result):
                    with self.assertRaisesRegex(RuntimeError, "incomplete|failed"):
                        self.mod.main(
                            [
                                "--config", str(config), "--state", str(state),
                                "--progress", str(progress), "--out", str(out),
                            ]
                        )
                report = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "INCOMPLETE")
                self.assertFalse(report["complete"])
                checkpoint = json.loads(state.read_text(encoding="utf-8"))
                self.assertEqual(checkpoint["completed_waves"], [])

    def test_run_wave_exception_replaces_stale_complete_output_and_persists_failure(self):
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
            out.write_text('{"status":"COMPLETE","complete":true}\n', encoding="utf-8")
            with mock.patch.object(self.mod, "run_wave", side_effect=ValueError("worker boom")):
                with self.assertRaisesRegex(ValueError, "worker boom"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertFalse(report["complete"])
            self.assertEqual(report["failure"]["stage"], "run_wave")
            self.assertEqual(report["failure"]["error"], "worker boom")
            checkpoint = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["failure"]["stage"], "run_wave")
            self.assertEqual(checkpoint["active_wave"], 0)
            self.assertIn('failure_stage: "run_wave"', progress.read_text(encoding="utf-8"))

    def test_result_processing_exception_replaces_stale_complete_output(self):
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
            out.write_text('{"status":"COMPLETE","complete":true}\n', encoding="utf-8")
            with mock.patch.object(self.mod, "run_wave", return_value=object()):
                with self.assertRaises(AttributeError):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertFalse(report["complete"])
            self.assertEqual(report["failure"]["stage"], "result_processing")
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["failure"]["stage"], "result_processing")

    def test_keyboard_interrupt_replaces_stale_complete_output_and_preserves_original_error(self):
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
            out.write_text('{"status":"COMPLETE","complete":true}\n', encoding="utf-8")
            with mock.patch.object(self.mod, "run_wave", side_effect=KeyboardInterrupt("user stop")):
                with self.assertRaisesRegex(KeyboardInterrupt, "user stop"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertFalse(report["complete"])
            self.assertEqual(report["failure"]["stage"], "run_wave")
            self.assertEqual(report["failure"]["error_type"], "KeyboardInterrupt")
            self.assertEqual(report["failure"]["error"], "user stop")
            checkpoint = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["failure"]["stage"], "run_wave")
            self.assertIn('failure_stage: "run_wave"', progress.read_text(encoding="utf-8"))

    def test_checkpoint_transition_failure_publishes_incomplete_without_recursing(self):
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
            out.write_text('{"status":"COMPLETE","complete":true}\n', encoding="utf-8")
            with mock.patch.object(self.mod, "_checkpoint", side_effect=OSError("checkpoint transition failed")) as checkpoint:
                with self.assertRaisesRegex(OSError, "checkpoint transition failed"):
                    self.mod.main(
                        [
                            "--config", str(config), "--state", str(state),
                            "--progress", str(progress), "--out", str(out),
                        ]
                    )
            checkpoint.assert_called_once()
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertFalse(report["complete"])
            self.assertEqual(report["failure"]["stage"], "checkpoint_transition")
            self.assertEqual(report["failure"]["error"], "checkpoint transition failed")
            checkpoint_data = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint_data["failure"]["stage"], "checkpoint_transition")
            self.assertIn('failure_stage: "checkpoint_transition"', progress.read_text(encoding="utf-8"))

    def test_derived_wave_metrics_are_consistent_on_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "run.toml"
            config_path.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\nmax_tokens = 7\n[resources]\nrequest_timeout_s = 11\n",
                encoding="utf-8",
            )
            config = self.mod.load_config(config_path)
            epoch = self.mod.config_epoch(config)
            base = _completed_wave(
                1, wave=0, config_epoch=epoch, provider="p", endpoint="http://e/v1",
                model="m", max_tokens=7, request_timeout_s=11,
            )
            cases = {
                "wave_wall": lambda result: result.update(wave_wall_s=0.0),
                "aggregate_decode": lambda result: result.update(agg_decode_tok_s=999.0),
                "aggregate_prompt": lambda result: result.update(agg_prompt_tok_s=999.0),
                "request_decode": lambda result: result["requests"][0].update(decode_tok_s=999.0),
                "logical_wall": lambda result: result["requests"][0].update(logical_wall_s=0.3),
            }
            for name, forge in cases.items():
                with self.subTest(case=name):
                    result = json.loads(json.dumps(base))
                    forge(result)
                    error = self.mod._wave_acceptance_error(result, 1, config)
                    self.assertIsNotNone(error)
                    self.assertIn(name.replace("_", " ").split()[0], error.lower())

    def test_derived_wave_metrics_allow_small_json_rounding(self):
        config = self.mod.load_config(None)
        config["execution"]["concurrencies"] = [1]
        epoch = self.mod.config_epoch(config)
        result = _completed_wave(
            1, wave=0, config_epoch=epoch, provider=config["run"]["provider"],
            endpoint=config["run"]["endpoint"], model=config["run"]["model_id"],
            max_tokens=config["execution"]["max_tokens"],
        )
        result["wave_wall_s"] = 0.2000000001
        result["agg_decode_tok_s"] = 2.0 / result["wave_wall_s"]
        result["agg_prompt_tok_s"] = MIN_PROMPT_TOKENS / result["wave_wall_s"]
        result["requests"][0]["decode_tok_s"] = 20.0000000001
        result["requests"][0]["retry_overhead_s"] = 1e-10
        result["requests"][0]["logical_wall_s"] = 0.2000000001
        self.assertIsNone(self.mod._wave_acceptance_error(result, 1, config))

    def test_resume_rejects_forged_derived_wave_metrics_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "run.toml"
            state_path = root / "run.state.json"
            progress_path = root / "run.progress.yaml"
            out_path = root / "run.json"
            config_path.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1]\nmax_tokens = 7\n[resources]\nrequest_timeout_s = 11\n",
                encoding="utf-8",
            )
            config = self.mod.load_config(config_path)
            epoch = self.mod.config_epoch(config)
            result = _completed_wave(
                1, wave=0, config_epoch=epoch, provider="p", endpoint="http://e/v1",
                model="m", max_tokens=7, request_timeout_s=11, agg_prompt_tok_s=1.0,
            )
            state = self.mod._new_state()
            self.mod._ensure_state_capacity(state, 1, [1])
            state["waves"][0] = result
            state["completed_waves"] = [0]
            state["completed_work_units"] = 1
            state["config_epochs"] = {epoch: config}
            state["pid"], state["start_time"] = (999999, 1)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            progress_path.write_text("keep progress\n", encoding="utf-8")
            out_path.write_text("keep output\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "refusing resume: malformed checkpoint"):
                self.mod.main(
                    [
                        "--config", str(config_path), "--state", str(state_path),
                        "--progress", str(progress_path), "--out", str(out_path), "--resume",
                    ]
                )
            self.assertEqual(out_path.read_text(encoding="utf-8"), "keep output\n")

    def test_schema1_complete_checkpoint_is_durably_migrated_before_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "run.toml"
            state_path = root / "run.state.json"
            progress_path = root / "run.progress.yaml"
            out_path = root / "run.json"
            config_path.write_text(
                '[run]\nprovider = "p"\nmodel_id = "m"\nendpoint = "http://e/v1"\n'
                "[execution]\nconcurrencies = [1, 2]\nmax_tokens = 7\n[resources]\nrequest_timeout_s = 11\n",
                encoding="utf-8",
            )
            config = self.mod.load_config(config_path)
            epoch = self.mod.config_epoch(config)
            waves = [
                _completed_wave(
                    n, wave=wave, config_epoch=epoch, provider="p", endpoint="http://e/v1",
                    model="m", max_tokens=7, request_timeout_s=11,
                )
                for wave, n in enumerate((1, 2))
            ]
            for result in waves:
                result.pop("work_units", None)
            legacy = {
                "schema_version": 1,
                "run_id": "legacy-complete",
                "started_at": 1.0,
                "elapsed_s": 0.5,
                "total": 2,
                "planned_total": 2,
                "planned_wave_ids": [0, 1],
                "completed_waves": [0, 1],
                "waves": waves,
                "config_epochs": {epoch: config},
                "pid": 999999,
                "start_time": 1,
            }
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            with mock.patch.object(self.mod, "run_wave") as run_wave:
                self.assertEqual(
                    self.mod.main(
                        [
                            "--config", str(config_path), "--state", str(state_path),
                            "--progress", str(progress_path), "--out", str(out_path), "--resume",
                        ]
                    ),
                    0,
                )
            run_wave.assert_not_called()
            migrated = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["completed_work_units"], 3)
            self.assertIsNone(migrated["active_wave"])
            self.assertIn("completed: 3", progress_path.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(out_path.read_text(encoding="utf-8"))["status"], "COMPLETE")

    def test_dedicated_64k_config_has_five_waves_per_level_and_raw_requirements(self):
        example = ROOT / "examples" / "sglang-64k-concurrency-throughput.toml"
        self.assertTrue(example.is_file())
        loaded = self.mod.load_config(example)
        levels = [1, 2, 4, 6, 8, 10, 12, 14, 16]
        self.assertEqual(loaded["execution"]["min_prompt_tokens"], 64000)
        self.assertEqual(loaded["execution"]["concurrencies"], [level for level in levels for _ in range(5)])
        self.assertGreaterEqual(loaded["resources"]["client_workers"], 16)
        self.assertGreaterEqual(loaded["execution"]["max_tokens"], 128)
        self.assertGreaterEqual(loaded["resources"]["request_timeout_s"], 600)
        self.assertGreaterEqual(loaded["resources"]["wave_timeout_s"], loaded["resources"]["request_timeout_s"])
        self.assertTrue(loaded["run"]["endpoint"].endswith("/v1"))
        self.assertTrue(loaded["run"]["model_id"])
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        copy = 'cp "$repo_root/examples/sglang-64k-concurrency-throughput.toml" "$run_dir/run.toml"'
        self.assertIn(copy, readme)
        self.assertLess(readme.index(copy), readme.index('--config "$run_dir/run.toml"', readme.index(copy)))
        self.assertIn("explicitly authorized operator", readme)
        self.assertIn("does **not** activate,", readme)
        dedicated_resume = readme[readme.index("### Dedicated 64K resume") :]
        dedicated_resume = dedicated_resume[:dedicated_resume.index("### Legacy 6K run")]
        for text in (
            "set -euo pipefail",
            'run_dir="$repo_root/sglang-64k-run"',
            'config="$run_dir/run.toml"',
            'state="$run_dir/run.state.json"',
            "--preflight",
            "--resume",
            'pid_start_tmp="$run_dir/.run.pid-start.',
            'mv -f -- "$pid_start_tmp" "$run_dir/run.pid-start"',
        ):
            self.assertIn(text, dedicated_resume)


if __name__ == "__main__":
    unittest.main()
