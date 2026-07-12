from __future__ import annotations

import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUERIT_UNIT = ROOT / "systemd" / "querit-4b-reranker.service"
AEON_UNIT = ROOT / "systemd" / "vllm-aeon-27b-dflash.service"
GUARD_UNIT = ROOT / "systemd" / "llm-guard-proxy.service"
LEGACY_UNIT = ROOT / "systemd" / "vllm-qwen3-reranker-8b.service"
CGROUP_HELPER = ROOT / "scripts" / "gb10_enforce_docker_cgroup_limits.sh"
MEMORY_GATE = ROOT / "scripts" / "gb10_check_mem_available.sh"
CONFIG = ROOT / "config" / "llm-guard-proxy" / "config.toml"
README = ROOT / "README.md"

IMAGE_DIGEST = "sha256:f6d453d0b4a7ef90eefee486f4ff769cc2e1bb1e206df16d70370da09c02203c"
MODEL_SNAPSHOT = "7b796de30ad8dc772d6c46c75659c1341283a665"
SHORT_GENERATION_REQUEST_TOKENS = 8_192


class QueritServiceContractTests(unittest.TestCase):
    def test_unit_uses_pinned_offline_artifacts(self) -> None:
        unit = QUERIT_UNIT.read_text()
        self.assertIn(f"@{IMAGE_DIGEST}", unit)
        self.assertIn(
            "models--Querit--Querit-4B:/models/querit-repo:ro", unit
        )
        self.assertIn(f"--model /models/querit-repo/snapshots/{MODEL_SNAPSHOT}", unit)
        self.assertIn("--entrypoint python3", unit)
        self.assertNotIn("pip install", unit)
        self.assertNotIn("--dns", unit)
        self.assertNotRegex(unit, r"aeon-vllm-ultimate:[^\s\\]+(?:\s|\\)")

    def test_unit_has_safe_lifecycle_memory_gate_and_readiness(self) -> None:
        unit = QUERIT_UNIT.read_text()
        for relationship in (
            "Requires=vllm-aeon-27b-dflash.service",
            "BindsTo=vllm-aeon-27b-dflash.service",
            "PartOf=vllm-aeon-27b-dflash.service",
            "Conflicts=vllm-qwen3-reranker-8b.service",
        ):
            self.assertIn(relationship, unit)
        self.assertIn("gb10_check_mem_available.sh 2", unit)
        self.assertIn("--memory 18g", unit)
        self.assertIn("--memory-swap 18g", unit)
        enforce = unit.index("gb10_enforce_docker_cgroup_limits.sh querit-4b-reranker 18")
        ready = unit.index("http://100.105.4.92:18013/v1/models")
        self.assertLess(enforce, ready)
        timeout = re.search(r"^TimeoutStartSec=(\d+)$", unit, re.MULTILINE)
        if timeout is None:
            self.fail("TimeoutStartSec missing")
        readiness_loops = re.findall(
            r"seq 1 (\d+).*?--max-time (\d+).*?sleep (\d+); done",
            unit,
        )
        self.assertEqual(len(readiness_loops), 2)
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

    def test_guard_depends_on_querit_and_has_live_hardening(self) -> None:
        unit = GUARD_UNIT.read_text()
        unit_section = unit.split("[Service]", 1)[0]
        self.assertIn("querit-4b-reranker.service", unit_section)
        self.assertNotIn("vllm-qwen3-reranker-8b.service", unit_section)
        wants = next(
            line for line in unit_section.splitlines() if line.startswith("Wants=")
        )
        self.assertNotIn("querit-4b-reranker.service", wants)
        for setting in (
            "MemoryHigh=1792M",
            "MemoryMax=2G",
            "MemorySwapMax=0",
            "OOMScoreAdjust=-200",
        ):
            self.assertIn(setting, unit)
        self.assertIn("CacheDirectory=llm-guard-proxy-evidence", unit)
        self.assertIn("CacheDirectoryMode=0700", unit)

    def test_aeon_unit_matches_pinned_live_memory_profile(self) -> None:
        unit = AEON_UNIT.read_text()
        for contract in (
            "--memory 69g",
            "--memory-swap 69g",
            "--kv-cache-memory-bytes 36864M",
            "gb10_enforce_docker_cgroup_limits.sh vllm-aeon-27b-dflash-n12 69",
            f"@{IMAGE_DIGEST}",
        ):
            self.assertIn(contract, unit)
        self.assertNotIn("--dns", unit)
        self.assertNotRegex(unit, r"aeon-vllm-ultimate:[^\s\\]+(?:\s|\\)")

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
        expected_limit = context_window // SHORT_GENERATION_REQUEST_TOKENS * 2
        self.assertEqual(backend_limit, expected_limit)

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