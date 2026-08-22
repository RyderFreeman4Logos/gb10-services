from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from test_vllm_no_swap_unit_contracts import _logical_argv


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "systemd" / "vllm-aeon-qwen38-dflash.service"
QWEN36_UNIT = ROOT / "systemd" / "vllm-aeon-27b-dflash.service"
SGLANG_UNIT = ROOT / "profile" / "qwen3.8-27b-nvfp4-sglang" / "sglang-qwen38-27b.service"
IMAGE_REPOSITORY = "ghcr.io/aeon-7/aeon-vllm-ultimate"
IMAGE_TAG = "latest"
IMAGE_DIGEST = "sha256:2fb855ffd6fbf4330cf9f4653c09d3e6584d197acba8e9e93a032da36bb4559f"
IMAGE = f"ghcr.io/aeon-7/aeon-vllm-ultimate@{IMAGE_DIGEST}"
IMAGE_CREATED = "2026-08-20T21:17:36.420048521-04:00"
ALIAS = "abliterated-qwen-latest-27b-nvfp4"
NVFP4_HOST = "/home/obj/models/Blackfrost-AI/Qwen3.8-27B-ABLITERATED-NVFP4"
NVFP4_MOUNT = "/models/qwen38-nvfp4"
CIDFILE = "%t/gb10-memory-guardian/aeon-qwen38-text.cid"
CONTAINER = "vllm-aeon-qwen38-dflash"
COMPILE_HOST = "/home/obj/.cache/vllm-compile/aeon-qwen38-v0271-2fb855"
COMPILE_CONTAINER = "/var/cache/vllm/aeon-qwen38-v0271"


def _unit_text() -> str:
    return UNIT.read_text() if UNIT.is_file() else ""


def _runtime_argv(text: str) -> list[str]:
    command = _logical_argv(text, "ExecStart")
    if len(command) != 1:
        return []
    argv = command[0]
    return argv[argv.index("serve") + 1 :] if "serve" in argv else []


def _option_value(argv: list[str], option: str) -> str:
    index = argv.index(option)
    return argv[index + 1]


class AeonQwen38CanaryUnitContractTests(unittest.TestCase):
    def test_qwen38_unit_exists(self) -> None:
        self.assertTrue(UNIT.is_file(), f"missing canary unit {UNIT}")

    def test_unit_mounts_flat_qwen38_nvfp4_without_dflash2_and_uses_mtp(self) -> None:
        text = _unit_text()
        self.assertIn(f"{NVFP4_HOST}:{NVFP4_MOUNT}:ro", text)
        self.assertNotIn("Qwen3.8-27B-DFlash2", text)
        self.assertNotIn("/models/qwen38-dflash2", text)
        self.assertNotIn("/snapshots/", text)
        argv = _runtime_argv(text)
        self.assertEqual(argv[0], NVFP4_MOUNT)
        speculative = json.loads(_option_value(argv, "--speculative-config"))
        self.assertEqual(
            speculative,
            {"method": "mtp", "num_speculative_tokens": 3},
        )

    def test_unit_pins_latest_aeon_engine_and_quality_envelope(self) -> None:
        text = _unit_text()
        argv = _runtime_argv(text)
        exec_start = _logical_argv(text, "ExecStart")
        self.assertEqual(len(exec_start), 1)
        self.assertIn(f"image tag: {IMAGE_TAG} (repository {IMAGE_REPOSITORY})", text)
        self.assertIn(f"resolved immutable digest: {IMAGE_DIGEST}", text)
        self.assertIn(f"created: {IMAGE_CREATED}", text)
        self.assertNotIn(f"{IMAGE_REPOSITORY}:{IMAGE_TAG}", " ".join(exec_start[0]))
        self.assertIn(IMAGE, text)
        self.assertIn("/opt/hang_guard/aeon_vllm_wrapper.py", text)
        self.assertEqual(_option_value(argv, "--max-model-len"), "262144")
        self.assertEqual(_option_value(argv, "--kv-cache-dtype"), "fp8_e4m3")
        self.assertEqual(_option_value(argv, "--mamba-cache-dtype"), "float32")
        self.assertIn("--mamba-ssm-cache-dtype", argv)
        self.assertEqual(_option_value(argv, "--mamba-ssm-cache-dtype"), "float32")
        self.assertEqual(_option_value(argv, "--quantization"), "modelopt")
        self.assertEqual(_option_value(argv, "--attention-backend"), "TRITON_ATTN")
        self.assertEqual(_option_value(argv, "--gpu-memory-utilization"), "${AEON_GPU_MEMORY_UTILIZATION}")
        self.assertNotIn("--kv-cache-memory-bytes", text)
        self.assertNotIn("--max-total-tokens", text)
        self.assertIn("-e VLLM_USE_V2_MODEL_RUNNER=0", text)
        self.assertIn("-e AEON_DEFAULT_THINKING_TOKEN_BUDGET=32768", text)
        self.assertIn("--no-enable-prefix-caching", argv)
        self.assertNotIn("--enable-prefix-caching", argv)
        self.assertIn("--memory 74g", text)
        self.assertIn("--memory-swap 74g", text)
        self.assertIn("--memory-swappiness 0", text)
        self.assertIn("MemoryMax=74G", text)
        self.assertIn("MemorySwapMax=0", text)

    def test_unit_omits_pooling_only_prefill_flags_for_generative_serve(self) -> None:
        argv = _runtime_argv(_unit_text())
        self.assertNotIn("--max-num-partial-prefills", argv)
        self.assertNotIn("--max-long-partial-prefills", argv)

    def test_unit_serves_stable_alias_first_and_uses_distinct_identity(self) -> None:
        text = _unit_text()
        argv = _runtime_argv(text)
        if "--served-model-name" not in argv:
            self.fail("canary unit has no parseable serve argv")
        served = argv[argv.index("--served-model-name") + 1 :]
        self.assertEqual(served[0], ALIAS)
        self.assertIn(f"--name {CONTAINER}", text)
        self.assertIn(f"--cidfile={CIDFILE}", text)
        self.assertNotIn("--name vllm-aeon-27b-dflash", text)
        self.assertNotIn("aeon-text.cid", text)
        self.assertIn(f"{COMPILE_HOST}:{COMPILE_CONTAINER}", text)
        self.assertNotIn("aeon-qwen36-v0271", text)

    def test_unit_orders_after_neighbors_and_conflicts_with_other_text_backends(self) -> None:
        text = _unit_text()
        self.assertIn("After=vllm-embedding.service", text)
        self.assertIn("After=vllm-querit-4b-reranker.service", text)
        conflicts = next(line for line in text.splitlines() if line.startswith("Conflicts="))
        self.assertIn(SGLANG_UNIT.name, conflicts)
        self.assertIn(QWEN36_UNIT.name, conflicts)
        self.assertIn("vllm-aeon-27b-dflash-hikv.service", conflicts)
        self.assertRegex(
            text,
            rf"(?m)^ExecStartPost=.*gb10_service_ready\.sh chat http://100\.105\.4\.92:18010 {ALIAS} --deadline 2800$",
        )
        self.assertIn("EnvironmentFile=/home/obj/.config/gb10/aeon-dflash-profiles/active.env", text)

    def test_unit_documents_canary_and_keeps_capacity_claim_honest(self) -> None:
        text = _unit_text()
        self.assertIn("canary of the AEON engine + incumbent Qwen3.8 weights", text)
        self.assertIn("KV≥262144 is a live receipt, not a source claim", text)
        self.assertIn("MTP K=3", text)
        self.assertNotIn("DFlash n=10", text)
        self.assertIn("SGLang", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
