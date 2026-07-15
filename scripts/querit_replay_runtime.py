#!/usr/bin/env python3
"""Local-only model runtime for the bounded Querit replay driver."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
import types
from pathlib import Path
from typing import Any, Sequence

from querit_replay_schema import (
    CANDIDATE_CONTRACT,
    LEGACY_CONTRACT,
    POSTPROCESSOR_TOKEN_ID,
    TERMINAL_ANCHOR_TOKEN_ID,
    attention_mask_sha256,
    normalized_f32_tensor_sha256,
    token_ids_sha256,
)
from querit_replay_sandbox import (
    SandboxError,
    attest_network_isolation,
    attest_running_system_python,
)
from querit_replay_trust import TrustError, attest_trusted_snapshot
from querit_score_contract import attest_head_load, attest_tokenizer, render_current_prompt


class RuntimeLoadError(RuntimeError):
    """The local snapshot or runtime could not be strictly attested."""


def _load_strict_single_cuda_model(
    *,
    model_class: Any,
    local: Path,
    config: Any,
    torch: Any,
    torch_dtype: Any,
    checkpoint_keys: set[str],
) -> tuple[Any, dict[str, Any], str]:
    """Load, attest, trim, and move the pinned model without Accelerate dispatch."""

    if not torch.cuda.is_available():
        raise RuntimeLoadError("CUDA is required for offline Querit replay")
    loaded = model_class.from_pretrained(
        str(local),
        config=config,
        torch_dtype=torch_dtype,
        local_files_only=True,
        trust_remote_code=False,
        use_lm_head=True,
        output_loading_info=True,
    )
    if not isinstance(loaded, tuple) or len(loaded) != 2:
        raise RuntimeLoadError("model loader did not return strict loading information")
    model, loading_info = loaded
    if not isinstance(loading_info, dict):
        raise RuntimeLoadError("model loader returned malformed model loading information")
    head = getattr(model, "head", None)
    weight = getattr(head, "weight", None)
    bias = getattr(head, "bias", None)
    if weight is None or bias is None:
        raise RuntimeLoadError("loaded model has no complete learned Querit head")
    attest_head_load(
        checkpoint_keys=checkpoint_keys,
        loading_info=loading_info,
        weight_shape=tuple(weight.shape),
        bias_shape=tuple(bias.shape),
    )
    # The generation head is required only while Transformers restores tied weights.
    # Drop it before the single CUDA transfer; the learned classifier head remains.
    if hasattr(model, "lm_head") and model.lm_head is not None:
        model.lm_head = None
    model.eval()
    model.to("cuda")
    return model, loading_info, "cuda"


def _local_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        info = absolute.lstat()
    except OSError as exc:
        raise RuntimeLoadError("local model snapshot does not exist") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeLoadError("local model snapshot must be a real directory")
    return absolute


def _read_regular_nofollow(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeLoadError(f"cannot open snapshot file without following links: {path.name}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
            raise RuntimeLoadError("snapshot file is non-regular or exceeds bound")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum_bytes:
            raise RuntimeLoadError("snapshot file exceeds bound while reading")
        return data
    finally:
        os.close(descriptor)


def _checkpoint_head_keys(snapshot: Path) -> set[str]:
    data = _read_regular_nofollow(
        snapshot / "model.safetensors.index.json", 16 * 1024 * 1024
    )
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise RuntimeLoadError(f"duplicate checkpoint-index key: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise RuntimeLoadError(f"nonfinite checkpoint-index constant: {value}")

    try:
        value = json.loads(
            data,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
        keys = set(value["weight_map"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeLoadError("checkpoint index is malformed") from exc
    return {key for key in keys if isinstance(key, str) and key.startswith("head.")}


def _module_version(name: str) -> str:
    module = sys.modules.get(name)
    return str(getattr(module, "__version__", "unknown"))


def _make_encoding_record(
    case: Any,
    track: str,
    input_ids: Sequence[int],
    pre_truncation_token_count: int,
) -> dict[str, Any]:
    if case.document is None:
        raise RuntimeLoadError("cannot encode unresolved boundary case")
    ids = list(input_ids)
    maximum = 40960 if track == LEGACY_CONTRACT else 40959
    expected_length = min(pre_truncation_token_count, maximum) + (
        1 if track == CANDIDATE_CONTRACT else 0
    )
    terminal = (
        POSTPROCESSOR_TOKEN_ID
        if track == LEGACY_CONTRACT
        else TERMINAL_ANCHOR_TOKEN_ID
    )
    if len(ids) != expected_length or not ids or ids[-1] != terminal:
        raise RuntimeLoadError("runtime encoding does not match score contract")
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


class LocalQueritRuntime:
    """Pinned local model adapter used only by the one-shot CLI."""

    def __init__(
        self,
        tokenizer: Any,
        model: Any,
        torch: Any,
        device: str,
        loading_info: dict[str, Any],
        snapshot_root: Path,
    ) -> None:
        self.tokenizer, self.model, self.torch, self.device = tokenizer, model, torch, device
        self.snapshot_root = snapshot_root
        self.classifier_load_report = loading_info
        weight = model.head.weight.detach().float().cpu().reshape(-1).tolist()
        bias = model.head.bias.detach().float().cpu().reshape(-1).tolist()
        self.head_attestation = {
            "bias_sha256": normalized_f32_tensor_sha256(bias, [2]),
            "bias_shape": [2],
            "loaded_dtype": str(model.head.weight.dtype).removeprefix("torch."),
            "normalized_dtype": "float32-le",
            "weight_sha256": normalized_f32_tensor_sha256(weight, [2, 2560]),
            "weight_shape": [2, 2560],
        }
        cuda_version = getattr(torch.version, "cuda", None)
        gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none"
        capability = (
            torch.cuda.get_device_capability(0) if torch.cuda.is_available() else None
        )
        self.runtime_identity = {
            "cuda": str(cuda_version),
            "gpu": gpu,
            "python": platform.python_version(),
            "pytorch": str(torch.__version__),
            "sm": ".".join(map(str, capability)) if capability else "none",
            "system_python": attest_running_system_python(),
            "tokenizer_class": type(tokenizer).__name__,
            "tokenizer_is_fast": bool(tokenizer.is_fast),
            "tokenizers": _module_version("tokenizers"),
            "transformers": _module_version("transformers"),
        }

    def encode(self, case: Any, track: str) -> dict[str, Any]:
        if case.document is None:
            raise RuntimeLoadError("cannot encode unresolved boundary case")
        prompt = render_current_prompt(case.query, case.document)
        full = self.tokenizer(
            prompt, add_special_tokens=True, padding=False, truncation=False
        )
        pre_count = len(full["input_ids"])
        maximum = 40960 if track == LEGACY_CONTRACT else 40959
        packed = self.tokenizer(
            prompt,
            add_special_tokens=True,
            padding=False,
            truncation=True,
            max_length=maximum,
        )
        ids = list(packed["input_ids"])
        if not ids or ids[-1] != POSTPROCESSOR_TOKEN_ID:
            raise RuntimeLoadError("pinned tokenizer postprocessor invariant failed")
        if track == CANDIDATE_CONTRACT:
            ids.append(TERMINAL_ANCHOR_TOKEN_ID)
        return _make_encoding_record(case, track, ids, pre_count)

    def infer(
        self,
        cases: list[Any],
        track: str,
        encodings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        torch = self.torch
        width = max(len(row["input_ids"]) for row in encodings)
        ids = [
            row["input_ids"]
            + [POSTPROCESSOR_TOKEN_ID] * (width - len(row["input_ids"]))
            for row in encodings
        ]
        masks = [
            [1] * len(row["input_ids"])
            + [0] * (width - len(row["input_ids"]))
            for row in encodings
        ]
        input_tensor = torch.tensor(ids, dtype=torch.long, device=self.device)
        mask_tensor = torch.tensor(masks, dtype=torch.long, device=self.device)
        with torch.inference_mode():
            hidden = self.model.model(
                input_ids=input_tensor, attention_mask=mask_tensor
            ).last_hidden_state
            positions = (
                [width - 1] * len(cases)
                if track == LEGACY_CONTRACT
                else [len(row["input_ids"]) - 1 for row in encodings]
            )
            selected = torch.stack(
                [hidden[index, position] for index, position in enumerate(positions)]
            )
            logits = self.model.head(selected)
            probabilities = torch.softmax(logits, dim=-1)
            native = probabilities[:, 1] - probabilities[:, 0]
            opaque_values = None
            if track == LEGACY_CONTRACT:
                opaque = self.model(input_ids=input_tensor, attention_mask=mask_tensor)
                values = opaque.score if hasattr(opaque, "score") else opaque["score"]
                opaque_values = values.detach().float().reshape(-1).cpu().tolist()
        logits_rows = logits.detach().float().cpu().tolist()
        probability_rows = probabilities.detach().float().cpu().tolist()
        score_rows = native.detach().float().cpu().tolist()
        results = []
        for index, encoding in enumerate(encodings):
            p0, p1 = probability_rows[index]
            selected_index = positions[index]
            padded_ids = ids[index]
            results.append(
                {
                    "legacy_opaque_score": (
                        opaque_values[index] if opaque_values else None
                    ),
                    "logits": logits_rows[index],
                    "native_score": score_rows[index],
                    "physical_last_id": padded_ids[-1],
                    "probabilities": probability_rows[index],
                    "recomputed_score": p1 - p0,
                    "selected_id": padded_ids[selected_index],
                    "selected_index": selected_index,
                    "width": width,
                }
            )
        return results


def load_local_runtime(snapshot: Path, dtype: str) -> LocalQueritRuntime:
    """Load only the exact local snapshot after proving OS-level isolation."""

    local = _local_directory(snapshot)
    try:
        attest_network_isolation()
        attest_running_system_python()
        attest_trusted_snapshot(local)
    except (SandboxError, TrustError) as exc:
        raise RuntimeLoadError("trusted isolated replay boundary is unavailable") from exc
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    try:
        import torch
        from transformers import AutoConfig, AutoTokenizer
    except ImportError as exc:
        raise RuntimeLoadError("local replay dependencies are unavailable") from exc
    tokenizer = AutoTokenizer.from_pretrained(
        str(local), local_files_only=True, trust_remote_code=False, use_fast=True
    )
    tokenizer.padding_side = "right"
    tokenizer.truncation_side = "right"
    attest_tokenizer(tokenizer)
    config = AutoConfig.from_pretrained(
        str(local), local_files_only=True, trust_remote_code=False
    )
    module_path = local / "modeling_querit_4b.py"
    module_source = _read_regular_nofollow(module_path, 4 * 1024 * 1024)
    try:
        module_code = compile(module_source, str(module_path), "exec")
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise RuntimeLoadError("pinned local Querit model source is invalid") from exc
    module = types.ModuleType("querit_4b_offline_replay")
    module.__file__ = str(module_path)
    sys.modules[module.__name__] = module
    exec(module_code, module.__dict__)
    model_class = getattr(module, "QueritModel", None)
    if model_class is None:
        raise RuntimeLoadError("pinned snapshot has no QueritModel")
    torch_dtype = torch.bfloat16 if dtype == "bfloat16" else torch.float16
    model, loading_info, device = _load_strict_single_cuda_model(
        model_class=model_class,
        local=local,
        config=config,
        torch=torch,
        torch_dtype=torch_dtype,
        checkpoint_keys=_checkpoint_head_keys(local),
    )
    return LocalQueritRuntime(tokenizer, model, torch, device, loading_info, local)


def reattest_loaded_runtime(
    runtime: LocalQueritRuntime, manifest: dict[str, Any]
) -> None:
    """Recompute live tokenizer/head/package identity before receipt authority."""

    if type(runtime) is not LocalQueritRuntime:
        raise RuntimeLoadError("PASS receipt requires the trusted local runtime type")
    attest_network_isolation()
    attest_tokenizer(runtime.tokenizer)
    attest_head_load(
        checkpoint_keys=_checkpoint_head_keys(runtime.snapshot_root),
        loading_info=runtime.classifier_load_report,
        weight_shape=tuple(runtime.model.head.weight.shape),
        bias_shape=tuple(runtime.model.head.bias.shape),
    )
    weight = runtime.model.head.weight.detach().float().cpu().reshape(-1).tolist()
    bias = runtime.model.head.bias.detach().float().cpu().reshape(-1).tolist()
    current_head = {
        "bias_sha256": normalized_f32_tensor_sha256(bias, [2]),
        "bias_shape": [2],
        "loaded_dtype": str(runtime.model.head.weight.dtype).removeprefix("torch."),
        "normalized_dtype": "float32-le",
        "weight_sha256": normalized_f32_tensor_sha256(weight, [2, 2560]),
        "weight_shape": [2, 2560],
    }
    cuda_version = getattr(runtime.torch.version, "cuda", None)
    gpu = (
        runtime.torch.cuda.get_device_name(0)
        if runtime.torch.cuda.is_available()
        else "none"
    )
    capability = (
        runtime.torch.cuda.get_device_capability(0)
        if runtime.torch.cuda.is_available()
        else None
    )
    current_runtime = {
        "cuda": str(cuda_version),
        "gpu": gpu,
        "python": platform.python_version(),
        "pytorch": str(runtime.torch.__version__),
        "sm": ".".join(map(str, capability)) if capability else "none",
        "snapshot_file_count": len(manifest["snapshot_ledger"]),
        "source_hashes": {
            row["path"]: row["sha256"] for row in manifest["source_ledger"]
        },
        "system_python": attest_running_system_python(),
        "tokenizer_class": type(runtime.tokenizer).__name__,
        "tokenizer_is_fast": bool(runtime.tokenizer.is_fast),
        "tokenizers": _module_version("tokenizers"),
        "transformers": _module_version("transformers"),
    }
    if current_head != manifest["head"] or current_runtime != manifest["runtime"]:
        raise RuntimeLoadError("live runtime/head re-attestation differs from manifest")
