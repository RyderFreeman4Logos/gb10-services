from __future__ import annotations

import ast
import errno
import fcntl
import gc
import hashlib
import importlib.util
import json
import os
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from guard_rebuild_fixtures import ROOT, RebuildFixture

BOUNDED = ROOT / "scripts" / "gb10_bounded_process.py"
ENGINE = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.py"
WORKER = ROOT / "scripts" / "llm_guard_proxy_scoped_worker.py"


def _load(
    path: Path, name: str, *, module_path: Path | None = None
) -> types.ModuleType:
    if path == ENGINE:
        source_path = module_path or path
        if module_path is not None:
            assert source_path.read_bytes() == path.read_bytes()
        source = source_path.read_text()
        entrypoint = "\nraise SystemExit(run_transaction())\n"
        assert source.endswith(entrypoint)
        module = types.ModuleType(name)
        module.__file__ = str(source_path)
        module.__package__ = ""
        sys.modules[name] = module
        exec(
            compile(source[: -len(entrypoint)], str(source_path), "exec"),
            module.__dict__,
        )
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _wait_for_path(path: Path, process: subprocess.Popen[str], timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.01)
    return path.exists()


def _kill_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return process.communicate(timeout=5)


def _terminate_reap_group(
    process: subprocess.Popen[bytes], worker_pid: int = 0
) -> None:
    errors: list[Exception] = []
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as error:  # noqa: BLE001 - cleanup must continue.
            errors.append(error)
    try:
        process.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    except Exception as error:  # noqa: BLE001 - cleanup must continue.
        errors.append(error)
    if worker_pid > 0 and process.returncode is None:
        worker_pidfd = -1
        try:
            worker_pidfd = os.pidfd_open(worker_pid, 0)
            signal.pidfd_send_signal(worker_pidfd, signal.SIGKILL, None, 0)
        except Exception as error:  # noqa: BLE001 - group cleanup still owns fallback.
            errors.append(error)
        finally:
            if worker_pidfd >= 0:
                try:
                    os.close(worker_pidfd)
                except Exception as error:  # noqa: BLE001 - cleanup must continue.
                    errors.append(error)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        except Exception as error:  # noqa: BLE001 - cleanup must continue.
            errors.append(error)
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as error:  # noqa: BLE001 - final reap still owns Popen.
            errors.append(error)
    try:
        process.communicate(timeout=5)
    except Exception as error:  # noqa: BLE001 - wait remains independently required.
        errors.append(error)
    finally:
        if process.returncode is None:
            try:
                process.wait(timeout=5)
            except Exception as error:  # noqa: BLE001 - report after all attempts.
                errors.append(error)
    if errors:
        raise ExceptionGroup("strict fake-bwrap cleanup failed", errors)


class GuardCanonicalAuthorityTests(unittest.TestCase):
    def test_sysroot_runtime_allows_bounded_usr_links_but_rejects_escape_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            usr = Path(temporary) / "usr"
            sysroot = usr / "lib" / "aarch64-linux-gnu"
            (usr / "lib" / "libreoffice" / "program").mkdir(parents=True)
            (usr / "share" / "qtchooser").mkdir(parents=True)
            (sysroot / "qt-default" / "qtchooser").mkdir(parents=True)
            (sysroot / "libuno_sal.so.3").symlink_to(
                "../libreoffice/program/libuno_sal.so.3"
            )
            (sysroot / "qt-default" / "qtchooser" / "default.conf").symlink_to(
                "../../../../share/qtchooser/qt5-aarch64-linux-gnu.conf"
            )
            with patch.object(sys, "argv", [str(ENGINE)]):
                engine = _load(ENGINE, "sysroot_runtime_authority")
            authority = engine._open_directory_authority("sysroot runtime", sysroot)
            authority.close()

            for name, target in (
                ("escape", "../../../etc/passwd"),
                ("absolute", "/etc/passwd"),
            ):
                with self.subTest(target=target):
                    unsafe = sysroot / name
                    unsafe.symlink_to(target)
                    with self.assertRaisesRegex(
                        engine.RebuildError,
                        "unsafe symlink in directory authority: sysroot runtime",
                    ):
                        engine._open_directory_authority("sysroot runtime", sysroot)

    def test_production_tool_specs_pin_complete_gb10_authority(self) -> None:
        old_handlers = {
            number: signal.getsignal(number)
            for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        }
        try:
            with patch.object(sys, "argv", [str(ENGINE)]):
                engine = _load(ENGINE, "complete_production_tool_authority")
        finally:
            for number, handler in old_handlers.items():
                signal.signal(number, handler)

        root_executables = {
            "ar": (
                "/usr/bin/aarch64-linux-gnu-ar",
                "f4583a612510e038dbc1ae8afb5eae5f0445c435bdd7c4c28e78acc7e757d2b1",
            ),
            "as": (
                "/usr/bin/aarch64-linux-gnu-as",
                "1ffda50efb6d91b6b05ef933aced099595c36577594c0296c91161f2d13db374",
            ),
            "bwrap": (
                "/usr/bin/bwrap",
                "ae27935781511400c65ebcc0b4669775d602f46251b8707c947a1ac1b160c1c8",
            ),
            "cc": (
                "/usr/bin/aarch64-linux-gnu-gcc-13",
                "a20520ee21543f243d40636a9181a142c45ecd989de31ab86b99a8ea5ada870d",
            ),
            "curl": (
                "/usr/bin/curl",
                "67054bcf748d42e1bf4b2a0eb4ba768e37dde8681313a64edd2f343c5d17a0ac",
            ),
            "git": (
                "/usr/bin/git",
                "aa6540695d076182256dd6e96c8b302e4d56381e3000bbfd5c71bbdfe94a4942",
            ),
            "git_remote_https": (
                "/usr/lib/git-core/git-remote-http",
                "8ebf256cd802e7af7ea0e91f67deed42c207737bd43c8e94070ed342bf55938d",
            ),
            "ionice": (
                "/usr/bin/ionice",
                "5853dfc5b2513284f4e837b837aa5c05747ef98b9ce150cb0ba096b2ede6fa4b",
            ),
            "ld": (
                "/usr/bin/aarch64-linux-gnu-ld.bfd",
                "1e4d3369b76845fa8e099b83513bf28f6f722e046f9b2cde1378c9e27f96d19c",
            ),
            "nice": (
                "/usr/bin/nice",
                "0746d1600af7606b356e98974e05a26ce8db22e7c99df5bf4613d06128a3e566",
            ),
            "prlimit": (
                "/usr/bin/prlimit",
                "2a479939c95a886fb8b52244381639816f8cf1f68eee713a9227d0c5d257c805",
            ),
            "python": (
                "/usr/bin/python3.12",
                "a7d56a8a764faf7bbf5c164055a48fd072be52287bdeb523a9e07b2042f4e7e1",
            ),
            "readelf": (
                "/usr/bin/aarch64-linux-gnu-readelf",
                "6bca2bbd23b072db9e9a19ae0e65cf7b7c15c08a3c2cf01dd56453e1ac9340b1",
            ),
            "systemctl": (
                "/usr/bin/systemctl",
                "1bf2f1e98c533b0313a78143ffcda2690116d90d76816dc7b74df79c4767aa95",
            ),
            "systemd_run": (
                "/usr/bin/systemd-run",
                "0253595d482ea0aa9c4bf2e58080e9615bc59a51e29dbe1cebd14145b92bd8fc",
            ),
        }
        root_data = {
            "ca_cert": (
                "/etc/ssl/certs/ca-certificates.crt",
                "6602a85a36afc2e51c66a0df5ae3d383c5b7c2fed93339ccef7d37e01faf09e8",
            ),
            "hosts": (
                "/etc/hosts",
                "3c2e57459d0663b68ff68f174b73511d57aafe73bfadca6355bdf80187a2918a",
            ),
            "nsswitch": (
                "/etc/nsswitch.conf",
                "0b955d14e07f12048c0eb69d9bf1a2c693636014d5007381286b2163fbeac8b8",
            ),
        }
        user_objects = {
            "cargo": (
                "/home/obj/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/bin/cargo",
                0o755,
                "7db170801729d4775347548ed5970b459844fc2f6b20798efb96f444b5b94fc3",
            ),
            "rustc": (
                "/home/obj/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/bin/rustc",
                0o755,
                "2425682b7dd432769e2600eb2b3ea5113a00d37f41a754598f2af2e56e4fa2fe",
            ),
            "scoped_worker": (
                "/home/obj/.local/bin/llm_guard_proxy_scoped_worker.py",
                0o644,
                "b764a30e5586059849b364c364c61688df40f20962c55e81e5b3a6a8874426af",
            ),
        }
        expected = {
            name: engine.ToolSpec(path, path, 0, 0, 0o755, digest)
            for name, (path, digest) in root_executables.items()
        }
        expected.update(
            {
                name: engine.ToolSpec(path, path, 0, 0, 0o644, digest)
                for name, (path, digest) in root_data.items()
            }
        )
        expected["resolv_conf"] = engine.ToolSpec(
            "/run/systemd/resolve/stub-resolv.conf",
            "/run/systemd/resolve/stub-resolv.conf",
            992,
            992,
            0o644,
            "dc1495fcea40128057c4bfd1fa765c9c153011c3a6d26ca4d24bca81561e5934",
        )
        expected.update(
            {
                name: engine.ToolSpec(path, path, 1001, 1001, mode, digest)
                for name, (path, mode, digest) in user_objects.items()
            }
        )
        self.assertEqual(engine.TOOL_NAMES, set(expected))
        self.assertEqual(engine._production_tool_specs(), expected)

    def test_engine_load_rejects_archive_mode_then_uses_exact_sibling_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            authority = Path(temporary)
            authority_engine = authority / ENGINE.name
            authority_helper = authority / BOUNDED.name
            shutil.copyfile(ENGINE, authority_engine)
            shutil.copyfile(BOUNDED, authority_helper)
            authority_engine.chmod(0o600)
            authority_helper.chmod(0o664)

            old_handlers = {
                number: signal.getsignal(number)
                for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }
            try:
                with (
                    patch.object(sys, "argv", [str(authority_engine)]),
                    self.assertRaisesRegex(
                        RuntimeError, "bounded-process import authority differs"
                    ),
                ):
                    _load(
                        ENGINE,
                        "archive_mode_failure",
                        module_path=authority_engine,
                    )
                sys.modules.pop("archive_mode_failure", None)
                authority_helper.chmod(0o600)
                with patch.object(sys, "argv", [str(authority_engine)]):
                    engine = _load(
                        ENGINE,
                        "exact_sibling_module_authority",
                        module_path=authority_engine,
                    )
            finally:
                for number, handler in old_handlers.items():
                    signal.signal(number, handler)

            self.assertEqual(engine.__file__, str(authority_engine))
            self.assertEqual(engine._BOUNDED_PROCESS_PATH, authority_helper)
            self.assertEqual(
                hashlib.sha256(authority_helper.read_bytes()).hexdigest(),
                engine.EXPECTED_BOUNDED_PROCESS_SHA256,
            )

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

    def test_last_pre_wal_candidate_replacement_has_no_wal_link_or_restart(self) -> None:
        with RebuildFixture() as fixture:
            before = os.readlink(fixture.service_bin)
            marker = fixture.root / "candidate-pre-wal"
            resume = fixture.root / "candidate-pre-wal-resume"
            process = fixture.popen(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_CRASH_POINT": "candidate-pre-wal",
                    "LLM_GUARD_REBUILD_TEST_CRASH_MARKER": str(marker),
                    "LLM_GUARD_REBUILD_TEST_RESUME_MARKER": str(resume),
                }
            )
            try:
                self.assertTrue(_wait_for_path(marker, process), "pre-WAL boundary not reached")
                os.replace(fixture.wrong_hash, fixture.candidate)
                resume.touch()
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    _kill_group(process)
            output = stdout + stderr
            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("candidate executable authority changed", output)
            self.assertNotIn("test boundary prestate-fsynced", output)
            self.assertEqual(os.readlink(fixture.service_bin), before)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)
            self.assertFalse(
                fixture.receipt_dir.joinpath("transaction.v1", "state.json").exists()
            )

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
            false_sha = hashlib.sha256(fixture.alternate_build_source.read_bytes()).hexdigest()
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

    def test_wrong_candidate_elf_machine_or_loader_fails_before_mutation(self) -> None:
        cases = (
            ("wrong-machine", "machine"),
            ("missing-machine", "machine"),
            ("multiple-machine", "machine"),
            ("malformed-machine", "machine"),
            ("wrong-interpreter", "interpreter"),
            ("missing-interpreter", "interpreter"),
            ("multiple-interpreter", "interpreter"),
            ("malformed-interpreter", "interpreter"),
        )
        for mode, diagnostic in cases:
            with self.subTest(mode=mode), RebuildFixture() as fixture:
                fixture.set_state(candidate_elf_output_mode=mode)
                before = os.readlink(fixture.service_bin)
                result = fixture.run()
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn(f"candidate ELF {diagnostic} differs", output)
                self.assertEqual(os.readlink(fixture.service_bin), before)
                self.assertEqual(fixture.reload_state()["restart_calls"], 0)
                self.assertFalse(
                    fixture.receipt_dir.joinpath("transaction.v1", "state.json").exists()
                )
                fixture.assert_no_backend_lifecycle(self)

    def test_candidate_elf_facts_are_derived_from_passed_bytes(self) -> None:
        with RebuildFixture() as fixture:
            before = os.readlink(fixture.service_bin)
            fixture.set_state(build_source_override="/usr/bin/false")
            result = fixture.run()
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("candidate ELF machine differs", output)
            self.assertEqual(os.readlink(fixture.service_bin), before)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)

    def test_production_source_pins_native_gb10_aarch64_authority(self) -> None:
        source = ENGINE.read_text()
        worker = WORKER.read_text()
        self.assertIn(
            'source_repo = "https://github.com/RyderFreeman4Logos/llm-guard-proxy.git"',
            source,
        )
        self.assertNotIn("https://github.com/NousResearch/llm-guard-proxy.git", source)
        self.assertIn('source_branch = "main"', source)
        self.assertIn('source_ref = "refs/heads/main"', source)
        guard_unit_source = "profile/llm-guard-proxy/llm-guard-proxy.service"
        stale_guard_unit_source = "systemd/llm-guard-proxy.service"
        for document in (ROOT / "README.md", ROOT / "docs/deployment/AGENTS.md"):
            text = document.read_text()
            self.assertIn(guard_unit_source, text)
            self.assertNotIn(stale_guard_unit_source, text)
        for required in (
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-all",
            "--disable-userns",
            "--die-with-parent",
            "--clearenv",
            "--tmpfs",
            "--chdir",
            "/src",
            "--offline",
            "--frozen",
            "--locked",
            "aarch64-unknown-linux-gnu",
            "/home/obj/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu",
            "/usr/lib/gcc/aarch64-linux-gnu/13",
            "/usr/lib/aarch64-linux-gnu",
            "/usr/lib/python3.12",
            "/usr/bin/aarch64-linux-gnu-gcc-13",
            "/usr/bin/aarch64-linux-gnu-as",
            "/usr/bin/aarch64-linux-gnu-ar",
            "/usr/bin/aarch64-linux-gnu-ld.bfd",
            "/usr/bin/aarch64-linux-gnu-readelf",
            "2425682b7dd432769e2600eb2b3ea5113a00d37f41a754598f2af2e56e4fa2fe",
            "7db170801729d4775347548ed5970b459844fc2f6b20798efb96f444b5b94fc3",
            "a20520ee21543f243d40636a9181a142c45ecd989de31ab86b99a8ea5ada870d",
            "1ffda50efb6d91b6b05ef933aced099595c36577594c0296c91161f2d13db374",
            "f4583a612510e038dbc1ae8afb5eae5f0445c435bdd7c4c28e78acc7e757d2b1",
            "1e4d3369b76845fa8e099b83513bf28f6f722e046f9b2cde1378c9e27f96d19c",
            "6bca2bbd23b072db9e9a19ae0e65cf7b7c15c08a3c2cf01dd56453e1ac9340b1",
        ):
            self.assertIn(required, source)
        self.assertIn('"--features",\n            "guard"', worker)
        self.assertNotIn("x86_64-unknown-linux-gnu", source)
        self.assertNotIn("x86_64-unknown-linux-gnu", worker)
        self.assertNotIn("shutil.which", source)
        self.assertNotIn('"--ro-bind",\n        "/usr",\n        "/usr"', source)
        self.assertIn('held_tools["ld"].exec_path', source)
        self.assertNotIn('clone",\n                "--filter=blob:none', source)

    def test_wrong_toolchain_target_identity_fails_before_build_or_link(self) -> None:
        for key, value in (
            ("cargo_host", "x86_64-unknown-linux-gnu"),
            ("rustc_host", "x86_64-unknown-linux-gnu"),
            ("rustc_release", "1.95.0"),
        ):
            with self.subTest(key=key), RebuildFixture() as fixture:
                fixture.set_state(**{key: value})
                before = os.readlink(fixture.service_bin)
                result = fixture.run()
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("Cargo/rustc version contract differs", output)
                self.assertNotIn("cargo build", fixture.calls())
                self.assertEqual(os.readlink(fixture.service_bin), before)
                self.assertEqual(fixture.reload_state()["restart_calls"], 0)

    @unittest.skipUnless(
        os.environ.get("JUST_NO_DOTENV") == "true" and os.uname().machine == "aarch64",
        "real Cargo smoke requires a repository Just gate on native GB10 AArch64",
    )
    def test_real_bwrap_offline_tiny_crate_positive(self) -> None:
        toolchain = Path(
            "/home/obj/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu"
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
                "7db170801729d4775347548ed5970b459844fc2f6b20798efb96f444b5b94fc3",
            ),
            "rustc": (
                toolchain / "bin" / "rustc",
                (1001, 1001, 0o755),
                "2425682b7dd432769e2600eb2b3ea5113a00d37f41a754598f2af2e56e4fa2fe",
            ),
            "cc": (
                Path("/usr/bin/aarch64-linux-gnu-gcc-13"),
                (0, 0, 0o755),
                "a20520ee21543f243d40636a9181a142c45ecd989de31ab86b99a8ea5ada870d",
            ),
            "ar": (
                Path("/usr/bin/aarch64-linux-gnu-ar"),
                (0, 0, 0o755),
                "f4583a612510e038dbc1ae8afb5eae5f0445c435bdd7c4c28e78acc7e757d2b1",
            ),
            "as": (
                Path("/usr/bin/aarch64-linux-gnu-as"),
                (0, 0, 0o755),
                "1ffda50efb6d91b6b05ef933aced099595c36577594c0296c91161f2d13db374",
            ),
            "ld": (
                Path("/usr/bin/aarch64-linux-gnu-ld.bfd"),
                (0, 0, 0o755),
                "1e4d3369b76845fa8e099b83513bf28f6f722e046f9b2cde1378c9e27f96d19c",
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
                    toolchain / "lib/rustlib/aarch64-unknown-linux-gnu",
                    cache,
                    index,
                    Path("/usr/lib/gcc/aarch64-linux-gnu/13"),
                    Path("/usr/lib/aarch64-linux-gnu"),
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
                    rustlib_fd,
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
                    "/usr/lib/gcc/aarch64-linux-gnu",
                    "--ro-bind",
                    f"/proc/self/fd/{gcc_fd}",
                    "/usr/lib/gcc/aarch64-linux-gnu/13",
                    "--ro-bind",
                    f"/proc/self/fd/{sysroot_lib_fd}",
                    "/usr/lib/aarch64-linux-gnu",
                    "--ro-bind",
                    f"/proc/self/fd/{sysroot_include_fd}",
                    "/usr/include",
                    "--ro-bind",
                    f"/proc/self/fd/{held['cc']}",
                    "/usr/bin/aarch64-linux-gnu-gcc-13",
                    "--ro-bind",
                    f"/proc/self/fd/{held['as']}",
                    "/usr/bin/aarch64-linux-gnu-as",
                    "--ro-bind",
                    f"/proc/self/fd/{held['ld']}",
                    "/usr/bin/aarch64-linux-gnu-ld.bfd",
                    "--ro-bind",
                    f"/proc/self/fd/{held['ar']}",
                    "/usr/bin/aarch64-linux-gnu-ar",
                    "--tmpfs",
                    "/home",
                    "--symlink",
                    "usr/lib/aarch64-linux-gnu",
                    "/lib",
                    "--ro-bind",
                    f"/proc/self/fd/{source_fd}",
                    "/src",
                    "--ro-bind",
                    f"/proc/self/fd/{toolchain_fd}",
                    "/toolchain",
                    "--ro-bind",
                    f"/proc/self/fd/{rustlib_fd}",
                    "/toolchain/lib/rustlib/aarch64-unknown-linux-gnu",
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
                    "/usr/bin/aarch64-linux-gnu-gcc-13",
                    "--setenv",
                    "AR",
                    "/usr/bin/aarch64-linux-gnu-ar",
                    "--setenv",
                    "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER",
                    "/usr/bin/aarch64-linux-gnu-gcc-13",
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
                    "cargo 1.96.0 (30a34c682 2026-05-25)",
                )
                self.assertEqual(
                    rustc_version.stdout.splitlines()[0],
                    "rustc 1.96.0 (ac68faa20 2026-05-25)",
                )
                self.assertIn("LLVM version: 22.1.2", rustc_version.stdout)
                metadata = sandbox(
                    "/toolchain/bin/cargo",
                    "metadata",
                    "--format-version=1",
                    "--frozen",
                    "--locked",
                    "--offline",
                    "--filter-platform",
                    "aarch64-unknown-linux-gnu",
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
                    "aarch64-unknown-linux-gnu",
                    "--features",
                    "guard",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                artifact = (
                    target / "aarch64-unknown-linux-gnu" / "release" / "llm-guard-proxy"
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

    def test_direct_sandbox_scope_materialization_resolves_worker_contract(self) -> None:
        old_argv = sys.argv[:]
        old_handlers = {
            number: signal.getsignal(number)
            for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        }
        try:
            sys.argv = [str(ENGINE)]
            engine = _load(ENGINE, "direct_sandbox_scope_contract")
        finally:
            sys.argv = old_argv
            for number, handler in old_handlers.items():
                signal.signal(number, handler)

        unit = "llm-guard-rebuild-build-" + "0" * 32 + ".scope"
        command = engine._materialize_scope_unit(
            engine._worker_payload("build", {"policy": {}}), unit
        )
        self.assertEqual(command[-1], unit)
        self.assertNotIn(engine.SCOPE_UNIT_TOKEN, command)
        self.assertEqual(
            hashlib.sha256(WORKER.read_bytes()).hexdigest(),
            engine._production_tool_specs()["scoped_worker"].sha256,
        )

    @unittest.skipUnless(
        os.environ.get("JUST_NO_DOTENV") == "true" and os.uname().machine == "aarch64",
        "production Cargo sandbox requires a repository Just gate on native GB10 AArch64",
    )
    def test_production_cargo_sandbox_runs_pinned_worker_and_emits_aarch64(self) -> None:
        old_argv = sys.argv[:]
        old_handlers = {
            number: signal.getsignal(number)
            for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        }
        try:
            sys.argv = [str(ENGINE)]
            module_authority = tempfile.TemporaryDirectory()
            self.addCleanup(module_authority.cleanup)
            authority = Path(module_authority.name)
            authority_engine = authority / ENGINE.name
            authority_helper = authority / BOUNDED.name
            shutil.copyfile(ENGINE, authority_engine)
            shutil.copyfile(BOUNDED, authority_helper)
            authority_engine.chmod(0o600)
            authority_helper.chmod(0o600)
            engine = _load(
                ENGINE,
                "native_production_cargo_sandbox",
                module_path=authority_engine,
            )
            archive_helper_sha256 = hashlib.sha256(BOUNDED.read_bytes()).hexdigest()
            installed_helper_sha256 = hashlib.sha256(
                Path("/home/obj/.local/bin/gb10_bounded_process.py").read_bytes()
            ).hexdigest()
            self.assertEqual(
                archive_helper_sha256,
                engine.EXPECTED_BOUNDED_PROCESS_SHA256,
            )
            self.assertEqual(installed_helper_sha256, archive_helper_sha256)
            self.assertEqual(engine._BOUNDED_PROCESS_PATH, authority_helper)
        finally:
            sys.argv = old_argv
            for number, handler in old_handlers.items():
                signal.signal(number, handler)

        authorities = []
        source_bundle = None
        old_cgroup_root = engine.cgroup_root
        parent, child = socket.socketpair()
        try:
            setattr(engine, "operation_deadline", time.monotonic() + 1800)
            production_worker = engine._production_tool_specs()["scoped_worker"]
            tracked_worker = WORKER.resolve(strict=True)
            tracked_worker_info = tracked_worker.stat(follow_symlinks=False)
            tracked_worker_sha256 = hashlib.sha256(tracked_worker.read_bytes()).hexdigest()
            self.assertEqual(tracked_worker_sha256, production_worker.sha256)
            engine.tool_specs["scoped_worker"] = engine.ToolSpec(
                str(tracked_worker),
                str(tracked_worker),
                tracked_worker_info.st_uid,
                tracked_worker_info.st_gid,
                stat.S_IMODE(tracked_worker_info.st_mode),
                tracked_worker_sha256,
            )
            engine._open_all_tools()
            self.assertEqual(
                engine.held_tools["scoped_worker"].spec.logical, str(tracked_worker)
            )
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                scope_root = root / "non-live-scope-contract"
                scope_root.mkdir()
                scope_policy = engine._scope_policy("build").contract()
                for name, value in {
                    "cgroup.events": "populated 1\n",
                    "cgroup.procs": "2\n",
                    "cpu.max": f"{int(scope_policy['cpu_percent']) * 1000} 100000\n",
                    "memory.high": f"{scope_policy['memory_high']}\n",
                    "memory.max": f"{scope_policy['memory_max']}\n",
                    "memory.oom.group": "1\n",
                    "memory.swap.max": "0\n",
                    "pids.max": f"{scope_policy['tasks_max']}\n",
                }.items():
                    (scope_root / name).write_text(value)
                setattr(engine, "cgroup_root", scope_root)
                source = root / "source"
                crate = source / "llm-guard-proxy"
                (crate / "src").mkdir(parents=True)
                (crate / "Cargo.toml").write_text(
                    "[package]\nname='llm-guard-proxy'\nversion='0.0.0'\nedition='2021'\n"
                    "[features]\nguard=[]\n"
                )
                (crate / "src" / "main.rs").write_text("fn main() {}\n")
                (source / "Cargo.toml").write_text(
                    "[workspace]\nmembers=['llm-guard-proxy']\nresolver='2'\n"
                )
                (source / "Cargo.lock").write_text(
                    'version = 4\n\n[[package]]\nname = "llm-guard-proxy"\nversion = "0.0.0"\n'
                )
                source_bundle = engine.SourceBundle(
                    root,
                    source,
                    engine._open_directory_authority("canonical source", source),
                    engine._open_directory_authority("sysroot runtime", engine.sysroot_lib),
                    engine._open_directory_authority("Python stdlib", engine.python_stdlib),
                    "0" * 64,
                    "1" * 40,
                    "2" * 40,
                    "3" * 64,
                    "4" * 64,
                    4,
                    sum(path.stat().st_size for path in source.glob("**/*") if path.is_file()),
                )
                for name, path in (
                    ("toolchain", engine.toolchain_root),
                    ("target rustlib", engine.target_rustlib),
                    ("registry cache", engine.registry_cache),
                    ("registry index", engine.registry_index),
                    ("gcc closure", engine.gcc_root),
                    ("sysroot include", engine.sysroot_include),
                ):
                    authorities.append(engine._open_directory_authority(name, path))
                command, descriptors = engine._cargo_sandbox(
                    source_bundle,
                    authorities[0],
                    authorities[1],
                    authorities[2],
                    authorities[3],
                    authorities[4],
                    authorities[5],
                    "build",
                )
                command = engine._materialize_scope_unit(
                    command, "llm-guard-rebuild-build-" + "0" * 32 + ".scope"
                )
                self.assertNotIn(engine.SCOPE_UNIT_TOKEN, command)
                self.assertNotIn("--share-net", command)
                self.assertIn(engine.held_tools["scoped_worker"].exec_path, command)

                fence_error = []

                def satisfy_fence() -> None:
                    try:
                        self.assertEqual(parent.recv(1), b"R")
                        parent.sendall(b"G")
                    except BaseException as error:  # noqa: BLE001 - joined below.
                        fence_error.append(error)

                fence = threading.Thread(target=satisfy_fence)
                fence.start()
                result = subprocess.run(
                    command,
                    stdin=child.fileno(),
                    capture_output=True,
                    timeout=1800,
                    pass_fds=tuple(sorted(set(engine._tool_fds() + descriptors))),
                    env=engine.child_env,
                )
                fence.join(timeout=5)
                self.assertFalse(fence.is_alive())
                self.assertEqual(fence_error, [])
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                header, payload = engine._decode_frame(
                    result.stdout, "build", engine.MAX_EXECUTABLE_BYTES
                )
                self.assertEqual(header["payload_sha256"], hashlib.sha256(payload).hexdigest())
                emitted = root / "emitted-llm-guard-proxy"
                emitted.write_bytes(payload)
                emitted.chmod(0o755)
                descriptor = os.open(emitted, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    self.assertEqual(
                        engine.candidate_elf_authority(descriptor),
                        {
                            "machine": "AArch64",
                            "interpreter": "/lib/ld-linux-aarch64.so.1",
                        },
                    )
                finally:
                    os.close(descriptor)
        finally:
            child.close()
            parent.close()
            if source_bundle is not None:
                source_bundle.close()
            for authority in reversed(authorities):
                authority.close()
            for held_tool in reversed(list(engine.held_tools.values())):
                held_tool.close()
            engine.held_tools.clear()
            setattr(engine, "cgroup_root", old_cgroup_root)
            setattr(engine, "operation_deadline", None)


class ScopedWorkerGenerationAuthorityTests(unittest.TestCase):
    def test_resource_snapshot_fence_runs_when_worker_operation_fails(self) -> None:
        worker = _load(WORKER, "scoped_worker_failure_fence_test")
        with (
            patch.object(worker, "_strict_object", return_value={}),
            patch.object(worker, "_self_attest"),
            patch.object(
                worker,
                "_fetch",
                side_effect=worker.WorkerError("child-status"),
            ),
            patch.object(worker, "_resource_snapshot_fence") as fence,
            patch.object(sys, "argv", [str(WORKER), "fetch", "{}", "fixture.scope"]),
            self.assertRaisesRegex(worker.WorkerError, "child-status"),
        ):
            worker.main()
        fence.assert_called_once_with()

        worker = _load(WORKER, "scoped_worker_namespace_test")
        unit = "llm-guard-rebuild-fetch-" + "a" * 32 + ".scope"
        policy = {
            "cpu_percent": 100,
            "fsize_bytes": 1,
            "memory_high": 2,
            "memory_max": 3,
            "min_mem_available": 4,
            "phase": "fetch",
            "runtime_seconds": 5,
            "tasks_max": 6,
        }
        pid = os.getpid()
        values = {
            "/proc/self/cgroup": b"0::/\n",
            "/sys/fs/cgroup/memory.high": b"2\n",
            "/sys/fs/cgroup/memory.max": b"3\n",
            "/sys/fs/cgroup/memory.oom.group": b"1\n",
            "/sys/fs/cgroup/memory.swap.max": b"0\n",
            "/sys/fs/cgroup/pids.max": b"6\n",
            "/sys/fs/cgroup/cpu.max": b"100000 100000\n",
            "/sys/fs/cgroup/cgroup.procs": f"{pid}\n".encode(),
            "/sys/fs/cgroup/cgroup.events": b"populated 1\nfrozen 0\n",
        }

        def read(path: Path, _maximum: int = 64 * 1024) -> bytes:
            return values[str(path)]

        with patch.object(worker, "_read_small", side_effect=read):
            worker._self_attest(unit, policy)
            values["/proc/self/cgroup"] = f"0::/fixture.slice/{unit}\n".encode()
            with self.assertRaisesRegex(worker.WorkerError, "scope"):
                worker._self_attest(unit, policy)

    def test_scope_pins_writable_nofollow_cgroup_kill(self) -> None:
        bounded = _load(BOUNDED, "bounded_cgroup_kill_test")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = "/fixture.slice/llm-guard-rebuild-fetch-" + "a" * 32 + ".scope"
            scope = root / relative.lstrip("/")
            scope.mkdir(parents=True)
            for name in (
                "cgroup.events",
                "cgroup.procs",
                "cpu.max",
                "memory.events",
                "memory.high",
                "memory.max",
                "memory.oom.group",
                "memory.swap.max",
                "pids.events",
                "pids.max",
                "cgroup.kill",
            ):
                (scope / name).write_text("0\n")
            policy = bounded.ScopePolicy(
                "fetch", 1, 2, 3, 100, 4, 5, 6, Path("/proc"), root
            )
            authority = bounded._open_scope_cgroup(policy, relative)
            try:
                descriptor = authority.descriptors["cgroup.kill"]
                self.assertEqual(
                    fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE,
                    os.O_WRONLY,
                )
            finally:
                authority.close()


class ScopeLifecycleRegressionTests(unittest.TestCase):
    def assert_failed(self, fixture: RebuildFixture, result: subprocess.CompletedProcess[str]) -> str:
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertNotIn("LLM_GUARD_PROXY_REBUILD_TEST_ONLY_COMPLETE", output)
        return output

    def test_nonzero_pre_go_resource_counters_keep_gate_closed(self) -> None:
        for event in ("memory", "oom-kill", "pids"):
            with self.subTest(event=event), RebuildFixture() as fixture:
                fixture.set_state(scope_pre_go_resource_event=event)
                output = self.assert_failed(fixture, fixture.run())
                self.assertIn("resource", output.lower())
                self.assertNotIn("payload ", fixture.calls())

    def test_pre_go_starttime_reuse_closes_gate_before_payload(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(scope_pre_go_identity_drift="starttime")
            output = self.assert_failed(fixture, fixture.run(timeout=8))
            self.assertIn("before GO", output)
            self.assertNotIn("payload ", fixture.calls())

    def test_worker_starttime_cgroup_and_liveness_drift_fail_while_wrapper_lives(self) -> None:
        for drift in ("starttime", "cgroup", "loss"):
            with self.subTest(drift=drift), RebuildFixture() as fixture:
                fixture.set_state(scope_runtime_identity_drift=drift)
                process = fixture.popen()
                started = time.monotonic()
                try:
                    deadline = started + 6
                    while process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    if process.poll() is None:
                        _kill_group(process)
                        self.fail(f"worker {drift} drift waited for the phase deadline")
                    stdout, stderr = process.communicate(timeout=2)
                finally:
                    if process.poll() is None:
                        _kill_group(process)
                output = stdout + stderr
                self.assertNotEqual(process.returncode, 0, output)
                self.assertLess(time.monotonic() - started, 6)
                self.assertTrue(fixture.reload_state()["scope_identity_drifted"])
                self.assertIn("identity", output.lower())
                if drift == "cgroup":
                    self.assertTrue(
                        fixture.reload_state()["scope_moved_worker_pidfd_reaped"]
                    )

    def test_reused_foreign_scope_generation_is_not_signalled(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(
                scope_failure="live-output",
                scope_reuse_on_kill_entry=True,
            )
            result = fixture.run()
            self.assert_failed(fixture, result)
            self.assertFalse(fixture.reload_state()["scope_foreign_signalled"])
            self.assertNotRegex(
                fixture.calls(),
                r"(?m)^systemctl --user kill .*llm-guard-rebuild-fetch-",
            )

    def _exercise_cleanup_nonzero_and_timeout_after_process_cleanup(self) -> None:
        for failure in ("nonzero", "timeout"):
            with self.subTest(failure=failure), RebuildFixture() as fixture:
                fixture.set_state(
                    scope_failure="live-output",
                    scope_cleanup_failure=failure,
                )
                output = self.assert_failed(fixture, fixture.run(timeout=12))
                self.assertIn("scoped output exceeded bound", output)
                diagnostic = (
                    "scope cgroup KILL failed"
                    if failure == "nonzero"
                    else "scope cgroup KILL did not quiesce"
                )
                self.assertIn(diagnostic, output)
                if failure == "nonzero":
                    self.assertTrue(
                        fixture.reload_state()[
                            "scope_pidfd_reaped_after_cgroup_failure"
                        ]
                    )
                self.assertNotRegex(output, r"llm-guard-rebuild-[a-z]+-[0-9a-f]{32}")
                self.assertNotIn("systemctl --user kill", fixture.calls())

        for state, reasons in (
            (
                {
                    "replace_bwrap_during_use": True,
                    "scope_resource_event": "memory",
                },
                (
                    "scope resource limit was reached",
                    "held tool pathname or metadata changed: bwrap",
                ),
            ),
            (
                {
                    "replace_bwrap_during_use": True,
                    "scope_cleanup_failure": "nonzero",
                    "scope_failure": "live-output",
                },
                (
                    "scope cgroup KILL failed",
                    "held tool pathname or metadata changed: bwrap",
                ),
            ),
        ):
            with self.subTest(combined=state), RebuildFixture() as fixture:
                fixture.set_state(**state)
                output = self.assert_failed(fixture, fixture.run(timeout=12))
                for reason in reasons:
                    self.assertIn(reason, output)

        with RebuildFixture() as fixture:
            fixture.set_state(scope_collect_immediate=True)
            completed = fixture.run(timeout=12)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(
                fixture.reload_state()["scope_resource_snapshot_fenced"],
                "fake collection did not wait for the final held-controller snapshot",
            )

        policy = types.SimpleNamespace(cgroup_root=Path("/unused"), proc_root=Path("/unused"))
        cgroup = types.SimpleNamespace(descriptors={"cgroup.kill": 91})
        bounded = _load(BOUNDED, "bounded_fixed_cleanup_independence_test")
        sent: list[tuple[int, int]] = []
        with (
            patch.object(bounded, "_scope_quiescent", side_effect=[False, True, True]),
            patch.object(bounded.os, "write", side_effect=OSError("forced controller failure")),
            patch.object(
                bounded.signal,
                "pidfd_send_signal",
                side_effect=lambda descriptor, number, *_: sent.append((descriptor, number)),
            ),
            self.assertRaisesRegex(bounded.BoundedProcessError, "scope cgroup KILL failed"),
        ):
            bounded._scope_signal(
                policy,
                "fixture.scope",
                123,
                456,
                cgroup,
                77,
                signal.SIGKILL,
                time.monotonic() + 1,
            )
        self.assertEqual(sent, [(77, signal.SIGKILL)])

        class InjectedRebuildError(RuntimeError):
            pass

        events: list[tuple[str, int]] = []
        quiescence_checks = 0

        def quiescent(*_arguments: object) -> bool:
            nonlocal quiescence_checks
            quiescence_checks += 1
            return quiescence_checks > 1

        def write_boundary(_descriptor: int, _payload: bytes) -> int:
            events.append(("cgroup", signal.SIGKILL))
            raise InjectedRebuildError("cgroup write boundary interrupted")

        class WrapperTree:
            def signal_group(self, number: int) -> None:
                events.append(("wrapper", number))

        def fixed_scope(number: int) -> None:
            bounded._scope_signal(
                policy,
                "fixture.scope",
                123,
                456,
                cgroup,
                77,
                number,
                time.monotonic() + 1,
            )

        with (
            patch.object(bounded, "_scope_quiescent", side_effect=quiescent),
            patch.object(bounded.os, "write", side_effect=write_boundary),
            patch.object(
                bounded.signal,
                "pidfd_send_signal",
                side_effect=lambda _descriptor, number, *_: events.append(("worker", number)),
            ),
            self.assertRaises(bounded.BoundedProcessError),
        ):
            bounded._signal_cleanup(WrapperTree(), fixed_scope, signal.SIGKILL)
        self.assertEqual(
            events[:3],
            [
                ("cgroup", signal.SIGKILL),
                ("worker", signal.SIGKILL),
                ("wrapper", signal.SIGKILL),
            ],
        )

        held_removed = bounded._ScopeCgroup(
            "/fixture.slice/removed.scope",
            {"directory": 90, "cgroup.events": 91},
        )
        with (
            patch.object(bounded.os, "fstat", return_value=types.SimpleNamespace(st_nlink=1)),
            patch.object(
                bounded.os,
                "lseek",
                side_effect=OSError(errno.ENODEV, "held cgroup removed"),
            ),
            self.assertRaises(bounded.BoundedProcessError) as removed,
        ):
            held_removed.read("cgroup.events")
        self.assertEqual(removed.exception.errno, errno.ENODEV)

        def removed_read(_name: str) -> bytes:
            raise bounded.BoundedProcessError(
                "scope controller read failed", error_number=errno.ENODEV
            )

        removed_cgroup = types.SimpleNamespace(
            relative_path="/fixture.slice/removed.scope", read=removed_read
        )
        with (
            patch.object(bounded, "_proc_row_under", return_value=None),
            patch.object(
                bounded.signal,
                "pidfd_send_signal",
                side_effect=ProcessLookupError,
            ),
        ):
            self.assertTrue(
                bounded._scope_quiescent(
                    policy, 123, 456, 77, removed_cgroup
                ),
                "terminal held worker plus ENODEV must classify the original scope as removed",
            )
        with (
            patch.object(bounded, "_proc_row_under", return_value=None),
            patch.object(bounded.signal, "pidfd_send_signal", return_value=None),
        ):
            self.assertFalse(
                bounded._scope_quiescent(policy, 123, 456, 77, removed_cgroup),
                "ENODEV must not forge quiescence while the held worker pidfd is live",
            )

        def unreadable(_name: str) -> bytes:
            raise bounded.BoundedProcessError("held controller read failed")

        with (
            patch.object(bounded, "_proc_row_under", return_value=None),
            patch.object(
                bounded.signal,
                "pidfd_send_signal",
                side_effect=ProcessLookupError,
            ),
        ):
            self.assertFalse(
                bounded._scope_quiescent(
                    policy,
                    123,
                    456,
                    77,
                    types.SimpleNamespace(relative_path="/gone.scope", read=unreadable),
                ),
                "a generic missing cgroup name must not replace held event/proc proof",
            )

        bounded = _load(BOUNDED, "bounded_manager_cancellation_test")
        real_popen = subprocess.Popen
        children: list[subprocess.Popen] = []

        def spawn(*args: object, **kwargs: object) -> subprocess.Popen:
            child = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            children.append(child)

            def interrupted(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
                raise KeyboardInterrupt

            child.communicate = interrupted  # type: ignore[method-assign]
            return child

        try:
            with patch.object(bounded.subprocess, "Popen", side_effect=spawn):
                with self.assertRaises(KeyboardInterrupt):
                    bounded._scope_manager_command(
                        sys.executable,
                        (),
                        ["-c", "import time; time.sleep(30)"],
                        "cancellation control",
                    )
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll(), "manager helper survived cancellation")
            self.assertFalse(Path(f"/proc/{children[0].pid}").exists())
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)

    def test_failed_worker_resource_events_survive_fence_or_collection(self) -> None:
        self._exercise_cleanup_nonzero_and_timeout_after_process_cleanup()
        for failure, event, fenced, classification in (
            ("payload", "pids", True, "pids.max"),
            ("oom-kill", "memory", False, "memory.oom_kill"),
        ):
            with self.subTest(failure=failure), RebuildFixture() as fixture:
                fixture.set_state(
                    replace_bwrap_during_use=True,
                    scope_collect_immediate=True,
                    scope_failure=failure,
                    scope_resource_event=event,
                )
                output = self.assert_failed(fixture, fixture.run(timeout=12))
                self.assertIn("scoped payload failed", output)
                self.assertIn("scope resource limit was reached", output)
                self.assertIn(classification, output)
                self.assertIn("held tool pathname or metadata changed: bwrap", output)
                self.assertEqual(
                    fixture.reload_state()["scope_resource_snapshot_fenced"], fenced
                )

        with RebuildFixture() as fixture:
            fixture.set_state(
                replace_bwrap_during_use=True,
                scope_cleanup_failure="nonzero",
                scope_failure="live-output",
                scope_resource_event="memory",
            )
            output = self.assert_failed(fixture, fixture.run(timeout=12))
            for diagnostic in (
                "scoped output exceeded bound",
                "scope resource limit was reached",
                "memory.oom_kill",
                "scope cgroup KILL failed",
                "held tool pathname or metadata changed: bwrap",
            ):
                self.assertIn(diagnostic, output)


class CrashSafeHostWriteTests(unittest.TestCase):
    def _crash_and_retry(self, point: str) -> None:
        with RebuildFixture() as fixture:
            if point == "scratch-reuse-published":
                primed = fixture.run(timeout=30)
                self.assertEqual(
                    primed.returncode, 0, primed.stdout + primed.stderr
                )
            marker = fixture.root / ("crash-" + point)
            process = fixture.popen(
                extra_env={
                    "LLM_GUARD_REBUILD_TEST_CRASH_POINT": point,
                    "LLM_GUARD_REBUILD_TEST_CRASH_MARKER": str(marker),
                }
            )
            try:
                self.assertTrue(_wait_for_path(marker, process), f"boundary not reached: {point}")
            finally:
                stdout, stderr = _kill_group(process)
            self.assertEqual(process.returncode, -signal.SIGKILL, stdout + stderr)
            recovered = fixture.run(timeout=30)
            output = recovered.stdout + recovered.stderr
            if recovered.returncode == 75:
                resumed = fixture.run(timeout=30)
                output += resumed.stdout + resumed.stderr
                recovered = resumed
            self.assertEqual(recovered.returncode, 0, output)
            self.assertFalse(
                any(
                    path.name.startswith(
                        (
                            ".rebuild-input-",
                            "..rebuild-input-",
                        )
                    )
                    for path in fixture.cache_root.iterdir()
                ),
                output,
            )
            self.assertEqual(
                len(list(fixture.cache_root.glob(".gb10-rebuild-scratch-delete.v1.*"))),
                1,
                output,
            )
            self.assertTrue(
                (fixture.cache_root / ".gb10-rebuild-scratch-delete-slot.v1").is_dir(),
                output,
            )
            release_temps = list((fixture.cache_root / "releases").glob("**/*.tmp.*"))
            self.assertEqual(release_temps, [], output)

    def test_scratch_prepublication_and_empty_tombstone_are_restart_safe(self) -> None:
        def load_engine(fixture: RebuildFixture, name: str) -> types.ModuleType:
            old_argv = sys.argv[:]
            old_handlers = {
                number: signal.getsignal(number)
                for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }
            try:
                with patch.dict(os.environ, fixture.env, clear=False):
                    sys.argv = [str(ENGINE), "--test-only"]
                    return _load(ENGINE, name)
            finally:
                sys.argv = old_argv
                for number, handler in old_handlers.items():
                    signal.signal(number, handler)

        def open_budget(engine: types.ModuleType, fixture: RebuildFixture):
            budget = engine.HostWriteBudget(fixture.cache_root)
            setattr(
                engine,
                "test_free_bytes",
                engine.HOST_FREE_FLOOR_BYTES + engine.HOST_WRITE_BUDGET_BYTES,
            )
            budget.ensure_private_directory(fixture.cache_root)
            return budget

        def completed_record(
            engine: types.ModuleType,
            fixture: RebuildFixture,
            budget,
            digit: str,
        ):
            parent_fd = budget.ensure_private_directory(fixture.cache_root)
            target = fixture.cache_root / (".rebuild-input-" + digit * 32)
            target.mkdir(mode=0o700)
            metadata = target.stat(follow_symlinks=False)
            identity = (metadata.st_dev, metadata.st_ino)
            record = engine._prepare_scratch_delete(
                budget, parent_fd, target.name, identity
            )
            target_fd, _ = engine._open_scratch_leaf(
                parent_fd,
                target.name,
                expected_identity=identity,
                allow_marker=False,
            )
            try:
                engine._finish_scratch_delete(
                    budget, parent_fd, target.name, target_fd, identity, record
                )
            finally:
                os.close(target_fd)
            self.assertFalse(target.exists())
            return parent_fd, record, identity

        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            tombstone = fixture.cache_root / (
                "..rebuild-input-" + "b" * 32 + ".cleanup.1234.cafebabe"
            )
            tombstone.mkdir(mode=0o700)
            replacement = fixture.cache_root / "replacement-tombstone"
            replacement.mkdir(mode=0o700)
            replacement_payload = replacement / "payload.bin"
            replacement_bytes = b"replacement race payload\x00must survive\n"
            replacement_payload.write_bytes(replacement_bytes)
            saved = fixture.cache_root / "original-tombstone"
            engine = load_engine(fixture, "markerless_tombstone_race_test")
            budget = open_budget(engine, fixture)
            metadata = tombstone.stat(follow_symlinks=False)
            parent_fd = budget.ensure_private_directory(fixture.cache_root)
            engine._prepare_scratch_delete(
                budget,
                parent_fd,
                tombstone.name,
                (metadata.st_dev, metadata.st_ino),
            )
            records = list(
                fixture.cache_root.glob(".gb10-rebuild-scratch-delete.v1.*")
            )
            self.assertEqual(len(records), 1)
            real_rename = os.rename
            real_exchange = engine._rename_exchange
            swapped = False

            def replace_after_check(
                parent: int, source: str, destination: str
            ) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    real_rename(tombstone, saved)
                    real_rename(replacement, tombstone)
                real_exchange(parent, source, destination)

            with (
                patch.object(engine, "_rename_exchange", side_effect=replace_after_check),
                self.assertRaises(engine.RebuildError) as raised,
            ):
                engine._remove_empty_markerless_tombstone(tombstone, budget)
            self.assertTrue(swapped, str(raised.exception))
            self.assertTrue(tombstone.is_dir())
            self.assertEqual((tombstone / "payload.bin").read_bytes(), replacement_bytes)
            self.assertEqual(list(saved.iterdir()), [])
            self.assertNotEqual(
                (records[0].stat().st_dev, records[0].stat().st_ino),
                (metadata.st_dev, metadata.st_ino),
            )
            budget.close()

        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            tombstone = fixture.cache_root / (
                "..rebuild-input-" + "c" * 32 + ".cleanup.1234.feedface"
            )
            tombstone.mkdir(mode=0o700)
            engine = load_engine(fixture, "markerless_tombstone_unavailable_test")
            budget = open_budget(engine, fixture)
            metadata = tombstone.stat(follow_symlinks=False)
            parent_fd = budget.ensure_private_directory(fixture.cache_root)
            engine._prepare_scratch_delete(
                budget,
                parent_fd,
                tombstone.name,
                (metadata.st_dev, metadata.st_ino),
            )
            with (
                patch.object(
                    engine,
                    "_rename_exchange",
                    side_effect=engine.RebuildError(
                        "scratch exact-leaf exchange is unavailable"
                    ),
                ),
                self.assertRaisesRegex(engine.RebuildError, "unavailable"),
            ):
                engine._remove_empty_markerless_tombstone(tombstone, budget)
            self.assertTrue(tombstone.is_dir())
            self.assertEqual(
                len(list(fixture.cache_root.glob(".gb10-rebuild-scratch-delete.v1.*"))),
                1,
            )
            budget.close()

        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            tombstone = fixture.cache_root / (
                "..rebuild-input-" + "d" * 32 + ".cleanup.1234.0123abcd"
            )
            tombstone.mkdir(mode=0o700)
            engine = load_engine(fixture, "markerless_tombstone_positive_test")
            budget = open_budget(engine, fixture)
            metadata = tombstone.stat(follow_symlinks=False)
            parent_fd = budget.ensure_private_directory(fixture.cache_root)
            engine._prepare_scratch_delete(
                budget,
                parent_fd,
                tombstone.name,
                (metadata.st_dev, metadata.st_ino),
            )
            engine._remove_empty_markerless_tombstone(tombstone, budget)
            self.assertFalse(tombstone.exists())
            self.assertEqual(
                len(list(fixture.cache_root.glob(".gb10-rebuild-scratch-delete.v1.*"))),
                1,
            )
            self.assertTrue(
                (fixture.cache_root / ".gb10-rebuild-scratch-delete-slot.v1").is_dir()
            )
            budget.close()

        prepublication_payload = b"foreign prepublication payload\x00must survive\n"
        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            prepublication = fixture.cache_root / (
                "..rebuild-input-" + "e" * 32 + ".publish.1234.89abcdef"
            )
            prepublication.mkdir(mode=0o700)
            engine = load_engine(fixture, "markerless_prepublication_race_test")
            budget = open_budget(engine, fixture)
            parent_fd = budget.ensure_private_directory(fixture.cache_root)
            metadata = prepublication.stat(follow_symlinks=False)
            real_prepare = engine._prepare_scratch_delete
            injected = False

            def inject_prepublication(
                *arguments: object, **keywords: object
            ):
                nonlocal injected
                injected = True
                (prepublication / "foreign.bin").write_bytes(prepublication_payload)
                return real_prepare(*arguments, **keywords)

            with (
                patch.object(
                    engine,
                    "_prepare_scratch_delete",
                    side_effect=inject_prepublication,
                ),
                self.assertRaises(engine.RebuildError),
            ):
                engine._remove_prepublication_scratch(
                    budget,
                    parent_fd,
                    prepublication.name,
                    (metadata.st_dev, metadata.st_ino),
                )
            self.assertTrue(injected)
            self.assertEqual(
                (prepublication / "foreign.bin").read_bytes(), prepublication_payload
            )
            self.assertEqual(
                list(fixture.cache_root.glob(".gb10-rebuild-scratch-delete.v1.*")),
                [],
            )
            budget.close()

        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            tombstone = fixture.cache_root / (
                "..rebuild-input-" + "f" * 32 + ".cleanup.1234.abcdef01"
            )
            tombstone.mkdir(mode=0o700)
            foreign = fixture.cache_root / "foreign-placeholder-replacement"
            foreign.mkdir(mode=0o700)
            os.setxattr(foreign, "user.gb10-test", b"placeholder-foreign-metadata")
            foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)
            saved_placeholder = fixture.cache_root / "saved-placeholder"
            engine = load_engine(fixture, "post_exchange_placeholder_race_test")
            budget = open_budget(engine, fixture)
            parent_fd = budget.ensure_private_directory(fixture.cache_root)
            target = tombstone.stat(follow_symlinks=False)
            target_identity = (target.st_dev, target.st_ino)
            record = engine._prepare_scratch_delete(
                budget, parent_fd, tombstone.name, target_identity
            )
            real_boundary = engine._test_boundary
            replaced = False

            def replace_placeholder(
                point: str,
            ) -> None:
                nonlocal replaced
                if point == "scratch-delete-placeholder-validated" and not replaced:
                    replaced = True
                    os.rename(tombstone, saved_placeholder)
                    os.rename(foreign, tombstone)
                real_boundary(point)

            with (
                patch.object(
                    engine, "_test_boundary", side_effect=replace_placeholder
                ),
                self.assertRaises(engine.RebuildError),
            ):
                engine._remove_empty_markerless_tombstone(tombstone, budget)
            self.assertTrue(replaced)
            self.assertEqual(
                (tombstone.stat().st_dev, tombstone.stat().st_ino), foreign_identity
            )
            self.assertEqual(
                os.getxattr(tombstone, "user.gb10-test"),
                b"placeholder-foreign-metadata",
            )
            record_path = fixture.cache_root / record.name
            self.assertEqual(
                (record_path.stat().st_dev, record_path.stat().st_ino), target_identity
            )
            budget.close()

        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            tombstone = fixture.cache_root / (
                "..rebuild-input-" + "1" * 32 + ".cleanup.1234.abcdef02"
            )
            tombstone.mkdir(mode=0o700)
            foreign = fixture.cache_root / "foreign-record-replacement"
            foreign.mkdir(mode=0o700)
            os.setxattr(foreign, "user.gb10-test", b"record-foreign-metadata")
            foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)
            saved_target = fixture.cache_root / "saved-original-target"
            engine = load_engine(fixture, "post_placeholder_record_race_test")
            budget = open_budget(engine, fixture)
            parent_fd = budget.ensure_private_directory(fixture.cache_root)
            target = tombstone.stat(follow_symlinks=False)
            target_identity = (target.st_dev, target.st_ino)
            record = engine._prepare_scratch_delete(
                budget, parent_fd, tombstone.name, target_identity
            )
            real_boundary = engine._test_boundary
            replaced = False

            def replace_record(
                point: str,
            ) -> None:
                nonlocal replaced
                if point == "scratch-delete-record-validated" and not replaced:
                    replaced = True
                    os.rename(fixture.cache_root / record.name, saved_target)
                    os.rename(foreign, fixture.cache_root / record.name)
                real_boundary(point)

            with (
                patch.object(engine, "_test_boundary", side_effect=replace_record),
                self.assertRaises(engine.RebuildError),
            ):
                engine._remove_empty_markerless_tombstone(tombstone, budget)
            self.assertTrue(replaced)
            record_path = fixture.cache_root / record.name
            self.assertEqual(
                (record_path.stat().st_dev, record_path.stat().st_ino), foreign_identity
            )
            self.assertEqual(
                os.getxattr(record_path, "user.gb10-test"),
                b"record-foreign-metadata",
            )
            self.assertEqual(
                (saved_target.stat().st_dev, saved_target.stat().st_ino), target_identity
            )
            record_before = (
                record_path.lstat().st_dev,
                record_path.lstat().st_ino,
                os.getxattr(record_path, "user.gb10-test"),
            )
            saved_before = (
                saved_target.lstat().st_dev,
                saved_target.lstat().st_ino,
            )
            with self.assertRaisesRegex(engine.RebuildError, "preserved"):
                engine._recover_scratch_delete(budget, parent_fd, record)
            self.assertEqual(
                (
                    record_path.lstat().st_dev,
                    record_path.lstat().st_ino,
                    os.getxattr(record_path, "user.gb10-test"),
                ),
                record_before,
            )
            self.assertEqual(
                (saved_target.lstat().st_dev, saved_target.lstat().st_ino),
                saved_before,
            )
            budget.close()

        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            engine = load_engine(fixture, "scratch_reuse_intermediate_recovery_test")
            budget = open_budget(engine, fixture)
            parent_fd, record, target_identity = completed_record(
                engine, fixture, budget, "2"
            )
            engine._rename_exchange(
                parent_fd, record.name, engine.SCRATCH_DELETE_SLOT
            )
            os.fsync(parent_fd)
            self.assertEqual(
                (
                    (fixture.cache_root / record.name).stat().st_dev,
                    (fixture.cache_root / record.name).stat().st_ino,
                ),
                record.placeholder_identity,
            )
            self.assertEqual(
                (
                    (fixture.cache_root / engine.SCRATCH_DELETE_SLOT).stat().st_dev,
                    (fixture.cache_root / engine.SCRATCH_DELETE_SLOT).stat().st_ino,
                ),
                target_identity,
            )
            engine._recover_scratch_delete(budget, parent_fd, record)
            self.assertEqual(
                (
                    (fixture.cache_root / record.name).stat().st_dev,
                    (fixture.cache_root / record.name).stat().st_ino,
                ),
                target_identity,
            )
            self.assertEqual(
                (
                    (fixture.cache_root / engine.SCRATCH_DELETE_SLOT).stat().st_dev,
                    (fixture.cache_root / engine.SCRATCH_DELETE_SLOT).stat().st_ino,
                ),
                record.placeholder_identity,
            )
            budget.close()

        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            engine = load_engine(fixture, "scratch_reuse_single_transition_test")
            budget = open_budget(engine, fixture)
            parent_fd, record, target_identity = completed_record(
                engine, fixture, budget, "3"
            )
            destination = fixture.cache_root / (
                "..rebuild-input-" + "4" * 32 + ".publish.1234.abcdef04"
            )
            with patch.object(
                engine,
                "_rename_exchange",
                side_effect=AssertionError("reuse must not exchange record and slot"),
            ):
                reused = engine._reuse_completed_scratch(
                    budget, parent_fd, destination
                )
            self.assertEqual(reused, record.placeholder_identity)
            self.assertEqual(
                (destination.stat().st_dev, destination.stat().st_ino),
                record.placeholder_identity,
            )
            self.assertFalse((fixture.cache_root / record.name).exists())
            self.assertFalse(
                (fixture.cache_root / engine.SCRATCH_DELETE_SLOT).exists()
            )
            budget.close()

        def leaf_snapshot(path: Path) -> tuple[object, ...]:
            metadata = path.lstat()
            kind = stat.S_IFMT(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                payload: object = tuple(
                    sorted(
                        (child.name, child.read_bytes())
                        for child in path.iterdir()
                    )
                )
            elif stat.S_ISLNK(metadata.st_mode):
                payload = os.readlink(path)
            elif stat.S_ISREG(metadata.st_mode):
                payload = path.read_bytes()
            else:
                payload = None
            return (
                kind,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_nlink,
                payload,
            )

        for replacement_kind in (
            "directory",
            "regular",
            "symlink",
            "hardlink",
            "fifo",
        ):
            with self.subTest(reuse_replacement=replacement_kind), RebuildFixture() as fixture:
                fixture.cache_root.mkdir(mode=0o700)
                engine = load_engine(
                    fixture, f"scratch_reuse_{replacement_kind}_replacement_test"
                )
                budget = open_budget(engine, fixture)
                parent_fd, record, target_identity = completed_record(
                    engine, fixture, budget, "5"
                )
                record_path = fixture.cache_root / record.name
                slot_path = fixture.cache_root / engine.SCRATCH_DELETE_SLOT
                saved_placeholder = fixture.cache_root / "saved-reuse-placeholder"
                foreign = fixture.cache_root / "foreign-reuse-slot"
                external = fixture.cache_root / "external-hardlink-source"
                if replacement_kind == "directory":
                    foreign.mkdir(mode=0o700)
                    (foreign / "payload").write_bytes(b"foreign directory\x00")
                elif replacement_kind == "regular":
                    foreign.write_bytes(b"foreign regular\x00")
                elif replacement_kind == "symlink":
                    foreign.symlink_to("foreign-symlink-target")
                elif replacement_kind == "hardlink":
                    external.write_bytes(b"foreign hardlink\x00")
                    os.link(external, foreign)
                else:
                    os.mkfifo(foreign, 0o600)
                foreign_before = leaf_snapshot(foreign)
                destination = fixture.cache_root / (
                    "..rebuild-input-" + "6" * 32 + ".publish.1234.abcdef05"
                )
                real_boundary = engine._test_boundary
                injected = False

                def replace_reuse_slot(point: str) -> None:
                    nonlocal injected
                    if point == "host-leaf-move-validated" and not injected:
                        injected = True
                        os.rename(slot_path, saved_placeholder)
                        os.rename(foreign, slot_path)
                    real_boundary(point)

                with (
                    patch.object(
                        engine,
                        "_test_boundary",
                        side_effect=replace_reuse_slot,
                    ),
                    self.assertRaises(engine.RebuildError),
                ):
                    engine._reuse_completed_scratch(
                        budget, parent_fd, destination
                    )
                self.assertTrue(injected)
                self.assertFalse(destination.exists())
                self.assertEqual(leaf_snapshot(slot_path), foreign_before)
                self.assertEqual(
                    (
                        saved_placeholder.stat().st_dev,
                        saved_placeholder.stat().st_ino,
                    ),
                    record.placeholder_identity,
                )
                self.assertEqual(
                    (record_path.stat().st_dev, record_path.stat().st_ino),
                    target_identity,
                )
                before_recovery = (
                    leaf_snapshot(slot_path),
                    (record_path.stat().st_dev, record_path.stat().st_ino),
                )
                with self.assertRaisesRegex(engine.RebuildError, "preserved"):
                    engine._recover_scratch_delete(budget, parent_fd, record)
                self.assertEqual(
                    (
                        leaf_snapshot(slot_path),
                        (record_path.stat().st_dev, record_path.stat().st_ino),
                    ),
                    before_recovery,
                )
                budget.close()

        for point in (
            "scratch-marker-directory-fsynced",
            "scratch-prepublication",
            "scratch-published",
            "scratch-tombstone-renamed",
            "scratch-delete-record-created",
            "scratch-marker-removed",
            "scratch-delete-exchanged",
            "scratch-delete-placeholder-removed",
            "scratch-delete-target-removed",
            "scratch-reuse-published",
        ):
            with self.subTest(point=point):
                self._crash_and_retry(point)

        with RebuildFixture() as fixture:
            fixture.cache_root.mkdir(mode=0o700)
            tombstone = fixture.cache_root / (
                "..rebuild-input-" + "a" * 32 + ".cleanup.1234.deadbeef"
            )
            payload = tombstone / "foreign" / "payload.bin"
            payload.parent.mkdir(parents=True, mode=0o700)
            expected = b"markerless replacement payload\x00must survive\n"
            payload.write_bytes(expected)
            result = fixture.run()
            self.assertTrue(tombstone.is_dir(), result.stdout + result.stderr)
            self.assertEqual(payload.read_bytes(), expected)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_candidate_write_and_validation_interruptions_leave_no_release_temp(self) -> None:
        for point in ("candidate-written", "candidate-validated"):
            with self.subTest(point=point):
                self._crash_and_retry(point)

    def test_candidate_validation_error_is_recovered_by_scratch_cleanup(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(scope_build_payload="invalid-elf")
            result = fixture.run()
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            release_temps = list((fixture.cache_root / "releases").glob("**/*.tmp.*"))
            self.assertEqual(release_temps, [])
            self.assertFalse(
                any(
                    path.name.startswith(
                        (
                            ".rebuild-input-",
                            "..rebuild-input-",
                        )
                    )
                    for path in fixture.cache_root.iterdir()
                ),
                result.stdout + result.stderr,
            )
            self.assertEqual(
                len(list(fixture.cache_root.glob(".gb10-rebuild-scratch-delete.v1.*"))),
                1,
            )
            self.assertTrue(
                (fixture.cache_root / ".gb10-rebuild-scratch-delete-slot.v1").is_dir()
            )

    def test_write_budget_tracks_actual_destination_device(self) -> None:
        with RebuildFixture() as fixture:
            fixture.test_free_bytes = 8 * 1024 * 1024 * 1024 - 1
            prior_target = os.readlink(fixture.service_bin)
            prior_bytes = fixture.prior.read_bytes()
            result = fixture.run()
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(fixture.receipt_dir.exists())
            self.assertFalse(fixture.cache_root.exists())
            self.assertEqual(os.readlink(fixture.service_bin), prior_target)
            self.assertEqual(fixture.prior.read_bytes(), prior_bytes)

        with RebuildFixture() as fixture:
            old_argv = sys.argv[:]
            old_handlers = {
                number: signal.getsignal(number)
                for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }
            try:
                with patch.dict(os.environ, fixture.env, clear=False):
                    sys.argv = [str(ENGINE), "--test-only"]
                    engine = _load(ENGINE, "rebuild_budget_device_test")
            finally:
                sys.argv = old_argv
                for number, handler in old_handlers.items():
                    signal.signal(number, handler)

            fixture.cache_root.mkdir(mode=0o700)
            fixture.receipt_dir.mkdir(mode=0o700)
            real_fstat = os.fstat
            cache_identity = (
                fixture.cache_root.stat().st_dev,
                fixture.cache_root.stat().st_ino,
            )
            receipt_identity = (
                fixture.receipt_dir.stat().st_dev,
                fixture.receipt_dir.stat().st_ino,
            )
            service_identity = (
                fixture.service_bin.parent.stat().st_dev,
                fixture.service_bin.parent.stat().st_ino,
            )

            def modeled_fstat(descriptor: int, state_device: int = 2) -> object:
                metadata = real_fstat(descriptor)
                identity = (metadata.st_dev, metadata.st_ino)
                device = {
                    cache_identity: 1,
                    receipt_identity: state_device,
                    service_identity: 3,
                }.get(identity, metadata.st_dev)
                return types.SimpleNamespace(
                    st_dev=device,
                    st_ino=metadata.st_ino,
                    st_mode=metadata.st_mode,
                    st_uid=metadata.st_uid,
                    st_nlink=metadata.st_nlink,
                    st_size=metadata.st_size,
                )

            def run_case(state_device: int) -> Exception | None:
                budget = engine.HostWriteBudget(fixture.cache_root)
                setattr(engine, "test_free_bytes", engine.HOST_FREE_FLOOR_BYTES + 100)
                try:
                    with patch.object(
                        engine.os,
                        "fstat",
                        side_effect=lambda descriptor: modeled_fstat(
                            descriptor, state_device
                        ),
                    ):
                        budget.reserve(fixture.cache_root / "candidate", 60)
                        try:
                            budget.reserve(fixture.receipt_dir / "state.json", 50)
                        except Exception as error:  # noqa: BLE001 - exact disposition.
                            return error
                    return None
                finally:
                    budget.close()

            self.assertIsInstance(run_case(1), engine.RebuildError)
            self.assertIsNone(run_case(2))

            budget = engine.HostWriteBudget(fixture.cache_root)
            setattr(engine, "test_free_bytes", engine.HOST_FREE_FLOOR_BYTES + 4096)
            with patch.object(engine.os, "fstat", side_effect=modeled_fstat):
                budget.reserve(fixture.service_bin, 0)
                engine.set_service_link(str(fixture.wrong_hash), budget)
                self.assertEqual(
                    budget.used_by_device[3], len(str(fixture.wrong_hash).encode())
                )
            budget.close()

            budget = engine.HostWriteBudget(fixture.cache_root)
            setattr(engine, "test_free_bytes", engine.HOST_FREE_FLOOR_BYTES + 4096)
            budget.reserve(fixture.service_bin, 0)
            original_parent = fixture.root / "held-service-parent"
            fixture.service_bin.parent.rename(original_parent)
            fixture.service_bin.parent.mkdir(mode=0o700)
            foreign = fixture.service_bin
            foreign_bytes = b"foreign replacement parent\x00must survive\n"
            foreign.write_bytes(foreign_bytes)
            engine.set_service_link(str(fixture.wrong_hash), budget)
            self.assertEqual(foreign.read_bytes(), foreign_bytes)
            self.assertEqual(
                os.readlink(original_parent / fixture.service_bin.name),
                str(fixture.wrong_hash),
            )
            budget.close()

            budget = engine.HostWriteBudget(fixture.cache_root)
            setattr(engine, "test_free_bytes", None)
            budget.reserve(fixture.cache_root, 0)
            held_cache = fixture.root / "held-cache-root"
            fixture.cache_root.rename(held_cache)
            fixture.cache_root.mkdir(mode=0o700)
            foreign_prepublication = fixture.cache_root / (
                "..rebuild-input-" + "2" * 32 + ".publish.1234.abcdef03"
            )
            foreign_prepublication.mkdir(mode=0o700)
            held_identity = (held_cache.stat().st_dev, held_cache.stat().st_ino)
            foreign_identity = (
                fixture.cache_root.stat().st_dev,
                fixture.cache_root.stat().st_ino,
            )

            def replacement_fstat(descriptor: int) -> object:
                metadata = real_fstat(descriptor)
                identity = (metadata.st_dev, metadata.st_ino)
                device = {
                    held_identity: 41,
                    foreign_identity: 42,
                }.get(identity, metadata.st_dev)
                return types.SimpleNamespace(
                    st_dev=device,
                    st_ino=metadata.st_ino,
                    st_mode=metadata.st_mode,
                    st_uid=metadata.st_uid,
                    st_nlink=metadata.st_nlink,
                    st_size=metadata.st_size,
                )

            def replacement_fstatvfs(descriptor: int) -> object:
                available = (
                    engine.HOST_FREE_FLOOR_BYTES + 4096
                    if getattr(replacement_fstat(descriptor), "st_dev") == 41
                    else engine.HOST_FREE_FLOOR_BYTES - 1
                )
                return types.SimpleNamespace(f_bavail=available, f_frsize=1)

            with (
                patch.object(engine.os, "fstat", side_effect=replacement_fstat),
                patch.object(
                    engine.os, "fstatvfs", side_effect=replacement_fstatvfs
                ),
            ):
                engine.sweep_orphan_scratch(budget)
            self.assertTrue(foreign_prepublication.is_dir())
            self.assertEqual(list(foreign_prepublication.iterdir()), [])
            self.assertEqual(
                list(fixture.cache_root.glob(".gb10-rebuild-scratch-delete.v1.*")),
                [],
            )
            self.assertEqual(
                list(held_cache.glob(".gb10-rebuild-scratch-delete.v1.*")),
                [],
            )
            budget.close()

            budget = engine.HostWriteBudget(fixture.cache_root)
            setattr(
                engine,
                "test_free_bytes",
                engine.HOST_FREE_FLOOR_BYTES + engine.HOST_WRITE_BUDGET_BYTES,
            )
            rollback = fixture.receipt_dir / "rollback"
            transaction = fixture.receipt_dir / "transaction.v1"
            receipts = fixture.receipt_dir / "receipts"
            for directory in (rollback, transaction, receipts):
                budget.ensure_private_directory(directory)
            source_fd = os.open(fixture.prior, os.O_RDONLY | os.O_CLOEXEC)
            try:
                backup = rollback / "backup.bin"
                engine.atomic_copy_fd(source_fd, backup, 0o700, budget)
            finally:
                os.close(source_fd)
            wal_bytes = b'{"phase":"committed"}\n'
            budget.write_new(transaction / "state.json", wal_bytes, 0o600)
            device = next(iter(budget.used_by_device))
            charged = fixture.prior.stat().st_size + len(wal_bytes)
            self.assertEqual(budget.used_by_device[device], charged)
            budget.rename(transaction, receipts / "receipt")
            self.assertEqual(budget.used_by_device[device], charged)
            budget.close()

        with RebuildFixture() as fixture:
            old_argv = sys.argv[:]
            old_handlers = {
                number: signal.getsignal(number)
                for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }
            try:
                with patch.dict(os.environ, fixture.env, clear=False):
                    sys.argv = [str(ENGINE), "--test-only"]
                    engine = _load(ENGINE, "exact_leaf_budget_boundary_test")
            finally:
                sys.argv = old_argv
                for number, handler in old_handlers.items():
                    signal.signal(number, handler)
            fixture.cache_root.mkdir(mode=0o700)
            setattr(
                engine,
                "test_free_bytes",
                engine.HOST_FREE_FLOOR_BYTES + engine.HOST_WRITE_BUDGET_BYTES,
            )
            budget = engine.HostWriteBudget(fixture.cache_root)
            parent_fd = budget.ensure_private_directory(fixture.cache_root)

            unlink_target = fixture.cache_root / "unlink-target"
            unlink_target.write_bytes(b"owned unlink target\n")
            unlink_target.chmod(0o600)
            saved_unlink = fixture.cache_root / "saved-unlink-target"
            foreign_unlink = fixture.cache_root / "foreign-unlink-target"
            foreign_unlink.write_bytes(b"foreign unlink target\x00")
            foreign_unlink.chmod(0o600)
            unlink_injected = False
            real_boundary = engine._test_boundary

            def replace_unlink(point: str) -> None:
                nonlocal unlink_injected
                if point == "host-leaf-move-validated" and not unlink_injected:
                    unlink_injected = True
                    os.rename(unlink_target, saved_unlink)
                    os.rename(foreign_unlink, unlink_target)
                real_boundary(point)

            with (
                patch.object(engine, "_test_boundary", side_effect=replace_unlink),
                self.assertRaises(engine.RebuildError),
            ):
                budget.unlink(unlink_target, regular_mode=0o600)
            self.assertTrue(unlink_injected)
            self.assertEqual(unlink_target.read_bytes(), b"foreign unlink target\x00")
            self.assertEqual(saved_unlink.read_bytes(), b"owned unlink target\n")

            rename_source = fixture.cache_root / "rename-source"
            rename_source.write_bytes(b"owned rename source\n")
            saved_source = fixture.cache_root / "saved-rename-source"
            foreign_source = fixture.cache_root / "foreign-rename-source"
            foreign_source.write_bytes(b"foreign rename source\x00")
            rename_destination = fixture.cache_root / "rename-destination"
            source_injected = False

            def replace_rename_source(point: str) -> None:
                nonlocal source_injected
                if point == "host-leaf-move-validated" and not source_injected:
                    source_injected = True
                    os.rename(rename_source, saved_source)
                    os.rename(foreign_source, rename_source)
                real_boundary(point)

            with (
                patch.object(
                    engine, "_test_boundary", side_effect=replace_rename_source
                ),
                self.assertRaises(engine.RebuildError),
            ):
                budget.rename(rename_source, rename_destination)
            self.assertTrue(source_injected)
            self.assertEqual(rename_source.read_bytes(), b"foreign rename source\x00")
            self.assertEqual(saved_source.read_bytes(), b"owned rename source\n")
            self.assertFalse(rename_destination.exists())

            exchange_source = fixture.cache_root / "exchange-source"
            exchange_source.write_bytes(b"new destination bytes\n")
            exchange_destination = fixture.cache_root / "exchange-destination"
            exchange_destination.write_bytes(b"old destination bytes\n")
            saved_destination = fixture.cache_root / "saved-exchange-destination"
            foreign_destination = fixture.cache_root / "foreign-exchange-destination"
            foreign_destination.write_bytes(b"foreign destination bytes\x00")
            destination_injected = False

            def replace_exchange_destination(point: str) -> None:
                nonlocal destination_injected
                if point == "host-leaf-exchange-validated" and not destination_injected:
                    destination_injected = True
                    os.rename(exchange_destination, saved_destination)
                    os.rename(foreign_destination, exchange_destination)
                real_boundary(point)

            with (
                patch.object(
                    engine,
                    "_test_boundary",
                    side_effect=replace_exchange_destination,
                ),
                self.assertRaises(engine.RebuildError),
            ):
                budget.rename(exchange_source, exchange_destination, replace=True)
            self.assertTrue(destination_injected)
            self.assertEqual(exchange_source.read_bytes(), b"new destination bytes\n")
            self.assertEqual(
                exchange_destination.read_bytes(), b"foreign destination bytes\x00"
            )
            self.assertEqual(saved_destination.read_bytes(), b"old destination bytes\n")

            cleanup_root = fixture.cache_root / "cleanup-root"
            cleanup_root.mkdir(mode=0o700)
            cleanup_child = cleanup_root / "child"
            cleanup_child.mkdir(mode=0o700)
            (cleanup_child / "owned").write_bytes(b"owned nested bytes\n")
            saved_child = cleanup_root / "saved-child"
            foreign_child = cleanup_root / "foreign-child"
            foreign_child.mkdir(mode=0o700)
            (foreign_child / "foreign").write_bytes(b"foreign nested bytes\x00")
            scan_injected = False

            def replace_scanned_child(point: str) -> None:
                nonlocal scan_injected
                if point == "cleanup-leaf-scanned" and not scan_injected:
                    scan_injected = True
                    os.rename(cleanup_child, saved_child)
                    os.rename(foreign_child, cleanup_child)
                real_boundary(point)

            cleanup_fd = os.open(
                cleanup_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                with (
                    patch.object(
                        engine, "_test_boundary", side_effect=replace_scanned_child
                    ),
                    self.assertRaises(engine.RebuildError),
                ):
                    engine._remove_tree_contents(
                        budget, cleanup_fd, cleanup_root.stat().st_dev
                    )
            finally:
                os.close(cleanup_fd)
            self.assertTrue(scan_injected)
            self.assertEqual(
                (cleanup_child / "foreign").read_bytes(), b"foreign nested bytes\x00"
            )
            self.assertEqual(
                (saved_child / "owned").read_bytes(), b"owned nested bytes\n"
            )

            slot_target = fixture.cache_root / (".rebuild-input-" + "7" * 32)
            slot_target.mkdir(mode=0o700)
            slot_metadata = slot_target.stat()
            slot_identity = (slot_metadata.st_dev, slot_metadata.st_ino)
            placeholder_identity = engine._ensure_scratch_delete_slot(
                budget, parent_fd
            )
            real_admit = budget._admit_fd

            def reject_record_publication(descriptor: int, amount: int) -> None:
                if descriptor == parent_fd:
                    raise engine.RebuildError("injected actual-FD free-space admission")
                real_admit(descriptor, amount)

            with (
                patch.object(
                    budget, "_admit_fd", side_effect=reject_record_publication
                ),
                self.assertRaisesRegex(engine.RebuildError, "actual-FD"),
            ):
                engine._prepare_scratch_delete(
                    budget, parent_fd, slot_target.name, slot_identity
                )
            self.assertEqual(
                (
                    (fixture.cache_root / engine.SCRATCH_DELETE_SLOT).stat().st_dev,
                    (fixture.cache_root / engine.SCRATCH_DELETE_SLOT).stat().st_ino,
                ),
                placeholder_identity,
            )
            self.assertEqual(
                (slot_target.stat().st_dev, slot_target.stat().st_ino), slot_identity
            )
            self.assertEqual(
                list(fixture.cache_root.glob(".gb10-rebuild-scratch-delete.v1.*")),
                [],
            )

            low_root = fixture.cache_root / "low-root"
            low_root.mkdir(mode=0o700)
            low_leaf = low_root / "foreign-preserved"
            low_leaf.write_bytes(b"low-space preserved bytes\x00")
            low_fd = os.open(
                low_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            low_identity = (low_root.stat().st_dev, low_root.stat().st_ino)
            low_after_scan = False
            park_before = tuple(
                sorted(path.name for path in fixture.cache_root.iterdir())
            )

            def lower_space(point: str) -> None:
                nonlocal low_after_scan
                if point == "cleanup-leaf-scanned":
                    low_after_scan = True
                real_boundary(point)

            def admit_actual_fd(descriptor: int, amount: int) -> None:
                metadata = os.fstat(descriptor)
                if low_after_scan and (
                    metadata.st_dev,
                    metadata.st_ino,
                ) == low_identity:
                    raise engine.RebuildError("injected actual-FD free-space admission")
                real_admit(descriptor, amount)

            try:
                with (
                    patch.object(engine, "_test_boundary", side_effect=lower_space),
                    patch.object(budget, "_admit_fd", side_effect=admit_actual_fd),
                    self.assertRaisesRegex(engine.RebuildError, "actual-FD"),
                ):
                    engine._remove_tree_contents(
                        budget, low_fd, low_root.stat().st_dev
                    )
            finally:
                os.close(low_fd)
            self.assertTrue(low_after_scan)
            self.assertEqual(low_leaf.read_bytes(), b"low-space preserved bytes\x00")
            self.assertEqual(
                tuple(sorted(path.name for path in fixture.cache_root.iterdir())),
                park_before,
            )

            source = ast.parse(ENGINE.read_text())
            budget_class = next(
                node
                for node in source.body
                if isinstance(node, ast.ClassDef) and node.name == "HostWriteBudget"
            )
            raw_destructive = [
                node
                for node in ast.walk(budget_class)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in {"rename", "replace", "unlink", "rmdir"}
            ]
            self.assertEqual(
                raw_destructive,
                [],
                "all HostWriteBudget leaf mutation must use the exact-leaf boundary",
            )
            functions = {
                node.name: node
                for node in source.body
                if isinstance(node, ast.FunctionDef)
            }
            for function_name in ("_remove_tree_contents", "cleanup_snapshot"):
                forbidden = [
                    node
                    for node in ast.walk(functions[function_name])
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and node.func.attr in {"unlink", "rmdir"}
                ]
                self.assertEqual(forbidden, [], function_name)
            recursive_walks = [
                node
                for node in ast.walk(functions["_remove_tree_contents"])
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_remove_tree_contents"
            ]
            self.assertEqual(recursive_walks, [])
            candidate_renames = [
                node
                for node in ast.walk(functions["_publish_candidate"])
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "rename"
            ]
            self.assertEqual(len(candidate_renames), 1)
            self.assertFalse(
                any(
                    keyword.arg == "replace"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in candidate_renames[0].keywords
                ),
                "content-addressed candidate publication must be NOREPLACE",
            )
            budget.close()

        self._exercise_cleanup_with_active_budget_and_held_parent()

    def _exercise_cleanup_with_active_budget_and_held_parent(self) -> None:
        with RebuildFixture() as fixture:
            old_argv = sys.argv[:]
            old_handlers = {
                number: signal.getsignal(number)
                for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }
            try:
                with patch.dict(os.environ, fixture.env, clear=False):
                    sys.argv = [str(ENGINE), "--test-only"]
                    engine = _load(ENGINE, "cleanup_budget_parent_test")
            finally:
                sys.argv = old_argv
                for number, handler in old_handlers.items():
                    signal.signal(number, handler)

            source = ast.parse(ENGINE.read_text())
            cleanup_calls = [
                node
                for node in ast.walk(source)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "cleanup_snapshot"
            ]
            self.assertTrue(cleanup_calls)
            self.assertTrue(
                all(any(keyword.arg == "budget" for keyword in call.keywords) for call in cleanup_calls),
                "every cleanup path must pass its active HostWriteBudget",
            )

            fixture.receipt_dir.mkdir(parents=True, mode=0o700)
            transaction = fixture.receipt_dir / "transaction.v1"
            transaction.mkdir(mode=0o700)
            budget = engine.HostWriteBudget(fixture.cache_root)
            parent_fd = budget.reserve(fixture.receipt_dir, 0)
            identity = (transaction.stat().st_dev, transaction.stat().st_ino)
            setattr(engine, "test_free_bytes", engine.HOST_FREE_FLOOR_BYTES - 1)
            try:
                with self.assertRaisesRegex(engine.RebuildError, "free-space admission"):
                    engine.cleanup_snapshot(
                        transaction,
                        identity,
                        budget=budget,
                        parent_fd=parent_fd,
                    )
                self.assertTrue(transaction.is_dir())
                self.assertEqual(
                    list(fixture.receipt_dir.glob(".transaction.v1.cleanup.*")), []
                )
            finally:
                budget.close()

            transaction.rmdir()
            cleanup = fixture.receipt_dir / ".transaction.v1.cleanup.1234.cafebabe"
            cleanup.mkdir(mode=0o700)
            setattr(
                engine,
                "test_free_bytes",
                engine.HOST_FREE_FLOOR_BYTES + engine.HOST_WRITE_BUDGET_BYTES,
            )
            budget = engine.HostWriteBudget(fixture.cache_root)
            budget.reserve(fixture.receipt_dir, 0)
            held_receipts = fixture.receipt_dir.with_name("held-receipts")
            fixture.receipt_dir.rename(held_receipts)
            fixture.receipt_dir.mkdir(mode=0o700)
            foreign = fixture.receipt_dir / "foreign.bin"
            foreign.write_bytes(b"foreign receipt parent must survive\n")
            try:
                self.assertTrue(engine._prepare_transaction_namespace(budget))
                self.assertFalse((held_receipts / cleanup.name).exists())
                self.assertEqual(
                    foreign.read_bytes(), b"foreign receipt parent must survive\n"
                )
                self.assertEqual(list(fixture.receipt_dir.iterdir()), [foreign])
            finally:
                budget.close()


class StrictFakeGrammarTests(unittest.TestCase):
    def test_fake_systemd_run_rejects_malformed_without_mutation(self) -> None:
        with RebuildFixture() as fixture:
            completed = fixture.run()
            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            line = next(
                entry
                for entry in fixture.calls().splitlines()
                if entry.startswith("systemd_run ")
            )
            valid = line.split()[1:]
            cases: dict[str, list[str]] = {}
            wrong_limit = valid.copy()
            wrong_limit[5] = "--property=MemoryHigh=1"
            cases["wrong-limit"] = wrong_limit
            cases["missing-property"] = valid[:7] + valid[8:]
            reordered = valid.copy()
            reordered[5], reordered[6] = reordered[6], reordered[5]
            cases["reordered-properties"] = reordered
            wrong_target = valid.copy()
            wrong_target[25] = "/tmp/foreign-bwrap"
            cases["wrong-target"] = wrong_target
            for name, arguments in cases.items():
                with self.subTest(name=name):
                    before = fixture.state_path.read_bytes()
                    result = subprocess.run(
                        [str(fixture.fake_bin / "systemd-run"), *arguments],
                        env=fixture.env,
                        capture_output=True,
                        timeout=5,
                    )
                    self.assertEqual(result.returncode, 93, result.stderr.decode())
                    self.assertIn(b"fixture grammar rejected systemd_run", result.stderr)
                    self.assertEqual(fixture.state_path.read_bytes(), before)

    def test_fake_systemctl_rejects_foreign_show_without_mutation(self) -> None:
        with RebuildFixture() as fixture:
            fixture.candidate.parent.mkdir(parents=True, exist_ok=True)
            fixture.candidate.write_bytes(fixture.build_source.read_bytes())
            fixture.candidate.chmod(0o755)
            fixture.set_state(candidate_path_swap=True, build_finished=True)
            before = fixture.state_path.read_bytes()
            result = subprocess.run(
                [
                    str(fixture.fake_bin / "systemctl"),
                    "--user",
                    "show",
                    "foreign.service",
                    "--property=ProtectSystem",
                ],
                env=fixture.env,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 93, result.stderr.decode())
            self.assertIn(b"fixture grammar rejected systemctl", result.stderr)
            self.assertEqual(fixture.state_path.read_bytes(), before)

    def test_fake_bwrap_rejects_incomplete_grammar_without_mutation(self) -> None:
        with RebuildFixture() as fixture:
            completed = fixture.run()
            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            lines = [
                entry.split()[1:]
                for entry in fixture.calls().splitlines()
                if entry.startswith("bwrap ")
            ]
            fetch = next(arguments for arguments in lines if arguments[-3] == "fetch")
            build = next(arguments for arguments in lines if arguments[-3] == "build")
            config = json.loads(fixture.authority_config.read_text())
            tools = config["tools"]
            destinations = {
                "/usr/lib/aarch64-linux-gnu": fixture.sysroot_lib,
                "/usr/lib/python3.12": Path(config["python_stdlib"]),
                "/tools/python": Path(tools["python"]["logical"]),
                "/worker.py": Path(tools["scoped_worker"]["logical"]),
                "/etc/ssl/certs/ca-certificates.crt": Path(tools["ca_cert"]["logical"]),
                "/etc/resolv.conf": Path(tools["resolv_conf"]["logical"]),
                "/etc/nsswitch.conf": Path(tools["nsswitch"]["logical"]),
                "/etc/hosts": Path(tools["hosts"]["logical"]),
                "/tools/git": Path(tools["git"]["logical"]),
                "/tools/git-remote-https": Path(tools["git_remote_https"]["logical"]),
                "/usr/lib/gcc/aarch64-linux-gnu/13": fixture.gcc_root,
                "/usr/include": fixture.sysroot_include,
                "/usr/bin/aarch64-linux-gnu-gcc-13": Path(tools["cc"]["logical"]),
                "/usr/bin/aarch64-linux-gnu-as": Path(tools["as"]["logical"]),
                "/usr/bin/aarch64-linux-gnu-ld.bfd": Path(tools["ld"]["logical"]),
                "/usr/bin/aarch64-linux-gnu-ar": Path(tools["ar"]["logical"]),
                "/toolchain": fixture.toolchain_root,
                "/toolchain/lib/rustlib/aarch64-unknown-linux-gnu": fixture.target_rustlib,
                "/toolchain/bin/cargo": Path(tools["cargo"]["logical"]),
                "/toolchain/bin/rustc": Path(tools["rustc"]["logical"]),
                "/cargo-home/registry/cache": fixture.registry_cache,
                "/cargo-home/registry/index": fixture.registry_index,
            }
            fresh_source = fixture.root / "fresh-source-authority"
            fresh_source.mkdir(mode=0o700)
            tracked = subprocess.check_output(
                [
                    "/usr/bin/git",
                    "-C",
                    str(fixture.remote),
                    "ls-tree",
                    "-r",
                    "-z",
                    fixture.source_commit,
                ]
            ).split(b"\0")
            for encoded in tracked:
                if not encoded:
                    continue
                metadata, encoded_path = encoded.split(b"\t", 1)
                mode, kind, oid = metadata.decode().split()
                self.assertEqual(kind, "blob")
                relative = Path(encoded_path.decode())
                destination = fresh_source / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.write_bytes(
                    subprocess.check_output(
                        [
                            "/usr/bin/git",
                            "-C",
                            str(fixture.remote),
                            "cat-file",
                            "blob",
                            oid,
                        ]
                    )
                )
                destination.chmod(0o500 if mode == "100755" else 0o400)
            for directory, children, _ in os.walk(fresh_source, topdown=False):
                for child in children:
                    (Path(directory) / child).chmod(0o500)
            fresh_source.chmod(0o500)
            destinations["/src"] = fresh_source

            def ledger(path: Path) -> tuple[tuple[str, str], ...]:
                if not path.exists() and not path.is_symlink():
                    return (("absent", str(path)),)
                rows: list[tuple[str, str]] = []
                candidates = [path]
                if path.is_dir():
                    candidates.extend(sorted(path.rglob("*")))
                for candidate in candidates:
                    relative = "." if candidate == path else str(candidate.relative_to(path))
                    metadata = candidate.lstat()
                    if stat.S_ISLNK(metadata.st_mode):
                        value = "L:" + os.readlink(candidate)
                    elif stat.S_ISREG(metadata.st_mode):
                        value = "F:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
                    else:
                        value = "D"
                    rows.append((relative, f"{stat.S_IMODE(metadata.st_mode):o}:{value}"))
                return tuple(rows)

            def mutation_state() -> tuple[object, ...]:
                return (
                    fixture.state_path.read_bytes(),
                    ledger(fixture.cache_root),
                    ledger(fixture.service_bin.parent),
                    ledger(fixture.source_dir),
                    ledger(fresh_source),
                    fixture.prior.read_bytes(),
                    fixture.wrong_hash.read_bytes(),
                    fixture.same_hash_other_inode.read_bytes(),
                )

            stage_witnesses: list[str] = []
            lifecycle_receipts: list[dict[str, object]] = []

            def invoke(
                template: list[str],
                mutate: object | None = None,
                foreign_destination: str | None = None,
                *,
                failure_stage: str | None = None,
            ) -> subprocess.CompletedProcess[bytes]:
                arguments = template.copy()
                descriptors: list[int] = []
                process: subprocess.Popen[bytes] | None = None
                reaped = False
                source_index: dict[str, int] = {}
                for index, value in enumerate(arguments):
                    if value != "--ro-bind":
                        continue
                    destination = arguments[index + 2]
                    authority = destinations.get(destination)
                    if authority is None:
                        continue
                    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
                    if authority.is_dir():
                        flags |= os.O_DIRECTORY
                    descriptor = os.open(authority, flags)
                    descriptors.append(descriptor)
                    arguments[index + 1] = f"/proc/self/fd/{descriptor}"
                    source_index[destination] = index + 1
                status_read, status_write = os.pipe()
                block_read, block_write = os.pipe()
                fence_parent, fence_child = socket.socketpair(
                    socket.AF_UNIX, socket.SOCK_SEQPACKET
                )
                fence_parent.settimeout(60)
                descriptors.extend((status_write, block_read))
                arguments[1] = str(status_write)
                arguments[3] = str(block_read)
                if foreign_destination is not None:
                    descriptor = os.open(
                        fixture.wrong_hash,
                        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    )
                    descriptors.append(descriptor)
                    arguments[source_index[foreign_destination]] = (
                        f"/proc/self/fd/{descriptor}"
                    )
                if callable(mutate):
                    mutate(arguments, source_index)
                try:
                    if os.write(block_write, b"G") != 1:
                        raise AssertionError("short fake-bwrap GO write")
                    os.close(block_write)
                    block_write = -1
                    process = subprocess.Popen(
                        [str(fixture.fake_bin / "bwrap"), *arguments],
                        env=fixture.env,
                        pass_fds=tuple(descriptors),
                        stdin=fence_child.fileno(),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                    )
                    owned_processes.append(process)
                    fence_child.close()
                    if failure_stage == "arbitrary":
                        stage_witnesses.append("arbitrary")
                        raise RuntimeError("injected strict fake-bwrap failure")
                    if failure_stage == "fence-recv":
                        stage_witnesses.append("fence-recv")
                        fence_parent.close()
                    payload = fence_parent.recv(2)
                    if failure_stage == "handshake":
                        stage_witnesses.append("handshake")
                        raise RuntimeError("injected strict fake-bwrap handshake failure")
                    if payload == b"R":
                        if failure_stage == "fence-send":
                            stage_witnesses.append("fence-send")
                            fence_parent.close()
                        if fence_parent.send(b"G") != 1:
                            raise AssertionError("short fake-bwrap resource-fence write")
                        fence_parent.shutdown(socket.SHUT_WR)
                    if failure_stage == "communicate-timeout":
                        stage_witnesses.append("communicate-timeout")
                    stdout, stderr = process.communicate(
                        timeout=0 if failure_stage == "communicate-timeout" else 60
                    )
                    reaped = True
                    return subprocess.CompletedProcess(
                        process.args, process.returncode, stdout, stderr
                    )
                finally:
                    if process is not None and not reaped:
                        worker_pid = 0
                        worker_starttime = 0
                        worker_deadline = time.monotonic() + 2
                        while time.monotonic() < worker_deadline:
                            worker_state = json.loads(fixture.state_path.read_text())
                            value = worker_state.get("scope_worker")
                            if isinstance(value, int) and value > 0:
                                worker_pid = value
                                starttime = worker_state.get("scope_worker_starttime")
                                if isinstance(starttime, int) and starttime > 0:
                                    worker_starttime = starttime
                                break
                            if process.poll() is not None:
                                break
                            time.sleep(0.01)
                        cleanup_error: BaseException | None = None
                        try:
                            _terminate_reap_group(process, worker_pid)
                        except BaseException as error:
                            cleanup_error = error
                        finally:
                            settled_state = json.loads(fixture.state_path.read_text())
                            lifecycle_receipts.append(
                                {
                                    "parent_pid": process.pid,
                                    "parent_returncode": process.returncode,
                                    "worker_pid": worker_pid,
                                    "worker_starttime": worker_starttime,
                                    "worker_reap_witness": settled_state.get(
                                        "scope_worker_reap_witness"
                                    ),
                                }
                            )
                        if cleanup_error is not None:
                            raise cleanup_error
                    fence_parent.close()
                    fence_child.close()
                    for descriptor in (*descriptors, status_read):
                        os.close(descriptor)
                    if block_write >= 0:
                        os.close(block_write)

            owned_processes: list[subprocess.Popen[bytes]] = []
            accepted = fetch.copy()
            accepted_unit = accepted[-1]
            fixture.set_state(
                scope_unit=accepted_unit,
                scope_registration={
                    "cgroup_path": str(
                        fixture.cgroup_root / "fixture.slice" / accepted_unit
                    ),
                    "unit": accepted_unit,
                    "worker_pid": 0,
                },
            )
            canary = invoke(accepted)
            self.assertEqual(canary.returncode, 0, canary.stderr.decode())
            self.assertNotIn(b"fixture grammar rejected", canary.stderr)

            failure_cases: dict[
                str, tuple[str, type[BaseException], object]
            ] = {
                "arbitrary": (
                    "live-output",
                    RuntimeError,
                    "injected strict fake-bwrap failure",
                ),
                "fence-recv": ("", OSError, errno.EBADF),
                "handshake": (
                    "",
                    RuntimeError,
                    "injected strict fake-bwrap handshake failure",
                ),
                "fence-send": ("", OSError, errno.EBADF),
                "communicate-timeout": (
                    "post-fence-live",
                    subprocess.TimeoutExpired,
                    0,
                ),
            }
            for failure_stage, (
                scope_failure,
                expected_error,
                expected_detail,
            ) in failure_cases.items():
                with self.subTest(failure_stage=failure_stage):
                    stage_witnesses.clear()
                    lifecycle_receipts.clear()
                    fixture.set_state(
                        scope_failure=scope_failure,
                        scope_registration={
                            "cgroup_path": str(
                                fixture.cgroup_root / "fixture.slice" / accepted_unit
                            ),
                            "unit": accepted_unit,
                            "worker_pid": 0,
                        },
                        scope_worker=0,
                        scope_worker_reap_witness=None,
                    )
                    failed_process: subprocess.Popen[bytes] | None = None
                    try:
                        with warnings.catch_warnings(record=True) as caught:
                            warnings.simplefilter("always", ResourceWarning)
                            with self.assertRaises(expected_error) as raised:
                                invoke(accepted, failure_stage=failure_stage)
                            gc.collect()
                        self.assertEqual(stage_witnesses, [failure_stage])
                        if isinstance(expected_detail, str):
                            self.assertEqual(raised.exception.args, (expected_detail,))
                        elif isinstance(raised.exception, OSError):
                            self.assertEqual(raised.exception.errno, expected_detail)
                        else:
                            self.assertIsInstance(
                                raised.exception, subprocess.TimeoutExpired
                            )
                            assert isinstance(raised.exception, subprocess.TimeoutExpired)
                            self.assertEqual(raised.exception.timeout, expected_detail)
                        self.assertEqual(
                            [item for item in caught if item.category is ResourceWarning],
                            [],
                        )
                        failed_process = owned_processes[-1]
                        self.assertEqual(len(lifecycle_receipts), 1)
                        lifecycle = lifecycle_receipts[0]
                        worker_pid = lifecycle["worker_pid"]
                        worker_starttime = lifecycle["worker_starttime"]
                        self.assertIsInstance(worker_pid, int)
                        self.assertIsInstance(worker_starttime, int)
                        assert isinstance(worker_pid, int)
                        assert isinstance(worker_starttime, int)
                        self.assertGreater(worker_pid, 0)
                        self.assertGreater(worker_starttime, 0)
                        self.assertEqual(
                            lifecycle["worker_reap_witness"],
                            {
                                "owner_pid": failed_process.pid,
                                "worker_pid": worker_pid,
                                "worker_starttime": worker_starttime,
                            },
                        )
                        self.assertIsNotNone(
                            lifecycle["parent_returncode"],
                            "strict fake-bwrap helper leaked its Popen",
                        )
                        self.assertIsNotNone(failed_process.poll())
                        self.assertFalse(Path(f"/proc/{failed_process.pid}").exists())
                        self.assertFalse(Path(f"/proc/{worker_pid}").exists())
                    finally:
                        if failed_process is None and owned_processes:
                            failed_process = owned_processes[-1]
                        if failed_process is not None and failed_process.poll() is None:
                            try:
                                os.killpg(failed_process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            failed_process.communicate(timeout=5)

            pidfd_failure = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import signal,sys,time;"
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                        "sys.stdout.buffer.write(b'ready\\n');sys.stdout.flush();"
                        "time.sleep(30)"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert pidfd_failure.stdout is not None
            self.assertEqual(pidfd_failure.stdout.readline(), b"ready\n")
            kill_events: list[int] = []
            real_killpg = os.killpg

            def traced_killpg(pid: int, number: int) -> None:
                kill_events.append(number)
                real_killpg(pid, number)

            try:
                with (
                    patch.object(
                        os,
                        "pidfd_open",
                        side_effect=OSError(errno.EMFILE, "pidfd budget exhausted"),
                    ),
                    patch.object(os, "killpg", side_effect=traced_killpg),
                    self.assertRaises(ExceptionGroup) as cleanup_failure,
                ):
                    _terminate_reap_group(pidfd_failure, pidfd_failure.pid)
                self.assertEqual(len(cleanup_failure.exception.exceptions), 1)
                pidfd_error = cleanup_failure.exception.exceptions[0]
                self.assertIsInstance(pidfd_error, OSError)
                assert isinstance(pidfd_error, OSError)
                self.assertEqual(pidfd_error.errno, errno.EMFILE)
                self.assertEqual(
                    kill_events, [signal.SIGTERM, signal.SIGKILL]
                )
                self.assertEqual(pidfd_failure.returncode, -signal.SIGKILL)
                self.assertFalse(Path(f"/proc/{pidfd_failure.pid}").exists())
            finally:
                if pidfd_failure.poll() is None:
                    real_killpg(pidfd_failure.pid, signal.SIGKILL)
                pidfd_failure.communicate(timeout=5)
            fixture.set_state(scope_failure="")

            def swap(left: str, right: str) -> object:
                def apply(arguments: list[str], indexes: dict[str, int]) -> None:
                    arguments[indexes[left]], arguments[indexes[right]] = (
                        arguments[indexes[right]],
                        arguments[indexes[left]],
                    )

                return apply

            cases: dict[str, tuple[list[str], object | None, str | None]] = {
                "zero-status-fd": (fetch, lambda args, _idx: args.__setitem__(1, "0"), None),
                "duplicate-control-fd": (
                    fetch,
                    lambda args, _idx: args.__setitem__(3, args[1]),
                    None,
                ),
                "missing-option": (
                    fetch,
                    lambda args, _idx: args.remove("--new-session"),
                    None,
                ),
                "reordered-options": (
                    fetch,
                    lambda args, _idx: args.__setitem__(
                        slice(args.index("--proc"), args.index("--proc") + 4),
                        args[args.index("--dev") : args.index("--dev") + 2]
                        + args[args.index("--proc") : args.index("--proc") + 2],
                    ),
                    None,
                ),
                "wrong-source": (
                    fetch,
                    lambda args, idx: args.__setitem__(idx["/worker.py"], "/tmp/foreign"),
                    None,
                ),
                "wrong-target": (
                    fetch,
                    lambda args, _idx: args.__setitem__(
                        args.index("/worker.py"), "/wrong-worker.py"
                    ),
                    None,
                ),
                "noncanonical-config": (
                    fetch,
                    lambda args, _idx: args.__setitem__(-2, " " + args[-2]),
                    None,
                ),
                "unknown-option": (
                    fetch,
                    lambda args, _idx: args.insert(args.index("--chdir"), "--unknown"),
                    None,
                ),
                "python-worker-swap": (fetch, swap("/tools/python", "/worker.py"), None),
                "cargo-rustc-swap": (
                    build,
                    swap("/toolchain/bin/cargo", "/toolchain/bin/rustc"),
                    None,
                ),
                "git-helper-swap": (
                    fetch,
                    swap("/tools/git", "/tools/git-remote-https"),
                    None,
                ),
                "duplicate-source": (
                    fetch,
                    lambda args, idx: args.__setitem__(
                        idx["/worker.py"], args[idx["/tools/python"]]
                    ),
                    None,
                ),
                "foreign-fd": (fetch, None, "/worker.py"),
            }
            for name, (template, mutate, foreign_destination) in cases.items():
                with self.subTest(name=name):
                    before = mutation_state()
                    result = invoke(template, mutate, foreign_destination)
                    self.assertEqual(result.returncode, 93, result.stderr.decode())
                    self.assertIn(b"fixture grammar rejected bwrap", result.stderr)
                    self.assertEqual(mutation_state(), before)


class RealBwrapWorkerIntegrationTests(unittest.TestCase):
    def test_bwrap_0_8_worker_entries_emit_parent_consumable_frames(self) -> None:
        self.assertEqual(
            subprocess.check_output(["/usr/bin/bwrap", "--version"], text=True).strip(),
            "bubblewrap 0.8.0",
        )
        with RebuildFixture() as fixture:
            cgroup = fixture.root / "worker-cgroup"
            cgroup.mkdir()
            policy = {
                "cpu_percent": 25,
                "fsize_bytes": 1048576,
                "memory_high": 2097152,
                "memory_max": 4194304,
                "min_mem_available": 0,
                "phase": "fetch",
                "runtime_seconds": 30,
                "tasks_max": 16,
            }
            for name, value in {
                "cgroup.events": "populated 1\n",
                "cgroup.procs": "2\n",
                "cpu.max": "25000 100000\n",
                "memory.high": "2097152\n",
                "memory.max": "4194304\n",
                "memory.oom.group": "1\n",
                "memory.swap.max": "0\n",
                "pids.max": "16\n",
            }.items():
                (cgroup / name).write_text(value)

            fake_git = fixture.root / "offline-git"
            fake_git.write_text(
                """#!/tools/python
import hashlib, io, os, sys, tarfile
payloads = {
    'Cargo.toml': b'[package]\\nname = "llm-guard-proxy"\\nversion = "0.0.0"\\n',
    'Cargo.lock': b'# lock\\nversion = 3\\n',
}
commit, tree = '1' * 40, '2' * 40
args = sys.argv[1:]
if args[:2] == ['init', '--bare']:
    os.makedirs(args[2], exist_ok=True)
    open(args[2] + '/config', 'wb').write(
        b'[core]\\n\\trepositoryformatversion = 0\\n\\tfilemode = true\\n\\tbare = true\\n'
        b'[gc]\\n\\tauto = 0\\n[transfer]\\n\\tfsckobjects = true\\n'
        b'[fetch]\\n\\tfsckobjects = true\\n[receive]\\n\\tfsckobjects = true\\n'
    )
elif 'rev-parse' in args:
    print(tree if args[-1].endswith('^{tree}') else commit)
elif 'rev-list' in args:
    print(commit)
elif 'ls-tree' in args:
    for name, payload in payloads.items():
        oid = hashlib.sha1(
            b'blob ' + str(len(payload)).encode() + b'\\0' + payload
        ).hexdigest()
        sys.stdout.buffer.write(
            f'100644 blob {oid} {len(payload)}\\t{name}\\0'.encode()
        )
elif 'archive' in args:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name)
            info.mode, info.mtime, info.size = 0o644, 0, len(payload)
            archive.addfile(info, io.BytesIO(payload))
    sys.stdout.buffer.write(output.getvalue())
elif not ({'remote', 'fetch', 'fsck'} & set(args)):
    raise SystemExit(90)
"""
            )
            fake_git.chmod(0o755)
            fake_cargo = fixture.root / "offline-cargo"
            fake_cargo.write_text(
                """#!/tools/python
import json, os, sys
if sys.argv[1] == 'metadata':
    package = 'path+file:///src/llm-guard-proxy#0.0.0'
    print(json.dumps({
        'packages': [{'dependencies': [], 'id': package,
            'manifest_path': '/src/llm-guard-proxy/Cargo.toml',
            'name': 'llm-guard-proxy', 'source': None}],
        'resolve': {'nodes': [{'dependencies': [], 'id': package}]},
        'target_directory': '/target', 'version': 1,
        'workspace_members': [package], 'workspace_root': '/src'}))
elif sys.argv[1] == 'build':
    path = '/target/aarch64-unknown-linux-gnu/release/llm-guard-proxy'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'wb').write(b'\\x7fELF-real-bwrap-worker\\n')
    os.chmod(path, 0o700)
else:
    raise SystemExit(91)
"""
            )
            fake_cargo.chmod(0o755)
            source = fixture.root / "offline-source"
            source.mkdir()
            (source / "Cargo.toml").write_text(
                '[package]\nname="llm-guard-proxy"\nversion="0.0.0"\n'
            )

            old_argv = sys.argv[:]
            old_handlers = {
                number: signal.getsignal(number)
                for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }
            try:
                with patch.dict(os.environ, fixture.env, clear=False):
                    sys.argv = [str(ENGINE), "--test-only"]
                    engine = _load(ENGINE, "real_bwrap_frame_parent")
            finally:
                sys.argv = old_argv
                for number, handler in old_handlers.items():
                    signal.signal(number, handler)

            common = [
                "/usr/bin/bwrap",
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
                "/usr/lib",
                "--dir",
                "/usr/lib/python3.11",
                "--dir",
                "/usr/lib/x86_64-linux-gnu",
                "--dir",
                "/sys",
                "--dir",
                "/sys/fs",
                "--dir",
                "/sys/fs/cgroup",
                "--dir",
                "/tools",
                "--ro-bind",
                "/usr/lib/x86_64-linux-gnu",
                "/usr/lib/x86_64-linux-gnu",
                "--ro-bind",
                "/usr/lib/python3.11",
                "/usr/lib/python3.11",
                "--ro-bind",
                "/usr/bin/python3.11",
                "/tools/python",
                "--ro-bind",
                str(WORKER),
                "/worker.py",
                "--ro-bind",
                str(cgroup),
                "/sys/fs/cgroup",
                "--symlink",
                "usr/lib",
                "/lib",
                "--symlink",
                "usr/lib/x86_64-linux-gnu",
                "/lib64",
                "--tmpfs",
                "/home",
                "--clearenv",
                "--setenv",
                "LANG",
                "C",
                "--setenv",
                "LC_ALL",
                "C",
                "--setenv",
                "PATH",
                "/tools",
            ]

            def run_worker(phase: str) -> tuple[dict[str, object], bytes]:
                config: dict[str, object] = {"policy": {**policy, "phase": phase}}
                if phase == "fetch":
                    config.update(
                        source_protocol="file",
                        source_ref="refs/heads/main",
                        source_repo="file:///offline",
                    )
                    phase_args = [
                        "--ro-bind",
                        str(fake_git),
                        "/tools/git",
                        "--ro-bind",
                        str(fake_git),
                        "/tools/git-remote-https",
                        "--size",
                        "67108864",
                        "--tmpfs",
                        "/fetch",
                        "--size",
                        "16777216",
                        "--tmpfs",
                        "/tmp",
                    ]
                    maximum = 160 * 1024 * 1024
                else:
                    phase_args = [
                        "--dir",
                        "/toolchain",
                        "--dir",
                        "/toolchain/bin",
                        "--ro-bind",
                        str(fake_cargo),
                        "/toolchain/bin/cargo",
                        "--dir",
                        "/src",
                        "--ro-bind",
                        str(source),
                        "/src/llm-guard-proxy",
                        "--dir",
                        "/cargo-home",
                        "--size",
                        "67108864",
                        "--tmpfs",
                        "/target",
                        "--size",
                        "16777216",
                        "--tmpfs",
                        "/tmp",
                    ]
                    maximum = 128 * 1024 * 1024
                command = [
                    *common,
                    *phase_args,
                    "--chdir",
                    "/",
                    "--",
                    "/tools/python",
                    "-I",
                    "-B",
                    "-S",
                    "/worker.py",
                    phase,
                    json.dumps(config, sort_keys=True, separators=(",", ":")),
                    "llm-guard-rebuild-real.scope",
                ]
                result = subprocess.run(command, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                return engine._decode_frame(result.stdout, phase, maximum)

            fetch_header, archive = run_worker("fetch")
            self.assertEqual(len(engine._entries_from_header(fetch_header)), 2)
            self.assertTrue(archive)
            _, metadata = run_worker("metadata")
            self.assertRegex(engine._validate_metadata(metadata.decode()), r"^[0-9a-f]{64}$")
            build_header, candidate = run_worker("build")
            self.assertEqual(build_header["payload_sha256"], hashlib.sha256(candidate).hexdigest())


if __name__ == "__main__":
    unittest.main()