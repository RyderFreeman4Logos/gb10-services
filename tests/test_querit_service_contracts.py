from __future__ import annotations

import json
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any

from test_embedding_service_contracts import (
    _logical_directive_argv,
    _option_values,
    _split_docker_run_argv,
)


ROOT = Path(__file__).resolve().parents[1]
QUERIT_UNIT = ROOT / "systemd" / "querit-4b-reranker.service"
AEON_UNIT = ROOT / "systemd" / "vllm-aeon-27b-dflash.service"
GUARD_UNIT = ROOT / "systemd" / "llm-guard-proxy.service"
LEGACY_UNIT = ROOT / "systemd" / "vllm-qwen3-reranker-8b.service"
CGROUP_HELPER = ROOT / "scripts" / "gb10_enforce_docker_cgroup_limits.sh"
MEMORY_GATE = ROOT / "scripts" / "gb10_check_mem_available.sh"
CONFIG = ROOT / "config" / "llm-guard-proxy" / "config.toml"
README = ROOT / "README.md"

LIVE_RECEIPT = ROOT / "docs" / "evidence" / "2026-07-14-aeon-15g-live-receipt.json"


def _live_receipt() -> dict[str, Any]:
    return json.loads(LIVE_RECEIPT.read_text())


_RECEIPT = _live_receipt()
# Production AEON text image digest (v0.25.0, 2026-07-14). Live receipt still records the
# 15 GiB KV capacity proof from the prior v0.24 run; KV budget stays 15360 MiB.
IMAGE_DIGEST = (
    "sha256:18c09e6b80141a530285160781f7fa720a78ef91143b3c15a65a8c9641b44e55"
)
# Querit still pins the prior offline transformers runtime image until a separate upgrade.
QUERIT_IMAGE_DIGEST = (
    "sha256:f6d453d0b4a7ef90eefee486f4ff769cc2e1bb1e206df16d70370da09c02203c"
)
MODEL_SNAPSHOT = "7b796de30ad8dc772d6c46c75659c1341283a665"
SHORT_GENERATION_REQUEST_TOKENS = 8_192
# AEON DFlash GB10 ceiling (author guidance + freeze mitigation).
AEON_MAX_NUM_SEQS = 16
AEON_CONTEXT_TOKENS = _RECEIPT["aeon_container"]["effective_contract"]["max_model_len"]
AEON_KV_BUDGET_MIB = _RECEIPT["aeon_container"]["effective_contract"]["kv_cache_memory_mib"]
MAX_AEON_KV_MIB_WITH_CURRENT_HEADROOM_EVIDENCE = AEON_KV_BUDGET_MIB
VERIFIED_15_GIB_KV_CAPACITY_TOKENS = _RECEIPT["startup"]["kv_capacity_tokens"]
OBSERVED_FRESH_MEM_AVAILABLE_KIB = _RECEIPT["planning_inputs"]["pre_activation_mem_available_kib"]
OBSERVED_TEXT_GROWTH_MIB = _RECEIPT["planning_inputs"]["previously_observed_text_growth_mib"]


def _aeon_contract(unit: str) -> dict[str, int | float]:
    exec_starts = _logical_directive_argv(unit, "ExecStart")
    if len(exec_starts) != 1:
        raise AssertionError(
            f"expected exactly one ExecStart, found {len(exec_starts)}"
        )
    argv = exec_starts[0]
    image = f"ghcr.io/aeon-7/aeon-vllm-ultimate@{IMAGE_DIGEST}"
    host_argv, container_argv = _split_docker_run_argv(argv, image)
    # Docker --memory does not police UMA; unit must not hard-cap the container that way.
    for banned in ("--memory", "--memory-swap"):
        if any(tok == banned or tok.startswith(f"{banned}=") for tok in host_argv):
            raise AssertionError(
                f"text unit must not set docker {banned} (UMA is unpoliced)"
            )
    for flag, expected in {
        "--memory-swappiness": "0",
        "--oom-score-adj": "800",
    }.items():
        actual = _option_values(host_argv, flag)[0]
        if actual != expected:
            raise AssertionError(f"{flag} must be {expected}, found {actual}")

    model_len = _option_values(container_argv, "--max-model-len")[0]
    if not model_len.isdecimal():
        raise AssertionError(f"invalid --max-model-len value: {model_len}")
    # AEON guidance: do NOT pin --kv-cache-memory-bytes (bypasses UMA guard).
    kv_flag_present = True
    try:
        _option_values(container_argv, "--kv-cache-memory-bytes")
    except AssertionError as e:
        if "expected exactly one" in str(e):
            kv_flag_present = False
        else:
            raise
    if kv_flag_present:
        raise AssertionError(
            "text unit must not pin --kv-cache-memory-bytes (bypasses UMA guard)"
        )
    util = float(_option_values(container_argv, "--gpu-memory-utilization")[0])
    return {
        "model_len": int(model_len),
        "util": util,
    }


def _assert_aeon_headroom_evidence(contract: dict[str, int | float]) -> None:
    if contract["util"] > 0.65:
        raise AssertionError(
            "gpu-memory-utilization above 0.65 requires updated UMA headroom evidence"
        )


class AeonLiveReceiptTests(unittest.TestCase):
    def test_receipt_is_structured_privacy_safe_and_source_grounded(self) -> None:
        receipt = _live_receipt()
        self.assertEqual(receipt["schema"], "gb10-live-evidence-v1")
        serialized = json.dumps(receipt, sort_keys=True)
        self.assertNotRegex(serialized, r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
        self.assertNotRegex(
            serialized.lower(),
            r"password|authorization|bearer|prompt|response_body",
        )
        self.assertEqual(receipt["privacy"]["payload_content_retained"], False)

        startup = receipt["startup"]
        match = re.fullmatch(
            r"GPU KV cache size: ([0-9,]+) tokens",
            startup["capacity_source_line"],
        )
        if match is None:
            self.fail("receipt startup capacity source line is malformed")
        self.assertEqual(
            int(match.group(1).replace(",", "")),
            startup["kv_capacity_tokens"],
        )
        self.assertEqual(startup["kv_capacity_tokens"], 269_589)
        for service in receipt["services"].values():
            self.assertRegex(service["invocation_pid_sha256"], r"^[0-9a-f]{64}$")

    def test_watchdog_summary_and_documented_arithmetic_derive_from_receipt(self) -> None:
        receipt = _live_receipt()
        run = receipt["watchdog"]["completed_runs"][0]
        self.assertEqual(
            run["stable_prefix_sha256"],
            "08930190cc6135d71ea0a5ee3794fa15b1818d79ff06e73f391ab6ef47732c53",
        )
        self.assertEqual(run["observed_samples"], 109)
        self.assertEqual(run["declared_samples"], 110)
        self.assertEqual(run["sample_discrepancy"], 1)
        self.assertEqual(run["threshold_events"], 0)
        self.assertEqual(run["end_cgroup_events"]["text"]["oom_kill"], 0)

        startup = receipt["startup"]
        context = receipt["aeon_container"]["effective_contract"]["max_model_len"]
        ratio = startup["kv_capacity_tokens"] / context
        margin_percent = (startup["kv_capacity_tokens"] - context) * 100 / context
        self.assertAlmostEqual(ratio, 1.0284004211425781)
        self.assertAlmostEqual(margin_percent, 2.8400421142578125)
        end_gib = run["end_mem_available_bytes"] / 1024**3
        text_gib = run["end_text_nvml_allocation_bytes"] / 1024**3
        self.assertIn(f"~{end_gib:.1f}GiB MemAvailable", AEON_UNIT.read_text())
        note = (ROOT / receipt["documentation"]["research_note_path"]).read_text()
        self.assertIn(f"**{end_gib:.3f} GiB**", note)
        self.assertIn(f"**{text_gib:.3f} GiB** NVML", note)


class HostileAeonUnitMutationTests(unittest.TestCase):
    def assert_contract_rejects(self, unit: str) -> None:
        with self.assertRaises(AssertionError):
            _aeon_contract(unit)

    def test_rejects_docker_memory_option_on_host_argv(self) -> None:
        unit = AEON_UNIT.read_text()
        # Inject a banned docker hard-cap before the image boundary (host argv).
        unit = unit.replace(
            "  --memory-swappiness 0 \\\n",
            "  --memory 69g \\\n  --memory-swappiness 0 \\\n",
            1,
        )
        self.assert_contract_rejects(unit)

    def test_rejects_docker_memory_swap_option_on_host_argv(self) -> None:
        unit = AEON_UNIT.read_text()
        unit = unit.replace(
            "  --memory-swappiness 0 \\\n",
            "  --memory-swap 69g \\\n  --memory-swappiness 0 \\\n",
            1,
        )
        self.assert_contract_rejects(unit)


class QueritServiceContractTests(unittest.TestCase):
    def test_unit_uses_pinned_offline_artifacts(self) -> None:
        unit = QUERIT_UNIT.read_text()
        self.assertIn(f"@{QUERIT_IMAGE_DIGEST}", unit)
        self.assertIn(
            "models--Querit--Querit-4B:/models/querit-repo:ro", unit
        )
        self.assertIn(f"--model /models/querit-repo/snapshots/{MODEL_SNAPSHOT}", unit)
        self.assertIn(
            "querit_score_contract.py:/opt/querit/querit_score_contract.py:ro", unit
        )
        self.assertNotIn(
            "--score-contract current-prompt-terminal-cls-v1", unit
        )
        self.assertIn("--entrypoint python3", unit)
        self.assertNotIn("pip install", unit)
        self.assertNotIn("--dns", unit)
        self.assertNotRegex(unit, r"aeon-vllm-ultimate:[^\s\\]+(?:\s|\\)")

    def test_unit_follows_text_lifecycle_for_uma_safe_restart(self) -> None:
        unit = QUERIT_UNIT.read_text()
        unit_section = unit.split("[Service]", 1)[0]
        # Reranker is independent of text lifecycle so it doesn't stop during
        # text restart (user cannot accept rr downtime).
        self.assertNotIn("Requires=vllm-aeon-27b-dflash.service", unit_section)
        self.assertNotIn("After=vllm-aeon-27b-dflash.service", unit_section)
        self.assertIn("Conflicts=vllm-reranker.service", unit)
        self.assertNotIn("http://100.105.4.92:18010", unit)
        self.assertIn("gb10_check_mem_available.sh 2", unit)
        self.assertIn("--memory 18g", unit)
        self.assertIn("--memory-swap 18g", unit)
        ready = unit.index("http://100.105.4.92:18013/v1/models")
        timeout = re.search(r"^TimeoutStartSec=(\d+)$", unit, re.MULTILINE)
        if timeout is None:
            self.fail("TimeoutStartSec missing")
        readiness_loops = re.findall(
            r"seq 1 (\d+).*?--max-time (\d+).*?sleep (\d+); done",
            unit,
        )
        self.assertEqual(len(readiness_loops), 1)
        readiness_budget = sum(
            int(attempts) * (int(curl_timeout) + int(sleep_seconds))
            for attempts, curl_timeout, sleep_seconds in readiness_loops
        )
        helper = CGROUP_HELPER.read_text()
        helper_wait = re.search(r"GB10_CGROUP_WAIT_SECONDS:-([0-9]+)", helper)
        docker_timeout = re.search(r"GB10_DOCKER_TIMEOUT_SECONDS:-([0-9]+)", helper)
        systemctl_timeout = re.search(
            r"GB10_SYSTEMCTL_TIMEOUT_SECONDS:-([0-9]+)", helper
        )
        if helper_wait is None or docker_timeout is None or systemctl_timeout is None:
            self.fail("cgroup helper timeout defaults missing")
        worst_case_seconds = (
            readiness_budget
            + int(helper_wait.group(1))
            + int(docker_timeout.group(1))
            + int(systemctl_timeout.group(1))
        )
        self.assertGreaterEqual(int(timeout.group(1)), worst_case_seconds + 60)

    def test_guard_orders_after_backends_without_owning_lifecycle(self) -> None:
        unit = GUARD_UNIT.read_text()
        unit_section = unit.split("[Service]", 1)[0]
        self.assertIn("querit-4b-reranker.service", unit_section)
        self.assertNotIn("vllm-qwen3-reranker-8b.service", unit_section)
        self.assertNotRegex(unit_section, r"(?m)^Wants=.*vllm-")
        self.assertIn("Ordering only", unit_section)
        for setting in (
            "MemoryHigh=1792M",
            "MemoryMax=2G",
            "MemorySwapMax=0",
            "OOMScoreAdjust=-200",
        ):
            self.assertIn(setting, unit)
        self.assertIn("CacheDirectory=llm-guard-proxy-evidence", unit)
        self.assertIn("CacheDirectoryMode=0700", unit)

    def test_aeon_unit_matches_source_memory_profile(self) -> None:
        unit = AEON_UNIT.read_text()
        for contract in (
            "gb10_enforce_docker_cgroup_limits.sh --publish-registration "
            "vllm-aeon-27b-dflash-n12 69",
            "GB10_CGROUP_REGISTRATION_PATH=%t/gb10-memory-guardian/text-cgroup.v1",
            f"@{IMAGE_DIGEST}",
            "--oom-score-adj 800",
            "OOMScoreAdjust=800",
            "--max-num-seqs 16",
            "--max-num-batched-tokens 4096",
            "--gpu-memory-utilization 0.60",
            "FULL_DECODE_ONLY",
        ):
            self.assertIn(contract, unit)
        self.assertNotIn("--memory 69g", unit)
        self.assertNotIn("--memory-swap 69g", unit)
        self.assertNotIn("--dns", unit)
        self.assertNotRegex(unit, r"aeon-vllm-ultimate:[^\s\\]+(?:\s|\\)")

    def test_aeon_text_uma_safe_profile(self) -> None:
        contract = _aeon_contract(AEON_UNIT.read_text())
        self.assertEqual(contract["model_len"], AEON_CONTEXT_TOKENS)
        self.assertAlmostEqual(contract["util"], 0.60)
        _assert_aeon_headroom_evidence(contract)

    def test_aeon_unit_documents_current_headroom_evidence(self) -> None:
        unit = AEON_UNIT.read_text()
        description = unit.splitlines()[1]
        self.assertIn("util=0.6", description)
        self.assertIn("AUTO KV", description)
        self.assertIn("bypasses UMA", unit)
        self.assertIn("~31.6GiB MemAvailable", unit)
        self.assertNotIn("36GiB KV keeps ~2.47", unit)

    def test_aeon_headroom_contract_rejects_excessive_utilization(self) -> None:
        unit = AEON_UNIT.read_text().replace(
            "--gpu-memory-utilization 0.60",
            "--gpu-memory-utilization 0.80",
            1,
        )
        with self.assertRaisesRegex(AssertionError, "updated UMA headroom evidence"):
            _assert_aeon_headroom_evidence(_aeon_contract(unit))

    def test_legacy_unit_remains_canonical_fallback(self) -> None:
        self.assertTrue(LEGACY_UNIT.exists())
        self.assertIn("WantedBy=default.target", LEGACY_UNIT.read_text())

    def test_cgroup_helper_validates_cap_and_sets_memory_and_swap(self) -> None:
        helper = CGROUP_HELPER.read_text()
        self.assertRegex(helper, r"expected_gib.*=~.*\^\[1-9\]\[0-9\]\*\$")
        self.assertIn('"MemoryMax=${expected_gib}G"', helper)
        self.assertIn("MemorySwapMax=0", helper)
        self.assertIn("/usr/bin/timeout", helper)
        self.assertNotIn("$(/usr/bin/docker inspect", helper)

    def test_memory_gate_exact_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            meminfo = Path(raw_tmp) / "meminfo"
            env = {"GB10_MEMINFO_PATH": str(meminfo)}
            meminfo.write_text("MemAvailable: 2097152 kB\n")
            at_threshold = subprocess.run(
                ["bash", str(MEMORY_GATE), "2"], env=env, capture_output=True
            )
            self.assertEqual(at_threshold.returncode, 0, at_threshold.stderr)
            meminfo.write_text("MemAvailable: 2097151 kB\n")
            below = subprocess.run(
                ["bash", str(MEMORY_GATE), "2"], env=env, capture_output=True
            )
            self.assertNotEqual(below.returncode, 0)

    def test_guard_owns_runtime_concurrency_and_queues_bursts(self) -> None:
        config = tomllib.loads(CONFIG.read_text())
        server = config["server"]
        self.assertEqual(server["max_in_flight_requests"], 4)
        self.assertEqual(server["max_queued_generation_requests"], 128)
        self.assertEqual(server["generation_queue_timeout_ms"], 1_800_000)

        profiles = {profile["name"]: profile for profile in config["upstreams"]}
        self.assertEqual(profiles["aeon-chat"]["max_in_flight_requests"], 4)
        self.assertEqual(profiles["aeon-chat"]["max_queued_generation_requests"], 64)
        self.assertEqual(profiles["qwen3-embedding-8b"]["max_in_flight_requests"], 8)
        self.assertEqual(
            profiles["qwen3-embedding-8b"]["max_queued_generation_requests"], 64
        )
        self.assertEqual(profiles["qwen3-reranker-8b"]["max_in_flight_requests"], 8)
        self.assertEqual(
            profiles["qwen3-reranker-8b"]["max_queued_generation_requests"], 64
        )

    def test_guard_uses_vllm_native_thinking_budget_schema_everywhere(self) -> None:
        config = tomllib.loads(CONFIG.read_text())
        profiles = {profile["name"]: profile for profile in config["upstreams"]}
        schemas = [
            profiles["aeon-chat"]["thinking"]["default_injection_schema"],
            config["thinking"]["default_injection_schema"],
            *[
                rung["default_injection_schema"]
                for rung in config["retry"]["ladder"]
            ],
        ]
        self.assertEqual(len(schemas), 6)
        self.assertEqual(set(schemas), {"vllm_native"})

    def test_guard_queue_body_residency_fits_memory_budget(self) -> None:
        config = tomllib.loads(CONFIG.read_text())
        server = config["server"]
        profiles = config["upstreams"]
        self.assertEqual(server["max_request_body_bytes"], 4 * 1024 * 1024)
        profile_slots = sum(
            p["max_in_flight_requests"] + p["max_queued_generation_requests"]
            for p in profiles
        )
        resident_slots = server["max_in_flight_requests"] + profile_slots
        projected_bytes = (
            384 * 1024 * 1024
            + resident_slots * server["max_request_body_bytes"] * 3 // 2
        )

        unit = GUARD_UNIT.read_text()
        high_match = re.search(r"^MemoryHigh=(\d+)([MG])$", unit, re.MULTILINE)
        max_match = re.search(r"^MemoryMax=(\d+)([MG])$", unit, re.MULTILINE)
        if high_match is None or max_match is None:
            self.fail("Guard MemoryHigh/MemoryMax missing")
        multipliers = {"M": 1024 * 1024, "G": 1024 * 1024 * 1024}
        memory_high = int(high_match.group(1)) * multipliers[high_match.group(2)]
        memory_max = int(max_match.group(1)) * multipliers[max_match.group(2)]
        self.assertLessEqual(projected_bytes, memory_high)
        self.assertLess(memory_high, memory_max)

    def test_vllm_backend_ceiling_exceeds_guard_runtime_concurrency(self) -> None:
        unit = AEON_UNIT.read_text()
        context_match = re.search(r"--max-model-len (\d+)", unit)
        backend_match = re.search(r"--max-num-seqs (\d+)", unit)
        if context_match is None or backend_match is None:
            self.fail("AEON context or sequence limit is missing")
        context_window = int(context_match.group(1))
        backend_limit = int(backend_match.group(1))
        self.assertEqual(context_window, AEON_CONTEXT_TOKENS)
        # DFlash on GB10 is capped at 16 concurrent sequences (AEON guidance).
        self.assertEqual(backend_limit, AEON_MAX_NUM_SEQS)

        config = tomllib.loads(CONFIG.read_text())
        profiles = {profile["name"]: profile for profile in config["upstreams"]}
        self.assertLess(profiles["aeon-chat"]["max_in_flight_requests"], backend_limit)

    def test_readme_disables_legacy_before_enabling_querit(self) -> None:
        readme = README.read_text()
        disable = readme.index(
            "systemctl --user disable --now vllm-qwen3-reranker-8b.service"
        )
        enable = readme.index("systemctl --user enable --now querit-4b-reranker.service")
        self.assertLess(disable, enable)


if __name__ == "__main__":
    unittest.main()