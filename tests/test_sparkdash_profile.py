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
SPARKDASH_DOCS = ROOT / "docs" / "deployment" / "sparkdash.md"


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
        self.assertIn("npm ci --include=dev --no-audit --no-fund", script)
        self.assertIn("npm run build", script)
        self.assertNotIn('! -d "${CHECKOUT}/node_modules"', script)
        self.assertIn("${PROFILE}/auth.js", script)
        self.assertIn("${PROFILE}/LlmProbe.js", script)
        self.assertIn("${PROFILE}/LlmDaily.js", script)
        self.assertIn("server/collectors/LlmProbe.js", script)
        self.assertIn("server/collectors/LlmDaily.js", script)
        self.assertIn("${CHECKOUT}/dist/index.html", script)
        self.assertIn("generationTpsState", script)
        self.assertIn("built dist missing generationTpsState", script)

    def test_tracked_source_docs_list_daily_overlay(self) -> None:
        docs = SPARKDASH_DOCS.read_text()
        self.assertIn("profile/sparkdash/LlmDaily.js", docs)
        self.assertIn("server/collectors/LlmDaily.js", docs)


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
    (checkout / "src" / "components" / "SparkPage").mkdir(parents=True)
    (checkout / "src" / "api").mkdir(parents=True)
    (checkout / "src" / "hooks").mkdir(parents=True)
    (checkout / "config").mkdir()
    (checkout / "server" / "index.js").write_text("console.log('pin');\n")
    (checkout / "server" / "collectors" / "llmHost.js").write_text(
        "export function llmProbeHost() {}\n"
    )
    (checkout / "server" / "collectors" / "LlmProbe.js").write_text("export class LlmProbe {}\n")
    (checkout / "server" / "collectors" / "LlmDaily.js").write_text("export class LlmDailyStore {}\n")
    (checkout / "server" / "auth.js").write_text("export function createAuthMiddleware() {}\n")
    (checkout / "src" / "components" / "SparkPage" / "LlmPanel.tsx").write_text("export {};\n")
    (checkout / "src" / "api" / "types.ts").write_text("export interface LlmMetrics {}\n")
    (checkout / "src" / "hooks" / "metricsStore.ts").write_text("export {};\n")
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
        """#!/bin/sh
printf 'npm:%s\\n' "$*" >> "${ACTION_LOG}"
printf '%s\\n' "$*" >> "${NPM_LOG}"
case " $* " in
  *" run build "*)
    mkdir -p "${SPARKDASH_CHECKOUT}/dist/assets"
    printf '%s\\n' '<!doctype html><script src="/assets/index.js"></script>' > "${SPARKDASH_CHECKOUT}/dist/index.html"
    printf '%s\\n' 'generationTpsState stale unavailable' > "${SPARKDASH_CHECKOUT}/dist/assets/index.js"
    ;;
esac
exit 0
"""
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
        "LlmProbe.js",
        "LlmDaily.js",
        "LlmPanel.tsx",
        "types.ts",
        "metricsStore.ts",
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

    def test_install_rejects_empty_dist_after_build(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkdash-install-") as raw:
            tmp = Path(raw)
            checkout, home, npm_log, _action_log, env, _pin = _prepare_installer_fixture(tmp)
            npm = Path(env["PATH"].split(":", 1)[0]) / "npm"
            npm.write_text(
                """#!/bin/sh
printf 'npm:%s\\n' "$*" >> "${ACTION_LOG}"
printf '%s\\n' "$*" >> "${NPM_LOG}"
exit 0
"""
            )
            npm.chmod(stat.S_IRWXU)
            (checkout / "dist" / "index.html").write_text("<!doctype html>\n")
            completed = subprocess.run(
                ["bash", str(tmp / "gb10-services" / "scripts" / "sparkdash_install.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertIn("built dist missing generationTpsState", completed.stderr)
            self.assertFalse((home / ".config" / "systemd" / "user" / "sparkdash.service").exists())

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
            self.assertIn("ci --include=dev --no-audit --no-fund", log)
            self.assertIn("run build", log)
            dist_html = (checkout / "dist" / "index.html").read_bytes()
            dist_js = (checkout / "dist" / "assets" / "index.js").read_bytes()
            self.assertIn(b"generationTpsState", dist_html + dist_js)
            self.assertIn(b"stale", dist_html + dist_js)
            overlay = (checkout / "server" / "collectors" / "llmHost.js").read_text()
            self.assertIn("if (ip) return ip;", overlay)
            probe = (checkout / "server" / "collectors" / "LlmProbe.js").read_text()
            self.assertIn("VLLM_RATE_STALE_WINDOW_MS", probe)
            daily = (checkout / "server" / "collectors" / "LlmDaily.js").read_text()
            self.assertIn("generationTpsState !== \"stale\"", daily)
            self.assertEqual(
                stat.S_IMODE((checkout / "server" / "collectors" / "LlmProbe.js").stat().st_mode),
                0o644,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (checkout / "server" / "collectors" / "LlmProbe.js.upstream-bbec3bb").stat().st_mode
                ),
                0o644,
            )
            self.assertEqual(
                stat.S_IMODE((checkout / "server" / "collectors" / "LlmDaily.js").stat().st_mode),
                0o644,
            )
            self.assertEqual(
                stat.S_IMODE(
                    (checkout / "server" / "collectors" / "LlmDaily.js.upstream-bbec3bb").stat().st_mode
                ),
                0o644,
            )
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
            self.assertIn("rateLabel", (checkout / "src" / "components" / "SparkPage" / "LlmPanel.tsx").read_text())
            self.assertIn("generationTpsState", (checkout / "src" / "api" / "types.ts").read_text())
            self.assertIn(
                'llm.generationTpsState !== "stale"',
                (checkout / "src" / "hooks" / "metricsStore.ts").read_text(),
            )

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
            ("probe-bytes", "server/collectors/LlmProbe.js", b"arbitrary-probe\n", None),
            ("daily-bytes", "server/collectors/LlmDaily.js", b"arbitrary-daily\n", None),
            ("panel-bytes", "src/components/SparkPage/LlmPanel.tsx", b"arbitrary-panel\n", None),
            ("types-bytes", "src/api/types.ts", b"arbitrary-types\n", None),
            ("store-bytes", "src/hooks/metricsStore.ts", b"arbitrary-store\n", None),
            ("auth-bytes", "server/auth.js", b"arbitrary-auth\n", None),
            ("sparks-bytes", "config/sparks.json", b"{}\n", None),
            ("llm-backup-bytes", "server/collectors/llmHost.js.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("probe-backup-bytes", "server/collectors/LlmProbe.js.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("daily-backup-bytes", "server/collectors/LlmDaily.js.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("panel-backup-bytes", "src/components/SparkPage/LlmPanel.tsx.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("types-backup-bytes", "src/api/types.ts.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("store-backup-bytes", "src/hooks/metricsStore.ts.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("auth-backup-bytes", "server/auth.js.upstream-bbec3bb", b"arbitrary-backup\n", None),
            ("llm-mode", "server/collectors/llmHost.js", None, 0o755),
            ("probe-mode", "server/collectors/LlmProbe.js", None, 0o755),
            ("daily-mode", "server/collectors/LlmDaily.js", None, 0o755),
            ("panel-mode", "src/components/SparkPage/LlmPanel.tsx", None, 0o755),
            ("types-mode", "src/api/types.ts", None, 0o755),
            ("store-mode", "src/hooks/metricsStore.ts", None, 0o755),
            ("auth-mode", "server/auth.js", None, 0o755),
            ("sparks-mode", "config/sparks.json", None, 0o600),
            ("llm-backup-mode", "server/collectors/llmHost.js.upstream-bbec3bb", None, 0o755),
            ("probe-backup-mode", "server/collectors/LlmProbe.js.upstream-bbec3bb", None, 0o755),
            ("daily-backup-mode", "server/collectors/LlmDaily.js.upstream-bbec3bb", None, 0o755),
            ("panel-backup-mode", "src/components/SparkPage/LlmPanel.tsx.upstream-bbec3bb", None, 0o755),
            ("types-backup-mode", "src/api/types.ts.upstream-bbec3bb", None, 0o755),
            ("store-backup-mode", "src/hooks/metricsStore.ts.upstream-bbec3bb", None, 0o755),
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
                if relative.endswith(".upstream-bbec3bb"):
                    source = relative.removesuffix(".upstream-bbec3bb")
                    target.write_bytes(
                        subprocess.check_output(
                            ["git", "show", f"HEAD:{source}"], cwd=checkout
                        )
                    )
                    target.chmod(0o664)
                if content is not None:
                    target.write_bytes(content)
                if mode is not None:
                    target.chmod(mode)
                managed = [
                    checkout / "server" / "collectors" / "llmHost.js",
                    checkout / "server" / "collectors" / "LlmProbe.js",
                    checkout / "server" / "collectors" / "LlmDaily.js",
                    checkout / "server" / "auth.js",
                    checkout / "src" / "components" / "SparkPage" / "LlmPanel.tsx",
                    checkout / "src" / "api" / "types.ts",
                    checkout / "src" / "hooks" / "metricsStore.ts",
                    checkout / "config" / "sparks.json",
                    checkout / "server" / "collectors" / "llmHost.js.upstream-bbec3bb",
                    user_env,
                ]
                if target.name.endswith(".upstream-bbec3bb"):
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


class SparkdashVllmRateFreshnessTests(unittest.TestCase):
    def test_vllm_rates_are_windowed_stale_and_recover(self) -> None:
        node = _node_bin()
        probe = PROFILE / "LlmProbe.js"
        self.assertTrue(probe.is_file(), "profile LlmProbe overlay is required")
        with tempfile.TemporaryDirectory(prefix="sparkdash-probe-") as raw:
            tmp = Path(raw)
            collector = tmp / "server" / "collectors"
            collector.mkdir(parents=True)
            (tmp / "server" / "config.js").write_text("export const LLM_PROBE_TIMEOUT_MS = 1;\n")
            (tmp / "server" / "validate.js").write_text(
                'export function classifyHostScope() { return "local"; }\n'
            )
            shutil.copy2(PROFILE / "llmHost.js", collector / "llmHost.js")
            shutil.copy2(probe, collector / "LlmProbe.js")
            (tmp / "package.json").write_text('{"type":"module"}\n')
            script = r"""
const { LlmProbe } = await import(process.env.SPARKDASH_LLMPROBE_JS);
let now = 1_000;
Date.now = () => now;
const probe = new LlmProbe({ isLocal: true }, 18010);
probe.backendType = "vllm";
const metrics = ({ prompt, generation, running, iteration }) => [
  `vllm:prompt_tokens_total ${prompt}`,
  `vllm:generation_tokens_total ${generation}`,
  `vllm:num_requests_running ${running}`,
  `vllm:iteration_tokens_total_sum ${iteration}`,
].join("\n");
const apply = (sample, advanceMs = 1_000) => {
  now += advanceMs;
  probe._applyVllmMetrics(metrics(sample), advanceMs / 1_000);
  return probe._getSnapshot();
};
let snap = apply({ prompt: 100, generation: 10, running: 1, iteration: 110 });
if (snap.generationTpsState !== "unavailable" || snap.prefillTpsState !== "unavailable") {
  throw new Error(`first sample was not unavailable: ${JSON.stringify(snap)}`);
}
snap = apply({ prompt: 200, generation: 20, running: 1, iteration: 220 });
if (snap.generationTpsState !== "fresh" || snap.prefillTpsState !== "fresh") {
  throw new Error(`advancing counters were not fresh: ${JSON.stringify(snap)}`);
}
snap = apply({ prompt: 300, generation: 20, running: 1, iteration: 320 });
if (snap.prefillTpsState !== "fresh" || snap.generationTpsState !== "stale" || snap.generationTpsAgeSeconds == null) {
  throw new Error(`prompt-only work fabricated decode freshness: ${JSON.stringify(snap)}`);
}
snap = apply({ prompt: 300, generation: 21, running: 1, iteration: 321 }, 22_600);
if (snap.generationTpsState !== "fresh" || !(snap.generationTps > 0 && snap.generationTps < 0.1)) {
  throw new Error(`slow new generation window was not measured: ${JSON.stringify(snap)}`);
}
snap = apply({ prompt: 300, generation: 21, running: 1, iteration: 321 }, 30_001);
if (snap.generationTpsState !== "unavailable" || snap.generationTpsAgeSeconds == null) {
  throw new Error(`expired generation rate was retained: ${JSON.stringify(snap)}`);
}
snap = apply({ prompt: 300, generation: 22, running: 0, iteration: 322 });
if (snap.generationTpsState !== "fresh" || snap.generationTps <= 0) {
  throw new Error(`final counter advance was overwritten by idle: ${JSON.stringify(snap)}`);
}
snap = apply({ prompt: 300, generation: 22, running: 0, iteration: 322 });
if (snap.generationTpsState !== "idle" || snap.generationTps !== 0) {
  throw new Error(`verified idle did not replace the prior counter window: ${JSON.stringify(snap)}`);
}
probe._fetch = async (url) => {
  if (url.endsWith("/v1/models")) {
    return { ok: true, status: 200, json: async () => ({ data: [{ id: "fixture" }] }) };
  }
  return { ok: false, status: 503 };
};
snap = await probe._probeOpenAICompatible();
if (snap.generationTpsState !== "unavailable" || snap.requestsRunning !== null) {
  throw new Error(`metrics failure fabricated idle: ${JSON.stringify(snap)}`);
}
snap = apply({ prompt: 1, generation: 1, running: 1, iteration: 2 });
if (snap.generationTpsState !== "unavailable" || snap.prefillTpsState !== "unavailable") {
  throw new Error(`counter reset was treated as throughput: ${JSON.stringify(snap)}`);
}
snap = apply({ prompt: 11, generation: 11, running: 1, iteration: 22 });
if (snap.generationTpsState !== "fresh" || snap.prefillTpsState !== "fresh") {
  throw new Error(`new counter window did not recover: ${JSON.stringify(snap)}`);
}
const unavailable = probe._defaultLlm();
if (unavailable.available || unavailable.generationTpsState !== "unavailable" || unavailable.prefillTpsState !== "unavailable") {
  throw new Error(`disconnect was not unavailable: ${JSON.stringify(unavailable)}`);
}
"""
            env = os.environ.copy()
            env["SPARKDASH_LLMPROBE_JS"] = (collector / "LlmProbe.js").as_uri()
            completed = subprocess.run(
                [node, "--input-type=module", "-e", script],
                env=env,
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)


class SparkdashDailyFreshnessTests(unittest.TestCase):
    def test_daily_rollup_excludes_stale_held_vllm_rates(self) -> None:
        node = _node_bin()
        daily = PROFILE / "LlmDaily.js"
        self.assertTrue(daily.is_file(), "profile LlmDaily overlay is required")
        with tempfile.TemporaryDirectory(prefix="sparkdash-daily-") as raw:
            tmp = Path(raw)
            collectors = tmp / "server" / "collectors"
            collectors.mkdir(parents=True)
            shutil.copy2(daily, collectors / "LlmDaily.js")
            (tmp / "server" / "config.js").write_text(
                f"export const LLM_DAILY_JSON_PATH = {str(tmp / 'daily.json')!r};\n"
            )
            util = tmp / "server" / "util"
            util.mkdir()
            (util / "atomicWrite.js").write_text(
                "import fs from 'fs'; export function atomicWrite(path, body) { fs.writeFileSync(path, body); }\n"
            )
            (tmp / "package.json").write_text('{"type":"module"}\n')
            script = r"""
const { LlmDailyStore } = await import(process.env.SPARKDASH_LLMDAILY_JS);
const store = new LlmDailyStore(process.env.SPARKDASH_LLMDAILY_OUT);
const now = new Date("2026-09-16T12:00:00Z");
store.record("spark-a", 18010, { available: true, generationTps: 12, prefillTps: 30, generationTpsState: "fresh", prefillTpsState: "fresh" }, now);
store.record("spark-a", 18010, { available: true, generationTps: 12, prefillTps: 30, generationTpsState: "stale", prefillTpsState: "stale" }, now);
store.record("spark-a", 18010, { available: true, generationTps: 12, prefillTps: 30, generationTpsState: "stale", prefillTpsState: "stale" }, now);
const day = store.getSeries("spark-a", 18010, { days: 1, now }).days[0];
if (day.decodeAvg !== 12 || day.prefillAvg !== 30) {
  throw new Error(`stale samples accumulated into daily rates: ${JSON.stringify(day)}`);
}
"""
            env = os.environ.copy()
            env["SPARKDASH_LLMDAILY_JS"] = (collectors / "LlmDaily.js").as_uri()
            env["SPARKDASH_LLMDAILY_OUT"] = str(tmp / "daily.json")
            completed = subprocess.run(
                [node, "--input-type=module", "-e", script],
                env=env,
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)


class SparkdashRatePresentationTests(unittest.TestCase):
    def test_dashboard_labels_stale_unavailable_rates_and_skips_stale_history(self) -> None:
        panel = PROFILE / "LlmPanel.tsx"
        types = PROFILE / "types.ts"
        store = PROFILE / "metricsStore.ts"
        self.assertTrue(panel.is_file(), "profile LlmPanel overlay is required")
        self.assertTrue(types.is_file(), "profile API type overlay is required")
        self.assertTrue(store.is_file(), "profile metrics store overlay is required")
        self.assertIn("stale", panel.read_text())
        self.assertIn("unavailable", panel.read_text())
        self.assertIn("generationTpsAgeSeconds", panel.read_text())
        self.assertIn("generationTpsState", types.read_text())
        self.assertIn('llm.generationTpsState !== "stale"', store.read_text())
        self.assertIn('llm.prefillTpsState !== "stale"', store.read_text())


if __name__ == "__main__":
    unittest.main()
