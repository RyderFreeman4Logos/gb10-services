from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Self

ROOT = Path(__file__).resolve().parents[1]
REBUILD_SCRIPT = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.sh"

__all__ = ["RebuildFixture"]


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
        self.toolchain_root = self.root / "toolchain"
        self.toolchain_bin = self.toolchain_root / "bin"
        self.registry_cache = self.root / "registry-cache"
        self.registry_index = self.root / "registry-index"
        self.authority_config = self.root / "authority.json"
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
            self.toolchain_bin,
            self.registry_cache,
            self.registry_index,
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
            "replace_bwrap_during_use": False,
            "rename_source_mount": False,
            "artifact_mode": "",
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
        self.env["LLM_GUARD_REBUILD_TEST_CONFIG"] = str(self.authority_config)
        self._write_authority_config()

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
        (self.remote / "Cargo.lock").write_text(
            "version = 4\n\n[[package]]\nname = \"llm-guard-proxy\"\nversion = \"0.0.0\"\n"
        )
        crate = self.remote / "llm-guard-proxy"
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            "[package]\nname = \"llm-guard-proxy\"\nversion = \"0.0.0\"\n"
            "edition = \"2021\"\n[features]\nguard = []\n"
        )
        (crate / "src" / "main.rs").write_text("fn main() {}\n")
        self._git("add", "--", "Cargo.toml", "Cargo.lock", "llm-guard-proxy")
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

name = "__TOOL_NAME__"
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
elif name == "bwrap":
    mounts = {}
    for index, argument in enumerate(args):
        if (
            argument in {"--ro-bind", "--bind"}
            and index + 2 < len(args)
            and args[index + 2] in {"/src", "/target"}
        ):
            mounts[args[index + 2]] = Path(os.readlink(args[index + 1]))
    source_root = mounts["/src"]
    target_root = mounts["/target"]
    if state.get("replace_bwrap_during_use"):
        logical = Path(os.environ["BWRAP_LOGICAL_PATH"])
        replacement = logical.with_suffix(".replacement")
        replacement.write_text("#!/bin/sh\nexit 99\n")
        replacement.chmod(0o755)
        os.replace(replacement, logical)
        state["replace_bwrap_during_use"] = False
        save()
    if state.get("rename_source_mount"):
        renamed = source_root.with_name(source_root.name + ".renamed")
        source_root.rename(renamed)
        source_root = renamed
        state["rename_source_mount"] = False
        save()
    if "metadata" in args:
        manifest = (source_root / "llm-guard-proxy" / "Cargo.toml").read_text()
        dependency_path = "/outside" if "path" in manifest else None
        dependencies = [] if dependency_path is None else [{"name": "outside", "path": dependency_path}]
        package_id = "path+file:///src/llm-guard-proxy#0.0.0"
        (target_root / ".rustc_info.json").write_text(
            '{"rustc_fingerprint":"fixture-1.90.0"}\n'
        )
        print(json.dumps({
            "packages": [{
                "id": package_id,
                "name": "llm-guard-proxy",
                "version": "0.0.0",
                "source": None,
                "manifest_path": "/src/llm-guard-proxy/Cargo.toml",
                "dependencies": dependencies,
                "targets": [{"src_path": "/src/llm-guard-proxy/src/main.rs"}],
            }],
            "workspace_members": [package_id],
            "workspace_root": "/src",
            "target_directory": "/target",
            "resolve": {"nodes": [{"id": package_id, "dependencies": []}]},
            "version": 1,
        }, sort_keys=True))
    elif "build" in args:
        source_file = source_root / "llm-guard-proxy" / "src" / "main.rs"
        if state.get("mutate_source_during_cargo"):
            original = source_file.read_bytes()
            mode = source_file.stat().st_mode & 0o777
            source_file.chmod(0o600)
            source_file.write_bytes(b"transient dirty source\n")
            source_file.write_bytes(original)
            source_file.chmod(mode)
        selected = "/usr/bin/false" if "alternate" in source_file.read_text() else os.environ["FIXTURE_BUILD_SOURCE"]
        target = target_root / "x86_64-unknown-linux-gnu" / "release" / "llm-guard-proxy"
        target.parent.mkdir(parents=True, exist_ok=True)
        artifact_mode = state.get("artifact_mode", "")
        if artifact_mode == "symlink":
            target.symlink_to(selected)
        else:
            shutil.copyfile(selected, target)
            target.chmod(0o755)
            if artifact_mode == "hardlink":
                os.link(target, target.with_name("llm-guard-proxy-hardlink"))
            elif artifact_mode == "group-writable":
                target.chmod(0o775)
            elif artifact_mode == "world-writable":
                target.chmod(0o777)
            elif artifact_mode == "empty":
                target.write_bytes(b"")
            elif artifact_mode == "oversize":
                with target.open("r+b") as stream:
                    stream.truncate(128 * 1024 * 1024 + 1)
        save()
    else:
        raise SystemExit(95)
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
        template = dispatcher.read_text()
        destinations = {
            "cargo": self.toolchain_bin / "cargo",
            "rustc": self.toolchain_bin / "rustc",
            "bwrap": self.fake_bin / "bwrap",
            "systemctl": self.fake_bin / "systemctl",
            "curl": self.fake_bin / "curl",
        }
        for name, destination in destinations.items():
            destination.write_text(template.replace("__TOOL_NAME__", name))
            destination.chmod(0o755)
        dispatcher.unlink()

    def _write_authority_config(self) -> None:
        tool_paths = {
            "cargo": self.toolchain_bin / "cargo",
            "rustc": self.toolchain_bin / "rustc",
            "bwrap": self.fake_bin / "bwrap",
            "systemctl": self.fake_bin / "systemctl",
            "curl": self.fake_bin / "curl",
            "git": Path("/usr/bin/git"),
            "git_remote_https": Path("/usr/lib/git-core/git-remote-http"),
            "readelf": Path("/usr/bin/x86_64-linux-gnu-readelf"),
            "nice": Path("/usr/bin/nice"),
            "ionice": Path("/usr/bin/ionice"),
            "cc": Path("/usr/bin/x86_64-linux-gnu-gcc-12"),
            "ld": Path("/usr/bin/x86_64-linux-gnu-ld.bfd"),
            "ar": Path("/usr/bin/x86_64-linux-gnu-ar"),
        }
        tools = {}
        for name, path in tool_paths.items():
            info = path.stat()
            tools[name] = {
                "logical": str(path),
                "resolved": str(path.resolve(strict=True)),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mode": stat.S_IMODE(info.st_mode),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        test_env = {
            "BWRAP_LOGICAL_PATH": str(self.fake_bin / "bwrap"),
            "EXPECTED_CANDIDATE": str(self.candidate),
            "FIXTURE_BUILD_SOURCE": str(self.build_source),
            "GUARD_TEST_STATE": str(self.state_path),
            "GUARD_TEST_TOOL_LOG": str(self.tool_log),
            "LLM_GUARD_PROXY_REBUILD_GUARD_UNIT": str(self.guard_unit),
            "LLM_GUARD_PROXY_REBUILD_PROC_ROOT": str(self.proc_root),
            "SAME_HASH_TARGET": str(self.same_hash_other_inode),
            "SERVICE_BIN": str(self.service_bin),
            "SOURCE_DIR": str(self.source_dir),
            "WRONG_HASH_TARGET": str(self.wrong_hash),
        }
        payload = {
            "schema": 1,
            "registry_cache": str(self.registry_cache),
            "registry_index": str(self.registry_index),
            "test_env": test_env,
            "toolchain_root": str(self.toolchain_root),
            "tools": tools,
        }
        self.authority_config.write_text(json.dumps(payload, sort_keys=True))
        self.authority_config.chmod(0o600)

    def run(
        self,
        *,
        test_only: bool = True,
        extra_env: dict[str, str] | None = None,
        timeout: int = 30,
        cwd: Path = ROOT,
    ) -> subprocess.CompletedProcess[str]:
        self._write_authority_config()
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
            cwd=cwd,
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
