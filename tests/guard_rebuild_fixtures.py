from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Self

ROOT = Path(__file__).resolve().parents[1]
REBUILD_SCRIPT = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.sh"

__all__ = ["RebuildFixture"]


class RebuildFixture:
    cargo_identity = (
        "cargo 1.90.0 (fixture)\nrelease: 1.90.0\nhost: x86_64-unknown-linux-gnu\n"
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
        self.gcc_root = self.root / "gcc-root"
        self.sysroot_lib = self.root / "sysroot-lib"
        self.sysroot_include = self.root / "sysroot-include"
        self.authority_config = self.root / "authority.json"
        self.remote = self.root / "remote"
        self.source_dir = self.root / "source"
        self.cache_root = self.root / "cache"
        self.service_bin = self.root / "service" / "llm-guard-proxy"
        self.guard_config = self.root / "guard" / "config.toml"
        self.guard_unit = self.root / "systemd" / "llm-guard-proxy.service"
        self.proc_root = self.root / "proc"
        self.receipt_dir = self.root / "receipts"
        self.crash_marker = self.root / "crash.marker"
        self.descendant_pids = self.root / "descendants.pid"
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
            self.gcc_root,
            self.sysroot_lib,
            self.sysroot_include,
            self.remote,
            self.service_bin.parent,
            self.guard_config.parent,
            self.guard_unit.parent,
            self.proc_root / "sys" / "kernel" / "random",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self._init_remote()
        (self.gcc_root / "cc1").write_text("sealed cc1\n")
        (self.sysroot_lib / "crt1.o").write_text("sealed crt\n")
        (self.sysroot_include / "stddef.h").write_text("sealed include\n")
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
            "health_passed": False,
            "candidate_restart_mode": "exact",
            "drift_after": "",
            "drift_kind": "",
            "drifted": False,
            "publication_job_checks": 0,
            "mutate_source_during_cargo": False,
            "replace_bwrap_during_use": False,
            "rename_source_mount": False,
            "artifact_mode": "",
            "candidate_path_swap": False,
            "build_finished": False,
            "jobs": {},
            "next_job_id": 41,
            "job_behavior": "normal",
            "job_polls_remaining": 1,
            "restart_noop_after": 0,
            "hang_restart_calls": [],
            "manager_contract_mismatch": False,
            "applied_config_override": "",
            "mutate_gcc_closure_during_build": False,
            "held_ld_consumed": False,
            "ambient_usr_consumed": False,
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
            "GUARD_TEST_DESCENDANT_PIDS": str(self.descendant_pids),
        }
        self.env["LLM_GUARD_REBUILD_TEST_CONFIG"] = str(self.authority_config)
        self._write_authority_config()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self._cleanup_recorded_descendants()
        finally:
            self.temp.cleanup()

    def _cleanup_recorded_descendants(self) -> None:
        if not self.descendant_pids.exists():
            return
        root = str(self.root).encode()
        pids = {
            int(line)
            for line in self.descendant_pids.read_text().splitlines()
            if line.isdigit()
        }
        owned: set[int] = set()
        for pid in pids:
            try:
                command = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            if root not in command:
                continue
            owned.add(pid)
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 2
        while owned and time.monotonic() < deadline:
            owned = {pid for pid in owned if Path(f"/proc/{pid}").exists()}
            if owned:
                time.sleep(0.01)

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
            '[workspace]\nmembers = ["llm-guard-proxy"]\nresolver = "2"\n'
        )
        (self.remote / "Cargo.lock").write_text(
            'version = 4\n\n[[package]]\nname = "llm-guard-proxy"\nversion = "0.0.0"\n'
        )
        crate = self.remote / "llm-guard-proxy"
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            '[package]\nname = "llm-guard-proxy"\nversion = "0.0.0"\n'
            'edition = "2021"\n[features]\nguard = []\n'
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
        (pid_dir / "cmdline").write_bytes(
            str(self.service_bin).encode()
            + b"\0--config\0/run/credentials/llm-guard-proxy.service/llm-guard-config\0"
            + b"--guardian-runtime-dir\0/run/user/1001/gb10-memory-guardian\0"
        )
        credential = (
            pid_dir / "root/run/credentials/llm-guard-proxy.service/llm-guard-config"
        )
        credential.parent.mkdir(parents=True, exist_ok=True)
        credential.unlink(missing_ok=True)
        override = str(self.state.get("applied_config_override", ""))
        credential.write_bytes(
            override.encode() if override else self.guard_config.read_bytes()
        )
        credential.chmod(0o400)

    @staticmethod
    def _python_runtime_authority_sha256() -> str:
        path = Path("/usr/bin/python3.11")
        info = path.stat()
        authority = {
            "logical_path": "/usr/bin/python3",
            "resolved_path": "/usr/bin/python3.11",
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "nlink": info.st_nlink,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode),
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        return hashlib.sha256(
            json.dumps(
                authority, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("ascii")
        ).hexdigest()

    def set_state(self, **updates: object) -> None:
        self.reload_state()
        self.state.update(updates)
        self.save_state()
        self._write_proc()

    def ensure_candidate(self) -> None:
        self.candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.build_source, self.candidate)
        self.candidate.chmod(0o755)

    @staticmethod
    def _build_id(path: Path) -> str:
        output = subprocess.run(
            ["/usr/bin/x86_64-linux-gnu-readelf", "-n", "--", str(path)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        matches = re.findall(r"(?m)^\s*Build ID:\s*([0-9A-Fa-f]+)\s*$", output)
        if len(matches) != 1:
            raise AssertionError(f"fixture ELF build ID unavailable: {path}")
        return matches[0].lower()

    @classmethod
    def _identity(cls, path: Path) -> dict[str, object]:
        info = path.stat()
        return {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "build_id": cls._build_id(path),
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "nlink": info.st_nlink,
        }

    def use_prior_bytes_for_candidate(self) -> None:
        self.build_source = self.prior
        self.binary_sha256 = hashlib.sha256(self.prior.read_bytes()).hexdigest()
        self.candidate = (
            self.cache_root
            / "releases"
            / f"{self.source_commit}-{self.binary_sha256}"
            / "llm-guard-proxy"
        )
        self.env["FIXTURE_BUILD_SOURCE"] = str(self.prior)
        self.env["EXPECTED_CANDIDATE"] = str(self.candidate)

    def write_wal(self, phase: str, *, mutate: bool = False) -> Path:
        """Create one future-schema WAL for actual-script recovery RED controls."""

        state_root = self.receipt_dir
        transaction = state_root / "transaction.v1"
        rollback = state_root / "rollback"
        receipts = state_root / "receipts"
        for directory in (state_root, transaction, rollback, receipts):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        self.ensure_candidate()
        prior_identity = self._identity(self.prior)
        backup = rollback / f"{prior_identity['sha256']}.bin"
        shutil.copyfile(self.prior, backup)
        backup.chmod(0o700)
        tool_authorities: dict[str, dict[str, object]] = {}
        config = json.loads(self.authority_config.read_text())
        for name, spec in config["tools"].items():
            info = Path(spec["logical"]).stat(follow_symlinks=False)
            tool_authorities[name] = {
                "logical_path": spec["logical"],
                "resolved_path": spec["resolved"],
                "expected_uid": spec["uid"],
                "expected_gid": spec["gid"],
                "expected_mode": spec["mode"],
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": info.st_size,
                "nlink": info.st_nlink,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
                "sha256": hashlib.sha256(
                    Path(spec["logical"]).read_bytes()
                ).hexdigest(),
            }
        build_inputs = {
            "toolchain": {},
            "registry_cache": {},
            "registry_index": {},
            "gcc_closure": {},
            "sysroot_lib": {},
            "sysroot_include": {},
            "metadata_target": {},
        }
        payload = {
            "schema": 1,
            "phase": phase,
            "txid": "a" * 32,
            "mode": "test-only",
            "service_bin": str(self.service_bin),
            "guard_config": str(self.guard_config),
            "guard_unit": str(self.guard_unit),
            "proc_root": str(self.proc_root),
            "snapshot_root": str(self.cache_root / ".rebuild-input-stale"),
            "candidate": {
                "path": str(self.candidate),
                "identity": self._identity(self.candidate),
            },
            "prior": {
                "link_target": str(self.prior),
                "boot_id": "12345678-1234-4abc-8def-1234567890ab",
                "generation": {
                    "pid": 4242,
                    "invocation": "1" * 32,
                    "started": 1000,
                    "proc_start": 2000,
                    "fragment": str(self.guard_unit),
                    "result": "success",
                    "job": None,
                },
                "running_link": str(self.prior),
                "executable": prior_identity,
                "backup": {
                    "path": str(backup),
                    "identity": self._identity(backup),
                },
            },
            "authorities": {
                "canonical_source_url": str(self.remote),
                "canonical_source_ref": "refs/heads/main",
                "source_commit": self.source_commit,
                "source_tree": self.source_tree,
                "source_archive_sha256": "3" * 64,
                "snapshot_content_sha256": "4" * 64,
                "source_file_count": 1,
                "source_byte_count": 1,
                "git_config_sha256": "5" * 64,
                "metadata_closure_sha256": "6" * 64,
                "sandbox_contract_sha256": "7" * 64,
                "build_inputs": build_inputs,
                "build_inputs_sha256": hashlib.sha256(
                    json.dumps(
                        build_inputs, sort_keys=True, separators=(",", ":")
                    ).encode("ascii")
                ).hexdigest(),
                "cargo_identity_sha256": "9" * 64,
                "rustc_identity_sha256": "a" * 64,
                "guard_config_sha256": hashlib.sha256(
                    self.guard_config.read_bytes()
                ).hexdigest(),
                "guard_unit_sha256": hashlib.sha256(
                    self.guard_unit.read_bytes()
                ).hexdigest(),
                "tool_authorities": tool_authorities,
                "python_runtime_authority_sha256": self._python_runtime_authority_sha256(),
            },
            "manager_job_ids": [],
            "committed": None,
            "error": None,
        }
        state_path = transaction / "state.json"
        state_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        )
        state_path.chmod(0o600)
        if mutate:
            self.service_bin.unlink(missing_ok=True)
            self.service_bin.symlink_to(self.candidate)
            self.set_state(
                pid=4243,
                invocation="2" * 32,
                systemd_start=1100,
                proc_start=2100,
                running_target=str(self.candidate),
            )
        return state_path

    def hold_rebuild_lock(self) -> tuple[int, Path]:
        self.receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.receipt_dir.chmod(0o700)
        lock = self.receipt_dir / "lock.v1"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor, lock

    def set_prior_absent_with_candidate_runtime(self) -> None:
        self.ensure_candidate()
        self.service_bin.unlink()
        self.set_state(running_target=str(self.candidate))

    def _write_dispatcher(self) -> None:
        dispatcher = self.fake_bin / "fixture-tool"
        dispatcher.write_text(
            textwrap.dedent(
                r"""#!/usr/bin/python3
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

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
    (pid_dir / "cmdline").write_bytes(
        os.environ["SERVICE_BIN"].encode()
        + b"\0--config\0/run/credentials/llm-guard-proxy.service/llm-guard-config\0"
        + b"--guardian-runtime-dir\0/run/user/1001/gb10-memory-guardian\0"
    )
    credential = pid_dir / "root/run/credentials/llm-guard-proxy.service/llm-guard-config"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.unlink(missing_ok=True)
    override = state.get("applied_config_override", "")
    credential.write_bytes(
        override.encode() if override else Path(
            os.environ["LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG"]
        ).read_bytes()
    )
    credential.chmod(0o400)

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

def drift():
    kind = state.get("drift_kind")
    if kind == "invocation":
        state["invocation"] = "e" * 32
    elif kind == "pid":
        state["pid"] += 1
        state["proc_start"] += 1
        write_proc(state["running_target"])
    elif kind == "starttime":
        state["systemd_start"] += 1
    elif kind == "pid-reuse":
        state["proc_start"] += 1
        write_proc(state["running_target"])
    else:
        raise SystemExit(96)
    state["drifted"] = True
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
    for index, argument in enumerate(args):
        if argument != "--ro-bind" or index + 2 >= len(args):
            continue
        source, destination = args[index + 1:index + 3]
        if source == "/usr" and destination == "/usr":
            state["ambient_usr_consumed"] = True
        if destination == "/usr/bin/ld":
            state["held_ld_consumed"] = os.readlink(source).endswith(
                "x86_64-linux-gnu-ld.bfd"
            )
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
        if state.get("mutate_gcc_closure_during_build"):
            closure = Path(os.environ["GCC_ROOT"]) / "cc1"
            original = closure.read_bytes()
            closure.write_bytes(b"substituted helper\n")
            closure.write_bytes(original)
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
        state["build_finished"] = True
        save()
    else:
        raise SystemExit(95)
elif name == "systemctl":
    joined = " ".join(args)
    if (
        state.get("candidate_path_swap")
        and state.get("build_finished")
        and not state.get("candidate_path_swapped")
    ):
        candidate = Path(os.environ["EXPECTED_CANDIDATE"])
        replacement = candidate.with_name(candidate.name + ".replacement")
        shutil.copyfile(candidate, replacement)
        replacement.chmod(0o755)
        os.replace(replacement, candidate)
        state["candidate_path_swapped"] = True
        save()
    if joined == "--user is-active --quiet llm-guard-proxy.service":
        raise SystemExit(0 if state["active"] else 3)
    if joined == "--user show -p MainPID --value llm-guard-proxy.service":
        print(state["pid"] if state["active"] else 0)
    elif " show " in f" {joined} " or joined.startswith("--user show "):
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
            "ActiveEnterTimestampMonotonic": str(
                state["systemd_start"] if state["active"] else 0
            ),
            "Result": "success" if state["active"] else "exit-code",
            "Job": next(iter(state.get("jobs", {})), ""),
            "ExecStart": (
                "{ path=" + os.environ["SERVICE_BIN"]
                + " ; argv[]=" + os.environ["SERVICE_BIN"]
                + " --config /run/credentials/llm-guard-proxy.service/llm-guard-config"
                + " --guardian-runtime-dir /run/user/1001/gb10-memory-guardian ; ignore_errors=no ; }"
            ),
            "LoadCredential": (
                "llm-guard-config:" + os.environ["LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG"]
            ),
            "NoNewPrivileges": "yes",
            "PrivateTmp": "yes",
            "ProtectSystem": "strict",
            "ProtectHome": "read-only",
            "UMask": "0077",
            "Environment": "",
            "EnvironmentFiles": "",
        }
        if state.get("manager_contract_mismatch"):
            values["ProtectSystem"] = "no"
        requested = [
            argument.split("=", 1)[1]
            for argument in args
            if argument.startswith("--property=")
        ]
        for key in requested:
            print(f"{key}={values[key]}")
        if (
            state.get("drift_after") == "candidate-attestation"
            and state.get("health_passed")
            and not state.get("drifted")
        ):
            drift()
    elif joined == "--user list-jobs --output=json":
        jobs = state.get("jobs", {})
        output = [
            {
                "job": int(job_id),
                "unit": "llm-guard-proxy.service",
                "type": "restart",
                "state": "running",
            }
            for job_id in sorted(jobs, key=int)
        ]
        print(json.dumps(output, separators=(",", ":")))
        if jobs and state.get("job_behavior") == "normal":
            remaining = int(state.get("job_polls_remaining", 1)) - 1
            state["job_polls_remaining"] = remaining
            if remaining <= 0:
                job_id, target = next(iter(jobs.items()))
                noop_after = int(state.get("restart_noop_after", 0))
                if not noop_after or state["restart_calls"] <= noop_after:
                    activate(target)
                state["jobs"].pop(job_id, None)
                state["job_polls_remaining"] = 1
        if state.get("health_passed"):
            state["publication_job_checks"] += 1
        save()
        expected_check = {
            "first-publication-bracket": 1,
            "second-publication-bracket": 2,
        }.get(state.get("drift_after"))
        if (
            expected_check == state.get("publication_job_checks")
            and not state.get("drifted")
        ):
            drift()
    elif joined.startswith("--user cancel "):
        job_id = args[-1]
        if state.get("job_behavior") != "cancel-still-present":
            state.get("jobs", {}).pop(job_id, None)
        save()
    elif joined in {
        "--user restart llm-guard-proxy.service",
        "--user restart --no-block --job-mode=fail -- llm-guard-proxy.service",
    }:
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
        if state["restart_calls"] in state.get("hang_restart_calls", []):
            pid_path = Path(os.environ["GUARD_TEST_DESCENDANT_PIDS"])
            with pid_path.open("a") as stream:
                stream.write(str(os.getpid()) + "\n")
            code = (
                "import os,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "open(sys.argv[1], 'a').write(str(os.getpid())+'\\n'); "
                "chunk=b'x'*65536; "
                "[(os.write(1,chunk),os.write(2,chunk)) for _ in iter(int,1)]"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(pid_path)],
                start_new_session=True,
            )
            with pid_path.open("a") as stream:
                stream.write(str(child.pid) + "\n")
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                os.write(1, b"p" * 65536)
                os.write(2, b"e" * 65536)
                time.sleep(0.001)
        if "--no-block" in args:
            job_id = str(state["next_job_id"])
            state["next_job_id"] += 1
            state.setdefault("jobs", {})[job_id] = str(target)
            save()
        elif (
            int(state.get("restart_noop_after", 0))
            and state["restart_calls"] > int(state["restart_noop_after"])
        ):
            pass
        else:
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
    state["health_passed"] = True
    save()
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
"""
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
            "as": Path("/usr/bin/x86_64-linux-gnu-as"),
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
            "GUARD_TEST_DESCENDANT_PIDS": str(self.descendant_pids),
            "GCC_ROOT": str(self.gcc_root),
            "LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG": str(self.guard_config),
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
            "gcc_root": str(self.gcc_root),
            "sysroot_lib": str(self.sysroot_lib),
            "sysroot_include": str(self.sysroot_include),
            "test_env": test_env,
            "toolchain_root": str(self.toolchain_root),
            "tools": tools,
        }
        self.authority_config.write_text(json.dumps(payload, sort_keys=True))
        self.authority_config.chmod(0o600)

    def _run_arguments(
        self,
        test_only: bool,
        extra_env: dict[str, str] | None,
    ) -> tuple[list[str], dict[str, str]]:
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
        return command, env

    def popen(
        self,
        *,
        test_only: bool = True,
        extra_env: dict[str, str] | None = None,
        cwd: Path = ROOT,
    ) -> subprocess.Popen[str]:
        command, env = self._run_arguments(test_only, extra_env)
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def run(
        self,
        *,
        test_only: bool = True,
        extra_env: dict[str, str] | None = None,
        timeout: int = 30,
        cwd: Path = ROOT,
    ) -> subprocess.CompletedProcess[str]:
        command, env = self._run_arguments(test_only, extra_env)
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

    def assert_call_order(self, test: unittest.TestCase, *needles: str) -> None:
        calls = self.calls()
        cursor = 0
        for needle in needles:
            cursor = calls.find(needle, cursor)
            test.assertNotEqual(cursor, -1, calls)
            cursor += len(needle)

    def receipt_paths(self) -> list[Path]:
        return sorted((self.receipt_dir / "receipts").glob("*/state.json"))

    def assert_transaction_clean(self, test: unittest.TestCase) -> None:
        test.assertFalse((self.receipt_dir / "transaction.v1").exists())
        rollback = self.receipt_dir / "rollback"
        if rollback.exists():
            test.assertEqual(list(rollback.iterdir()), [])

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
