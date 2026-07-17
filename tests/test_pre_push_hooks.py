from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
        self.bin = root / "bin"
        self.bin.mkdir()
        self.csa_log = root / "csa.log"
        csa = self.bin / "csa"
        csa.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$FAKE_CSA_LOG"\n'
            'if [ "${FAKE_CSA_MOVE_HEAD:-0}" = 1 ]; then git reset --hard -q main; fi\n'
            'exit "${FAKE_CSA_EXIT:-0}"\n'
        )
        csa.chmod(0o755)

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def run(self, updates: str, **environment: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "FAKE_CSA_LOG": str(self.csa_log),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                **environment,
            }
        )
        return subprocess.run(
            ["bash", "scripts/hooks/review-check.sh"],
            cwd=self.root,
            env=env,
            input=updates,
            text=True,
            capture_output=True,
            check=False,
        )


class PrePushHookTests(unittest.TestCase):
    def test_exact_checked_out_ref_and_sha_use_explicit_full_diff_range(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            result = fixture.run(
                f"refs/heads/feat/test {fixture.head} refs/heads/feat/test {'0' * 40}\n"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                fixture.csa_log.read_text().splitlines(),
                ["review --check-verdict --range main...HEAD"],
            )

    def test_protected_remote_ref_is_blocked_from_feature_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            result = fixture.run(
                f"refs/heads/feat/test {fixture.head} refs/heads/main {fixture.first}\n"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("protected remote ref", result.stderr)
            self.assertFalse(fixture.csa_log.exists())

    def test_review_success_is_rejected_if_the_attested_head_moves(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            result = fixture.run(
                f"refs/heads/feat/test {fixture.head} refs/heads/feat/test {'0' * 40}\n",
                FAKE_CSA_MOVE_HEAD="1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed while the gate was running", result.stderr)

    def test_explicit_refspec_cannot_substitute_a_different_sha_or_branch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            for local_ref, local_sha in (
                ("refs/heads/feat/test", fixture.first),
                ("refs/heads/other", fixture.head),
            ):
                with self.subTest(local_ref=local_ref, local_sha=local_sha):
                    result = fixture.run(
                        f"{local_ref} {local_sha} refs/heads/feature {'0' * 40}\n"
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("checked-out", result.stderr)
            self.assertFalse(fixture.csa_log.exists())

    def test_environment_bypasses_and_missing_csa_all_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            updates = (
                f"refs/heads/feat/test {fixture.head} refs/heads/feat/test {'0' * 40}\n"
            )
            for environment in (
                {"CSA_SKIP_REVIEW_CHECK": "1"},
                {"CSA_SESSION_ID": "forged"},
                {"CSA_DEPTH": "1"},
            ):
                with self.subTest(environment=environment):
                    result = fixture.run(updates, FAKE_CSA_EXIT="1", **environment)
                    self.assertNotEqual(result.returncode, 0)

            fixture.bin.joinpath("csa").unlink()
            result = fixture.run(updates)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("csa is required", result.stderr)

    def test_multiple_updates_and_empty_input_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = HookFixture(Path(raw_tmp))
            update = (
                f"refs/heads/feat/test {fixture.head} refs/heads/feat/test {'0' * 40}\n"
            )
            for payload in ("", update + update):
                with self.subTest(payload=payload):
                    result = fixture.run(payload)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("exactly one", result.stderr)


if __name__ == "__main__":
    unittest.main()
