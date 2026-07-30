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
        # Active [guard_workflows] is temporarily commented: the installed
        # binary fail-closes on unknown sections. Keep the intended bound in
        # comments so deploy does not forget the value when re-enabled.
        text = GUARD_CONFIG.read_text()
        config = tomllib.loads(text)
        self.assertNotIn("guard_workflows", config)
        self.assertRegex(
            text,
            r"(?m)^# \[guard_workflows\]\s*$",
        )
        self.assertRegex(
            text,
            r"(?m)^# max_in_flight_executions = 4\s*$",
        )


if __name__ == "__main__":
    unittest.main()
