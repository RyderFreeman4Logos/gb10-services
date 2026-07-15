from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import querit_offline_replay as replay  # noqa: E402
import querit_replay_schema as schema  # noqa: E402
import querit_replay_trust as trust  # noqa: E402
import test_querit_offline_replay as support  # noqa: E402


class CanonicalAndHashTests(unittest.TestCase):
    def test_canonical_json_and_domain_separated_hashes(self) -> None:
        payload = {"é": "NFD:e\u0301", "a": [1, True, None]}
        self.assertEqual(
            schema.canonical_json_bytes(payload),
            b'{"a":[1,true,null],"\xc3\xa9":"NFD:e\xcc\x81"}',
        )
        self.assertNotEqual(
            schema.token_ids_sha256([1, 0]), schema.attention_mask_sha256([1, 0])
        )
        self.assertNotEqual(
            schema.token_ids_sha256([1, 2]), schema.token_ids_sha256([2, 1])
        )
        self.assertEqual(
            schema.float32_cell(0.5), {"f32_be": "3f000000", "value": 0.5}
        )
        first = schema.normalized_f32_tensor_sha256([1.0, -2.0], [1, 2])
        second = schema.normalized_f32_tensor_sha256([1.0, -2.0], [2, 1])
        self.assertNotEqual(first, second)
        with self.assertRaises(schema.SchemaError):
            schema.canonical_json_bytes({"bad": math.nan})
        with self.assertRaises(schema.SchemaError):
            schema.normalized_f32_tensor_sha256([math.inf], [1])

    def test_private_atomic_bounded_nofollow_single_link_io(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "value.json"
            schema.write_atomic_json(target, {"value": 1}, root=root)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.read_bytes(), b'{"value":1}\n')
            self.assertEqual(schema.safe_read_json(target, root=root), {"value": 1})
            with self.assertRaises(schema.SchemaError):
                schema.safe_read_json(target, root=root, maximum_bytes=2)
            with self.assertRaises(schema.SchemaError):
                schema.write_atomic_json(root.parent / "escape.json", {}, root=root)
            link = root / "linked.json"
            link.symlink_to(target)
            with self.assertRaises(schema.SchemaError):
                schema.safe_read_json(link, root=root)
            hardlink = root / "hardlinked.json"
            os.link(target, hardlink)
            with self.assertRaises(schema.SchemaError):
                schema.safe_read_json(target, root=root)


class ArtifactValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)
        self._create()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create(self) -> None:
        runtime = support.FakeRuntime()
        self.identity = support.fake_identity(runtime)
        self.root = replay.run_replay(
            runtime,
            output_root=self.output,
            run_id="source-only-test-run",
            identity=self.identity,
        )

    def _reset(self) -> None:
        shutil.rmtree(self.root)
        self._create()

    def _reseal(self) -> None:
        manifest = schema.safe_read_json(
            self.root / schema.MANIFEST_NAME, root=self.root
        )
        manifest.pop("artifacts")
        schema.seal_artifact_set(self.root, manifest)

    def _observations(self) -> list[dict[str, object]]:
        return schema.safe_read_jsonl(
            self.root / schema.OBSERVATIONS_NAME, root=self.root
        )

    def test_fixed_semantic_artifact_is_exact_but_has_no_authoritative_receipt(self) -> None:
        manifest = schema.validate_artifact_set(
            self.root, expected_identity=self.identity, require_receipt=False
        )
        self.assertEqual(manifest["status"], "SEMANTIC_PASS")
        self.assertEqual(manifest["artifacts"][schema.CASES_NAME]["count"], 40)
        self.assertEqual(manifest["artifacts"][schema.ENCODINGS_NAME]["count"], 80)
        self.assertEqual(
            manifest["artifacts"][schema.OBSERVATIONS_NAME]["count"], 680
        )
        self.assertEqual(
            manifest["corpus_definition_sha256"], replay.corpus_definition_sha256()
        )
        self.assertEqual(
            manifest["schedule_sha256"],
            replay.schedule_sha256(replay.replay_schedule()),
        )
        self.assertFalse((self.root / schema.RECEIPT_NAME).exists())
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(self.root, expected_identity=self.identity)

    def test_hash_schedule_duplicate_and_count_mutations_fail(self) -> None:
        cases_path = self.root / schema.CASES_NAME
        cases = schema.safe_read_jsonl(cases_path, root=self.root)
        cases[0]["query"] += " tampered"
        schema.write_atomic_jsonl(cases_path, cases, root=self.root)
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

        self._reset()
        observations = self._observations()
        observations[0]["observation_id"] = "qro-hostile"
        schema.write_atomic_jsonl(
            self.root / schema.OBSERVATIONS_NAME, observations, root=self.root
        )
        self._reseal()
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

        self._reset()
        observations = self._observations()
        observations[-1] = dict(observations[0])
        schema.write_atomic_jsonl(
            self.root / schema.OBSERVATIONS_NAME, observations, root=self.root
        )
        self._reseal()
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

        self._reset()
        manifest = schema.safe_read_json(
            self.root / schema.MANIFEST_NAME, root=self.root
        )
        manifest["artifacts"][schema.CASES_NAME]["count"] = 39
        schema.write_atomic_json(
            self.root / schema.MANIFEST_NAME, manifest, root=self.root
        )
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

    def test_float_formula_selection_and_rank_mutations_fail(self) -> None:
        observations = self._observations()
        observations[0]["native_score"] = {"f32_be": "7fc00000", "value": 0.0}
        schema.write_atomic_jsonl(
            self.root / schema.OBSERVATIONS_NAME, observations, root=self.root
        )
        self._reseal()
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

        self._reset()
        observations = self._observations()
        candidate = next(
            row
            for row in observations
            if row["track"] == schema.CANDIDATE_CONTRACT
        )
        candidate["selected_index"] = 0
        schema.write_atomic_jsonl(
            self.root / schema.OBSERVATIONS_NAME, observations, root=self.root
        )
        self._reseal()
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

        self._reset()
        observations = self._observations()
        observations[0]["stable_rank"] = 7
        observations[0]["sorted_index"] = 7
        schema.write_atomic_jsonl(
            self.root / schema.OBSERVATIONS_NAME, observations, root=self.root
        )
        self._reseal()
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

    def test_head_source_snapshot_and_trusted_identity_mutations_fail(self) -> None:
        manifest = schema.safe_read_json(
            self.root / schema.MANIFEST_NAME, root=self.root
        )
        manifest["classifier_load_report"].pop("error_msgs")
        manifest.pop("artifacts")
        schema.seal_artifact_set(self.root, manifest)
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

        self._reset()
        manifest = schema.safe_read_json(
            self.root / schema.MANIFEST_NAME, root=self.root
        )
        manifest["snapshot_ledger"].append(dict(manifest["snapshot_ledger"][0]))
        manifest["runtime"]["snapshot_file_count"] += 1
        manifest["identity"]["runtime_sha256"] = schema.sha256_bytes(
            schema.canonical_json_bytes(manifest["runtime"]),
            domain=b"querit-runtime-identity-v1\0",
        )
        manifest["identity"]["snapshot_tree_sha256"] = schema.sha256_bytes(
            schema.canonical_json_bytes(manifest["snapshot_ledger"]),
            domain=b"querit-snapshot-tree-v1\0",
        )
        manifest.pop("artifacts")
        with self.assertRaises(schema.SchemaError):
            schema.seal_artifact_set(self.root, manifest)

        self._reset()
        manifest = schema.safe_read_json(
            self.root / schema.MANIFEST_NAME, root=self.root
        )
        manifest["identity"]["trusted_model_ledger_sha256"] = "0" * 64
        manifest.pop("artifacts")
        with self.assertRaises(schema.SchemaError):
            schema.seal_artifact_set(self.root, manifest)

    def test_nonfinite_duplicate_json_oversize_and_hardlink_fail(self) -> None:
        path = self.root / schema.ERRORS_NAME
        path.write_bytes(b'{"value":NaN}\n')
        with self.assertRaises(schema.SchemaError):
            schema.safe_read_jsonl(path, root=self.root)
        path.write_bytes(b'{"a":1,"a":2}\n')
        with self.assertRaises(schema.SchemaError):
            schema.safe_read_jsonl(path, root=self.root)
        path.write_bytes(b"x" * 33)
        with self.assertRaises(schema.SchemaError):
            schema.safe_read_jsonl(path, root=self.root, maximum_bytes=32)

        self._reset()
        alias = self.root / "cases-hardlink.jsonl"
        os.link(self.root / schema.CASES_NAME, alias)
        with self.assertRaises(schema.SchemaError):
            schema.validate_artifact_set(
                self.root, expected_identity=self.identity, require_receipt=False
            )

    def test_source_ledger_rehashes_all_reviewed_sources(self) -> None:
        ledger, tree_hash, hashes = trust.attest_source_tree(SCRIPTS)
        self.assertEqual([row["path"] for row in ledger], list(trust.SOURCE_NAMES))
        self.assertEqual(hashes, {row["path"]: row["sha256"] for row in ledger})
        self.assertEqual(
            tree_hash,
            schema.sha256_bytes(
                schema.canonical_json_bytes(ledger),
                domain=b"querit-source-tree-v1\0",
            ),
        )


if __name__ == "__main__":
    unittest.main()
