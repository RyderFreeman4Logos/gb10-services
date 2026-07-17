from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

reranker = importlib.import_module("reranker_endpoint_equivalence")


CORPUS = ROOT / "data" / "reranker-equivalence" / "miracl-reranking-en-zh-dev.jsonl"
METADATA = ROOT / "data" / "reranker-equivalence" / "metadata.json"

SOURCE_SHA256 = {
    "en-corpus/dev-00000-of-00001.parquet": "cf0679ffc34fd67a14c68554b446618c353e7f0a8c3cb6ce1c31b14f9b765416",
    "en-qrels/dev-00000-of-00001.parquet": "925bacac253aa59680f308c2779367bc31e8189e569df5519880e623b489c7e4",
    "en-queries/dev-00000-of-00001.parquet": "79265c85080b101de7abd590cc6aecbafa14cf0001fa778f7c2eeac1928c7734",
    "en-top_ranked/dev-00000-of-00001.parquet": "f549fd8a3746546f37095083a9ce006f7348e09fab4da2b471b57703cf538c01",
    "zh-corpus/dev-00000-of-00001.parquet": "c0fdd6d7b6b7ca30dddc3bb2fe761433fc507c7eb9d99e502985faef5bc8d1a6",
    "zh-qrels/dev-00000-of-00001.parquet": "23d7986206a286f23683da48d42771bee8c61cb6e7e3cce8586625bb0f64999f",
    "zh-queries/dev-00000-of-00001.parquet": "f5c75b9f96531886dc906097d8451f508921c22850076be5820d4f51d8b6b7f2",
    "zh-top_ranked/dev-00000-of-00001.parquet": "023a96094f80d889f8fe026449c3fb528cb276ef8e463da7b4f0335eb8be2ecb",
}


def _response(scores: list[float], input_tokens: int = 17) -> bytes:
    return json.dumps(
        {
            "scores": scores,
            "input_tokens": input_tokens,
            "request_id": "request-test",
            "inference_status": {"status": "complete"},
        },
        separators=(",", ":"),
    ).encode()


def _valid_row(language: str = "en", query_id: str = "q-1") -> dict[str, Any]:
    return {
        "query_id": query_id,
        "query": "public query",
        "source_language": language,
        "candidates": [
            {
                "document_id": f"{query_id}-d-{index}",
                "document": f"public document {index}",
                "relevance": 1 if index == 2 else 0,
                "source_language": language,
                "top_ranked_rank": index + 1,
            }
            for index in range(10)
        ],
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


class EndpointEvidenceCacheTests(unittest.TestCase):
    def test_request_identity_has_golden_bytes_and_binds_every_paid_dimension(
        self,
    ) -> None:
        body = reranker.canonical_payload(["q"], ["d"])
        identity = reranker.request_identity(body, reranker.CLOUD_ENDPOINT)

        self.assertEqual(
            identity.canonical_bytes(),
            (
                b'{"body_sha256":"f12cae033231437c589a0172f374f6c82442a50638339da9'
                b'042bfd09a45b3560","instruction":null,"method":"POST","model":"Qwen/'
                b'Qwen3-Reranker-8B","path_and_query":"/v1/inference/Qwen/Qwen3-Reranker-8B'
                b'?version=5fa94080caafeaa45a15d11f969d7978e087a3db","provider":"deepinfra"'
                b',"provider_model_version":"5fa94080caafeaa45a15d11f969d7978e087a3db"'
                b',"request_body":"{\\"documents\\":[\\"d\\"],\\"queries\\":[\\"q\\"]}"'
                b',"runner_transform":"gb10-reranker-equivalence-v2+deepinfra-native-v1"'
                b',"schema":"reranker-request-identity-v2","service_tier":null}'
            ),
        )
        self.assertEqual(
            reranker.canonical_request_hash(identity),
            "b5fb011fedb315e44a30c38e483b15165af2ab5e50b2431b13385f64db376f43",
        )

        variants = [
            reranker.request_identity(body, reranker.LOCAL_ENDPOINT),
            reranker.request_identity(
                reranker.canonical_payload(["q"], ["d"], instruction="rank"),
                reranker.CLOUD_ENDPOINT,
            ),
            reranker.request_identity(
                reranker.canonical_payload(["q"], ["d"], service_tier="priority"),
                reranker.CLOUD_ENDPOINT,
            ),
            reranker.request_identity(
                body,
                reranker.EndpointSpec(
                    provider="deepinfra",
                    model="Qwen/Qwen3-Reranker-8B",
                    provider_model_version="different-immutable-version",
                    runner_transform="gb10-reranker-equivalence-v2+deepinfra-native-v1",
                ),
            ),
            reranker.request_identity(
                body,
                reranker.EndpointSpec(
                    provider="deepinfra",
                    model="Qwen/Qwen3-Reranker-8B",
                    provider_model_version=reranker.DEEPINFRA_MODEL_VERSION,
                    runner_transform="different-runner-transform",
                ),
            ),
        ]
        self.assertEqual(
            len(
                {
                    reranker.canonical_request_hash(identity),
                    *map(reranker.canonical_request_hash, variants),
                }
            ),
            1 + len(variants),
        )

    def test_cloud_and_local_share_exact_target_and_body_but_not_cache_identity(
        self,
    ) -> None:
        body = reranker.canonical_payload(
            ["q"], ["d"], instruction="rank", service_tier="priority"
        )
        cloud = reranker.request_identity(body, reranker.CLOUD_ENDPOINT)
        local = reranker.request_identity(body, reranker.LOCAL_ENDPOINT)
        self.assertEqual(cloud.path_and_query, local.path_and_query)
        self.assertEqual(cloud.request_body, local.request_body)
        self.assertEqual(
            cloud.path_and_query,
            "/v1/inference/Qwen/Qwen3-Reranker-8B"
            "?version=5fa94080caafeaa45a15d11f969d7978e087a3db",
        )
        self.assertEqual(
            reranker.endpoint_url("https://api.deepinfra.com", cloud),
            "https://api.deepinfra.com" + cloud.path_and_query,
        )
        self.assertEqual(
            reranker.endpoint_url("http://100.105.4.92:18014", local),
            "http://100.105.4.92:18014" + local.path_and_query,
        )
        with self.assertRaises(ValueError):
            reranker.endpoint_url("https://api.deepinfra.com?version=stale", cloud)
        self.assertNotEqual(
            reranker.canonical_request_hash(cloud),
            reranker.canonical_request_hash(local),
        )

    def test_cache_hit_prevents_network_and_http_errors_are_cached(self) -> None:
        calls = 0

        def transport(
            _url: str, _body: bytes, _headers: dict[str, str], _timeout: float
        ) -> reranker.HttpResult:
            nonlocal calls
            calls += 1
            return reranker.HttpResult(
                status=429,
                headers={"Content-Type": "application/json", "Retry-After": "10"},
                body=b'{"error":"capacity"}',
                elapsed_ms=12,
            )

        with tempfile.TemporaryDirectory() as raw_tmp:
            body = reranker.canonical_payload(["q"], ["d"])
            identity = reranker.request_identity(body, reranker.CLOUD_ENDPOINT)
            cache = reranker.EndpointEvidenceCache(Path(raw_tmp), transport=transport)
            first = cache.fetch(
                identity,
                base_url="https://api.deepinfra.com",
                api_key="secret-key",
                timeout=1,
            )
            second = cache.fetch(
                identity,
                base_url="https://api.deepinfra.com",
                api_key="different-secret",
                timeout=1,
            )
            self.assertEqual(calls, 1)
            self.assertFalse(first.from_cache)
            self.assertTrue(second.from_cache)
            self.assertEqual(second.status, 429)
            self.assertEqual(second.body, b'{"error":"capacity"}')

    def test_request_ledger_is_durable_before_network_send(self) -> None:
        observed = False
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            body = reranker.canonical_payload(["q"], ["d"])
            identity = reranker.request_identity(body, reranker.CLOUD_ENDPOINT)
            request_hash = reranker.canonical_request_hash(identity)

            def transport(
                _url: str,
                sent_body: bytes,
                _headers: dict[str, str],
                _timeout: float,
            ) -> reranker.HttpResult:
                nonlocal observed
                record_path = root / request_hash / "request.json"
                self.assertTrue(record_path.is_file())
                record = json.loads(record_path.read_text())
                self.assertEqual(record["request_hash"], request_hash)
                self.assertEqual(record["request_body"].encode(), sent_body)
                self.assertFalse((root / request_hash / "response.json").exists())
                observed = True
                return reranker.HttpResult(200, {}, _response([0.5]), 3)

            cache = reranker.EndpointEvidenceCache(root, transport=transport)
            cache.fetch(
                identity,
                base_url="https://api.deepinfra.com",
                api_key="secret-key",
                timeout=1,
            )
            self.assertTrue(observed)

    def test_offline_reader_needs_no_transport_base_url_or_secret(self) -> None:
        calls = 0

        def transport(
            _url: str, _body: bytes, _headers: dict[str, str], _timeout: float
        ) -> reranker.HttpResult:
            nonlocal calls
            calls += 1
            return reranker.HttpResult(200, {}, _response([0.5]), 2)

        with tempfile.TemporaryDirectory() as raw_tmp:
            cache = reranker.EndpointEvidenceCache(Path(raw_tmp), transport=transport)
            body = reranker.canonical_payload(["q"], ["d"])
            identity = reranker.request_identity(body, reranker.CLOUD_ENDPOINT)
            cache.fetch(
                identity,
                base_url="https://api.deepinfra.com",
                api_key="initial-paid-request-secret",
                timeout=1,
            )

            offline = reranker.EndpointEvidenceCache(Path(raw_tmp))
            cached = reranker.load_cached_batches([identity], cache=offline)
            self.assertEqual(calls, 1)
            self.assertEqual(cached[0].scores, (0.5,))

    def test_offline_reader_fails_on_missing_incomplete_or_ambiguous_local_cache(
        self,
    ) -> None:
        body = reranker.canonical_payload(["q"], ["d"])
        identity = reranker.request_identity(body, reranker.LOCAL_ENDPOINT)
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            cache = reranker.EndpointEvidenceCache(root)
            with self.assertRaises(reranker.CacheMissError):
                cache.load(identity)

            request_dir = root / reranker.canonical_request_hash(identity)
            request_dir.mkdir()
            (request_dir / "request.json").write_bytes(
                reranker.request_ledger_bytes(identity)
            )
            with self.assertRaises(reranker.CacheStateError):
                cache.load(identity)

            (request_dir / "ambiguous-1.json").write_text("{}\n")
            with self.assertRaises(reranker.CacheStateError):
                cache.load(identity)

    def test_ambiguous_transport_is_recorded_once_and_never_resent(self) -> None:
        calls = 0

        def ambiguous(
            _url: str, _body: bytes, _headers: dict[str, str], _timeout: float
        ) -> reranker.HttpResult:
            nonlocal calls
            calls += 1
            raise reranker.AmbiguousTransportError("connection ended after send")

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            body = reranker.canonical_payload(["q"], ["d"])
            identity = reranker.request_identity(body, reranker.CLOUD_ENDPOINT)
            cache = reranker.EndpointEvidenceCache(root, transport=ambiguous)
            with self.assertRaises(reranker.AmbiguousTransportError):
                cache.fetch(
                    identity,
                    base_url="https://api.deepinfra.com",
                    api_key="secret-key",
                    timeout=1,
                )
            with self.assertRaises(reranker.CacheStateError):
                cache.fetch(
                    identity,
                    base_url="https://api.deepinfra.com",
                    api_key="secret-key",
                    timeout=1,
                )
            request_dir = root / reranker.canonical_request_hash(identity)
            self.assertEqual(calls, 1)
            self.assertEqual(len(list(request_dir.glob("ambiguous-*.json"))), 1)
            self.assertFalse((request_dir / "response.json").exists())

    def test_headers_and_secrets_are_never_persisted(self) -> None:
        secret = "api-key-that-must-not-appear"

        def transport(
            _url: str, _body: bytes, _headers: dict[str, str], _timeout: float
        ) -> reranker.HttpResult:
            return reranker.HttpResult(
                200,
                {
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                    "Set-Cookie": f"session={secret}",
                    "X-Request-ID": "safe-id",
                    "X-Api-Key": secret,
                },
                _response([0.25]),
                4,
            )

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            body = reranker.canonical_payload(["q"], ["d"])
            identity = reranker.request_identity(body, reranker.CLOUD_ENDPOINT)
            cache = reranker.EndpointEvidenceCache(root, transport=transport)
            cache.fetch(
                identity,
                base_url="https://api.deepinfra.com",
                api_key=secret,
                timeout=1,
            )
            persisted = b"\n".join(
                path.read_bytes() for path in root.rglob("*") if path.is_file()
            )
            lowered = persisted.lower()
            self.assertNotIn(secret.encode(), persisted)
            self.assertNotIn(b"authorization", lowered)
            self.assertNotIn(b"set-cookie", lowered)
            self.assertNotIn(b"x-api-key", lowered)
            self.assertIn(b"x-request-id", lowered)

            leaking = reranker.EndpointEvidenceCache(
                root / "leaking",
                transport=lambda *_args: reranker.HttpResult(
                    200, {}, _response([0.1]) + secret.encode(), 1
                ),
            )
            leaking_identity = reranker.request_identity(
                reranker.canonical_payload(["q2"], ["d2"]),
                reranker.CLOUD_ENDPOINT,
            )
            with self.assertRaises(reranker.EvidenceError):
                leaking.fetch(
                    leaking_identity,
                    base_url="https://api.deepinfra.com",
                    api_key=secret,
                    timeout=1,
                )


class OfflineMainTests(unittest.TestCase):
    def test_cache_only_main_reads_both_caches_without_env_dns_socket_or_transport(
        self,
    ) -> None:
        row = _valid_row()
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            corpus = root / "corpus.jsonl"
            report = root / "report.json"
            _write_jsonl(corpus, [row])
            groups = reranker.load_corpus(corpus)
            payloads = reranker.build_batches(groups, 128)
            cache = reranker.EndpointEvidenceCache(
                root / "cache",
                transport=lambda *_args: reranker.HttpResult(
                    200, {}, _response([0.5] * 10), 1
                ),
            )
            for endpoint, base_url, key in (
                (reranker.CLOUD_ENDPOINT, "https://api.deepinfra.com", "cloud-key"),
                (reranker.LOCAL_ENDPOINT, "http://127.0.0.1:18014", ""),
            ):
                for payload in payloads:
                    cache.fetch(
                        reranker.request_identity(payload, endpoint),
                        base_url=base_url,
                        api_key=key,
                        timeout=1,
                    )

            class NoEnvironment(dict[str, str]):
                def get(self, *_args: object, **_kwargs: object) -> str:
                    raise AssertionError("cache-only read an environment variable")

            with (
                mock.patch.object(os, "environ", NoEnvironment()),
                mock.patch(
                    "socket.getaddrinfo", side_effect=AssertionError("DNS attempted")
                ),
                mock.patch(
                    "socket.socket", side_effect=AssertionError("socket attempted")
                ),
                mock.patch.object(
                    reranker,
                    "_urllib_transport",
                    side_effect=AssertionError("transport attempted"),
                ),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "reranker_endpoint_equivalence.py",
                        "--cache-only",
                        "--corpus",
                        str(corpus),
                        "--cache-root",
                        str(root / "cache"),
                        "--output-json",
                        str(report),
                    ],
                ),
            ):
                exit_code = reranker.main()
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(report.read_text())["pairs"], 10)


class CostAndResponseTests(unittest.TestCase):
    def test_cost_cap_fails_closed_before_first_cloud_request(self) -> None:
        calls = 0

        def transport(
            _url: str, _body: bytes, _headers: dict[str, str], _timeout: float
        ) -> reranker.HttpResult:
            nonlocal calls
            calls += 1
            return reranker.HttpResult(200, {}, _response([0.1]), 1)

        with tempfile.TemporaryDirectory() as raw_tmp:
            cache = reranker.EndpointEvidenceCache(Path(raw_tmp), transport=transport)
            with self.assertRaises(reranker.CostCapError):
                reranker.fetch_cloud_batches(
                    [reranker.canonical_payload(["q"], ["d"])],
                    cache=cache,
                    base_url="https://api.deepinfra.com",
                    api_key="secret",
                    timeout=1,
                    estimated_tokens=1_000_001,
                    max_estimated_tokens=1_000_000,
                    max_cost_usd=0.05,
                    cache_only=False,
                )
            self.assertEqual(calls, 0)
            self.assertEqual(list(Path(raw_tmp).iterdir()), [])

    def test_response_schema_cardinality_finiteness_and_public_domain_are_strict(
        self,
    ) -> None:
        valid = reranker.validate_response(_response([0.1, 0.8]), 2)
        self.assertEqual(valid.scores, (0.1, 0.8))
        self.assertEqual(valid.input_tokens, 17)

        invalid = (
            b"not json",
            b'{"scores":[0.1]}',
            b'{"scores":[0.1],"input_tokens":1,"extra":true}',
            b'{"scores":[0.1],"input_tokens":true}',
            b'{"scores":[true],"input_tokens":1}',
            b'{"scores":[NaN],"input_tokens":1}',
            b'{"scores":[Infinity],"input_tokens":1}',
            b'{"scores":[-0.000001,0.5],"input_tokens":1}',
            b'{"scores":[0.5,1.000001],"input_tokens":1}',
        )
        for body in invalid:
            with (
                self.subTest(body=body),
                self.assertRaises(reranker.ResponseValidationError),
            ):
                reranker.validate_response(body, 2)


class MetricsTests(unittest.TestCase):
    def test_rank_ties_and_quality_metrics_are_deterministic(self) -> None:
        self.assertEqual(reranker.rank_indices([0.5, 0.5, 0.4]), [0, 1, 2])
        rows = [_valid_row("en", "q-en"), _valid_row("zh", "q-zh")]
        rows[0]["candidates"][0]["relevance"] = 1
        rows[0]["candidates"][2]["relevance"] = 0
        rows[1]["candidates"][1]["relevance"] = 1
        rows[1]["candidates"][2]["relevance"] = 0
        with tempfile.TemporaryDirectory() as raw_tmp:
            corpus = Path(raw_tmp) / "corpus.jsonl"
            _write_jsonl(corpus, rows)
            groups = reranker.load_corpus(corpus)

        tied = [0.5] * 20
        cloud = reranker.compute_endpoint_metrics(groups, tied)
        self.assertAlmostEqual(cloud["aggregate"]["mrr_at_10"], 0.75)
        self.assertAlmostEqual(cloud["aggregate"]["map_at_10"], 0.75)
        self.assertAlmostEqual(cloud["per_language"]["en"]["mrr_at_10"], 1.0)
        self.assertAlmostEqual(cloud["per_language"]["zh"]["mrr_at_10"], 0.5)

        comparison = reranker.compute_comparison_metrics(groups, tied, tied)
        self.assertEqual(comparison["rank_correlation"]["mean_spearman"], 1.0)
        self.assertEqual(comparison["top_k_overlap"]["at_1"], 1.0)
        self.assertEqual(comparison["score_calibration"]["rmse"], 0.0)


class CorpusTests(unittest.TestCase):
    def test_corpus_validation_rejects_invalid_groups(self) -> None:
        invalid_rows = []
        short = _valid_row()
        short["candidates"] = short["candidates"][:-1]
        invalid_rows.append(short)
        no_positive = _valid_row(query_id="q-no-positive")
        for candidate in no_positive["candidates"]:
            candidate["relevance"] = 0
        invalid_rows.append(no_positive)
        duplicate = _valid_row(query_id="q-duplicate")
        duplicate["candidates"][1]["document_id"] = duplicate["candidates"][0][
            "document_id"
        ]
        invalid_rows.append(duplicate)
        wrong_language = _valid_row(query_id="q-language")
        wrong_language["candidates"][0]["source_language"] = "zh"
        invalid_rows.append(wrong_language)

        for index, row in enumerate(invalid_rows):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as raw_tmp:
                corpus = Path(raw_tmp) / "corpus.jsonl"
                _write_jsonl(corpus, [row])
                with self.assertRaises(reranker.CorpusValidationError):
                    reranker.load_corpus(corpus)

    def test_committed_public_corpus_and_provenance_are_exact(self) -> None:
        groups = reranker.load_corpus(CORPUS)
        self.assertEqual(len(groups), 200)
        self.assertEqual(
            {
                language: sum(g.source_language == language for g in groups)
                for language in ("en", "zh")
            },
            {"en": 100, "zh": 100},
        )
        self.assertTrue(all(len(group.candidates) == 10 for group in groups))
        self.assertTrue(
            all(
                any(candidate.relevance > 0 for candidate in group.candidates)
                for group in groups
            )
        )
        self.assertTrue(
            all(
                any(candidate.relevance == 0 for candidate in group.candidates)
                for group in groups
            )
        )

        metadata = json.loads(METADATA.read_text())
        self.assertEqual(metadata["dataset"], "mteb/MIRACLReranking")
        self.assertEqual(
            metadata["revision"], "ab6f54eff185a84bc1f6ab96b56bc7df87433228"
        )
        self.assertEqual(metadata["license"], "CC-BY-SA-4.0")
        self.assertEqual(metadata["split"], "dev")
        self.assertEqual(metadata["groups_per_language"], 100)
        self.assertEqual(metadata["candidates_per_group"], 10)
        sources = {row["path"]: row for row in metadata["source_files"]}
        self.assertEqual(
            {path: row["sha256"] for path, row in sources.items()}, SOURCE_SHA256
        )
        for path, row in sources.items():
            self.assertIn(metadata["revision"], row["url"])
            self.assertIn(path, row["url"])


if __name__ == "__main__":
    unittest.main()
