from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EMBEDDING_UNIT = ROOT / "systemd" / "vllm-embedding.service"

VALIDATED_KV_MIB = 5_820
VALIDATED_KV_TOKENS = 41_376
CONTRACT_TOKENS = 32_768
MIN_KV_MARGIN_BPS = 400
OBSERVED_PEAK_BYTES = 16_870_580_224
MIN_UNADJUSTED_CAP_MARGIN_GIB = 4
EXPECTED_KV_REDUCTION_MIB = 1_020
MIN_PROJECTED_CAP_MARGIN_GIB = 5


def _numeric_arg(unit: str, flag: str, suffix: str = "") -> int:
    match = re.search(
        rf"{re.escape(flag)}\s+(\d+){re.escape(suffix)}(?:\s|\\|$)", unit
    )
    if match is None:
        raise AssertionError(f"missing numeric argument: {flag}")
    return int(match.group(1))


class EmbeddingServiceContractTests(unittest.TestCase):
    def test_32k_profile_has_bounded_kv_and_container_headroom(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        model_len = _numeric_arg(unit, "--max-model-len")
        kv_mib = _numeric_arg(unit, "--kv-cache-memory-bytes", "M")
        memory_gib = _numeric_arg(unit, "--memory", "g")
        swap_gib = _numeric_arg(unit, "--memory-swap", "g")
        helper_gib = _numeric_arg(
            unit,
            "gb10_enforce_docker_cgroup_limits.sh vllm-embedding",
        )

        self.assertEqual(model_len, CONTRACT_TOKENS)
        self.assertEqual(kv_mib, 4_800)
        self.assertEqual(memory_gib, 20)
        self.assertEqual(swap_gib, memory_gib)
        self.assertEqual(helper_gib, memory_gib)

        projected_kv_tokens = kv_mib * VALIDATED_KV_TOKENS // VALIDATED_KV_MIB
        required_kv_tokens = (
            CONTRACT_TOKENS * (10_000 + MIN_KV_MARGIN_BPS) + 9_999
        ) // 10_000
        self.assertGreaterEqual(projected_kv_tokens, required_kv_tokens)

        gib = 1024**3
        cap_bytes = memory_gib * gib
        self.assertGreaterEqual(
            cap_bytes - OBSERVED_PEAK_BYTES,
            MIN_UNADJUSTED_CAP_MARGIN_GIB * gib,
        )
        projected_peak_bytes = OBSERVED_PEAK_BYTES - EXPECTED_KV_REDUCTION_MIB * 1024**2
        self.assertGreaterEqual(
            cap_bytes - projected_peak_bytes,
            MIN_PROJECTED_CAP_MARGIN_GIB * gib,
        )

    def test_quality_and_throughput_semantics_remain_unchanged(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        self.assertIn("vllm serve Qwen/Qwen3-Embedding-8B", unit)
        self.assertIn(
            "--served-model-name qwen3-embedding-8b Qwen/Qwen3-Embedding-8B",
            unit,
        )
        self.assertIn("--convert embed", unit)
        self.assertIn("--dtype bfloat16", unit)
        self.assertEqual(_numeric_arg(unit, "--max-num-batched-tokens"), 8_192)
        self.assertEqual(_numeric_arg(unit, "--max-num-seqs"), 64)
        self.assertNotIn("--quantization", unit)
        self.assertNotIn("--kv-cache-dtype", unit)
        self.assertNotIn("--truncate-dim", unit)


if __name__ == "__main__":
    unittest.main()
