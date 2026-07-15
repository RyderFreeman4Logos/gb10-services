from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "gb10_deploy_memory_guardian.sh"


class DeploymentOrderingContractTests(unittest.TestCase):
    def test_deployment_installs_complete_reviewed_bundle_before_activation(self) -> None:
        deploy = DEPLOY.read_text()
        required_sources = (
            "config/gb10-memory-guardian/config.toml",
            "target/release/gb10-memory-guardian",
            "scripts/gb10_enforce_docker_cgroup_limits.sh",
            "scripts/gb10_memory_guardian_canary.sh",
            "systemd/gb10-memory-guardian.service",
            "systemd/gb10-memory-guardian-canary.service",
            "systemd/vllm-aeon-27b-dflash.service",
            "systemd/querit-4b-reranker.service",
            "systemd/vllm-qwen3-reranker-8b.service",
        )
        activation = deploy.index('enable --now "$guardian_unit"')
        for source in required_sources:
            self.assertIn(source, deploy)
            self.assertLess(deploy.index(source), activation, source)
        self.assertIn("install -m 0600", deploy)
        self.assertNotIn("systemd/*", deploy)
        self.assertIn("configured-target", deploy)
        self.assertIn("trap fail_closed_activation EXIT", deploy)

    def test_runbook_uses_fail_closed_deployer_not_loose_manual_enable(self) -> None:
        readme = (ROOT / "README.md").read_text()
        self.assertIn("gb10_deploy_memory_guardian.sh", readme)
        self.assertIn("aeon-text", readme)
        self.assertIn("text-cgroup.v1", readme)
        self.assertIn("owner-only", readme)
        self.assertIn("read-only configured-target identity check", readme)
        self.assertNotIn("cp systemd/*", readme)

    def test_deployer_never_swallows_failure_to_stop_stale_guardian(self) -> None:
        source = DEPLOY.read_text()
        for line in source.splitlines():
            if 'disable --now "$guardian_unit"' in line:
                self.assertNotIn("|| true", line)


if __name__ == "__main__":
    unittest.main()
