from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "justfile"
LEFTHOOK = ROOT / "lefthook.yml"
SYSTEMD_VERIFY = ROOT / "scripts" / "verify_systemd_units.py"
LOOP_RECOVERY_SMOKE = ROOT / "scripts" / "llm_guard_proxy_loop_recovery_smoke.py"
ACTIVE_GUARD_DOCS = (ROOT / "README.md", ROOT / "docs" / "deployment" / "AGENTS.md")


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
        # Config-only repo: no CSA review gate on push.
        self.assertNotIn("scripts/hooks/review-check.sh", lefthook)
        self.assertNotIn("use_stdin: true", lefthook)
        self.assertIn("run: just pre-push", lefthook)

    def test_justfile_exposes_guard_loop_recovery_operational_gate(self) -> None:
        justfile = JUSTFILE.read_text()
        self.assertTrue(LOOP_RECOVERY_SMOKE.is_file())
        self.assertIn("guard-loop-recovery-smoke:", justfile)
        self.assertIn("guard-loop-recovery-unit", justfile)
        self.assertIn("LLM_GUARD_PROXY_BINARY", justfile)
        self.assertIn(str(LOOP_RECOVERY_SMOKE.relative_to(ROOT)), justfile)

    def test_loop_recovery_smoke_scans_private_prefix_marker_at_every_sink(self) -> None:
        smoke = LOOP_RECOVERY_SMOKE.read_text()
        for contract in (
            'sensitive_markers = tuple("S" + secrets.token_hex(24) for _ in range(6))',
            "self.private_prefix_marker = private_prefix_marker",
            'f"{fixture.private_prefix_marker} derive the isolated invariant before answering\\n"',
            '"private_prefix_present": private_prefix_present',
            '"private_prefix_marker_leak_count": raw.count(private_prefix_marker.encode())',
            'attempt["private_prefix_present"]',
            '"non_salvage_replayed_private_material"',
            '"group_quiesced"',
            "os.O_NOFOLLOW",
            "tuple(marker.encode() for marker in sensitive_markers)",
        ):
            self.assertIn(contract, smoke)
        self.assertGreaterEqual(
            smoke.count('["private_prefix_marker_leak_count"] == 0'),
            2,
        )
        self.assertNotIn('"private_prefix_marker":', smoke)

    def test_guard_offline_self_test_precedes_f1_network_and_binds_runtime(self) -> None:
        smoke = LOOP_RECOVERY_SMOKE.read_text()
        prerequisite = smoke.index(
            "offline_self_test, binary_identity = run_offline_self_test(binary)"
        )
        for later in (
            "free_port(), free_port()",
            "FixtureHTTPServer(",
            "isolated_config(candidate, root, fake_port, guard_port)",
            "start_candidate_supervisor(",
        ):
            self.assertLess(prerequisite, smoke.index(later, prerequisite))
        self.assertIn('Path(f"/proc/{candidate_identity[\'pid\']}/exe")', smoke)
        self.assertIn('binary, binary_identity, "final_binary_identity_drift"', smoke)

    def test_active_guard_docs_match_private_metadata_only_policy(self) -> None:
        raw_flags = (
            "capture_raw_payloads = false",
            "include_raw_payloads = false",
            "include_raw_input = false",
            "include_raw_output = false",
            "include_raw_reasoning = false",
        )
        for path in ACTIVE_GUARD_DOCS:
            text = path.read_text()
            with self.subTest(path=path):
                self.assertIn("retry-local", text)
                self.assertIn("metadata-only", text)
                for flag in raw_flags:
                    self.assertIn(flag, text)
                self.assertRegex(
                    text,
                    r"`:18014`(?s:.{0,300})`bounded_answer_from_cot`",
                )
                self.assertRegex(
                    text,
                    r"`:18011`(?s:.{0,300})`truncate_cot_then_answer`",
                )

        active_text = "\n".join(path.read_text() for path in ACTIVE_GUARD_DOCS)
        for stale_claim in (
            "raw observability payload capture are enabled",
            "include_raw_payloads = true",
            "redacted raw payloads",
            "storing redacted raw input",
            "paired raw artifacts retain",
        ):
            self.assertNotIn(stale_claim, active_text)

    def test_systemd_gate_uses_unprivileged_user_manager_semantics(self) -> None:
        helper = SYSTEMD_VERIFY.read_text()
        self.assertIn('"--user"', helper)
        self.assertIn('environment["SYSTEMD_UNIT_PATH"]', helper)
        self.assertNotIn('"--root', helper)
        self.assertNotIn("/etc/systemd/system", helper)


if __name__ == "__main__":
    unittest.main()
