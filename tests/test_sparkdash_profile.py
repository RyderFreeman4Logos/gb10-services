#!/usr/bin/env python3
"""sparkDash profile contracts: pin, loopback bind, llmHost overlay, no Docker."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profile" / "sparkdash"


class SparkdashProfileTests(unittest.TestCase):
    def test_pin_is_exact_upstream_revision(self) -> None:
        pin = (PROFILE / "PIN").read_text()
        self.assertIn("bbec3bb7886a95498cb495f574d1099a854f8e7f", pin)
        self.assertIn("https://github.com/MiaAI-Lab/sparkDash", pin)
        self.assertIn("127.0.0.1:20080", pin)

    def test_env_is_loopback_only_and_host_sysfs(self) -> None:
        env = (PROFILE / "sparkdash.env").read_text()
        self.assertIn("BIND_HOST=127.0.0.1", env)
        self.assertIn("PORT=20080", env)
        self.assertIn("SPARKDASH_ALLOW_OPEN_REMOTE=0", env)
        self.assertIn("HOST_PROC_PATH=/proc", env)
        self.assertNotRegex(env, r"(?m)^BIND_HOST=0\.0\.0\.0$")

    def test_unit_does_not_couple_to_model_services(self) -> None:
        unit = (PROFILE / "sparkdash.service").read_text()
        self.assertIn("ExecStart=/home/obj/.local/share/mise/installs/node/22.23.2/bin/node server/index.js", unit)
        self.assertIn("EnvironmentFile=/home/obj/.config/sparkdash/sparkdash.env", unit)
        self.assertNotRegex(unit, r"(?m)^Requires=")
        self.assertNotIn("vllm-", unit)
        self.assertNotIn("llm-guard-proxy", unit)
        self.assertNotIn("docker.sock", unit)

    def test_sparks_json_monitors_raw_vllm_ports_not_guard(self) -> None:
        sparks = json.loads((PROFILE / "sparks.json").read_text())
        spark = sparks["sparks"][0]
        self.assertEqual(spark["llmPorts"], [18010, 18012, 18013])
        self.assertTrue(spark["isLocal"])
        self.assertEqual(spark["lanIp"], "100.105.4.92")
        self.assertFalse(spark["hermesMonitoring"])
        self.assertFalse(spark["comfyMonitoring"])
        self.assertTrue(spark["storagePollDisabled"])

    def test_llm_host_prefers_lan_ip(self) -> None:
        src = (PROFILE / "llmHost.js").read_text()
        self.assertIn("if (ip) return ip;", src)
        self.assertIn("if (spark?.isLocal) return \"127.0.0.1\";", src)
        self.assertTrue(re.search(r"export function llmProbeHost", src))

    def test_install_script_is_syntax_valid(self) -> None:
        script = ROOT / "scripts" / "sparkdash_install.sh"
        self.assertTrue(script.is_file())
        self.assertIn("checkout --detach", script.read_text())


if __name__ == "__main__":
    unittest.main()
