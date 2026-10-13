from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class StackRecoveryTests(unittest.TestCase):
    def setUp(self):
        try:
            self.c = importlib.import_module("gb10_stack_recovery")
        except ModuleNotFoundError:
            self.fail("owned stack recovery coordinator is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "recovery-test"
        self.run.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.invocation = "a" * 32
        self.manifest = dict(version=1, receipt_id=self.run.name,
            unit_name="gb10-stack-recovery@recovery-test.service",
            boot_id="b" * 32, config_revision=1, target_device=1,
            target_inode=2, container_id="c" * 64,
            control_group="/test/old", threshold_bytes=100,
            deadline_boottime_ms=1000000)
        self.write("manifest.json", self.manifest)
        self.host = FakeHost(self.c)
        self.host.generations[self.c.TEXT].update(cid=self.manifest["container_id"],
            cgroup=self.manifest["control_group"])
        self.recovery = self.c.Recovery(self.run, self.host, self.state, self.invocation)
        self.recovery.startup_min = 101

    def write(self, name, value):
        path = self.run / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def permit(self):
        ready = self.run / "ready.json"
        (self.run / "permit.json").write_bytes(ready.read_bytes())
        (self.run / "permit.json").chmod(0o600)

    def execute(self):
        self.host.on_wait = self.permit
        self.recovery.run()

    def test_permit_is_required_before_any_model_mutation(self):
        self.host.on_wait = lambda: setattr(self.host, "now", 1001)
        with self.assertRaises(RuntimeError):
            self.recovery.run()
        self.assertEqual(self.host.actions, [])
        self.recovery.cleanup()
        self.assertEqual(self.host.actions, [])
        self.assertFalse((self.run / "result.json").exists())

    def test_missing_or_emergency_only_calibration_rejects_before_ready(self):
        for calibration in (None, 0, 100, True):
            owner = self.c.Recovery(self.run, self.host, self.state, self.invocation)
            owner.startup_min = calibration
            self.host.on_wait = self.permit
            with self.assertRaises(RuntimeError): owner.run()
        self.assertFalse((self.run / "ready.json").exists())
        self.assertEqual(self.host.actions, [])

    def test_success_stops_all_then_starts_in_dependency_order(self):
        self.execute()
        self.assertEqual(self.host.actions, [("stop", x) for x in self.c.STOP_ORDER]
                         + [("start", x) for x in self.c.START_ORDER])
        self.assertEqual(json.loads((self.run / "result.json").read_text())["status"], "succeeded")
        self.assertFalse((self.state / "recovery-owner.json").exists())
        self.assertEqual(self.host.probes, list(self.c.START_ORDER))

    def test_mismatched_permit_never_mutates_models(self):
        def wrong():
            ready = json.loads((self.run / "ready.json").read_text())
            ready["invocation_id"] = "f" * 32
            self.write("permit.json", ready)
        self.host.on_wait = wrong
        with self.assertRaises(RuntimeError):
            self.recovery.run()
        self.assertEqual(self.host.actions, [])

    def test_investigation_or_stale_recovery_blocks_before_ready(self):
        for name in ("investigation.lock", "recovery-owner.json"):
            with self.subTest(name=name):
                marker = self.state / name
                marker.write_text("stale")
                with self.assertRaises(RuntimeError):
                    self.recovery.run()
                self.assertEqual(self.host.actions, [])
                self.assertFalse((self.run / "ready.json").exists())
                marker.unlink()

    def test_target_replacement_rejected_before_permit(self):
        self.host.target_matches = False
        with self.assertRaises(RuntimeError):
            self.recovery.run()
        self.assertEqual(self.host.actions, [])

    def test_automatic_restart_is_not_adopted_as_owned_start(self):
        original = self.host.start
        def restarted(unit):
            original(unit)
            self.host.generations[unit]["restarts"] = 1
        self.host.start = restarted
        with self.assertRaises(RuntimeError): self.execute()
        self.assertFalse((self.run / "result.json").exists())

    def test_startup_rejects_container_replacement_in_same_invocation(self):
        original = self.host.snapshot
        observations = 0
        def changing(unit):
            nonlocal observations
            result = original(unit)
            if unit == self.c.START_ORDER[0] and ("start", unit) in self.host.actions:
                observations += 1
                if observations == 1:
                    result["running"] = False
                else:
                    self.host.generations[unit]["cid"] = "replacement"
                    result = original(unit)
            return result
        self.host.snapshot = changing
        with self.assertRaises(RuntimeError): self.execute()
        self.assertFalse((self.run / "result.json").exists())

    def test_generation_replaced_after_journal_before_stop_is_not_killed(self):
        original = self.recovery.save
        def replace():
            original()
            if self.recovery.journal["intent"] == ["stop", self.c.TEXT]:
                self.host.generations[self.c.TEXT]["invocation"] = "replacement"
        with patch.object(self.recovery, "save", side_effect=replace):
            with self.assertRaises(RuntimeError): self.execute()
        self.assertEqual(self.host.actions, [])

    def test_validated_target_replaced_before_journal_is_not_adopted(self):
        target = self.host.target
        calls = 0
        def replaced(manifest):
            nonlocal calls
            validated = target(manifest)
            calls += 1
            if calls == 2:
                self.host.generations[self.c.TEXT].update(invocation="foreign", cid="foreign")
            return validated
        self.host.target = replaced
        with self.assertRaises(RuntimeError): self.execute()
        self.assertEqual(self.host.actions, [])
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertEqual(self.host.actions, [])
        self.assertTrue(self.recovery.marker.exists())
        self.assertFalse((self.run / "result.json").exists())

    def test_cleanup_retains_validated_snapshot_instead_of_recapturing_owner(self):
        self.host.fail_probe = self.c.EMBED
        with self.assertRaises(RuntimeError): self.execute()
        snapshot = self.host.snapshot
        observations = 0
        def replaced(unit):
            nonlocal observations
            if unit == self.c.TEXT:
                observations += 1
                if observations == 2:
                    self.host.generations[unit].update(invocation="foreign", cid="foreign", running=True)
            return snapshot(unit)
        self.host.snapshot = replaced
        before = list(self.host.actions)
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)
        self.assertTrue(self.recovery.marker.exists())
        self.assertFalse((self.run / "result.json").exists())

    def test_unsubmitted_start_intent_cannot_authorize_foreign_generation(self):
        save = self.recovery.save
        def crash():
            save()
            if self.recovery.journal["intent"] == ["start", self.c.EMBED]:
                raise RuntimeError("crash before start submission")
        with patch.object(self.recovery, "save", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "crash before start submission"):
                self.execute()
        self.assertFalse(any(action == "start" for action, _ in self.host.actions))
        self.host.generations[self.c.EMBED].update(invocation="foreign", cid="foreign", running=True, restarts=0)
        before = list(self.host.actions)
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)
        self.assertTrue(self.recovery.marker.exists())
        self.assertFalse((self.run / "result.json").exists())

    def test_submitted_but_uncaptured_start_holds_without_generation_proof(self):
        start = self.host.start
        def crash(unit):
            start(unit)
            raise RuntimeError("crash after submit before capture")
        self.host.start = crash
        with self.assertRaisesRegex(RuntimeError, "crash after submit before capture"):
            self.execute()
        before = list(self.host.actions)
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)
        self.assertTrue(self.recovery.marker.exists())
        self.assertFalse((self.run / "result.json").exists())

    def test_startup_binds_container_when_first_observed_before_running(self):
        snapshot = self.host.snapshot
        observations = 0
        def changing(unit):
            nonlocal observations
            result = snapshot(unit)
            if unit == self.c.EMBED and ("start", unit) in self.host.actions:
                observations += 1
                if observations == 1:
                    result.update(cid=None, cgroup=None, device=None, inode=None, running=False)
                elif observations == 2:
                    result["running"] = False
                else:
                    self.host.generations[unit]["cid"] = "foreign"
                    result = snapshot(unit)
            return result
        self.host.snapshot = changing
        with self.assertRaises(RuntimeError): self.execute()
        before = list(self.host.actions)
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)
        self.assertTrue(self.recovery.marker.exists())
        self.assertFalse((self.run / "result.json").exists())

    def test_success_cleanup_rechecks_whole_stack_after_last_probe(self):
        with patch.object(self.recovery, "release_marker", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError): self.execute()
        (self.run / "result.json").unlink()
        ready = self.host.ready
        def replaced(unit, deadline):
            ready(unit, deadline)
            if unit == self.c.TEXT:
                self.host.generations[self.c.EMBED].update(invocation="foreign", cid="foreign")
        self.host.ready = replaced
        before = list(self.host.actions)
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)
        self.assertTrue(self.recovery.marker.exists())
        self.assertFalse((self.run / "result.json").exists())

    def test_startup_rejects_same_container_restarted_at_new_time(self):
        snapshot = self.host.snapshot
        observations = 0
        def restarted(unit):
            nonlocal observations
            result = snapshot(unit)
            if unit == self.c.EMBED and ("start", unit) in self.host.actions:
                observations += 1
                result["started"] = "first" if observations == 1 else "replacement"
                if observations == 1: result["running"] = False
            return result
        self.host.snapshot = restarted
        with self.assertRaises(RuntimeError): self.execute()
        self.assertFalse((self.run / "result.json").exists())

    def test_cleanup_rejects_same_container_restarted_at_new_time(self):
        start = self.host.start
        def started(unit):
            start(unit)
            self.host.generations[unit]["started"] = "first"
        self.host.start = started
        self.host.fail_probe = self.c.EMBED
        with self.assertRaises(RuntimeError): self.execute()
        self.host.generations[self.c.EMBED]["started"] = "replacement"
        before = list(self.host.actions)
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)
        self.assertTrue(self.recovery.marker.exists())
        self.assertFalse((self.run / "result.json").exists())

    def test_template_is_disabled_and_cleanup_is_owned_by_manager(self):
        unit = Path(__file__).resolve().parents[1] / "profile/llm-guard-proxy/gb10-stack-recovery@.service"
        self.assertTrue(unit.exists(), "disabled recovery template missing")
        text = unit.read_text()
        self.assertNotIn("[Install]", text)
        self.assertIn("Type=oneshot", text)
        self.assertIn("BindsTo=llm-guard-proxy.service", text)
        self.assertIn("ExecStopPost=", text)
        self.assertIn("gb10_stack_recovery.py cleanup", text)

    def test_readiness_failure_settles_owned_models_without_success(self):
        self.host.fail_probe = self.c.START_ORDER[1]
        with self.assertRaises(RuntimeError):
            self.execute()
        self.recovery.cleanup()
        self.assertTrue(all(not x["running"] for x in self.host.generations.values()))
        self.assertEqual(json.loads((self.run / "result.json").read_text())["status"], "stopped")
        self.assertFalse((self.state / "recovery-owner.json").exists())

    def test_memory_not_released_prevents_any_start(self):
        self.host.release_memory = False
        self.host.on_wait = lambda: (self.permit(), setattr(self.host, "now", self.host.now + 100))
        with self.assertRaises(RuntimeError):
            self.recovery.run()
        self.assertFalse(any(action == "start" for action, _ in self.host.actions))

    def test_cleanup_accepts_already_stopped_old_unit(self):
        self.host.fail_probe = self.c.START_ORDER[0]
        with self.assertRaises(RuntimeError): self.execute()
        self.host.generations[self.c.TEXT].update(invocation="", cid=None, settled=True)
        self.recovery.cleanup()
        self.assertEqual(json.loads((self.run / "result.json").read_text())["status"], "stopped")

    def test_sigkill_leaves_marker_and_new_process_settles_owned_start(self):
        import signal
        import time
        state_file = self.root / "fake-host.json"
        barrier = self.root / "probe-entered"
        original_ready = self.host.ready
        def blocked(unit, deadline):
            state_file.write_text(json.dumps(self.host.generations))
            barrier.touch()
            time.sleep(30)
        self.host.ready = blocked
        pid = os.fork()
        if pid == 0:
            try:
                self.execute()
            finally:
                os._exit(3)
        try:
            until = time.monotonic() + 5
            while not barrier.exists() and time.monotonic() < until: time.sleep(.01)
            self.assertTrue(barrier.exists())
            self.assertTrue((self.state / "recovery-owner.json").exists())
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        self.host.ready = original_ready
        self.host.generations = json.loads(state_file.read_text())
        self.c.Recovery(self.run, self.host, self.state, self.invocation).cleanup()
        self.assertTrue(all(not x["running"] for x in self.host.generations.values()))
        self.assertFalse((self.state / "recovery-owner.json").exists())

    def test_cleanup_rejects_foreign_marker_without_stopping(self):
        self.host.fail_probe = self.c.START_ORDER[0]
        with self.assertRaises(RuntimeError):
            self.execute()
        before = list(self.host.actions)
        marker = self.state / "recovery-owner.json"
        value = json.loads(marker.read_text())
        value["invocation_id"] = "d" * 32
        marker.write_text(json.dumps(value))
        with self.assertRaises(RuntimeError):
            self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)

    def test_success_unlink_crash_cleanup_preserves_ready_stack(self):
        with patch.object(self.recovery, "release_marker", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.execute()
        before = list(self.host.actions)
        self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)
        self.assertFalse((self.state / "recovery-owner.json").exists())

    def test_cleanup_rejects_outside_protocol_generation_replacement(self):
        self.host.fail_probe = self.c.START_ORDER[0]
        with self.assertRaises(RuntimeError):
            self.execute()
        self.host.generations[self.c.START_ORDER[0]]["invocation"] = "foreign"
        before = list(self.host.actions)
        with self.assertRaises(RuntimeError):
            self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)
        self.assertTrue((self.state / "recovery-owner.json").exists())

    def test_cleanup_rejects_replacement_container_in_same_unit_invocation(self):
        self.host.fail_probe = self.c.START_ORDER[0]
        with self.assertRaises(RuntimeError): self.execute()
        self.host.generations[self.c.START_ORDER[0]]["cid"] = "foreign"
        before = list(self.host.actions)
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertEqual(self.host.actions, before)

    def test_late_cleanup_settles_but_cannot_publish_terminal_success(self):
        self.host.fail_probe = self.c.START_ORDER[0]
        with self.assertRaises(RuntimeError): self.execute()
        self.host.now = 1001
        with self.assertRaises(RuntimeError): self.recovery.cleanup()
        self.assertTrue(all(not x["running"] for x in self.host.generations.values()))
        self.assertFalse((self.run / "result.json").exists())
        self.assertTrue((self.state / "recovery-owner.json").exists())

    def test_host_rejects_wrong_docker_identity_before_process_access(self):
        host = self.c.Host()
        properties = dict(InvocationID="a"*32, MainPID="42", NRestarts="0",
                          ActiveState="active", SubState="running", Job="")
        with patch.object(host, "properties", return_value=properties), patch.object(host, "call", side_effect=[
                "a"*64, json.dumps([dict(Id="b"*64, Name="/"+self.c.MODELS[self.c.TEXT][0])])]):
            with self.assertRaisesRegex(RuntimeError, "Docker identity"):
                host.snapshot(self.c.TEXT)

    def test_manifest_owner_mode_symlink_and_extra_fields_rejected(self):
        path = self.run / "manifest.json"
        path.chmod(0o644)
        with self.assertRaises(RuntimeError): self.recovery.run()
        self.write("manifest.json", {**self.manifest, "extra": True})
        with self.assertRaises(RuntimeError): self.recovery.run()
        path.unlink()
        path.symlink_to(self.root / "missing")
        with self.assertRaises((RuntimeError, OSError)): self.recovery.run()
        self.assertEqual(self.host.actions, [])


class FakeHost:
    def __init__(self, module):
        self.module = module
        self.now = 0
        self.memory = 1000
        self.release_memory = True
        self.actions = []
        self.probes = []
        self.target_matches = True
        self.fail_probe = None
        self.on_wait = lambda: None
        self.generations = {unit: dict(invocation="old-" + unit, cid="old-" + unit,
            running=True, cgroup="/old/" + unit, device=1, inode=2)
            for unit in module.STOP_ORDER}

    def clock(self): return self.now
    def boot_id(self): return "b" * 32
    def sleep(self): self.now += 1; self.on_wait()
    def target(self, manifest):
        if not self.target_matches: raise RuntimeError("target replaced")
        return self.module.Host.target(self, manifest)
    def snapshot(self, unit): return dict(self.generations[unit])
    def stop(self, unit):
        self.actions.append(("stop", unit))
        self.generations[unit]["running"] = False
        if self.release_memory: self.memory += 100
    def start(self, unit):
        self.actions.append(("start", unit))
        self.generations[unit] = dict(invocation="new-" + unit, cid="new-" + unit,
            running=True, cgroup="/new/" + unit, device=3, inode=4, restarts=0)
    def gone(self, generation):
        return not any(x["running"] and x["cid"] == generation["cid"] for x in self.generations.values())
    def available(self): return self.memory
    def ready(self, unit, deadline):
        self.probes.append(unit)
        if self.fail_probe == unit: raise RuntimeError("readiness failed")


if __name__ == "__main__": unittest.main()
