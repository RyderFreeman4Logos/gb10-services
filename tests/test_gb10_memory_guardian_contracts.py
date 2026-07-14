from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "Cargo.toml"
CORE = ROOT / "crates" / "gb10-memory-guardian-core" / "src" / "lib.rs"
CORE_NO_ALLOC_TEST = ROOT / "crates" / "gb10-memory-guardian-core" / "tests" / "no_alloc.rs"
BINARY = ROOT / "crates" / "gb10-memory-guardian" / "src" / "main.rs"
GUARDIAN_UNIT = ROOT / "systemd" / "gb10-memory-guardian.service"
TARGET_CONFIG = ROOT / "config" / "gb10-memory-guardian" / "config.toml"
CANARY_DRIVER_UNIT = ROOT / "systemd" / "gb10-memory-guardian-canary.service"
QUERIT_UNIT = ROOT / "systemd" / "querit-4b-reranker.service"
TEXT_UNIT = ROOT / "systemd" / "vllm-aeon-27b-dflash.service"
EMBEDDING_UNIT = ROOT / "systemd" / "vllm-embedding.service"
LEGACY_RERANKER_UNIT = ROOT / "systemd" / "vllm-qwen3-reranker-8b.service"
GUARD_UNIT = ROOT / "systemd" / "llm-guard-proxy.service"
CGROUP_HELPER = ROOT / "scripts" / "gb10_enforce_docker_cgroup_limits.sh"
CANARY = ROOT / "scripts" / "gb10_memory_guardian_canary.sh"
HEALTHCHECK = ROOT / "scripts" / "aeon_healthcheck.sh"
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
            "GB10_MEMORY_GUARDIAN_CONFIG_PATH",
        ):
            self.assertIn(override, binary)
        self.assertNotIn("std::process::Command", core)
        self.assertNotIn("std::process::Command", binary)

        for forbidden in ("Command::new", "process::Command"):
            self.assertNotIn(forbidden, core)
            self.assertNotIn(forbidden, binary)

        emergency_branch = binary.split("controller.enter_emergency();", 1)[1].split(
            "} else {", 1
        )[0]
        self.assertNotIn("eprintln!", emergency_branch)
        self.assertNotIn("active_label", emergency_branch)

    def test_guardian_unit_reserves_and_protects_memory(self) -> None:
        unit = GUARDIAN_UNIT.read_text()
        for setting in (
            "MemoryMin=64M",
            "MemoryMax=96M",
            "MemorySwapMax=0",
            "OOMScoreAdjust=100",
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

    def test_guardian_uses_tracked_config_without_hardcoded_target_identity(self) -> None:
        binary = BINARY.read_text()
        unit = GUARDIAN_UNIT.read_text()
        config = tomllib.loads(TARGET_CONFIG.read_text())

        self.assertEqual(config["schema_version"], 1)
        self.assertEqual(config["target"]["label"], "aeon-text")
        self.assertEqual(config["target"]["registration_file"], "text-cgroup.v1")
        self.assertIn("TargetRegistrationSet", binary)
        self.assertIn("GB10_MEMORY_GUARDIAN_CONFIG_PATH", binary)
        self.assertIn("XDG_CONFIG_HOME", binary)
        self.assertIn("HOME", binary)
        self.assertNotIn("REGISTRATION_NAME", binary)
        self.assertNotIn("querit", binary.lower())
        self.assertNotIn("--shed-registered-querit", binary)
        self.assertNotIn("querit-cgroup", CORE.read_text().lower())
        self.assertNotIn("querit-cgroup", CORE_NO_ALLOC_TEST.read_text().lower())
        self.assertIn(
            "Environment=GB10_MEMORY_GUARDIAN_CONFIG_PATH=%h/.config/gb10-memory-guardian/config.toml",
            unit,
        )

    def test_guardian_initial_reconcile_emits_armed_identity_receipt(self) -> None:
        binary = BINARY.read_text()
        initial_reconcile = binary.split(
            "let initial_transition = targets.reconcile()", 1
        )[1].split("let started", 1)[0]
        self.assertIn("armed target", initial_reconcile)
        self.assertIn("targets.active_label()", initial_reconcile)

    def test_guardian_unit_pins_and_binary_enforces_production_target_identity(self) -> None:
        unit = GUARDIAN_UNIT.read_text()
        binary = BINARY.read_text()
        self.assertIn("GB10_MEMORY_GUARDIAN_EXPECTED_LABEL=aeon-text", unit)
        self.assertIn(
            "GB10_MEMORY_GUARDIAN_EXPECTED_REGISTRATION_FILE=text-cgroup.v1", unit
        )
        self.assertIn("enforce_expected_target_identity", binary)
        self.assertGreaterEqual(binary.count("enforce_expected_target_identity"), 3)

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
        self.assertIn('"$registration_dir" != "$expected_registration_dir"', helper)
        self.assertIn('[[ -L "$registration_dir" ]]', helper)
        self.assertIn("^[0-9a-f]{64}$", helper)
        self.assertIn("app.slice/${scope}", helper)
        cleanup_trap = helper.index("trap fail_closed_registration EXIT")
        self.assertLess(
            helper.index('if [[ "$registration_dir" != "$expected_registration_dir"'),
            cleanup_trap,
            "the cleanup trap must never see an unvalidated registration path",
        )
        self.assertLess(
            helper.index('/usr/bin/install -d -m 0700 "$registration_dir"'),
            cleanup_trap,
            "the cleanup trap must never traverse an unvalidated parent directory",
        )
        for hardcoded_target in (
            "querit-4b-reranker",
            "vllm-aeon-27b-dflash",
            "vllm-embedding",
            "vllm-qwen3-reranker",
        ):
            self.assertNotIn(hardcoded_target, helper.lower())

    def test_canary_has_only_fixed_safe_targets_and_benchmark_gate(self) -> None:
        canary = CANARY.read_text()
        driver = CANARY_DRIVER_UNIT.read_text()
        self.assertIn("GB10_BENCHMARK_EXCLUDED", canary)
        self.assertIn("gb10-memory-guardian-disposable-canary.service", canary)
        self.assertIn("gb10-memory-guardian-canary.service", canary)
        self.assertIn("configured-target", canary)
        self.assertIn("read-only", canary)
        self.assertNotIn("--kill-configured-target", canary)
        self.assertNotIn("I_UNDERSTAND_CONFIGURED_TARGET_WILL_BE_KILLED", canary)
        self.assertIn("GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT", canary)
        self.assertIn("tomllib", canary)
        self.assertIn("registration_file", canary)
        self.assertIn("GB10_CGROUP_REGISTRATION_PATH", canary)
        self.assertIn("LoadState", canary)
        self.assertIn("ActiveState", canary)
        self.assertIn("SubState", canary)
        self.assertIn("MainPID", canary)
        self.assertIn("Result", canary)
        self.assertIn("ExecMainStatus", canary)
        self.assertIn("NRestarts", canary)
        self.assertNotIn("--target", canary)
        for protected in (
            "vllm-embedding.service",
            "querit-4b-reranker.service",
            "vllm-qwen3-reranker-8b.service",
            "llm-guard-proxy.service",
            "gb10-memory-guardian.service",
        ):
            self.assertIn(protected, canary)
        self.assertIn("--disposable-canary", driver)
        self.assertGreaterEqual(
            canary.count('run_systemctl revert "$canary_unit"'),
            2,
            "the disposable transient unit must be reverted before create and during cleanup",
        )
        disposable = canary.split("run_disposable()", 1)[1].split(
            "run_configured_target()", 1
        )[0]
        self.assertLess(
            disposable.index('run_systemctl revert "$canary_unit"'),
            disposable.index("run_systemd_run"),
        )
        production = GUARDIAN_UNIT.read_text()
        self.assertIn("OOMScoreAdjust=100", production)
        self.assertIn("OOMScoreAdjust=100", driver)
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

    def test_text_is_the_only_registered_automatic_target(self) -> None:
        text = TEXT_UNIT.read_text()
        text_unit_section = text.split("[Service]", 1)[0]
        self.assertNotIn("Requires=vllm-embedding.service", text_unit_section)
        self.assertNotIn("Wants=vllm-embedding.service", text_unit_section)
        self.assertIn("After=network.target vllm-embedding.service", text_unit_section)
        self.assertNotIn("gb10-memory-guardian.service", text_unit_section)
        self.assertIn("--cgroup-parent app.slice", text)
        self.assertIn(
            "GB10_CGROUP_REGISTRATION_PATH=%t/gb10-memory-guardian/text-cgroup.v1",
            text,
        )
        self.assertRegex(text, r"(?m)^Restart=on-failure$")

        for protected in (
            EMBEDDING_UNIT.read_text(),
            QUERIT_UNIT.read_text(),
            LEGACY_RERANKER_UNIT.read_text(),
        ):
            self.assertNotIn("GB10_CGROUP_REGISTRATION_PATH", protected)
            self.assertNotIn("text-cgroup.v1", protected)

    def test_rerankers_and_guard_do_not_own_text_lifecycle(self) -> None:
        for path in (QUERIT_UNIT, LEGACY_RERANKER_UNIT):
            unit = path.read_text()
            unit_section = unit.split("[Service]", 1)[0]
            for relationship in (
                "Requires=",
                "BindsTo=",
                "PartOf=",
                "PropagatesStopTo=",
                "StopPropagatedFrom=",
            ):
                self.assertNotRegex(
                    unit_section,
                    rf"(?m)^{relationship}.*vllm-aeon-27b-dflash\.service",
                )
            self.assertNotIn("http://100.105.4.92:18010", unit)
            self.assertIn("lifecycle-independent", unit)
        text_unit_section = TEXT_UNIT.read_text().split("[Service]", 1)[0]
        for relationship in ("Conflicts=", "PropagatesStopTo="):
            for line in text_unit_section.splitlines():
                if line.startswith(relationship):
                    self.assertNotIn("querit-4b-reranker.service", line)
                    self.assertNotIn("vllm-qwen3-reranker-8b.service", line)
        self.assertIn("Conflicts=vllm-qwen3-reranker-8b.service", QUERIT_UNIT.read_text())
        self.assertIn("querit-4b-reranker.service", LEGACY_RERANKER_UNIT.read_text())
        self.assertNotIn("gb10-memory-guardian.service", QUERIT_UNIT.read_text())

        guard_section = GUARD_UNIT.read_text().split("[Service]", 1)[0]
        self.assertNotRegex(guard_section, r"(?m)^Wants=.*vllm-")
        self.assertIn("Ordering only", guard_section)

        healthcheck = HEALTHCHECK.read_text().lower()
        for protected in ("vllm-embedding", "querit", "qwen3-reranker"):
            self.assertNotIn(protected, healthcheck)

    def test_readme_documents_text_policy_install_canary_and_rollback(self) -> None:
        readme = README.read_text()
        for phrase in (
            "gb10-memory-guardian.service",
            "cgroup.kill",
            "disposable user cgroup",
            "gb10-swap-guard.service",
            "observer-only",
            "config/gb10-memory-guardian/config.toml",
            "install -m 0600",
            "text-cgroup.v1",
            "embedding and both rerankers",
            "rollback",
        ):
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
