from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Self

import tomllib

ROOT = Path(__file__).resolve().parents[1]
REBUILD_SCRIPT = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.sh"
REBUILD_ENGINE = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.py"
GUARD_CONFIG = ROOT / "config" / "llm-guard-proxy" / "config.toml"
README = ROOT / "README.md"
DEPLOYMENT_GUIDE = ROOT / "docs" / "deployment" / "AGENTS.md"
PRODUCTION_COMPLETE = "LLM_GUARD_REBUILD_PRODUCTION_COMPLETE"
TEST_COMPLETE = "LLM_GUARD_REBUILD_TEST_ONLY_COMPLETE"


class GuardProductionFeatureContractTests(unittest.TestCase):
    def test_production_config_bounds_workflow_executions(self) -> None:
        # Active [guard_workflows] is temporarily commented: the installed
        # binary fail-closes on unknown sections. Keep the intended bound in
        # comments so deploy does not forget the value when re-enabled.
        text = GUARD_CONFIG.read_text()
        config = tomllib.loads(text)
        self.assertNotIn("guard_workflows", config)
        self.assertRegex(text, r"(?m)^# \[guard_workflows\]\s*$")
        self.assertRegex(text, r"(?m)^# max_in_flight_executions = 4\s*$")

    def test_docs_match_production_and_test_only_rebuild_boundaries(self) -> None:
        readme = README.read_text()
        deployment = DEPLOYMENT_GUIDE.read_text()
        for text in (readme, deployment):
            normalized = " ".join(text.split())
            for required in (
                "Production invocation accepts no override environment variables",
                "`--test-only`",
                "immutable Git archive",
                "`MainPID`",
                "`InvocationID`",
                "`/proc/<pid>/stat` starttime",
                "held `/proc/<pid>/exe` file descriptor",
                "atomic rename",
                "directory `fsync`",
                PRODUCTION_COMPLETE,
                "never restarts a vLLM backend",
            ):
                self.assertIn(required, normalized)


class RebuildFixture:
    cargo_identity = (
        "cargo 1.90.0 (fixture)\n"
        "release: 1.90.0\n"
        "host: x86_64-unknown-linux-gnu\n"
    )
    rustc_identity = (
        "rustc 1.90.0 (fixture)\n"
        "binary: rustc\n"
        "commit-hash: fixture\n"
        "host: x86_64-unknown-linux-gnu\n"
        "release: 1.90.0\n"
        "LLVM version: fixture\n"
    )

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "bin"
        self.remote = self.root / "remote"
        self.source_dir = self.root / "source"
        self.cache_root = self.root / "cache"
        self.service_bin = self.root / "service" / "llm-guard-proxy"
        self.guard_config = self.root / "guard" / "config.toml"
        self.guard_unit = self.root / "systemd" / "llm-guard-proxy.service"
        self.proc_root = self.root / "proc"
        self.receipt_dir = self.root / "receipts"
        self.state_path = self.root / "state.json"
        self.tool_log = self.root / "tools.log"
        self.prior = self.root / "prior-llm-guard-proxy"
        self.wrong_hash = self.root / "wrong-hash-llm-guard-proxy"
        self.same_hash_other_inode = self.root / "same-hash-other-inode"
        self.build_source = Path("/usr/bin/true")

        for directory in (
            self.home,
            self.fake_bin,
            self.remote,
            self.service_bin.parent,
            self.guard_config.parent,
            self.guard_unit.parent,
            self.proc_root / "sys" / "kernel" / "random",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self._init_remote()
        self.source_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self.source_tree = self._git("rev-parse", "HEAD^{tree}").stdout.strip()
        self.binary_sha256 = hashlib.sha256(self.build_source.read_bytes()).hexdigest()
        self.candidate = (
            self.cache_root
            / "releases"
            / f"{self.source_commit}-{self.binary_sha256}"
            / "llm-guard-proxy"
        )

        shutil.copyfile("/usr/bin/false", self.prior)
        shutil.copyfile("/usr/bin/false", self.wrong_hash)
        shutil.copyfile(self.build_source, self.same_hash_other_inode)
        for binary in (self.prior, self.wrong_hash, self.same_hash_other_inode):
            binary.chmod(0o755)
        self.service_bin.symlink_to(self.prior)
        self.guard_config.write_text('private_config_payload = "fixture-only"\n')
        self.guard_unit.write_text("[Service]\n# private-unit-payload\n")
        (self.proc_root / "sys" / "kernel" / "random" / "boot_id").write_text(
            "12345678-1234-4abc-8def-1234567890ab\n"
        )
        self.state = {
            "active": True,
            "sub": "running",
            "pid": 4242,
            "invocation": "1" * 32,
            "systemd_start": 1000,
            "proc_start": 2000,
            "running_target": str(self.prior),
            "restart_calls": 0,
            "restart_failures": 0,
            "health_failures": 0,
            "health_generation_drift": False,
            "show_calls": 0,
            "candidate_restart_mode": "exact",
            "drift_on_show": 0,
            "drift_kind": "",
            "mutate_source_during_cargo": False,
        }
        self.save_state()
        self._write_proc()
        self._write_dispatcher()

        self.env = {
            "CACHE_ROOT": str(self.cache_root),
            "FIXTURE_BUILD_SOURCE": str(self.build_source),
            "GUARD_TEST_STATE": str(self.state_path),
            "GUARD_TEST_TOOL_LOG": str(self.tool_log),
            "HOME": str(self.home),
            "LC_ALL": "C",
            "LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG": str(self.guard_config),
            "LLM_GUARD_PROXY_REBUILD_GUARD_UNIT": str(self.guard_unit),
            "LLM_GUARD_PROXY_REBUILD_PROC_ROOT": str(self.proc_root),
            "LLM_GUARD_PROXY_REBUILD_RECEIPT_DIR": str(self.receipt_dir),
            "LOG_DIR": str(self.root / "log"),
            "LOG_FILE": str(self.root / "legacy-receipt.log"),
            "PATH": f"{self.fake_bin}:/usr/bin:/bin",
            "SERVICE_BIN": str(self.service_bin),
            "SOURCE_BRANCH": "main",
            "SOURCE_DIR": str(self.source_dir),
            "SOURCE_REPO": str(self.remote),
            "EXPECTED_CANDIDATE": str(self.candidate),
            "TZ": "UTC",
        }

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.temp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        return subprocess.run(
            ["/usr/bin/git", "-C", str(self.remote), *args],
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )

    def _init_remote(self) -> None:
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.remote), "init", "-b", "main"],
            check=True,
            text=True,
            capture_output=True,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        (self.remote / "src").mkdir()
        (self.remote / "Cargo.toml").write_text(
            "[workspace]\nmembers = [\"llm-guard-proxy\"]\nresolver = \"2\"\n"
        )
        crate = self.remote / "llm-guard-proxy"
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            "[package]\nname = \"llm-guard-proxy\"\nversion = \"0.0.0\"\n"
            "edition = \"2021\"\n[features]\nguard = []\n"
        )
        (crate / "src" / "main.rs").write_text("fn main() {}\n")
        self._git("add", "--", "Cargo.toml", "llm-guard-proxy")
        self._git("commit", "-m", "fixture source")

    def save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, sort_keys=True))

    def reload_state(self) -> dict[str, object]:
        self.state = json.loads(self.state_path.read_text())
        return self.state

    def _write_proc(self) -> None:
        pid_dir = self.proc_root / str(self.state["pid"])
        pid_dir.mkdir(parents=True, exist_ok=True)
        exe = pid_dir / "exe"
        exe.unlink(missing_ok=True)
        exe.symlink_to(str(self.state["running_target"]))
        fields = ["S", *("0" for _ in range(18)), str(self.state["proc_start"])]
        (pid_dir / "stat").write_text(
            f"{self.state['pid']} (guard (fixture) name) " + " ".join(fields) + "\n"
        )

    def set_state(self, **updates: object) -> None:
        self.reload_state()
        self.state.update(updates)
        self.save_state()
        self._write_proc()

    def ensure_candidate(self) -> None:
        self.candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.build_source, self.candidate)
        self.candidate.chmod(0o755)

    def set_prior_absent_with_candidate_runtime(self) -> None:
        self.ensure_candidate()
        self.service_bin.unlink()
        self.set_state(running_target=str(self.candidate))

    def _write_dispatcher(self) -> None:
        dispatcher = self.fake_bin / "fixture-tool"
        dispatcher.write_text(
            textwrap.dedent(
                r'''#!/usr/bin/python3
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ["GUARD_TEST_STATE"])
log_path = Path(os.environ["GUARD_TEST_TOOL_LOG"])
state = json.loads(state_path.read_text())
with log_path.open("a") as log:
    log.write(name + " " + " ".join(args) + "\n")

def save():
    state_path.write_text(json.dumps(state, sort_keys=True))

def write_proc(target):
    proc = Path(os.environ["LLM_GUARD_PROXY_REBUILD_PROC_ROOT"])
    pid_dir = proc / str(state["pid"])
    pid_dir.mkdir(parents=True, exist_ok=True)
    exe = pid_dir / "exe"
    try:
        exe.unlink()
    except FileNotFoundError:
        pass
    exe.symlink_to(target)
    fields = ["S"] + ["0"] * 18 + [str(state["proc_start"])]
    (pid_dir / "stat").write_text(
        f"{state['pid']} (guard (fixture) name) " + " ".join(fields) + "\n"
    )

def activate(target):
    state["pid"] += 1
    state["systemd_start"] += 100
    state["proc_start"] += 100
    state["invocation"] = f"{int(state['invocation'], 16) + 1:032x}"[-32:]
    state["active"] = True
    state["sub"] = "running"
    state["running_target"] = str(target)
    write_proc(target)
    save()

if name == "cargo":
    if args == ["--version", "--verbose"]:
        print("cargo 1.90.0 (fixture)")
        print("release: 1.90.0")
        print("host: x86_64-unknown-linux-gnu")
    elif args and args[0] == "build":
        manifest = Path(args[args.index("--manifest-path") + 1])
        if state.get("mutate_source_during_cargo"):
            source = Path(os.environ["SOURCE_DIR"]) / "Cargo.toml"
            original = source.read_bytes()
            mode = source.stat().st_mode & 0o777
            source.write_bytes(b"transient dirty source\n")
            source.chmod(0o600)
            source.write_bytes(original)
            source.chmod(mode)
        target = Path(os.environ["CARGO_TARGET_DIR"]) / "release" / "llm-guard-proxy"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(os.environ["FIXTURE_BUILD_SOURCE"], target)
        target.chmod(0o755)
        save()
    else:
        raise SystemExit(91)
elif name == "rustc":
    if args != ["-vV"]:
        raise SystemExit(92)
    print("rustc 1.90.0 (fixture)")
    print("binary: rustc")
    print("commit-hash: fixture")
    print("host: x86_64-unknown-linux-gnu")
    print("release: 1.90.0")
    print("LLVM version: fixture")
elif name == "systemctl":
    joined = " ".join(args)
    if joined == "--user is-active --quiet llm-guard-proxy.service":
        raise SystemExit(0 if state["active"] else 3)
    if joined == "--user show -p MainPID --value llm-guard-proxy.service":
        print(state["pid"] if state["active"] else 0)
    elif " show " in f" {joined} " or joined.startswith("--user show "):
        state["show_calls"] += 1
        if state.get("drift_on_show") == state["show_calls"]:
            kind = state.get("drift_kind")
            if kind == "invocation":
                state["invocation"] = "e" * 32
            elif kind == "pid":
                state["pid"] += 1
                state["proc_start"] += 1
                write_proc(state["running_target"])
            elif kind in {"starttime", "pid-reuse"}:
                state["proc_start"] += 1
                write_proc(state["running_target"])
        save()
        values = {
            "LoadState": "loaded",
            "ActiveState": "active" if state["active"] else "inactive",
            "SubState": state["sub"] if state["active"] else "dead",
            "FragmentPath": os.environ["LLM_GUARD_PROXY_REBUILD_GUARD_UNIT"],
            "DropInPaths": "",
            "MainPID": str(state["pid"] if state["active"] else 0),
            "InvocationID": state["invocation"] if state["active"] else "",
            "ExecMainStartTimestampMonotonic": str(
                state["systemd_start"] if state["active"] else 0
            ),
        }
        for key in (
            "LoadState", "ActiveState", "SubState", "FragmentPath",
            "DropInPaths", "MainPID", "InvocationID",
            "ExecMainStartTimestampMonotonic",
        ):
            print(f"{key}={values[key]}")
    elif joined == "--user restart llm-guard-proxy.service":
        state["restart_calls"] += 1
        link = Path(os.environ["SERVICE_BIN"])
        target = Path(os.readlink(link))
        if not target.is_absolute():
            target = link.parent / target
        if str(target) == os.environ["EXPECTED_CANDIDATE"]:
            mode = state.get("candidate_restart_mode", "exact")
            if mode == "wrong-hash":
                target = Path(os.environ["WRONG_HASH_TARGET"])
            elif mode == "same-hash-different-inode":
                target = Path(os.environ["SAME_HASH_TARGET"])
        activate(target)
        if state.get("restart_failures", 0):
            state["restart_failures"] -= 1
            save()
            raise SystemExit(7)
    else:
        print("unexpected systemctl args: " + joined, file=sys.stderr)
        raise SystemExit(93)
elif name == "curl":
    if state.get("health_failures", 0):
        state["health_failures"] -= 1
        save()
        raise SystemExit(22)
    if state.get("health_generation_drift"):
        state["health_generation_drift"] = False
        activate(state["running_target"])
elif name == "sleep":
    pass
elif name == "sha256sum":
    paths = [arg for arg in args if arg != "--"]
    if paths:
        payload = Path(paths[0]).read_bytes()
        label = paths[0]
    else:
        payload = sys.stdin.buffer.read()
        label = "-"
    print(f"{hashlib.sha256(payload).hexdigest()}  {label}")
else:
    print("unexpected fixture tool: " + name, file=sys.stderr)
    raise SystemExit(94)
'''
            )
        )
        dispatcher.chmod(0o755)
        for name in ("cargo", "rustc", "systemctl", "curl", "sleep", "sha256sum"):
            (self.fake_bin / name).symlink_to(dispatcher.name)

    def run(
        self,
        *,
        test_only: bool = True,
        extra_env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(
            {
                "WRONG_HASH_TARGET": str(self.wrong_hash),
                "SAME_HASH_TARGET": str(self.same_hash_other_inode),
            }
        )
        if extra_env:
            env.update(extra_env)
        command = [str(REBUILD_SCRIPT)]
        if test_only:
            command.append("--test-only")
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def calls(self) -> str:
        return self.tool_log.read_text() if self.tool_log.exists() else ""

    def assert_no_backend_lifecycle(self, test: unittest.TestCase) -> None:
        calls = self.calls()
        test.assertNotIn("vllm-", calls)
        test.assertNotIn("docker ", calls)

    def assert_prior_restored(self, test: unittest.TestCase) -> None:
        test.assertTrue(self.service_bin.is_symlink())
        test.assertEqual(os.readlink(self.service_bin), str(self.prior))
        state = self.reload_state()
        test.assertEqual(state["running_target"], str(self.prior))
        test.assertTrue(state["active"])


class GuardRebuildProvenanceTests(unittest.TestCase):
    def test_launcher_hash_pins_real_module_without_embedded_python(self) -> None:
        launcher = REBUILD_SCRIPT.read_text()
        self.assertTrue(REBUILD_ENGINE.is_file(), "real rebuild engine is missing")
        self.assertTrue(launcher.startswith("#!/usr/bin/bash -p\n"))
        self.assertLessEqual(len(launcher.splitlines()), 64)
        self.assertNotIn("<<'PY'", launcher)
        self.assertIn("/usr/bin/env -i", launcher)
        self.assertIn("/usr/bin/python3 -I -B -S", launcher)
        match = re.search(
            r'^expected_engine_sha256="([0-9a-f]{64})"$', launcher, re.MULTILINE
        )
        self.assertIsNotNone(match, "launcher engine authority is missing")
        assert match is not None
        self.assertEqual(
            hashlib.sha256(REBUILD_ENGINE.read_bytes()).hexdigest(), match.group(1)
        )
        self.assertIn("class RebuildError", REBUILD_ENGINE.read_text())

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_failed_without_completion(
        self, result: subprocess.CompletedProcess[str]
    ) -> str:
        output = self.output(result)
        self.assertNotEqual(result.returncode, 0, output)
        self.assertNotIn(PRODUCTION_COMPLETE, output)
        self.assertNotIn(TEST_COMPLETE, output)
        return output

    def test_production_rejects_overrides_before_build_or_link(self) -> None:
        with RebuildFixture() as fixture:
            bash_marker = fixture.root / "bash-env-ran"
            python_marker = fixture.root / "python-site-ran"
            bash_env = fixture.root / "bash-env"
            bash_env.write_text(f"printf x > {shlex.quote(str(bash_marker))}\n")
            python_path = fixture.root / "python-path"
            python_path.mkdir()
            (python_path / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(python_marker)!r}).write_text('x')\n"
            )
            result = fixture.run(
                test_only=False,
                extra_env={
                    "BASH_ENV": str(bash_env),
                    "LLM_GUARD_REBUILD_TEST_ONLY": "1",
                    "PYTHONPATH": str(python_path),
                },
            )
            output = self.assert_failed_without_completion(result)
            self.assertIn("--test-only", output)
            self.assertIn("LLM_GUARD_REBUILD_TEST_ONLY", output)
            self.assertEqual(fixture.calls(), "")
            self.assertFalse(bash_marker.exists())
            self.assertFalse(python_marker.exists())
            fixture.assert_prior_restored(self)

    def test_exact_candidate_uses_snapshot_and_durable_test_only_receipt(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run()
            output = self.output(result)
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("LLM_GUARD_REBUILD_TEST_ONLY=1", output)
            self.assertIn(TEST_COMPLETE, output)
            self.assertNotIn(PRODUCTION_COMPLETE, output)
            self.assertTrue(fixture.service_bin.is_symlink())
            self.assertEqual(os.readlink(fixture.service_bin), str(fixture.candidate))
            state = fixture.reload_state()
            self.assertEqual(state["running_target"], str(fixture.candidate))
            receipts = list(fixture.receipt_dir.glob("*.receipt.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(stat.S_IMODE(receipts[0].stat().st_mode), 0o600)
            completion = re.search(
                rf"{TEST_COMPLETE} receipt_sha256=([0-9a-f]{{64}}) receipt=(\S+)",
                output,
            )
            self.assertIsNotNone(completion)
            assert completion is not None
            self.assertEqual(Path(completion.group(2)), receipts[0])
            self.assertEqual(
                completion.group(1), hashlib.sha256(receipts[0].read_bytes()).hexdigest()
            )
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(receipt["mode"], "test-only")
            self.assertEqual(receipt["source_commit"], fixture.source_commit)
            self.assertEqual(receipt["source_tree"], fixture.source_tree)
            self.assertRegex(receipt["source_archive_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(receipt["snapshot_content_sha256"], r"^[0-9a-f]{64}$")
            serialized = receipts[0].read_text()
            for forbidden in (
                "private_config_payload",
                "private-unit-payload",
                "credential",
                "prompt",
                "response",
                "header",
                "payload",
            ):
                self.assertNotIn(forbidden, serialized)
            calls = fixture.calls()
            cargo_line = next(line for line in calls.splitlines() if line.startswith("cargo build "))
            self.assertIn("--release -p llm-guard-proxy --features guard", cargo_line)
            manifest = cargo_line.split("--manifest-path ", 1)[1].split()[0]
            self.assertNotEqual(Path(manifest), fixture.source_dir / "Cargo.toml")
            self.assertNotIn(str(fixture.source_dir), manifest)
            source_status = subprocess.run(
                ["/usr/bin/git", "-C", str(fixture.source_dir), "status", "--porcelain=v1"],
                check=True,
                text=True,
                capture_output=True,
                env={"HOME": str(fixture.home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            ).stdout
            self.assertEqual(source_status, "")
            fixture.assert_no_backend_lifecycle(self)

    def test_wrong_hash_and_same_hash_other_inode_roll_back(self) -> None:
        for mode, diagnostic in (
            ("wrong-hash", "SHA-256"),
            ("same-hash-different-inode", "inode"),
        ):
            with self.subTest(mode=mode), RebuildFixture() as fixture:
                fixture.set_state(candidate_restart_mode=mode)
                output = self.assert_failed_without_completion(fixture.run())
                self.assertIn(diagnostic, output)
                fixture.assert_prior_restored(self)
                fixture.assert_no_backend_lifecycle(self)

    def test_missing_conditional_tool_rolls_back_without_restart(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(
                extra_env={"LLM_GUARD_REBUILD_TEST_MISSING_TOOL": "curl"}
            )
            output = self.assert_failed_without_completion(result)
            self.assertIn("required tool unavailable: curl", output)
            fixture.assert_prior_restored(self)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)
            fixture.assert_no_backend_lifecycle(self)

    def test_inactive_and_unsupported_service_prestates_fail_before_mutation(self) -> None:
        with self.subTest(prestate="inactive"), RebuildFixture() as fixture:
            fixture.set_state(active=False, sub="dead")
            self.assert_failed_without_completion(fixture.run())
            self.assertTrue(fixture.service_bin.is_symlink())
            self.assertEqual(os.readlink(fixture.service_bin), str(fixture.prior))
            self.assertFalse(fixture.reload_state()["active"])
            self.assertNotIn("cargo build ", fixture.calls())
        with self.subTest(prestate="regular-service-file"), RebuildFixture() as fixture:
            fixture.service_bin.unlink()
            fixture.service_bin.write_text("unsupported\n")
            original = fixture.service_bin.read_bytes()
            self.assert_failed_without_completion(fixture.run())
            self.assertFalse(fixture.service_bin.is_symlink())
            self.assertEqual(fixture.service_bin.read_bytes(), original)
            self.assertNotIn("cargo build ", fixture.calls())

    def test_restart_failure_and_health_failure_restore_prior_runtime(self) -> None:
        for updates in ({"restart_failures": 1}, {"health_failures": 1}):
            with self.subTest(updates=updates), RebuildFixture() as fixture:
                fixture.set_state(**updates)
                self.assert_failed_without_completion(fixture.run())
                fixture.assert_prior_restored(self)
                restart_calls = fixture.reload_state()["restart_calls"]
                if not isinstance(restart_calls, int):
                    self.fail("fixture restart count is not an integer")
                self.assertGreaterEqual(restart_calls, 2)
                fixture.assert_no_backend_lifecycle(self)

    def test_every_receipt_failure_rolls_back_and_emits_no_completion(self) -> None:
        for stage in (
            "create",
            "write",
            "fsync",
            "rename",
            "dir-fsync",
            "enospc",
            "completion-sink",
        ):
            with self.subTest(stage=stage), RebuildFixture() as fixture:
                result = fixture.run(
                    extra_env={"LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE": stage}
                )
                self.assert_failed_without_completion(result)
                fixture.assert_prior_restored(self)
                self.assertEqual(list(fixture.receipt_dir.iterdir()), [])
                fixture.assert_no_backend_lifecycle(self)

    def test_source_mutation_and_restore_during_cargo_is_rejected_pre_cutover(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(mutate_source_during_cargo=True)
            output = self.assert_failed_without_completion(fixture.run())
            self.assertIn("source checkout metadata changed during build", output)
            fixture.assert_prior_restored(self)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)
            status = subprocess.run(
                ["/usr/bin/git", "-C", str(fixture.source_dir), "status", "--porcelain=v1"],
                check=True,
                text=True,
                capture_output=True,
                env={"HOME": str(fixture.home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            ).stdout
            self.assertEqual(status, "")

    def test_generation_and_proc_drift_after_attestation_roll_back(self) -> None:
        for kind in ("pid", "invocation", "starttime", "pid-reuse"):
            with self.subTest(kind=kind), RebuildFixture() as fixture:
                fixture.set_state(drift_on_show=5, drift_kind=kind)
                output = self.assert_failed_without_completion(fixture.run())
                self.assertIn("generation changed", output)
                fixture.assert_prior_restored(self)
                fixture.assert_no_backend_lifecycle(self)

    def test_generation_drift_at_durable_publication_removes_receipt(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(drift_on_show=6, drift_kind="invocation")
            output = self.assert_failed_without_completion(fixture.run())
            self.assertIn("generation changed", output)
            self.assertEqual(list(fixture.receipt_dir.glob("*.receipt.json")), [])
            fixture.assert_prior_restored(self)
            fixture.assert_no_backend_lifecycle(self)

    def test_executable_replacement_during_held_fd_hash_rolls_back(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_REPLACE_EXE_DURING_HASH": str(
                        fixture.same_hash_other_inode
                    )
                }
            )
            output = self.assert_failed_without_completion(result)
            self.assertIn("current proc executable no longer names held inode", output)
            fixture.assert_prior_restored(self)
            fixture.assert_no_backend_lifecycle(self)

    def test_health_is_bound_to_the_attested_generation(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(health_generation_drift=True)
            output = self.assert_failed_without_completion(fixture.run())
            self.assertIn("changed during health check", output)
            fixture.assert_prior_restored(self)
            fixture.assert_no_backend_lifecycle(self)

    def test_prior_absence_is_restored_on_receipt_failure_without_runtime_restart(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_prior_absent_with_candidate_runtime()
            result = fixture.run(
                extra_env={"LLM_GUARD_REBUILD_TEST_FAIL_RECEIPT_STAGE": "write"}
            )
            self.assert_failed_without_completion(result)
            self.assertFalse(fixture.service_bin.exists())
            self.assertFalse(fixture.service_bin.is_symlink())
            state = fixture.reload_state()
            self.assertEqual(state["running_target"], str(fixture.candidate))
            self.assertEqual(state["restart_calls"], 0)
            fixture.assert_no_backend_lifecycle(self)


if __name__ == "__main__":
    unittest.main()
