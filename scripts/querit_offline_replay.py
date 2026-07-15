#!/usr/bin/env python3
"""Bounded, deterministic, local-snapshot-only Querit dual replay.

This is a one-shot offline tool.  It opens no listener, has no network path, never
changes service selection, and writes only a private caller-selected output root.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from querit_replay_schema import (
    ARTIFACT_SCHEMA,
    CANDIDATE_CONTRACT,
    LEGACY_CONTRACT,
    POSTPROCESSOR_TOKEN_ID,
    TERMINAL_ANCHOR_TOKEN_ID,
    TRACK_DEFINITIONS,
    SchemaError,
    attention_mask_sha256,
    canonical_json_bytes,
    float32_cell,
    identity_sha256,
    seal_pass_receipt,
    sha256_bytes,
    token_ids_sha256,
    validate_artifact_set,
    verify_pass_receipt,
    write_artifact_set,
)
from querit_replay_sandbox import (
    SandboxError,
    attest_network_isolation,
    attest_running_system_python,
)
from querit_replay_trust import (
    MODEL_ID,
    PINNED_CONTAINER_IMAGE_DIGEST,
    PINNED_REVISION,
    TRUSTED_MODEL_LEDGER_SHA256,
    TrustError,
    attest_source_tree,
    attest_trusted_snapshot,
)
from querit_score_contract import render_current_prompt
from querit_replay_plan import (
    BOUNDARY_PRIMARY_ATOM,  # noqa: F401  (public replay/test API)
    ReplayCase,
    ReplayError,
    ScheduleBatch,
    construct_exact_boundary_document,  # noqa: F401  (public replay/test API)
    corpus_definition_sha256,
    corpus_definitions,  # noqa: F401  (public replay/test API)
    materialize_corpus,
    replay_schedule,
    schedule_observation_count,  # noqa: F401  (public replay/test API)
    schedule_sha256,
)
from querit_replay_validation import calibrate_tolerances, validate_replay_evidence


MAX_QUERY_CHARS = 4096
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOLERANCE_BOUNDS = {
    "logits": (1e-5, 5e-2),
    "probabilities": (1e-6, 5e-3),
    "score": (1e-6, 1e-2),
}


def prepare_run_directory(output_root: Path, run_id: str) -> Path:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ReplayError("run ID is invalid or could escape the output root")
    root = Path(os.path.abspath(output_root))
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_info = root.lstat()
    except OSError as exc:
        raise ReplayError("cannot create replay output root") from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ReplayError("output root must be a real directory")
    if root_info.st_uid != os.getuid() or root_info.st_mode & 0o077:
        raise ReplayError("output root must be owner-only")
    run = root / run_id
    try:
        run.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise ReplayError("run directory already exists or cannot be created") from exc
    return run


def require_local_snapshot(model_path: str) -> Path:
    if "://" in model_path:
        raise ReplayError("model must be a local snapshot path, not a URL")
    path = Path(os.path.abspath(model_path))
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReplayError("local model snapshot does not exist") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ReplayError("local model snapshot must be a real directory")
    return path


def _hash_regular_nofollow(path: Path, maximum_bytes: int) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReplayError(f"cannot open regular file without following links: {path.name}") from exc
    try:
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or info.st_uid != os.getuid()
            or before.st_uid != os.getuid()
            or info.st_nlink != 1
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != identity
            or info.st_size > maximum_bytes
        ):
            raise ReplayError("snapshot/source file is linked, replaced, or exceeds bound")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise ReplayError("snapshot/source file exceeds bound while reading")
            digest.update(chunk)
        after = path.lstat()
        if (
            (after.st_dev, after.st_ino) != identity
            or after.st_size != total
            or after.st_nlink != 1
            or stat.S_ISLNK(after.st_mode)
        ):
            raise ReplayError("snapshot/source file changed during hashing")
        return total, digest.hexdigest()
    finally:
        os.close(descriptor)


def snapshot_ledger(
    root: Path,
    *,
    maximum_files: int = 256,
    maximum_bytes: int = 32 * 1024 * 1024 * 1024,
) -> tuple[list[dict[str, Any]], str]:
    snapshot = require_local_snapshot(str(root))
    paths: list[Path] = []
    for directory, names, filenames in os.walk(snapshot, followlinks=False):
        directory_path = Path(directory)
        for name in names:
            if (directory_path / name).is_symlink():
                raise ReplayError("snapshot directory symlinks are forbidden")
        for filename in filenames:
            path = directory_path / filename
            if path.is_symlink():
                raise ReplayError("snapshot file symlinks are forbidden")
            paths.append(path)
    paths.sort(key=lambda path: path.relative_to(snapshot).as_posix())
    if not paths or len(paths) > maximum_files:
        raise ReplayError("snapshot file count is empty or exceeds bound")
    remaining = maximum_bytes
    ledger: list[dict[str, Any]] = []
    for path in paths:
        size, digest = _hash_regular_nofollow(path, remaining)
        remaining -= size
        ledger.append(
            {"path": path.relative_to(snapshot).as_posix(), "sha256": digest, "size": size}
        )
    tree_hash = sha256_bytes(
        canonical_json_bytes(ledger), domain=b"querit-snapshot-tree-v1\0"
    )
    return ledger, tree_hash


def calibrate_tolerance(*, jitter: float, batch_delta: float, scalar: str) -> float:
    if scalar not in _TOLERANCE_BOUNDS:
        raise ReplayError("unknown tolerance scalar")
    floor, cap = _TOLERANCE_BOUNDS[scalar]
    if not all(math.isfinite(value) and value >= 0 for value in (jitter, batch_delta)):
        raise ReplayError("tolerance inputs must be finite and nonnegative")
    value = max(floor, 8 * jitter, 4 * batch_delta)
    if value > cap:
        raise ReplayError(f"{scalar} tolerance exceeds hard cap")
    return value


def pairwise_rank_preserved(
    canonical: Sequence[float], observed: Sequence[float], tolerance: float
) -> bool:
    if len(canonical) != len(observed):
        return False
    for left in range(len(canonical)):
        for right in range(left + 1, len(canonical)):
            baseline_gap = canonical[left] - canonical[right]
            if abs(baseline_gap) <= 2 * tolerance:
                continue
            observed_gap = observed[left] - observed[right]
            if baseline_gap * observed_gap <= 0:
                return False
    return True


def make_encoding_record(
    *,
    case: ReplayCase,
    track: str,
    input_ids: Sequence[int],
    pre_truncation_token_count: int,
) -> dict[str, Any]:
    if case.document is None:
        raise ReplayError("cannot encode an unresolved boundary case")
    ids = list(input_ids)
    maximum = 40960 if track == LEGACY_CONTRACT else 40959
    expected_length = min(pre_truncation_token_count, maximum) + (
        1 if track == CANDIDATE_CONTRACT else 0
    )
    terminal = POSTPROCESSOR_TOKEN_ID if track == LEGACY_CONTRACT else TERMINAL_ANCHOR_TOKEN_ID
    if len(ids) != expected_length or not ids or ids[-1] != terminal:
        raise ReplayError("runtime encoding does not match score contract")
    prompt = render_current_prompt(case.query, case.document)
    prompt_bytes = prompt.encode("utf-8")
    mask = [1] * len(ids)
    return {
        "attention_length": len(mask),
        "attention_mask": mask,
        "attention_mask_sha256": attention_mask_sha256(mask),
        "candidate_reference_prefix_sha256": (
            token_ids_sha256(ids[:-1]) if track == CANDIDATE_CONTRACT else None
        ),
        "case_id": case.case_id,
        "dropped_right_count": max(0, pre_truncation_token_count - maximum),
        "encoding_id": f"{case.case_id}:{track}",
        "expected_terminal_id": terminal,
        "expected_terminal_string": (
            "<|im_end|>" if track == LEGACY_CONTRACT else "[CLS]"
        ),
        "input_ids": ids,
        "input_ids_sha256": token_ids_sha256(ids),
        "internal_anchor_count": (
            ids if track == LEGACY_CONTRACT else ids[:-1]
        ).count(TERMINAL_ANCHOR_TOKEN_ID),
        "last_real_id": ids[-1],
        "last_real_index": len(ids) - 1,
        "pre_truncation_token_count": pre_truncation_token_count,
        "prompt_utf8_length": len(prompt_bytes),
        "prompt_utf8_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "track": track,
    }


def _case_record(case: ReplayCase) -> dict[str, Any]:
    if case.document is None:
        raise ReplayError("corpus contains an unresolved boundary case")
    return {
        "case_id": case.case_id,
        "document": case.document,
        "group": case.group,
        "query": case.query,
        "target_prepack_tokens": case.target_prepack_tokens,
    }


def _finite_result(result: Mapping[str, Any]) -> None:
    values = [*result["logits"], *result["probabilities"], result["native_score"], result["recomputed_score"]]
    if any(not math.isfinite(float(value)) for value in values):
        raise ReplayError("runtime produced a nonfinite replay scalar")
    if any(not 0 <= float(value) <= 1 for value in result["probabilities"]):
        raise ReplayError("runtime probability is outside [0,1]")
    if not -1 <= float(result["native_score"]) <= 1:
        raise ReplayError("runtime score is outside [-1,1]")


def _execute_schedule(
    runtime: Any,
    cases: Sequence[ReplayCase],
    encodings: Mapping[tuple[str, str], dict[str, Any]],
    schedule: Sequence[ScheduleBatch],
) -> list[dict[str, Any]]:
    case_map = {case.case_id: case for case in cases}
    observations: list[dict[str, Any]] = []
    for batch in schedule:
        batch_cases = [case_map[case_id] for case_id in batch.case_ids]
        batch_encodings = [encodings[(case.case_id, batch.track)] for case in batch_cases]
        results = runtime.infer(batch_cases, batch.track, batch_encodings)
        if len(results) != len(batch_cases):
            raise ReplayError("runtime result count does not match batch")
        scores = [float(result["native_score"]) for result in results]
        sorted_rows = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
        ranks = {row: rank for rank, row in enumerate(sorted_rows)}
        width = max(len(record["input_ids"]) for record in batch_encodings)
        for row, (case, encoding, result) in enumerate(
            zip(batch_cases, batch_encodings, results, strict=True)
        ):
            _finite_result(result)
            if int(result["width"]) != width or int(result["selected_index"]) >= width:
                raise ReplayError("runtime selection metadata is inconsistent")
            observations.append(
                {
                    "batch_id": batch.batch_id,
                    "batch_size": len(batch_cases),
                    "batch_width": width,
                    "case_id": case.case_id,
                    "encoding_id": encoding["encoding_id"],
                    "finite_range_ok": True,
                    "legacy_opaque_score": (
                        float32_cell(result["legacy_opaque_score"])
                        if result.get("legacy_opaque_score") is not None
                        else None
                    ),
                    "logits": [float32_cell(value) for value in result["logits"]],
                    "native_score": float32_cell(result["native_score"]),
                    "observation_id": f"qro-{len(observations):04d}",
                    "original_index": row,
                    "padded_token_count": width - len(encoding["input_ids"]),
                    "permutation": batch.permutation,
                    "phase": batch.phase,
                    "physical_last_id": int(result["physical_last_id"]),
                    "physical_last_index": width - 1,
                    "probabilities": [float32_cell(value) for value in result["probabilities"]],
                    "recomputed_score": float32_cell(result["recomputed_score"]),
                    "repetition": batch.repetition,
                    "row_position": row,
                    "schedule_id": batch.batch_id,
                    "selected_id": int(result["selected_id"]),
                    "selected_index": int(result["selected_index"]),
                    "sorted_index": ranks[row],
                    "stable_rank": ranks[row],
                    "track": batch.track,
                }
            )
    if len(observations) != 680:
        raise ReplayError("runtime did not produce exactly 680 observations")
    return observations


def run_replay(
    runtime: Any,
    *,
    output_root: Path,
    run_id: str,
    identity: Mapping[str, Any],
    cases: Sequence[ReplayCase] | None = None,
) -> Path:
    run_dir = prepare_run_directory(output_root, run_id)
    try:
        materialized = list(cases) if cases is not None else materialize_corpus(runtime.tokenizer)
        if len(materialized) != 40:
            raise ReplayError("replay corpus must contain exactly 40 cases")
        schedule = replay_schedule()
        encoding_rows: list[dict[str, Any]] = []
        encoding_map: dict[tuple[str, str], dict[str, Any]] = {}
        for case in materialized:
            for track in (LEGACY_CONTRACT, CANDIDATE_CONTRACT):
                record = runtime.encode(case, track)
                if case.group == "L" and record["pre_truncation_token_count"] != case.target_prepack_tokens:
                    raise ReplayError("long boundary case token length is not exact")
                encoding_rows.append(record)
                encoding_map[(case.case_id, track)] = record
        observations = _execute_schedule(runtime, materialized, encoding_map, schedule)
        tolerances = calibrate_tolerances(observations)
        case_rows = [_case_record(case) for case in materialized]
        corpus_hash = sha256_bytes(
            canonical_json_bytes(case_rows), domain=b"querit-replay-corpus-v1\0"
        )
        evidence_manifest = {
            "classifier_load_report": getattr(runtime, "classifier_load_report", {}),
            "corpus_definition_sha256": corpus_definition_sha256(),
            "corpus_sha256": corpus_hash,
            "schedule_sha256": schedule_sha256(schedule),
            "tolerances": tolerances,
        }
        gates, errors = validate_replay_evidence(
            evidence_manifest, case_rows, encoding_rows, observations
        )
        status = (
            "SEMANTIC_PASS"
            if all(value == "PASS" for value in gates.values())
            else "FAIL"
        )
        manifest = {
            "classifier_load_report": getattr(runtime, "classifier_load_report", {}),
            "constants": {
                "anchor_token_id": TERMINAL_ANCHOR_TOKEN_ID,
                "max_model_length": 40960,
                "padding_side": "right",
                "postprocessor_token_id": POSTPROCESSOR_TOKEN_ID,
                "truncation_side": "right",
            },
            "corpus_sha256": corpus_hash,
            "corpus_definition_sha256": corpus_definition_sha256(),
            "gates": gates,
            "head": dict(runtime.head_attestation),
            "identity": dict(identity),
            "run_id": run_id,
            "runtime": dict(runtime.runtime_identity),
            "schedule_sha256": schedule_sha256(schedule),
            "schema": ARTIFACT_SCHEMA,
            "snapshot_ledger": list(runtime.snapshot_ledger),
            "source_ledger": list(runtime.source_ledger),
            "status": status,
            "tolerances": tolerances,
            "track_definitions": TRACK_DEFINITIONS,
            "tracks": [LEGACY_CONTRACT, CANDIDATE_CONTRACT],
        }
        write_artifact_set(
            run_dir,
            cases=case_rows,
            encodings=encoding_rows,
            observations=observations,
            errors=errors,
            manifest=manifest,
        )
        validate_artifact_set(
            run_dir, expected_identity=identity, require_receipt=False
        )
        if status != "SEMANTIC_PASS":
            raise ReplayError("replay gates failed; no PASS receipt was created")
        return run_dir
    except Exception:
        # Preserve private failure artifacts if already written; never delete evidence.
        raise


def load_local_runtime(snapshot: Path, dtype: str) -> Any:
    """Load the cohesive local-only runtime without importing ML dependencies early."""

    from querit_replay_runtime import RuntimeLoadError, load_local_runtime as load

    try:
        return load(snapshot, dtype)
    except RuntimeLoadError as exc:
        raise ReplayError(str(exc)) from exc



def _source_identity() -> tuple[list[dict[str, Any]], str, dict[str, str]]:
    try:
        ledger, tree_hash, hashes = attest_source_tree(Path(__file__).resolve().parent)
    except TrustError as exc:
        raise ReplayError(str(exc)) from exc
    return ledger, tree_hash, hashes


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline bounded Querit dual replay")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="execute one offline local-snapshot replay")
    run.add_argument("--model", required=True)
    run.add_argument("--output-root", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--dtype", choices=("bfloat16", "float16"), default="bfloat16")
    verify = subparsers.add_parser("verify-pass", help="verify a future selection receipt")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--manifest-sha256", required=True)
    verify.add_argument("--identity-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "verify-pass":
        receipt = verify_pass_receipt(
            Path(args.receipt),
            expected_manifest_sha256=args.manifest_sha256,
            expected_identity_sha256=args.identity_sha256,
        )
        print(canonical_json_bytes(receipt).decode("utf-8"))
        return 0
    try:
        attest_network_isolation()
        system_python = attest_running_system_python()
    except SandboxError as exc:
        raise ReplayError(str(exc)) from exc
    snapshot = require_local_snapshot(args.model)
    try:
        ledger, snapshot_hash = attest_trusted_snapshot(snapshot)
    except TrustError as exc:
        raise ReplayError(str(exc)) from exc
    source_ledger, source_tree_hash, source_hashes = _source_identity()
    runtime = load_local_runtime(snapshot, args.dtype)
    if runtime.runtime_identity.get("system_python") != system_python:
        raise ReplayError("loaded runtime system Python differs from launcher attestation")
    runtime.snapshot_ledger = ledger
    runtime.source_ledger = source_ledger
    runtime.runtime_identity["snapshot_file_count"] = len(ledger)
    runtime.runtime_identity["source_hashes"] = source_hashes
    runtime_hash = sha256_bytes(
        canonical_json_bytes(runtime.runtime_identity),
        domain=b"querit-runtime-identity-v1\0",
    )
    identity = {
        "container_image_digest": PINNED_CONTAINER_IMAGE_DIGEST,
        "model_id": MODEL_ID,
        "model_revision": PINNED_REVISION,
        "runtime_sha256": runtime_hash,
        "snapshot_tree_sha256": snapshot_hash,
        "source_tree_sha256": source_tree_hash,
        "system_python": system_python,
        "trusted_model_ledger_sha256": TRUSTED_MODEL_LEDGER_SHA256,
    }
    run_dir = run_replay(
        runtime,
        output_root=Path(args.output_root),
        run_id=args.run_id,
        identity=identity,
    )
    seal_pass_receipt(
        run_dir,
        snapshot_root=snapshot,
        source_root=Path(__file__).resolve().parent,
        runtime=runtime,
    )
    validate_artifact_set(run_dir, expected_identity=identity, require_receipt=True)
    _, manifest_hash = _hash_regular_nofollow(
        run_dir / "manifest.json", 4 * 1024 * 1024
    )
    print(
        canonical_json_bytes(
            {
                "identity_sha256": identity_sha256(identity),
                "manifest_sha256": manifest_hash,
                "run_id_sha256": sha256_bytes(
                    args.run_id.encode("utf-8"), domain=b"querit-run-id-v1\0"
                ),
                "status": "PASS",
            }
        ).decode("utf-8")
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReplayError, SchemaError) as error:
        print(f"querit replay failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
