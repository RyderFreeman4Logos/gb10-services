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

from vllm.model_executor.layers.linear import LinearBase, UnquantizedLinearMethod
from vllm.model_executor.layers.quantization import modelopt as mo
from vllm.config import vllm as vc


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
