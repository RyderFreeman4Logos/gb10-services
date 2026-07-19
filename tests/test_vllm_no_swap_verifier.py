from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from vllm_no_swap_fixtures import (
    VERIFIER,
    VERIFIER_CORE,
    VllmNoSwapFixture,
    _proc_stat,
)


class VllmNoSwapVerifierTests(VllmNoSwapFixture):
    def _copy_verifier_bundle(self) -> tuple[Path, Path]:
        bundle = self.root / "verifier-bundle"
        bundle.mkdir(exist_ok=True)
        wrapper = bundle / VERIFIER.name
        core = bundle / VERIFIER_CORE.name
        shutil.copy2(VERIFIER, wrapper)
        shutil.copy2(VERIFIER_CORE, core)
        wrapper.chmod(0o755)
        core.chmod(0o644)
        return wrapper, core

    def test_executes_only_the_digest_bound_trusted_companion(self) -> None:
        wrapper, core = self._copy_verifier_bundle()
        result = self._run(wrapper=wrapper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        relative = Path(wrapper.parent.name) / wrapper.name
        result = self._run(wrapper=relative, cwd=wrapper.parent.parent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        link = self.root / "verifier-link"
        link.symlink_to(wrapper)
        result = self._run(wrapper=link)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        marker = self.root / "untrusted-core-executed"
        hostile = (
            "missing",
            "symlink",
            "hardlink",
            "unsafe-mode",
            "digest",
        )
        for case in hostile:
            with self.subTest(case=case):
                wrapper, core = self._copy_verifier_bundle()
                core.unlink()
                payload = self.root / f"payload-{case}.py"
                payload.write_bytes(
                    VERIFIER_CORE.read_bytes()
                    + f"\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
                )
                payload.chmod(0o644)
                if case == "missing":
                    pass
                elif case == "symlink":
                    core.symlink_to(payload)
                elif case == "hardlink":
                    os.link(payload, core)
                else:
                    shutil.copy2(payload, core)
                    core.chmod(0o666 if case == "unsafe-mode" else 0o644)
                result = self._run(wrapper=wrapper)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("gb10_vllm_no_swap:", result.stderr)
                self.assertFalse(marker.exists())

    def test_companion_path_replacement_while_open_is_rejected(self) -> None:
        wrapper, core = self._copy_verifier_bundle()
        marker = self.root / "raced-core-executed"
        payload = (
            b"from pathlib import Path\n"
            + f"Path({str(marker)!r}).write_text('executed')\n".encode()
            + b"#"
            + b"x" * (256 * 1024)
            + b"\n"
        )
        replacement = core.with_name("replacement-core.py")
        core.write_bytes(payload)
        replacement.write_bytes(payload)
        core.chmod(0o644)
        replacement.chmod(0o644)
        wrapper_text = wrapper.read_text()
        wrapper_text = wrapper_text.replace(
            hashlib.sha256(VERIFIER_CORE.read_bytes()).hexdigest(),
            hashlib.sha256(payload).hexdigest(),
            1,
        ).replace("os.read(descriptor, 1024 * 1024)", "os.read(descriptor, 1)", 1)
        wrapper.write_text(wrapper_text)
        wrapper.chmod(0o755)
        environment = self._test_environment()
        process = subprocess.Popen(
            [
                "/usr/bin/env",
                "-i",
                *[f"{key}={value}" for key, value in environment.items()],
                "/usr/bin/bash",
                "--noprofile",
                "--norc",
                str(wrapper),
                "--test-only",
                "--unit",
                str(self.unit),
                "--container",
                "vllm-test",
            ],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        replaced = False
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline and not replaced:
            pending = [process.pid]
            seen: set[int] = set()
            while pending and not replaced:
                pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    children = Path(
                        f"/proc/{pid}/task/{pid}/children"
                    ).read_text().split()
                    pending.extend(int(child) for child in children)
                    for descriptor in Path(f"/proc/{pid}/fd").iterdir():
                        try:
                            if os.readlink(descriptor) == str(core):
                                os.replace(replacement, core)
                                replaced = True
                                break
                        except (FileNotFoundError, PermissionError):
                            continue
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
            if not replaced:
                time.sleep(0.001)
        stdout, stderr = process.communicate(timeout=10)
        self.assertTrue(replaced, stdout + stderr)
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        self.assertIn("changed while loading", stderr)
        self.assertFalse(marker.exists())

    def test_companion_unsafe_owner_is_rejected_before_marker_execution(self) -> None:
        wrapper, core = self._copy_verifier_bundle()
        marker = self.root / "unsafe-owner-executed"
        payload = (
            f"from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
        ).encode()
        core.write_bytes(payload)
        core.chmod(0o644)
        wrapper_text = wrapper.read_text()
        loader = wrapper_text.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
        stderr = io.StringIO()
        argv = [
            "-",
            str(wrapper),
            core.name,
            hashlib.sha256(payload).hexdigest(),
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch(
            "os.geteuid", return_value=os.getuid() + 1
        ), mock.patch.object(sys, "stderr", stderr):
            with self.assertRaisesRegex(SystemExit, "1"):
                exec(compile(loader, str(wrapper), "exec"), {})
        self.assertIn("unsafe owner", stderr.getvalue())
        self.assertFalse(marker.exists())

    def test_accepts_generation_bound_proc_and_cgroup_evidence(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.command_log.read_text()
        self.assertIn("docker info --format {{.CgroupVersion}}", log)
        self.assertIn("docker inspect --type container vllm-test", log)
        self.assertIn(
            f"systemctl show -p ControlGroup --value docker-{'a' * 64}.scope",
            log,
        )
        self.assertGreaterEqual(log.count("docker inspect --type container vllm-test"), 2)

    def test_preflight_rejects_non_v2_or_failed_info_before_unit_or_container_access(self) -> None:
        for version, info_fail in (("1", False), ("unknown", False), ("2", True)):
            with self.subTest(version=version, info_fail=info_fail):
                self.command_log.unlink(missing_ok=True)
                self.inspect_state.unlink(missing_ok=True)
                self.assert_rejected(cgroup_version=version, info_fail=info_fail)
                log = self.command_log.read_text()
                self.assertEqual(log.count("docker info --format {{.CgroupVersion}}"), 1)
                self.assertNotIn("docker inspect", log)
                self.assertFalse(self.inspect_state.exists())

    def test_rejects_every_noncanonical_swap_space_spelling_or_placement(self) -> None:
        mutations = (
            ["/usr/local/bin/vllm", "serve", "model"],
            ["/usr/local/bin/vllm", "serve", "model", "--swap-space", "1"],
            [
                "/usr/local/bin/vllm",
                "serve",
                "model",
                "--swap-space",
                "0",
                "--swap-space",
                "0",
            ],
            ["/usr/local/bin/vllm", "serve", "model", "--swap-space=0"],
            ["/usr/local/bin/vllm", "serve", "model", "--swap_space", "0"],
            ["/bin/sh", "-c", "vllm serve model --swap-space 0"],
        )
        for command in mutations:
            with self.subTest(command=command):
                self._write_unit(
                    self.unit,
                    "vllm-test",
                    "/run/user/1001/gb10-vllm-cids/test.cid",
                    application=command,
                )
                self.assert_rejected(containers=())

        self._write_unit(
            self.unit,
            "vllm-test",
            "/run/user/1001/gb10-vllm-cids/test.cid",
        )
        text = self.unit.read_text().replace(
            "--memory 18g", "--swap-space 0 --memory 18g", 1
        )
        self.unit.write_text(text)
        self.assert_rejected(containers=())

    def test_rejects_unit_memory_intent_or_container_identity_drift(self) -> None:
        self.unit.write_text(self.unit.read_text().replace("--memory-swap 18g", "--memory-swap 19g"))
        self.assert_rejected(containers=())
        self._write_unit(
            self.unit,
            "vllm-test",
            "/run/user/1001/gb10-vllm-cids/test.cid",
        )
        cases = (
            self._inspect("vllm-test", memory=self.memory - 1),
            self._inspect("vllm-test", memory_swap=self.memory - 1),
            self._inspect("vllm-test", entrypoint=["/bin/sh", "-c"]),
            self._inspect(
                "vllm-test",
                command=["/usr/local/bin/vllm", "serve", "model", "--swap-space=0"],
            ),
            self._inspect("vllm-test", started_at=""),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.inspect_state.unlink(missing_ok=True)
                self.assert_rejected(inspect_sequences={"vllm-test": [payload]})

    def test_rejects_id_pid_or_started_at_churn(self) -> None:
        first = self._inspect("vllm-test")
        replacements = (
            self._inspect("vllm-test", identifier="c" * 64),
            self._inspect("vllm-test", pid=6262),
            self._inspect(
                "vllm-test", started_at="2026-07-18T01:02:05.123456789Z"
            ),
        )
        for second in replacements:
            with self.subTest(second=second):
                self.inspect_state.unlink(missing_ok=True)
                self.assert_rejected(
                    inspect_sequences={"vllm-test": [first, second]}
                )

    def test_rejects_proc_starttime_or_cgroup_path_churn(self) -> None:
        stat_path = self.proc_root / str(self.pids["vllm-test"]) / "stat"
        cgroup_path = self.proc_root / str(self.pids["vllm-test"]) / "cgroup"
        cases = (
            [{"op": "write", "path": str(stat_path), "data": _proc_stat(4242, 999_999)}],
            [
                {
                    "op": "write",
                    "path": str(cgroup_path),
                    "data": "0::/app.slice/docker-" + "a" * 64 + ".scope\n",
                }
            ],
        )
        for actions in cases:
            with self.subTest(actions=actions):
                self.inspect_state.unlink(missing_ok=True)
                self.assert_rejected(second_inspect_actions=actions)
                self._write_generation("vllm-test")

    def test_rejects_ambiguous_or_noncanonical_proc_cgroup_path(self) -> None:
        proc_cgroup = self.proc_root / str(self.pids["vllm-test"]) / "cgroup"
        identifier = self.identifiers["vllm-test"]
        malformed = (
            "0::relative\n",
            f"0::/app.slice/docker-{identifier}.scope/\n",
            f"0::/app.slice//docker-{identifier}.scope\n",
            f"0::/app.slice/../docker-{identifier}.scope\n",
            f"0::/app.slice/wrapper.scope/docker-{identifier}.scope\n",
            f"0::/app.slice/docker-{'b' * 64}.scope\n",
            f"0::/app.slice/docker-{identifier}.scope\n0::/second\n",
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                proc_cgroup.write_text(payload)
                self.assert_rejected()
        self._write_generation("vllm-test")
        proc_cgroup.unlink()
        proc_cgroup.mkdir()
        self.assert_rejected()

    def test_systemd_scope_is_only_a_required_cross_check(self) -> None:
        self.scopes["vllm-test"] = "/app.slice/docker-" + "a" * 64 + ".scope"
        self.assert_rejected()

    def test_rejects_cgroup_inode_populated_or_metric_churn(self) -> None:
        scope = self.cgroup_root / self.scopes["vllm-test"].removeprefix("/")
        replacement_files = {
            "cgroup.events": "populated 1\nfrozen 0\n",
            "memory.max": f"{self.memory}\n",
            "memory.swap.max": "0\n",
            "memory.swap.current": "0\n",
        }
        cases = (
            [
                {
                    "op": "replace_dir",
                    "path": str(scope),
                    "files": replacement_files,
                }
            ],
            [
                {
                    "op": "write",
                    "path": str(scope / "cgroup.events"),
                    "data": "populated 0\nfrozen 0\n",
                }
            ],
            [
                {
                    "op": "write",
                    "path": str(scope / "memory.swap.current"),
                    "data": "1\n",
                }
            ],
        )
        for actions in cases:
            with self.subTest(actions=actions):
                self.inspect_state.unlink(missing_ok=True)
                self.assert_rejected(second_inspect_actions=actions)
                if scope.exists():
                    import shutil

                    shutil.rmtree(scope)
                old = scope.with_name(scope.name + ".old")
                if old.exists():
                    old.rename(scope)
                self._write_generation("vllm-test")

    def test_rejects_missing_nonregular_symlinked_or_malformed_cgroup_evidence(self) -> None:
        scope = self.cgroup_root / self.scopes["vllm-test"].removeprefix("/")
        cases = (
            ("cgroup.events", "populated 0\nfrozen 0\n"),
            ("cgroup.events", "populated 1\npopulated 1\n"),
            ("memory.max", f"{self.memory - 1}\n"),
            ("memory.swap.max", "max\n"),
            ("memory.swap.current", "1\n"),
            ("memory.swap.max", "0\nextra\n"),
        )
        for filename, payload in cases:
            with self.subTest(filename=filename, payload=payload):
                self._write_generation("vllm-test")
                (scope / filename).write_text(payload)
                self.assert_rejected()
        self._write_generation("vllm-test")
        (scope / "memory.max").unlink()
        self.assert_rejected()
        self._write_generation("vllm-test")
        target = scope / "memory.max"
        target.unlink()
        target.symlink_to(scope / "memory.swap.max")
        self.assert_rejected()

    def test_repeatable_container_binds_each_to_its_unit_contract(self) -> None:
        result = self._run(
            units=(self.unit, self.second_unit),
            containers=("vllm-test", "vllm-second"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.command_log.read_text()
        self.assertIn(f"docker-{'a' * 64}.scope", log)
        self.assertIn(f"docker-{'b' * 64}.scope", log)
        self.assert_rejected(containers=("vllm-second",), units=(self.unit,))

    def test_production_mode_rejects_test_selectors_and_python_is_isolated(self) -> None:
        marker = self.root / "bash-env-ran"
        bash_env = self.root / "bash-env"
        bash_env.write_text(f"touch {shlex.quote(str(marker))}\n")
        result = subprocess.run(
            [
                "/usr/bin/env",
                "-i",
                f"DOCKER_HOST=unix:///run/user/{os.getuid()}/docker.sock",
                f"BASH_ENV={bash_env}",
                f"GB10_VLLM_NO_SWAP_DOCKER_BIN={self.docker}",
                "/usr/bin/bash",
                "--noprofile",
                "--norc",
                str(VERIFIER),
                "--unit",
                str(self.unit),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test-only selector", result.stderr)
        self.assertFalse(self.command_log.exists())
        self.assertTrue(marker.exists(), "direct bash proves why production units must use env -i")
        source = VERIFIER.read_text()
        self.assertIn("/usr/bin/python3 -I", source)
