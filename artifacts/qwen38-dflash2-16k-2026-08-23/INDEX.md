# Qwen3.8-27B NVFP4 + DFlash2 K=8 16k unique-prompt decode-sum benchmark

Saved 2026-08-23 for later comparison after swapping the target to OrcaRouter NVFP4.

Endpoint at collection: `http://100.105.4.92:18010/v1`
Served model: `abliterated-qwen-latest-27b-nvfp4`
Target: `/home/obj/models/r0b0tlab/Qwen3.8-27B-NVFP4-MTP-sm121`
Draft: `/home/obj/models/z-lab/Qwen3.8-27B-DFlash2`
Constraints: 70G/no-swap, AEON_GPU_MEMORY_UTILIZATION=0.53, KV fp8, Mamba float32, max-model-len=262144, prefix cache off, DFlash2 K=8

Metric the user cares about: sum of per-stream decode tok/s (`completion_tokens / gen_s`), not wave-wall `agg_decode_tok_s`.

Valid 1-16 (from 16to1-b.json): best c=15 ≈ 104.18 tok/s
Valid 17-30 + isolated c=32: best c=27 = 128.48 tok/s
c=31 invalid: service status=137, 8/31 ok, 23 StreamTruncatedError. Do not compare.

Files:
- `qwen38-dflash2-18010-16to1-16k.toml` size=389 sha256=aa768224dbf2777f9329e3589b9357729bf46e92c3ec3cec835ee92bc8c3fb4e
- `qwen38-dflash2-18010-16to1-b.json` size=99351 sha256=721d8ad233d311ea9d3a2ed19658653accdc01e31fc2466e798812fbdf3097e2
- `qwen38-dflash2-18010-16to1-b.progress.yaml` size=350 sha256=1bedce2b7fca3d790410f76c2ac34b9f01a8fb826cee05ec19775a50384b84e6
- `qwen38-dflash2-18010-16to1-b.state.json` size=99638 sha256=46261754ceba1bb60c5124fa395b3ec3c92e1276bd246d199fffe90e86e1d336
- `qwen38-dflash2-18010-17to32-16k.toml` size=398 sha256=803d1341912db3a9c3e5ea125d59fbb81940e50db9661dbdcc26d75b56905593
- `qwen38-dflash2-18010-17to32.json` size=246080 sha256=1fc17ddafd4f83256615b7cbd52f637a748b584670523843b7c825b1cf5707c1
- `qwen38-dflash2-18010-17to32.progress.yaml` size=520 sha256=3e5a15d43acbeebbef9a12baf626bfec26663a4670d2f8d469475b34a767d6b0
- `qwen38-dflash2-18010-17to32.state.json` size=246427 sha256=a3fc3c631307a100a7a392811a71ebc46fa09ba3726ab1656922421c30588b7e
- `qwen38-dflash2-18010-c32-16k.toml` size=338 sha256=c10e0405f623c0c5448d3765ae17bcfd889bae693f54c7aa7795d16635b73f7b
- `qwen38-dflash2-18010-c32.json` size=22361 sha256=4ae9f568e30aa71dbc2083917b70b50fcdf148a07fa154f51ab209ca77e45fbb
- `qwen38-dflash2-18010-c32.progress.yaml` size=342 sha256=0a6f32f5349c6ad2eae053911ae47b517d7212f1e6d61a4b3c93410b58c91551
- `qwen38-dflash2-18010-c32.state.json` size=22420 sha256=8898c75eaff7a4f21a350e8a46a9b8b5d94e867d3a9b00d4da9c7b7fca35da63
