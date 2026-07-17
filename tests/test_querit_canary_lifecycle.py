from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

lifecycle = importlib.import_module("querit_canary_lifecycle")
artifact = importlib.import_module("querit_vllm_artifact")


class FakeHost:
    def __init__(
        self, *, memory: list[int], warm_error: Exception | None = None
    ) -> None:
        self.memory = iter(memory)
        self.warm_error = warm_error
        self.commands: list[tuple[str, str]] = []
        self.verify_calls = 0
        self.sequence = 1
        self.states = {
            lifecycle.TEXT_UNIT: lifecycle.ServiceState(True, "text-before"),
            lifecycle.BACKEND_UNIT: lifecycle.ServiceState(False, ""),
            lifecycle.ADAPTER_UNIT: lifecycle.ServiceState(False, ""),
            lifecycle.EMBEDDING_UNIT: lifecycle.ServiceState(True, "embedding-before"),
            lifecycle.PRODUCTION_RERANKER_UNIT: lifecycle.ServiceState(
                True, "rr-before"
            ),
            lifecycle.LEGACY_RERANKER_UNIT: lifecycle.ServiceState(False, ""),
            lifecycle.GUARD_UNIT: lifecycle.ServiceState(True, "guard-before"),
        }

    def verify_artifact(self) -> str:
        self.verify_calls += 1
        return "a" * 64

    def memory_available_gib(self) -> int:
        return next(self.memory)

    def service_state(self, unit: str) -> lifecycle.ServiceState:
        return self.states[unit]

    def start(self, unit: str) -> None:
        self.commands.append(("start", unit))
        if unit in lifecycle.IMMUTABLE_NEIGHBORS:
            raise AssertionError(f"attempted to start protected neighbor {unit}")
        self.sequence += 1
        self.states[unit] = lifecycle.ServiceState(True, f"invocation-{self.sequence}")

    def stop(self, unit: str) -> None:
        self.commands.append(("stop", unit))
        if unit in lifecycle.IMMUTABLE_NEIGHBORS:
            raise AssertionError(f"attempted to stop protected neighbor {unit}")
        self.states[unit] = lifecycle.ServiceState(False, "")

    def warm(self) -> None:
        self.commands.append(("warm", lifecycle.ADAPTER_UNIT))
        if self.warm_error is not None:
            raise self.warm_error


class ArtifactManifestTests(unittest.TestCase):
    def test_manifest_binds_converted_artifact_and_rejects_mutation_or_symlink(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for name, payload in (
                ("config.json", b"{}\n"),
                ("model.safetensors.index.json", b"{}\n"),
                ("model-00001-of-00002.safetensors", b"weights-1"),
                ("model-00002-of-00002.safetensors", b"weights-2"),
                ("querit-rerank.jinja", b"template\n"),
            ):
                (root / name).write_bytes(payload)
            manifest = artifact.write_manifest(root)
            verified = artifact.verify_manifest(root)
            self.assertEqual(verified, manifest)
            self.assertEqual(
                verified["source_revision"],
                "7b796de30ad8dc772d6c46c75659c1341283a665",
            )
            self.assertEqual(verified["transform"], "querit-tanh-scalar-head-v1")

            target = root / "model-00002-of-00002.safetensors"
            target.write_bytes(b"mutated")
            with self.assertRaises(artifact.ArtifactError):
                artifact.verify_manifest(root)
            target.unlink()
            target.symlink_to(root / "model-00001-of-00002.safetensors")
            with self.assertRaises(artifact.ArtifactError):
                artifact.verify_manifest(root)


class CanaryLifecycleTests(unittest.TestCase):
    def test_unit_preflight_reads_parent_transaction_without_taking_its_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            state = root / "state.json"
            fake_host = object()
            with (
                mock.patch.object(
                    lifecycle,
                    "_lock",
                    side_effect=AssertionError("preflight tried to take parent lock"),
                ),
                mock.patch.object(lifecycle, "SystemHost", return_value=fake_host),
                mock.patch.object(lifecycle, "preflight") as checked,
            ):
                result = lifecycle.main(
                    [
                        "preflight",
                        "--state",
                        str(state),
                        "--model-root",
                        str(root),
                    ]
                )
            self.assertEqual(result, 0)
            checked.assert_called_once_with(fake_host, state)

    def test_low_headroom_pauses_text_then_deactivation_restores_exact_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state = Path(raw_tmp) / "state.json"
            host = FakeHost(memory=[10, 24])
            lifecycle.activate(host, state)

            self.assertEqual(host.verify_calls, 1)
            self.assertEqual(
                host.commands,
                [
                    ("stop", lifecycle.TEXT_UNIT),
                    ("start", lifecycle.BACKEND_UNIT),
                    ("start", lifecycle.ADAPTER_UNIT),
                    ("warm", lifecycle.ADAPTER_UNIT),
                ],
            )
            active = json.loads(state.read_text())
            self.assertEqual(active["phase"], "active")
            self.assertTrue(active["text_paused"])
            self.assertEqual(active["artifact_manifest_sha256"], "a" * 64)

            lifecycle.deactivate(host, state)
            self.assertEqual(
                host.commands[-3:],
                [
                    ("stop", lifecycle.ADAPTER_UNIT),
                    ("stop", lifecycle.BACKEND_UNIT),
                    ("start", lifecycle.TEXT_UNIT),
                ],
            )
            self.assertTrue(host.states[lifecycle.TEXT_UNIT].active)
            self.assertFalse(state.exists())
            self.assertFalse(
                any(command == "restart" for command, _unit in host.commands)
            )

    def test_high_headroom_never_stops_text(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            host = FakeHost(memory=[24])
            lifecycle.activate(host, Path(raw_tmp) / "state.json")
            self.assertNotIn(("stop", lifecycle.TEXT_UNIT), host.commands)
            self.assertTrue(host.states[lifecycle.TEXT_UNIT].active)

    def test_readiness_failure_and_signal_both_restore_text_and_stop_canary(
        self,
    ) -> None:
        failures = (
            RuntimeError("readiness failed"),
            lifecycle.LifecycleCancelled("SIGTERM"),
        )
        for failure in failures:
            with (
                self.subTest(failure=type(failure).__name__),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                host = FakeHost(memory=[10, 24], warm_error=failure)
                state = Path(raw_tmp) / "state.json"
                with self.assertRaises(type(failure)):
                    lifecycle.activate(host, state)
                self.assertTrue(host.states[lifecycle.TEXT_UNIT].active)
                self.assertFalse(host.states[lifecycle.ADAPTER_UNIT].active)
                self.assertFalse(host.states[lifecycle.BACKEND_UNIT].active)
                self.assertFalse(state.exists())
                self.assertEqual(
                    host.commands[-3:],
                    [
                        ("stop", lifecycle.ADAPTER_UNIT),
                        ("stop", lifecycle.BACKEND_UNIT),
                        ("start", lifecycle.TEXT_UNIT),
                    ],
                )

    def test_insufficient_headroom_fails_before_canary_start_and_restores_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            host = FakeHost(memory=[10, 18])
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.activate(host, Path(raw_tmp) / "state.json")
            self.assertEqual(
                host.commands,
                [("stop", lifecycle.TEXT_UNIT), ("start", lifecycle.TEXT_UNIT)],
            )
            self.assertTrue(host.states[lifecycle.TEXT_UNIT].active)


if __name__ == "__main__":
    unittest.main()
