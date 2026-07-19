#!/usr/bin/env python3
"""Convert a Querit-4B snapshot for vLLM sequence classification.

The conversion is intentionally in-place: point the CLI at a disposable copy of the
pinned Hugging Face snapshot, never at the cache-owned source snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import querit_vllm_artifact


__all__ = [
    "convert_snapshot",
    "main",
    "rewrite_config",
    "rewrite_head_state",
    "rewrite_weight_index",
]


SHARD_NAME = "model-00002-of-00002.safetensors"
INDEX_NAME = "model.safetensors.index.json"
CONFIG_NAME = "config.json"
TEMPLATE_NAME = "querit-rerank.jinja"
SOURCE_REVISION = querit_vllm_artifact.SOURCE_REVISION
DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "querit" / TEMPLATE_NAME
)

TensorLoader = Callable[[Path], tuple[dict[str, Any], dict[str, str] | None]]
TensorSaver = Callable[[dict[str, Any], Path, dict[str, str] | None], None]


def rewrite_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the vLLM scalar sequence-classification config."""

    converted = dict(config)
    converted.update(
        {
            "architectures": ["Qwen3ForSequenceClassification"],
            "num_labels": 1,
            "head_dtype": "model",
            "sbert_ce_default_activation_function": (
                "torch.nn.modules.activation.Tanh"
            ),
        }
    )
    return converted


def rewrite_weight_index(
    index: Mapping[str, Any], *, shard_name: str = SHARD_NAME
) -> dict[str, Any]:
    """Replace Querit's head keys in a safetensors index without other changes."""

    replacements = {"head.weight": "score.weight", "head.bias": "score.bias"}
    metadata = index.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("weight index has no mapping-valued metadata")
    total_size = metadata.get("total_size")
    if (
        isinstance(total_size, bool)
        or not isinstance(total_size, int)
        or total_size != querit_vllm_artifact.SOURCE_TOTAL_SIZE
    ):
        raise ValueError("weight index total_size is not the pinned source size")
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, Mapping):
        raise ValueError("weight index has no mapping-valued weight_map")
    if "score.weight" in weight_map or "score.bias" in weight_map:
        raise ValueError("pre-existing score key found in weight index")
    for key in replacements:
        if key not in weight_map:
            raise ValueError(f"missing {key} in weight index")
        if weight_map[key] != shard_name:
            raise ValueError(f"{key} shard must be {shard_name}, got {weight_map[key]}")
    converted_map = {
        replacements.get(key, key): value for key, value in weight_map.items()
    }
    converted = dict(index)
    converted_metadata = dict(metadata)
    converted_metadata["total_size"] = querit_vllm_artifact.OUTPUT_TOTAL_SIZE
    converted["metadata"] = converted_metadata
    converted["weight_map"] = converted_map
    return converted


def rewrite_head_state(
    state: Mapping[str, Any], *, bfloat16_dtype: object
) -> dict[str, Any]:
    """Return a copy with Querit's two-class head rewritten as a scalar Tanh head."""

    if "score.weight" in state or "score.bias" in state:
        raise ValueError("pre-existing score tensor found")
    try:
        head_weight = state["head.weight"]
        head_bias = state["head.bias"]
    except KeyError as error:
        raise ValueError(f"missing required tensor: {error.args[0]}") from error
    if tuple(head_weight.shape) != (2, 2560):
        raise ValueError(f"weight shape must be (2, 2560), got {head_weight.shape}")
    if tuple(head_bias.shape) != (2,):
        raise ValueError(f"bias shape must be (2,), got {head_bias.shape}")
    if head_weight.dtype != bfloat16_dtype:
        raise ValueError(f"weight dtype must be BF16, got {head_weight.dtype}")
    if head_bias.dtype != bfloat16_dtype:
        raise ValueError(f"bias dtype must be BF16, got {head_bias.dtype}")

    score_weight = ((head_weight[1:2] - head_weight[0:1]) / 2).contiguous()
    score_bias = ((head_bias[1:2] - head_bias[0:1]) / 2).contiguous()
    if tuple(score_weight.shape) != (1, 2560) or tuple(score_bias.shape) != (1,):
        raise ValueError("score shape changed unexpectedly during conversion")
    if score_weight.dtype != bfloat16_dtype or score_bias.dtype != bfloat16_dtype:
        raise ValueError("score dtype changed unexpectedly from BF16")

    converted = {
        key: tensor
        for key, tensor in state.items()
        if key not in {"head.weight", "head.bias"}
    }
    converted["score.weight"] = score_weight
    converted["score.bias"] = score_bias
    return converted


def _load_safetensors(path: Path) -> tuple[dict[str, Any], dict[str, str] | None]:
    try:
        from safetensors import safe_open
    except ImportError as error:
        raise RuntimeError(
            "safetensors is required for checkpoint conversion"
        ) from error

    with safe_open(str(path), framework="pt", device="cpu") as handle:
        state = {key: handle.get_tensor(key) for key in handle.keys()}
        metadata = handle.metadata()
    return state, metadata


def _save_safetensors(
    tensors: dict[str, Any], path: Path, metadata: dict[str, str] | None
) -> None:
    try:
        from safetensors.torch import save_file
    except ImportError as error:
        raise RuntimeError(
            "safetensors is required for checkpoint conversion"
        ) from error

    save_file(tensors, str(path), metadata=metadata)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON object from {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _new_stage_path(target: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=target.suffix, dir=target.parent
    )
    os.close(descriptor)
    return Path(raw_path)


def _target_mode(path: Path, *, default: int = 0o644) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return default


def _stage_bytes(target: Path, content: bytes) -> Path:
    stage = _new_stage_path(target)
    try:
        stage.write_bytes(content)
        stage.chmod(_target_mode(target))
    except BaseException:
        stage.unlink(missing_ok=True)
        raise
    return stage


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def convert_snapshot(
    snapshot_path: Path | str,
    *,
    template_path: Path | str = DEFAULT_TEMPLATE_PATH,
    load_shard: TensorLoader | None = None,
    save_shard: TensorSaver | None = None,
    bfloat16_dtype: object | None = None,
) -> dict[str, Any]:
    """Convert one copied snapshot in place and return a machine-readable receipt."""

    snapshot = Path(snapshot_path).expanduser().resolve(strict=True)
    if not snapshot.is_dir():
        raise ValueError(f"snapshot path is not a directory: {snapshot}")
    shard_path = snapshot / SHARD_NAME
    index_path = snapshot / INDEX_NAME
    config_path = snapshot / CONFIG_NAME
    for required in (shard_path, index_path, config_path):
        if not required.exists():
            raise ValueError(f"required checkpoint file is missing: {required}")

    source_template = Path(template_path).expanduser().resolve(strict=True)
    template_bytes = source_template.read_bytes()
    source_tree_sha256 = querit_vllm_artifact.attest_source_snapshot(snapshot)
    index = rewrite_weight_index(_load_json_object(index_path))
    config = rewrite_config(_load_json_object(config_path))

    loader = load_shard or _load_safetensors
    saver = save_shard or _save_safetensors
    if bfloat16_dtype is None:
        try:
            import torch
        except ImportError as error:
            raise RuntimeError(
                "PyTorch is required for BF16 checkpoint conversion"
            ) from error
        bfloat16_dtype = torch.bfloat16
    state, metadata = loader(shard_path)
    converted_state = rewrite_head_state(state, bfloat16_dtype=bfloat16_dtype)

    staged: list[tuple[Path, Path]] = []
    try:
        shard_stage = _new_stage_path(shard_path)
        staged.append((shard_path, shard_stage))
        saver(converted_state, shard_stage, metadata)
        shard_stage.chmod(_target_mode(shard_path))

        staged.extend(
            [
                (index_path, _stage_bytes(index_path, _json_bytes(index))),
                (config_path, _stage_bytes(config_path, _json_bytes(config))),
                (
                    snapshot / TEMPLATE_NAME,
                    _stage_bytes(snapshot / TEMPLATE_NAME, template_bytes),
                ),
            ]
        )
        for target, stage in staged:
            os.replace(stage, target)
    finally:
        for _, stage in staged:
            stage.unlink(missing_ok=True)

    score_weight = converted_state["score.weight"]
    score_bias = converted_state["score.bias"]
    querit_vllm_artifact.write_manifest(snapshot, source_tree_sha256=source_tree_sha256)
    return {
        "snapshot": str(snapshot),
        "shard": SHARD_NAME,
        "score_weight_shape": list(score_weight.shape),
        "score_bias_shape": list(score_bias.shape),
        "dtype": str(score_weight.dtype),
        "template": TEMPLATE_NAME,
        "artifact_manifest": querit_vllm_artifact.MANIFEST_NAME,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a copied Querit-4B snapshot for vLLM pooling"
    )
    parser.add_argument(
        "snapshot",
        type=Path,
        help="copied snapshot directory to convert in place",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE_PATH,
        help="tracked Querit rerank Jinja template",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    receipt = convert_snapshot(args.snapshot, template_path=args.template)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
