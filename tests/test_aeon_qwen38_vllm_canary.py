from __future__ import annotations

import unittest
from pathlib import Path

from test_vllm_no_swap_unit_contracts import _logical_argv


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "profile" / "qwen3.8-27b-nvfp4-vllm" / "vllm-aeon-qwen38-dflash.service"
QWEN36_UNIT_NAME = "vllm-aeon-27b-dflash.service"
SGLANG_UNIT = ROOT / "profile" / "qwen3.8-27b-nvfp4-sglang" / "sglang-qwen38-27b.service"
OVERLAY_REPOSITORY = "qwen38-27b-vllm-dflash2-sm121"
OVERLAY_DIGEST = "sha256:3bae63b93ff99690376eec892b5c52f6c8cd7cc462f70bb3bef83ef32114b53a"
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

    def test_unit_pins_the_live_overlay_image_digest(self) -> None:
        argv = _logical_argv(UNIT.read_text(), "ExecStart")[0]
        pins = [
            token
            for token in argv
            if token.startswith(f"{OVERLAY_REPOSITORY}@sha256:")
        ]
        self.assertEqual(pins, [f"{OVERLAY_REPOSITORY}@{OVERLAY_DIGEST}"])

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

    def test_unit_uses_in_checkpoint_mtp_and_prefix_cache(self) -> None:
        text = UNIT.read_text()
        argv = _logical_argv(text, "ExecStart")[0]
        runtime = _runtime_argv(text)
        spec = runtime[runtime.index("--speculative-config") + 1]
        self.assertIn("qwen3_5_mtp", spec)
        self.assertIn('"num_speculative_tokens":5', spec.replace(" ", ""))
        self.assertNotIn("dflash", spec)
        self.assertNotIn("/draft", text)
        self.assertIn("--enable-prefix-caching", runtime)
        self.assertNotIn("--no-enable-prefix-caching", runtime)
        self.assertEqual(argv[argv.index("--attention-backend") + 1], "TRITON_ATTN")
        self.assertIn("--enable-chunked-prefill", runtime)


if __name__ == "__main__":
    unittest.main(verbosity=2)
