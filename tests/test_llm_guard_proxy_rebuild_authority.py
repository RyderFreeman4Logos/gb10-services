from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from guard_rebuild_fixtures import ROOT, RebuildFixture

ENGINE = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.py"


class GuardCanonicalAuthorityTests(unittest.TestCase):
    def test_bounded_helper_loads_from_verified_bytes_after_path_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary)
            engine = copied / ENGINE.name
            helper = copied / "gb10_bounded_process.py"
            replacement = copied / "replacement.py"
            marker = copied / "replacement-imported"
            swapped = copied / "helper-swapped"
            engine.write_bytes(ENGINE.read_bytes())
            helper.write_bytes((ROOT / "scripts/gb10_bounded_process.py").read_bytes())
            replacement.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                "raise SystemExit(88)\n"
            )
            bootstrap = (
                "import os,runpy,sys\n"
                "engine,helper,replacement,swapped=sys.argv[1:]\n"
                "original_close=os.close; done=False\n"
                "def close(descriptor):\n"
                " global done\n"
                " try: target=os.readlink(f'/proc/self/fd/{descriptor}')\n"
                " except OSError: target=''\n"
                " original_close(descriptor)\n"
                " if not done and target==helper:\n"
                "  os.replace(replacement,helper); open(swapped,'w').close(); done=True\n"
                "os.close=close; sys.argv=[engine,'--test-only']\n"
                "try: runpy.run_path(engine,run_name='guard_import_test')\n"
                "except BaseException: pass\n"
            )
            result = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "-S",
                    "-c",
                    bootstrap,
                    str(engine),
                    str(helper),
                    str(replacement),
                    str(swapped),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(swapped.exists())
            self.assertFalse(marker.exists(), result.stdout + result.stderr)

    @staticmethod
    def _git(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(path), *arguments],
            check=True,
            text=True,
            capture_output=True,
            env={
                "HOME": str(path.parent),
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "GIT_AUTHOR_NAME": "Fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            },
        )

    def _refresh_fixture_source_identity(self, fixture: RebuildFixture) -> None:
        fixture.source_commit = self._git(
            fixture.remote, "rev-parse", "HEAD"
        ).stdout.strip()
        fixture.source_tree = self._git(
            fixture.remote, "rev-parse", "HEAD^{tree}"
        ).stdout.strip()
        fixture.candidate = (
            fixture.cache_root
            / "releases"
            / f"{fixture.source_commit}-{fixture.binary_sha256}"
            / "llm-guard-proxy"
        )
        fixture.env["EXPECTED_CANDIDATE"] = str(fixture.candidate)

    def _add_gitlink(self, fixture: RebuildFixture) -> None:
        self._git(
            fixture.remote,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{fixture.source_commit},vendor",
        )

    def test_existing_checkout_origin_cannot_redirect_canonical_fetch(self) -> None:
        with RebuildFixture() as fixture:
            first = fixture.run()
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            alternate = fixture.root / "alternate-remote"
            shutil.copytree(fixture.remote, alternate)
            source = alternate / "llm-guard-proxy" / "src" / "main.rs"
            source.write_text('fn main() { println!("alternate"); }\n')
            self._git(alternate, "add", "--", str(source.relative_to(alternate)))
            self._git(alternate, "commit", "-m", "alternate source")
            subprocess.run(
                [
                    "/usr/bin/git",
                    "clone",
                    str(fixture.remote),
                    str(fixture.source_dir),
                ],
                check=True,
                text=True,
                capture_output=True,
                env={
                    "HOME": str(fixture.home),
                    "PATH": "/usr/bin:/bin",
                    "LC_ALL": "C",
                },
            )
            self._git(fixture.source_dir, "remote", "set-url", "origin", str(alternate))
            hook_marker = fixture.root / "checkout-hook-ran"
            hook = fixture.source_dir / ".git" / "hooks" / "post-checkout"
            hook.write_text(f"#!/bin/sh\ntouch {hook_marker}\n")
            hook.chmod(0o755)
            self._git(
                fixture.source_dir,
                "config",
                "url.file:///noncanonical/.insteadOf",
                "https://github.com/",
            )

            second = fixture.run()
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertFalse(hook_marker.exists())
            receipts = fixture.receipt_paths()
            self.assertEqual(len(receipts), 2)
            receipt = json.loads(receipts[-1].read_text())
            self.assertEqual(
                receipt["authorities"]["source_commit"], fixture.source_commit
            )
            self.assertEqual(
                receipt["authorities"]["canonical_source_url"],
                str(fixture.remote),
            )

    def test_missing_bwrap_fails_before_build_or_link(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(
                extra_env={"LLM_GUARD_REBUILD_TEST_MISSING_TOOL": "bwrap"}
            )
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("bwrap", output)

    def test_config_and_unit_special_files_fail_closed_within_deadline(self) -> None:
        cases = ("fifo", "symlink", "device", "oversize")
        for target in ("config", "unit"):
            for kind in cases:
                with (
                    self.subTest(target=target, kind=kind),
                    RebuildFixture() as fixture,
                ):
                    original_link = os.readlink(fixture.service_bin)
                    path = (
                        fixture.guard_config
                        if target == "config"
                        else fixture.guard_unit
                    )
                    if kind == "fifo":
                        path.unlink()
                        os.mkfifo(path, 0o600)
                    elif kind == "symlink":
                        path.unlink()
                        path.symlink_to(fixture.prior)
                    elif kind == "device":
                        name = (
                            "LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG"
                            if target == "config"
                            else "LLM_GUARD_PROXY_REBUILD_GUARD_UNIT"
                        )
                        fixture.env[name] = "/dev/null"
                    else:
                        path.write_bytes(b"x" * (1024 * 1024 + 1))
                    started = time.monotonic()
                    result = fixture.run(
                        extra_env={"LLM_GUARD_REBUILD_TEST_FORWARD_SECONDS": "2"},
                        timeout=6,
                    )
                    elapsed = time.monotonic() - started
                    output = result.stdout + result.stderr
                    self.assertNotEqual(result.returncode, 0, output)
                    self.assertLess(elapsed, 5, f"{kind} blocked for {elapsed:.3f}s")
                    self.assertEqual(os.readlink(fixture.service_bin), original_link)
                    self.assertNotIn("cargo build", fixture.calls())

    def test_manager_loaded_contract_mismatch_prevents_link_mutation(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(manager_contract_mismatch=True)
            before = os.readlink(fixture.service_bin)
            result = fixture.run(timeout=20)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("manager-loaded Guard contract differs", output)
            self.assertEqual(os.readlink(fixture.service_bin), before)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)

    def test_running_config_credential_mismatch_is_not_adopted(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(applied_config_override="substituted-config\n")
            result = fixture.run(timeout=20)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("running Guard config authority differs", output)
            fixture.assert_prior_restored(self)

    def test_exact_running_config_credential_generation_passes(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(timeout=20)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("LLM_GUARD_PROXY_REBUILD_TEST_ONLY_COMPLETE", output)

    def test_sealed_gcc_closure_is_consumed_and_ambient_usr_is_not(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(timeout=20)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            state = fixture.reload_state()
            self.assertTrue(state["held_ld_consumed"])
            self.assertFalse(state["ambient_usr_consumed"])
            receipt = json.loads(fixture.receipt_paths()[0].read_text())
            inputs = receipt["authorities"]["build_inputs"]
            self.assertIn("gcc_closure", inputs)
            self.assertIn("sysroot_lib", inputs)
            self.assertIn("sysroot_include", inputs)

    def test_mutated_consumed_gcc_closure_is_rejected_without_adoption(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(mutate_gcc_closure_during_build=True)
            before = os.readlink(fixture.service_bin)
            result = fixture.run(timeout=20)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("directory authority ledger changed: gcc closure", output)
            self.assertEqual(os.readlink(fixture.service_bin), before)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)
            fixture.assert_prior_restored(self)

    def test_candidate_path_swap_after_fd_check_is_rejected(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(candidate_path_swap=True)
            before = os.readlink(fixture.service_bin)
            result = fixture.run(timeout=20)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("candidate executable authority changed", output)
            self.assertEqual(os.readlink(fixture.service_bin), before)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)

    def test_forbidden_tree_and_path_dependency_classes_fail_pre_cutover(self) -> None:
        cases = {
            "symlink": lambda fixture: (fixture.remote / "host-link").symlink_to(
                "/etc/passwd"
            ),
            "git-attributes": lambda fixture: (
                fixture.remote / ".gitattributes"
            ).write_text("* filter=hostile\n"),
            "git-modules": lambda fixture: (fixture.remote / ".gitmodules").write_text(
                "[submodule 'vendor']\npath=vendor\nurl=https://example.invalid\n"
            ),
            "gitlink": self._add_gitlink,
            "cargo-config": lambda fixture: (
                fixture.remote / ".cargo" / "config.toml"
            ).write_text("[build]\nrustc-wrapper='/bin/false'\n"),
            "path-escape": lambda fixture: (
                fixture.remote / "llm-guard-proxy" / "Cargo.toml"
            ).write_text(
                "[package]\nname='llm-guard-proxy'\nversion='0.0.0'\nedition='2021'\n"
                "[features]\nguard=[]\n[dependencies]\noutside={path='../../outside'}\n"
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), RebuildFixture() as fixture:
                if name == "cargo-config":
                    (fixture.remote / ".cargo").mkdir()
                mutate(fixture)
                if name != "gitlink":
                    self._git(fixture.remote, "add", "--all")
                self._git(fixture.remote, "commit", "-m", f"hostile {name}")
                self._refresh_fixture_source_identity(fixture)
                result = fixture.run()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(fixture.reload_state()["restart_calls"], 0)
                fixture.assert_prior_restored(self)

    def test_path_git_config_home_and_cwd_are_not_build_authority(self) -> None:
        with RebuildFixture() as fixture:
            ambient = fixture.root / "ambient"
            shim = ambient / "bin"
            shim.mkdir(parents=True)
            marker = ambient / "shim-ran"
            (shim / "cargo").write_text(f"#!/bin/sh\ntouch {marker}\nexec /bin/false\n")
            (shim / "cargo").chmod(0o755)
            (ambient / "Cargo.toml").write_text("hostile ambient manifest\n")
            git_config = ambient / "gitconfig"
            git_config.write_text(
                "[url 'file:///noncanonical/']\n\tinsteadOf = https://github.com/\n"
            )
            result = fixture.run(
                cwd=ambient,
                extra_env={
                    "PATH": f"{shim}:/usr/bin:/bin",
                    "HOME": str(ambient),
                    "GIT_CONFIG_GLOBAL": str(git_config),
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists())
            receipt = json.loads(fixture.receipt_paths()[0].read_text())
            self.assertEqual(
                receipt["authorities"]["source_commit"], fixture.source_commit
            )
            self.assertEqual(
                receipt["candidate"]["identity"]["sha256"], fixture.binary_sha256
            )

    def test_held_bwrap_swap_and_source_mount_rename_fail_pre_cutover(self) -> None:
        for state_key, diagnostic in (
            (
                "replace_bwrap_during_use",
                "held tool pathname or metadata changed: bwrap",
            ),
            ("rename_source_mount", "directory authority changed: canonical source"),
        ):
            with self.subTest(state_key=state_key), RebuildFixture() as fixture:
                fixture.set_state(**{state_key: True})
                result = fixture.run()
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn(diagnostic, output)
                self.assertEqual(fixture.reload_state()["restart_calls"], 0)
                fixture.assert_prior_restored(self)

    def test_fake_sandbox_artifact_bytes_vary_with_canonical_source(self) -> None:
        with RebuildFixture() as fixture:
            source = fixture.remote / "llm-guard-proxy" / "src" / "main.rs"
            source.write_text('fn main() { println!("alternate"); }\n')
            self._git(
                fixture.remote, "add", "--", str(source.relative_to(fixture.remote))
            )
            self._git(fixture.remote, "commit", "-m", "alternate artifact")
            false_sha = hashlib.sha256(Path("/usr/bin/false").read_bytes()).hexdigest()
            self.assertNotEqual(false_sha, fixture.binary_sha256)
            fixture.binary_sha256 = false_sha
            self._refresh_fixture_source_identity(fixture)
            result = fixture.run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(fixture.receipt_paths()[0].read_text())
            self.assertEqual(receipt["candidate"]["identity"]["sha256"], false_sha)

    def test_unsafe_raw_artifact_objects_fail_before_adoption(self) -> None:
        for artifact_mode in (
            "symlink",
            "hardlink",
            "group-writable",
            "world-writable",
            "empty",
            "oversize",
        ):
            with self.subTest(artifact_mode=artifact_mode), RebuildFixture() as fixture:
                fixture.set_state(artifact_mode=artifact_mode)
                result = fixture.run()
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("built artifact", output)
                self.assertEqual(fixture.reload_state()["restart_calls"], 0)
                fixture.assert_prior_restored(self)

    def test_production_source_pins_canonical_tools_and_bwrap_shape(self) -> None:
        source = ENGINE.read_text()
        self.assertIn("https://github.com/NousResearch/llm-guard-proxy.git", source)
        self.assertNotIn("RyderFreeman4Logos/llm-guard-proxy", source)
        for required in (
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-all",
            "--disable-userns",
            "--die-with-parent",
            "--clearenv",
            "--tmpfs",
            "/usr/local",
            "--chdir",
            "/src",
            "--offline",
            "--frozen",
            "--locked",
            "x86_64-unknown-linux-gnu",
        ):
            self.assertIn(required, source)
        self.assertNotIn("shutil.which", source)
        self.assertNotIn('"--ro-bind",\n        "/usr",\n        "/usr"', source)
        self.assertIn('held_tools["ld"].exec_path', source)
        self.assertNotIn('clone",\n                "--filter=blob:none', source)

    @unittest.skipUnless(
        os.environ.get("JUST_NO_DOTENV") == "true",
        "real Cargo is permitted only when reached through repository Just gates",
    )
    def test_real_bwrap_offline_tiny_crate_positive(self) -> None:
        toolchain = Path(
            "/usr/local/share/mise/installs/rust/stable/toolchains/"
            "stable-x86_64-unknown-linux-gnu"
        )
        reviewed = {
            "bwrap": (
                Path("/usr/bin/bwrap"),
                (0, 0, 0o755),
                "85580dd52ed366ece8844e90fa75ac7c4de8802963071344e123221fb9f6f11e",
            ),
            "cargo": (
                toolchain / "bin" / "cargo",
                (1001, 1001, 0o755),
                "828980723df339d62434390e9fb8ef8831036583343ae2316b7ab5646b5c1953",
            ),
            "rustc": (
                toolchain / "bin" / "rustc",
                (1001, 1001, 0o755),
                "d3a664c970a9fd8361b64194861bebc1ae37b9054e5ee3400dc1c9e691797eea",
            ),
            "cc": (
                Path("/usr/bin/x86_64-linux-gnu-gcc-12"),
                (0, 0, 0o755),
                "75e997ec62297a6484f491bae28ab0ccb489daba23e398fd10fe68e9e6f0def8",
            ),
            "ar": (
                Path("/usr/bin/x86_64-linux-gnu-ar"),
                (0, 0, 0o755),
                "3acbee2794e3668a74bcb90f2eaf7d981211fb95288ab940f0e3ac380e8f6023",
            ),
            "as": (
                Path("/usr/bin/x86_64-linux-gnu-as"),
                (0, 0, 0o755),
                "41fe4f5a03389ea5cf7c92d6753fa1ecc69b45b12534fd8713c53bba0e2d7e17",
            ),
            "ld": (
                Path("/usr/bin/x86_64-linux-gnu-ld.bfd"),
                (0, 0, 0o755),
                "f6d71a1bcd45764550a42dfaa179bc43b63ee879ec6f875bfd39fca013515da7",
            ),
        }
        held: dict[str, int] = {}
        directory_fds: list[int] = []
        try:
            for name, (path, expected_metadata, expected_digest) in reviewed.items():
                descriptor = os.open(
                    path,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                )
                held[name] = descriptor
                before = os.fstat(descriptor)
                self.assertTrue(stat.S_ISREG(before.st_mode))
                self.assertEqual(
                    (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)),
                    expected_metadata,
                )
                self.assertEqual(before.st_nlink, 1)
                digest = hashlib.sha256()
                offset = 0
                while chunk := os.pread(descriptor, 1024 * 1024, offset):
                    offset += len(chunk)
                    digest.update(chunk)
                after = os.fstat(descriptor)
                current = os.stat(path, follow_symlinks=False)
                self.assertEqual(
                    (before.st_dev, before.st_ino, before.st_size),
                    (after.st_dev, after.st_ino, after.st_size),
                )
                self.assertEqual(
                    (before.st_dev, before.st_ino, before.st_size),
                    (current.st_dev, current.st_ino, current.st_size),
                )
                self.assertEqual(digest.hexdigest(), expected_digest, name)

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                target = root / "target"
                cache = root / "cache"
                index = root / "index"
                for directory in (source / "src", target, cache, index):
                    directory.mkdir(parents=True)
                (source / "Cargo.toml").write_text(
                    "[package]\nname='llm-guard-proxy'\nversion='0.0.0'\nedition='2021'\n"
                    "[features]\nguard=[]\n"
                )
                (source / "Cargo.lock").write_text(
                    'version = 4\n\n[[package]]\nname = "llm-guard-proxy"\n'
                    'version = "0.0.0"\n'
                )
                (source / "src" / "main.rs").write_text("fn main() {}\n")

                mount_paths = (
                    source,
                    target,
                    toolchain,
                    cache,
                    index,
                    Path("/usr/lib/gcc/x86_64-linux-gnu/12"),
                    Path("/usr/lib/x86_64-linux-gnu"),
                    Path("/usr/include"),
                )
                for path in mount_paths:
                    directory_fds.append(
                        os.open(
                            path,
                            os.O_RDONLY
                            | os.O_DIRECTORY
                            | os.O_CLOEXEC
                            | getattr(os, "O_NOFOLLOW", 0),
                        )
                    )
                (
                    source_fd,
                    target_fd,
                    toolchain_fd,
                    cache_fd,
                    index_fd,
                    gcc_fd,
                    sysroot_lib_fd,
                    sysroot_include_fd,
                ) = directory_fds
                base = [
                    f"/proc/self/fd/{held['bwrap']}",
                    "--unshare-user",
                    "--unshare-all",
                    "--disable-userns",
                    "--die-with-parent",
                    "--new-session",
                    "--cap-drop",
                    "ALL",
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "--dir",
                    "/usr",
                    "--dir",
                    "/usr/bin",
                    "--dir",
                    "/usr/lib",
                    "--dir",
                    "/usr/lib/gcc",
                    "--dir",
                    "/usr/lib/gcc/x86_64-linux-gnu",
                    "--ro-bind",
                    f"/proc/self/fd/{gcc_fd}",
                    "/usr/lib/gcc/x86_64-linux-gnu/12",
                    "--ro-bind",
                    f"/proc/self/fd/{sysroot_lib_fd}",
                    "/usr/lib/x86_64-linux-gnu",
                    "--ro-bind",
                    f"/proc/self/fd/{sysroot_include_fd}",
                    "/usr/include",
                    "--ro-bind",
                    f"/proc/self/fd/{held['cc']}",
                    "/usr/bin/x86_64-linux-gnu-gcc-12",
                    "--ro-bind",
                    f"/proc/self/fd/{held['as']}",
                    "/usr/bin/as",
                    "--ro-bind",
                    f"/proc/self/fd/{held['ld']}",
                    "/usr/bin/ld",
                    "--ro-bind",
                    f"/proc/self/fd/{held['ar']}",
                    "/usr/bin/x86_64-linux-gnu-ar",
                    "--tmpfs",
                    "/usr/local",
                    "--tmpfs",
                    "/home",
                    "--symlink",
                    "usr/lib",
                    "/lib",
                    "--symlink",
                    "usr/lib/x86_64-linux-gnu",
                    "/lib64",
                    "--ro-bind",
                    f"/proc/self/fd/{source_fd}",
                    "/src",
                    "--ro-bind",
                    f"/proc/self/fd/{toolchain_fd}",
                    "/toolchain",
                    "--ro-bind",
                    f"/proc/self/fd/{held['cargo']}",
                    "/toolchain/bin/cargo",
                    "--ro-bind",
                    f"/proc/self/fd/{held['rustc']}",
                    "/toolchain/bin/rustc",
                    "--dir",
                    "/cargo-home",
                    "--dir",
                    "/cargo-home/registry",
                    "--ro-bind",
                    f"/proc/self/fd/{cache_fd}",
                    "/cargo-home/registry/cache",
                    "--ro-bind",
                    f"/proc/self/fd/{index_fd}",
                    "/cargo-home/registry/index",
                    "--bind",
                    f"/proc/self/fd/{target_fd}",
                    "/target",
                    "--tmpfs",
                    "/tmp",
                    "--clearenv",
                    "--setenv",
                    "HOME",
                    "/nonexistent",
                    "--setenv",
                    "PATH",
                    "/toolchain/bin:/usr/bin",
                    "--setenv",
                    "LC_ALL",
                    "C",
                    "--setenv",
                    "LANG",
                    "C",
                    "--setenv",
                    "CARGO_HOME",
                    "/cargo-home",
                    "--setenv",
                    "CARGO_TARGET_DIR",
                    "/target",
                    "--setenv",
                    "CARGO_NET_OFFLINE",
                    "true",
                    "--setenv",
                    "CARGO_INCREMENTAL",
                    "0",
                    "--setenv",
                    "CARGO_BUILD_JOBS",
                    "1",
                    "--setenv",
                    "RUSTC",
                    "/toolchain/bin/rustc",
                    "--setenv",
                    "CC",
                    "/usr/bin/x86_64-linux-gnu-gcc-12",
                    "--setenv",
                    "AR",
                    "/usr/bin/x86_64-linux-gnu-ar",
                    "--setenv",
                    "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER",
                    "/usr/bin/x86_64-linux-gnu-gcc-12",
                    "--chdir",
                    "/src",
                    "--",
                ]
                pass_fds = (*held.values(), *directory_fds)

                def sandbox(*arguments: str) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [*base, *arguments],
                        check=False,
                        text=True,
                        capture_output=True,
                        timeout=120,
                        pass_fds=pass_fds,
                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
                    )

                cargo_version = sandbox(
                    "/toolchain/bin/cargo", "--version", "--verbose"
                )
                self.assertEqual(
                    cargo_version.returncode,
                    0,
                    cargo_version.stdout + cargo_version.stderr,
                )
                rustc_version = sandbox("/toolchain/bin/rustc", "-vV")
                self.assertEqual(
                    rustc_version.returncode,
                    0,
                    rustc_version.stdout + rustc_version.stderr,
                )
                self.assertEqual(
                    cargo_version.stdout.splitlines()[0],
                    "cargo 1.97.1 (c980f4866 2026-06-30)",
                )
                self.assertEqual(
                    rustc_version.stdout.splitlines()[0],
                    "rustc 1.97.1 (8bab26f4f 2026-07-14)",
                )
                self.assertIn("LLVM version: 22.1.6", rustc_version.stdout)
                metadata = sandbox(
                    "/toolchain/bin/cargo",
                    "metadata",
                    "--format-version=1",
                    "--frozen",
                    "--locked",
                    "--offline",
                    "--filter-platform",
                    "x86_64-unknown-linux-gnu",
                )
                self.assertEqual(
                    metadata.returncode, 0, metadata.stdout + metadata.stderr
                )
                metadata_payload = json.loads(metadata.stdout)
                self.assertEqual(metadata_payload["workspace_root"], "/src")
                self.assertEqual(metadata_payload["target_directory"], "/target")
                self.assertEqual(
                    [path.name for path in target.iterdir()], [".rustc_info.json"]
                )
                rustc_info = (target / ".rustc_info.json").stat(follow_symlinks=False)
                self.assertTrue(stat.S_ISREG(rustc_info.st_mode))
                self.assertEqual(rustc_info.st_uid, os.geteuid())
                self.assertEqual(rustc_info.st_nlink, 1)
                self.assertFalse(rustc_info.st_mode & 0o022)
                self.assertGreater(rustc_info.st_size, 0)
                result = sandbox(
                    "/toolchain/bin/cargo",
                    "build",
                    "--release",
                    "--frozen",
                    "--locked",
                    "--offline",
                    "--target",
                    "x86_64-unknown-linux-gnu",
                    "--features",
                    "guard",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                artifact = (
                    target / "x86_64-unknown-linux-gnu" / "release" / "llm-guard-proxy"
                )
                artifact_info = artifact.stat(follow_symlinks=False)
                if artifact_info.st_nlink == 2:
                    linked = [
                        path
                        for path in (artifact.parent / "deps").iterdir()
                        if path.stat(follow_symlinks=False).st_ino
                        == artifact_info.st_ino
                        and path.stat(follow_symlinks=False).st_dev
                        == artifact_info.st_dev
                    ]
                    self.assertEqual(len(linked), 1)
                    self.assertRegex(linked[0].name, r"^llm_guard_proxy-[0-9a-f]{16}$")
                    linked[0].unlink()
                    artifact_info = artifact.stat(follow_symlinks=False)
                self.assertTrue(stat.S_ISREG(artifact_info.st_mode))
                self.assertEqual(artifact_info.st_uid, os.geteuid())
                self.assertEqual(artifact_info.st_nlink, 1)
                self.assertFalse(artifact_info.st_mode & 0o022)
                self.assertGreater(artifact_info.st_size, 0)
                self.assertFalse((source / "target").exists())
        finally:
            for descriptor in reversed(directory_fds):
                os.close(descriptor)
            for descriptor in reversed(list(held.values())):
                os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
