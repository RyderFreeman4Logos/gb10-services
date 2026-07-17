from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ZERO = "0" * 40
LEFTHOOK = shutil.which("lefthook")


class HookFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git_env = os.environ.copy()
        local_env_vars = subprocess.check_output(
            ["git", "rev-parse", "--local-env-vars"],
            env=self.git_env,
            text=True,
        ).splitlines()
        for name in local_env_vars:
            self.git_env.pop(name, None)

        self.hooks = root / "scripts" / "hooks"
        self.hooks.mkdir(parents=True)
        for name in ("branch-protection.sh", "receipt-store.py", "review-check.sh"):
            shutil.copy2(ROOT / "scripts" / "hooks" / name, self.hooks / name)
        self.git_run("init", "-q", "-b", "feat/test")
        self.git_run("config", "user.email", "test@example.invalid")
        self.git_run("config", "user.name", "Test")
        (root / "tracked.txt").write_text("one\n")
        self.git_run("add", "tracked.txt")
        self.git_run("commit", "-qm", "first")
        self.first = self.git("rev-parse", "HEAD")
        self.git_run("branch", "main")
        (root / "tracked.txt").write_text("two\n")
        self.git_run("add", "tracked.txt")
        self.git_run("commit", "-qm", "second")
        self.head = self.git("rev-parse", "HEAD")
        self.first_tree = self.git("rev-parse", f"{self.first}^{{tree}}")
        self.head_tree = self.git("rev-parse", f"{self.head}^{{tree}}")

        self.remote = root / "remote.git"
        self.git_run("init", "--bare", "-q", str(self.remote), from_root=False)
        self.git_run("remote", "add", "origin", str(self.remote))
        self.git_run("update-ref", "refs/remotes/origin/main", self.first)
        self.git_run(
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        )

        self.csa_log = root / "trusted-csa.log"
        self.trusted_csa = root / "trusted-csa"
        self.trusted_csa.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$FAKE_CSA_LOG"\n'
            'if [ "${FAKE_CSA_MOVE_REF:-0}" = 1 ]; then '
            'git update-ref refs/heads/feat/test refs/heads/main; fi\n'
            'if [ "${FAKE_CSA_MOVE_BASE:-0}" = 1 ]; then '
            'git update-ref refs/remotes/origin/main refs/heads/feat/test; fi\n'
            'if [ "${FAKE_CSA_MUTATE_BINARY:-0}" = 1 ]; then '
            'printf \'# changed\\n\' >> "$0"; fi\n'
            'exit "${FAKE_CSA_EXIT:-0}"\n'
        )
        self.trusted_csa.chmod(0o755)
        review = self.hooks.joinpath("review-check.sh")
        review.write_text(
            review.read_text().replace(
                'CSA_EXECUTABLE="/home/obj/.local/bin/csa"',
                f'CSA_EXECUTABLE="{self.trusted_csa}"',
            )
        )

        self.bin = root / "bin"
        self.bin.mkdir()
        self.malicious_log = root / "malicious-csa.log"
        malicious = self.bin / "csa"
        malicious.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$FAKE_MALICIOUS_LOG"\n'
            'exit "${FAKE_MALICIOUS_EXIT:-0}"\n'
        )
        malicious.chmod(0o755)

    def git_run(self, *args: str, from_root: bool = True) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.root if from_root else None,
            env=self.git_env,
            check=True,
        )

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=self.root, env=self.git_env, text=True
        ).strip()

    def update(
        self,
        local_ref: str,
        local_sha: str,
        remote_ref: str,
        remote_old_sha: str,
    ) -> str:
        return f"{local_ref} {local_sha} {remote_ref} {remote_old_sha}\n"

    def run(
        self,
        updates: str,
        *,
        arguments: tuple[str, ...] | None = None,
        **environment: str,
    ) -> subprocess.CompletedProcess[str]:
        env = self.git_env.copy()
        env.update(
            {
                "FAKE_CSA_LOG": str(self.csa_log),
                "FAKE_MALICIOUS_LOG": str(self.malicious_log),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                **environment,
            }
        )
        if arguments is None:
            arguments = ("origin", str(self.remote))
        return subprocess.run(
            ["/usr/bin/bash", "scripts/hooks/review-check.sh", *arguments],
            cwd=self.root,
            env=env,
            input=updates,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def run_planner(
        self, updates_file: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/usr/bin/bash",
                "scripts/hooks/branch-protection.sh",
                str(updates_file),
                *arguments,
            ],
            cwd=self.root,
            env=self.git_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def receipts(self) -> list[Path]:
        return sorted(
            (self.root / ".git" / "gb10-pre-push-receipts").glob("*.receipt")
        )

    def run_topology(
        self, updates: str, **environment: str
    ) -> subprocess.CompletedProcess[str]:
        if LEFTHOOK is None:
            raise RuntimeError("lefthook is required for the hook topology contract")
        shutil.copy2(ROOT / "lefthook.yml", self.root / "lefthook.yml")
        env = self.git_env.copy()
        env.update(
            {
                "FAKE_CSA_LOG": str(self.csa_log),
                "FAKE_MALICIOUS_LOG": str(self.malicious_log),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                **environment,
            }
        )
        return subprocess.run(
            [
                LEFTHOOK,
                "run",
                "pre-push",
                "--command",
                "branch-protection",
                "--command",
                "review-check",
                "--no-auto-install",
                "origin",
                str(self.remote),
            ],
            cwd=self.root,
            env=env,
            input=updates,
            text=True,
            capture_output=True,
            check=False,
        )

    def force_commit(self) -> str:
        self.git_run("checkout", "-qb", "force-side", self.first)
        (self.root / "tracked.txt").write_text("force side\n")
        self.git_run("add", "tracked.txt")
        self.git_run("commit", "-qm", "force side")
        side = self.git("rev-parse", "HEAD")
        self.git_run("checkout", "-q", "feat/test")
        return side


class PrePushHookTests(unittest.TestCase):
    @unittest.skipIf(LEFTHOOK is None, "lefthook is not installed")
    def test_lefthook_routes_updates_only_through_the_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )

            result = fixture.run_topology(update)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(fixture.receipts()), 1)
            self.assertEqual(
                fixture.csa_log.read_text().splitlines(),
                [f"review --check-verdict --range {fixture.first}..{fixture.head}"],
            )
            malformed = fixture.run_topology("malformed\n")
            self.assertNotEqual(malformed.returncode, 0)
            self.assertIn("malformed", malformed.stdout + malformed.stderr)
            self.assertEqual(len(fixture.csa_log.read_text().splitlines()), 1)

    def test_private_btrfs_style_receipt_directory_is_accepted(self) -> None:
        fixture_parent = ROOT / "target" / "pre-push-hook-tests"
        fixture_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=fixture_parent) as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )

            result = fixture.run(update)
            receipt_dir = fixture.root / ".git" / "gb10-pre-push-receipts"
            if receipt_dir.stat().st_nlink != 1:
                self.skipTest("fixture filesystem does not expose Btrfs-style st_nlink=1")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(fixture.receipts()), 1)

    def test_planner_rejects_missing_arguments_and_unsafe_capture_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )
            capture = fixture.root / "updates"
            capture.write_text(update)
            capture.chmod(0o600)

            for arguments in ((), ("origin",)):
                with self.subTest(arguments=arguments):
                    result = fixture.run_planner(capture, *arguments)
                    self.assertNotEqual(result.returncode, 0)

            capture.chmod(0o644)
            unsafe_mode = fixture.run_planner(capture, "origin", str(fixture.remote))
            self.assertNotEqual(unsafe_mode.returncode, 0)

            capture.chmod(0o600)
            hard_link = fixture.root / "updates-hard-link"
            os.link(capture, hard_link)
            linked = fixture.run_planner(capture, "origin", str(fixture.remote))
            self.assertNotEqual(linked.returncode, 0)

            hard_link.unlink()
            arbitrary = fixture.root / "arbitrary-updates"
            arbitrary.write_text(update)
            arbitrary.chmod(0o600)
            capture.unlink()
            capture.symlink_to(arbitrary)
            symlinked = fixture.run_planner(capture, "origin", str(fixture.remote))
            self.assertNotEqual(symlinked.returncode, 0)

            capture.unlink()
            capture.mkdir(mode=0o700)
            nonregular = fixture.run_planner(capture, "origin", str(fixture.remote))
            self.assertNotEqual(nonregular.returncode, 0)

    def test_hostile_receipt_directories_fail_without_touching_targets(self) -> None:
        def update_for(fixture: HookFixture) -> str:
            return fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )

        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            target = fixture.root / "arbitrary-directory"
            target.mkdir(mode=0o700)
            sentinel = target / "sentinel"
            sentinel.write_text("untouched\n")
            receipt_dir = fixture.root / ".git" / "gb10-pre-push-receipts"
            receipt_dir.symlink_to(target, target_is_directory=True)

            result = fixture.run(update_for(fixture))

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(), "untouched\n")
            self.assertFalse(fixture.csa_log.exists())

        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            receipt_dir = fixture.root / ".git" / "gb10-pre-push-receipts"
            receipt_dir.write_text("do not truncate\n")
            receipt_dir.chmod(0o600)

            result = fixture.run(update_for(fixture))

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt_dir.read_text(), "do not truncate\n")
            self.assertFalse(fixture.csa_log.exists())

        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            receipt_dir = fixture.root / ".git" / "gb10-pre-push-receipts"
            receipt_dir.mkdir(mode=0o755)
            sentinel = receipt_dir / "sentinel"
            sentinel.write_text("untouched\n")

            result = fixture.run(update_for(fixture))

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(), "untouched\n")
            self.assertFalse(fixture.csa_log.exists())

    def test_hostile_lock_and_receipt_entries_are_never_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )
            receipt_dir = fixture.root / ".git" / "gb10-pre-push-receipts"
            receipt_dir.mkdir(mode=0o700)
            arbitrary = fixture.root / "arbitrary-target"
            arbitrary.write_text("never read or changed\n")
            arbitrary.chmod(0o600)
            (receipt_dir / ".lock").symlink_to(arbitrary)

            hostile_lock = fixture.run(update)

            self.assertNotEqual(hostile_lock.returncode, 0)
            self.assertEqual(arbitrary.read_text(), "never read or changed\n")
            self.assertFalse(fixture.csa_log.exists())

            lock_path = receipt_dir / ".lock"
            lock_path.unlink()
            lock_path.write_text("")
            lock_path.chmod(0o600)
            lock_alias = fixture.root / "lock-hard-link"
            os.link(lock_path, lock_alias)
            linked_lock = fixture.run(update)
            self.assertNotEqual(linked_lock.returncode, 0)
            self.assertEqual(lock_path.read_text(), "")
            self.assertFalse(fixture.csa_log.exists())

        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )
            first = fixture.run(update)
            self.assertEqual(first.returncode, 0, first.stderr)
            receipt = fixture.receipts()[0]
            original = receipt.read_bytes()
            original_inode = receipt.stat().st_ino
            calls_before = fixture.csa_log.read_text()

            receipt.chmod(0o644)
            unsafe_mode = fixture.run(update)
            self.assertNotEqual(unsafe_mode.returncode, 0)
            self.assertEqual(receipt.read_bytes(), original)
            self.assertEqual(receipt.stat().st_ino, original_inode)
            self.assertEqual(fixture.csa_log.read_text(), calls_before)
            receipt.chmod(0o600)

            hard_link = fixture.root / "receipt-hard-link"
            os.link(receipt, hard_link)

            linked = fixture.run(update)

            self.assertNotEqual(linked.returncode, 0)
            self.assertEqual(receipt.read_bytes(), original)
            self.assertEqual(receipt.stat().st_ino, original_inode)
            self.assertEqual(fixture.csa_log.read_text(), calls_before)

            hard_link.unlink()
            receipt.unlink()
            arbitrary = fixture.root / "arbitrary-target"
            arbitrary.write_text("never read or changed\n")
            arbitrary.chmod(0o600)
            receipt.symlink_to(arbitrary)

            symlinked = fixture.run(update)

            self.assertNotEqual(symlinked.returncode, 0)
            self.assertEqual(arbitrary.read_text(), "never read or changed\n")
            self.assertEqual(fixture.csa_log.read_text(), calls_before)

            receipt.unlink()
            os.mkfifo(receipt, mode=0o600)
            nonregular = fixture.run(update)
            self.assertNotEqual(nonregular.returncode, 0)
            self.assertEqual(fixture.csa_log.read_text(), calls_before)

    def test_concurrent_publication_creates_one_immutable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )

            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(lambda _: fixture.run(update), range(6)))

            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            receipts = fixture.receipts()
            self.assertEqual(len(receipts), 1)
            metadata = receipts[0].stat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual(metadata.st_nlink, 1)
            self.assertEqual(len(fixture.csa_log.read_text().splitlines()), 6)
            leftovers = [
                path.name
                for path in receipts[0].parent.iterdir()
                if path.name != ".lock" and path != receipts[0]
            ]
            self.assertEqual(leftovers, [])

    def test_fixture_ignores_ambient_git_local_environment(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw_outer,
            tempfile.TemporaryDirectory() as raw_fixture,
        ):
            outer = Path(raw_outer)
            fixture_root = Path(raw_fixture)
            clean_env = os.environ.copy()
            local_env_vars = subprocess.check_output(
                ["git", "rev-parse", "--local-env-vars"],
                env=clean_env,
                text=True,
            ).splitlines()
            for name in local_env_vars:
                clean_env.pop(name, None)

            def outer_git(*args: str) -> str:
                return subprocess.check_output(
                    ["git", *args], cwd=outer, env=clean_env, text=True
                ).strip()

            subprocess.run(
                ["git", "init", "-q", "-b", "ambient"],
                cwd=outer,
                env=clean_env,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Outer"],
                cwd=outer,
                env=clean_env,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "outer@example.invalid"],
                cwd=outer,
                env=clean_env,
                check=True,
            )
            (outer / "outer.txt").write_text("outer\n")
            subprocess.run(
                ["git", "add", "outer.txt"], cwd=outer, env=clean_env, check=True
            )
            subprocess.run(
                ["git", "commit", "-qm", "outer"],
                cwd=outer,
                env=clean_env,
                check=True,
            )

            def outer_state() -> dict[str, str]:
                git_dir = outer / ".git"
                return {
                    "head": outer_git("rev-parse", "HEAD"),
                    "head_ref": outer_git("symbolic-ref", "HEAD"),
                    "refs": outer_git(
                        "for-each-ref", "--format=%(refname) %(objectname)"
                    ),
                    "index": hashlib.sha256(
                        git_dir.joinpath("index").read_bytes()
                    ).hexdigest(),
                    "config": hashlib.sha256(
                        git_dir.joinpath("config").read_bytes()
                    ).hexdigest(),
                    "core.bare": outer_git("config", "--local", "--get", "core.bare"),
                    "user.name": outer_git("config", "--local", "--get", "user.name"),
                    "user.email": outer_git(
                        "config", "--local", "--get", "user.email"
                    ),
                }

            before = outer_state()
            ambient_env = clean_env.copy()
            ambient_env.update(
                {
                    "GIT_COMMON_DIR": str(outer / ".git"),
                    "GIT_CONFIG": str(outer / ".git" / "config"),
                    "GIT_DIR": str(outer / ".git"),
                    "GIT_INDEX_FILE": str(outer / ".git" / "index"),
                    "GIT_OBJECT_DIRECTORY": str(outer / ".git" / "objects"),
                    "GIT_WORK_TREE": str(fixture_root),
                }
            )
            construction_error: subprocess.CalledProcessError | None = None
            fixture_git_dir = ""
            with mock.patch.dict(os.environ, ambient_env, clear=True):
                try:
                    fixture = HookFixture(fixture_root)
                    fixture_git_dir = fixture.git("rev-parse", "--absolute-git-dir")
                except subprocess.CalledProcessError as error:
                    construction_error = error

            self.assertEqual(outer_state(), before)
            self.assertIsNone(construction_error)
            self.assertEqual(fixture_git_dir, str(fixture_root / ".git"))
            self.assertTrue((fixture_root / ".git").is_dir())

    def test_new_branch_receipt_binds_remote_update_range_and_trees(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )

            result = fixture.run(update)

            self.assertEqual(result.returncode, 0, result.stderr)
            expected_range = f"{fixture.first}..{fixture.head}"
            self.assertEqual(
                fixture.csa_log.read_text().splitlines(),
                [f"review --check-verdict --range {expected_range}"],
            )
            self.assertFalse(fixture.malicious_log.exists())
            receipts = fixture.receipts()
            self.assertEqual(len(receipts), 1)
            receipt = receipts[0].read_text()
            for expected in (
                "gb10-pre-push-receipt-v1",
                "origin",
                "refs/heads/feat/test",
                fixture.first,
                fixture.first_tree,
                fixture.head,
                fixture.head_tree,
                expected_range,
                "new",
            ):
                self.assertIn(expected, receipt)
            self.assertNotIn(str(fixture.remote), receipt)
            remote_hash = hashlib.sha256(str(fixture.remote).encode()).hexdigest()
            self.assertIn(remote_hash, receipt)

    def test_multiple_refs_are_canonical_and_altered_order_is_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            fixture.git_run("tag", "v-test", fixture.head)
            branch = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )
            tag = fixture.update(
                "refs/tags/v-test", fixture.head, "refs/tags/v-test", ZERO
            )

            first = fixture.run(branch + tag)
            receipt_paths = fixture.receipts()
            second = fixture.run(tag + branch)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(fixture.receipts(), receipt_paths)
            calls = fixture.csa_log.read_text().splitlines()
            expected = f"review --check-verdict --range {fixture.first}..{fixture.head}"
            self.assertEqual(calls, [expected, expected])

    def test_unprotected_delete_is_attested_but_protected_delete_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            fixture.git_run("update-ref", "refs/remotes/origin/old", fixture.first)
            deletion = fixture.update("(delete)", ZERO, "refs/heads/old", fixture.first)

            allowed = fixture.run(deletion)
            protected = fixture.run(
                fixture.update("(delete)", ZERO, "refs/heads/main", fixture.first)
            )

            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertIn("delete", fixture.receipts()[0].read_text())
            self.assertFalse(fixture.csa_log.exists())
            self.assertNotEqual(protected.returncode, 0)
            self.assertIn("protected", protected.stderr)

    def test_force_update_binds_remote_old_sha_and_exact_tree_transition(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            side = fixture.force_commit()
            side_tree = fixture.git("rev-parse", f"{side}^{{tree}}")
            fixture.git_run("update-ref", "refs/remotes/origin/force", fixture.head)
            update = fixture.update(
                "refs/heads/force-side", side, "refs/heads/force", fixture.head
            )

            result = fixture.run(update)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                fixture.csa_log.read_text().splitlines(),
                [f"review --check-verdict --range {fixture.head}..{side}"],
            )
            receipt = fixture.receipts()[0].read_text()
            for expected in ("force", fixture.head, fixture.head_tree, side, side_tree):
                self.assertIn(expected, receipt)

    def test_existing_branch_fast_forward_uses_the_remote_tracking_base(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            fixture.git_run(
                "update-ref",
                "refs/remotes/origin/feat/test",
                fixture.first,
            )
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                fixture.first,
            )

            result = fixture.run(update)

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = fixture.receipts()[0].read_text()
            self.assertIn("fast-forward", receipt)
            self.assertIn("refs/remotes/origin/feat/test", receipt)
            self.assertIn(f"{fixture.first}..{fixture.head}", receipt)

    def test_empty_transition_and_same_tree_commit_fail_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            fixture.git_run(
                "update-ref", "refs/remotes/origin/feat/test", fixture.head
            )
            same_sha = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                fixture.head,
            )
            metadata = fixture.git(
                "commit-tree",
                fixture.head_tree,
                "-p",
                fixture.head,
                "-m",
                "metadata only",
            )
            fixture.git_run("update-ref", "refs/heads/metadata", metadata)
            same_tree = fixture.update(
                "refs/heads/metadata",
                metadata,
                "refs/heads/feat/test",
                fixture.head,
            )

            for update in (same_sha, same_tree):
                with self.subTest(update=update):
                    result = fixture.run(update)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("empty", result.stderr)
            self.assertFalse(fixture.csa_log.exists())

    def test_ref_or_remote_base_movement_invalidates_passing_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )
            for environment in (
                {"FAKE_CSA_MOVE_REF": "1"},
                {"FAKE_CSA_MOVE_BASE": "1"},
            ):
                with self.subTest(environment=environment):
                    result = fixture.run(update, **environment)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("changed while", result.stderr)
                    fixture.git_run(
                        "update-ref", "refs/heads/feat/test", fixture.head
                    )
                    fixture.git_run(
                        "update-ref",
                        "refs/remotes/origin/main",
                        fixture.first,
                    )

    def test_stale_or_tampered_receipt_never_authorizes_a_push(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )
            first = fixture.run(update)
            self.assertEqual(first.returncode, 0, first.stderr)
            receipt = fixture.receipts()[0]
            receipt.write_text(receipt.read_text() + "forged\n")
            calls_before = fixture.csa_log.read_text()

            second = fixture.run(update)

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("receipt", second.stderr)
            self.assertEqual(fixture.csa_log.read_text(), calls_before)

    def test_remote_identity_direct_invocation_and_path_substitution_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )

            no_arguments = fixture.run(update, arguments=())
            wrong_remote = fixture.run(
                update, arguments=("origin", str(fixture.root / "other.git"))
            )
            substituted = fixture.run(
                update,
                FAKE_CSA_EXIT="1",
                FAKE_MALICIOUS_EXIT="0",
            )

            self.assertNotEqual(no_arguments.returncode, 0)
            self.assertIn("remote", no_arguments.stderr)
            self.assertNotEqual(wrong_remote.returncode, 0)
            self.assertIn("remote", wrong_remote.stderr)
            self.assertNotEqual(substituted.returncode, 0)
            self.assertTrue(fixture.csa_log.exists())
            self.assertFalse(fixture.malicious_log.exists())

    def test_csa_binary_mutation_malformed_updates_and_environment_bypasses_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )
            mutated = fixture.run(update, FAKE_CSA_MUTATE_BINARY="1")
            self.assertNotEqual(mutated.returncode, 0)
            self.assertIn("executable changed", mutated.stderr)

        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                ZERO,
            )
            for payload in ("", update + "malformed\n", update + update):
                with self.subTest(payload=payload):
                    result = fixture.run(payload)
                    self.assertNotEqual(result.returncode, 0)
            for environment in (
                {"CSA_SKIP_REVIEW_CHECK": "1"},
                {"CSA_SESSION_ID": "forged"},
                {"CSA_DEPTH": "1"},
            ):
                with self.subTest(environment=environment):
                    result = fixture.run(update, FAKE_CSA_EXIT="1", **environment)
                    self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
