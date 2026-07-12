from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "Cargo.toml"
CORE = ROOT / "crates" / "gb10-memory-guardian-core" / "src" / "lib.rs"
BINARY = ROOT / "crates" / "gb10-memory-guardian" / "src" / "main.rs"
GUARDIAN_UNIT = ROOT / "systemd" / "gb10-memory-guardian.service"
CANARY_DRIVER_UNIT = ROOT / "systemd" / "gb10-memory-guardian-canary.service"
QUERIT_UNIT = ROOT / "systemd" / "querit-4b-reranker.service"
CGROUP_HELPER = ROOT / "scripts" / "gb10_enforce_docker_cgroup_limits.sh"
CANARY = ROOT / "scripts" / "gb10_memory_guardian_canary.sh"
README = ROOT / "README.md"


class MemoryGuardianContractTests(unittest.TestCase):
    def test_workspace_has_core_and_service_crates(self) -> None:
        manifest = WORKSPACE.read_text()
        self.assertIn('"crates/gb10-memory-guardian-core"', manifest)
        self.assertIn('"crates/gb10-memory-guardian"', manifest)
        self.assertTrue(CORE.is_file())
        self.assertTrue(BINARY.is_file())

    def test_emergency_path_is_direct_and_subprocess_free(self) -> None:
        core = CORE.read_text()
        binary = BINARY.read_text()
        self.assertIn("cgroup.kill", core)
        self.assertIn("EmergencyReserve", binary)
        self.assertIn("kill_direct", core)
        self.assertIn("release", core)
        for override in (
            "GB10_MEMORY_GUARDIAN_MEMINFO_PATH",
            "GB10_MEMORY_GUARDIAN_CGROUP_ROOT",
            "GB10_MEMORY_GUARDIAN_REGISTRATION_PATH",
        ):
            self.assertIn(override, binary)
        self.assertNotIn("std::process::Command", core)
        self.assertNotIn("std::process::Command", binary)

        for forbidden in ("Command::new", "process::Command"):
            self.assertNotIn(forbidden, core)
            self.assertNotIn(forbidden, binary)

    def test_guardian_unit_reserves_and_protects_memory(self) -> None:
        unit = GUARDIAN_UNIT.read_text()
        for setting in (
            "MemoryMin=64M",
            "MemoryMax=96M",
            "MemorySwapMax=0",
            "OOMScoreAdjust=-1000",
            "RuntimeDirectory=gb10-memory-guardian",
            "RuntimeDirectoryMode=0700",
            "RuntimeDirectoryPreserve=yes",
        ):
            self.assertIn(setting, unit)
        self.assertIn("GB10_MEMORY_GUARDIAN_RESERVE_MIB=64", unit)
        self.assertIn("GB10_MEMORY_GUARDIAN_MEM_AVAIL_STOP_GIB=1", unit)
        for hardening in (
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "RestrictNamespaces=yes",
        ):
            self.assertIn(hardening, unit)
        self.assertNotIn("CapabilityBoundingSet", unit)

    def test_cgroup_helper_publishes_registration_atomically(self) -> None:
        helper = CGROUP_HELPER.read_text()
        self.assertIn("GB10_CGROUP_REGISTRATION_PATH", helper)
        self.assertIn("container_id=", helper)
        self.assertIn("control_group=", helper)
        self.assertRegex(helper, r"mktemp|\.tmp")
        self.assertRegex(helper, r"mv\s")
        self.assertIn('chmod 0600 "$registration_tmp"', helper)
        self.assertIn("fail_closed_registration", helper)
        self.assertRegex(helper, r"run_docker stop")

    def test_canary_has_only_fixed_safe_targets_and_benchmark_gate(self) -> None:
        canary = CANARY.read_text()
        driver = CANARY_DRIVER_UNIT.read_text()
        self.assertIn("GB10_BENCHMARK_EXCLUDED", canary)
        self.assertIn("gb10-memory-guardian-disposable-canary.service", canary)
        self.assertIn("gb10-memory-guardian-canary.service", canary)
        self.assertIn("--shed-registered-querit", canary)
        self.assertIn("MainPID", canary)
        self.assertNotIn("--target", canary)
        for protected in (
            "vllm-aeon-27b-dflash.service",
            "vllm-embedding.service",
            "llm-guard-proxy.service",
            "gb10-memory-guardian.service",
        ):
            self.assertIn(protected, canary)
        self.assertIn("--disposable-canary", driver)
        production = GUARDIAN_UNIT.read_text()
        for hardening in (
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "RestrictNamespaces=yes",
        ):
            self.assertIn(hardening, production)
            self.assertIn(hardening, driver)
        self.assertNotIn("CapabilityBoundingSet", production)
        self.assertNotIn("CapabilityBoundingSet", driver)
        for unsupported in (
            "PrivateDevices=yes",
            "ProtectClock=yes",
            "ProtectKernelLogs=yes",
            "ProtectKernelModules=yes",
        ):
            self.assertNotIn(unsupported, production)
            self.assertNotIn(unsupported, driver)

    def test_querit_uses_guardian_and_app_slice_without_auto_restart(self) -> None:
        unit = QUERIT_UNIT.read_text()
        self.assertRegex(unit, r"(?m)^Wants=.*gb10-memory-guardian\.service")
        self.assertRegex(unit, r"(?m)^After=.*gb10-memory-guardian\.service")
        self.assertIn("--cgroup-parent app.slice", unit)
        self.assertIn("GB10_CGROUP_REGISTRATION_PATH=%t/gb10-memory-guardian/querit-cgroup.v1", unit)
        self.assertRegex(unit, r"(?m)^Restart=no$")

    def test_readme_documents_canary_rollback_and_bash_guard_overlap(self) -> None:
        readme = README.read_text()
        for phrase in (
            "gb10-memory-guardian.service",
            "cgroup.kill",
            "disposable user cgroup",
            "gb10-swap-guard.service",
            "rollback",
        ):
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
