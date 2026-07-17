from __future__ import annotations

import importlib
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
runtime = importlib.import_module("querit_canary_runtime")
PRODUCTION_TARGETS = deployment.TARGETS
VERIFY_BUNDLE_CONTENTS = deployment._verify_bundle_contents


class FakeHost:
    def __init__(self, targets: tuple[dict[str, object], ...]) -> None:
        self.targets = targets
        self.commands: list[tuple[str, str]] = []
        self._masked: set[str] = set()
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
        self._admission = {
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
        self.info[unit]["UnitFileState"] = "masked-runtime"

    def runtime_unmask(self, unit: str) -> None:
        self.commands.append(("unmask", unit))
        self._masked.discard(unit)
        self.info[unit]["UnitFileState"] = "disabled"

    def daemon_reload(self) -> None:
        self.commands.append(("daemon-reload", ""))
        for unit in deployment.CANDIDATE_UNITS:
            if deployment._unit_target(unit).exists():
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
            self.states[runtime.BACKEND_UNIT] = runtime.ServiceState(True, "backend-new")
            self.states[runtime.ADAPTER_UNIT] = runtime.ServiceState(True, "adapter-new")
        elif action == "deactivate":
            self.states[runtime.BACKEND_UNIT] = runtime.ServiceState(False, "")
            self.states[runtime.ADAPTER_UNIT] = runtime.ServiceState(False, "")
            self.states[runtime.TEXT_UNIT] = runtime.ServiceState(True, "text-restored")
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

    def test_exact_mapping_covers_ready_converter_template_and_owner(self) -> None:
        mapping = {str(item["source"]): item for item in PRODUCTION_TARGETS}
        self.assertEqual(mapping["scripts/gb10_service_ready.sh"]["mode"], 0o755)
        self.assertIn("scripts/querit_checkpoint_convert.py", mapping)
        self.assertIn("config/querit/querit-rerank.jinja", mapping)
        self.assertIn("scripts/querit_canary_deployment.py", mapping)
        self.assertIn("scripts/gb10_querit_canary_deploy.py", mapping)

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
