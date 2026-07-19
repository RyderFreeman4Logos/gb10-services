from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "justfile"
LEFTHOOK = ROOT / "lefthook.yml"
SYSTEMD_VERIFY = ROOT / "scripts" / "verify_systemd_units.py"


class LocalGateContractTests(unittest.TestCase):
    def test_repository_uses_local_gates_only(self) -> None:
        self.assertFalse((ROOT / ".github").exists())
        self.assertTrue(JUSTFILE.is_file())
        self.assertTrue(LEFTHOOK.is_file())
        self.assertFalse((ROOT / "Cargo.toml").exists())
        self.assertFalse((ROOT / "Cargo.lock").exists())
        self.assertFalse((ROOT / "crates").exists())

    def test_justfile_exposes_complete_python_and_systemd_gates(self) -> None:
        justfile = JUSTFILE.read_text()
        for contract in (
            "bash -n",
            "python3 -m unittest discover",
            "python3 scripts/verify_systemd_units.py",
            "git diff --check",
            "quick-check:",
            "systemd-check:",
            "pre-push: quick-check systemd-check",
        ):
            self.assertIn(contract, justfile)
        for obsolete in (
            "cargo ",
            "rust-check:",
            "GB10_LOCAL_GATE_JOBS",
            "GB10_LOCAL_TEST_THREADS",
        ):
            self.assertNotIn(obsolete, justfile)

    def test_lefthook_routes_commit_and_push_to_local_just_recipes(self) -> None:
        lefthook = LEFTHOOK.read_text()
        self.assertIn("pre-commit:", lefthook)
        self.assertIn("run: just quick-check", lefthook)
        self.assertIn("pre-push:", lefthook)
        self.assertNotIn("run: scripts/hooks/branch-protection.sh", lefthook)
        self.assertEqual(
            lefthook.count("run: scripts/hooks/review-check.sh {1} {2}"), 1
        )
        self.assertIn("use_stdin: true", lefthook)
        self.assertIn("run: just pre-push", lefthook)

    def test_systemd_gate_uses_unprivileged_user_manager_semantics(self) -> None:
        helper = SYSTEMD_VERIFY.read_text()
        self.assertIn('"--user"', helper)
        self.assertIn('environment["SYSTEMD_UNIT_PATH"]', helper)
        self.assertNotIn('"--root', helper)
        self.assertNotIn("/etc/systemd/system", helper)


if __name__ == "__main__":
    unittest.main()
