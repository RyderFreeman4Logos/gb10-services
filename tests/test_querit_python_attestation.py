from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import querit_replay_sandbox as sandbox  # noqa: E402
import querit_replay_schema as schema  # noqa: E402


@contextmanager
def pinned_chain_fixture():
    with tempfile.TemporaryDirectory() as temporary:
        authority = Path(temporary) / "authority"
        usr = authority / "usr"
        usr_bin = usr / "bin"
        usr_local = usr / "local"
        usr_local_bin = usr_local / "bin"
        usr_bin.mkdir(parents=True, mode=0o755)
        usr_local_bin.mkdir(parents=True, mode=0o755)
        executable = usr_local_bin / "python3.12"
        shutil.copy2("/bin/true", executable)
        executable.chmod(0o755)
        launcher = usr_bin / "python3"
        launcher.symlink_to(executable)
        yield {
            "ancestors": (authority, usr, usr_bin, usr_local, usr_local_bin),
            "authority": authority,
            "executable": executable,
            "launcher": launcher,
            "roots": (usr_bin, usr_local_bin),
            "usr_local": usr_local,
        }


def attest_fixture(fixture, *, links=None):
    launcher = fixture["launcher"]
    executable = fixture["executable"]
    return sandbox._attest_python_path(
        launcher,
        trusted_roots=fixture["roots"],
        trusted_ancestors=fixture["ancestors"],
        allowed_cross_root_links=(
            links
            if links is not None
            else ((fixture["roots"][0], launcher, executable),)
        ),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )


class CrossRootPythonAttestationTests(unittest.TestCase):
    def test_exact_pinned_image_cross_root_chain_is_attested(self) -> None:
        with pinned_chain_fixture() as fixture:
            attestation = attest_fixture(fixture)

            self.assertEqual(attestation["launcher_path"], str(fixture["launcher"]))
            self.assertEqual(attestation["resolved_path"], str(fixture["executable"]))
            self.assertEqual(attestation["nlink"], 1)
            self.assertRegex(attestation["chain_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(attestation["sha256"], r"^[0-9a-f]{64}$")

            fixture["executable"].write_bytes(b"replacement-python")
            fixture["executable"].chmod(0o755)
            changed = attest_fixture(fixture)
            self.assertEqual(changed["inode"], attestation["inode"])
            self.assertNotEqual(changed["chain_sha256"], attestation["chain_sha256"])
            self.assertNotEqual(changed["sha256"], attestation["sha256"])

    def test_cross_root_link_must_be_declared_exactly(self) -> None:
        with pinned_chain_fixture() as fixture:
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture, links=())
            wrong_target = fixture["executable"].with_name("python3.11")
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(
                    fixture,
                    links=((fixture["roots"][0], fixture["launcher"], wrong_target),),
                )

    def test_cross_root_chain_rejects_unsafe_roots_ancestors_and_owner(self) -> None:
        with pinned_chain_fixture() as fixture:
            fixture["usr_local"].chmod(0o775)
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)
            fixture["usr_local"].chmod(0o755)

            fixture["roots"][1].chmod(0o775)
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)
            fixture["roots"][1].chmod(0o755)

            with self.assertRaises(sandbox.SandboxError):
                sandbox._attest_python_path(
                    fixture["launcher"],
                    trusted_roots=fixture["roots"],
                    trusted_ancestors=fixture["ancestors"],
                    allowed_cross_root_links=((
                        fixture["roots"][0], fixture["launcher"], fixture["executable"]
                    ),),
                    expected_uid=os.getuid() + 1,
                    expected_gid=os.getgid(),
                )

            original_owner_check = sandbox._normalized_owner

            def reject_symlink_owner(info, expected_uid, expected_gid):
                if stat.S_ISLNK(info.st_mode):
                    raise sandbox.SandboxError("hostile symlink owner")
                return original_owner_check(info, expected_uid, expected_gid)

            with mock.patch.object(
                sandbox, "_normalized_owner", side_effect=reject_symlink_owner
            ), self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)

    def test_cross_root_chain_rejects_mutable_hardlinked_and_nonregular_targets(self) -> None:
        with pinned_chain_fixture() as fixture:
            executable = fixture["executable"]
            executable.chmod(0o775)
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)
            executable.chmod(0o755)

            executable.chmod(0o644)
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)
            executable.chmod(0o755)

            with mock.patch.object(sandbox, "_MAX_PYTHON_BYTES", 1), self.assertRaises(
                sandbox.SandboxError
            ):
                attest_fixture(fixture)

            hardlink = executable.with_name("python-hardlink")
            os.link(executable, hardlink)
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)
            hardlink.unlink()

            executable.unlink()
            executable.mkdir()
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)

    def test_cross_root_chain_rejects_relative_and_absolute_escapes(self) -> None:
        with pinned_chain_fixture() as fixture:
            launcher = fixture["launcher"]
            outside = fixture["authority"] / "outside-python"
            shutil.copy2("/bin/true", outside)
            outside.chmod(0o755)
            launcher.unlink()
            launcher.symlink_to("../outside-python")
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)
            launcher.unlink()
            launcher.symlink_to(outside)
            with self.assertRaises(sandbox.SandboxError):
                attest_fixture(fixture)

    def test_chain_rejects_cycles_and_excessive_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trusted"
            root.mkdir(mode=0o755)
            launcher = root / "python3"
            loop = root / "loop"
            launcher.symlink_to(loop.name)
            loop.symlink_to(launcher.name)
            kwargs = {
                "trusted_roots": (root,),
                "expected_uid": os.getuid(),
                "expected_gid": os.getgid(),
            }
            with self.assertRaises(sandbox.SandboxError):
                sandbox._attest_python_path(launcher, **kwargs)

            launcher.unlink()
            loop.unlink()
            executable = root / "python3.12"
            shutil.copy2("/bin/true", executable)
            executable.chmod(0o755)
            links = [launcher, *(root / f"link-{index}" for index in range(8))]
            for current, following in zip(links, links[1:]):
                current.symlink_to(following.name)
            links[-1].symlink_to(executable.name)
            with self.assertRaises(sandbox.SandboxError):
                sandbox._attest_python_path(launcher, **kwargs)

    def test_fixed_launcher_ignores_caller_path_and_sys_executable(self) -> None:
        fixed = {"resolved_path": "/usr/bin/python3.12"}
        with mock.patch.object(sandbox, "attest_system_python", return_value=fixed), mock.patch.object(
            sys, "executable", "/tmp/caller-python"
        ), mock.patch.dict(
            os.environ,
            {"PATH": "/tmp/hostile", "QUERIT_PYTHON": "/tmp/environment-python"},
            clear=False,
        ):
            command = sandbox.build_unshare_argv(["run"])
        self.assertEqual(command[5], fixed["resolved_path"])
        self.assertNotIn("/tmp/", " ".join(command))

    def test_system_contract_declares_only_the_pinned_cross_root_link(self) -> None:
        fixed = {"resolved_path": "/usr/local/bin/python3.12"}
        with mock.patch.object(sandbox, "_attest_python_path", return_value=fixed) as attest:
            self.assertIs(sandbox.attest_system_python(), fixed)
        attest.assert_called_once_with(
            Path("/usr/bin/python3"),
            trusted_roots=(Path("/usr/bin"), Path("/usr/local/bin")),
            trusted_ancestors=(
                Path("/"),
                Path("/usr"),
                Path("/usr/bin"),
                Path("/usr/local"),
                Path("/usr/local/bin"),
            ),
            allowed_cross_root_links=(
                (
                    Path("/usr/bin"),
                    Path("/usr/bin/python3"),
                    Path("/usr/local/bin/python3.12"),
                ),
            ),
            expected_uid=0,
            expected_gid=0,
        )
        with mock.patch.object(
            sandbox,
            "_attest_python_path",
            return_value={"resolved_path": "/usr/local/bin/python3.11"},
        ), self.assertRaises(sandbox.SandboxError):
            sandbox.attest_system_python()

    def test_schema_allows_only_the_exact_cross_root_resolved_path(self) -> None:
        value = {
            "authority": "runtime-attested-local-system-python",
            "chain_sha256": "a" * 64,
            "device": 1,
            "gid": 0,
            "inode": 1,
            "launcher_path": "/usr/bin/python3",
            "mode": "0755",
            "nlink": 1,
            "resolved_path": "/usr/local/bin/python3.12",
            "sha256": "b" * 64,
            "size": 1,
            "uid": 0,
        }
        self.assertIs(schema._validate_system_python(value), value)
        for field, hostile_value in (
            ("chain_sha256", "g" * 64),
            ("device", -1),
            ("inode", 0),
            ("nlink", 2),
            ("size", 0),
            ("gid", 1),
            ("sha256", "g" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(schema.SchemaError):
                schema._validate_system_python({**value, field: hostile_value})
        for hostile in (
            "/usr/local/bin/python3",
            "/usr/local/bin/python3.11",
            "/usr/local/bin/python3.12/child",
            "/usr/local/bin/../bin/python3.12",
            "/usr/local/sbin/python3.12",
            "/tmp/python3.12",
        ):
            with self.subTest(hostile=hostile), self.assertRaises(schema.SchemaError):
                schema._validate_system_python({**value, "resolved_path": hostile})


if __name__ == "__main__":
    unittest.main()
