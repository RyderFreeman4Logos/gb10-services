#!/usr/bin/env python3
"""Compare version-pinned DeepInfra and local reranker endpoint evidence."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from reranker_equivalence_metrics import (
    Candidate,
    QueryGroup,
    build_batches,
    compute_comparison_metrics,
    compute_endpoint_metrics,
    estimate_input_tokens,
    load_corpus,
    rank_indices,
)
from reranker_equivalence_wire import (
    AmbiguousTransportError,
    CLOUD_ENDPOINT,
    CacheMissError,
    CacheStateError,
    CorpusValidationError,
    CostCapError,
    DEEPINFRA_MODEL_VERSION,
    ENDPOINT_PATH,
    LOCAL_ENDPOINT,
    EndpointEvidenceCache,
    EndpointHttpError,
    EndpointSpec,
    EquivalenceError,
    EvidenceError,
    EvidenceResponse,
    HttpResult,
    RequestIdentity,
    ResponseValidationError,
    ValidatedResponse,
    _atomic_write,
    _enforce_cost_cap,
    _estimated_cost,
    _json_bytes,
    _urllib_transport,
    canonical_payload,
    canonical_request_hash,
    endpoint_url,
    fetch_cloud_batches,
    fetch_endpoint_batches,
    load_cached_batches,
    request_identity,
    request_ledger_bytes,
    sanitize_response_headers,
    validate_response,
)


DEFAULT_CACHE_ROOT = Path(
    "/ssd/mirror-rootfs/home/obj/project/github/RyderFreeman4Logos/"
    "llm-guard-proxy/evaluation/deepinfra-qwen3-reranker-8b"
)
DEFAULT_CORPUS = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "reranker-equivalence"
    / "miracl-reranking-en-zh-dev.jsonl"
)
PRICE_USD_PER_MILLION_INPUT_TOKENS = 0.05


def _flatten_scores(responses: Sequence[ValidatedResponse]) -> list[float]:
    return [score for response in responses for score in response.scores]


def _field_sets(responses: Sequence[ValidatedResponse]) -> list[list[str]]:
    return [list(response.present_fields) for response in responses]


def _human_report(report: Mapping[str, object]) -> str:
    quality = report["quality"]
    comparison = report["cloud_vs_local"]
    cost = report["cost"]
    parity = report["api_schema_parity"]
    if not all(
        isinstance(section, dict) for section in (quality, comparison, cost, parity)
    ):
        raise ValueError("report sections are malformed")
    lines = [
        "RERANKER ENDPOINT EQUIVALENCE REPORT",
        (
            f"groups={report['groups']} pairs={report['pairs']} "
            f"languages={','.join(report['languages'])}"
        ),
        (
            "wire: target="
            f"{parity['endpoint_target']} byte_equivalent_requests="
            f"{str(parity['request_payloads_byte_equivalent']).lower()} "
            f"cloud_schema={str(parity['response_schema_valid']['cloud']).lower()} "
            f"local_schema={str(parity['response_schema_valid']['local']).lower()}"
        ),
        (
            f"cost: estimated_input_tokens_upper_bound="
            f"{cost['estimated_input_tokens_upper_bound']} "
            f"estimated_cost_usd={cost['estimated_cost_usd']:.8f} "
            f"actual_input_tokens={cost['actual_input_tokens']} "
            f"actual_cost_usd={cost['actual_cost_usd']:.8f}"
        ),
    ]
    for endpoint in ("cloud", "local"):
        metrics = quality[endpoint]["aggregate"]
        domain = quality[endpoint]["score_domain"]
        lines.append(
            f"{endpoint}: MRR@10={metrics['mrr_at_10']:.6f} "
            f"nDCG@10={metrics['ndcg_at_10']:.6f} "
            f"MAP@10={metrics['map_at_10']:.6f}"
        )
        lines.append(
            f"{endpoint} score domain: min={domain['min']:.8f} "
            f"max={domain['max']:.8f} mean={domain['mean']:.8f} "
            f"stddev={domain['standard_deviation']:.8f}"
        )
        for language, language_metrics in quality[endpoint]["per_language"].items():
            lines.append(
                f"  {endpoint}/{language}: "
                f"MRR@10={language_metrics['mrr_at_10']:.6f} "
                f"nDCG@10={language_metrics['ndcg_at_10']:.6f} "
                f"MAP@10={language_metrics['map_at_10']:.6f}"
            )
    rank = comparison["rank_correlation"]
    overlap = comparison["top_k_overlap"]
    calibration = comparison["score_calibration"]
    lines.append(
        f"cloud-vs-local: mean_spearman={rank['mean_spearman']:.6f} "
        f"min_spearman={rank['min_spearman']:.6f} "
        f"top1={overlap['at_1']:.6f} top3={overlap['at_3']:.6f} "
        f"top5={overlap['at_5']:.6f} top10={overlap['at_10']:.6f}"
    )
    lines.append(
        f"calibration: paired_pearson={calibration['paired_pearson']:.6f} "
        f"MAE={calibration['mean_absolute_error']:.8f} "
        f"RMSE={calibration['rmse']:.8f} "
        "mean_local_minus_cloud="
        f"{calibration['mean_difference_local_minus_cloud']:.8f}"
    )
    lines.append(
        "No quality PASS threshold is applied; endpoint quality is reported "
        "independently."
    )
    return "\n".join(lines) + "\n"


def _emit_report(
    report: dict[str, object], output_json: str, human_output: str | None
) -> None:
    human = _human_report(report)
    print(human, file=sys.stderr, end="")
    if human_output is not None:
        target = Path(human_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, human.encode("utf-8"))
    encoded = _json_bytes(report, pretty=True)
    if output_json == "-":
        sys.stdout.buffer.write(encoded)
    else:
        target = Path(output_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, encoded)


def _emit_preview(
    preview: dict[str, object], output_json: str, human_output: str | None
) -> None:
    human = (
        "RERANKER CLOUD COST PREVIEW\n"
        f"pairs={preview['pairs']} batches={preview['batches']}\n"
        "conservative_estimated_input_tokens="
        f"{preview['estimated_input_tokens_upper_bound']} "
        f"estimated_cost_usd={preview['estimated_cost_usd']:.8f}\n"
        f"within_hard_cap={str(preview['within_hard_cap']).lower()}\n"
        "No endpoint request was sent.\n"
    )
    print(human, file=sys.stderr, end="")
    if human_output is not None:
        target = Path(human_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, human.encode("utf-8"))
    encoded = _json_bytes(preview, pretty=True)
    if output_json == "-":
        sys.stdout.buffer.write(encoded)
    else:
        target = Path(output_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, encoded)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    # argparse delegates labels to gettext, whose locale discovery reads
    # os.environ.  Cache-only is a forensic offline mode, so suppress that
    # implicit locale lookup before even constructing the parser.
    effective_argv = tuple(sys.argv[1:] if argv is None else argv)
    offline_parse = "--cache-only" in effective_argv
    original_translate = argparse._
    if offline_parse:
        argparse._ = lambda message: message
    try:
        return _build_parser().parse_args(effective_argv)
    finally:
        if offline_parse:
            argparse._ = original_translate


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--cloud-base-url", default="https://api.deepinfra.com")
    parser.add_argument("--cloud-api-key-env", default="DEEPINFRA_KEY")
    parser.add_argument("--local-base-url", default="http://100.105.4.92:18014")
    parser.add_argument("--local-api-key-env")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--instruction")
    parser.add_argument("--service-tier")
    parser.add_argument("--max-estimated-input-tokens", type=int, default=1_000_000)
    parser.add_argument("--max-cloud-cost-usd", type=float, default=0.05)
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help=(
            "read complete cloud and local evidence only; perform no environment, "
            "credential, DNS, socket, URL, or transport setup"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-json", default="-")
    parser.add_argument("--human-output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        groups = load_corpus(args.corpus)
        payloads = build_batches(
            groups,
            args.batch_size,
            instruction=args.instruction,
            service_tier=args.service_tier,
        )
        estimated_tokens = estimate_input_tokens(groups, instruction=args.instruction)
        estimated_cost = _estimated_cost(
            estimated_tokens, PRICE_USD_PER_MILLION_INPUT_TOKENS
        )
        within_cap = (
            estimated_tokens <= args.max_estimated_input_tokens
            and estimated_cost <= args.max_cloud_cost_usd
        )
        if args.dry_run:
            preview: dict[str, object] = {
                "batches": len(payloads),
                "estimated_cost_usd": estimated_cost,
                "estimated_input_tokens_upper_bound": estimated_tokens,
                "max_cloud_cost_usd": args.max_cloud_cost_usd,
                "max_estimated_input_tokens": args.max_estimated_input_tokens,
                "pairs": sum(len(group.candidates) for group in groups),
                "schema": "reranker-equivalence-cost-preview-v1",
                "within_hard_cap": within_cap,
            }
            _emit_preview(preview, args.output_json, args.human_output)
            return 0

        cloud_identities = [
            request_identity(payload, CLOUD_ENDPOINT) for payload in payloads
        ]
        local_identities = [
            request_identity(payload, LOCAL_ENDPOINT) for payload in payloads
        ]
        if args.cache_only:
            offline_cache = EndpointEvidenceCache(args.cache_root)
            cloud = load_cached_batches(cloud_identities, cache=offline_cache)
            local = load_cached_batches(local_identities, cache=offline_cache)
        else:
            cloud_key = os.environ.get(args.cloud_api_key_env, "")
            if not cloud_key:
                raise EquivalenceError(
                    "cloud API key environment variable is unset: "
                    f"{args.cloud_api_key_env}"
                )
            local_key = (
                os.environ.get(args.local_api_key_env, "")
                if args.local_api_key_env
                else ""
            )
            if args.local_api_key_env and not local_key:
                raise EquivalenceError(
                    "local API key environment variable is unset: "
                    f"{args.local_api_key_env}"
                )
            live_cache = EndpointEvidenceCache(
                args.cache_root, transport=_urllib_transport
            )
            _enforce_cost_cap(
                estimated_tokens,
                args.max_estimated_input_tokens,
                args.max_cloud_cost_usd,
                PRICE_USD_PER_MILLION_INPUT_TOKENS,
            )
            cloud = fetch_endpoint_batches(
                cloud_identities,
                cache=live_cache,
                base_url=args.cloud_base_url,
                api_key=cloud_key,
                timeout=args.timeout,
            )
            local = fetch_endpoint_batches(
                local_identities,
                cache=live_cache,
                base_url=args.local_base_url,
                api_key=local_key,
                timeout=args.timeout,
            )
        cloud_scores = _flatten_scores(cloud)
        local_scores = _flatten_scores(local)
        actual_tokens = sum(response.input_tokens for response in cloud)
        report: dict[str, object] = {
            "api_schema_parity": {
                "cloud_response_fields_by_batch": _field_sets(cloud),
                "endpoint_target": cloud_identities[0].path_and_query,
                "local_response_fields_by_batch": _field_sets(local),
                "request_payload_sha256": [
                    hashlib.sha256(payload).hexdigest() for payload in payloads
                ],
                "request_payloads_byte_equivalent": all(
                    cloud_identity.request_body == local_identity.request_body
                    and cloud_identity.path_and_query == local_identity.path_and_query
                    for cloud_identity, local_identity in zip(
                        cloud_identities, local_identities, strict=True
                    )
                ),
                "response_schema_valid": {"cloud": True, "local": True},
            },
            "cloud_vs_local": compute_comparison_metrics(
                groups, cloud_scores, local_scores
            ),
            "cost": {
                "actual_cost_usd": _estimated_cost(
                    actual_tokens, PRICE_USD_PER_MILLION_INPUT_TOKENS
                ),
                "actual_input_tokens": actual_tokens,
                "estimated_cost_usd": estimated_cost,
                "estimated_input_tokens_upper_bound": estimated_tokens,
                "price_usd_per_million_input_tokens": (
                    PRICE_USD_PER_MILLION_INPUT_TOKENS
                ),
            },
            "groups": len(groups),
            "languages": sorted({group.source_language for group in groups}),
            "pairs": len(cloud_scores),
            "quality": {
                "cloud": compute_endpoint_metrics(groups, cloud_scores),
                "local": compute_endpoint_metrics(groups, local_scores),
            },
            "request_identity": {
                "cloud": cloud_identities[0].record(),
                "local": local_identities[0].record(),
            },
            "schema": "reranker-endpoint-equivalence-report-v2",
        }
        _emit_report(report, args.output_json, args.human_output)
        return 0
    except (EquivalenceError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "AmbiguousTransportError",
    "CLOUD_ENDPOINT",
    "CacheMissError",
    "CacheStateError",
    "Candidate",
    "CorpusValidationError",
    "CostCapError",
    "DEEPINFRA_MODEL_VERSION",
    "ENDPOINT_PATH",
    "EndpointEvidenceCache",
    "EndpointHttpError",
    "EndpointSpec",
    "EquivalenceError",
    "EvidenceError",
    "EvidenceResponse",
    "HttpResult",
    "LOCAL_ENDPOINT",
    "QueryGroup",
    "RequestIdentity",
    "ResponseValidationError",
    "ValidatedResponse",
    "build_batches",
    "canonical_payload",
    "canonical_request_hash",
    "compute_comparison_metrics",
    "compute_endpoint_metrics",
    "endpoint_url",
    "estimate_input_tokens",
    "fetch_cloud_batches",
    "fetch_endpoint_batches",
    "load_cached_batches",
    "load_corpus",
    "main",
    "rank_indices",
    "request_identity",
    "request_ledger_bytes",
    "sanitize_response_headers",
    "validate_response",
]


if __name__ == "__main__":
    raise SystemExit(main())
