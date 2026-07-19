from __future__ import annotations

import copy
import json
import math
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import querit_legacy_canary_equivalence as harness  # noqa: E402


class PlanCorpusAndScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan, self.plan_hash = harness.load_plan()
        self.corpus = harness.validate_corpus(harness.DEFAULT_CORPUS, self.plan)
        self.calibration, self.evaluation = harness.split_groups(self.corpus, self.plan)

    def test_committed_corpus_identity_cardinality_and_tuple_identity(self) -> None:
        self.assertEqual(len(self.corpus), 200)
        self.assertEqual(sum(len(group.candidates) for group in self.corpus), 2000)
        self.assertEqual(Counter(group.source_language for group in self.corpus), {"en": 100, "zh": 100})
        self.assertEqual(len(self.calibration), 40)
        self.assertEqual(len(self.evaluation), 160)
        bare = Counter(group.query_id for group in self.corpus)
        self.assertTrue(any(count > 1 for count in bare.values()))
        self.assertEqual(len({(group.source_language, group.query_id) for group in self.corpus}), 200)

    def test_plan_rejects_unknown_fields_bad_threshold_and_corpus_hash_drift(self) -> None:
        changed = copy.deepcopy(self.plan)
        changed["unexpected"] = True
        with self.assertRaises(harness.PlanError):
            harness.validate_plan(changed)
        changed = copy.deepcopy(self.plan)
        changed["thresholds"]["ranking"]["overall"]["top1_agreement_min"] = math.nan
        with self.assertRaises(harness.PlanError):
            harness.validate_plan(changed)
        changed = copy.deepcopy(self.plan)
        changed["corpus"]["sha256"] = "0" * 64
        with self.assertRaises(harness.HarnessError):
            harness.validate_corpus(harness.DEFAULT_CORPUS, changed)
        changed = copy.deepcopy(self.plan)
        changed["corpus"]["groups"] = 199
        with self.assertRaises(harness.HarnessError):
            harness.validate_corpus(harness.DEFAULT_CORPUS, changed)

    def test_split_schedule_hashes_are_deterministic_and_endpoint_order_alternates(self) -> None:
        again = harness.split_groups(self.corpus, self.plan)
        self.assertEqual(
            [row.identity_digest for row in self.calibration],
            [row.identity_digest for row in again[0]],
        )
        schedule = harness.main_schedule([*self.calibration, *self.evaluation])
        self.assertEqual([row.endpoint for row in schedule[:4]], ["legacy", "candidate", "candidate", "legacy"])
        self.assertEqual(harness.schedule_sha256(schedule), harness.schedule_sha256(list(schedule)))
        self.assertEqual(self.plan["schedule"]["retries"], 0)
        self.assertEqual(len(harness.warm_schedule(self.calibration, 8)), 80)

    def test_dry_run_never_initializes_transport_or_uses_urls(self) -> None:
        invoked = False

        def forbidden(*_args: object) -> harness.HttpResponse:
            nonlocal invoked
            invoked = True
            raise AssertionError("dry run initialized transport")

        args = Namespace(
            corpus=harness.DEFAULT_CORPUS,
            plan=harness.DEFAULT_PLAN,
            legacy_url="not a URL",
            candidate_url="also not a URL",
            dry_run=True,
            output=None,
        )
        code, receipt = harness.run(args, transport=forbidden)
        self.assertEqual(code, harness.EXIT_PASS)
        self.assertFalse(invoked)
        self.assertEqual(receipt["schedule"]["attempts"], {"main": 400, "auxiliary": 4, "warm_1": 80, "warm_2": 80, "warm_4": 80, "warm_8": 80})


class NativeContractNormalizationTests(unittest.TestCase):
    def legacy_body(self, rows: list[dict[str, object]], *, alias: str = "results") -> bytes:
        return json.dumps({alias: rows}, allow_nan=False, separators=(",", ":")).encode()

    def test_legacy_sorted_aliases_restore_input_order(self) -> None:
        rows = [
            {"index": index, "document_index": index, "score": index / 10.0, "relevance_score": index / 10.0}
            for index in reversed(range(10))
        ]
        self.assertEqual(harness.normalize_legacy_response(self.legacy_body(rows)), tuple(index / 10.0 for index in range(10)))
        self.assertEqual(harness.normalize_legacy_response(self.legacy_body(rows, alias="data")), tuple(index / 10.0 for index in range(10)))
        with self.assertRaises(harness.NormalizationError):
            harness.normalize_legacy_response(b'{"unsupported":[]}')

    def test_legacy_rejects_every_index_cardinality_nonfinite_and_domain_failure(self) -> None:
        valid = [{"index": index, "score": 0.0} for index in range(10)]
        invalid_rows = [
            valid[:-1],
            [{"index": 0, "score": 0.0}] * 10,
            [{"index": 10, "score": 0.0}] + valid[1:],
            [{"index": 0, "score": 2.0}] + valid[1:],
        ]
        for rows in invalid_rows:
            with self.subTest(rows=rows[:1]), self.assertRaises(harness.NormalizationError):
                harness.normalize_legacy_response(self.legacy_body(rows))
        nonfinite = b'{"results":[{"index":0,"score":NaN}]}'
        with self.assertRaises(harness.NormalizationError):
            harness.normalize_legacy_response(nonfinite, 1)
        disagreement = {"results": valid, "data": [{"index": index, "score": 0.1} for index in range(10)]}
        with self.assertRaises(harness.NormalizationError):
            harness.normalize_legacy_response(json.dumps(disagreement, separators=(",", ":")).encode())

    def test_candidate_positional_pairing_and_signed_normalization_are_strict(self) -> None:
        body = json.dumps({"input_tokens": 0, "scores": [0.0, 0.5, 1.0]}, separators=(",", ":")).encode()
        self.assertEqual(harness.normalize_candidate_response(body, 3), (-1.0, 0.0, 1.0))
        for bad in (
            b'{"input_tokens":0,"scores":[0.0,0.5]}',
            b'{"input_tokens":0,"scores":[0.0,NaN,1.0]}',
            b'{"input_tokens":0,"scores":[0.0,0.5,1.00001]}',
        ):
            with self.subTest(bad=bad), self.assertRaises(harness.NormalizationError):
                harness.normalize_candidate_response(bad, 3)
        group = harness.validate_corpus(harness.DEFAULT_CORPUS, harness.load_plan()[0])[0]
        request = json.loads(harness._candidate_request(group))
        self.assertEqual(request["queries"], [group.query] * 10)
        self.assertNotIn("instruction", request)
        self.assertNotIn("service_tier", request)


class AggregationAndVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan, _ = harness.load_plan()
        self.corpus = harness.validate_corpus(harness.DEFAULT_CORPUS, self.plan)
        self.calibration, self.evaluation = harness.split_groups(self.corpus, self.plan)

    def _schedules(self) -> dict[str, list[harness.PlannedAttempt]]:
        schedules = {
            "auxiliary": harness.auxiliary_schedule(self.calibration),
            "main": harness.main_schedule([*self.calibration, *self.evaluation]),
        }
        for concurrency in (1, 2, 4, 8):
            schedules[f"warm_{concurrency}"] = harness.warm_schedule(self.calibration, concurrency)
        return schedules

    def _attempts(self, *, failure: str | None = None, duration: float = 0.1) -> tuple[dict[str, list[harness.PlannedAttempt]], list[harness.Attempt]]:
        schedules = self._schedules()
        scores = tuple(1.0 - index / 20.0 for index in range(10))
        attempts = [
            harness.Attempt(plan, duration, failure, None if failure else scores)
            for rows in schedules.values()
            for plan in rows
        ]
        return schedules, attempts

    def test_failed_attempts_remain_in_wilson_denominators_and_quality_is_inconclusive(self) -> None:
        _schedules, attempts = self._attempts(failure="http")
        metrics, verdicts = harness.aggregate_results(
            attempts,
            main_groups=[*self.calibration, *self.evaluation],
            evaluation=self.evaluation,
            plan=self.plan,
        )
        self.assertEqual(metrics["failure_classes"]["legacy"]["all"]["wilson_failure_rate"]["attempts"], 362)
        self.assertEqual(metrics["failure_classes"]["candidate"]["all"]["wilson_failure_rate"]["attempts"], 362)
        self.assertEqual(metrics["failure_classes"]["legacy"]["main"]["wilson_failure_rate"]["attempts"], 200)
        self.assertEqual(verdicts["ranking_behavior"], harness.INCONCLUSIVE)
        self.assertEqual(verdicts["reliability"], harness.FAIL)

    def test_fixed_latency_aggregation_bootstrap_and_exit_conventions(self) -> None:
        schedules, attempts = self._attempts()
        short_plan = copy.deepcopy(self.plan)
        short_plan["bootstrap"]["resamples"] = 3
        metrics, verdicts = harness.aggregate_results(
            attempts,
            main_groups=[*self.calibration, *self.evaluation],
            evaluation=self.evaluation,
            plan=short_plan,
        )
        warm = metrics["latency"]["warm"]["1"]
        self.assertEqual(warm["legacy"]["p95"], 0.1)
        self.assertEqual(warm["legacy"]["p50"], 0.1)
        self.assertEqual(warm["legacy"]["p99"], 0.1)
        self.assertAlmostEqual(warm["legacy"]["pairs_per_second"], 100.0)
        self.assertEqual(warm["candidate_legacy_p95_ratio"], 1.0)
        self.assertEqual(verdicts["wire_drop_in_compatibility"], "NOT_CLAIMED_CONTRACTS_DIFFER")
        self.assertEqual(verdicts["score_interchangeability"], "NOT_CLAIMED_EXPECTED_NONIDENTICAL")
        self.assertEqual(verdicts["canary_operational_suitability"], harness.PASS)
        receipt = harness.build_receipt(
            plan=self.plan,
            plan_hash="0" * 64,
            corpus=self.corpus,
            schedules=schedules,
            metrics=metrics,
            verdicts=verdicts,
            legacy_url="http://127.0.0.1:18013",
            candidate_url="http://127.0.0.1:18014",
        )
        self.assertEqual(harness.validate_receipt(receipt), receipt)
        self.assertEqual(harness.exit_code_for(verdicts, attempts), harness.EXIT_PASS)
        failed = dict(verdicts, canary_operational_suitability=harness.FAIL)
        self.assertEqual(harness.exit_code_for(failed, attempts), harness.EXIT_FAIL)
        transport_attempt = harness.Attempt(attempts[0].plan, 0.1, "transport", None)
        self.assertEqual(harness.exit_code_for(failed, [transport_attempt]), harness.EXIT_INCONCLUSIVE)
        self.assertEqual(harness.exit_code_for(dict(failed, canary_operational_suitability=harness.INCONCLUSIVE), attempts), harness.EXIT_INCONCLUSIVE)
        boundary_attempts = [
            harness.Attempt(
                attempt.plan,
                0.2 if attempt.plan.phase == "warm" and attempt.plan.concurrency in (1, 2) and attempt.plan.endpoint == "candidate" else 0.1,
                attempt.failure_class,
                attempt.scores,
            )
            for attempt in attempts
        ]
        _boundary_metrics, boundary_verdicts = harness.aggregate_results(
            boundary_attempts,
            main_groups=[*self.calibration, *self.evaluation],
            evaluation=self.evaluation,
            plan=short_plan,
        )
        self.assertEqual(boundary_verdicts["latency_usability"], harness.PASS)

    def test_bootstrap_boundary_qrels_ranking_and_composition_are_deterministic(self) -> None:
        first = harness.paired_bootstrap_lower_bound([0.0, 0.1, -0.1], seed=1801318014, resamples=10_000)
        self.assertEqual(first, harness.paired_bootstrap_lower_bound([0.0, 0.1, -0.1], seed=1801318014, resamples=10_000))
        values = {metric: {"lower_bound": -0.02} for metric in ("mrr_at_10", "ndcg_at_10", "map_at_10")}
        qrels = {"overall": values, "en": {metric: {"lower_bound": -0.04} for metric in values}, "zh": {metric: {"lower_bound": -0.04} for metric in values}}
        thresholds = self.plan["thresholds"]["qrels_quality_noninferiority"]
        self.assertEqual(harness._verdict_for_qrels(qrels, True, thresholds), harness.PASS)
        qrels["en"]["mrr_at_10"]["lower_bound"] = -0.0400001
        self.assertEqual(harness._verdict_for_qrels(qrels, True, thresholds), harness.FAIL)
        self.assertEqual(harness._verdict_for_qrels(qrels, False, thresholds), harness.INCONCLUSIVE)
        ranking = {"overall": {"top1_agreement": 0.9, "mean_spearman": 0.9, "top3_overlap": 0.85}, "per_language": {"en": {"top1_agreement": 0.85, "mean_spearman": 0.85, "top3_overlap": 0.8}, "zh": {"top1_agreement": 0.85, "mean_spearman": 0.85, "top3_overlap": 0.8}}}
        self.assertEqual(harness._verdict_for_ranking(ranking, True, self.plan["thresholds"]["ranking"]), harness.PASS)
        self.assertEqual(harness._compose(["a", "b"], {"a": harness.PASS, "b": harness.PASS}), harness.PASS)
        self.assertEqual(harness._compose(["a", "b"], {"a": harness.FAIL, "b": harness.INCONCLUSIVE}), harness.FAIL)
        self.assertEqual(harness._compose(["a", "b"], {"a": harness.PASS, "b": harness.INCONCLUSIVE}), harness.INCONCLUSIVE)
        for a in (harness.PASS, harness.FAIL, harness.INCONCLUSIVE):
            for b in (harness.PASS, harness.FAIL, harness.INCONCLUSIVE):
                expected = harness.PASS if (a, b) == (harness.PASS, harness.PASS) else (harness.FAIL if harness.FAIL in {a, b} else harness.INCONCLUSIVE)
                self.assertEqual(harness._compose(["a", "b"], {"a": a, "b": b}), expected)
        self.assertEqual(harness._wilson(0, 40)["attempts"], 40)


class ReceiptTests(unittest.TestCase):
    def receipt(self) -> dict[str, object]:
        return {
            "corpus": {},
            "identities": {"plan_sha256": "0" * 64},
            "metrics": {},
            "schedule": {},
            "schema": "querit-legacy-canary-equivalence-receipt-v2",
            "thresholds": {},
            "verdicts": {},
        }

    def test_privacy_rejects_fields_and_url_ip_path_credential_environment_values(self) -> None:
        for name in harness.FORBIDDEN_KEY_FRAGMENTS:
            with self.subTest(field=name), self.assertRaises(harness.ReceiptError):
                harness.assert_receipt_private({name: "text"})
        for value in (
            {"query": "text"},
            {"safe": "http://example.invalid"},
            {"safe": "host.example.invalid"},
            {"safe": "192.0.2.1"},
            {"safe": "/private/value"},
            {"safe": "Bearer secret"},
            {"safe": "TOKEN=value"},
        ):
            with self.subTest(value=value), self.assertRaises(harness.ReceiptError):
                harness.assert_receipt_private(value)
        self.assertEqual(harness.validate_receipt(self.receipt())["schema"], "querit-legacy-canary-equivalence-receipt-v2")

    def test_owner_only_atomic_receipt_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "private" / "receipt.json"
            harness.write_owner_only_receipt(target, self.receipt())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(target.read_text())["schema"], "querit-legacy-canary-equivalence-receipt-v2")
            self.assertEqual(os.environ.get("PYTHONHASHSEED"), os.environ.get("PYTHONHASHSEED"))


if __name__ == "__main__":
    unittest.main()
