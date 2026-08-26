from __future__ import annotations

import json
import unittest
from pathlib import Path

from test_vllm_no_swap_unit_contracts import _logical_argv


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "profile" / "qwen3.8-27b-nvfp4-vllm" / "vllm-aeon-qwen38-dflash.service"
QWEN36_UNIT_NAME = "vllm-aeon-27b-dflash.service"
SGLANG_UNIT = ROOT / "profile" / "qwen3.8-27b-nvfp4-sglang" / "sglang-qwen38-27b.service"
IMAGE_REPOSITORY = "ghcr.io/aeon-7/aeon-vllm-ultimate"
IMAGE_DIGEST = "sha256:e62ac10d744ed7c8f3dd4d5631be0f7615870a88c327db9c1d382a27b36a61ee"
CONTAINER = "vllm-aeon-qwen38-dflash"
CIDFILE = "%t/gb10-memory-guardian/aeon-qwen38-text.cid"


def _runtime_argv(text: str) -> list[str]:
    command = _logical_argv(text, "ExecStart")
    if len(command) != 1:
        return []
    argv = command[0]
    return argv[argv.index("serve") + 1 :] if "serve" in argv else []


class AeonQwen38CanaryUnitContractTests(unittest.TestCase):
    def test_qwen38_unit_exists(self) -> None:
        self.assertTrue(UNIT.is_file(), f"missing canary unit {UNIT}")

    def test_unit_pins_the_live_aeon_omni_image_digest(self) -> None:
        argv = _logical_argv(UNIT.read_text(), "ExecStart")[0]
        pins = [
            token
            for token in argv
            if token.startswith(f"{IMAGE_REPOSITORY}@sha256:")
        ]
        self.assertEqual(pins, [f"{IMAGE_REPOSITORY}@{IMAGE_DIGEST}"])

    def test_unit_keeps_qwen_tool_parser_flags(self) -> None:
        argv = _runtime_argv(UNIT.read_text())
        self.assertEqual(argv[argv.index("--reasoning-parser") + 1], "qwen3")
        self.assertIn("--enable-auto-tool-choice", argv)
        self.assertEqual(argv[argv.index("--tool-call-parser") + 1], "qwen3_coder")

    def test_unit_keeps_no_swap_memory_envelope(self) -> None:
        text = UNIT.read_text()
        argv = _logical_argv(text, "ExecStart")[0]
        self.assertEqual(argv[argv.index("--memory") + 1], "70g")
        self.assertEqual(argv[argv.index("--memory-swap") + 1], "70g")
        self.assertEqual(argv[argv.index("--memory-swappiness") + 1], "0")
        self.assertIn("MemoryMax=70G", text)
        self.assertIn("MemorySwapMax=0", text)

    def test_unit_keeps_cgroup_and_lifecycle_contract(self) -> None:
        text = UNIT.read_text()
        argv = _logical_argv(text, "ExecStart")[0]
        self.assertEqual(argv[argv.index("--cgroup-parent") + 1], "app.slice")
        self.assertIn(f"--name {CONTAINER}", text)
        self.assertIn(f"--cidfile={CIDFILE}", text)
        self.assertIn("GB10_CGROUP_REGISTRATION_PATH=%t/gb10-memory-guardian/text-cgroup.v1", text)
        conflicts = next(line for line in text.splitlines() if line.startswith("Conflicts="))
        self.assertIn(SGLANG_UNIT.name, conflicts)
        self.assertIn(QWEN36_UNIT_NAME, conflicts)

    def test_unit_uses_dflash2_drafter_and_disables_prefix_cache(self) -> None:
        text = UNIT.read_text()
        argv = _logical_argv(text, "ExecStart")[0]
        runtime = _runtime_argv(text)
        spec = runtime[runtime.index("--speculative-config") + 1]
        self.assertEqual(
            json.loads(spec),
            {
                "method": "dflash",
                "model": "/drafter",
                "num_speculative_tokens": 7,
                "attention_backend": "TRITON_ATTN",
            },
        )
        self.assertIn("/home/obj/models/z-lab/Qwen3.8-27B-DFlash2:/drafter:ro", text)
        self.assertIn("--no-enable-prefix-caching", runtime)
        self.assertNotIn("--enable-prefix-caching", runtime)
        self.assertEqual(argv[argv.index("--attention-backend") + 1], "TRITON_ATTN")
        self.assertIn("--enable-chunked-prefill", runtime)
        self.assertEqual(runtime[runtime.index("--kv-cache-dtype") + 1], "fp8_e4m3")
        self.assertNotIn("--enforce-eager", runtime)
        compilation = runtime[runtime.index("--compilation-config") + 1]
        self.assertEqual(json.loads(compilation)["cudagraph_mode"], "FULL_AND_PIECEWISE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
