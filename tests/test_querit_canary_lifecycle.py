from __future__ import annotations

import importlib
import json
import struct
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


def _safetensors_stub(*names: str) -> bytes:
    header = {
        name: {
            "data_offsets": [index, index + 1],
            "dtype": "BF16",
            "shape": [1],
        }
        for index, name in enumerate(names)
    }
    encoded = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack("<Q", len(encoded)) + encoded + b"\0" * len(names)


def _write_converted_artifact(root: Path) -> None:
    config = {
        "architectures": ["Qwen3ForSequenceClassification"],
        "head_dtype": "model",
        "hidden_size": 2560,
        "max_position_embeddings": 32768,
        "num_labels": 1,
        "sbert_ce_default_activation_function": "torch.nn.modules.activation.Tanh",
    }
    index = {
        "metadata": {"total_size": 8_043_558_914},
        "weight_map": {
            "model.weight": "model-00001-of-00002.safetensors",
            "score.bias": "model-00002-of-00002.safetensors",
            "score.weight": "model-00002-of-00002.safetensors",
        },
    }
    (root / "config.json").write_text(json.dumps(config))
    (root / "model.safetensors.index.json").write_text(json.dumps(index))
    (root / "model-00001-of-00002.safetensors").write_bytes(
        _safetensors_stub("model.weight")
    )
    (root / "model-00002-of-00002.safetensors").write_bytes(
        _safetensors_stub("score.bias", "score.weight")
    )
    template = ROOT / "config" / "querit" / "querit-rerank.jinja"
    (root / "querit-rerank.jinja").write_bytes(template.read_bytes())


class FakeHost:
    def __init__(
        self,
        *,
        memory: list[int],
        warm_error: Exception | None = None,
        no_swap_fail_at: int = 0,
    ) -> None:
        self.memory = iter(memory)
        self.warm_error = warm_error
        self.commands: list[tuple[str, str]] = []
        self.verify_calls = 0
        self.no_swap_calls: list[tuple[str, str | None]] = []
        self.no_swap_fail_at = no_swap_fail_at
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
        self.unit_file_states = {lifecycle.TEXT_UNIT: "disabled"}
        self.cgroup_version = "2"
        self.cgroup_preflights = 0

    def require_cgroup_v2(self) -> None:
        self.cgroup_preflights += 1
        if self.cgroup_version != "2":
            raise lifecycle.LifecycleError("Docker cgroup version is not exactly 2")

    def verify_no_swap(self, unit: str, container: str | None = None) -> None:
        self.no_swap_calls.append((unit, container))
        if self.no_swap_fail_at == len(self.no_swap_calls):
            raise lifecycle.LifecycleError("no-swap verification failed")

    def verify_artifact(self) -> str:
        self.verify_calls += 1
        return "a" * 64

    def memory_available_gib(self) -> int:
        return next(self.memory)

    def service_state(self, unit: str) -> lifecycle.ServiceState:
        return self.states[unit]

    def unit_file_state(self, unit: str) -> str:
        return self.unit_file_states.get(unit, "disabled")

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
            _write_converted_artifact(root)
            manifest = artifact.write_manifest(root)
            verified = artifact.verify_manifest(root)
            self.assertEqual(verified, manifest)
            self.assertEqual(
                verified["source_revision"],
                "7b796de30ad8dc772d6c46c75659c1341283a665",
            )
            self.assertEqual(verified["transform"], "querit-tanh-scalar-head-v1")
            self.assertEqual(
                verified["source_ledger_sha256"], artifact.SOURCE_LEDGER_SHA256
            )
            self.assertEqual(
                verified["source_tree_sha256"], artifact.SOURCE_TREE_SHA256
            )
            self.assertEqual(
                verified["total_size"],
                {
                    "delta": -5_122,
                    "output": 8_043_558_914,
                    "source": 8_043_564_036,
                },
            )

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
            _write_converted_artifact(root)
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
                with (
                    self.subTest(changed=changed),
                    self.assertRaises(artifact.ArtifactError),
                ):
                    artifact.write_manifest(root)

            (root / "config.json").write_text(json.dumps(valid_config))
            template = ROOT / "config" / "querit" / "querit-rerank.jinja"
            (root / "querit-rerank.jinja").write_bytes(template.read_bytes() + b"\n")
            with self.assertRaises(artifact.ArtifactError):
                artifact.write_manifest(root)

    def test_manifest_rejects_total_size_index_and_tensor_consumption_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _write_converted_artifact(root)
            index_path = root / "model.safetensors.index.json"
            valid_index = json.loads(index_path.read_text())

            cases = {
                "wrong total size": lambda row: row["metadata"].update(
                    {"total_size": 8_043_564_036}
                ),
                "obsolete head": lambda row: row["weight_map"].update(
                    {"head.weight": "model-00002-of-00002.safetensors"}
                ),
                "missing scalar": lambda row: row["weight_map"].pop("score.bias"),
            }
            for label, mutate in cases.items():
                changed = json.loads(json.dumps(valid_index))
                mutate(changed)
                index_path.write_text(json.dumps(changed))
                with (
                    self.subTest(label=label),
                    self.assertRaises(artifact.ArtifactError),
                ):
                    artifact.write_manifest(root)

            index_path.write_text(json.dumps(valid_index))
            (root / "model-00002-of-00002.safetensors").write_bytes(
                _safetensors_stub("score.bias", "score.weight", "orphan.weight")
            )
            with self.assertRaisesRegex(
                artifact.ArtifactError, "consumed exactly once"
            ):
                artifact.write_manifest(root)

            _write_converted_artifact(root)
            (root / "unexpected.safetensors").write_bytes(
                _safetensors_stub("unexpected.weight")
            )
            with self.assertRaisesRegex(artifact.ArtifactError, "shard set"):
                artifact.write_manifest(root)

    def test_manifest_rejects_a_tampered_source_or_output_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _write_converted_artifact(root)
            artifact.write_manifest(root)
            manifest_path = root / artifact.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())

            for field, value in (
                ("source_ledger", []),
                ("source_ledger_sha256", "0" * 64),
                ("output_tree_sha256", "0" * 64),
            ):
                changed = dict(manifest)
                changed[field] = value
                manifest_path.write_text(artifact._json_bytes(changed).decode())
                with (
                    self.subTest(field=field),
                    self.assertRaises(artifact.ArtifactError),
                ):
                    artifact.verify_manifest(root)


class CanaryLifecycleTests(unittest.TestCase):
    def test_systemd_conflicts_cannot_stop_immutable_rerankers_before_exec_condition(
        self,
    ) -> None:
        class ConflictAwareHost(FakeHost):
            state_path: Path

            def start(self, unit: str) -> None:
                if unit == lifecycle.BACKEND_UNIT:
                    source = (ROOT / "systemd" / f"{unit}").read_text()
                    conflicts = next(
                        (
                            line.removeprefix("Conflicts=").split()
                            for line in source.splitlines()
                            if line.startswith("Conflicts=")
                        ),
                        [],
                    )
                    for conflict in conflicts:
                        if self.states.get(conflict, lifecycle.ServiceState(False, "")).active:
                            self.states[conflict] = lifecycle.ServiceState(False, "")
                    lifecycle.preflight(self, self.state_path)
                super().start(unit)

        with tempfile.TemporaryDirectory() as raw_tmp:
            state = Path(raw_tmp) / "state.json"
            host = ConflictAwareHost(memory=[24, 24])
            host.state_path = state
            neighbor_before = {unit: host.states[unit] for unit in lifecycle.IMMUTABLE_NEIGHBORS}
            lifecycle.activate(host, state)
            self.assertEqual(
                {unit: host.states[unit] for unit in lifecycle.IMMUTABLE_NEIGHBORS},
                neighbor_before,
            )

    def test_cgroup_v1_or_unknown_fails_before_state_or_service_mutation(self) -> None:
        for version in ("1", "unknown", ""):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as raw_tmp:
                host = FakeHost(memory=[32])
                host.cgroup_version = version
                state = Path(raw_tmp) / "state.json"
                with self.assertRaises(lifecycle.LifecycleError):
                    lifecycle.activate(host, state)
                self.assertEqual(host.commands, [])
                self.assertFalse(state.exists())
                self.assertEqual(host.cgroup_preflights, 1)

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
                host.no_swap_calls,
                [
                    (lifecycle.BACKEND_UNIT, None),
                    (lifecycle.BACKEND_UNIT, "vllm-querit-4b-canary"),
                ],
            )
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
            self.assertEqual(host.no_swap_calls[-1], (lifecycle.BACKEND_UNIT, None))

    def test_high_headroom_never_stops_text(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            host = FakeHost(memory=[24])
            lifecycle.activate(host, Path(raw_tmp) / "state.json")
            self.assertNotIn(("stop", lifecycle.TEXT_UNIT), host.commands)
            self.assertTrue(host.states[lifecycle.TEXT_UNIT].active)

    def test_explicit_pause_stops_active_text_at_high_headroom_and_restores_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state = Path(raw_tmp) / "state.json"
            host = FakeHost(memory=[24, 24])
            lifecycle.activate(host, state, pause_text=True)

            record = json.loads(state.read_text())
            self.assertTrue(record["text_pause_requested"])
            self.assertTrue(record["text_paused"])
            self.assertEqual(host.commands[0], ("stop", lifecycle.TEXT_UNIT))

            lifecycle.deactivate(host, state)
            self.assertEqual(host.commands[-1], ("start", lifecycle.TEXT_UNIT))

    def test_explicit_pause_leaves_inactive_text_unowned_and_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state = Path(raw_tmp) / "state.json"
            host = FakeHost(memory=[24])
            host.states[lifecycle.TEXT_UNIT] = lifecycle.ServiceState(False, "")

            lifecycle.activate(host, state, pause_text=True)
            record = json.loads(state.read_text())
            self.assertTrue(record["text_pause_requested"])
            self.assertFalse(record["text_paused"])
            lifecycle.deactivate(host, state)

            self.assertNotIn(("stop", lifecycle.TEXT_UNIT), host.commands)
            self.assertNotIn(("start", lifecycle.TEXT_UNIT), host.commands)

    def test_explicit_pause_restores_text_after_activation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state = Path(raw_tmp) / "state.json"
            host = FakeHost(memory=[24, 24], warm_error=RuntimeError("warm failed"))
            with self.assertRaisesRegex(RuntimeError, "warm failed"):
                lifecycle.activate(host, state, pause_text=True)

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

    def test_post_start_no_swap_failure_removes_candidate_without_neighbor_outage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state = Path(raw_tmp) / "state.json"
            host = FakeHost(memory=[24], no_swap_fail_at=2)
            neighbor_before = {
                unit: host.states[unit] for unit in lifecycle.IMMUTABLE_NEIGHBORS
            }

            with self.assertRaisesRegex(
                lifecycle.LifecycleError, "no-swap verification failed"
            ):
                lifecycle.activate(host, state)

            self.assertFalse(state.exists())
            self.assertFalse(host.states[lifecycle.BACKEND_UNIT].active)
            self.assertFalse(host.states[lifecycle.ADAPTER_UNIT].active)
            self.assertEqual(
                {unit: host.states[unit] for unit in lifecycle.IMMUTABLE_NEIGHBORS},
                neighbor_before,
            )
            self.assertEqual(
                host.no_swap_calls,
                [
                    (lifecycle.BACKEND_UNIT, None),
                    (lifecycle.BACKEND_UNIT, "vllm-querit-4b-canary"),
                    (lifecycle.BACKEND_UNIT, None),
                ],
            )

    def test_explicit_pause_rejects_persistent_or_runtime_mask_before_mutation(self) -> None:
        for mask in ("masked", "masked-runtime"):
            with self.subTest(mask=mask), tempfile.TemporaryDirectory() as raw_tmp:
                state = Path(raw_tmp) / "state.json"
                host = FakeHost(memory=[24])
                host.unit_file_states[lifecycle.TEXT_UNIT] = mask

                with self.assertRaisesRegex(lifecycle.LifecycleError, "masked"):
                    lifecycle.activate(host, state, pause_text=True)
                self.assertFalse(state.exists())
                self.assertEqual(host.commands, [])

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
        for failure in (
            RuntimeError("stop failed"),
            lifecycle.LifecycleCancelled("SIGTERM"),
        ):
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
                        command == "start"
                        and unit
                        in {
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
            with self.assertRaisesRegex(
                lifecycle.LifecycleError, "changed during warmup"
            ):
                host.warm()


if __name__ == "__main__":
    unittest.main()
