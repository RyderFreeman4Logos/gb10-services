from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

lifecycle = importlib.import_module("querit_canary_lifecycle")
runtime = importlib.import_module("querit_canary_runtime")
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
        self.stop_failures: dict[str, BaseException] = {}

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
        failure = self.stop_failures.pop(unit, None)
        if failure is not None:
            raise failure
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
                (
                    "config.json",
                    json.dumps(
                        {
                            "architectures": ["Qwen3ForSequenceClassification"],
                            "head_dtype": "model",
                            "hidden_size": 2560,
                            "max_position_embeddings": 40960,
                            "num_labels": 1,
                            "sbert_ce_default_activation_function": (
                                "torch.nn.modules.activation.Tanh"
                            ),
                        }
                    ).encode(),
                ),
                ("model.safetensors.index.json", b"{}\n"),
                ("model-00001-of-00002.safetensors", b"weights-1"),
                ("model-00002-of-00002.safetensors", b"weights-2"),
                (
                    "querit-rerank.jinja",
                    (ROOT / "config" / "querit" / "querit-rerank.jinja").read_bytes(),
                ),
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

    def test_manifest_semantically_attests_32k_config_and_exact_template(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "model.safetensors.index.json").write_text("{}\n")
            (root / "model.safetensors").write_bytes(b"weights")
            template = ROOT / "config" / "querit" / "querit-rerank.jinja"
            (root / "querit-rerank.jinja").write_bytes(template.read_bytes())
            valid_config = {
                "architectures": ["Qwen3ForSequenceClassification"],
                "head_dtype": "model",
                "hidden_size": 2560,
                "max_position_embeddings": 32768,
                "num_labels": 1,
                "sbert_ce_default_activation_function": (
                    "torch.nn.modules.activation.Tanh"
                ),
            }
            (root / "config.json").write_text(json.dumps(valid_config))
            artifact.write_manifest(root)

            for mutation in (
                lambda row: row.update({"max_position_embeddings": 32767}),
                lambda row: row.update({"num_labels": 2}),
                lambda row: row.update({"problem_type": "regression"}),
            ):
                changed = dict(valid_config)
                mutation(changed)
                (root / "config.json").write_text(json.dumps(changed))
                with self.subTest(changed=changed), self.assertRaises(
                    artifact.ArtifactError
                ):
                    artifact.write_manifest(root)

            (root / "config.json").write_text(json.dumps(valid_config))
            (root / "querit-rerank.jinja").write_bytes(template.read_bytes() + b"\n")
            with self.assertRaises(artifact.ArtifactError):
                artifact.write_manifest(root)


class CanaryLifecycleTests(unittest.TestCase):
    def test_service_snapshot_binds_unit_container_and_all_observed_pids(self) -> None:
        state = lifecycle.ServiceState(
            active=True,
            invocation_id="invocation",
            main_pid=101,
            control_group="/user.slice/example.service",
            unit_pids=(101, 102),
            container_id="a" * 64,
            container_pid=201,
            container_cgroup="/user.slice/docker.scope",
            container_pids=(201, 202),
        )
        self.assertEqual(
            state.record(),
            {
                "active": True,
                "container_cgroup": "/user.slice/docker.scope",
                "container_id": "a" * 64,
                "container_pid": 201,
                "container_pids": [201, 202],
                "control_group": "/user.slice/example.service",
                "invocation_id": "invocation",
                "main_pid": 101,
                "unit_pids": [101, 102],
            },
        )

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

    def test_deactivation_failure_and_signal_restore_original_instead_of_canary(
        self,
    ) -> None:
        for failure in (RuntimeError("stop failed"), lifecycle.LifecycleCancelled("SIGTERM")):
            with (
                self.subTest(failure=type(failure).__name__),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                state = Path(raw_tmp) / "state.json"
                host = FakeHost(memory=[10, 24])
                lifecycle.activate(host, state)
                host.stop_failures[lifecycle.ADAPTER_UNIT] = failure
                with self.assertRaises(type(failure)):
                    lifecycle.deactivate(host, state)
                self.assertFalse(state.exists())
                self.assertTrue(host.states[lifecycle.TEXT_UNIT].active)
                self.assertFalse(host.states[lifecycle.ADAPTER_UNIT].active)
                self.assertFalse(host.states[lifecycle.BACKEND_UNIT].active)
                self.assertFalse(
                    any(
                        command == "start" and unit in {
                            lifecycle.ADAPTER_UNIT,
                            lifecycle.BACKEND_UNIT,
                        }
                        for command, unit in host.commands[-4:]
                    )
                )

    def test_incomplete_restoration_keeps_a_retryable_failure_record(self) -> None:
        class StuckAdapterHost(FakeHost):
            stuck = False

            def stop(self, unit: str) -> None:
                if self.stuck and unit == lifecycle.ADAPTER_UNIT:
                    self.commands.append(("stop", unit))
                    raise RuntimeError("adapter remains live")
                super().stop(unit)

        with tempfile.TemporaryDirectory() as raw_tmp:
            state = Path(raw_tmp) / "state.json"
            host = StuckAdapterHost(memory=[10, 24])
            lifecycle.activate(host, state)
            host.stuck = True

            with self.assertRaisesRegex(
                lifecycle.LifecycleError, "could not restore its original state"
            ):
                lifecycle.deactivate(host, state)

            retained = json.loads(state.read_text())
            self.assertEqual(retained["phase"], "rollback-failed")
            self.assertTrue(host.states[lifecycle.TEXT_UNIT].active)
            self.assertFalse(host.states[lifecycle.BACKEND_UNIT].active)
            self.assertTrue(host.states[lifecycle.ADAPTER_UNIT].active)

    def test_stale_deactivation_is_recovered_to_original_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state = Path(raw_tmp) / "state.json"
            host = FakeHost(memory=[10, 24])
            lifecycle.activate(host, state)
            record = json.loads(state.read_text())
            record["phase"] = "deactivating"
            state.write_text(json.dumps(record))
            host.stop(lifecycle.ADAPTER_UNIT)

            lifecycle.deactivate(host, state)

            self.assertFalse(state.exists())
            self.assertTrue(host.states[lifecycle.TEXT_UNIT].active)
            self.assertFalse(host.states[lifecycle.BACKEND_UNIT].active)

    def test_warmup_exercises_public_wire_full_context_and_batching(self) -> None:
        class Response:
            def __init__(self, body: bytes) -> None:
                self.status = 200
                self.body = body

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, limit: int) -> bytes:
                return self.body[:limit]

        calls: list[object] = []

        def native_response(count: int, tokens: int) -> bytes:
            return json.dumps(
                {
                    "created": 1,
                    "data": [
                        {"index": index, "object": "score", "score": 0.0}
                        for index in range(count)
                    ],
                    "id": "score-warmup",
                    "model": "Querit/Querit-4B",
                    "object": "list",
                    "usage": {
                        "completion_tokens": 0,
                        "prompt_tokens": tokens,
                        "prompt_tokens_details": None,
                        "total_tokens": tokens,
                    },
                }
            ).encode()

        responses = iter(
            (
                Response(b'{"input_tokens":8,"request_id":"public","scores":[0.5]}'),
                Response(native_response(1, 32768)),
                Response(native_response(16, 4096)),
            )
        )

        def open_request(request: object, timeout: int) -> Response:
            self.assertEqual(timeout, 300)
            calls.append(request)
            return next(responses)

        class WarmHost(lifecycle.SystemHost):
            def service_state(self, unit: str) -> lifecycle.ServiceState:
                return lifecycle.ServiceState(True, f"{unit}-stable")

            def memory_available_gib(self) -> int:
                return 4

            def cgroup_memory_events(self, unit: str) -> dict[str, int]:
                return {"oom": 0, "oom_kill": 0}

        with mock.patch.object(runtime.urllib.request, "urlopen", open_request):
            WarmHost().warm()

        self.assertEqual(len(calls), 3)
        public, peak, batch = calls
        self.assertEqual(public.full_url, runtime.PUBLIC_URL)
        self.assertEqual(urllib.parse.urlsplit(peak.full_url).path, "/score")
        peak_body = json.loads(peak.data)
        self.assertEqual(peak_body["truncate_prompt_tokens"], -1)
        self.assertGreater(len(peak_body["documents"][0]), 32768)
        batch_body = json.loads(batch.data)
        self.assertEqual(len(batch_body["queries"]), 16)
        self.assertEqual(len(batch_body["documents"]), 16)

    def test_warmup_rejects_oom_and_unit_identity_drift(self) -> None:
        def native_response(count: int, tokens: int) -> bytes:
            return json.dumps(
                {
                    "created": 1,
                    "data": [
                        {"index": index, "object": "score", "score": 0.0}
                        for index in range(count)
                    ],
                    "id": "score-warmup",
                    "model": "Querit/Querit-4B",
                    "object": "list",
                    "usage": {
                        "completion_tokens": 0,
                        "prompt_tokens": tokens,
                        "prompt_tokens_details": None,
                        "total_tokens": tokens,
                    },
                }
            ).encode()

        responses = (
            b'{"input_tokens":8,"request_id":"public","scores":[0.5]}',
            native_response(1, 32768),
            native_response(16, 4096),
        )
        stable_backend = lifecycle.ServiceState(True, "backend-stable")
        stable_adapter = lifecycle.ServiceState(True, "adapter-stable")
        host = runtime.SystemHost()
        with (
            mock.patch.object(
                host,
                "service_state",
                side_effect=(stable_backend, stable_adapter),
            ),
            mock.patch.object(host, "_post", side_effect=responses),
            mock.patch.object(
                host,
                "cgroup_memory_events",
                side_effect=(
                    {"oom": 0, "oom_kill": 0},
                    {"oom": 1, "oom_kill": 0},
                ),
            ),
        ):
            with self.assertRaisesRegex(lifecycle.LifecycleError, "memory pressure"):
                host.warm()

        drifted_backend = lifecycle.ServiceState(True, "backend-replaced")
        host = runtime.SystemHost()
        with (
            mock.patch.object(
                host,
                "service_state",
                side_effect=(stable_backend, stable_adapter, drifted_backend),
            ),
            mock.patch.object(host, "_post", side_effect=responses),
            mock.patch.object(
                host,
                "cgroup_memory_events",
                side_effect=(
                    {"oom": 0, "oom_kill": 0},
                    {"oom": 0, "oom_kill": 0},
                ),
            ),
            mock.patch.object(host, "memory_available_gib", return_value=4),
        ):
            with self.assertRaisesRegex(lifecycle.LifecycleError, "changed during warmup"):
                host.warm()


if __name__ == "__main__":
    unittest.main()
