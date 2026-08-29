"""Focused tests for the vLLM healthcheck and metrics stall probe."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "healthcheck_vllm.py"


def _load_module():
    spec = spec_from_file_location("healthcheck_vllm_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HealthcheckVllmTest(unittest.TestCase):
    def test_metrics_sum_engines_and_detect_activity_without_waiting(self):
        if not SCRIPT.exists():
            self.fail("new healthcheck script is missing")
        mod = _load_module()
        first = (
            b'# TYPE vllm:generation_tokens_total counter\n'
            b'vllm:generation_tokens_total{engine="0",model_name="aeon"} 10.0 1\n'
            b'vllm:generation_tokens_total{engine="1",model_name="aeon"} 4.0 1\n'
            b'vllm:generation_tokens_total_created{engine="0",model_name="aeon"} 0.0\n'
        )
        second = (
            b'vllm:generation_tokens_total{engine="0",model_name="aeon"} 18.0 2\n'
            b'vllm:generation_tokens_total{engine="1",model_name="aeon"} 5.0 2\n'
        )
        self.assertEqual(mod.parse_duration("2m"), 120)
        self.assertEqual(mod.parse_generation_tokens_total(first.decode()), 14.0)
        sleeps = []
        with mock.patch.object(
            mod.urllib.request, "urlopen", side_effect=[_Response(first), _Response(second)]
        ):
            status = mod.check_output_token_activity(
                "http://example.test:18010", 60, 5, sleep_fn=sleeps.append
            )
        self.assertEqual(status, 0)
        self.assertEqual(sleeps, [60])

        with mock.patch.object(
            mod.urllib.request, "urlopen", side_effect=[_Response(first), _Response(first)]
        ):
            self.assertEqual(
                mod.check_output_token_activity(
                    "http://example.test:18010", 60, 5, sleep_fn=lambda _: None
                ),
                1,
            )
        with mock.patch.object(
            mod.urllib.request, "urlopen", side_effect=[_Response(b"vllm:other_total 4\n")]
        ):
            self.assertEqual(
                mod.check_output_token_activity(
                    "http://example.test:18010", 60, 5, sleep_fn=lambda _: None
                ),
                2,
            )


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


if __name__ == "__main__":
    unittest.main()
