from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ZERO = "0" * 40


class HookFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.hooks = root / "scripts" / "hooks"
        self.hooks.mkdir(parents=True)
        for name in ("branch-protection.sh", "review-check.sh"):
            shutil.copy2(ROOT / "scripts" / "hooks" / name, self.hooks / name)
        subprocess.run(["git", "init", "-q", "-b", "feat/test"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        (root / "tracked.txt").write_text("one\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "first"], cwd=root, check=True)
        self.first = self.git("rev-parse", "HEAD")
        subprocess.run(["git", "branch", "main"], cwd=root, check=True)
        (root / "tracked.txt").write_text("two\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "second"], cwd=root, check=True)
        self.head = self.git("rev-parse", "HEAD")
        self.first_tree = self.git("rev-parse", f"{self.first}^{{tree}}")
        self.head_tree = self.git("rev-parse", f"{self.head}^{{tree}}")

        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", self.remote], check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(self.remote)],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", self.first],
            cwd=root,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
            ],
            cwd=root,
            check=True,
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

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

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
        env = os.environ.copy()
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
        )

    def receipts(self) -> list[Path]:
        return sorted(
            (self.root / ".git" / "gb10-pre-push-receipts").glob("*.receipt")
        )

    def force_commit(self) -> str:
        subprocess.run(
            ["git", "checkout", "-qb", "force-side", self.first],
            cwd=self.root,
            check=True,
        )
        (self.root / "tracked.txt").write_text("force side\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "force side"], cwd=self.root, check=True)
        side = self.git("rev-parse", "HEAD")
        subprocess.run(["git", "checkout", "-q", "feat/test"], cwd=self.root, check=True)
        return side


class PrePushHookTests(unittest.TestCase):
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
            subprocess.run(
                ["git", "tag", "v-test", fixture.head], cwd=fixture.root, check=True
            )
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
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/old", fixture.first],
                cwd=fixture.root,
                check=True,
            )
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
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/force", fixture.head],
                cwd=fixture.root,
                check=True,
            )
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
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    "refs/remotes/origin/feat/test",
                    fixture.first,
                ],
                cwd=fixture.root,
                check=True,
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
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/feat/test", fixture.head],
                cwd=fixture.root,
                check=True,
            )
            same_sha = fixture.update(
                "refs/heads/feat/test",
                fixture.head,
                "refs/heads/feat/test",
                fixture.head,
            )
            metadata = subprocess.check_output(
                [
                    "git",
                    "commit-tree",
                    fixture.head_tree,
                    "-p",
                    fixture.head,
                    "-m",
                    "metadata only",
                ],
                cwd=fixture.root,
                text=True,
            ).strip()
            subprocess.run(
                ["git", "update-ref", "refs/heads/metadata", metadata],
                cwd=fixture.root,
                check=True,
            )
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
                    subprocess.run(
                        ["git", "update-ref", "refs/heads/feat/test", fixture.head],
                        cwd=fixture.root,
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "update-ref",
                            "refs/remotes/origin/main",
                            fixture.first,
                        ],
                        cwd=fixture.root,
                        check=True,
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
