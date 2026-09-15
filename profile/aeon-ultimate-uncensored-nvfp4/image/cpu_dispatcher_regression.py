#!/usr/bin/env python3
"""CPU-only mixed-dispatcher regression. No model load, no GPU, no engine claim."""
from __future__ import annotations

import os
import sys
import traceback
from types import SimpleNamespace

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("VLLM_SKIP_CUDA_CHECK", "1")
os.environ.setdefault("VLLM_NO_USAGE_STATS", "1")

import inspect

import torch
from transformers import Qwen3Config

from vllm.config import set_current_vllm_config
from vllm.config.compilation import CompilationConfig
from vllm.model_executor.layers import linear as linear_mod
from vllm.model_executor.layers.attention import attention as attention_mod
from vllm.model_executor.layers.linear import LinearBase, UnquantizedLinearMethod
from vllm.model_executor.layers.quantization import modelopt as mo
from vllm.model_executor import parameter as parameter_mod
from vllm.config import vllm as vc
from vllm.model_executor.models import qwen3_dflash as dflash
from vllm.model_executor.models import qwen3_dflash2 as dflash2
from vllm.v1.attention.backends import triton_attn


class DummyLinear(LinearBase):
    def __init__(self) -> None:
        # Bypass LinearBase.__init__ (would recurse into get_quant_method).
        super(LinearBase, self).__init__()
        self.input_size = 8
        self.output_size = 8
        self.quant_config = None
        self.prefix = ""


def _mixed_cfg():
    return mo.ModelOptMixedPrecisionConfig._from_config(
        quant_method="MIXED_PRECISION",
        kv_cache_quant_method=None,
        exclude_modules=[],
        original_config={
            "quantized_layers": {
                "model.layers.0.self_attn.in_proj_qkv": {
                    "quant_algo": "FP8_PER_CHANNEL_PER_TOKEN"
                },
                "model.layers.0.self_attn.in_proj_z": {
                    "quant_algo": "FP8_PER_CHANNEL_PER_TOKEN"
                },
                "model.layers.0.self_attn.q_proj": {"quant_algo": "FP8"},
                "model.layers.0.mlp.gate_proj": {
                    "quant_algo": "NVFP4",
                    "group_size": 16,
                },
            }
        },
        group_size=16,
    )


def _dflash2_v2_path() -> dict:
    draft = SimpleNamespace(
        architectures=["DFlash2DraftModel"],
        hf_config=SimpleNamespace(layer_types=[]),
    )
    spec = SimpleNamespace(
        method="dflash",
        draft_model_config=draft,
        enable_adaptive_verification=False,
        parallel_drafting=False,
    )
    obj = vc.VllmConfig.__new__(vc.VllmConfig)
    obj.speculative_config = spec
    is_dflash2 = vc.VllmConfig._is_dflash2_draft(obj)

    dummy = vc.VllmConfig.__new__(vc.VllmConfig)
    dummy.speculative_config = spec
    dummy.model_config = None
    dummy.parallel_config = SimpleNamespace(
        prefill_context_parallel_size=1, enable_batch_sharded_sampling=False
    )
    v1 = vc.VllmConfig._get_v1_model_runner_unsupported_features(dummy)

    v2_ns = SimpleNamespace(
        compilation_config=SimpleNamespace(
            mode=None, pass_config=SimpleNamespace(enable_sp=False)
        ),
        parallel_config=SimpleNamespace(
            tensor_parallel_size=1,
            distributed_executor_backend=None,
            pipeline_parallel_size=1,
            enable_dbo=False,
            enable_elastic_ep=False,
        ),
        speculative_config=spec,
        model_config=None,
        cache_config=SimpleNamespace(kv_sharing_fast_prefill=False),
    )
    v2 = vc.VllmConfig._get_v2_model_runner_unsupported_features(v2_ns)
    return {
        "is_dflash2_draft": bool(is_dflash2),
        "v1_lists_dflash2": "dflash2 drafts" in v1,
        "v2_lists_dflash2": "dflash2 drafts" in list(v2 or []),
        "v1_unsupported": list(v1),
    }


def _dflash2_layer_type_forward() -> dict:
    cls = dflash2.DFlash2Qwen3DecoderLayer
    sig = inspect.signature(cls.__init__)
    params = sig.parameters
    has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    captured: dict[str, object] = {}
    orig = dflash.DFlashQwen3DecoderLayer.__init__

    def spy(self, *args: object, **kwargs: object) -> None:
        captured["kwargs"] = dict(kwargs)

    dflash.DFlashQwen3DecoderLayer.__init__ = spy  # type: ignore[method-assign]
    unexpected_rejected = False
    try:
        obj = cls.__new__(cls)
        try:
            cls.__init__(
                obj,
                object(),
                config=object(),
                layer_idx=0,
                cache_config=None,
                quant_config=None,
                layer_type="sliding_attention",
                prefix="model.layers.0",
            )
        except Exception:
            pass
        try:
            cls.__init__(
                obj,
                object(),
                config=object(),
                layer_idx=0,
                unexpected_kw=1,
            )
        except TypeError as error:
            unexpected_rejected = "unexpected_kw" in str(error)
    finally:
        dflash.DFlashQwen3DecoderLayer.__init__ = orig  # type: ignore[method-assign]
    forwarded = captured.get("kwargs")
    forwarded_type = forwarded.get("layer_type") if isinstance(forwarded, dict) else None
    return {
        "has_layer_type": "layer_type" in params,
        "layer_type_default": params["layer_type"].default if "layer_type" in params else None,
        "has_var_kw": has_var_kw,
        "captured": forwarded_type,
        "unexpected_rejected": unexpected_rejected,
    }


class CPUHardwareBoundary:
    """Only the unavailable device/backend-selection boundary is stubbed."""

    @staticmethod
    def is_cuda() -> bool:
        return False

    @staticmethod
    def is_xpu() -> bool:
        return False

    @staticmethod
    def opaque_attention_op() -> bool:
        return False

    @staticmethod
    def fp8_dtype() -> torch.dtype:
        return torch.float8_e4m3fn

    @staticmethod
    def make_synced_weight_loader(loader):
        return loader


def _dflash2_triton_constructor() -> dict:
    """Real Attention -> Triton constructor. CPU tests do not prove GPU kernels."""
    attention_mod.current_platform = CPUHardwareBoundary()
    triton_attn.current_platform = CPUHardwareBoundary()
    attention_mod.get_attn_backend = lambda *args, **kwargs: triton_attn.TritonAttentionBackend
    dflash.get_tensor_model_parallel_world_size = lambda: 1
    dflash.get_tensor_model_parallel_rank = lambda: 0
    linear_mod.get_tensor_model_parallel_world_size = lambda: 1
    linear_mod.get_tensor_model_parallel_rank = lambda: 0
    parameter_mod.get_tensor_model_parallel_world_size = lambda: 1
    parameter_mod.get_tensor_model_parallel_rank = lambda: 0
    parameter_mod.current_platform = CPUHardwareBoundary()

    runtime = SimpleNamespace(
        model_config=SimpleNamespace(dtype=torch.bfloat16, is_mm_prefix_lm=False),
        attention_config=SimpleNamespace(flex_attn_block_m=None, flex_attn_block_n=None),
        compilation_config=CompilationConfig(custom_ops=["none"]),
        kernel_config=SimpleNamespace(linear_backend="auto"),
    )
    attention_mod.get_current_vllm_config = lambda: runtime

    config = Qwen3Config(
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=5,
        num_attention_heads=4,
        num_key_value_heads=2,
        head_dim=8,
        max_position_embeddings=64,
        rms_norm_eps=1e-6,
        attention_bias=False,
        layer_types=["sliding_attention"] * 5,
        sliding_window=2048,
        is_causal=False,
    )
    config.rope_parameters = {"rope_theta": 10_000_000, "rope_type": "default"}
    config.layer_types = ["sliding_attention"] * 5
    config.sliding_window = 2048
    config.is_causal = False
    config.dflash_config = {
        "block_size": 8,
        "conv_group_size": 16,
        "conv_kernel_size": 2,
        "mask_token_id": 63,
        "selector_rank": 8,
        "selector_top_k": 16,
        "target_layer_ids": [5, 19, 33, 47, 61],
    }
    vcfg = SimpleNamespace(
        speculative_config=SimpleNamespace(num_speculative_tokens=7),
        model_config=SimpleNamespace(dtype=torch.bfloat16),
        cache_config=SimpleNamespace(block_size=16, skip_page_size_padded=None),
    )
    kwargs = dict(
        config=config,
        layer_idx=0,
        cache_config=None,
        quant_config=None,
        layer_type="sliding_attention",
        prefix="model.layers.64",
    )
    runtime.compilation_config.static_forward_context.clear()
    with set_current_vllm_config(runtime, check_compile=False):
        layer = dflash2.DFlash2Qwen3DecoderLayer(vcfg, **kwargs)
    attn = layer.self_attn.attn
    spec = attn.get_kv_cache_spec(vcfg)
    return {
        "impl": type(attn.impl).__name__,
        "impl_type": type(attn.impl),
        "layer_type": layer.layer_type,
        "attn_window": attn.sliding_window,
        "compute_window": attn.impl.sliding_window,
        "kv_spec": type(spec).__name__,
        "kv_block": spec.block_size,
        "query_block": layer.attention_conv.block_size,
        "use_mm_prefix": attn.use_mm_prefix,
        "resolved_causal": dflash._dflash_layer_causal(config, 0),
        "layer_causal": layer.self_attn.causal,
        "owns_init": "__init__" in dflash.DFlashAttention.__dict__,
    }


def _patch_current_vllm_config() -> None:
    import torch

    class FakeModelConfig:
        dtype = torch.bfloat16

    fake = SimpleNamespace(model_config=FakeModelConfig())

    def _fake():
        return fake

    import vllm.config as vllm_config

    vllm_config.get_current_vllm_config = _fake  # type: ignore[assignment]
    try:
        import vllm.model_executor.layers.quantization.modelopt as mo_mod

        mo_mod.get_current_vllm_config = _fake  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> int:
    expect_pcpt_dispatch = os.environ.get("EXPECT_PCPT_DISPATCH", "1") == "1"
    label = os.environ.get("IMAGE_LABEL", "unknown")
    try:
        _patch_current_vllm_config()
        cfg = _mixed_cfg()
        layer = DummyLinear()
        pcpt_algo = cfg._resolve_quant_algo("model.layers.0.self_attn.in_proj_qkv")
        fused_algo = cfg._resolve_quant_algo("model.layers.0.self_attn.in_proj_qkvz")
        nvfp4_algo = cfg._resolve_quant_algo("model.layers.0.mlp.gate_proj")
        # Instantiate only PcPt/unquant methods. NVFP4 LinearMethod needs CUDA kernels.
        pcpt = cfg.get_quant_method(layer, "model.layers.0.self_attn.in_proj_qkv")
        fused = cfg.get_quant_method(layer, "model.layers.0.self_attn.in_proj_qkvz")
        dflash = _dflash2_v2_path()
        layer_type = _dflash2_layer_type_forward()
        triton = _dflash2_triton_constructor()
        results = {
            "label": label,
            "expect_pcpt_dispatch": expect_pcpt_dispatch,
            "pcpt_type": type(pcpt).__name__,
            "fused_type": type(fused).__name__,
            "pcpt_algo": pcpt_algo,
            "fused_algo": fused_algo,
            "nvfp4_algo": nvfp4_algo,
            "pcpt_is_pcpt": isinstance(pcpt, mo.ModelOptFp8PcPtLinearMethod),
            "fused_is_pcpt": isinstance(fused, mo.ModelOptFp8PcPtLinearMethod),
            "pcpt_unquant": isinstance(pcpt, UnquantizedLinearMethod),
            "fused_unquant": isinstance(fused, UnquantizedLinearMethod),
            "nvfp4_ok": nvfp4_algo == "NVFP4",
            "dflash": dflash,
            "layer_type": layer_type,
            "triton": {
                "impl": triton["impl"],
                "layer_type": triton["layer_type"],
                "compute_window": triton["compute_window"],
                "kv_spec": triton["kv_spec"],
                "query_block": triton["query_block"],
                "use_mm_prefix": triton["use_mm_prefix"],
                "layer_causal": triton["layer_causal"],
            },
            "has_class_pcpt": hasattr(mo, "ModelOptFp8PcPtLinearMethod"),
        }
        checks = []
        if expect_pcpt_dispatch:
            checks.append(("pcpt_dispatch", results["pcpt_is_pcpt"]))
            checks.append(("fused_qkvz_dispatch", results["fused_is_pcpt"]))
            checks.append(("fused_algo", fused_algo == "FP8_PER_CHANNEL_PER_TOKEN"))
        else:
            checks.append(
                (
                    "pcpt_no_dispatch",
                    results["pcpt_unquant"] and not results["pcpt_is_pcpt"],
                )
            )
            checks.append(
                (
                    "fused_qkvz_unresolved",
                    fused_algo is None and results["fused_unquant"],
                )
            )
        checks.append(("nvfp4_still_dispatches", results["nvfp4_ok"]))
        checks.append(("dflash2_detected", dflash["is_dflash2_draft"] is True))
        checks.append(("v1_rejects_dflash2", dflash["v1_lists_dflash2"] is True))
        checks.append(
            ("v2_does_not_reject_dflash2", dflash["v2_lists_dflash2"] is False)
        )
        checks.append(("dflash2_accepts_layer_type", layer_type["has_layer_type"] is True))
        checks.append(
            ("dflash2_layer_type_default", layer_type["layer_type_default"] == "full_attention")
        )
        checks.append(("dflash2_no_var_kw", layer_type["has_var_kw"] is False))
        checks.append(
            ("dflash2_forwards_sliding_attention", layer_type["captured"] == "sliding_attention")
        )
        checks.append(
            ("dflash2_rejects_unknown_kw", layer_type["unexpected_rejected"] is True)
        )
        checks.append(("triton_impl", triton["impl_type"] is triton_attn.TritonAttentionImpl))
        checks.append(("sliding_compute_window", triton["compute_window"] == (2047, 0)))
        checks.append(("full_kv_spec", triton["kv_spec"] == "FullAttentionSpec"))
        checks.append(("query_block8", triton["query_block"] == 8))
        checks.append(("no_mm_prefix", triton["use_mm_prefix"] is False))
        checks.append(("resolved_causal_false", triton["resolved_causal"] is False))
        checks.append(("layer.self_attn.causal is False", triton["layer_causal"] is False))
        checks.append(("parent_init_deleted", triton["owns_init"] is False))
        results["checks"] = [(n, bool(v)) for n, v in checks]
        failed = [n for n, v in checks if not v]
        results["failed"] = failed
        results["ok"] = not failed
        print(results)
        if failed:
            print("FAIL", failed, file=sys.stderr)
            return 1
        print("PASS")
        return 0
    except Exception:
        traceback.print_exc()
        print({"label": label, "ok": False, "exception": True})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
