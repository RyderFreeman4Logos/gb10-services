from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

deployment = importlib.import_module("querit_canary_deployment")
profile = deployment
runtime = importlib.import_module("querit_canary_runtime")
PRODUCTION_TARGETS = deployment.TARGETS
VERIFY_BUNDLE_CONTENTS = deployment._verify_bundle_contents


class FakeHost:
    def __init__(self, targets: tuple[dict[str, object], ...]) -> None:
        self.targets = targets
        self.commands: list[tuple[str, str]] = []
        self._masked: set[str] = set()
        self.runtime_masks: dict[str, dict[str, object] | None] = {
            unit: None for unit in deployment.CANDIDATE_UNITS
        }
        self._mask_inode = 100
        self.listener_lines: tuple[str, ...] = ()
        self.containers: dict[str, dict[str, str] | None] = {
            runtime.CONTAINER_NAMES[unit]: None
            for unit in deployment.CANDIDATE_UNITS
            if unit in runtime.CONTAINER_NAMES
        }
        self.states = {
            runtime.TEXT_UNIT: runtime.ServiceState(True, "text-before"),
            runtime.BACKEND_UNIT: runtime.ServiceState(False, ""),
            runtime.ADAPTER_UNIT: runtime.ServiceState(False, ""),
            runtime.EMBEDDING_UNIT: runtime.ServiceState(True, "embedding-before"),
            runtime.PRODUCTION_RERANKER_UNIT: runtime.ServiceState(True, "rerank-before"),
            runtime.LEGACY_RERANKER_UNIT: runtime.ServiceState(False, ""),
            runtime.GUARD_UNIT: runtime.ServiceState(True, "guard-before"),
        }
        self._lifecycle_state = False
        self._text_was_paused = False
        self._admission = {
            "mem_available_kib": profile.REQUIRED_ADMISSION_KIB,
            "mem_available_gib": 32,
            "pressure_sha256": "p" * 64,
            "swaps_sha256": "s" * 64,
        }
        self.info = {
            unit: {
                "FragmentPath": "",
                "DropInPaths": "",
                "LoadState": "not-found",
                "UnitFileState": "disabled",
            }
            for unit in deployment.CANDIDATE_UNITS
        }
        self.protected_info = {
            unit: {
                "FragmentPath": f"/protected/{unit}",
                "DropInPaths": "",
                "LoadState": "loaded",
                "UnitFileState": "enabled",
            }
            for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT)
        }

    def unit_info(self, unit: str) -> dict[str, str]:
        if unit in self.info:
            return dict(self.info[unit])
        return dict(self.protected_info[unit])

    def service_state(self, unit: str) -> runtime.ServiceState:
        return self.states[unit]

    def runtime_mask(self, unit: str) -> None:
        self.commands.append(("mask", unit))
        self._masked.add(unit)
        self._mask_inode += 1
        self.runtime_masks[unit] = {
            "scope": "runtime",
            "path": f"/run/user/1001/systemd/user/{unit}",
            "lstat": {
                "st_dev": 1,
                "st_ino": self._mask_inode,
                "st_mode": stat.S_IFLNK | 0o777,
                "type": "symlink",
            },
            "link_target": "/dev/null",
        }
        self.info[unit]["UnitFileState"] = "masked-runtime"
        self.info[unit]["FragmentPath"] = f"/run/user/1001/systemd/user/{unit}"
        self.info[unit]["LoadState"] = "masked"

    def runtime_unmask(self, unit: str) -> None:
        self.commands.append(("unmask", unit))
        self._masked.discard(unit)
        self.runtime_masks[unit] = None
        self.info[unit]["UnitFileState"] = "disabled"
        if deployment._unit_target(unit).exists():
            self.info[unit]["FragmentPath"] = str(deployment._unit_target(unit))
            self.info[unit]["LoadState"] = "loaded"
        else:
            self.info[unit]["FragmentPath"] = ""
            self.info[unit]["LoadState"] = "not-found"

    def runtime_mask_attestation(self, unit: str) -> dict[str, object] | None:
        evidence = self.runtime_masks[unit]
        if evidence is None:
            return None
        return {
            **evidence,
            "lstat": dict(evidence["lstat"]),
        }

    def daemon_reload(self) -> None:
        self.commands.append(("daemon-reload", ""))
        for unit in deployment.CANDIDATE_UNITS:
            if unit in self._masked:
                self.info[unit]["FragmentPath"] = f"/run/user/1001/systemd/user/{unit}"
                self.info[unit]["LoadState"] = "masked"
            elif deployment._unit_target(unit).exists():
                self.info[unit]["FragmentPath"] = str(deployment._unit_target(unit))
                self.info[unit]["LoadState"] = "loaded"
            else:
                self.info[unit]["FragmentPath"] = ""
                self.info[unit]["LoadState"] = "not-found"

    def listeners(self) -> tuple[str, ...]:
        return self.listener_lines

    def container(self, name: str) -> dict[str, str] | None:
        return self.containers[name]

    def admission(self) -> dict[str, object]:
        return dict(self._admission)

    def convert(self, _converter: Path, snapshot: Path, _template: Path) -> None:
        self.commands.append(("convert", str(snapshot)))
        (snapshot / "converted.marker").write_text("converted")

    def lifecycle(self, action: str, *, pause_text: bool) -> None:
        self.commands.append((f"lifecycle-{action}", str(pause_text)))
        if action == "activate":
            self._lifecycle_state = True
            if pause_text:
                self.states[runtime.TEXT_UNIT] = runtime.ServiceState(False, "")
                self._text_was_paused = True
            self.states[runtime.BACKEND_UNIT] = runtime.ServiceState(True, "backend-new")
            self.states[runtime.ADAPTER_UNIT] = runtime.ServiceState(True, "adapter-new")
        elif action == "deactivate":
            self.states[runtime.BACKEND_UNIT] = runtime.ServiceState(False, "")
            self.states[runtime.ADAPTER_UNIT] = runtime.ServiceState(False, "")
            if self._text_was_paused:
                self.states[runtime.TEXT_UNIT] = runtime.ServiceState(True, "text-restored")
                self._text_was_paused = False
            self._lifecycle_state = False
        else:
            raise AssertionError(action)

    def lifecycle_state_exists(self) -> bool:
        return self._lifecycle_state


class DeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.raw_tmp.name)
        self.lib = self.root / "lib"
        self.bin = self.root / "bin"
        self.units = self.root / "units"
        self.targets = (
            deployment._mapping("scripts/module.py", self.lib / "module.py", 0o644),
            deployment._mapping("scripts/converter.py", self.lib / "converter.py", 0o644),
            deployment._mapping("config/template.jinja", self.lib / "template.jinja", 0o644),
            deployment._mapping("scripts/ready.sh", self.bin / "ready.sh", 0o755),
            deployment._mapping(
                "systemd/vllm-querit-4b-canary.service",
                self.units / runtime.ADAPTER_UNIT,
                0o644,
            ),
            deployment._mapping(
                "systemd/vllm-querit-4b-canary-backend.service",
                self.units / runtime.BACKEND_UNIT,
                0o644,
            ),
        )
        self.targets_patch = mock.patch.object(deployment, "TARGETS", self.targets)
        self.targets_patch.start()
        self.addCleanup(self.targets_patch.stop)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir(mode=0o700)
        entries: list[dict[str, object]] = []
        for index, mapped in enumerate(self.targets):
            payload = f"payload-{index}\n".encode()
            source = self.bundle / "payload" / str(mapped["source"])
            source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source.write_bytes(payload)
            source.chmod(0o600)
            entries.append(
                {
                    "mode": mapped["mode"],
                    "path": mapped["source"],
                    "sha256": deployment._sha256(payload),
                    "size": len(payload),
                    "target": mapped["target"],
                }
            )
        self.manifest = deployment._bundle_manifest(entries, "a" * 40)
        (self.bundle / "manifest.json").write_bytes(deployment._canonical(self.manifest))
        (self.bundle / "manifest.json").chmod(0o600)
        self.source_snapshot = self.root / "source-snapshot"
        self.source_snapshot.mkdir(mode=0o700)
        (self.source_snapshot / "source.bin").write_bytes(b"source")
        self.artifact = self.root / "artifact"
        self.state = self.root / "state" / "state.json"
        self.host = FakeHost(self.targets)
        self.owner = deployment.Deployment(
            self.host,
            self.state,
            source_root=self.root,
            artifact_path=self.artifact,
        )
        self.bundle_patch = mock.patch.object(
            deployment, "verify_bundle", return_value=self.manifest
        )
        self.bundle_patch.start()
        self.addCleanup(self.bundle_patch.stop)
        self.bundle_contents_patch = mock.patch.object(
            deployment, "_verify_bundle_contents", return_value=self.manifest
        )
        self.bundle_contents_patch.start()
        self.addCleanup(self.bundle_contents_patch.stop)
        self.artifact_patch = mock.patch.object(
            deployment.artifact, "attest_source_snapshot", return_value="source-ledger"
        )
        self.artifact_patch.start()
        self.addCleanup(self.artifact_patch.stop)
        self.manifest_patch = mock.patch.object(
            deployment.artifact,
            "manifest_sha256",
            side_effect=lambda path: "m" * 64 if Path(path).exists() else (_ for _ in ()).throw(FileNotFoundError()),
        )
        self.manifest_patch.start()
        self.addCleanup(self.manifest_patch.stop)

    def tearDown(self) -> None:
        self.raw_tmp.cleanup()

    def _set_exact_runtime_mask_prestate(self) -> None:
        for unit in deployment.CANDIDATE_UNITS:
            self.host.runtime_mask(unit)
        self.host.commands.clear()

    def _runtime_mask_owner(self) -> deployment.Deployment:
        return deployment.Deployment(
            self.host,
            self.state,
            source_root=self.root,
            artifact_path=self.artifact,
            accept_runtime_mask_prestate=True,
        )

    def _unsealed_artifact_owner(self) -> deployment.Deployment:
        return deployment.Deployment(
            self.host,
            self.state,
            source_root=self.root,
            artifact_path=self.artifact,
            accept_unsealed_artifact_prestate=True,
        )

    def _write_unsealed_artifact(self) -> Path:
        self.artifact.mkdir(mode=0o750)
        nested = self.artifact / "nested"
        nested.mkdir(mode=0o750)
        original = nested / "weights.bin"
        original.write_bytes(b"unsealed weights")
        original.chmod(0o640)
        return original

    def _assert_exact_runtime_mask_prestate_restored(self) -> None:
        self.assertEqual(self.host._masked, set(deployment.CANDIDATE_UNITS))
        for unit in deployment.CANDIDATE_UNITS:
            self.assertEqual(self.host.info[unit]["UnitFileState"], "masked-runtime")
            self.assertEqual(
                self.host.info[unit]["FragmentPath"],
                f"/run/user/1001/systemd/user/{unit}",
            )
            self.assertEqual(self.host.info[unit]["DropInPaths"], "")
            self.assertEqual(self.host.info[unit]["LoadState"], "masked")
            evidence = self.host.runtime_mask_attestation(unit)
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assertEqual(evidence["scope"], "runtime")
            self.assertEqual(evidence["link_target"], "/dev/null")
        self.assertEqual(self.host.listener_lines, ())
        self.assertTrue(all(container is None for container in self.host.containers.values()))

    def test_exact_mapping_covers_ready_converter_template_and_owner(self) -> None:
        mapping = {str(item["source"]): item for item in PRODUCTION_TARGETS}
        self.assertEqual(mapping["scripts/gb10_service_ready.sh"]["mode"], 0o755)
        self.assertIn("scripts/querit_checkpoint_convert.py", mapping)
        self.assertIn("config/querit/querit-rerank.jinja", mapping)
        self.assertIn("scripts/querit_canary_deployment.py", mapping)
        self.assertIn("scripts/gb10_querit_canary_deploy.py", mapping)

    def test_owner_rejects_candidate_admission_below_profile_threshold(self) -> None:
        self.host._admission["mem_available_kib"] = profile.REQUIRED_ADMISSION_KIB - 1

        with self.assertRaisesRegex(
            deployment.DeploymentError, "candidate admission is below profile threshold"
        ):
            self.owner.plan(self.bundle)

        self.assertEqual(self.host.commands, [])

    def test_owner_rejects_bundle_unit_profile_mismatch_before_mutation(self) -> None:
        payload = (
            self.bundle / "payload" / "systemd" / runtime.BACKEND_UNIT
        )
        changed = (
            (ROOT / "systemd" / runtime.BACKEND_UNIT)
            .read_text()
            .replace("--gpu-memory-utilization 0.17", "--gpu-memory-utilization 0.16")
            .encode()
        )
        payload.write_bytes(changed)
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        entries = manifest["files"]
        entry = next(
            item
            for item in entries
            if item["path"] == "systemd/vllm-querit-4b-canary-backend.service"
        )
        entry["sha256"] = deployment._sha256(changed)
        entry["size"] = len(changed)
        (self.bundle / "manifest.json").write_bytes(
            deployment._canonical(deployment._bundle_manifest(entries, "a" * 40))
        )

        with self.assertRaisesRegex(
            deployment.DeploymentError, "profile authority"
        ):
            VERIFY_BUNDLE_CONTENTS(self.bundle)

        self.assertEqual(self.host.commands, [])

    def test_prepare_install_activate_and_rollback_restore_exact_prestate(self) -> None:
        self.owner.prepare(self.bundle)
        self.assertEqual(
            self.host.commands[:2],
            [("mask", runtime.BACKEND_UNIT), ("mask", runtime.ADAPTER_UNIT)],
        )

        self.owner.install(self.source_snapshot)
        self.assertTrue(self.artifact.exists())
        self.assertIn(("convert", str(self.artifact.parent / next(path.name for path in self.artifact.parent.iterdir() if path.name.startswith(".gb10-querit-owner-")) / "converted")), self.host.commands)
        for unit in deployment.CANDIDATE_UNITS:
            self.assertEqual(self.host.info[unit]["UnitFileState"], "disabled")
        for mapped in self.targets:
            target = Path(str(mapped["target"]))
            self.assertTrue(target.exists())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), mapped["mode"])

        self.owner.activate(pause_text=True)
        self.assertIn(("lifecycle-activate", "True"), self.host.commands)
        self.owner.rollback()

        self.assertFalse(self.state.exists())
        self.assertFalse(self.artifact.exists())
        self.assertIn(("lifecycle-deactivate", "False"), self.host.commands)
        self.assertTrue(self.host.states[runtime.TEXT_UNIT].active)
        for mapped in self.targets:
            self.assertFalse(Path(str(mapped["target"])).exists())
        self.assertEqual(self.host._masked, set())
        self.assertFalse(
            any(
                action in {"start", "stop"}
                for action, _value in self.host.commands
            )
        )

    def test_runtime_mask_prestate_requires_opt_in_and_records_ownership(self) -> None:
        self._set_exact_runtime_mask_prestate()

        with self.assertRaisesRegex(
            deployment.DeploymentError, "foreign runtime candidate mask"
        ):
            self.owner.prepare(self.bundle)
        self.assertEqual(self.host.commands, [])

        owner = self._runtime_mask_owner()
        owner.prepare(self.bundle)

        record = owner._read()
        self.assertEqual(
            record["runtime_mask_ownership"],
            {
                "accepted_prestate": True,
                "owned_units": list(deployment.CANDIDATE_UNITS),
                "removed_units": [],
                "restored_units": [],
            },
        )
        self.assertEqual(self.host.commands, [])

    def test_plan_declares_runtime_mask_ownership_without_mutation(self) -> None:
        self._set_exact_runtime_mask_prestate()
        plan = self._runtime_mask_owner().plan(self.bundle)

        self.assertEqual(plan["schema"], "gb10-querit-canary-plan-v1")
        self.assertEqual(
            plan["candidate_runtime_mask_ownership"],
            {
                "accepted_prestate": True,
                "owned_units": list(deployment.CANDIDATE_UNITS),
                "remove_before_activation": list(deployment.CANDIDATE_UNITS),
                "restore_on_rollback": list(deployment.CANDIDATE_UNITS),
            },
        )
        self.assertEqual(self.host.commands, [])
        self.assertFalse(self.state.exists())

    def test_opt_in_rejects_noncanonical_runtime_mask_or_candidate_activity(self) -> None:
        def persistent() -> None:
            self.host.info[runtime.BACKEND_UNIT]["UnitFileState"] = "masked"

        def wrong_target() -> None:
            evidence = self.host.runtime_masks[runtime.BACKEND_UNIT]
            assert evidence is not None
            evidence["link_target"] = "/wrong/null"

        def regular_file() -> None:
            evidence = self.host.runtime_masks[runtime.BACKEND_UNIT]
            assert evidence is not None
            lstat = evidence["lstat"]
            assert isinstance(lstat, dict)
            lstat["st_mode"] = stat.S_IFREG | 0o600
            lstat["type"] = "regular"

        def mixed() -> None:
            self.host.runtime_unmask(runtime.ADAPTER_UNIT)
            self.host.commands.clear()

        def missing() -> None:
            self.host.runtime_masks[runtime.BACKEND_UNIT] = None

        def active() -> None:
            self.host.states[runtime.BACKEND_UNIT] = runtime.ServiceState(True, "foreign")

        def listener() -> None:
            self.host.listener_lines = ("LISTEN 0 1 100.105.4.92:18014",)

        def container() -> None:
            self.host.containers[runtime.CONTAINER_NAMES[runtime.BACKEND_UNIT]] = {
                "id": "a" * 64,
                "image": "candidate",
                "config_image": "candidate",
                "pid": "0",
                "running": "false",
            }

        cases = {
            "persistent": persistent,
            "wrong target": wrong_target,
            "regular file": regular_file,
            "mixed": mixed,
            "missing": missing,
            "active": active,
            "listener": listener,
            "container": container,
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self._set_exact_runtime_mask_prestate()
                mutate()
                with self.assertRaises(deployment.DeploymentError):
                    self._runtime_mask_owner().prepare(self.bundle)
                self.assertEqual(self.host.commands, [])
                self.assertFalse(self.state.exists())

    def test_system_host_runtime_mask_attestation_requires_exact_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime_root = Path(raw_tmp) / "runtime" / "systemd" / "user"
            runtime_root.mkdir(parents=True)
            path = runtime_root / runtime.BACKEND_UNIT
            path.symlink_to("/dev/null")
            with mock.patch.object(deployment, "RUNTIME_UNIT_ROOT", runtime_root):
                host = deployment.SystemHost(model_root=self.artifact)
                evidence = host.runtime_mask_attestation(runtime.BACKEND_UNIT)
                self.assertIsNotNone(evidence)
                assert evidence is not None
                self.assertEqual(evidence["path"], str(path))
                self.assertEqual(evidence["link_target"], "/dev/null")

                path.unlink()
                path.symlink_to("/wrong/null")
                with self.assertRaisesRegex(deployment.DeploymentError, "target"):
                    host.runtime_mask_attestation(runtime.BACKEND_UNIT)

                path.unlink()
                path.write_text("not a symlink")
                with self.assertRaisesRegex(deployment.DeploymentError, "not a symlink"):
                    host.runtime_mask_attestation(runtime.BACKEND_UNIT)

    def test_first_runtime_mask_removal_failure_restores_exact_prestate(self) -> None:
        self._set_exact_runtime_mask_prestate()
        owner = self._runtime_mask_owner()
        original_unmask = self.host.runtime_unmask
        calls = 0

        def fail_after_first(unit: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise deployment.DeploymentError("second runtime unmask failed")
            original_unmask(unit)

        with mock.patch.object(self.host, "runtime_unmask", side_effect=fail_after_first):
            with self.assertRaisesRegex(deployment.DeploymentError, "second runtime unmask failed"):
                owner.deploy(self.bundle, self.source_snapshot, pause_text=False)

        self.assertFalse(self.state.exists())
        self._assert_exact_runtime_mask_prestate_restored()

    def test_post_unmask_prestart_failure_restores_both_runtime_masks(self) -> None:
        self._set_exact_runtime_mask_prestate()
        owner = self._runtime_mask_owner()
        with mock.patch.object(
            owner,
            "_verify_installed",
            side_effect=deployment.DeploymentError("prestart verification failed"),
        ):
            with self.assertRaisesRegex(deployment.DeploymentError, "prestart verification failed"):
                owner.deploy(self.bundle, self.source_snapshot, pause_text=False)

        self.assertFalse(self.state.exists())
        self._assert_exact_runtime_mask_prestate_restored()

    def test_activate_then_deactivate_restores_masks_without_touching_protected_units(self) -> None:
        self._set_exact_runtime_mask_prestate()
        protected_states = {
            unit: self.host.states[unit]
            for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT)
        }
        protected_info = {
            unit: self.host.unit_info(unit)
            for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT)
        }
        owner = self._runtime_mask_owner()
        owner.prepare(self.bundle)
        owner.install(self.source_snapshot)
        owner.activate(pause_text=False)
        owner.rollback()

        self.assertFalse(self.state.exists())
        self._assert_exact_runtime_mask_prestate_restored()
        self.assertEqual(
            {
                unit: self.host.states[unit]
                for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT)
            },
            protected_states,
        )
        self.assertEqual(
            {
                unit: self.host.unit_info(unit)
                for unit in (*runtime.IMMUTABLE_NEIGHBORS, runtime.TEXT_UNIT)
            },
            protected_info,
        )

    def test_activation_failure_after_candidate_ownership_restores_exact_prestate(self) -> None:
        self._set_exact_runtime_mask_prestate()
        owner = self._runtime_mask_owner()
        original_lifecycle = self.host.lifecycle

        def activate_then_fail(action: str, *, pause_text: bool) -> None:
            original_lifecycle(action, pause_text=pause_text)
            if action == "activate":
                raise deployment.DeploymentError("candidate activation failed")

        with mock.patch.object(self.host, "lifecycle", side_effect=activate_then_fail):
            with self.assertRaisesRegex(deployment.DeploymentError, "candidate activation failed"):
                owner.deploy(self.bundle, self.source_snapshot, pause_text=False)

        self.assertFalse(self.state.exists())
        self._assert_exact_runtime_mask_prestate_restored()

    def test_persistent_and_foreign_runtime_masks_fail_without_unmasking(self) -> None:
        for mask in ("masked", "masked-runtime"):
            with self.subTest(mask=mask):
                self.host.info[runtime.BACKEND_UNIT]["UnitFileState"] = mask
                with self.assertRaisesRegex(deployment.DeploymentError, "mask"):
                    self.owner.prepare(self.bundle)
                self.assertEqual(self.host.commands, [])
                self.assertFalse(self.state.exists())
                self.host.info[runtime.BACKEND_UNIT]["UnitFileState"] = "disabled"

    def test_text_mask_is_rejected_before_the_owner_creates_candidate_masks(self) -> None:
        self.host.protected_info[runtime.TEXT_UNIT]["UnitFileState"] = "masked-runtime"
        with self.assertRaisesRegex(deployment.DeploymentError, "text service is masked"):
            self.owner.prepare(self.bundle)
        self.assertEqual(self.host.commands, [])
        self.assertFalse(self.state.exists())

    def test_owned_runtime_masks_are_exactly_removed_during_partial_recovery(self) -> None:
        self.owner.prepare(self.bundle)
        self.owner.rollback()

        self.assertFalse(self.state.exists())
        self.assertEqual(self.host._masked, set())
        self.assertEqual(
            self.host.commands,
            [
                ("mask", runtime.BACKEND_UNIT),
                ("mask", runtime.ADAPTER_UNIT),
                ("unmask", runtime.BACKEND_UNIT),
                ("unmask", runtime.ADAPTER_UNIT),
            ],
        )

    def test_rollback_refuses_a_foreign_mask_after_owner_unmasked_its_own(self) -> None:
        self.owner.prepare(self.bundle)
        self.owner.install(self.source_snapshot)
        unmask_count = self.host.commands.count(("unmask", runtime.BACKEND_UNIT))
        self.host.info[runtime.BACKEND_UNIT]["UnitFileState"] = "masked-runtime"

        with self.assertRaisesRegex(deployment.DeploymentError, "mask prestate"):
            self.owner.rollback()

        self.assertEqual(
            self.host.commands.count(("unmask", runtime.BACKEND_UNIT)), unmask_count
        )
        self.assertEqual(self.owner._read()["phase"], "rollback-failed")

    def test_refuses_unit_bytes_mode_fragment_and_dropin_drift_before_mask(self) -> None:
        backend_target = deployment._unit_target(runtime.BACKEND_UNIT)
        backend_target.parent.mkdir(parents=True)
        backend_target.write_text("foreign")
        backend_target.chmod(0o644)
        self.host.info[runtime.BACKEND_UNIT]["FragmentPath"] = str(backend_target)
        with self.assertRaisesRegex(deployment.DeploymentError, "bytes"):
            self.owner.prepare(self.bundle)
        self.assertEqual(self.host.commands, [])

        matching_payload = (self.bundle / "payload" / "systemd" / "vllm-querit-4b-canary-backend.service").read_bytes()
        backend_target.write_bytes(matching_payload)
        backend_target.chmod(0o755)
        with self.assertRaisesRegex(deployment.DeploymentError, "bytes"):
            self.owner.prepare(self.bundle)
        self.assertEqual(self.host.commands, [])

        backend_target.unlink()
        self.host.info[runtime.BACKEND_UNIT]["FragmentPath"] = "/foreign/unit.service"
        with self.assertRaisesRegex(deployment.DeploymentError, "path"):
            self.owner.prepare(self.bundle)
        self.assertEqual(self.host.commands, [])

    def test_bundle_payload_hash_mismatch_is_rejected_before_owner_mutation(self) -> None:
        verified_bundle = self.root / str(self.manifest["bundle_sha256"])
        shutil.copytree(self.bundle, verified_bundle)
        payload = verified_bundle / "payload" / "scripts" / "module.py"
        payload.write_text("tampered")
        with self.assertRaisesRegex(deployment.DeploymentError, "payload drifted"):
            VERIFY_BUNDLE_CONTENTS(verified_bundle)
        self.assertEqual(self.host.commands, [])

        self.host.info[runtime.BACKEND_UNIT]["FragmentPath"] = ""
        self.host.info[runtime.BACKEND_UNIT]["DropInPaths"] = "/foreign/dropin.conf"
        with self.assertRaisesRegex(deployment.DeploymentError, "drop-ins"):
            self.owner.prepare(self.bundle)
        self.assertEqual(self.host.commands, [])

    def test_listener_and_container_conflicts_block_before_mutation(self) -> None:
        self.host.listener_lines = ("LISTEN 0 1 100.105.4.92:18014",)
        with self.assertRaisesRegex(deployment.DeploymentError, "listener"):
            self.owner.prepare(self.bundle)
        self.assertEqual(self.host.commands, [])

        self.host.listener_lines = ()
        self.host.containers[runtime.CONTAINER_NAMES[runtime.BACKEND_UNIT]] = {
            "id": "a" * 64,
            "image": "candidate",
            "config_image": "candidate",
            "pid": "0",
            "running": "false",
        }
        with self.assertRaisesRegex(deployment.DeploymentError, "container"):
            self.owner.prepare(self.bundle)
        self.assertEqual(self.host.commands, [])

    def test_unsealed_artifact_prestate_requires_explicit_opt_in_without_mutation(self) -> None:
        self.artifact.mkdir()
        original = self.artifact / "weights.bin"
        original.write_bytes(b"unsealed weights")
        before = original.read_bytes()
        deployment.artifact.manifest_sha256.side_effect = deployment.artifact.ArtifactError(
            "manifest is absent"
        )

        with self.assertRaises(deployment.artifact.ArtifactError):
            self.owner.plan(self.bundle)

        accepted = deployment.Deployment(
            self.host,
            self.state,
            source_root=self.root,
            artifact_path=self.artifact,
            accept_unsealed_artifact_prestate=True,
        )
        plan = accepted.plan(self.bundle)

        self.assertTrue(plan["artifact_prestate"]["accepted_unsealed_prestate"])
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(self.host.commands, [])
        parsed = deployment._parse_args(
            ["plan", "--accept-unsealed-artifact-prestate"]
        )
        self.assertTrue(parsed.accept_unsealed_artifact_prestate)
        with self.assertRaisesRegex(deployment.DeploymentError, "valid only"):
            deployment.main(["install", "--accept-unsealed-artifact-prestate"])

    def test_unsealed_prestate_rejects_symlink_and_special_roots_before_effects(self) -> None:
        target = self.root / "symlink-target"
        target.mkdir()
        self.artifact.symlink_to(target, target_is_directory=True)
        owner = self._unsealed_artifact_owner()
        with self.assertRaisesRegex(deployment.DeploymentError, "real directory"):
            owner.plan(self.bundle)
        self.assertEqual(self.host.commands, [])

        self.artifact.unlink()
        os.mkfifo(self.artifact)
        with self.assertRaisesRegex(deployment.DeploymentError, "real directory"):
            owner.plan(self.bundle)
        self.assertEqual(self.host.commands, [])

        self.artifact.unlink()
        self.artifact.mkdir()
        payload = self.root / "payload"
        payload.write_bytes(b"payload")
        (self.artifact / "linked").symlink_to(payload)
        with self.assertRaisesRegex(deployment.DeploymentError, "prestate file is unsafe"):
            owner.plan(self.bundle)
        (self.artifact / "linked").unlink()
        os.mkfifo(self.artifact / "pipe")
        with self.assertRaisesRegex(deployment.DeploymentError, "prestate file is unsafe"):
            owner.plan(self.bundle)
        self.assertEqual(self.host.commands, [])

    def test_unsealed_prestate_drift_rejects_before_rename_or_publication(self) -> None:
        original = self._write_unsealed_artifact()
        owner = self._unsealed_artifact_owner()
        owner.prepare(self.bundle)
        original.write_bytes(b"drifted")

        with self.assertRaisesRegex(deployment.DeploymentError, "prestate drifted"):
            owner.install(self.source_snapshot)

        self.assertTrue(self.artifact.exists())
        self.assertFalse(any(self.artifact.parent.glob(".gb10-querit-previous-*")))
        owner.rollback()

    def test_failure_after_old_rename_restores_unsealed_prestate_automatically(self) -> None:
        original = self._write_unsealed_artifact()
        owner = self._unsealed_artifact_owner()
        owner.prepare(self.bundle)
        real_replace = deployment.os.replace

        def fail_after_old_rename(source: str | Path, destination: str | Path) -> None:
            real_replace(source, destination)
            if Path(source) == self.artifact:
                raise deployment.DeploymentError("injected after old rename")

        with mock.patch.object(deployment.os, "replace", side_effect=fail_after_old_rename):
            with self.assertRaisesRegex(deployment.DeploymentError, "injected after old rename"):
                owner.install(self.source_snapshot)

        self.assertEqual(original.read_bytes(), b"unsealed weights")
        self.assertFalse(self.state.exists())

    def test_failure_after_fresh_publication_restores_unsealed_prestate_automatically(self) -> None:
        original = self._write_unsealed_artifact()
        owner = self._unsealed_artifact_owner()
        owner.prepare(self.bundle)

        with mock.patch.object(
            owner,
            "_install_files",
            side_effect=deployment.DeploymentError("injected after publication"),
        ):
            with self.assertRaisesRegex(deployment.DeploymentError, "injected after publication"):
                owner.install(self.source_snapshot)

        self.assertEqual(original.read_bytes(), b"unsealed weights")
        self.assertFalse(self.state.exists())

    def test_active_unsealed_prestate_retains_recovery_state_then_deactivate_restores_it(self) -> None:
        original = self._write_unsealed_artifact()
        owner = self._unsealed_artifact_owner()
        owner.prepare(self.bundle)
        owner.install(self.source_snapshot)
        owner.activate(pause_text=False)

        record = owner._read()
        publication = record["artifact_publication"]
        self.assertEqual(publication["state"], "published")
        backup = Path(publication["previous_backup"])
        self.assertTrue(backup.exists())
        self.assertEqual((backup / "nested" / "weights.bin").read_bytes(), b"unsealed weights")

        owner.rollback()
        self.assertEqual(original.read_bytes(), b"unsealed weights")
        self.assertFalse(self.state.exists())

    def test_unsealed_rollback_replay_restores_exact_prestate_after_later_failure(self) -> None:
        original = self._write_unsealed_artifact()
        owner = self._unsealed_artifact_owner()
        owner.prepare(self.bundle)
        owner.install(self.source_snapshot)
        owner.activate(pause_text=False)

        with mock.patch.object(
            owner,
            "_restore_files",
            side_effect=deployment.DeploymentError("injected later rollback failure"),
        ):
            with self.assertRaisesRegex(deployment.DeploymentError, "rollback was incomplete"):
                owner.rollback()

        self.assertTrue(owner._read()["artifact_restored"])
        self.assertEqual(original.read_bytes(), b"unsealed weights")
        owner.rollback()
        self.assertEqual(original.read_bytes(), b"unsealed weights")
        self.assertFalse(self.state.exists())

    def test_unsealed_backup_collision_and_ambiguous_recovery_fail_closed(self) -> None:
        original = self._write_unsealed_artifact()
        owner = self._unsealed_artifact_owner()
        owner.prepare(self.bundle)

        with mock.patch.object(
            deployment,
            "_reserve_artifact_rollback_directory",
            create=True,
            side_effect=deployment.DeploymentError("artifact rollback backup collision"),
        ):
            with self.assertRaisesRegex(deployment.DeploymentError, "backup collision"):
                owner.install(self.source_snapshot)
        self.assertEqual(original.read_bytes(), b"unsealed weights")

        backup = self.artifact.parent / ".gb10-querit-previous-ambiguous"
        backup.mkdir()
        (backup / "foreign").write_text("do not overwrite")
        record = owner._read()
        record["artifact_publication"] = {
            "new_manifest_sha256": "m" * 64,
            "previous_backup": str(backup),
            "state": "rename-intent",
        }
        owner._write(record)
        with self.assertRaisesRegex(deployment.DeploymentError, "ambiguous"):
            owner.rollback()
        self.assertEqual(original.read_bytes(), b"unsealed weights")
        self.assertEqual((backup / "foreign").read_text(), "do not overwrite")

    def test_absent_and_sealed_artifact_prestate_contracts_remain_distinct(self) -> None:
        absent = self.owner.plan(self.bundle)["artifact_prestate"]
        self.assertEqual(
            absent,
            {"accept_unsealed_artifact_prestate": False, "exists": False},
        )

        self.artifact.mkdir()
        sealed = self.owner.plan(self.bundle)["artifact_prestate"]
        self.assertEqual(sealed["manifest_sha256"], "m" * 64)
        self.assertFalse(sealed["accept_unsealed_artifact_prestate"])

    def test_atomic_artifact_publication_restores_previous_tree(self) -> None:
        self.artifact.mkdir()
        (self.artifact / "old").write_text("previous")
        self.owner.prepare(self.bundle)
        self.owner.install(self.source_snapshot)
        self.assertTrue((self.artifact / "converted.marker").exists())
        self.owner.rollback()
        self.assertEqual((self.artifact / "old").read_text(), "previous")
        self.assertFalse((self.artifact / "converted.marker").exists())

    def test_install_rejects_source_target_drift_before_any_install(self) -> None:
        self.owner.prepare(self.bundle)
        target = Path(str(self.targets[0]["target"]))
        target.parent.mkdir(parents=True)
        target.write_text("drift")
        with self.assertRaisesRegex(deployment.DeploymentError, "drifted"):
            self.owner.install(self.source_snapshot)
        self.assertNotIn(("daemon-reload", ""), self.host.commands)
        with self.assertRaisesRegex(deployment.DeploymentError, "rollback was incomplete"):
            self.owner.rollback()
        retained = self.owner._read()
        self.assertEqual(retained["phase"], "rollback-failed")

    def test_deploy_recovers_a_nonactive_partial_receipt_before_new_work(self) -> None:
        self.owner.prepare(self.bundle)
        self.owner.deploy(self.bundle, self.source_snapshot, pause_text=False)

        self.assertEqual(self.host.commands.count(("mask", runtime.BACKEND_UNIT)), 2)
        self.assertIn(("lifecycle-activate", "False"), self.host.commands)
        self.owner.rollback()

    def test_rollback_retries_after_lifecycle_deactivation_then_later_failure(self) -> None:
        self.owner.prepare(self.bundle)
        self.owner.install(self.source_snapshot)
        self.owner.activate(pause_text=False)

        with mock.patch.object(
            self.owner, "_restore_files", side_effect=deployment.DeploymentError("disk")
        ):
            with self.assertRaisesRegex(deployment.DeploymentError, "rollback was incomplete"):
                self.owner.rollback()

        retained = self.owner._read()
        self.assertEqual(retained["phase"], "rollback-failed")
        self.assertTrue(retained["lifecycle_deactivated"])
        self.assertFalse(self.host.lifecycle_state_exists())

        self.owner.rollback()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.host.commands.count(("lifecycle-deactivate", "False")), 1)

    def test_rollback_retries_after_restoring_a_previous_artifact(self) -> None:
        self.artifact.mkdir()
        (self.artifact / "old").write_text("previous")
        self.owner.prepare(self.bundle)
        self.owner.install(self.source_snapshot)
        self.owner.activate(pause_text=False)

        with mock.patch.object(
            self.owner, "_restore_files", side_effect=deployment.DeploymentError("disk")
        ):
            with self.assertRaisesRegex(deployment.DeploymentError, "rollback was incomplete"):
                self.owner.rollback()

        retained = self.owner._read()
        self.assertTrue(retained["artifact_restored"])
        self.assertEqual((self.artifact / "old").read_text(), "previous")

        self.owner.rollback()
        self.assertFalse(self.state.exists())
        self.assertEqual((self.artifact / "old").read_text(), "previous")


if __name__ == "__main__":
    unittest.main()
