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
        "cargo 1.96.0 (fixture)\nrelease: 1.96.0\nhost: aarch64-unknown-linux-gnu\n"
    )
    rustc_identity = (
        "rustc 1.96.0 (fixture)\n"
        "binary: rustc\n"
        "commit-hash: fixture\n"
        "host: aarch64-unknown-linux-gnu\n"
        "release: 1.96.0\n"
        "LLVM version: fixture\n"
    )

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "bin"
        self.toolchain_root = self.root / "toolchain"
        self.toolchain_bin = self.toolchain_root / "bin"
        self.target_rustlib = (
            self.toolchain_root / "lib/rustlib/aarch64-unknown-linux-gnu"
        )
        self.cargo_home = self.root / "cargo-home"
        self.registry_cache = self.cargo_home / "registry" / "cache" / "fixture"
        self.registry_index = self.cargo_home / "registry" / "index" / "fixture"
        self.gcc_root = self.root / "gcc-root"
        self.sysroot_lib = self.root / "sysroot-lib"
        self.sysroot_include = self.root / "sysroot-include"
        self.python_stdlib = self.root / "python-stdlib"
        self.cgroup_root = self.root / "cgroup"
        self.authority_config = self.root / "authority.json"
        self.bwrap_authority_config = self.root / "bwrap-authority.json"
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
        self.build_source = self.root / "aarch64-elf-fixture"
        self.alternate_build_source = self.root / "alternate-aarch64-elf-fixture"

        for directory in (
            self.home,
            self.fake_bin,
            self.toolchain_bin,
            self.target_rustlib,
            self.registry_cache,
            self.registry_index,
            self.gcc_root,
            self.sysroot_lib,
            self.sysroot_include,
            self.python_stdlib,
            self.cgroup_root,
            self.remote,
            self.service_bin.parent,
            self.guard_config.parent,
            self.guard_unit.parent,
            self.proc_root / "sys" / "kernel" / "random",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        for source, destination in (
            (Path("/usr/bin/true"), self.build_source),
            (Path("/usr/bin/false"), self.alternate_build_source),
        ):
            payload = bytearray(source.read_bytes())
            payload[18:20] = (183).to_bytes(2, "little")
            old_interpreter = b"/lib64/ld-linux-x86-64.so.2"
            new_interpreter = b"/lib/ld-linux-aarch64.so.1"
            if payload.count(old_interpreter) == 1:
                offset = payload.index(old_interpreter)
                payload[offset : offset + len(old_interpreter)] = new_interpreter + b"\0"
            elif payload.count(new_interpreter) != 1:
                raise AssertionError("host ELF interpreter fixture differs")
            destination.write_bytes(payload)
            destination.chmod(0o755)

        self._init_remote()
        (self.gcc_root / "cc1").write_text("sealed cc1\n")
        (self.target_rustlib / "libstd.rlib").write_text("sealed target rustlib\n")
        (self.sysroot_lib / "crt1.o").write_text("sealed crt\n")
        (self.sysroot_include / "stddef.h").write_text("sealed include\n")
        (self.python_stdlib / "os.py").write_text("# sealed test stdlib\n")
        self.source_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self.source_tree = self._git("rev-parse", "HEAD^{tree}").stdout.strip()
        self.binary_sha256 = hashlib.sha256(self.build_source.read_bytes()).hexdigest()
        self.candidate = (
            self.cache_root
            / "releases"
            / f"{self.source_commit}-{self.binary_sha256}"
            / "llm-guard-proxy"
        )

        shutil.copyfile(self.alternate_build_source, self.prior)
        shutil.copyfile(self.alternate_build_source, self.wrong_hash)
        shutil.copyfile(self.build_source, self.same_hash_other_inode)
        for binary in (self.prior, self.wrong_hash, self.same_hash_other_inode):
            binary.chmod(0o755)
        self.service_bin.symlink_to(self.prior)
        self.guard_config.write_text('private_config_payload = "fixture-only"\n')
        self.guard_config.chmod(0o644)
        self.guard_unit.write_text("[Service]\n# private-unit-payload\n")
        self.guard_unit.chmod(0o644)
        (self.proc_root / "sys" / "kernel" / "random" / "boot_id").write_text(
            "12345678-1234-4abc-8def-1234567890ab\n"
        )
        (self.proc_root / "meminfo").write_text(
            "MemTotal:       134217728 kB\nMemAvailable:   67108864 kB\n"
        )
        self.test_free_bytes = 64 * 1024 * 1024 * 1024
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
            "build_source_override": "",
            "candidate_path_swap": False,
            "build_finished": False,
            "jobs": {},
            "manager_invocation": "f" * 32,
            "manager_started": 500,
            "next_job_id": 41,
            "job_behavior": "normal",
            "job_type": "restart",
            "job_unit": "llm-guard-proxy.service",
            "job_state": "running",
            "job_polls_remaining": 1,
            "systemctl_call_limit": 0,
            "systemctl_calls": 0,
            "restart_noop_after": 0,
            "hang_restart_calls": [],
            "manager_contract_mismatch": False,
            "omit_environment_files": False,
            "omit_generation_field": "",
            "applied_config_override": "",
            "mutate_gcc_closure_during_build": False,
            "held_ld_consumed": False,
            "ambient_usr_consumed": False,
            "cargo_host": "aarch64-unknown-linux-gnu",
            "rustc_host": "aarch64-unknown-linux-gnu",
            "rustc_release": "1.96.0",
            "candidate_elf_output_mode": "",
            "scope_failure": "",
            "scope_status_mode": "canonical",
            "scope_frame_mode": "",
            "scope_resource_event": "",
            "scope_pre_go_resource_event": "",
            "scope_pre_go_identity_drift": "",
            "scope_runtime_identity_drift": "",
            "scope_identity_drifted": False,
            "scope_reuse_after_collect": False,
            "scope_reuse_on_kill_entry": False,
            "scope_foreign_signalled": False,
            "scope_cleanup_failure": "",
            "scope_collection_requested": False,
            "scope_moved_worker_pidfd_reaped": False,
            "scope_pidfd_reaped_after_cgroup_failure": False,
            "scope_build_payload": "",
            "scope_build_exit": 0,
            "scope_build_hang": False,
            "scope_build_oom": False,
            "scope_build_stderr": "",
            "real_cargo_artifact_authority": False,
            "scope_collect_immediate": True,
            "scope_resource_snapshot_fenced": False,
            "scope_worker_live_after_collect": False,
            "scope_unit": "",
            "scope_worker": 0,
            "scope_worker_starttime": 0,
            "scope_worker_reap_witness": None,
            "scope_worker_pre_admission_reaped": False,
            "scope_registration": {},
            "scope_props": {},
        }
        self.save_state()
        self._write_proc()
        self._write_dispatcher()

        self.env = {
            "CACHE_ROOT": str(self.cache_root),
            "FIXTURE_BUILD_SOURCE": str(self.build_source),
            "FIXTURE_ALTERNATE_BUILD_SOURCE": str(self.alternate_build_source),
            "GUARD_TEST_CGROUP_ROOT": str(self.cgroup_root),
            "GUARD_TEST_BWRAP_AUTHORITY": str(self.bwrap_authority_config),
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
            ["/usr/bin/readelf", "-n", "--", str(path)],
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
        snapshot = self.cache_root / (".rebuild-input-" + "a" * 32)
        snapshot.mkdir(mode=0o700)
        snapshot.chmod(0o700)
        snapshot_info = snapshot.stat()
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
        directory_authorities = {
            name: {
                "path": str(self.root / f"directory-{name}"),
                "device": 1,
                "inode": index,
                "mode": 0o755,
                "mtime_ns": 1,
                "ctime_ns": 1,
                "content_sha256": "b" * 64,
                "metadata_sha256": "c" * 64,
                "file_count": 1,
                "byte_count": 1,
            }
            for index, name in enumerate(
                (
                    "canonical_source",
                    "cargo_home",
                    "gcc_closure",
                    "linker_tools",
                    "registry_cache",
                    "registry_index",
                    "sysroot_include",
                    "sysroot_runtime",
                    "target_rustlib",
                    "toolchain",
                ),
                start=1,
            )
        }
        tool_sha256 = {
            name: authority["sha256"]
            for name, authority in tool_authorities.items()
        }
        build_inputs = {
            "source": str(self.build_source),
            "target": str(self.cache_root / "target"),
            "cargo_argv": [],
            "rustc_exec": tool_sha256["rustc"],
            "tool_sha256": tool_sha256,
            "directory_authorities": directory_authorities,
            "write_contract": {
                "limits": {
                    "cargo_target_bytes": 512 * 1024 * 1024,
                    "git_object_bytes": 64 * 1024 * 1024,
                    "host_write_bytes": 576 * 1024 * 1024,
                },
                "cargo_target_bytes": 512 * 1024 * 1024,
                "git_object_bytes": 64 * 1024 * 1024,
            },
        }
        direct_build_contract = {
            "schema": 1,
            "output": {"candidate_bytes": 128 * 1024 * 1024},
            "host": {
                "free_floor_bytes": 8 * 1024 * 1024 * 1024,
                "write_budget_bytes": 576 * 1024 * 1024,
            },
            "scope": {
                phase: {
                    "cpu_percent": 200,
                    "fsize_bytes": 128 * 1024 * 1024,
                    "memory_high": 8 * 1024 * 1024 * 1024,
                    "memory_max": 10 * 1024 * 1024 * 1024,
                    "min_mem_available": 16 * 1024 * 1024 * 1024,
                    "phase": phase,
                    "runtime_seconds": runtime,
                    "tasks_max": 768,
                }
                for phase, runtime in (("metadata", 300), ("build", 1800))
            },
            "direct_build": {
                "target": "aarch64-unknown-linux-gnu",
                "cargo": "held-file-fd",
                "rustc": "held-file-fd",
                "manifest": "held-source-dirfd",
                "directory_authorities": [
                    "canonical_source",
                    "cargo_home",
                    "gcc_closure",
                    "linker_tools",
                    "registry_cache",
                    "registry_index",
                    "sysroot_include",
                    "sysroot_runtime",
                    "target_rustlib",
                    "toolchain",
                ],
                "write_limits": {
                    "cargo_target_bytes": 512 * 1024 * 1024,
                    "git_object_bytes": 64 * 1024 * 1024,
                },
            },
            "tool_sha256": {
                name: tool_sha256[name] for name in sorted(tool_sha256)
            },
        }
        direct_build_contract_sha256 = hashlib.sha256(
            json.dumps(
                direct_build_contract, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        candidate_identity = self._identity(self.candidate)
        committed = None
        if phase == "committed":
            committed = {
                "generation": {
                    "pid": 4243,
                    "invocation": "2" * 32,
                    "started": 1100,
                    "proc_start": 2100,
                    "fragment": str(self.guard_unit),
                    "result": "success",
                    "job": None,
                },
                "boot_id": "12345678-1234-4abc-8def-1234567890ab",
                "running_link": str(self.candidate),
                "executable": candidate_identity,
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
            "snapshot_root": str(snapshot),
            "snapshot_identity": {
                "device": snapshot_info.st_dev,
                "inode": snapshot_info.st_ino,
            },
            "candidate": {
                "path": str(self.candidate),
                "identity": candidate_identity,
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
                "direct_build_contract_sha256": direct_build_contract_sha256,
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
                "target_triple": "aarch64-unknown-linux-gnu",
                "candidate_elf_machine": "AArch64",
                "candidate_elf_interpreter": "/lib/ld-linux-aarch64.so.1",
            },
            "manager_generation": {
                "invocation": self.state["manager_invocation"],
                "started": self.state["manager_started"],
            },
            "manager_job_ids": [],
            "restart_intent": False,
            "committed": committed,
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
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time

name = "__TOOL_NAME__"
args = sys.argv[1:]
state_path = Path(os.environ["GUARD_TEST_STATE"])
log_path = Path(os.environ["GUARD_TEST_TOOL_LOG"])
state = json.loads(state_path.read_text())
authority = json.loads(Path(os.environ["GUARD_TEST_BWRAP_AUTHORITY"]).read_text())

SCOPE_UNIT = re.compile(
    r"llm-guard-rebuild-(metadata|build)-(?:[0-9]+-[0-9]+|[0-9a-f]{32})\.scope"
)

def valid_systemd_run(values):
    if (
        len(values) >= 19
        and values[:3] == ["--user", "--scope", "--quiet"]
        and values[3].startswith("--unit=")
        and "--" in values[4:]
    ):
        match = SCOPE_UNIT.fullmatch(values[3].split("=", 1)[1])
        separator = values.index("--", 4)
        if match is None:
            return False
        limits = {
            "metadata": (8589934592, 10737418240, 768, 200, 300, 134217728),
            "build": (8589934592, 10737418240, 768, 200, 1800, 134217728),
        }[match.group(1)]
        properties = values[4:separator]
        expected = [
            f"--property=MemoryHigh={limits[0]}",
            f"--property=MemoryMax={limits[1]}",
            "--property=MemorySwapMax=0",
            f"--property=TasksMax={limits[2]}",
            f"--property=CPUQuota={limits[3]}%",
            "--property=CPUQuotaPeriodSec=100ms",
            "--property=KillMode=control-group",
            "--property=SendSIGKILL=yes",
            "--property=OOMPolicy=kill",
            f"--property=RuntimeMaxSec={limits[4]}",
            f"--property=LimitFSIZE={limits[5]}",
        ]
        payload = values[separator + 1:]
        return (
            properties == expected
            and len(payload) >= 10
            and payload[1].startswith("/proc/self/fd/")
            and payload[2] == "--direct-scope-child"
            and payload[6] == "--"
            and re.fullmatch(r"/proc/self/fd/[1-9][0-9]*", payload[7]) is not None
        )
    if (
        len(values) < 8
        or values[:4] != ["--user", "--scope", "--quiet", "--collect"]
        or not values[4].startswith("--unit=")
        or "--" not in values[5:]
    ):
        return False
    match = SCOPE_UNIT.fullmatch(values[4].split("=", 1)[1])
    if match is None:
        return False
    separator = values.index("--", 5)
    if separator != 5 + 2 * 11:
        return False
    properties = [
        values[index + 1]
        for index in range(5, separator, 2)
        if values[index] == "--property"
    ]
    limits = {
        "metadata": (8589934592, 10737418240, 768, 200, 300, 134217728),
        "build": (8589934592, 10737418240, 768, 200, 1800, 134217728),
    }[match.group(1)]
    expected = [
        f"MemoryHigh={limits[0]}",
        f"MemoryMax={limits[1]}",
        "MemorySwapMax=0",
        f"TasksMax={limits[2]}",
        f"CPUQuota={limits[3]}%",
        "CPUQuotaPeriodSec=100ms",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "OOMPolicy=kill",
        f"RuntimeMaxSec={limits[4]}",
        f"LimitFSIZE={limits[5]}",
    ]
    return values[5:separator:2] == ["--property"] * 11 and properties == expected

def valid_systemctl(values):
    manager = [
        "--user", "show", "--property=InvocationID",
        "--property=UserspaceTimestampMonotonic",
    ]
    fields = [
        "LoadState", "ActiveState", "SubState", "FragmentPath", "DropInPaths",
        "MainPID", "InvocationID", "ActiveEnterTimestampMonotonic", "Result",
        "Job", "ExecStart", "LoadCredential", "NoNewPrivileges", "PrivateTmp",
        "ProtectSystem", "ProtectHome", "UMask", "Environment",
    ]
    service = [
        "--user", "show", "llm-guard-proxy.service", "--no-pager",
        *("--property=" + field for field in fields),
    ]
    legacy_service = [*service, "--property=EnvironmentFiles"]
    candidate = tuple(values)
    if candidate in {
        tuple(manager),
        tuple(service),
        tuple(legacy_service),
        ("--user", "list-jobs", "--output=json"),
        (
            "--user", "restart", "--no-block", "--job-mode=fail", "--",
            "llm-guard-proxy.service",
        ),
    }:
        return True
    if len(values) == 3 and values[:2] == ["--user", "cancel"]:
        return values[2].isdigit()
    if (
        len(values) == 5
        and values[:4] == ["--user", "show", "--property=ControlGroup", "--value"]
    ):
        return SCOPE_UNIT.fullmatch(values[4]) is not None
    if (
        len(values) == 5
        and values[:4] == ["--user", "show", "--property=LoadState", "--value"]
    ):
        return SCOPE_UNIT.fullmatch(values[4]) is not None
    if len(values) == 3 and values[:2] == ["--user", "reset-failed"]:
        return SCOPE_UNIT.fullmatch(values[2]) is not None
    return (
        len(values) == 5
        and values[:4] == [
            "--user", "kill", "--kill-whom=all", "--signal=SIGTERM"
        ]
        and SCOPE_UNIT.fullmatch(values[4]) is not None
    )

def digest_fd(descriptor):
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)

def valid_source_tree(descriptor, spec):
    root = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(root.st_mode)
        or root.st_uid != spec["uid"]
        or stat.S_IMODE(root.st_mode) != spec["mode"]
        or root.st_nlink not in {1, 2}
    ):
        return False
    expected = spec["entries"]
    expected_directories = {
        str(parent)
        for name in expected
        for parent in Path(name).parents
        if str(parent) != "."
    }
    seen = set()
    seen_directories = set()

    def visit(directory_fd, prefix):
        for entry in sorted(os.scandir(directory_fd), key=lambda item: item.name):
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if (
                    relative not in expected_directories
                    or info.st_uid != spec["uid"]
                    or stat.S_IMODE(info.st_mode) != spec["directory_mode"]
                    or info.st_nlink not in {1, 2}
                ):
                    return False
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    held = os.fstat(child)
                    if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino):
                        return False
                    seen_directories.add(relative)
                    if not visit(child, relative):
                        return False
                finally:
                    os.close(child)
                continue
            row = expected.get(relative)
            if row is None or not stat.S_ISREG(info.st_mode):
                return False
            child = os.open(
                entry.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                before = os.fstat(child)
                if (
                    before.st_uid != spec["uid"]
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != row["mode"]
                    or before.st_size != row["size"]
                    or digest_fd(child) != row["sha256"]
                ):
                    return False
                after = os.fstat(child)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    return False
            finally:
                os.close(child)
            seen.add(relative)
        return True

    return visit(descriptor, "") and seen == set(expected) and seen_directories == expected_directories

def valid_fd_authority(descriptor, spec):
    before = os.fstat(descriptor)
    if (
        before.st_dev != spec.get("device", before.st_dev)
        or before.st_ino != spec.get("inode", before.st_ino)
        or before.st_uid != spec["uid"]
        or before.st_nlink != spec.get("nlink", before.st_nlink)
        or stat.S_IMODE(before.st_mode) != spec["mode"]
    ):
        return False
    if spec["kind"] == "directory":
        return stat.S_ISDIR(before.st_mode)
    if spec["kind"] == "source-tree":
        return valid_source_tree(descriptor, spec)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size != spec["size"]
        or digest_fd(descriptor) != spec["sha256"]
    ):
        return False
    after = os.fstat(descriptor)
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )

def valid_bind_authorities(binds):
    specs = authority["bwrap_authorities"]
    seen_destinations = set()
    seen_descriptors = set()
    seen_objects = set()
    for source, destination in binds:
        spec = specs.get(destination)
        if spec is None or destination in seen_destinations:
            return False
        seen_destinations.add(destination)
        if spec["kind"] == "path-directory":
            try:
                info = os.stat(source, follow_symlinks=False)
            except OSError:
                return False
            if (
                source != spec["path"]
                or not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
                != (spec["device"], spec["inode"], spec["uid"], spec["mode"])
            ):
                return False
            continue
        match = re.fullmatch(r"/proc/self/fd/([1-9][0-9]*)", source)
        if match is None:
            return False
        descriptor = int(match.group(1))
        try:
            info = os.fstat(descriptor)
        except OSError:
            return False
        identity = (info.st_dev, info.st_ino)
        if descriptor in seen_descriptors or identity in seen_objects:
            return False
        seen_descriptors.add(descriptor)
        seen_objects.add(identity)
        if not valid_fd_authority(descriptor, spec):
            return False
    return True

def valid_bwrap(values):
    if (
        len(values) < 12
        or values[0] != "--json-status-fd"
        or re.fullmatch(r"[1-9][0-9]*", values[1]) is None
        or values[2] != "--block-fd"
        or re.fullmatch(r"[1-9][0-9]*", values[3]) is None
        or values[1] == values[3]
    ):
        return False
    try:
        separator = len(values) - 1 - values[::-1].index("--")
    except ValueError:
        return False
    if separator < 6 or values[separator - 2:separator] != ["--chdir", "/"]:
        return False
    payload = values[separator + 1:]
    if len(payload) != 8 or payload[:5] != [
        "/tools/python", "-I", "-B", "-S", "/worker.py"
    ]:
        return False
    phase, raw_config, unit = payload[5:]
    match = SCOPE_UNIT.fullmatch(unit)
    if phase not in {"fetch", "metadata", "build"} or match is None or match.group(1) != phase:
        return False
    try:
        config = json.loads(raw_config)
    except (TypeError, ValueError):
        return False
    if raw_config != json.dumps(
        config, sort_keys=True, separators=(",", ":"), allow_nan=False
    ):
        return False
    expected_config = (
        {"policy", "source_protocol", "source_ref", "source_repo"}
        if phase == "fetch" else {"policy"}
    )
    policy = config.get("policy") if isinstance(config, dict) else None
    policies = {
        "fetch": {
            "cpu_percent": 100,
            "fsize_bytes": 167772160,
            "memory_high": 805306368,
            "memory_max": 1073741824,
            "min_mem_available": 7516192768,
            "phase": "fetch",
            "runtime_seconds": 300,
            "tasks_max": 32,
        },
        "metadata": {
            "cpu_percent": 200,
            "fsize_bytes": 134217728,
            "memory_high": 8589934592,
            "memory_max": 10737418240,
            "min_mem_available": 17179869184,
            "phase": "metadata",
            "runtime_seconds": 300,
            "tasks_max": 768,
        },
        "build": {
            "cpu_percent": 200,
            "fsize_bytes": 134217728,
            "memory_high": 8589934592,
            "memory_max": 10737418240,
            "min_mem_available": 17179869184,
            "phase": "build",
            "runtime_seconds": 1800,
            "tasks_max": 768,
        },
    }
    if (
        not isinstance(config, dict)
        or set(config) != expected_config
        or policy != policies[phase]
        or (
            phase == "fetch"
            and (
                config["source_protocol"] != "file"
                or config["source_ref"] != "refs/heads/main"
                or config["source_repo"] != os.environ["SOURCE_REPO"]
            )
        )
    ):
        return False
    arities = {
        "--unshare-user": 0, "--unshare-all": 0, "--disable-userns": 0,
        "--die-with-parent": 0, "--new-session": 0, "--share-net": 0,
        "--clearenv": 0, "--cap-drop": 1, "--proc": 1, "--dev": 1,
        "--dir": 1, "--tmpfs": 1, "--size": 1, "--ro-bind": 2,
        "--symlink": 2, "--setenv": 2,
    }
    parsed = []
    options = values[4:separator - 2]
    index = 0
    while index < len(options):
        option = options[index]
        if option not in arities or index + arities[option] >= len(options):
            return False
        arguments = options[index + 1:index + 1 + arities[option]]
        parsed.append((option, arguments))
        index += 1 + arities[option]
    common_names = [
        "--unshare-user", "--unshare-all", "--disable-userns", "--die-with-parent",
        "--new-session", "--cap-drop", "--proc", "--dev",
        *(["--dir"] * 9), *(["--ro-bind"] * 5), "--symlink",
        "--tmpfs", "--clearenv", *(["--setenv"] * 4),
    ]
    extension_names = (
        ["--share-net", *(["--dir"] * 3), *(["--ro-bind"] * 6),
         "--size", "--tmpfs", "--size", "--tmpfs"]
        if phase == "fetch"
        else [*(["--dir"] * 2), *(["--ro-bind"] * 11), *(["--dir"] * 2),
              *(["--ro-bind"] * 2), "--size", "--tmpfs", "--size", "--tmpfs"]
    )
    if [option for option, _ in parsed] != common_names + extension_names:
        return False
    directories = [arguments[0] for option, arguments in parsed if option == "--dir"]
    common_directories = [
        "/usr", "/usr/bin", "/usr/lib", "/usr/lib/python3.12",
        "/usr/lib/aarch64-linux-gnu", "/sys", "/sys/fs", "/sys/fs/cgroup", "/tools",
    ]
    phase_directories = (
        ["/etc", "/etc/ssl", "/etc/ssl/certs"]
        if phase == "fetch"
        else ["/usr/lib/gcc", "/usr/lib/gcc/aarch64-linux-gnu", "/cargo-home", "/cargo-home/registry"]
    )
    binds = [arguments for option, arguments in parsed if option == "--ro-bind"]
    destinations = [arguments[1] for arguments in binds]
    sources = [arguments[0] for arguments in binds]
    common_destinations = [
        "/usr/lib/aarch64-linux-gnu", "/usr/lib/python3.12", "/tools/python",
        "/worker.py", "/sys/fs/cgroup",
    ]
    phase_destinations = (
        [
            "/etc/ssl/certs/ca-certificates.crt", "/etc/resolv.conf",
            "/etc/nsswitch.conf", "/etc/hosts", "/tools/git",
            "/tools/git-remote-https",
        ]
        if phase == "fetch"
        else [
            "/usr/lib/gcc/aarch64-linux-gnu/13", "/usr/include",
            "/usr/bin/aarch64-linux-gnu-gcc-13", "/usr/bin/aarch64-linux-gnu-as",
            "/usr/bin/aarch64-linux-gnu-ld.bfd", "/usr/bin/aarch64-linux-gnu-ar",
            "/src", "/toolchain", "/toolchain/lib/rustlib/aarch64-unknown-linux-gnu",
            "/toolchain/bin/cargo", "/toolchain/bin/rustc",
            "/cargo-home/registry/cache", "/cargo-home/registry/index",
        ]
    )
    fd_source = re.compile(r"/proc/self/fd/[1-9][0-9]*").fullmatch
    return (
        directories == common_directories + phase_directories
        and destinations == common_destinations + phase_destinations
        and all(fd_source(source) is not None for source in sources[:4])
        and sources[4] == os.environ["GUARD_TEST_CGROUP_ROOT"]
        and all(fd_source(source) is not None for source in sources[5:])
        and valid_bind_authorities(binds)
        and [arguments for option, arguments in parsed if option == "--cap-drop"]
        == [["ALL"]]
        and [arguments for option, arguments in parsed if option == "--proc"]
        == [["/proc"]]
        and [arguments for option, arguments in parsed if option == "--dev"]
        == [["/dev"]]
        and [arguments for option, arguments in parsed if option == "--symlink"]
        == [["usr/lib/aarch64-linux-gnu", "/lib"]]
        and [arguments for option, arguments in parsed if option == "--tmpfs"]
        == ([ ["/home"], ["/fetch"], ["/tmp"] ] if phase == "fetch"
            else [ ["/home"], ["/target"], ["/tmp"] ])
        and [arguments for option, arguments in parsed if option == "--setenv"] == [
            ["GB10_RESOURCE_FENCE", "1"],
            ["LANG", "C"],
            ["LC_ALL", "C"],
            ["PATH", "/tools"],
        ]
        and [arguments for option, arguments in parsed if option == "--size"]
        == ([ ["402653184"], ["67108864"] ] if phase == "fetch"
            else [ ["6442450944"], ["536870912"] ])
    )

valid = {
    "systemd_run": valid_systemd_run,
    "systemctl": valid_systemctl,
    "bwrap": valid_bwrap,
}.get(name)
if valid is not None and not valid(args):
    with log_path.open("a") as log:
        log.write("rejected " + name + " " + " ".join(args) + "\n")
    print("fixture grammar rejected " + name + ": " + repr(args), file=sys.stderr)
    raise SystemExit(93)

with log_path.open("a") as log:
    log.write(name + " " + " ".join(args) + "\n")

def publish_state(payload):
    temporary = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True))
    os.replace(temporary, state_path)

if name == "systemctl" and state.get("systemctl_call_limit", 0) and state.get("jobs"):
    state["systemctl_calls"] = int(state.get("systemctl_calls", 0)) + 1
    publish_state(state)
    if state["systemctl_calls"] > state["systemctl_call_limit"]:
        raise SystemExit(21)

def save():
    publish_state(state)

def canonical_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")

def artifact_frame(header, payload):
    encoded = canonical_json(header)
    return b"GB10ART1" + struct.pack(">I", len(encoded)) + encoded + payload

def scope_paths(unit):
    return Path(os.environ["GUARD_TEST_CGROUP_ROOT"]) / "fixture.slice" / unit

def clear_scope(unit):
    root = scope_paths(unit)
    if root.exists():
        (root / "cgroup.procs").write_text("")
        (root / "cgroup.events").write_text("populated 0\nfrozen 0\n")

def write_resource_event(unit):
    event = state.get("scope_resource_event", "")
    scope = scope_paths(unit)
    if event == "memory":
        (scope / "memory.events").write_text(
            "low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n"
        )
    elif event == "pids":
        (scope / "pids.events").write_text("max 1\n")
    return event

def resource_fence(unit):
    event = write_resource_event(unit)
    if os.write(0, b"R") != 1 or os.read(0, 2) != b"G":
        raise SystemExit(34)
    latest = json.loads(state_path.read_text())
    latest["scope_resource_snapshot_fenced"] = True
    publish_state(latest)
    return event

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

if name == "systemd_run":
    if state.get("scope_failure") in {"manager", "property"}:
        raise SystemExit(23)
    if "--direct-scope-child" in args:
        signal.signal(signal.SIGTERM, lambda *_: None)
        unit_arg = next((arg for arg in args if arg.startswith("--unit=")), "")
        separator = args.index("--")
        unit = unit_arg.split("=", 1)[1]
        props = {
            value.split("=", 1)[0]: value.split("=", 1)[1]
            for value in args[4:separator]
            if value.startswith("--property=")
            for value in (value.removeprefix("--property="),)
        }
        scope = scope_paths(unit)
        scope.mkdir(parents=True, exist_ok=True)
        worker = os.fork()
        if worker == 0:
            os.kill(os.getpid(), signal.SIGSTOP)
            os.execv(args[separator + 1], args[separator + 1:])
        waited, stopped = os.waitpid(worker, os.WUNTRACED)
        if waited != worker or not os.WIFSTOPPED(stopped):
            raise SystemExit(30)
        state["scope_unit"] = unit
        state["scope_worker"] = worker
        state["scope_worker_starttime"] = 9000 + worker
        state["scope_registration"] = {
            "cgroup_path": str(scope),
            "unit": unit,
            "worker_pid": worker,
        }
        save()
        proc = Path(os.environ["LLM_GUARD_PROXY_REBUILD_PROC_ROOT"]) / str(worker)
        proc.mkdir(parents=True, exist_ok=True)
        fields = ["S"] + ["0"] * 18 + [str(9000 + worker)]
        (proc / "stat").write_text(
            f"{worker} (direct cargo) " + " ".join(fields) + "\n"
        )
        cgroup_unit = unit if state.get("scope_failure") != "cgroup" else "foreign.scope"
        (proc / "cgroup").write_text(f"0::/fixture.slice/{cgroup_unit}\n")
        files = {
            "cgroup.procs": f"{worker}\n",
            "cgroup.events": "populated 1\nfrozen 0\n",
            "memory.high": str(props.get("MemoryHigh", "0")) + "\n",
            "memory.max": str(props.get("MemoryMax", "0")) + "\n",
            "memory.swap.max": str(props.get("MemorySwapMax", "0")) + "\n",
            "memory.oom.group": "1\n",
            "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
            "pids.max": str(props.get("TasksMax", "0")) + "\n",
            "pids.events": "max 0\n",
            "cpu.max": (
                "100000 100000\n"
                if props.get("CPUQuota") == "100%"
                else "200000 100000\n"
            ),
            "cgroup.kill": "",
        }
        if state.get("scope_failure") == "property-readback":
            files["memory.max"] = "1\n"
        if state.get("scope_failure") == "membership":
            files["cgroup.procs"] = "999999\n"
        if state.get("scope_failure") == "controller":
            files.pop("memory.swap.max")
        pre_go_event = state.get("scope_pre_go_resource_event", "")
        if pre_go_event == "memory":
            files["memory.events"] = "low 0\nhigh 0\nmax 1\noom 0\noom_kill 0\n"
        elif pre_go_event == "oom-kill":
            files["memory.events"] = "low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\n"
        elif pre_go_event == "pids":
            files["pids.events"] = "max 1\n"
        for filename, value in files.items():
            (scope / filename).write_text(value)
        os.kill(worker, signal.SIGCONT)
        if state.get("scope_failure") == "pidfd":
            wait_deadline = time.monotonic() + 2
            while time.monotonic() < wait_deadline:
                try:
                    if Path(f"/proc/{worker}/wchan").read_text().strip() == "pipe_read":
                        break
                except OSError:
                    pass
                time.sleep(0.01)
            os.kill(worker, signal.SIGKILL)
            _, child_status = os.waitpid(worker, 0)
            state["scope_worker_pre_admission_reaped"] = True
            save()
        else:
            child_status = 0
        admission_rejection = state.get("scope_failure") in {
            "membership", "pidfd", "property-readback"
        } or bool(state.get("scope_pre_go_resource_event"))
        kill_deadline = time.monotonic() + 4
        while True:
            if state.get("scope_failure") == "pidfd":
                if (
                    (scope / "cgroup.kill").read_text()
                    or time.monotonic() >= kill_deadline
                ):
                    break
                time.sleep(0.01)
                continue
            waited, child_status = os.waitpid(worker, os.WNOHANG)
            if waited == worker:
                while (
                    admission_rejection
                    and not (scope / "cgroup.kill").read_text()
                    and time.monotonic() < kill_deadline
                ):
                    time.sleep(0.01)
                break
            if (scope / "cgroup.kill").read_text():
                try:
                    os.kill(worker, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, child_status = os.waitpid(worker, 0)
                break
            time.sleep(0.01)
        if not admission_rejection or (scope / "cgroup.kill").read_text():
            clear_scope(unit)
            shutil.rmtree(proc, ignore_errors=True)
        latest = json.loads(state_path.read_text())
        latest["scope_worker_reap_witness"] = {
            "owner_pid": os.getpid(),
            "worker_pid": worker,
            "worker_starttime": 9000 + worker,
        }
        state.clear()
        state.update(latest)
        save()
        raise SystemExit(os.waitstatus_to_exitcode(child_status))
    unit_arg = next((arg for arg in args if arg.startswith("--unit=")), "")
    if not unit_arg:
        raise SystemExit(24)
    state["scope_unit"] = unit_arg.split("=", 1)[1]
    state["scope_worker"] = 0
    state["scope_worker_starttime"] = 0
    state["scope_worker_reap_witness"] = None
    state["scope_registration"] = {
        "cgroup_path": str(scope_paths(state["scope_unit"])),
        "unit": state["scope_unit"],
        "worker_pid": 0,
    }
    state["scope_props"] = {
        arg.split("=", 1)[0]: arg.split("=", 1)[1]
        for index, arg in enumerate(args)
        if arg == "--property" and index + 1 < len(args)
        for arg in (args[index + 1],)
        if "=" in arg
    }
    save()
    separator = args.index("--")
    os.execv(args[separator + 1], args[separator + 1:])
elif name == "prlimit":
    separator = args.index("--")
    os.execv(args[separator + 1], args[separator + 1:])
elif name == "cargo":
    if args == ["--version", "--verbose"]:
        print("cargo 1.96.0 (fixture)")
        print("release: 1.96.0")
        print("host: " + state["cargo_host"])
    elif args and args[0] == "metadata":
        manifest = Path(args[args.index("--manifest-path") + 1])
        source_root = str(manifest.resolve().parent)
        package_id = f"path+file://{source_root}#llm-guard-proxy@0.1.0"
        print(json.dumps({
            "packages": [{
                "id": package_id,
                "manifest_path": str(manifest.resolve()),
                "source": None,
                "dependencies": [],
            }],
            "workspace_members": [package_id],
            "resolve": {"nodes": [{"id": package_id}]},
            "workspace_root": source_root,
            "target_directory": os.environ["CARGO_TARGET_DIR"],
        }, sort_keys=True, separators=(",", ":")))
    elif args and args[0] == "build":
        manifest = Path(args[args.index("--manifest-path") + 1])
        linker_path = os.environ.get("PATH", "")
        linker_match = re.fullmatch(r"/proc/self/fd/([1-9][0-9]*)", linker_path)
        linker = Path(linker_path) / "ld"
        linker_spec = authority["bwrap_authorities"][
            "/usr/bin/aarch64-linux-gnu-ld.bfd"
        ]
        try:
            linker_info = linker.stat(follow_symlinks=False)
            state["held_ld_consumed"] = (
                linker_match is not None
                and stat.S_ISREG(linker_info.st_mode)
                and stat.S_IMODE(linker_info.st_mode) == 0o500
                and linker_info.st_nlink == 1
                and linker_info.st_size == linker_spec["size"]
                and hashlib.sha256(linker.read_bytes()).hexdigest()
                == linker_spec["sha256"]
            )
        except OSError:
            state["held_ld_consumed"] = False
        state["ambient_usr_consumed"] = linker_path in {"/usr/bin", "/bin"}
        save()
        if state.get("scope_build_oom"):
            unit = state["scope_unit"]
            (scope_paths(unit) / "memory.events").write_text(
                "low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n"
            )
            raise SystemExit(137)
        if state.get("scope_build_hang"):
            time.sleep(30)
        if state.get("scope_build_exit"):
            sys.stderr.write(state.get("scope_build_stderr", ""))
            raise SystemExit(state["scope_build_exit"])
        if state.get("mutate_source_during_cargo"):
            source = manifest
            original = source.read_bytes()
            mode = source.stat().st_mode & 0o777
            source.chmod(0o600)
            source.write_bytes(b"transient dirty source\n")
            source.write_bytes(original)
            source.chmod(mode)
        selected = Path(state.get("build_source_override") or os.environ["FIXTURE_BUILD_SOURCE"])
        source_file = Path(os.environ["SOURCE_REPO"]) / "llm-guard-proxy" / "src" / "main.rs"
        if "alternate" in source_file.read_text():
            selected = Path(os.environ["FIXTURE_ALTERNATE_BUILD_SOURCE"])
        target = Path(os.environ["CARGO_TARGET_DIR"]) / "aarch64-unknown-linux-gnu" / "release" / "llm-guard-proxy"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            b"not-an-elf\n"
            if state.get("scope_build_payload") == "invalid-elf"
            else selected.read_bytes()
        )
        if state.get("real_cargo_artifact_authority"):
            target.chmod(0o700)
            dependency = target.parent / "deps" / "llm_guard_proxy-fixture"
            dependency.parent.mkdir()
            os.link(target, dependency)
        else:
            target.chmod(0o755)
        state["build_finished"] = True
        save()
    else:
        raise SystemExit(91)
elif name == "rustc":
    if args != ["-vV"]:
        raise SystemExit(92)
    print("rustc " + state["rustc_release"] + " (fixture)")
    print("binary: rustc")
    print("commit-hash: fixture")
    print("host: " + state["rustc_host"])
    print("release: " + state["rustc_release"])
    print("LLVM version: fixture")
elif name == "readelf":
    path = args[-1]
    if args[:2] == ["-hW", "-lW"]:
        descriptor = int(path.rsplit("/", 1)[-1])
        result = subprocess.run(
            ["/usr/bin/readelf", *args],
            check=False,
            text=True,
            capture_output=True,
            pass_fds=(descriptor,),
        )
        if result.returncode:
            sys.stderr.write(result.stderr)
            raise SystemExit(result.returncode)
        output = result.stdout
        mode = state.get("candidate_elf_output_mode", "")
        machine = re.compile(r"(?m)^\s*Machine:.*$")
        interpreter = re.compile(r"(?m)^\s*\[Requesting program interpreter:.*$")
        if mode == "wrong-machine":
            output = machine.sub("  Machine: Advanced Micro Devices X86-64", output)
        elif mode == "missing-machine":
            output = machine.sub("", output)
        elif mode == "multiple-machine":
            output += "  Machine: AArch64\n"
        elif mode == "malformed-machine":
            output = machine.sub("  Machine AArch64", output)
        elif mode == "wrong-interpreter":
            output = interpreter.sub(
                "      [Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]",
                output,
            )
        elif mode == "missing-interpreter":
            output = interpreter.sub("", output)
        elif mode == "multiple-interpreter":
            output += "      [Requesting program interpreter: /lib/ld-linux-aarch64.so.1]\n"
        elif mode == "malformed-interpreter":
            output = interpreter.sub(
                "      [Requesting program interpreter /lib/ld-linux-aarch64.so.1]",
                output,
            )
        elif mode:
            raise SystemExit(93)
        sys.stdout.write(output)
    elif args[:2] == ["-n", "--"]:
        os.execv("/usr/bin/readelf", ["readelf", *args])
    else:
        raise SystemExit(92)
elif name == "bwrap":
    status_fd = int(args[args.index("--json-status-fd") + 1])
    block_fd = int(args[args.index("--block-fd") + 1])
    separator = len(args) - 1 - args[::-1].index("--")
    payload_args = args[separator + 1:]
    operation = payload_args[-3]
    config = json.loads(payload_args[-2])
    unit = payload_args[-1]
    props = state.get("scope_props", {})
    worker = os.fork()
    if worker == 0:
        os.close(status_fd)
        try:
            if not os.read(block_fd, 1):
                raise SystemExit(25)
            with log_path.open("a") as log:
                log.write("payload " + operation + "\n")
            if state.get("replace_bwrap_during_use") and operation == "fetch":
                logical = Path(os.environ["BWRAP_LOGICAL_PATH"])
                replacement = logical.with_name("bwrap.replacement")
                shutil.copyfile(logical, replacement)
                replacement.chmod(0o755)
                os.replace(replacement, logical)
            if state.get("scope_failure") == "payload":
                resource_fence(unit)
                raise SystemExit(26)
            if state.get("scope_failure") == "oom-kill":
                write_resource_event(unit)
                time.sleep(0.5)
                os._exit(137)
            if state.get("scope_failure") == "live-output":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                write_resource_event(unit)
                os.write(2, b"x" * 70000)
                time.sleep(30)
            identity_drift = state.get("scope_runtime_identity_drift", "")
            if identity_drift:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                proc = Path(os.environ["LLM_GUARD_PROXY_REBUILD_PROC_ROOT"]) / str(os.getpid())
                if identity_drift == "starttime":
                    fields = ["S"] + ["0"] * 18 + [str(19000 + os.getpid())]
                    (proc / "stat").write_text(
                        f"{os.getpid()} (scope worker) " + " ".join(fields) + "\n"
                    )
                elif identity_drift == "cgroup":
                    (proc / "cgroup").write_text("0::/fixture.slice/foreign.scope\n")
                    moved_scope = scope_paths(unit)
                    (moved_scope / "cgroup.procs").write_text("")
                    (moved_scope / "cgroup.events").write_text(
                        "populated 0\nfrozen 0\n"
                    )
                elif identity_drift == "loss":
                    shutil.rmtree(proc)
                else:
                    raise SystemExit(32)
                latest = json.loads(state_path.read_text())
                latest["scope_identity_drifted"] = True
                publish_state(latest)
                time.sleep(30)
            frame = b""
            if operation == "fetch":
                remote = Path(config["source_repo"])
                commit = subprocess.check_output([
                    "/usr/bin/git", "-C", str(remote), "rev-parse",
                    config["source_ref"] + "^{commit}",
                ]).decode().strip()
                tree = subprocess.check_output([
                    "/usr/bin/git", "-C", str(remote), "rev-parse", commit + "^{tree}",
                ]).decode().strip()
                tree_rows = subprocess.check_output([
                    "/usr/bin/git", "-C", str(remote), "ls-tree", "-lrz", "--full-tree", commit,
                ])
                entries = []
                total = 0
                for row in tree_rows.rstrip(b"\0").split(b"\0"):
                    meta, raw_path = row.split(b"\t", 1)
                    mode, kind, oid, size = meta.split()
                    if kind != b"blob":
                        raise SystemExit(27)
                    item = {
                        "mode": int(mode, 8),
                        "oid": oid.decode(),
                        "path": raw_path.decode(),
                        "size": int(size),
                    }
                    total += item["size"]
                    entries.append(item)
                archive = subprocess.check_output([
                    "/usr/bin/git", "-C", str(remote), "archive", "--format=tar", commit,
                ])
                frame = artifact_frame({
                    "archive_sha256": hashlib.sha256(archive).hexdigest(),
                    "byte_count": total,
                    "commit": commit,
                    "entries": entries,
                    "file_count": len(entries),
                    "git_config_sha256": "5" * 64,
                    "kind": "fetch",
                    "payload_size": len(archive),
                    "schema": 1,
                    "tree": tree,
                }, archive)
            elif operation == "metadata":
                package_id = "path+file:///src/llm-guard-proxy#0.0.0"
                dependencies = []
                if "outside={path=" in (
                    Path(os.environ["SOURCE_REPO"])
                    / "llm-guard-proxy"
                    / "Cargo.toml"
                ).read_text():
                    dependencies = [{"path": "/outside"}]
                metadata = canonical_json({
                    "packages": [{
                        "dependencies": dependencies,
                        "id": package_id,
                        "manifest_path": "/src/llm-guard-proxy/Cargo.toml",
                        "name": "llm-guard-proxy",
                        "source": None,
                        "targets": [{"src_path": "/src/llm-guard-proxy/src/main.rs"}],
                        "version": "0.0.0",
                    }],
                    "resolve": {"nodes": [{"dependencies": [], "id": package_id}]},
                    "target_directory": "/target",
                    "version": 1,
                    "workspace_members": [package_id],
                    "workspace_root": "/src",
                })
                frame = artifact_frame({
                    "kind": "metadata", "payload_size": len(metadata), "schema": 1,
                }, metadata)
            elif operation == "build":
                source_fd = args[args.index("/src") - 1]
                source_root = Path(os.readlink(source_fd))
                if state.get("rename_source_mount"):
                    source_root.rename(source_root.with_name(source_root.name + ".renamed"))
                if state.get("mutate_source_during_cargo"):
                    manifest = source_root / "Cargo.toml"
                    original = manifest.read_bytes()
                    mode = manifest.stat().st_mode & 0o777
                    manifest.chmod(0o600)
                    manifest.write_bytes(b"transient dirty source\n")
                    manifest.write_bytes(original)
                    manifest.chmod(mode)
                if state.get("mutate_gcc_closure_during_build"):
                    (Path(os.environ["GCC_ROOT"]) / "cc1").write_text("mutated cc1\n")
                source_file = Path(os.environ["SOURCE_REPO"]) / "llm-guard-proxy" / "src" / "main.rs"
                selected = Path(state.get("build_source_override") or os.environ["FIXTURE_BUILD_SOURCE"])
                if "alternate" in source_file.read_text():
                    selected = Path(os.environ["FIXTURE_ALTERNATE_BUILD_SOURCE"])
                artifact = (
                    b"not-an-elf\n"
                    if state.get("scope_build_payload") == "invalid-elf"
                    else selected.read_bytes()
                )
                artifact_mode = state.get("artifact_mode")
                if artifact_mode:
                    candidate = Path(os.environ["EXPECTED_CANDIDATE"])
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    if artifact_mode == "symlink":
                        candidate.symlink_to(selected)
                    elif artifact_mode == "hardlink":
                        sibling = candidate.with_name("raw-hardlink")
                        shutil.copyfile(selected, sibling)
                        sibling.chmod(0o755)
                        os.link(sibling, candidate)
                    elif artifact_mode in {"group-writable", "world-writable"}:
                        shutil.copyfile(selected, candidate)
                        candidate.chmod(
                            0o775 if artifact_mode == "group-writable" else 0o777
                        )
                    elif artifact_mode == "empty":
                        candidate.touch()
                        candidate.chmod(0o755)
                    elif artifact_mode == "oversize":
                        with candidate.open("wb") as stream:
                            stream.truncate(128 * 1024 * 1024 + 1)
                        candidate.chmod(0o755)
                    else:
                        raise SystemExit(28)
                frame = artifact_frame({
                    "kind": "build",
                    "payload_sha256": hashlib.sha256(artifact).hexdigest(),
                    "payload_size": len(artifact),
                    "schema": 1,
                }, artifact)
                latest = json.loads(state_path.read_text())
                ld_source = args[args.index("/usr/bin/aarch64-linux-gnu-ld.bfd") - 1]
                latest["held_ld_consumed"] = (
                    ld_source.startswith("/proc/self/fd/")
                    and os.path.samefile(
                        ld_source, "/usr/bin/x86_64-linux-gnu-ld.bfd"
                    )
                )
                latest["ambient_usr_consumed"] = any(
                    args[index:index + 3] == ["--ro-bind", "/usr", "/usr"]
                    for index in range(len(args) - 2)
                )
                latest["build_finished"] = True
                state.clear()
                state.update(latest)
                save()
            else:
                raise SystemExit(29)
            mode = state.get("scope_frame_mode", "")
            if mode == "oversized":
                frame = artifact_frame({
                    "kind": operation,
                    "payload_size": 128 * 1024 * 1024 + 1,
                    "schema": 1,
                }, b"")
            elif mode == "partial":
                frame = frame[:7]
            elif mode == "trailing":
                frame += b"trailing"
            elif mode == "malformed":
                frame = b"not-a-frame"
            view = memoryview(frame)
            while view:
                view = view[os.write(1, view):]
            event = resource_fence(unit)
            if state.get("scope_failure") == "post-fence-live":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                time.sleep(30)
        finally:
            if not state.get("scope_worker_live_after_collect"):
                shutil.rmtree(
                    Path(os.environ["LLM_GUARD_PROXY_REBUILD_PROC_ROOT"]) / str(os.getpid()),
                    ignore_errors=True,
                )
            if state.get("scope_cleanup_failure") != "timeout":
                clear_scope(unit)
        raise SystemExit(42 if event else 0)
    # Model systemd-run surviving group TERM long enough to collect the worker.
    signal.signal(signal.SIGTERM, lambda *_: None)
    registration = state.get("scope_registration")
    if (
        not isinstance(registration, dict)
        or registration.get("unit") != unit
        or registration.get("cgroup_path") != str(scope_paths(unit))
    ):
        os.kill(worker, signal.SIGKILL)
        os.waitpid(worker, 0)
        raise SystemExit(30)
    registration["worker_pid"] = worker
    state["scope_registration"] = registration
    state["scope_worker"] = worker
    state["scope_worker_starttime"] = 9000 + worker
    save()
    scope = scope_paths(unit)
    scope.mkdir(parents=True, exist_ok=True)
    proc = Path(os.environ["LLM_GUARD_PROXY_REBUILD_PROC_ROOT"]) / str(worker)
    proc.mkdir(parents=True, exist_ok=True)
    fields = ["S"] + ["0"] * 18 + [str(9000 + worker)]
    stat_row = f"{worker} (scope worker) " + " ".join(fields) + "\n"
    if state.get("scope_pre_go_identity_drift") == "starttime":
        os.mkfifo(proc / "stat", 0o600)
        stat_writer = os.fork()
        if stat_writer == 0:
            stat_path = proc / "stat"
            for index in range(3):
                starttime = 9000 + worker if index < 2 else 19000 + worker
                row = ["S"] + ["0"] * 18 + [str(starttime)]
                payload = (
                    f"{worker} (scope worker) " + " ".join(row) + "\n"
                ).encode()
                replacement = stat_path.with_name(f".stat.next.{index}")
                try:
                    if index < 2:
                        os.mkfifo(replacement, 0o600)
                    else:
                        replacement.write_bytes(payload)
                    descriptor = os.open(stat_path, os.O_WRONLY)
                    os.write(descriptor, payload)
                    os.replace(replacement, stat_path)
                    os.close(descriptor)
                except OSError:
                    replacement.unlink(missing_ok=True)
                    raise SystemExit(0)
            raise SystemExit(0)
    else:
        (proc / "stat").write_text(stat_row)
    cgroup_unit = unit if state.get("scope_failure") != "cgroup" else "foreign.scope"
    (proc / "cgroup").write_text(f"0::/fixture.slice/{cgroup_unit}\n")
    files = {
        "cgroup.procs": f"{worker}\n",
        "cgroup.events": "populated 1\nfrozen 0\n",
        "memory.high": str(props.get("MemoryHigh", "0")) + "\n",
        "memory.max": str(props.get("MemoryMax", "0")) + "\n",
        "memory.swap.max": str(props.get("MemorySwapMax", "0")) + "\n",
        "memory.oom.group": "1\n",
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
        "pids.max": str(props.get("TasksMax", "0")) + "\n",
        "pids.events": "max 0\n",
        "cpu.max": (
            "100000 100000\n"
            if props.get("CPUQuota") == "100%"
            else "200000 100000\n"
        ),
    }
    cleanup_failure = state.get("scope_cleanup_failure", "")
    if cleanup_failure != "nonzero":
        files["cgroup.kill"] = ""
    pre_go_event = state.get("scope_pre_go_resource_event", "")
    if pre_go_event == "memory":
        files["memory.events"] = "low 0\nhigh 0\nmax 1\noom 0\noom_kill 0\n"
    elif pre_go_event == "oom-kill":
        files["memory.events"] = "low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\n"
    elif pre_go_event == "pids":
        files["pids.events"] = "max 1\n"
    if state.get("scope_failure") == "property-readback":
        files["memory.max"] = "1\n"
    if state.get("scope_failure") == "controller":
        files.pop("memory.swap.max")
    for filename, value in files.items():
        (scope / filename).write_text(value)
    kill_reader = -1
    if cleanup_failure == "nonzero":
        os.mkfifo(scope / "cgroup.kill", 0o600)
        kill_reader = os.open(scope / "cgroup.kill", os.O_RDONLY | os.O_NONBLOCK)
    reported = os.getpid() if state.get("scope_failure") == "worker-wrapper" else worker
    ready_row = {
        "child-pid": reported,
        "mnt-namespace": 4_026_533_281,
    }
    namespace_options = {
        "--unshare-cgroup": "cgroup-namespace",
        "--unshare-ipc": "ipc-namespace",
        "--unshare-net": "net-namespace",
        "--unshare-pid": "pid-namespace",
        "--unshare-uts": "uts-namespace",
    }
    if "--unshare-all" in args:
        for offset, key in enumerate(namespace_options.values(), start=2):
            ready_row[key] = 4_026_533_280 + offset
    for offset, (option, key) in enumerate(namespace_options.items(), start=2):
        if option in args:
            ready_row[key] = 4_026_533_280 + offset
    if "--share-net" in args:
        ready_row.pop("net-namespace", None)
    ready = canonical_json(ready_row)
    status_mode = state.get("scope_status_mode", "canonical")
    if state.get("scope_failure") == "status":
        status_payload = b"{}\n"
    elif status_mode in {
        "canonical",
        "exit-array",
        "exit-boolean",
        "exit-duplicate-key",
        "exit-unknown-member",
    }:
        status_payload = ready + b"\n"
    elif status_mode == "array":
        status_payload = b'[["child-pid",' + str(reported).encode() + b']]\n'
    elif status_mode == "duplicate-key":
        pid = str(reported).encode()
        status_payload = b'{"child-pid":' + pid + b',"child-pid":' + pid + b'}\n'
    elif status_mode == "duplicate-record":
        status_payload = ready + b"\n" + ready + b"\n"
    elif status_mode == "unknown-member":
        invalid_ready = dict(ready_row)
        invalid_ready["unexpected"] = 0
        status_payload = canonical_json(invalid_ready) + b"\n"
    elif status_mode == "unknown-record":
        status_payload = ready + b"\n" + canonical_json({"unexpected": 0}) + b"\n"
    elif status_mode in {"boolean", "zero", "negative", "oversized"}:
        invalid_ready = dict(ready_row)
        invalid_ready["child-pid"] = {
            "boolean": True,
            "zero": 0,
            "negative": -1,
            "oversized": 1 << 22,
        }[status_mode]
        status_payload = canonical_json(invalid_ready) + b"\n"
    elif status_mode == "trailing-bytes":
        status_payload = ready + b"\ntrailing"
    else:
        raise SystemExit(31)
    os.write(status_fd, status_payload)
    os.close(block_fd)
    if kill_reader >= 0:
        reader_deadline = time.monotonic() + 2
        while time.monotonic() < reader_deadline:
            try:
                observed = os.read(kill_reader, 1)
            except BlockingIOError:
                break
            if observed:
                raise SystemExit(33)
            time.sleep(0.01)
        os.close(kill_reader)
    while True:
        waited, status = os.waitpid(worker, os.WNOHANG)
        if waited == worker:
            break
        if cleanup_failure != "nonzero" and (scope / "cgroup.kill").read_text():
            if state.get("scope_runtime_identity_drift") == "cgroup":
                time.sleep(0.01)
                continue
            try:
                os.kill(worker, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(worker, 0)
            break
        time.sleep(0.01)
    exit_code = os.waitstatus_to_exitcode(status)
    latest = json.loads(state_path.read_text())
    latest["scope_worker_reap_witness"] = {
        "owner_pid": os.getpid(),
        "worker_pid": worker,
        "worker_starttime": 9000 + worker,
    }
    publish_state(latest)
    if cleanup_failure != "timeout":
        clear_scope(unit)
    if not state.get("scope_worker_live_after_collect"):
        shutil.rmtree(
            Path(os.environ["LLM_GUARD_PROXY_REBUILD_PROC_ROOT"]) / str(worker),
            ignore_errors=True,
        )
    exit_payload = canonical_json({"exit-code": exit_code}) + b"\n"
    if status_mode == "exit-array":
        exit_payload = b'[["exit-code",0]]\n'
    elif status_mode == "exit-duplicate-key":
        exit_payload = b'{"exit-code":0,"exit-code":0}\n'
    elif status_mode == "exit-unknown-member":
        exit_payload = canonical_json({"exit-code": 0, "unexpected": 0}) + b"\n"
    elif status_mode == "exit-boolean":
        exit_payload = canonical_json({"exit-code": True}) + b"\n"
    os.write(status_fd, exit_payload)
    if state.get("scope_collect_immediate") and cleanup_failure != "timeout":
        for child in list(scope.iterdir()):
            child.unlink()
        scope.rmdir()
    latest = json.loads(state_path.read_text())
    if state.get("scope_runtime_identity_drift") == "cgroup":
        latest["scope_moved_worker_pidfd_reaped"] = True
    if cleanup_failure == "nonzero":
        latest["scope_pidfd_reaped_after_cgroup_failure"] = True
    registration = latest.get("scope_registration")
    if (
        isinstance(registration, dict)
        and registration.get("unit") == unit
        and registration.get("worker_pid") == worker
        and registration.get("cgroup_path") == str(scope)
    ):
        latest["scope_registration"] = {}
        latest["scope_unit"] = ""
        latest["scope_worker"] = 0
        latest["scope_worker_starttime"] = 0
        publish_state(latest)
    if state.get("scope_reuse_after_collect"):
        scope.mkdir(parents=True, exist_ok=True)
        (scope / "cgroup.procs").write_text("999999\n")
        (scope / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        (scope / "foreign.sentinel").write_text("foreign-generation\n")
        latest = json.loads(state_path.read_text())
        latest["scope_registration"] = {
            "cgroup_path": str(scope),
            "unit": unit,
            "worker_pid": 999999,
        }
        latest["scope_unit"] = unit
        latest["scope_worker"] = 999999
        publish_state(latest)
    raise SystemExit(exit_code)
elif name == "systemctl":
    joined = " ".join(args)
    if (
        len(args) == 5
        and args[:4] == ["--user", "show", "--property=LoadState", "--value"]
    ):
        unit = args[4]
        registration = state.get("scope_registration")
        scope = scope_paths(unit)
        loaded = (
            isinstance(registration, dict)
            and registration.get("unit") == unit
            and registration.get("cgroup_path") == str(scope)
            and scope.is_dir()
        )
        print("failed" if loaded else "not-found")
        raise SystemExit(0)
    if len(args) == 3 and args[:2] == ["--user", "reset-failed"]:
        unit = args[2]
        registration = state.get("scope_registration")
        scope = scope_paths(unit)
        if (
            not isinstance(registration, dict)
            or registration.get("unit") != unit
            or registration.get("cgroup_path") != str(scope)
            or not scope.is_dir()
        ):
            raise SystemExit(93)
        if "populated 1" in (scope / "cgroup.events").read_text().splitlines():
            raise SystemExit(94)
        clear_scope(unit)
        shutil.rmtree(scope)
        state["scope_collection_requested"] = True
        state["scope_registration"] = {}
        state["scope_unit"] = ""
        state["scope_worker"] = 0
        save()
        raise SystemExit(0)
    if (
        len(args) == 5
        and args[:4] == ["--user", "show", "--property=ControlGroup", "--value"]
    ):
        unit = args[4]
        registration = state.get("scope_registration")
        scope = scope_paths(unit)
        if (
            not isinstance(registration, dict)
            or registration.get("unit") != unit
            or registration.get("cgroup_path") != str(scope)
            or not scope.is_dir()
        ):
            raise SystemExit(93)
        if state.get("scope_failure") == "pidfd":
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if json.loads(state_path.read_text()).get("scope_worker_pre_admission_reaped"):
                    break
                time.sleep(0.01)
            else:
                raise SystemExit(95)
        print("/fixture.slice/" + unit)
        raise SystemExit(0)
    if args[:2] == ["--user", "kill"]:
        valid_signals = {
            "--signal=SIGTERM": signal.SIGTERM,
            "--signal=SIGKILL": signal.SIGKILL,
        }
        if (
            len(args) != 5
            or args[:3] != ["--user", "kill", "--kill-whom=all"]
            or args[3] not in valid_signals
        ):
            print("unexpected systemctl args: " + joined, file=sys.stderr)
            raise SystemExit(93)
        unit = args[4]
        if state.get("scope_reuse_on_kill_entry"):
            scope = scope_paths(unit)
            state["scope_registration"] = {
                "cgroup_path": str(scope),
                "unit": unit,
                "worker_pid": 999999,
            }
            state["scope_unit"] = unit
            state["scope_worker"] = 999999
            state["scope_foreign_signalled"] = True
            save()
        registration = state.get("scope_registration")
        scope = scope_paths(unit)
        worker = int(state.get("scope_worker", 0))
        if (
            not isinstance(registration, dict)
            or registration.get("unit") != unit
            or registration.get("worker_pid") != worker
            or registration.get("cgroup_path") != str(scope)
            or worker <= 1
            or state.get("scope_unit") != unit
            or not scope.is_dir()
        ):
            print("unowned fixture scope: " + unit, file=sys.stderr)
            raise SystemExit(93)
        if worker:
            try:
                os.kill(worker, valid_signals[args[3]])
            except ProcessLookupError:
                pass
        clear_scope(unit)
        shutil.rmtree(scope, ignore_errors=True)
        if worker:
            shutil.rmtree(
                Path(os.environ["LLM_GUARD_PROXY_REBUILD_PROC_ROOT"]) / str(worker),
                ignore_errors=True,
            )
        state["scope_registration"] = {}
        state["scope_unit"] = ""
        state["scope_worker"] = 0
        save()
        raise SystemExit(0)
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
    elif joined == "--user show --property=InvocationID --property=UserspaceTimestampMonotonic":
        print(f"InvocationID={state['manager_invocation']}")
        print(f"UserspaceTimestampMonotonic={state['manager_started']}")
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
            "Job": (
                next(iter(state.get("jobs", {})), "")
                if state.get("job_unit") == "llm-guard-proxy.service"
                else ""
            ),
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
            if key == "EnvironmentFiles" and state.get("omit_environment_files"):
                continue
            if key == state.get("omit_generation_field"):
                continue
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
                "unit": state.get("job_unit", "llm-guard-proxy.service"),
                "type": state.get("job_type", "restart"),
                "state": state.get("job_state", "running"),
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
        elif jobs and state.get("job_behavior") == "waiting-to-running":
            if state.get("job_state") == "waiting":
                state["job_state"] = "running"
            else:
                job_id, target = next(iter(jobs.items()))
                activate(target)
                state["jobs"].pop(job_id, None)
                state["job_state"] = "waiting"
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
        if state.get("job_behavior") == "cancel-owned-then-normal":
            state["job_behavior"] = "normal"
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
        expected_candidate = Path(os.environ["EXPECTED_CANDIDATE"])
        is_expected_candidate = str(target) == str(expected_candidate) or (
            target.parent == expected_candidate.parent
            and target.name.startswith(f".{expected_candidate.name}.bound.")
        )
        if is_expected_candidate:
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
            if state.get("job_behavior") == "complete-before-observation":
                activate(target)
            elif state.get("job_behavior") == "disappear-without-activation":
                save()
            else:
                job_id = str(state["next_job_id"])
                state["next_job_id"] += 1
                state.setdefault("jobs", {})[job_id] = str(target)
                if state.get("job_behavior") == "multiple-on-dispatch":
                    second = str(state["next_job_id"])
                    state["next_job_id"] += 1
                    state["jobs"][second] = str(target)
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
            "prlimit": self.fake_bin / "prlimit",
            "systemd_run": self.fake_bin / "systemd-run",
            "systemctl": self.fake_bin / "systemctl",
            "curl": self.fake_bin / "curl",
            "readelf": self.fake_bin / "readelf",
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
            "prlimit": self.fake_bin / "prlimit",
            "systemd_run": self.fake_bin / "systemd-run",
            "systemctl": self.fake_bin / "systemctl",
            "curl": self.fake_bin / "curl",
            "git": Path("/usr/bin/git"),
            "git_remote_https": Path("/usr/lib/git-core/git-remote-http"),
            "readelf": self.fake_bin / "readelf",
            "nice": Path("/usr/bin/nice"),
            "ionice": Path("/usr/bin/ionice"),
            "cc": Path("/usr/bin/gcc").resolve(strict=True),
            "ld": Path("/usr/bin/ld.bfd").resolve(strict=True),
            "ar": Path("/usr/bin/ar").resolve(strict=True),
            "as": Path("/usr/bin/as").resolve(strict=True),
            "python": Path("/usr/bin/python3").resolve(strict=True),
            "ca_cert": Path("/etc/ssl/certs/ca-certificates.crt"),
            "resolv_conf": Path("/etc/resolv.conf"),
            "nsswitch": Path("/etc/nsswitch.conf"),
            "hosts": Path("/etc/hosts"),
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

        def bind_spec(path: Path) -> dict[str, object]:
            resolved = path.resolve(strict=True)
            info = resolved.stat()
            spec: dict[str, object] = {
                "device": info.st_dev,
                "inode": info.st_ino,
                "kind": "directory" if resolved.is_dir() else "regular",
                "mode": stat.S_IMODE(info.st_mode),
                "nlink": info.st_nlink,
                "path": str(resolved),
                "uid": info.st_uid,
            }
            if resolved.is_file():
                spec.update(
                    size=info.st_size,
                    sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(),
                )
            return spec

        source_entries: dict[str, dict[str, object]] = {}
        for row in self._git("ls-tree", "-r", self.source_commit).stdout.splitlines():
            metadata, name = row.split("\t", 1)
            mode, kind, oid = metadata.split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                continue
            content = self._git("cat-file", "blob", oid).stdout.encode()
            source_entries[name] = {
                "mode": 0o500 if mode == "100755" else 0o400,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }

        def tool(name: str) -> Path:
            return Path(tools[name]["resolved"])

        bwrap_authorities = {
            "/usr/lib/aarch64-linux-gnu": bind_spec(self.sysroot_lib),
            "/usr/lib/python3.12": bind_spec(self.python_stdlib),
            "/tools/python": bind_spec(tool("python")),
            "/etc/ssl/certs/ca-certificates.crt": bind_spec(tool("ca_cert")),
            "/etc/resolv.conf": bind_spec(tool("resolv_conf")),
            "/etc/nsswitch.conf": bind_spec(tool("nsswitch")),
            "/etc/hosts": bind_spec(tool("hosts")),
            "/tools/git": bind_spec(tool("git")),
            "/tools/git-remote-https": bind_spec(tool("git_remote_https")),
            "/usr/lib/gcc/aarch64-linux-gnu/13": bind_spec(self.gcc_root),
            "/usr/include": bind_spec(self.sysroot_include),
            "/usr/bin/aarch64-linux-gnu-gcc-13": bind_spec(tool("cc")),
            "/usr/bin/aarch64-linux-gnu-as": bind_spec(tool("as")),
            "/usr/bin/aarch64-linux-gnu-ld.bfd": bind_spec(tool("ld")),
            "/usr/bin/aarch64-linux-gnu-ar": bind_spec(tool("ar")),
            "/toolchain": bind_spec(self.toolchain_root),
            "/toolchain/lib/rustlib/aarch64-unknown-linux-gnu": bind_spec(
                self.target_rustlib
            ),
            "/toolchain/bin/cargo": bind_spec(tool("cargo")),
            "/toolchain/bin/rustc": bind_spec(tool("rustc")),
            "/cargo-home/registry/cache": bind_spec(self.registry_cache),
            "/cargo-home/registry/index": bind_spec(self.registry_index),
            "/src": {
                "directory_mode": 0o500,
                "entries": source_entries,
                "kind": "source-tree",
                "mode": 0o500,
                "uid": os.geteuid(),
            },
        }
        cgroup_info = self.cgroup_root.stat()
        bwrap_authorities["/sys/fs/cgroup"] = {
            "device": cgroup_info.st_dev,
            "inode": cgroup_info.st_ino,
            "kind": "path-directory",
            "mode": stat.S_IMODE(cgroup_info.st_mode),
            "path": str(self.cgroup_root),
            "uid": cgroup_info.st_uid,
        }
        self.bwrap_authority_config.write_text(
            json.dumps({"bwrap_authorities": bwrap_authorities}, sort_keys=True)
        )
        self.bwrap_authority_config.chmod(0o600)
        test_env = {
            "BWRAP_LOGICAL_PATH": str(self.fake_bin / "bwrap"),
            "EXPECTED_CANDIDATE": str(self.candidate),
            "FIXTURE_BUILD_SOURCE": str(self.build_source),
            "FIXTURE_ALTERNATE_BUILD_SOURCE": str(self.alternate_build_source),
            "GUARD_TEST_BWRAP_AUTHORITY": str(self.bwrap_authority_config),
            "GUARD_TEST_STATE": str(self.state_path),
            "GUARD_TEST_TOOL_LOG": str(self.tool_log),
            "GUARD_TEST_DESCENDANT_PIDS": str(self.descendant_pids),
            "GCC_ROOT": str(self.gcc_root),
            "GUARD_TEST_CGROUP_ROOT": str(self.cgroup_root),
            "LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG": str(self.guard_config),
            "LLM_GUARD_PROXY_REBUILD_GUARD_UNIT": str(self.guard_unit),
            "LLM_GUARD_PROXY_REBUILD_PROC_ROOT": str(self.proc_root),
            "SAME_HASH_TARGET": str(self.same_hash_other_inode),
            "SERVICE_BIN": str(self.service_bin),
            "SOURCE_DIR": str(self.source_dir),
            "SOURCE_REPO": str(self.remote),
            "WRONG_HASH_TARGET": str(self.wrong_hash),
        }
        payload = {
            "schema": 1,
            "cgroup_root": str(self.cgroup_root),
            "registry_cache": str(self.registry_cache),
            "registry_index": str(self.registry_index),
            "gcc_root": str(self.gcc_root),
            "sysroot_lib": str(self.sysroot_lib),
            "sysroot_include": str(self.sysroot_include),
            "python_stdlib": str(self.python_stdlib),
            "test_free_bytes": self.test_free_bytes,
            "test_env": test_env,
            "toolchain_root": str(self.toolchain_root),
            "target_rustlib": str(self.target_rustlib),
            "tools": {name: tools[name] for name in {
                "ar", "cargo", "cc", "curl", "git", "ld", "readelf", "rustc",
                "systemd_run", "systemctl",
            }},
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
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if self.service_bin.is_symlink():
            published = Path(os.readlink(self.service_bin))
            try:
                published.relative_to(self.cache_root / "releases")
            except ValueError:
                pass
            else:
                self.candidate = published
                self.env["EXPECTED_CANDIDATE"] = str(published)
        return result

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

    def assert_no_scopes_or_scratch(self, test: unittest.TestCase) -> None:
        populated = []
        if self.cgroup_root.exists():
            for events in self.cgroup_root.glob("**/cgroup.events"):
                if "populated 1" in events.read_text():
                    populated.append(events)
        test.assertEqual(populated, [], self.calls())
        if self.cache_root.exists():
            test.assertEqual(
                [path for path in self.cache_root.iterdir() if path.name.startswith(".rebuild-input-")],
                [],
            )

    def assert_prior_restored(self, test: unittest.TestCase) -> None:
        test.assertTrue(self.service_bin.is_symlink())
        test.assertEqual(os.readlink(self.service_bin), str(self.prior))
        state = self.reload_state()
        test.assertEqual(state["running_target"], str(self.prior))
        test.assertTrue(state["active"])
