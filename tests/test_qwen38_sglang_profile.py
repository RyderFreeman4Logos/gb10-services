"""Focused contract tests for the Qwen3.8 NVFP4 SGLang profile.

The profile is source-prepared only (not activated). These tests pin the
SGLang unit/Guard contract so a later authorized cutover cannot silently
drift. The live latest profile alias is AEON Ultimate, not this directory.

Run (no pipe):
    python3 -m unittest discover -s tests -p 'test_qwen38_sglang_profile.py' -v
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "profile" / "qwen3.8-27b-nvfp4-sglang"
UNIT = PROFILE_DIR / "sglang-qwen38-27b.service"
QUERIT_UNIT = ROOT / "profile" / "querit-4b-reranker" / "vllm-querit-4b-reranker.service"
GUARD = PROFILE_DIR / "llm-guard-proxy" / "config.toml"

# Exact immutable arm64 image digest built for the DFlash2 trial.
SGLANG_DIGEST = "lmsysorg/sglang@sha256:f6c809a2ebdeea97a3732e8bc139a32c18c9fb00a6e2fd770d257d9f72466ed3"
# Host source of the read-only DFlash2 draft mount (docker -v source).
DFLASH_DRAFT_PATH = "/home/obj/models/z-lab/Qwen3.8-27B-DFlash2"
# The stable public alias every caller uses; must survive model swaps.
SERVED_ALIAS = "abliterated-qwen-latest-27b-nvfp4"
# Flat hf --local-dir docker mount targets the server paths must resolve to
# (the snapshots/<sha> subdirs do NOT exist under a --local-dir install).
MODEL_MOUNT = "/models/qwen38-nvfp4"
DRAFT_MOUNT = "/models/qwen38-dflash2"
# Revision-locked hub SHA pins, documented in the unit comments (kept when the
# server path is the flat mount, not the hub snapshots/<sha> path).
NVFP4_SHA = "faf7945020c138c8ef864ab1644273f3158f85fa"
DFLASH_SHA = "50307d4c4cde6860d4eee73e2547cd786fe8e8a4"


class ModelStartupOrderingContractTests(unittest.TestCase):
    def test_model_units_wait_on_real_readiness_probes(self):
        querit = QUERIT_UNIT.read_text()
        sglang = UNIT.read_text()

        self.assertIn("After=network.target", querit)
        self.assertIn("After=vllm-embedding.service", querit)
        self.assertIn("After=network.target", sglang)
        self.assertIn("After=vllm-querit-4b-reranker.service", sglang)
        self.assertIn("After=vllm-embedding.service", sglang)
        self.assertRegex(
            sglang,
            r"(?m)^ExecStartPost=.*gb10_service_ready\.sh chat "
            r"http://100\.105\.4\.92:18010 "
            r"abliterated-qwen-latest-27b-nvfp4 --deadline \d+$",
        )
        for unit in (querit, sglang):
            self.assertNotRegex(
                unit,
                r"(?m)^(?:Requires|BindsTo|PartOf)=.*vllm-embedding\.service",
            )


class Qwen38ProfileLayoutTests(unittest.TestCase):
    def test_profile_directory_exists(self):
        self.assertTrue(PROFILE_DIR.is_dir(), f"missing profile dir {PROFILE_DIR}")

    def test_source_prepared_profile_is_not_live_latest_alias(self):
        self.assertFalse(
            (ROOT / "profile" / "abliterated-qwen-latest-27b-nvfp4").exists(),
            "live latest alias must not still point at the source-prepared SGLang profile",
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
        self.assertNotIn(
            "lmsysorg/sglang@sha256:3c0abdf41ef22de9d7a859dc16ed71eae69452e36c91f071a25e60c85a6d1fc6",
            self.text,
        )
        # tag-comment must be present and not the deleted spark image.
        self.assertIn("lmsysorg/sglang:qwen38-27b", self.text)
        self.assertNotIn("lmsysorg/sglang:spark", self.text)

    def test_unit_pins_nvfp4_revision_pinned_model_path(self):
        # Server path is the flat --local-dir mount target (no snapshots/<sha>).
        self.assertIn(f"--model-path {MODEL_MOUNT}", self.text)
        # Host bind must be the RadixArk NVFP4 weights (Blackfrost superseded).
        self.assertIn("/home/obj/models/RadixArk/Qwen3.8-27B-NVFP4", self.text)
        self.assertNotIn(
            "/home/obj/models/Blackfrost-AI/Qwen3.8-27B-ABLITERATED-NVFP4",
            self.text,
        )
        # Hub SHA pin kept as a documented comment, not a snapshots path.
        self.assertIn(NVFP4_SHA, self.text)

    def test_unit_pins_kv_cache_dtype_fp8(self):
        # Live `auto` allocated torch.bfloat16 / 41028 tokens and did NOT honor
        # NVFP4's FP8 calibration; fp8_e4m3 is pinned for the 262144 trial window.
        # Scope to the launch_server argv so the required historical comment
        # (which mentions auto/41028) does not trip the negative assertion.
        argv = self.text.split("--sampling-defaults model", 1)[0]
        self.assertIn("--kv-cache-dtype fp8_e4m3", argv)
        self.assertNotIn("--kv-cache-dtype auto", argv)

    def test_unit_omits_max_total_tokens_pool_cap(self):
        # --max-total-tokens is a pool CAP, not an OOM guard; it blocks leftover
        # envelope from becoming concurrent KV. Dropping it lets SGLang grow KV
        # into the leftover envelope within --mem-fraction-static 0.90.
        # Scope to launch_server argv so comments cannot false-positive.
        argv = self.text.split("--sampling-defaults model", 1)[0]
        self.assertNotIn("--max-total-tokens", argv)
        self.assertIn("--context-length 262144", argv)
        launch_argv = self.text.split("python3 -m sglang.launch_server", 1)[1]
        launch_argv = launch_argv.split("--sampling-defaults model", 1)[0]
        self.assertEqual(launch_argv.count("--context-length 262144"), 1)

    def test_unit_adds_dflash_speed_flags(self):
        # Keep the tuned compile/decode flags for the DFlash2 trial.
        for flag in (
            "--speculative-num-draft-tokens 8",
            "--enable-torch-compile",
            "--torch-compile-max-bs 4",
            "--cuda-graph-max-bs-decode 4",
            "--num-continuous-decode-steps 2",
        ):
            self.assertIn(flag, self.text)
        self.assertNotIn("--torch-compile-max-bs 16", self.text)
        self.assertNotIn("--cuda-graph-max-bs-decode 16", self.text)
        # Deprecated prefill CUDA-graph flag must NOT be used; only the decode
        # variant above. Prefill CUDA graphs remain disabled (see unit test).
        self.assertNotIn("--cuda-graph-max-bs \\", self.text)
        self.assertNotIn("--cuda-graph-max-bs \n", self.text)

    def test_unit_uses_dflash2_speculative_decoding(self):
        self.assertIn("--speculative-algorithm DFLASH", self.text)
        self.assertNotIn("--speculative-algorithm DSPARK", self.text)
        self.assertNotIn("--speculative-algorithm EAGLE", self.text)
        self.assertIn("--mamba-radix-cache-strategy extra_buffer", self.text)
        self.assertNotIn("--mamba-radix-cache-strategy extra_buffer_lazy", self.text)
        self.assertNotIn("--speculative-dspark-block-size", self.text)
        self.assertNotIn("--speculative-draft-model-quantization", self.text)
        # Draft server path is the flat --local-dir mount target.
        self.assertIn(f"--speculative-draft-model-path {DRAFT_MOUNT}", self.text)
        self.assertIn(DFLASH_DRAFT_PATH, self.text)
        # Hub SHA pin kept as a documented comment, not a snapshots path.
        self.assertIn(DFLASH_SHA, self.text)

    def test_unit_has_no_hub_snapshots_paths(self):
        # hf --local-dir never creates snapshots/<sha>; the server must use the
        # flat mount root, not a raw hub cache layout path.
        self.assertNotIn("/snapshots/", self.text)

    def test_unit_enables_sleep_on_idle(self):
        # The DFlash2 scheduler must use SGLang's idle-sleep path rather than
        # burning a CPU core in its default nonblocking receive loop.
        argv = self.text.split("python3 -m sglang.launch_server", 1)[1]
        self.assertIn("--sleep-on-idle", argv)

    def test_unit_pins_throughput_and_memory_contract(self):
        # mem-fraction 0.90 leaves the post-weight, post-mamba, post-graph
        # remainder available to KV without changing the model's max window.
        argv = self.text.split("python3 -m sglang.launch_server", 1)[1]
        self.assertIn("--context-length 262144", self.text)
        self.assertIn("--mem-fraction-static 0.90", argv)
        self.assertIn("SGLANG_MEM_FRACTION=0.90", self.text)
        self.assertNotIn("--mem-fraction-static 0.83", argv)
        self.assertNotIn("SGLANG_MEM_FRACTION=0.83", self.text)
        self.assertNotIn("--mem-fraction-static 0.72", argv)
        self.assertNotIn("SGLANG_MEM_FRACTION=0.72", self.text)
        self.assertNotIn("--mem-fraction-static 0.53", argv)
        self.assertNotIn("SGLANG_MEM_FRACTION=0.53", self.text)
        self.assertNotIn("--mem-fraction-static 0.95", argv)
        self.assertNotIn("SGLANG_MEM_FRACTION=0.95", self.text)
        self.assertIn("--mamba-ssm-dtype float32", argv)
        self.assertIn("--max-mamba-cache-size 80", argv)
        self.assertIn("--max-running-requests 16", argv)

    def test_unit_scales_dflash_mamba_capacity_with_admission(self):
        argv = self.text.split("python3 -m sglang.launch_server", 1)[1]
        running_match = re.search(r"--max-running-requests (\d+)", argv)
        mamba_match = re.search(r"--max-mamba-cache-size (\d+)", argv)
        if running_match is None or mamba_match is None:
            self.fail("SGLang admission flags are missing")
        max_running = int(running_match.group(1))
        max_mamba = int(mamba_match.group(1))
        self.assertEqual(max_running, 16)
        self.assertEqual(max_mamba, max_running * 5)

    def test_unit_keeps_quality_knobs_and_honest_leftover_contract(self):
        launch_argv = self.text.split("python3 -m sglang.launch_server", 1)[1]
        launch_argv = launch_argv.split("--sampling-defaults model", 1)[0]
        for flag in (
            "--kv-cache-dtype fp8_e4m3",
            "--mamba-ssm-dtype float32",
            "--max-mamba-cache-size 80",
            "--max-running-requests 16",
            "--torch-compile-max-bs 4",
            "--cuda-graph-max-bs-decode 4",
            "--speculative-draft-model-path /models/qwen38-dflash2",
            "--reasoning-parser qwen3",
        ):
            self.assertIn(flag, launch_argv)
        self.assertNotIn("--max-total-tokens", launch_argv)

        for phrase in (
            "5 GiB OS pad",
            "74g leftover envelope",
            "Leftover after weights+mamba+graphs goes to KV",
            "context window remains the model max 262144",
        ):
            self.assertIn(phrase, self.text)
        for stale_claim in (
            "10 GiB OS pad",
            "69g envelope",
            "69g leftover",
            "funds one 262144-token KV window",
            "restores KV>=262144",
        ):
            self.assertNotIn(stale_claim, self.text)
        self.assertNotRegex(self.text, r"74g.{0,160}(?:2.?3|two|three).{0,80}KV")

    def test_unit_pins_backend_and_chunked_prefill(self):
        self.assertIn("--attention-backend flashinfer", self.text)
        self.assertIn("--chunked-prefill-size 8192", self.text)
        argv = self.text.split("python3 -m sglang.launch_server", 1)[1]
        self.assertIn("--max-prefill-tokens 32768", argv)

    def test_unit_serves_public_stable_alias(self):
        # Image CLI accepts ONE SERVED_MODEL_NAME; only the stable public alias
        # is passed on this line (no second argv token like the old two-token form).
        self.assertIn(SERVED_ALIAS, self.text)
        self.assertIn(f'--served-model-name "{SERVED_ALIAS}" \\', self.text)
        self.assertNotIn(
            f'--served-model-name "{SERVED_ALIAS}" "qwen3.8-27b-sglang"',
            self.text,
        )

    def test_unit_cpuset_and_no_privileged(self):
        self.assertIn("--cpuset-cpus 5-8,15-18", self.text)
        self.assertNotIn("--cpuset-cpus 5-9,15-19", self.text)
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
        self.assertRegex(self.text, r"--memory\s+74g")
        self.assertRegex(self.text, r"--memory-swap\s+74g")
        self.assertNotIn("--memory 69g", self.text)
        self.assertNotIn("--memory-swap 69g", self.text)
        self.assertIn("MemoryMax=74G", self.text)
        self.assertNotIn("MemoryMax=69G", self.text)
        # systemd: no swap escapes the unit.
        self.assertIn("MemorySwapMax=0", self.text)


class Qwen38GuardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = GUARD.read_text()
        cls.config = tomllib.loads(cls.text)

    def test_dflash_chat_admission_is_32_in_flight_and_queued(self) -> None:
        default_chat = next(
            profile
            for profile in self.config["upstreams"]
            if profile["name"] == "qwen3.8-sglang-default-chat"
        )
        for admission in (self.config["server"], default_chat):
            self.assertEqual(admission["max_in_flight_requests"], 32)
            self.assertEqual(admission["max_queued_generation_requests"], 32)

    def test_default_chat_keeps_public_stable_alias(self):
        self.assertIn(SERVED_ALIAS, self.text)

    def test_default_chat_rewrites_legacy_aliases_to_served_model(self):
        default_chat = next(
            profile
            for profile in self.config["upstreams"]
            if profile["name"] == "qwen3.8-sglang-default-chat"
        )
        self.assertEqual(default_chat["upstream_model"], SERVED_ALIAS)
        self.assertEqual(
            default_chat["match_models"],
            [
                SERVED_ALIAS,
                "qwen3.8-27b-sglang",
                "qwen3.6-27b-decensor-by-aeon",
                "qwen3.6-27b-decensored",
                "aeon-ultimate",
            ],
        )

    def test_no_force_disable_on_default_chat(self):
        # Default chat must not force thinking off; faithful forward of caller.
        self.assertNotIn("mode = \"force_disable\"", self.text)
        self.assertNotIn("force_disable = true", self.text)

    def test_default_chat_listener_port_is_unique(self):
        # [server].port is the implicit default listener for 18009; a
        # [[listeners]] on the same port is rejected by live Guard ("listener
        # ports must be unique to avoid startup bind conflicts").
        self.assertRegex(
            self.text,
            r"(?m)^port\s*=\s*18009\s*$",
            "[server].port must keep the default chat entry on 18009",
        )
        # No explicit [[listeners]] block may bind 18009 or use the old
        # chat-default name.
        self.assertNotIn(
            'name = "chat-default"',
            self.text,
            "chat-default [[listeners]] must be dropped (implicit default owns 18009)",
        )
        # Every explicit [[listeners]] port listed except 18009.
        for line in self.text.splitlines():
            self.assertFalse(
                re.match(r"port\s*=\s*18009\s*$", line)
                and "[[listeners]]" in self.text[: self.text.index(line)],
                "no [[listeners]] block may bind 18009",
            )

    def test_param_override_fill_if_absent_defaults(self):
        # First [upstreams.param_override] is default chat; caller-wins fill.
        self.assertIn("[upstreams.param_override]", self.text)
        section_end = self.text.index(
            "[upstreams.loop_guard]", self.text.index("[upstreams.param_override]")
        )
        block = self.text[self.text.index("[upstreams.param_override]") : section_end]
        self.assertTrue(
            re.search(r"enabled\s*=\s*true", block), "param_override must be enabled"
        )
        self.assertRegex(block, r'mode\s*=\s*"fill_if_absent"')
        default_chat = next(
            profile
            for profile in self.config["upstreams"]
            if profile["name"] == "qwen3.8-sglang-default-chat"
        )
        override = default_chat["param_override"]
        self.assertTrue(override["enabled"])
        self.assertEqual(override["mode"], "fill_if_absent")
        self.assertEqual(override["temperature"], 1.0)
        self.assertEqual(override["top_p"], 0.95)
        self.assertEqual(override["top_k"], 20)
        self.assertEqual(override["min_p"], 0.0)
        self.assertEqual(override["presence_penalty"], 0.0)
        self.assertEqual(override["repetition_penalty"], 1.0)
        self.assertEqual(override["max_tokens"], 50000)
        self.assertEqual(override["reasoning_effort"], "medium")
        self.assertNotIn("thinking_budget", override)
        self.assertNotIn("thinking_token_budget", block)
        self.assertNotIn("force_disable", block)
        self.assertNotIn("force_thinking", block)

    def test_retry_ladder_must_not_rewrite_sampling_or_thinking(self):
        # No retry ladder that force_* thinking or rewrites sampling.
        self.assertNotIn("thinking_mode = \"force_disable\"", self.text)
        self.assertNotIn("thinking_mode = \"force_thinking\"", self.text)
        self.assertNotIn("temperature = 0.6", self.text)

    def test_retry_disables_shielded_streaming(self):
        self.assertIn("shielded_streaming_enabled = false", self.text)
        self.assertNotIn("shielded_streaming_enabled = true", self.text)

    def test_live_guard_rejects_unknown_upstreams_retry_table(self):
        # Live Guard 8adcce30 fails on table [upstreams.retry]; keep ladder + [retry].
        self.assertIsNone(
            re.search(r"(?m)^\[upstreams\.retry\]\s*$", self.text),
            "unknown [upstreams.retry] table is not parseable by live Guard",
        )
        self.assertIn("[[upstreams.retry.ladder]]", self.text)
        self.assertIn("[retry]", self.text)

    def test_default_chat_local_recovery_is_disarmed(self):
        # AEON helper Conflicts-kills SGLang if either recovery block stays armed.
        for header in ("[upstream.local_recovery]", "[upstreams.local_recovery]"):
            start = self.text.index(header)
            nxt = self.text.find("\n[", start + len(header))
            block = self.text[start:] if nxt < 0 else self.text[start:nxt]
            self.assertTrue(
                re.search(r"(?m)^enabled\s*=\s*false\s*$", block),
                f"{header} must be disabled",
            )

    def test_paired_comparison_variants_are_live_guard_parseable(self):
        # Live Guard 8adcce30 rejects any non-allowlisted variants entry.
        # Scope to the paired_comparison section: the retry.ladder name
        # intentionally keeps the "same-policy" token (a section-local name,
        # not a paired_comparison variant).
        head = self.text.index("[evidence.shadow.paired_comparison]")
        nxt = self.text.find("\n[", head + 1)
        block = self.text[head:] if nxt < 0 else self.text[head:nxt]
        variants_line = next(
            line for line in block.splitlines() if line.startswith("variants")
        )
        self.assertNotIn(
            "same-policy",
            variants_line,
            'paired_comparison variants must not contain "same-policy" '
            "(live Guard enum rejects it)",
        )
        self.assertIn(
            'variants = ["no-thinking"]',
            block,
            "variants must pin the live-Guard-parseable token no-thinking",
        )

    def test_paired_comparison_is_disabled(self):
        # Faithful-forward profile must not spawn extra GPU thinking shadows.
        head = self.text.index("[evidence.shadow.paired_comparison]")
        nxt = self.text.find("\n[", head + 1)
        block = self.text[head:] if nxt < 0 else self.text[head:nxt]
        self.assertTrue(
            re.search(r"(?m)^enabled\s*=\s*false\s*$", block),
            "[evidence.shadow.paired_comparison] must be disabled",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
