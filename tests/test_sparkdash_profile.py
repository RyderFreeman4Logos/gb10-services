#!/usr/bin/env python3
"""sparkDash profile contracts: pin, loopback bind, read-only CSRF, installer bytes."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profile" / "sparkdash"
INSTALLER = ROOT / "scripts" / "sparkdash_install.sh"
AGENT_PLAYBOOK = ROOT / "docs" / "deployment" / "AGENTS.md"


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
        self.assertIn("SPARKDASH_READ_ONLY=1", env)
        self.assertIn("HOST_PROC_PATH=/proc", env)
        self.assertNotRegex(env, r"(?m)^BIND_HOST=0\.0\.0\.0$")
        self.assertNotRegex(env, r"(?m)^SPARKDASH_TOKEN=")

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
        self.assertIn('if (spark?.isLocal) return "127.0.0.1";', src)
        self.assertTrue(re.search(r"export function llmProbeHost", src))

    def test_fresh_stack_docs_do_not_install_optional_dashboard_unit(self) -> None:
        agents = AGENT_PLAYBOOK.read_text()
        block = agents.split("### 5. Systemd User Services Installation", 1)[1]
        block = block.split("systemctl --user daemon-reload", 1)[0]
        self.assertNotIn("profile/sparkdash/sparkdash.service", block)
        self.assertIn("scripts/sparkdash_install.sh", agents)

    def test_install_script_attests_pin_bytes_and_rebuilds(self) -> None:
        script = INSTALLER.read_text()
        self.assertTrue(INSTALLER.is_file())
        self.assertNotIn("reset --hard", script)
        self.assertNotIn("git clean", script)
        self.assertNotIn("git stash", script)
        self.assertIn("status --porcelain", script)
        self.assertIn("npm ci --no-audit --no-fund", script)
        self.assertIn("npm run build", script)
        self.assertNotIn('! -d "${CHECKOUT}/node_modules"', script)
        self.assertIn("${PROFILE}/auth.js", script)


def _node_bin() -> str:
    found = subprocess.check_output(["bash", "-lc", "command -v node"], text=True).strip()
    if not found:
        raise unittest.SkipTest("node is required for sparkDash auth overlay tests")
    return found


class SparkdashReadOnlyAuthTests(unittest.TestCase):
    def test_readonly_rejects_hostile_origin_post_not_get(self) -> None:
        node = _node_bin()
        auth = PROFILE / "auth.js"
        self.assertTrue(auth.is_file(), "profile overlay auth.js is required")
        probe = r"""
import { pathToFileURL } from "node:url";
const { createAuthMiddleware } = await import(pathToFileURL(process.env.SPARKDASH_AUTH_JS).href);
const mw = createAuthMiddleware();
function call(method, origin) {
  const req = { method, headers: origin ? { origin } : {}, query: {} };
  let status = 200;
  let body = null;
  let nexted = false;
  const res = {
    status(code) { status = code; return this; },
    json(payload) { body = payload; return this; },
  };
  mw(req, res, () => { nexted = true; });
  return { status, body, nexted };
}
const post = call("POST", "https://evil.example");
const get = call("GET", "https://evil.example");
if (post.nexted || post.status !== 403) {
  throw new Error(`POST not blocked: ${JSON.stringify(post)}`);
}
if (!get.nexted || get.status !== 200) {
  throw new Error(`GET telemetry blocked: ${JSON.stringify(get)}`);
}
"""
        env = os.environ.copy()
        env["SPARKDASH_READ_ONLY"] = "1"
        env["SPARKDASH_ALLOW_OPEN_REMOTE"] = "0"
        env["BIND_HOST"] = "127.0.0.1"
        env["SPARKDASH_AUTH_JS"] = str(auth)
        completed = subprocess.run(
            [node, "--input-type=module", "-e", probe],
            env=env,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "sparkdash-test",
        "GIT_AUTHOR_EMAIL": "sparkdash-test@example.invalid",
        "GIT_COMMITTER_NAME": "sparkdash-test",
        "GIT_COMMITTER_EMAIL": "sparkdash-test@example.invalid",
    }


def _prepare_installer_fixture(tmp: Path) -> tuple[Path, Path, Path, dict[str, str], str]:
    checkout = tmp / "sparkDash"
    home = tmp / "home"
    bin_dir = tmp / "bin"
    npm_log = tmp / "npm.log"
    checkout.mkdir()
    home.mkdir()
    bin_dir.mkdir()
    (checkout / "server" / "collectors").mkdir(parents=True)
    (checkout / "config").mkdir()
    (checkout / "server" / "index.js").write_text("console.log('pin');\n")
    (checkout / "server" / "collectors" / "llmHost.js").write_text(
        "export function llmProbeHost() {}\n"
    )
    (checkout / "server" / "auth.js").write_text("export function createAuthMiddleware() {}\n")
    (checkout / "package.json").write_text("{}\n")
    git_env = _git_env()
    subprocess.run(["git", "init"], cwd=checkout, env=git_env, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=checkout, env=git_env, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "pin"],
        cwd=checkout,
        env=git_env,
        check=True,
        capture_output=True,
    )
    pin = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    (checkout / "node_modules").mkdir()
    (checkout / "dist").mkdir()
    (bin_dir / "node").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "npm").write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"${NPM_LOG}\"\nexit 0\n"
    )
    for name in ("node", "npm"):
        (bin_dir / name).chmod(stat.S_IRWXU)
    root = tmp / "gb10-services"
    profile = root / "profile" / "sparkdash"
    scripts = root / "scripts"
    profile.mkdir(parents=True)
    scripts.mkdir()
    for name in ("llmHost.js", "sparks.json", "sparkdash.env", "sparkdash.service", "auth.js"):
        src = PROFILE / name
        if src.is_file():
            (profile / name).write_bytes(src.read_bytes())
    (profile / "PIN").write_text(
        "\n".join(
            [
                "repo=https://github.com/MiaAI-Lab/sparkDash",
                f"commit={pin}",
                f"node_bin={bin_dir / 'node'}",
                "bind=127.0.0.1:20080",
                "",
            ]
        )
    )
    installer = scripts / "sparkdash_install.sh"
    installer.write_bytes(INSTALLER.read_bytes())
    installer.chmod(stat.S_IRWXU)
    (tmp / "tmp").mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "SPARKDASH_CHECKOUT": str(checkout),
        "NPM_LOG": str(npm_log),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TMPDIR": str(tmp / "tmp"),
    }
    return checkout, home, npm_log, env, pin


class SparkdashInstallerAttestationTests(unittest.TestCase):
    def test_installer_rejects_unexpected_dirty_tracked_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkdash-install-") as raw:
            tmp = Path(raw)
            checkout, home, npm_log, env, _pin = _prepare_installer_fixture(tmp)
            dirty = "console.log('DIRTY');\n"
            (checkout / "server" / "index.js").write_text(dirty)
            pin_llm = (checkout / "server" / "collectors" / "llmHost.js").read_text()
            completed = subprocess.run(
                ["bash", str(tmp / "gb10-services" / "scripts" / "sparkdash_install.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertEqual((checkout / "server" / "index.js").read_text(), dirty)
            self.assertEqual((checkout / "server" / "collectors" / "llmHost.js").read_text(), pin_llm)
            self.assertFalse(npm_log.exists())
            self.assertFalse((home / ".config" / "sparkdash" / "sparkdash.env").exists())

    def test_installer_rebuilds_clean_and_known_overlay_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkdash-install-") as raw:
            tmp = Path(raw)
            checkout, home, npm_log, env, _pin = _prepare_installer_fixture(tmp)
            user_env = home / ".config" / "sparkdash" / "sparkdash.env"
            user_env.parent.mkdir(parents=True)
            user_env.write_text("USER_RUNTIME=keep\n")
            (checkout / "server" / "collectors" / "llmHost.js").write_text(
                "export function llmProbeHost() { return 'overlay-dirty'; }\n"
            )
            installer = tmp / "gb10-services" / "scripts" / "sparkdash_install.sh"
            completed = subprocess.run(
                ["bash", str(installer)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertEqual((checkout / "server" / "index.js").read_text(), "console.log('pin');\n")
            log = npm_log.read_text()
            self.assertIn("ci --no-audit --no-fund", log)
            self.assertIn("run build", log)
            overlay = (checkout / "server" / "collectors" / "llmHost.js").read_text()
            self.assertIn("if (ip) return ip;", overlay)
            self.assertEqual(user_env.read_text(), "USER_RUNTIME=keep\n")
            auth = (checkout / "server" / "auth.js").read_text()
            self.assertIn("createAuthMiddleware", auth)
            self.assertIn("SPARKDASH_READ_ONLY", auth)


if __name__ == "__main__":
    unittest.main()
