import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REBUILD_SCRIPT = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.sh"
GUARD_CONFIG = ROOT / "config" / "llm-guard-proxy" / "config.toml"


class GuardProductionFeatureContractTests(unittest.TestCase):
    def test_cached_rebuild_enables_guard_feature_explicitly(self) -> None:
        script = REBUILD_SCRIPT.read_text()
        build = next(
            line
            for line in script.splitlines()
            if "cargo build" in line and "-p llm-guard-proxy" in line
        )
        self.assertRegex(build, r"--features(?:=|\s+)guard(?:\s|$)")

    def test_production_config_bounds_workflow_executions(self) -> None:
        raw = GUARD_CONFIG.read_text()
        config = tomllib.loads(raw)
        # Current deployed binary rejects unknown [guard_workflows]; keep it
        # commented until the live binary supports the section again.
        if "guard_workflows" in config:
            self.assertEqual(config["guard_workflows"]["max_in_flight_executions"], 4)
        else:
            self.assertIn("# [guard_workflows]", raw)
            self.assertIn("not supported by deployed binary", raw)


if __name__ == "__main__":
    unittest.main()
