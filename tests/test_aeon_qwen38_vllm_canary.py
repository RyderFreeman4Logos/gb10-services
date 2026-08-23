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
IMAGE_REPOSITORY = "ghcr.io/r0b0tlab/qwen38-27b-nvfp4-sm121"
IMAGE_TAG = "v0.27.2rc0-sm121"
IMAGE_DIGEST = "sha256:5bd3f329c531da4cd5f41f2c32d4ed9527b2131b88032b737364fb582e6b775f"
OVERLAY_REPOSITORY = "qwen38-27b-vllm-dflash2-sm121"
OVERLAY_DIGEST = "sha256:3bae63b93ff99690376eec892b5c52f6c8cd7cc462f70bb3bef83ef32114b53a"
ALIAS = "abliterated-qwen-latest-27b-nvfp4"
NVFP4_HOST = "/home/obj/models/r0b0tlab/Qwen3.8-27B-NVFP4-MTP-sm121"
NVFP4_MOUNT = "/model"
DRAFT_HOST = "/home/obj/models/z-lab/Qwen3.8-27B-DFlash2"
DRAFT_MOUNT = "/draft"
QWEN38_PROFILE = "/home/obj/.config/gb10/aeon-dflash-profiles/qwen38.env"
QWEN38_PROFILE_SOURCE = ROOT / "config" / "aeon-dflash-profiles" / "qwen38.env"
CIDFILE = "%t/gb10-memory-guardian/aeon-qwen38-text.cid"
CONTAINER = "vllm-aeon-qwen38-dflash"
NVFP4_REV = "36f717a22990e82c54c1d48ee77c491b87825680"
DRAFT_REV = "50307d4c4cde6860d4eee73e2547cd786fe8e8a4"


def _unit_text() -> str:
    return UNIT.read_text() if UNIT.is_file() else ""


def _runtime_argv(text: str) -> list[str]:
    command = _logical_argv(text, "ExecStart")
    if len(command) != 1:
        return []
    argv = command[0]
    if "serve" in argv:
        return argv[argv.index("serve") + 1 :]
    for index, token in enumerate(argv):
        if "@sha256:" in token:
            return argv[index + 1 :]
    return []


def _option_value(argv: list[str], option: str) -> str:
    index = argv.index(option)
    return argv[index + 1]


class AeonQwen38CanaryUnitContractTests(unittest.TestCase):
    def test_qwen38_unit_exists(self) -> None:
        self.assertTrue(UNIT.is_file(), f"missing canary unit {UNIT}")

    def test_qwen38_unit_uses_its_own_053_profile(self) -> None:
        text = _unit_text()
        self.assertTrue(QWEN38_PROFILE_SOURCE.is_file())
        self.assertEqual(
            QWEN38_PROFILE_SOURCE.read_text(),
            "AEON_GPU_MEMORY_UTILIZATION=0.53\n",
        )
        self.assertIn(f"EnvironmentFile={QWEN38_PROFILE}", text)
        self.assertNotIn(
            "EnvironmentFile=/home/obj/.config/gb10/aeon-dflash-profiles/active.env",
            text,
        )
        argv = _runtime_argv(text)
        self.assertEqual(
            _option_value(argv, "--gpu-memory-utilization"),
            "${AEON_GPU_MEMORY_UTILIZATION}",
        )

    def test_unit_mounts_r0b0tlab_nvfp4_and_zlab_dflash2(self) -> None:
        text = _unit_text()
        self.assertIn(f"{NVFP4_HOST}:{NVFP4_MOUNT}:ro", text)
        self.assertIn(f"{DRAFT_HOST}:{DRAFT_MOUNT}:ro", text)
        self.assertIn(NVFP4_REV, text)
        self.assertIn(DRAFT_REV, text)
        self.assertNotIn("orcarouter/Qwen3.8-27B-Uncensored-NVFP4", text)
        self.assertNotIn("kstoyanov99/Qwen3.8-27B-Dflash", text)
        self.assertNotIn("/models/qwen38-dflash", text)
        self.assertNotIn("/snapshots/", text)
        argv = _runtime_argv(text)
        self.assertEqual(_option_value(argv, "--model"), NVFP4_MOUNT)
        speculative = json.loads(_option_value(argv, "--speculative-config"))
        self.assertEqual(
            speculative,
            {
                "method": "dflash",
                "model": DRAFT_MOUNT,
                "num_speculative_tokens": 8,
            },
        )

    def test_unit_pins_r0b0tlab_dflash2_overlay_and_quality_envelope(self) -> None:
        text = _unit_text()
        argv = _runtime_argv(text)
        exec_start = _logical_argv(text, "ExecStart")
        self.assertEqual(len(exec_start), 1)
        start = " ".join(exec_start[0])
        self.assertIn(f"image tag: {IMAGE_TAG} (repository {IMAGE_REPOSITORY})", text)
        self.assertIn(f"resolved immutable digest: {IMAGE_DIGEST}", text)
        self.assertNotIn(f"{IMAGE_REPOSITORY}:{IMAGE_TAG}", start)
        self.assertNotIn("ghcr.io/aeon-7/aeon-vllm-ultimate", text)
        self.assertNotIn("/opt/hang_guard/aeon_vllm_wrapper.py", text)
        self.assertEqual(
            exec_start[0][exec_start[0].index("--entrypoint") + 1],
            "python3",
        )
        self.assertIn("/usr/local/bin/vllm", exec_start[0])
        self.assertEqual(
            exec_start[0][exec_start[0].index("/usr/local/bin/vllm") + 1],
            "serve",
        )
        overlay_pins = [
            token
            for token in exec_start[0]
            if token.startswith(f"{OVERLAY_REPOSITORY}@sha256:")
        ]
        self.assertEqual(len(overlay_pins), 1)
        self.assertRegex(
            overlay_pins[0],
            rf"^{re.escape(OVERLAY_REPOSITORY)}@sha256:[0-9a-f]{{64}}$",
        )
        self.assertEqual(overlay_pins[0], f"{OVERLAY_REPOSITORY}@{OVERLAY_DIGEST}")
        self.assertNotEqual(
            overlay_pins[0],
            f"{OVERLAY_REPOSITORY}@sha256:{'0' * 64}",
        )
        self.assertIn(OVERLAY_DIGEST, text)
        self.assertIn(IMAGE_DIGEST, text)
        self.assertEqual(_option_value(argv, "--max-model-len"), "262144")
        self.assertEqual(_option_value(argv, "--kv-cache-dtype"), "fp8")
        self.assertEqual(_option_value(argv, "--mamba-cache-dtype"), "float32")
        self.assertEqual(_option_value(argv, "--mamba-ssm-cache-dtype"), "float32")
        self.assertEqual(
            _option_value(argv, "--gpu-memory-utilization"),
            "${AEON_GPU_MEMORY_UTILIZATION}",
        )
        self.assertEqual(_option_value(argv, "--tool-call-parser"), "qwen3_xml")
        self.assertIn("--enable-auto-tool-choice", argv)
        self.assertIn("--enforce-eager", argv)
        self.assertIn("--no-enable-flashinfer-autotune", argv)
        self.assertIn("--kernel-config.enable_jit_warmup=false", argv)
        self.assertIn("--kernel-config.enable_cutedsl_warmup=false", argv)
        self.assertIn("--trust-remote-code", argv)
        self.assertNotIn("--kv-cache-memory-bytes", text)
        self.assertNotIn("--max-total-tokens", text)
        self.assertIn("-e VLLM_USE_V2_MODEL_RUNNER=1", text)
        self.assertNotIn("-e VLLM_USE_V2_MODEL_RUNNER=0", text)
        self.assertIn("--no-enable-prefix-caching", argv)
        self.assertNotIn("--enable-prefix-caching", argv)
        self.assertIn("--memory 70g", text)
        self.assertIn("--memory-swap 70g", text)
        self.assertIn("--memory-swappiness 0", text)
        self.assertIn("MemoryMax=70G", text)
        self.assertIn("MemorySwapMax=0", text)
        self.assertNotIn("MemoryMax=74G", text)
        self.assertNotIn("--memory 74g", text)

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
        self.assertIn(f"EnvironmentFile={QWEN38_PROFILE}", text)

    def test_unit_documents_dflash2_canary_and_keeps_capacity_claim_honest(self) -> None:
        text = _unit_text()
        self.assertIn("r0b0tlab DFlash2 K=8", text)
        self.assertIn("KV≥262144 is a live receipt, not a source claim", text)
        self.assertNotIn("MTP K=3", text)
        self.assertNotIn("DFlash n=10", text)
        self.assertNotIn("canary of the AEON engine + incumbent Qwen3.8 weights", text)
        self.assertIn("SGLang", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
