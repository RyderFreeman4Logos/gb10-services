"""Focused contract tests for the Qwen3.8 NVFP4 SGLang profile.

The profile is source-prepared only (not activated). These tests pin the
contract the profile must satisfy so a model swap behind the stable
`abliterated-qwen-latest-27b-nvfp4` alias cannot silently drift.

Run (no pipe):
    python3 -m unittest discover -s tests -p 'test_qwen38_sglang_profile.py' -v
"""

import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "profile" / "qwen3.8-27b-nvfp4-sglang"
ALIAS = ROOT / "profile" / "abliterated-qwen-latest-27b-nvfp4"
UNIT = PROFILE_DIR / "sglang-qwen38-27b.service"
GUARD = PROFILE_DIR / "llm-guard-proxy" / "config.toml"

# Exact immutable arm64 image digest inspected for this box (brief / cookbook).
SGLANG_DIGEST = "lmsysorg/sglang@sha256:3c0abdf41ef22de9d7a859dc16ed71eae69452e36c91f071a25e60c85a6d1fc6"
# Host source of the read-only draft mount (docker -v source, always stays).
DSPARK_DRAFT_PATH = "/home/obj/models/RadixArk/Qwen3.8-27B-DSpark"
# The stable public alias every caller uses; must survive model swaps.
SERVED_ALIAS = "abliterated-qwen-latest-27b-nvfp4"
# Flat hf --local-dir docker mount targets the server paths must resolve to
# (the snapshots/<sha> subdirs do NOT exist under a --local-dir install).
MODEL_MOUNT = "/models/qwen38-nvfp4"
DRAFT_MOUNT = "/models/qwen38-dspark"
# Revision-locked hub SHA pins, documented in the unit comments (kept when the
# server path is the flat mount, not the hub snapshots/<sha> path).
NVFP4_SHA = "554ebba9b5f1b79dc11246341960360e6ef05ef4"
DSPARK_SHA = "85ef153be924f17ce4bf62726954eeaa4a73e854"


class Qwen38ProfileLayoutTests(unittest.TestCase):
    def test_profile_directory_exists(self):
        self.assertTrue(PROFILE_DIR.is_dir(), f"missing profile dir {PROFILE_DIR}")

    def test_stable_alias_retargeted_to_new_profile(self):
        self.assertTrue(ALIAS.is_symlink(), "stable alias must be a symlink")
        self.assertEqual(
            os.readlink(ALIAS),
            "qwen3.8-27b-nvfp4-sglang",
            "stable alias must resolve to the new Qwen3.8 SGLang profile",
        )

    def test_unit_and_guard_config_exist(self):
        self.assertTrue(UNIT.is_file(), f"missing unit {UNIT}")
        self.assertTrue(GUARD.is_file(), f"missing guard config {GUARD}")


class Qwen38UnitContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = UNIT.read_text()
        cls.conflicts = cls._grab_section(cls.text, "Conflicts=")

    @staticmethod
    def _grab_section(text, key):
        for line in text.splitlines():
            if line.startswith(key):
                return line[len(key):].strip()
        return None

    def test_unit_pins_exact_sglang_digest(self):
        self.assertIn(SGLANG_DIGEST, self.text)
        # tag-comment must be present and not the deleted spark image.
        self.assertIn("lmsysorg/sglang:qwen38-27b", self.text)
        self.assertNotIn("lmsysorg/sglang:spark", self.text)

    def test_unit_pins_nvfp4_revision_pinned_model_path(self):
        # Server path is the flat --local-dir mount target (no snapshots/<sha>).
        self.assertIn(f"--model-path {MODEL_MOUNT}", self.text)
        self.assertIn("/home/obj/models/RadixArk/Qwen3.8-27B-NVFP4", self.text)
        # Hub SHA pin kept as a documented comment, not a snapshots path.
        self.assertIn(NVFP4_SHA, self.text)

    def test_unit_adds_dspark_speed_flags(self):
        # MiaAI 2026-08-18 start-dspark.sh speed flags (code-decode ~51 tok/s).
        for flag in (
            "--speculative-num-draft-tokens 8",
            "--enable-torch-compile",
            "--torch-compile-max-bs 4",
            "--cuda-graph-max-bs-decode 4",
            "--num-continuous-decode-steps 2",
        ):
            self.assertIn(flag, self.text)
        # Deprecated prefill CUDA-graph flag must NOT be used; only the decode
        # variant above. Prefill CUDA graphs remain disabled (see unit test).
        self.assertNotIn("--cuda-graph-max-bs \\", self.text)
        self.assertNotIn("--cuda-graph-max-bs \n", self.text)

    def test_unit_uses_dspark_speculative_decoding(self):
        self.assertIn("--speculative-algorithm DSPARK", self.text)
        self.assertNotIn("--speculative-algorithm EAGLE", self.text)
        self.assertIn("--speculative-dspark-block-size 7", self.text)
        self.assertIn("--speculative-draft-model-quantization unquant", self.text)
        # Draft server path is the flat --local-dir mount target.
        self.assertIn(f"--speculative-draft-model-path {DRAFT_MOUNT}", self.text)
        self.assertIn(DSPARK_DRAFT_PATH, self.text)
        # Hub SHA pin kept as a documented comment, not a snapshots path.
        self.assertIn(DSPARK_SHA, self.text)

    def test_unit_has_no_hub_snapshots_paths(self):
        # hf --local-dir never creates snapshots/<sha>; the server must use the
        # flat mount root, not a raw hub cache layout path.
        self.assertNotIn("/snapshots/", self.text)

    def test_unit_pins_kv_cache_dtype_auto(self):
        # NVFP4 declares FP8 kv calibration; auto honors fp8_e4m3 scales.
        self.assertIn("--kv-cache-dtype auto", self.text)
        self.assertNotIn("--kv-cache-dtype fp8_e4m3", self.text)

    def test_unit_pins_throughput_and_memory_contract(self):
        self.assertIn("--context-length 262144", self.text)
        self.assertIn("--mem-fraction-static 0.53", self.text)
        self.assertIn("--mamba-ssm-dtype float32", self.text)
        self.assertIn("--max-mamba-cache-size 32", self.text)
        self.assertIn("--max-running-requests 8", self.text)

    def test_unit_pins_backend_and_chunked_prefill(self):
        self.assertIn("--attention-backend flashinfer", self.text)
        self.assertIn("--chunked-prefill-size 8192", self.text)

    def test_unit_serves_public_stable_alias(self):
        self.assertIn(SERVED_ALIAS, self.text)

    def test_unit_cpuset_and_no_privileged(self):
        self.assertIn("--cpuset-cpus 5-9,15-19", self.text)
        self.assertNotIn("--privileged", self.text)

    def test_unit_conflicts_with_aeon_text_units(self):
        self.assertIsNotNone(self.conflicts)
        conflicts = self.conflicts or ""
        for name in (
            "vllm-aeon-27b.service",
            "vllm-aeon-27b-dflash.service",
            "vllm-aeon-27b-dflash-hikv.service",
        ):
            self.assertIn(name, conflicts)

    def test_unit_swap_is_impossible(self):
        # Docker: memory == memory-swap (zero extra swap) + swappiness 0.
        self.assertIn("--memory-swappiness 0", self.text)
        self.assertRegex(self.text, r"--memory\s+70g")
        self.assertRegex(self.text, r"--memory-swap\s+70g")
        # systemd: no swap escapes the unit.
        self.assertIn("MemorySwapMax=0", self.text)


class Qwen38GuardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = GUARD.read_text()

    def test_default_chat_keeps_public_stable_alias(self):
        self.assertIn(SERVED_ALIAS, self.text)

    def test_no_force_disable_on_default_chat(self):
        # Default chat must not force thinking off; faithful forward of caller.
        self.assertNotIn("mode = \"force_disable\"", self.text)
        self.assertNotIn("force_disable = true", self.text)

    def test_param_override_disabled(self):
        # TOML table form: [upstreams.param_override] with enabled = false.
        self.assertIn("[upstreams.param_override]", self.text)
        section_end = self.text.index("[upstreams.loop_guard]", self.text.index("[upstreams.param_override]"))
        block = self.text[self.text.index("[upstreams.param_override]"):section_end]
        self.assertTrue(re.search(r"enabled\s*=\s*false", block), "param_override must be disabled")
        self.assertNotIn("temperature", block)

    def test_retry_ladder_must_not_rewrite_sampling_or_thinking(self):
        # No retry ladder that force_* thinking or rewrites sampling.
        self.assertNotIn("thinking_mode = \"force_disable\"", self.text)
        self.assertNotIn("thinking_mode = \"force_thinking\"", self.text)
        self.assertNotIn("temperature = 0.6", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
