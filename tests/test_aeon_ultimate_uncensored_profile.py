"""Contract checks for the AEON Ultimate Uncensored GB10 profile."""

from __future__ import annotations

import json
import shlex
import tomllib
import unittest
from pathlib import Path

from test_vllm_no_swap_unit_contracts import _logical_argv


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profile/aeon-ultimate-uncensored-nvfp4"
UNIT = PROFILE / "vllm-aeon-ultimate-uncensored-nvfp4.service"
ENV = ROOT / "config/aeon-dflash-profiles/aeon-ultimate-uncensored-nvfp4.env"
GUARD_CONFIG = ROOT / "config/llm-guard-proxy/config.toml"
DEPLOYMENT_GUIDANCE = ROOT / "docs/deployment/AGENTS.md"
IMAGE = (
    "ghcr.io/aeon-7/aeon-vllm-ultimate@"
    "sha256:e62ac10d744ed7c8f3dd4d5631be0f7615870a88c327db9c1d382a27b36a61ee"
)
ALIASES = {
    "aeon",
    "aeon-ultimate",
    "abliterated-qwen-latest-27b-nvfp4",
}


def _unit_text() -> str:
    return UNIT.read_text()


def _argv() -> list[str]:
    commands = _logical_argv(_unit_text(), "ExecStart")
    if len(commands) != 1:
        raise AssertionError(f"expected one ExecStart command, got {len(commands)}")
    return commands[0]


def _option_value(argv: list[str], option: str) -> str:
    index = argv.index(option)
    return argv[index + 1]


class AeonUltimateUncensoredProfileTests(unittest.TestCase):
    def test_profile_files_and_memory_profile(self) -> None:
        self.assertTrue(PROFILE.is_dir())
        self.assertTrue(UNIT.is_file())
        self.assertEqual(ENV.read_text(), "AEON_GPU_MEMORY_UTILIZATION=0.545\n")

    def test_unit_uses_pinned_image_model_and_draft_mounts(self) -> None:
        unit = _unit_text()
        argv = _argv()
        self.assertIn(IMAGE, unit)
        self.assertIn(
            "-v /home/obj/models/AEON-ULTIMATE-UNCENSORED-NVFP4-beta-version:/model:ro",
            unit,
        )
        self.assertIn(
            "-v /home/obj/models/z-lab/Qwen3.8-27B-DFlash2:/draft:ro",
            unit,
        )
        self.assertEqual(_option_value(argv, "--model"), "/model")
        self.assertNotIn("model-mtp-transplant.safetensors", unit)
        self.assertNotIn("mtp", " ".join(argv).lower())

    def test_unit_enforces_72g_no_swap_envelope(self) -> None:
        unit = _unit_text()
        argv = _argv()
        self.assertIn("MemoryMax=72G", unit)
        self.assertIn("MemorySwapMax=0", unit)
        self.assertIn("--memory", argv)
        self.assertEqual(argv[argv.index("--memory") + 1], "72g")
        self.assertEqual(argv[argv.index("--memory-swap") + 1], "72g")
        self.assertEqual(argv[argv.index("--memory-swappiness") + 1], "0")
        self.assertNotIn("--swap-space", argv)
        self.assertNotIn("--rm", argv)

    def test_unit_uses_spark_engine_contract_without_yarn(self) -> None:
        unit = _unit_text()
        argv = _argv()
        self.assertEqual(_option_value(argv, "--quantization"), "compressed-tensors")
        self.assertEqual(_option_value(argv, "--attention-backend"), "TRITON_ATTN")
        self.assertEqual(_option_value(argv, "--kv-cache-dtype"), "fp8")
        self.assertEqual(_option_value(argv, "--max-model-len"), "262144")
        self.assertEqual(_option_value(argv, "--max-num-batched-tokens"), "8192")
        self.assertIn("--enable-chunked-prefill", argv)
        self.assertIn("--no-enable-prefix-caching", argv)
        self.assertNotIn("--max-num-seqs", argv)
        self.assertNotIn("--enable-prefix-caching", argv)
        self.assertIn("--enforce-eager", argv)
        self.assertNotIn("--scheduler-reserve-full-isl", argv)
        self.assertNotIn("VLLM_ALLOW_LONG_MAX_MODEL_LEN", unit)
        self.assertNotIn("--hf-overrides", argv)
        self.assertNotIn("rope_scaling", unit)
        self.assertIn("--trust-remote-code", argv)
        self.assertIn("--tool-call-parser", argv)
        self.assertEqual(_option_value(argv, "--tool-call-parser"), "qwen3_coder")
        self.assertEqual(_option_value(argv, "--reasoning-parser"), "qwen3")
        self.assertIn("--enable-auto-tool-choice", argv)
        self.assertEqual(
            json.loads(_option_value(argv, "--override-generation-config")),
            {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "repetition_penalty": 1.05,
            },
        )

    def test_unit_uses_dflash2_n7_and_production_aliases(self) -> None:
        unit = _unit_text()
        argv = _argv()
        spec = json.loads(_option_value(argv, "--speculative-config"))
        self.assertEqual(spec, {
            "method": "dflash",
            "model": "/draft",
            "num_speculative_tokens": 7,
            "attention_backend": "TRITON_ATTN",
        })
        self.assertEqual(
            set(argv[argv.index("--served-model-name") + 1 : argv.index("--served-model-name") + 4]),
            ALIASES,
        )
        self.assertIn("100.105.4.92:18010:8000", unit)
        self.assertIn("--gpu-memory-utilization", argv)
        self.assertIn("${AEON_GPU_MEMORY_UTILIZATION}", unit)

    def test_unit_has_guardian_ready_and_mutual_text_conflict(self) -> None:
        unit = _unit_text()
        self.assertIn("--cgroup-parent app.slice", unit)
        self.assertIn("gb10_verify_vllm_no_swap.sh", unit)
        self.assertIn("llm_guard_proxy_publish_cgroup_registration.sh", unit)
        self.assertIn("gb10_service_ready.sh chat http://100.105.4.92:18010 aeon", unit)
        self.assertIn("--deadline 2800", unit)
        self.assertIn("TimeoutStartSec=3000", unit)
        self.assertIn("OOMScoreAdjust=800", unit)
        self.assertIn("After=network-online.target vllm-embedding.service vllm-querit-4b-reranker.service", unit)
        self.assertNotIn("Requires=", unit)
        self.assertIn("Conflicts=", unit)
        self.assertIn("vllm-aeon-qwen38-dflash.service", unit)
        self.assertIn("vllm-aeon-27b-dflash.service", unit)
        self.assertIn("sglang-qwen38-27b.service", unit)
        self.assertIn("EnvironmentFile=/home/obj/.config/gb10/aeon-dflash-profiles/aeon-ultimate-uncensored-nvfp4.env", unit)

    def test_guard_routes_all_18010_aliases_and_docs_record_candidate(self) -> None:
        config = tomllib.loads(GUARD_CONFIG.read_text())
        upstreams = [
            item for item in config["upstreams"] if item["name"] == "aeon-default-no-think"
        ]
        self.assertEqual(len(upstreams), 1)
        self.assertTrue(ALIASES <= set(upstreams[0]["match_models"]))
        self.assertEqual(upstreams[0]["upstream_model"], "aeon-ultimate")
        self.assertEqual(upstreams[0]["base_url"], "http://100.105.4.92:18010/v1")
        docs = DEPLOYMENT_GUIDANCE.read_text()
        for required in (
            "vllm-aeon-ultimate-uncensored-nvfp4.service",
            "AEON-ULTIMATE-UNCENSORED-NVFP4-beta-version",
            "DFlash n=7",
            "max-model-len=262144",
            "72G",
            "not deployed",
        ):
            self.assertIn(required, docs)


if __name__ == "__main__":
    unittest.main()
