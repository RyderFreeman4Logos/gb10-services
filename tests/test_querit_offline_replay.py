from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import querit_offline_replay as replay  # noqa: E402
import querit_replay_sandbox as sandbox  # noqa: E402
import querit_replay_schema as schema  # noqa: E402
import querit_replay_trust as trust  # noqa: E402
from querit_score_contract import (  # noqa: E402
    CLS_TOKEN_ID as TERMINAL_ANCHOR_TOKEN_ID,
    CURRENT_PROMPT_TERMINAL_CLS_V1 as CANDIDATE_CONTRACT,
    LEGACY_PHYSICAL_LAST_V1 as LEGACY_CONTRACT,
    POSTPROCESSOR_TOKEN_ID,
    render_current_prompt,
)


class BoundaryTokenizer:
    is_fast = True
    padding_side = "right"
    truncation_side = "right"
    pad_token_id = POSTPROCESSOR_TOKEN_ID
    cls_token_id = None

    @staticmethod
    def _document(text: str) -> str:
        return text.split("<Document>: ", 1)[1].split("<|im_end|>", 1)[0]

    def __call__(
        self,
        text,
        *,
        add_special_tokens=True,
        padding=False,
        truncation=False,
        max_length=None,
    ):
        if isinstance(text, list):
            rows = [
                self(
                    item,
                    add_special_tokens=add_special_tokens,
                    padding=False,
                    truncation=truncation,
                    max_length=max_length,
                )
                for item in text
            ]
            width = max(len(row["input_ids"]) for row in rows)
            return {
                "attention_mask": [
                    row["attention_mask"] + [0] * (width - len(row["input_ids"]))
                    for row in rows
                ],
                "input_ids": [
                    row["input_ids"]
                    + [POSTPROCESSOR_TOKEN_ID] * (width - len(row["input_ids"]))
                    for row in rows
                ],
            }
        document = self._document(text)
        # The first committed boundary atom contributes two tokens; ASCII "a"
        # contributes one. Other scalar values contribute three.
        body_count = 100
        for char in document:
            if char == replay.BOUNDARY_PRIMARY_ATOM:
                body_count += 2
            elif char == "a":
                body_count += 1
            else:
                body_count += 3
        ids = [17] * max(0, body_count - 1) + [POSTPROCESSOR_TOKEN_ID]
        if truncation and max_length is not None and len(ids) > max_length:
            ids = ids[: max_length - 1] + [POSTPROCESSOR_TOKEN_ID]
        return {"attention_mask": [1] * len(ids), "input_ids": ids}


class AlwaysEvenTokenizer(BoundaryTokenizer):
    def __call__(self, text, **kwargs):
        encoded = super().__call__(text, **kwargs)
        if isinstance(text, list):
            return encoded
        length = len(encoded["input_ids"])
        if length % 2:
            encoded["input_ids"].insert(-1, 17)
            encoded["attention_mask"].insert(-1, 1)
        return encoded


class FakeRuntime:
    def __init__(self) -> None:
        self.tokenizer = BoundaryTokenizer()
        self.snapshot_ledger = [
            {"path": "model.safetensors", "sha256": "c" * 64, "size": 1}
        ]
        self.source_ledger = [
            {
                "path": name,
                "sha256": f"{index + 1:064x}",
                "size": index + 1,
            }
            for index, name in enumerate(trust.SOURCE_NAMES)
        ]
        self.runtime_identity = {
            "python": "test",
            "pytorch": "test",
            "transformers": "test",
            "tokenizers": "test",
            "cuda": "none",
            "gpu": "fake",
            "sm": "none",
            "snapshot_file_count": 1,
            "source_hashes": {
                name: f"{index + 1:064x}"
                for index, name in enumerate(trust.SOURCE_NAMES)
            },
            "system_python": sandbox.attest_system_python(),
            "tokenizer_class": type(self.tokenizer).__name__,
            "tokenizer_is_fast": True,
        }
        self.classifier_load_report = {
            "error_msgs": [],
            "mismatched_keys": [],
            "missing_keys": [],
            "reinitialized_keys": [],
            "unexpected_keys": [],
        }
        self.head_attestation = {
            "bias_sha256": "a" * 64,
            "bias_shape": [2],
            "loaded_dtype": "float32",
            "normalized_dtype": "float32-le",
            "weight_sha256": "b" * 64,
            "weight_shape": [2, 2560],
        }

    def encode(self, case: replay.ReplayCase, track: str) -> dict[str, object]:
        prompt = render_current_prompt(case.query, case.document)
        encoded = self.tokenizer(
            prompt,
            add_special_tokens=True,
            padding=False,
            truncation=True,
            max_length=40959 if track == CANDIDATE_CONTRACT else 40960,
        )
        input_ids = list(encoded["input_ids"])
        if track == CANDIDATE_CONTRACT:
            input_ids.append(TERMINAL_ANCHOR_TOKEN_ID)
        pre_count = len(
            self.tokenizer(
                prompt,
                add_special_tokens=True,
                padding=False,
                truncation=False,
            )["input_ids"]
        )
        return replay.make_encoding_record(
            case=case,
            track=track,
            input_ids=input_ids,
            pre_truncation_token_count=pre_count,
        )

    def infer(
        self,
        cases: list[replay.ReplayCase],
        track: str,
        encodings: list[dict[str, object]],
    ) -> list[dict[str, Any]]:
        width = max(len(row["input_ids"]) for row in encodings)
        results = []
        for case, encoding in zip(cases, encodings, strict=True):
            content = case.query + (case.document or "")
            base = (sum(ord(char) for char in content) % 17 - 8) / 16
            z0, z1 = -base, base
            maximum = max(z0, z1)
            e0, e1 = math.exp(z0 - maximum), math.exp(z1 - maximum)
            p0, p1 = e0 / (e0 + e1), e1 / (e0 + e1)
            selected_index = (
                len(encoding["input_ids"]) - 1
                if track == CANDIDATE_CONTRACT
                else width - 1
            )
            selected_id = (
                encoding["input_ids"][selected_index]
                if selected_index < len(encoding["input_ids"])
                else POSTPROCESSOR_TOKEN_ID
            )
            results.append(
                {
                    "legacy_opaque_score": p1 - p0 if track == LEGACY_CONTRACT else None,
                    "logits": [z0, z1],
                    "native_score": p1 - p0,
                    "physical_last_id": (
                        encoding["input_ids"][-1]
                        if len(encoding["input_ids"]) == width
                        else POSTPROCESSOR_TOKEN_ID
                    ),
                    "probabilities": [p0, p1],
                    "recomputed_score": p1 - p0,
                    "selected_id": selected_id,
                    "selected_index": selected_index,
                    "width": width,
                }
            )
        return results


class DriftRuntime(FakeRuntime):
    def infer(self, cases, track, encodings):
        results = super().infer(cases, track, encodings)
        if track == CANDIDATE_CONTRACT and any(case.group == "B" for case in cases):
            results[0]["native_score"] = float(results[0]["native_score"]) + 0.001
            results[0]["recomputed_score"] = float(results[0]["recomputed_score"]) + 0.001
        if track == LEGACY_CONTRACT:
            results[0]["legacy_opaque_score"] = float(
                results[0]["legacy_opaque_score"]
            ) + 0.001
        return results


def fake_identity(runtime: FakeRuntime) -> dict[str, Any]:
    return {
        "container_image_digest": trust.PINNED_CONTAINER_IMAGE_DIGEST,
        "model_id": trust.MODEL_ID,
        "model_revision": trust.PINNED_REVISION,
        "runtime_sha256": schema.sha256_bytes(
            schema.canonical_json_bytes(runtime.runtime_identity),
            domain=b"querit-runtime-identity-v1\0",
        ),
        "snapshot_tree_sha256": schema.sha256_bytes(
            schema.canonical_json_bytes(runtime.snapshot_ledger),
            domain=b"querit-snapshot-tree-v1\0",
        ),
        "source_tree_sha256": schema.sha256_bytes(
            schema.canonical_json_bytes(runtime.source_ledger),
            domain=b"querit-source-tree-v1\0",
        ),
        "system_python": runtime.runtime_identity["system_python"],
        "trusted_model_ledger_sha256": trust.TRUSTED_MODEL_LEDGER_SHA256,
    }


class CorpusAndScheduleTests(unittest.TestCase):
    def test_committed_corpus_has_exact_groups_hostility_and_unicode_pairs(self) -> None:
        definitions = replay.corpus_definitions()
        self.assertEqual(len(definitions), 40)
        self.assertEqual(
            replay.corpus_definition_sha256(),
            "084582c8e85dd7705a108ad420ed99376ee7b63d2b8720dcc059c8fcac63bb47",
        )
        counts = {
            group: sum(case.group == group for case in definitions)
            for group in ("W", "B", "ZH", "XL", "H", "L")
        }
        self.assertEqual(counts, {"W": 4, "B": 8, "ZH": 8, "XL": 8, "H": 8, "L": 4})
        hostile = "\n".join((case.query or "") + (case.document or "") for case in definitions)
        for marker in ("[CLS]", "<|im_start|>", "<|im_end|>", "\x00", "\r\n", "\t", "\u202e", "\u200d"):
            self.assertIn(marker, hostile)
        nfc = next(case for case in definitions if case.case_id == "H06").document
        nfd = next(case for case in definitions if case.case_id == "H07").document
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(unicodedata.normalize("NFC", nfc), unicodedata.normalize("NFC", nfd))
        self.assertEqual(
            [case.target_prepack_tokens for case in definitions if case.group == "L"],
            [40958, 40959, 40960, 40961],
        )

    def test_boundary_materialization_is_exact_or_fails_never_approximates(self) -> None:
        tokenizer = BoundaryTokenizer()
        cases = replay.materialize_corpus(tokenizer)
        for case in cases:
            if case.group != "L":
                continue
            prompt = render_current_prompt(case.query, case.document)
            actual = len(
                tokenizer(
                    prompt,
                    add_special_tokens=True,
                    padding=False,
                    truncation=False,
                )["input_ids"]
            )
            self.assertEqual(actual, case.target_prepack_tokens)
            self.assertLessEqual(len(case.document), 32768)
        with self.assertRaises(replay.ReplayError):
            replay.construct_exact_boundary_document(
                AlwaysEvenTokenizer(), "boundary", 40959, max_chars=32768
            )

    def test_schedule_is_exactly_680_and_has_predeclared_permutations(self) -> None:
        schedule = replay.replay_schedule()
        self.assertEqual(replay.schedule_observation_count(schedule), 680)
        self.assertEqual(
            replay.schedule_sha256(schedule),
            "18421d4b6f0c0767a004e4b2bdf00151b3318da571f636406df44488c2c589b3",
        )
        phase_counts = {}
        for batch in schedule:
            phase_counts[batch.phase] = phase_counts.get(batch.phase, 0) + len(batch.case_ids)
        self.assertEqual(
            phase_counts,
            {
                "w4-calibration": 80,
                "w4-all-permutations": 192,
                "b8-mixed-permutations": 272,
                "language-hostility": 96,
                "long-boundaries": 40,
            },
        )
        w4_permutations = {
            batch.case_ids
            for batch in schedule
            if batch.phase == "w4-all-permutations" and batch.track == LEGACY_CONTRACT
        }
        self.assertEqual(len(w4_permutations), 24)
        b8_permutations = {
            batch.case_ids
            for batch in schedule
            if batch.phase == "b8-mixed-permutations" and len(batch.case_ids) == 8
            and batch.track == LEGACY_CONTRACT
        }
        self.assertEqual(len(b8_permutations), 16)
        self.assertEqual(replay.schedule_sha256(schedule), replay.schedule_sha256(replay.replay_schedule()))


class OfflineReplayTests(unittest.TestCase):
    def test_fake_runtime_executes_all_rows_and_emits_verifiable_private_artifact(self) -> None:
        runtime = FakeRuntime()
        identity = fake_identity(runtime)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "output"
            run_dir = replay.run_replay(
                runtime,
                output_root=output_root,
                run_id="fake-runtime-test",
                identity=identity,
            )
            self.assertEqual(run_dir.parent, output_root)
            self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
            manifest = schema.validate_artifact_set(
                run_dir, expected_identity=identity, require_receipt=False
            )
            self.assertEqual(manifest["status"], "SEMANTIC_PASS")
            self.assertEqual(manifest["artifacts"][schema.OBSERVATIONS_NAME]["count"], 680)
            self.assertFalse((run_dir / schema.RECEIPT_NAME).exists())
            with self.assertRaisesRegex(schema.SchemaError, "final replay re-attestation"):
                schema.seal_pass_receipt(
                    run_dir,
                    snapshot_root=Path(temporary),
                    source_root=SCRIPTS,
                    runtime=runtime,
                )
            self.assertFalse((run_dir / schema.RECEIPT_NAME).exists())

    def test_failed_gates_emit_bounded_evidence_but_never_a_pass_receipt(self) -> None:
        runtime = DriftRuntime()
        identity = fake_identity(runtime)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "output"
            with self.assertRaisesRegex(replay.ReplayError, "no PASS receipt"):
                replay.run_replay(
                    runtime,
                    output_root=output_root,
                    run_id="expected-failure",
                    identity=identity,
                )
            run_dir = output_root / "expected-failure"
            manifest = schema.safe_read_json(
                run_dir / schema.MANIFEST_NAME, root=run_dir
            )
            errors = schema.safe_read_jsonl(run_dir / schema.ERRORS_NAME, root=run_dir)
            self.assertEqual(manifest["status"], "FAIL")
            self.assertEqual(manifest["gates"]["candidate_batch_invariance"], "FAIL")
            self.assertEqual(manifest["gates"]["candidate_permutation_invariance"], "FAIL")
            self.assertEqual(manifest["gates"]["legacy_direct_parity"], "FAIL")
            self.assertLessEqual(len(errors), schema.MAX_ERROR_COUNT)
            self.assertFalse((run_dir / schema.RECEIPT_NAME).exists())

    def test_output_escape_duplicate_run_and_nonlocal_snapshot_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(replay.ReplayError):
                replay.prepare_run_directory(root, "../escape")
            run = replay.prepare_run_directory(root, "valid-run")
            self.assertTrue(run.is_dir())
            with self.assertRaises(replay.ReplayError):
                replay.prepare_run_directory(root, "valid-run")
            with self.assertRaises(replay.ReplayError):
                replay.require_local_snapshot("https://example.invalid/model")
            linked = root / "linked-snapshot"
            local = root / "snapshot"
            local.mkdir()
            linked.symlink_to(local, target_is_directory=True)
            with self.assertRaises(replay.ReplayError):
                replay.require_local_snapshot(str(linked))

    def test_snapshot_ledger_is_bounded_regular_nofollow_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_bytes(b"a")
            (root / "b").write_bytes(b"bb")
            first, first_hash = replay.snapshot_ledger(root, maximum_files=2, maximum_bytes=3)
            second, second_hash = replay.snapshot_ledger(root, maximum_files=2, maximum_bytes=3)
            self.assertEqual((first, first_hash), (second, second_hash))
            self.assertEqual([row["path"] for row in first], ["a", "b"])
            with self.assertRaises(replay.ReplayError):
                replay.snapshot_ledger(root, maximum_files=1, maximum_bytes=3)
            (root / "link").symlink_to(root / "a")
            with self.assertRaises(replay.ReplayError):
                replay.snapshot_ledger(root, maximum_files=3, maximum_bytes=4)
            (root / "link").unlink()
            os.link(root / "a", root / "hardlink")
            with self.assertRaises(replay.ReplayError):
                replay.snapshot_ledger(root, maximum_files=3, maximum_bytes=4)

    def test_trusted_model_ledger_and_caller_identity_are_fail_closed(self) -> None:
        by_name = {row["path"]: row for row in trust.TRUSTED_MODEL_FILES}
        self.assertEqual(set(by_name), {row["path"] for row in trust.TRUSTED_MODEL_FILES})
        self.assertEqual(by_name["config.json"]["sha256"], "4fd7167e58d6adbf806ddd06894e65efdee4d3dfa7532bd897f0ca68ac84fb4c")
        self.assertEqual(by_name["tokenizer.json"]["size"], 11423129)
        self.assertEqual(by_name["modeling_querit_4b.py"]["size"], 3770)
        self.assertEqual(by_name["model.safetensors.index.json"]["size"], 32958)
        self.assertEqual(by_name["model-00001-of-00002.safetensors"]["sha256"], "5b2b13727c7138ba8b75e87a9c38f321f1fab710633f19bee2b48cece6d06bbf")
        self.assertEqual(by_name["model-00002-of-00002.safetensors"]["sha256"], "79aa6357725b61757d902afac3ff52e79b9193b2f5db8dd7a9ce3ba312469694")
        identity = fake_identity(FakeRuntime())
        schema._validate_identity(identity)
        with self.assertRaises(schema.SchemaError):
            schema._validate_identity({**identity, "trusted_model_ledger_sha256": "0" * 64})
        for mutation in (
            {"launcher_path": "/tmp/caller-python"},
            {"mode": "0775"},
            {"uid": 1001},
            {"sha256": "g" * 64},
        ):
            hostile_python = {**identity["system_python"], **mutation}
            with self.assertRaises(schema.SchemaError):
                schema._validate_identity({**identity, "system_python": hostile_python})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(trust.TrustError):
                trust.attest_trusted_snapshot(root)

    def test_launcher_contract_and_in_process_gate_precede_model_import(self) -> None:
        arguments = [
            "run",
            "--model",
            "/not-used",
            "--output-root",
            "/not-used",
            "--run-id",
            "gate",
        ]
        with mock.patch.object(sys, "executable", "/tmp/caller-controlled-python"), mock.patch.dict(
            os.environ,
            {"QUERIT_PYTHON": "/tmp/environment-controlled-python"},
            clear=False,
        ):
            command = sandbox.build_unshare_argv(arguments)
        self.assertEqual(command[:5], ["/usr/bin/unshare", "--user", "--map-root-user", "--net", "--"])
        self.assertEqual(
            (SCRIPTS / "querit_replay_sandbox.py").read_text(encoding="utf-8").splitlines()[0],
            "#!/usr/bin/python3",
        )
        self.assertEqual((SCRIPTS / "querit_replay_sandbox.py").stat().st_mode & 0o777, 0o755)
        system_python = sandbox.attest_system_python()
        self.assertEqual(system_python["launcher_path"], "/usr/bin/python3")
        self.assertEqual(command[5], system_python["resolved_path"])
        self.assertNotIn("caller-controlled", " ".join(command))
        self.assertNotIn("environment-controlled", " ".join(command))
        with mock.patch.dict(
            os.environ,
            {"HTTPS_PROXY": "secret", "LD_PRELOAD": "evil", "SAFE_VALUE": "kept"},
            clear=True,
        ):
            environment = sandbox.sanitized_environment()
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertNotIn("LD_PRELOAD", environment)
        self.assertNotIn("SAFE_VALUE", environment)
        self.assertRegex(environment["QUERIT_PARENT_NETNS"], r"^net:\[[0-9]+\]$")
        self.assertEqual(environment["HOME"], "/nonexistent")
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        probe = [
            "/usr/bin/unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--",
            sandbox.attest_system_python()["resolved_path"],
            "-I",
            "-c",
            (
                f"import sys;sys.path.insert(0,{str(SCRIPTS)!r});"
                "import querit_replay_sandbox as s;print(s.attest_network_isolation())"
            ),
        ]
        completed = subprocess.run(
            probe,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertLessEqual(len(completed.stdout) + len(completed.stderr), 4096)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertRegex(completed.stdout.strip(), r"^[0-9a-f]{64}$")
        launch = subprocess.run(
            command,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertLessEqual(len(launch.stdout) + len(launch.stderr), 4096)
        self.assertEqual(launch.returncode, 1)
        self.assertIn("local model snapshot", launch.stderr)
        already_imported = "querit_replay_runtime" in sys.modules
        with self.assertRaises(replay.ReplayError):
            replay.main(
                [
                    "run",
                    "--model",
                    "/definitely-not-a-model",
                    "--output-root",
                    "/definitely-not-output",
                    "--run-id",
                    "gate",
                ]
            )
        if not already_imported:
            self.assertNotIn("querit_replay_runtime", sys.modules)

    def test_isolated_fake_runtime_cannot_seal_authoritative_receipt(self) -> None:
        environment = sandbox.sanitized_environment()
        code = f"""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, {str(SCRIPTS)!r})
sys.path.insert(0, {str(ROOT / 'tests')!r})
import querit_offline_replay as replay
import querit_replay_schema as schema
import test_querit_offline_replay as support
runtime = support.FakeRuntime()
identity = support.fake_identity(runtime)
with tempfile.TemporaryDirectory() as temporary:
    output = Path(temporary)
    run = replay.run_replay(
        runtime,
        output_root=output,
        run_id='hostile-fake-runtime',
        identity=identity,
    )
    try:
        schema.seal_pass_receipt(
            run,
            snapshot_root=output,
            source_root=Path({str(SCRIPTS)!r}),
            runtime=runtime,
        )
    except schema.SchemaError:
        assert not (run / schema.RECEIPT_NAME).exists()
        print('FAKE_RUNTIME_BLOCKED')
    else:
        raise AssertionError('fake runtime created authority')
"""
        command = [
            "/usr/bin/unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--",
            sandbox.attest_system_python()["resolved_path"],
            "-I",
            "-c",
            code,
        ]
        completed = subprocess.run(
            command,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertLessEqual(len(completed.stdout) + len(completed.stderr), 4096)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "FAKE_RUNTIME_BLOCKED")

    def test_system_python_chain_rejects_unsafe_link_mode_owner_and_nonregular_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "trusted"
            root.mkdir(mode=0o755)
            executable = root / "python3.11"
            shutil.copy2("/bin/true", executable)
            executable.chmod(0o755)
            launcher = root / "python3"
            launcher.symlink_to("python3.11")
            kwargs = {
                "trusted_roots": (root,),
                "expected_uid": os.getuid(),
                "expected_gid": os.getgid(),
            }
            baseline = sandbox._attest_python_path(launcher, **kwargs)
            self.assertEqual(baseline["resolved_path"], str(executable))
            self.assertRegex(baseline["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(baseline["chain_sha256"], r"^[0-9a-f]{64}$")

            executable.chmod(0o775)
            with self.assertRaises(sandbox.SandboxError):
                sandbox._attest_python_path(launcher, **kwargs)
            executable.chmod(0o755)

            with self.assertRaises(sandbox.SandboxError):
                sandbox._attest_python_path(
                    launcher,
                    trusted_roots=(root,),
                    expected_uid=os.getuid() + 1,
                    expected_gid=os.getgid(),
                )

            launcher.unlink()
            hostile_directory = root / "hostile"
            hostile_directory.mkdir(mode=0o777)
            hostile_directory.chmod(0o777)
            nested = hostile_directory / "python3.11"
            shutil.copy2("/bin/true", nested)
            nested.chmod(0o755)
            launcher.symlink_to("hostile/python3.11")
            with self.assertRaises(sandbox.SandboxError):
                sandbox._attest_python_path(launcher, **kwargs)

            hostile_directory.chmod(0o755)
            launcher.unlink()
            launcher.symlink_to("hostile", target_is_directory=True)
            with self.assertRaises(sandbox.SandboxError):
                sandbox._attest_python_path(launcher, **kwargs)

            launcher.unlink()
            launcher.symlink_to("/bin/true")
            with self.assertRaises(sandbox.SandboxError):
                sandbox._attest_python_path(launcher, **kwargs)

    def test_tolerances_are_predeclared_capped_and_rank_gate_is_strict(self) -> None:
        calibrated = replay.calibrate_tolerance(jitter=0.000002, batch_delta=0.000003, scalar="score")
        self.assertEqual(calibrated, 0.000016)
        with self.assertRaises(replay.ReplayError):
            replay.calibrate_tolerance(jitter=0.1, batch_delta=0.0, scalar="score")
        self.assertTrue(replay.pairwise_rank_preserved([0.0, 0.1], [0.001, 0.099], 0.01))
        self.assertFalse(replay.pairwise_rank_preserved([0.0, 0.1], [0.2, -0.1], 0.01))


if __name__ == "__main__":
    unittest.main()
