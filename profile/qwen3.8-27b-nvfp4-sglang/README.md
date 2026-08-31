# Qwen3.8-27B NVFP4 SGLang profile

Source-prepared next text backend, **not activated**. The live latest
profile alias is `profile/abliterated-qwen-latest-27b` → AEON Ultimate.
This directory is not that alias.

## Status

- Profile is source-prepared only. It is **not enabled or started**: live
  AEON/Guard stay on the current v0.26.0/v0.27.1 generation.
- Do not `systemctl enable/start` the unit here; a lifecycle cutover is a
  separate, authorized operation.
- Live Guard still reads `/home/obj/.config/llm-guard-proxy/config.toml`; this
  profile Guard config (`llm-guard-proxy/config.toml`) is not loaded by live
  Guard yet.

## Layout

- `sglang-qwen38-27b.service` — systemd user unit (DSpark, NVFP4, swap-impossible).
- `llm-guard-proxy/config.toml` — source-prepared default chat Guard config
  (admission 32; fill-if-absent sampling defaults; requires llm-guard-proxy #245).

## Unit contract

- Image: `lmsysorg/sglang@sha256:3c0abdf41ef22de9d7a859dc16ed71eae69452e36c91f071a25e60c85a6d1fc6`
  (`lmsysorg/sglang:qwen38-27b` tag). Not the deleted `:spark` image.
- Model: `/home/obj/models/RadixArk/Qwen3.8-27B-NVFP4` @ `554ebba9…`
  (NVFP4 W4A4; FP8 KV calibration scales honored via `--kv-cache-dtype auto`).
  Server path is the flat `hf --local-dir` mount `--model-path /models/qwen38-nvfp4`
  (no `snapshots/<sha>` subdir exists under a `--local-dir` install).
- Draft: DSPARK `/home/obj/models/RadixArk/Qwen3.8-27B-DSpark` @ `85ef153b…`,
  mounted read-only as the flat mount `--speculative-draft-model-path /models/qwen38-dspark`,
  `--speculative-dspark-block-size 7`,
  `--speculative-draft-model-quantization unquant`.
- MiaAI 2026-08-18 DSpark speed flags chase code-decode ~51 tok/s (chat ~23;
  long essay still prefers MTP): `--speculative-num-draft-tokens 8`,
  `--enable-torch-compile`, `--torch-compile-max-bs 4`,
  `--cuda-graph-max-bs-decode 4`, `--num-continuous-decode-steps 2`.
- `--mem-fraction-static 0.53` (operator contract, not MIAI's 0.95) on a
  121.63 GiB box: ~64.73 GiB reservation. Do **not** copy MIAI's
  `--mamba-full-memory-ratio 4.21` — it was tuned at 0.95 and is untrusted
  here. Authoritative pins: `--max-mamba-cache-size 32` (8×4 slots) and
  `--max-running-requests 8`.
- Swap-impossible: Docker `--memory 70g --memory-swap 70g --memory-swappiness 0`
  plus systemd `MemorySwapMax=0`. A cutover must keep peer hardcaps
  (embedding KV + querit) so the three-service sum stays ≤ ~114 GiB.
- `--served-model-name` includes `abliterated-qwen-latest-27b-nvfp4` (the
  stable public alias) and `qwen3.8-27b-sglang`.

## Guard contract

The profile Guard config's default chat path (`:18009`) is **fill-if-absent
passthrough**: public alias kept in `match_models`, `upstream_model` =
`abliterated-qwen-latest-27b-nvfp4`, thinking passthrough (not forced),
loop_guard disabled, no retry ladder that mutates thinking or sampling, and
no `thinking_token_budget` injection. Server and default-chat admission are
32/32. `[upstreams.param_override]` is enabled with `fill_if_absent = true`
and these defaults (caller-supplied fields win):

`temperature=1.0`, `top_p=0.95`, `top_k=20`, `min_p=0.0`,
`presence_penalty=0.0`, `repetition_penalty=1.0`, `reasoning_effort=medium`,
`thinking_budget=32768`, `max_tokens=50000` (non-CoT remainder 17232).

This file must not be copied onto live Guard until
[llm-guard-proxy#245](https://github.com/RyderFreeman4Logos/llm-guard-proxy/issues/245)
is deployed — current live Guard rejects unknown keys. Embedding/reranker
admission stays 8/64.
