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


    def test_production_tool_specs_pin_complete_gb10_authority(self) -> None:
        with patch.object(sys, "argv", [str(ENGINE)]):
            engine = _load(ENGINE, "complete_production_tool_authority")
        expected = {
            "ar": ("/usr/bin/aarch64-linux-gnu-ar", "f4583a612510e038dbc1ae8afb5eae5f0445c435bdd7c4c28e78acc7e757d2b1"),
            "cargo": ("/home/obj/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/bin/cargo", "7db170801729d4775347548ed5970b459844fc2f6b20798efb96f444b5b94fc3"),
            "cc": ("/usr/bin/aarch64-linux-gnu-gcc-13", "a20520ee21543f243d40636a9181a142c45ecd989de31ab86b99a8ea5ada870d"),
            "curl": ("/usr/bin/curl", "67054bcf748d42e1bf4b2a0eb4ba768e37dde8681313a64edd2f343c5d17a0ac"),
            "git": ("/usr/bin/git", "aa6540695d076182256dd6e96c8b302e4d56381e3000bbfd5c71bbdfe94a4942"),
            "readelf": ("/usr/bin/aarch64-linux-gnu-readelf", "6bca2bbd23b072db9e9a19ae0e65cf7b7c15c08a3c2cf01dd56453e1ac9340b1"),
            "rustc": ("/home/obj/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/bin/rustc", "2425682b7dd432769e2600eb2b3ea5113a00d37f41a754598f2af2e56e4fa2fe"),
            "systemctl": ("/usr/bin/systemctl", "1bf2f1e98c533b0313a78143ffcda2690116d90d76816dc7b74df79c4767aa95"),
            "systemd_run": ("/usr/bin/systemd-run", "0253595d482ea0aa9c4bf2e58080e9615bc59a51e29dbe1cebd14145b92bd8fc"),
        }
        specs = engine._production_tool_specs()
        self.assertEqual(set(specs), set(expected))
        self.assertEqual(
            {name: (spec.logical, spec.sha256) for name, spec in specs.items()},
            expected,
        )

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



    def test_built_candidate_open_rejects_special_or_oversized_files_within_deadline(
        self,
    ) -> None:
        with RebuildFixture() as fixture, patch.dict(os.environ, fixture.env, clear=False):
            old_argv = sys.argv[:]
            try:
                sys.argv = [str(ENGINE), "--test-only"]
                engine = _load(ENGINE, "built_candidate_open_test")
            finally:
                sys.argv = old_argv

            regular = fixture.root / "candidate-regular"
            regular.write_bytes(b"candidate")
            regular.chmod(0o755)
            link = fixture.root / "candidate-link"
            link.symlink_to(regular)
            fifo = fixture.root / "candidate-fifo"
            os.mkfifo(fifo, 0o700)
            oversized = fixture.root / "candidate-oversized"
            with oversized.open("wb") as stream:
                stream.truncate(engine.MAX_EXECUTABLE_BYTES + 1)
            oversized.chmod(0o755)

            engine.operation_deadline = time.monotonic() + 1
            for path in (link, fifo, oversized):
                started = time.monotonic()
                with self.subTest(path=path.name), self.assertRaises(engine.RebuildError):
                    engine._open_built_candidate(path)
                self.assertLess(time.monotonic() - started, 0.5)

            engine.operation_deadline = time.monotonic() - 1
            with self.assertRaisesRegex(engine.RebuildError, "read deadline exhausted"):
                engine._open_built_candidate(regular)

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


    def test_direct_cargo_artifact_bytes_vary_with_canonical_source(self) -> None:
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
        self.assertIn('source_repo = "https://github.com/RyderFreeman4Logos/llm-guard-proxy.git"', source)
        self.assertIn('source_ref = "refs/heads/main"', source)
        self.assertIn('TARGET_TRIPLE = "aarch64-unknown-linux-gnu"', source)
        self.assertIn('EXPECTED_ELF_MACHINE = "AArch64"', source)
        self.assertNotIn("bwrap", source)
        self.assertNotIn("scoped_worker", source)

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


    def test_direct_build_revalidates_canonical_source_after_cargo(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(mutate_source_during_cargo=True)
            before = os.readlink(fixture.service_bin)
            result = fixture.run(timeout=20)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("directory authority ledger changed: canonical source", output)
            self.assertEqual(os.readlink(fixture.service_bin), before)
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)
            self.assertFalse(
                fixture.receipt_dir.joinpath("transaction.v1", "state.json").exists()
            )

    def test_direct_build_records_revalidated_input_authorities_and_write_contract(self) -> None:
        with RebuildFixture() as fixture:
            result = fixture.run(timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(fixture.receipt_paths()[0].read_text())
            inputs = receipt["authorities"]["build_inputs"]
            self.assertIn("directory_authorities", inputs)
            self.assertEqual(
                set(inputs["directory_authorities"]),
                {
                    "canonical_source",
                    "cargo_home",
                    "gcc_closure",
                    "registry_cache",
                    "registry_index",
                    "sysroot_include",
                    "sysroot_runtime",
                    "target_rustlib",
                    "toolchain",
                },
            )
            self.assertEqual(
                inputs["write_contract"]["limits"],
                {
                    "cargo_target_bytes": 512 * 1024 * 1024,
                    "git_object_bytes": 64 * 1024 * 1024,
                    "host_write_bytes": 576 * 1024 * 1024,
                },
            )
            self.assertGreater(inputs["write_contract"]["cargo_target_bytes"], 0)
            self.assertGreater(inputs["write_contract"]["git_object_bytes"], 0)
            self.assertEqual(
                inputs["rustc_exec"],
                receipt["authorities"]["tool_authorities"]["rustc"]["sha256"],
            )

    @unittest.skipUnless(
        os.uname().machine == "aarch64"
        and os.environ.get("GB10_NATIVE_AARCH64_REAL_CARGO") == "1",
        "requires explicit native GB10 AArch64 Cargo selector",
    )
    def test_native_aarch64_direct_cargo_build_produces_candidate(self) -> None:
        marker = "gb10-native-direct-cargo-production-path"
        with RebuildFixture() as fixture:
            main = fixture.remote / "llm-guard-proxy" / "src" / "main.rs"
            main.write_text(f'fn main() {{ println!("{marker}"); }}\n')
            self._git(fixture.remote, "add", "--", str(main.relative_to(fixture.remote)))
            self._git(fixture.remote, "commit", "-m", "native Cargo artifact")
            self._refresh_fixture_source_identity(fixture)

            command, env = fixture._run_arguments(True, None)
            config = json.loads(fixture.authority_config.read_text())
            config.update(
                {
                    "toolchain_root": "/home/obj/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu",
                    "target_rustlib": "/home/obj/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/lib/rustlib/aarch64-unknown-linux-gnu",
                    "registry_cache": "/home/obj/.cargo/registry/cache/index.crates.io-1949cf8c6b5b557f",
                    "registry_index": "/home/obj/.cargo/registry/index/index.crates.io-1949cf8c6b5b557f",
                    "gcc_root": "/usr/lib/gcc/aarch64-linux-gnu/13",
                    "sysroot_lib": "/usr/lib/aarch64-linux-gnu",
                    "sysroot_include": "/usr/include",
                }
            )
            real_tools = {
                "cargo": Path(config["toolchain_root"]) / "bin/cargo",
                "rustc": Path(config["toolchain_root"]) / "bin/rustc",
                "cc": Path("/usr/bin/aarch64-linux-gnu-gcc-13"),
                "ar": Path("/usr/bin/aarch64-linux-gnu-ar"),
                "readelf": Path("/usr/bin/aarch64-linux-gnu-readelf"),
            }
            for name, path in real_tools.items():
                resolved = path.resolve(strict=True)
                info = resolved.stat()
                config["tools"][name] = {
                    "logical": str(path),
                    "resolved": str(resolved),
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "mode": stat.S_IMODE(info.st_mode),
                    "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
                }
            fixture.authority_config.write_text(json.dumps(config, sort_keys=True))
            fixture.authority_config.chmod(0o600)

            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=600)
            finally:
                if process.poll() is None:
                    stdout, stderr = _kill_group(process)
            output = stdout + stderr
            self.assertEqual(process.returncode, 0, output)

            receipt = json.loads(fixture.receipt_paths()[0].read_text())
            candidate = Path(receipt["candidate"]["path"])
            payload = candidate.read_bytes()
            self.assertIn(marker.encode(), payload)
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                receipt["candidate"]["identity"]["sha256"],
            )
            header = subprocess.run(
                [str(real_tools["readelf"]), "-hW", str(candidate)],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            self.assertIn("Machine:", header)
            self.assertIn("AArch64", header)
            argv = receipt["authorities"]["build_inputs"]["cargo_argv"]
            self.assertEqual(
                argv[:6],
                [
                    "build",
                    "--release",
                    "--locked",
                    "--offline",
                    "--target",
                    "aarch64-unknown-linux-gnu",
                ],
            )
            self.assertEqual(
                argv[8:],
                [
                    "--package",
                    "llm-guard-proxy",
                    "--no-default-features",
                    "--features",
                    "guard",
                ],
            )
            self.assertEqual(
                receipt["authorities"]["tool_authorities"]["cargo"]["resolved_path"],
                str(real_tools["cargo"].resolve(strict=True)),
            )
            fixture.assert_no_backend_lifecycle(self)

    def test_direct_cargo_build_is_pinned_and_does_not_need_bwrap(self) -> None:
        source = ENGINE.read_text()
        self.assertNotIn("bwrap", source)
        self.assertNotIn("scoped_worker", source)
        self.assertIn(
            '"build", "--release", "--locked", "--offline", "--target", TARGET_TRIPLE',
            source,
        )
        self.assertIn('manifest = f"/proc/self/fd/{source.source_authority.descriptor}/Cargo.toml"', source)
        self.assertIn('"RUSTC": require_tool("rustc")', source)
        self.assertIn('"--package", "llm-guard-proxy", "--no-default-features", "--features", "guard"', source)




class SharedBoundedScopeAuthorityTests(unittest.TestCase):

    def test_direct_command_mode_reuses_scope_authority_and_cleanup(self) -> None:
        bounded = _load(BOUNDED, "bounded_direct_scope_test")
        direct = bounded.scoped_direct_command

        def code_names(code: types.CodeType) -> set[str]:
            return set(code.co_names).union(
                *(code_names(value) for value in code.co_consts if isinstance(value, types.CodeType))
            )

        names = code_names(direct.__code__) | code_names(bounded.scoped_command.__code__)
        self.assertIn("_verify_scope", names)
        self.assertIn("_scope_resource_events", names)
        self.assertIn("_scope_signal", names)
        self.assertIn("_scope_quiescent", names)

        source = ENGINE.read_text()
        self.assertNotIn("def execute_scoped(", source)
        self.assertIn("run_scoped_direct(", source)

    def test_direct_command_mode_rejects_unproven_scope_before_cargo(self) -> None:
        with RebuildFixture() as fixture:
            fixture.set_state(scope_failure="property-readback")
            result = fixture.run(timeout=20)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("scope controller value is not exact", output)
            self.assertNotIn("cargo metadata", fixture.calls())
            self.assertNotIn("cargo build", fixture.calls())
            self.assertEqual(fixture.reload_state()["restart_calls"], 0)

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

    def test_write_budget_accounts_external_git_and_cargo_trees(self) -> None:
        with RebuildFixture() as fixture, patch.dict(os.environ, fixture.env, clear=False):
            old_argv = sys.argv[:]
            try:
                sys.argv = [str(ENGINE), "--test-only"]
                engine = _load(ENGINE, "external_write_budget_test")
            finally:
                sys.argv = old_argv

            fixture.cache_root.mkdir(mode=0o700)
            target = fixture.cache_root / "target"
            target.mkdir(mode=0o700)
            (target / "one").write_bytes(b"123")
            (target / "two").write_bytes(b"45")
            budget = engine.HostWriteBudget(fixture.cache_root)
            setattr(
                engine,
                "test_free_bytes",
                engine.HOST_FREE_FLOOR_BYTES + engine.HOST_WRITE_BUDGET_BYTES,
            )
            try:
                self.assertEqual(budget.account_tree(target, 5, "Cargo target"), 5)
                self.assertEqual(budget.used, 5)
                (target / "three").write_bytes(b"6")
                with self.assertRaisesRegex(engine.RebuildError, "Cargo target byte bound"):
                    budget.account_tree(target, 5, "Cargo target")
            finally:
                budget.close()

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





if __name__ == "__main__":
    unittest.main()