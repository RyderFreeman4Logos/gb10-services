from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "justfile"
LEFTHOOK = ROOT / "lefthook.yml"
SYSTEMD_VERIFY = ROOT / "scripts" / "verify_systemd_units.py"
GITIGNORE = ROOT / ".gitignore"


class LocalGateContractTests(unittest.TestCase):
    def test_repository_uses_local_gates_only(self) -> None:
        self.assertFalse((ROOT / ".github").exists())
        self.assertTrue(JUSTFILE.is_file())
        self.assertTrue(LEFTHOOK.is_file())
        self.assertIn("/target/", GITIGNORE.read_text().splitlines())

    def test_justfile_exposes_bounded_parallelism_and_complete_gates(self) -> None:
        justfile = JUSTFILE.read_text()
        for contract in (
            "GB10_LOCAL_GATE_JOBS",
            "GB10_LOCAL_TEST_THREADS",
            "bash -n",
            "python3 -m unittest discover",
            "python3 scripts/verify_systemd_units.py",
            "cargo fmt --all -- --check",
            "cargo clippy --workspace --all-targets --all-features",
            "cargo test --workspace --all-features",
            "git diff --check",
            "quick-check:",
            "pre-push:",
        ):
            self.assertIn(contract, justfile)

    def test_lefthook_routes_commit_and_push_to_local_just_recipes(self) -> None:
        lefthook = LEFTHOOK.read_text()
        self.assertIn("pre-commit:", lefthook)
        self.assertIn("run: just quick-check", lefthook)
        self.assertIn("pre-push:", lefthook)
        self.assertIn("run: just pre-push", lefthook)

    def test_systemd_gate_uses_unprivileged_user_manager_semantics(self) -> None:
        helper = SYSTEMD_VERIFY.read_text()
        self.assertIn('"--user"', helper)
        self.assertIn('environment["SYSTEMD_UNIT_PATH"]', helper)
        self.assertNotIn('"--root', helper)
        self.assertNotIn("/etc/systemd/system", helper)


if __name__ == "__main__":
    unittest.main()
