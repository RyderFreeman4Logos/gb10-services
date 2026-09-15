#!/usr/bin/env python3
"""sparkDash profile contracts: pin, loopback bind, read-only CSRF, installer bytes."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
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
        self.assertIn(
            "ExecStart=/usr/bin/env SPARKDASH_READ_ONLY=1 "
            "/home/obj/.local/share/mise/installs/node/22.23.2/bin/node server/index.js",
            unit,
        )
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


def _prepare_installer_fixture(
    tmp: Path,
) -> tuple[Path, Path, Path, Path, dict[str, str], str]:
    checkout = tmp / "sparkDash"
    home = tmp / "home"
    bin_dir = tmp / "bin"
    npm_log = tmp / "npm.log"
    action_log = tmp / "actions.log"
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
    baseline_llm = checkout / "server" / "collectors" / "llmHost.js"
    backup_llm = baseline_llm.with_name("llmHost.js.upstream-bbec3bb")
    backup_llm.write_bytes(baseline_llm.read_bytes())
    backup_llm.chmod(0o664)
    baseline_llm.write_bytes((PROFILE / "llmHost.js").read_bytes())
    baseline_llm.chmod(0o600)
    (checkout / "server" / "auth.js").chmod(0o664)
    sparks = checkout / "config" / "sparks.json"
    sparks.write_bytes((PROFILE / "sparks.legacy-bbec3bb.json").read_bytes())
    sparks.chmod(0o644)
    (checkout / "node_modules").mkdir()
    (checkout / "dist").mkdir()
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is required for sparkDash installer tests")
    (bin_dir / "node").symlink_to(node)
    (bin_dir / "npm").write_text(
        "#!/bin/sh\n"
        "printf 'npm:%s\\n' \"$*\" >> \"${ACTION_LOG}\"\n"
        "printf '%s\\n' \"$*\" >> \"${NPM_LOG}\"\n"
        "exit 0\n"
    )
    for name in ("mkdir", "install", "cp"):
        real = shutil.which(name)
        if not real:
            raise unittest.SkipTest(f"{name} is required for sparkDash installer tests")
        (bin_dir / name).write_text(
            "#!/bin/sh\n"
            f"printf '{name}:%s\\n' \"$*\" >> \"${{ACTION_LOG}}\"\n"
            f"exec {shlex.quote(real)} \"$@\"\n"
        )
    real_git = shutil.which("git")
    if not real_git:
        raise unittest.SkipTest("git is required for sparkDash installer tests")
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    clone|fetch|checkout|reset|switch) "
        "printf 'git:%s\\n' \"$*\" >> \"${ACTION_LOG}\"; break ;;\n"
        "  esac\n"
        "done\n"
        f"exec {shlex.quote(real_git)} \"$@\"\n"
    )
    for name in ("npm", "mkdir", "install", "cp", "git"):
        (bin_dir / name).chmod(stat.S_IRWXU)
    root = tmp / "gb10-services"
    profile = root / "profile" / "sparkdash"
    scripts = root / "scripts"
    profile.mkdir(parents=True)
    scripts.mkdir()
    for name in (
        "llmHost.js",
        "sparks.json",
        "sparks.legacy-bbec3bb.json",
        "sparkdash.env",
        "sparkdash.service",
        "auth.js",
    ):
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
        "ACTION_LOG": str(action_log),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TMPDIR": str(tmp / "tmp"),
    }
    return checkout, home, npm_log, action_log, env, pin


def _snapshot(paths: list[Path]) -> dict[Path, tuple[bytes, int]]:
    return {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in paths}


class SparkdashInstallerAttestationTests(unittest.TestCase):
    def test_installer_rejects_unexpected_dirty_tracked_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkdash-install-") as raw:
            tmp = Path(raw)
            checkout, home, npm_log, action_log, env, _pin = _prepare_installer_fixture(tmp)
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
            self.assertFalse(action_log.exists())
            self.assertFalse((home / ".config" / "sparkdash" / "sparkdash.env").exists())

    def test_old_install_upgrade_forces_readonly_and_serves_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkdash-install-") as raw:
            tmp = Path(raw)
            checkout, home, npm_log, _action_log, env, _pin = _prepare_installer_fixture(tmp)
            user_env = home / ".config" / "sparkdash" / "sparkdash.env"
            user_env.parent.mkdir(parents=True)
            old_env = b"USER_RUNTIME=keep\nSPARKDASH_TOKEN=fixture-secret\n"
            user_env.write_bytes(old_env)
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
            self.assertEqual(
                stat.S_IMODE((checkout / "server" / "collectors" / "llmHost.js").stat().st_mode),
                0o644,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (checkout / "server" / "collectors" / "llmHost.js.upstream-bbec3bb").stat().st_mode
                ),
                0o644,
            )
            self.assertEqual(user_env.read_bytes(), old_env)
            auth = (checkout / "server" / "auth.js").read_text()
            self.assertIn("createAuthMiddleware", auth)
            self.assertIn("SPARKDASH_READ_ONLY", auth)

            unit = (home / ".config" / "systemd" / "user" / "sparkdash.service").read_text()
            exec_start = next(
                line.removeprefix("ExecStart=")
                for line in unit.splitlines()
                if line.startswith("ExecStart=")
            )
            argv = shlex.split(exec_start)
            self.assertEqual(argv[:2], ["/usr/bin/env", "SPARKDASH_READ_ONLY=1"])
            argv[2] = shutil.which("node") or self.fail("node is required")
            witness = tmp / "benchmark-launched"
            (checkout / "server" / "index.js").write_text(
                """
import http from "node:http";
import { createAuthMiddleware } from "./auth.js";
import { writeFileSync } from "node:fs";
const middleware = createAuthMiddleware();
const server = http.createServer((req, response) => {
  const res = {
    status(code) { response.statusCode = code; return this; },
    json(payload) { response.setHeader("content-type", "application/json"); response.end(JSON.stringify(payload)); },
  };
  middleware(req, res, () => {
    if (req.method === "POST") writeFileSync(process.env.BENCHMARK_WITNESS, "launched");
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({ ok: true, metadata: "fixture" }));
  });
});
server.listen(0, "127.0.0.1", async () => {
  const port = server.address().port;
  const get = await fetch(`http://127.0.0.1:${port}/api/health`);
  const metadata = await get.json();
  const post = await fetch(`http://127.0.0.1:${port}/api/benchmark`, { method: "POST" });
  const rejected = await post.json();
  server.close();
  if (get.status !== 200 || metadata.metadata !== "fixture") throw new Error("metadata failed");
  if (post.status !== 403 || !rejected.error?.includes("read-only")) throw new Error("mutation was not read-only rejected");
});
"""
            )
            runtime_env = {
                **env,
                "BENCHMARK_WITNESS": str(witness),
                "SPARKDASH_TOKEN": "fixture-secret",
                "USER_RUNTIME": "keep",
            }
            for inherited_readonly in (None, "0"):
                if inherited_readonly is None:
                    runtime_env.pop("SPARKDASH_READ_ONLY", None)
                else:
                    runtime_env["SPARKDASH_READ_ONLY"] = inherited_readonly
                runtime = subprocess.run(
                    argv,
                    cwd=checkout,
                    env=runtime_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(runtime.returncode, 0, runtime.stderr + runtime.stdout)
                self.assertFalse(witness.exists(), "mutating handler launched benchmark work")

    def test_arbitrary_managed_bytes_and_modes_reject_before_any_write(self) -> None:
        cases = (
            ("llm-bytes", "server/collectors/llmHost.js", b"arbitrary-overlay\n", None),
            ("auth-bytes", "server/auth.js", b"arbitrary-auth\n", None),
            ("sparks-bytes", "config/sparks.json", b"{}\n", None),
            ("llm-backup-bytes", "server/collectors/llmHost.js.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("auth-backup-bytes", "server/auth.js.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("llm-mode", "server/collectors/llmHost.js", None, 0o755),
            ("auth-mode", "server/auth.js", None, 0o755),
            ("sparks-mode", "config/sparks.json", None, 0o600),
            ("llm-backup-mode", "server/collectors/llmHost.js.upstream-bbec3bb", None, 0o755),
            ("auth-backup-mode", "server/auth.js.upstream-bbec3bb", None, 0o755),
        )
        for name, relative, content, mode in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix="sparkdash-install-"
            ) as raw:
                tmp = Path(raw)
                checkout, home, npm_log, action_log, env, _pin = _prepare_installer_fixture(tmp)
                user_env = home / ".config" / "sparkdash" / "sparkdash.env"
                user_env.parent.mkdir(parents=True)
                user_env.write_bytes(b"USER_RUNTIME=keep\nSPARKDASH_TOKEN=fixture-secret\n")
                target = checkout / relative
                if relative == "server/auth.js.upstream-bbec3bb":
                    target.write_bytes(
                        subprocess.check_output(
                            ["git", "show", "HEAD:server/auth.js"], cwd=checkout
                        )
                    )
                    target.chmod(0o664)
                if content is not None:
                    target.write_bytes(content)
                if mode is not None:
                    target.chmod(mode)
                managed = [
                    checkout / "server" / "collectors" / "llmHost.js",
                    checkout / "server" / "auth.js",
                    checkout / "config" / "sparks.json",
                    checkout / "server" / "collectors" / "llmHost.js.upstream-bbec3bb",
                    user_env,
                ]
                if target.name == "auth.js.upstream-bbec3bb":
                    managed.append(target)
                before = _snapshot(managed)
                completed = subprocess.run(
                    ["bash", str(tmp / "gb10-services" / "scripts" / "sparkdash_install.sh")],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0, completed.stderr + completed.stdout)
                self.assertEqual(_snapshot(managed), before)
                self.assertFalse(
                    action_log.exists(), action_log.read_text() if action_log.exists() else ""
                )
                self.assertFalse(npm_log.exists())
                self.assertFalse(
                    (home / ".config" / "systemd" / "user" / "sparkdash.service").exists()
                )


if __name__ == "__main__":
    unittest.main()
