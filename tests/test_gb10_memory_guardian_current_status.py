from __future__ import annotations

import os
import unittest

from gb10_memory_guardian_canary_harness import (
    GUARDIAN_INVOCATION,
    GUARDIAN_UNIT,
    TEXT_INVOCATION,
    TEXT_UNIT,
    CanaryHarness,
)


class StrictCanaryStateTests(CanaryHarness):
    def test_current_receipts_use_v2_generation_and_invocation_schema(self) -> None:
        result = self._run("configured-target")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_disposable_canary_rejects_missing_protected_unit(self) -> None:
        self._write_state(
            "vllm-embedding.service",
            self._state(load="not-found", active="inactive", sub="dead", pid=0),
        )
        stamp = self.runtime / "gb10-memory-guardian" / "disposable-canary.passed"
        stamp.unlink()
        result = self._run("disposable")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(stamp.exists())

    def test_configured_target_check_is_read_only_and_proves_text_identity(self) -> None:
        result = self._run("configured-target")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("read-only", result.stdout)
        self.assertFalse(self.guardian_invocations.exists())
        commands = self.commands.read_text()
        for mutation in (
            f"systemctl --user stop {TEXT_UNIT}",
            f"systemctl --user restart {TEXT_UNIT}",
            f"systemctl --user start {TEXT_UNIT}",
        ):
            self.assertNotIn(mutation, commands)

    def test_configured_target_rejects_hostile_systemd_output_mutations(self) -> None:
        original = (self.states / f"{GUARDIAN_UNIT}.show").read_text()
        mutations = {
            "missing LoadState": original.replace("LoadState=loaded\n", ""),
            "duplicate ActiveState": original + "ActiveState=active\n",
            "substring Result": original.replace("Result=success\n", "Result=successfully\n"),
            "non-numeric MainPID": original.replace("MainPID=102\n", "MainPID=102oops\n"),
            "unknown field": original + "ActiveStateHint=active\n",
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                (self.states / f"{GUARDIAN_UNIT}.show").write_text(mutation)
                result = self._run("configured-target")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.guardian_invocations.exists())
                (self.states / f"{GUARDIAN_UNIT}.show").write_text(original)

    def test_configured_target_rejects_stale_target_surfaces(self) -> None:
        scenarios = []

        self._write_config("querit", "querit-cgroup.v1")
        scenarios.append(("wrong config", self._run("configured-target")))
        self._write_config("aeon-text", "text-cgroup.v1")

        text_registration = self.runtime / "gb10-memory-guardian" / "text-cgroup.v1"
        text_registration.unlink()
        self._write_registration("querit-cgroup.v1")
        scenarios.append(("only querit registration", self._run("configured-target")))
        self._write_registration("text-cgroup.v1")
        scenarios.append(("stale querit coexists with text", self._run("configured-target")))

        text_state = self.states / f"{TEXT_UNIT}.show"
        original_text = text_state.read_text()
        text_state.write_text(original_text.replace("text-cgroup.v1", "querit-cgroup.v1"))
        scenarios.append(("text unit publishes wrong registration", self._run("configured-target")))
        text_state.write_text(original_text)

        for label, result in scenarios:
            with self.subTest(label=label):
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.guardian_invocations.exists())

    def test_configured_target_requires_current_populated_cgroup_generation(self) -> None:
        cgroup = self.cgroup_root / self._control_group().removeprefix("/")
        for scenario in ("nonexistent", "replaced", "empty"):
            with self.subTest(scenario=scenario):
                self._write_guardian_status("armed")
                events = cgroup / "cgroup.events"
                if scenario == "empty":
                    events.write_text("populated 0\n")
                else:
                    events.unlink()
                    cgroup.rmdir()
                    if scenario == "replaced":
                        cgroup.mkdir()
                        (cgroup / "cgroup.events").write_text("populated 1\n")
                result = self._run("configured-target")
                if cgroup.exists():
                    (cgroup / "cgroup.events").unlink()
                    cgroup.rmdir()
                cgroup.mkdir()
                (cgroup / "cgroup.events").write_text("populated 1\n")
                self._write_guardian_status("armed")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_historical_arm_cannot_override_latest_disarmed_state(self) -> None:
        self.journal.write_text("gb10-memory-guardian: armed target aeon-text\n")
        self._write_guardian_status("disarmed")
        result = self._run("configured-target")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_configured_target_rejects_unsafe_receipt_authority(self) -> None:
        directory = self.runtime / "gb10-memory-guardian"
        for name in ("guardian-status.v2", "text-cgroup.v1"):
            path = directory / name
            original = path.read_bytes()
            with self.subTest(name=name, mutation="mode"):
                path.chmod(0o640)
                result = self._run("configured-target")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                path.chmod(0o600)
            with self.subTest(name=name, mutation="hardlink"):
                link = directory / f"{name}.extra-link"
                os.link(path, link)
                result = self._run("configured-target")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                link.unlink()
            with self.subTest(name=name, mutation="symlink"):
                target = directory / f"{name}.target"
                path.rename(target)
                path.symlink_to(target.name)
                result = self._run("configured-target")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                path.unlink()
                target.rename(path)
                self.assertEqual(path.read_bytes(), original)

    def test_configured_target_rejects_linked_or_insecure_receipt_directory(self) -> None:
        directory = self.runtime / "gb10-memory-guardian"
        alternate = self.runtime / "guardian-real"
        directory.rename(alternate)
        directory.symlink_to(alternate.name, target_is_directory=True)
        result = self._run("configured-target")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_configured_target_rejects_noncanonical_or_oversized_receipts(self) -> None:
        directory = self.runtime / "gb10-memory-guardian"
        for name in ("guardian-status.v2", "text-cgroup.v1"):
            path = directory / name
            original = path.read_bytes()
            mutations = {
                "crlf": original.replace(b"\n", b"\r\n"),
                "duplicate": original + original.splitlines(keepends=True)[0],
                "missing-final-newline": original.rstrip(b"\n"),
                "oversized": original + b"x" * 4097,
                "non-ascii": original + b"\xff\n",
            }
            for label, mutation in mutations.items():
                with self.subTest(name=name, mutation=label):
                    path.write_bytes(mutation)
                    result = self._run("configured-target")
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    path.write_bytes(original)
                    path.chmod(0o600)

    def test_status_binds_exact_registration_generation(self) -> None:
        registration = self.runtime / "gb10-memory-guardian" / "text-cgroup.v1"
        original = registration.read_bytes()
        replacement = registration.with_suffix(".replacement")
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        replacement.replace(registration)
        result = self._run("configured-target")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_status_binds_current_guardian_pid_and_invocation(self) -> None:
        status = self.runtime / "gb10-memory-guardian" / "guardian-status.v2"
        original = status.read_text()
        mutations = {
            "pid": original.replace("guardian_pid=102\n", "guardian_pid=999999\n"),
            "invocation": original.replace(
                f"guardian_invocation_id={GUARDIAN_INVOCATION}\n",
                f"guardian_invocation_id={'4' * 32}\n",
            ),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                status.write_text(mutation)
                result = self._run("configured-target")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                status.write_text(original)

    def test_systemd_generation_must_remain_stable_across_verification(self) -> None:
        registration = self.runtime / "gb10-memory-guardian" / "text-cgroup.v1"
        self._write_state(
            f"{TEXT_UNIT}.after",
            self._state(
                active="active",
                sub="running",
                pid=888,
                restarts=3,
                restart="on-failure",
                environment=f"GB10_CGROUP_REGISTRATION_PATH={registration}",
                invocation_id="5" * 32,
            ),
            literal=True,
        )
        self._write_state(
            f"{GUARDIAN_UNIT}.after",
            self._state(
                active="active",
                sub="running",
                pid=777,
                restarts=2,
                restart="always",
                invocation_id="6" * 32,
            ),
            literal=True,
        )
        result = self._run("configured-target")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cgroup_events_is_bounded_canonical_and_unlinked(self) -> None:
        events = (
            self.cgroup_root
            / self._control_group().removeprefix("/")
            / "cgroup.events"
        )
        original = events.read_bytes()
        mutations = {
            "duplicate": b"populated 1\npopulated 1\n",
            "malformed": b"populated=1\n",
            "noncanonical": b"populated 1\r\n",
            "oversized": b"populated 1\n" + b"x" * 4097,
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                events.write_bytes(mutation)
                result = self._run("configured-target")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                events.write_bytes(original)
        backing = events.with_name("events-backing")
        events.rename(backing)
        os.link(backing, events)
        result = self._run("configured-target")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_text_and_guardian_invocation_ids_are_strict(self) -> None:
        for unit, invocation in (
            (TEXT_UNIT, TEXT_INVOCATION),
            (GUARDIAN_UNIT, GUARDIAN_INVOCATION),
        ):
            path = self.states / f"{unit}.show"
            original = path.read_text()
            for label, value in (
                ("missing", ""),
                ("malformed", "xyz"),
                ("duplicate", f"{invocation}\nInvocationID={invocation}"),
            ):
                with self.subTest(unit=unit, mutation=label):
                    path.write_text(original.replace(f"InvocationID={invocation}", f"InvocationID={value}"))
                    result = self._run("configured-target")
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    path.write_text(original)

    def test_disposable_canary_uses_same_strict_bounded_state_parser(self) -> None:
        result = self._run("disposable")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.commands.read_text()
        self.assertIn("systemd-run", commands)
        self.assertIn("--property=LoadState", commands)
        self.assertIn("--property=ExecMainStatus", commands)

    def test_disposable_canary_rejects_mutated_driver_result(self) -> None:
        driver = self.states / "gb10-memory-guardian-canary.service.show"
        driver.write_text(driver.read_text().replace("Result=success\n", "Result=successfully\n"))
        result = self._run("disposable")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_disposable_canary_rejects_transitional_target_state(self) -> None:
        self._write_state(
            "gb10-memory-guardian-disposable-canary.service.after",
            self._state(
                active="deactivating",
                sub="stop-sigterm",
                pid=0,
                result="signal",
                exit_status=15,
            ),
            literal=True,
        )
        result = self._run("disposable")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
