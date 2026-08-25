# SGLang lean-digest exclusive 16-graph cutover — 2026-08-21

- **Evidence cut:** 2026-08-21 ~06:25 PDT
- **Question:** Can Qwen3.8-27B NVFP4 + SGLang on GB10 serve Hermes `auxiliary.compression` without dropping GDN/mamba `float32`, hard context `262144`, or KV `fp8_e4m3`, while raising concurrency so aggregate decode overlap shortens lean-digest wall-clock?
- **Decision status:** Exclusive 16-graph / 32k-prefill generation is live and inference-ready. The compression-shaped `c=1…16` ladder on this generation has not finished. Optimum concurrency is **not** claimed.
- **Live change in this documentation transaction:** None. The exclusive cutover already happened earlier the same morning; this note records it.

## Executive result

Co-resident embedding/reranker plus decode CUDA graphs capped at batch 4 made lean-digest concurrency look decode-starved. That was a scheduler/workspace limit, not a proof that float32 GDN is an order of magnitude slower than MiaAI's short-prompt decode chart.

Stopping embedding and reranker, keeping the quality contract, and sizing decode graphs / torch.compile / prefill to 16-way recovered a KV pool of **1,166,643** tokens (above the 262144 floor that 16-way + 0.90 still missed while co-resident). Chat healthcheck then returned `OK` with 2 tokens. The exclusive compression-shaped ladder is the measurement that can decide `auxiliary.compression.max_concurrency`.

This note is the running diary for a later `gb10-services` release. That release should wait until one concurrency minimizes measured digest-map wall-clock on this exclusive generation, and must cite the Hermes lean parallel-digest patches below. It must not treat MiaAI 227.6 tok/s or a 64K cold-prefill receipt as the compression answer.

## Proven

1. Hermes lean compression digests ~72,000-character chunks (~18k tokens) with `max_tokens=1400`. The first chunk is a serial route probe; remaining sibling digests use `auxiliary.compression.max_concurrency`.
2. The co-resident 16-way + `mem-fraction-static 0.90` generation allocated `max_total_num_tokens=205216` (below 262144). Graph/compile max batch was 4; effective `max_prefill_tokens=16384` was below one lean chunk.
3. The co-resident 18k/1400 ladder was SIGTERM'd at **47/80 waves, 245/680 requests**, 0 failures / 0 retries. Artifacts remain at `/home/obj/sglang-lean-digest-run-2026-08-21` and must not be overwritten.
4. Source commit `0b3ebaa7adea8e1836be9a50db5246495777d7f1` (`Raise SGLang decode graphs and prefill step to 16-way`) set `--cuda-graph-max-bs-decode 16`, `--torch-compile-max-bs 16`, and `--max-prefill-tokens 32768`. Quality flags were unchanged: GDN `float32`, context `262144`, KV `fp8_e4m3`, admission 16, mem-fraction 0.90, Docker/systemd envelope 69g.
5. After stop of `vllm-embedding.service` and `vllm-querit-4b-reranker.service`, exclusive SGLang PID `1635702` / container `5c8721fb7731` allocated KV `#tokens: 1166643` (K/V 17.80 GiB + 5.56 GiB reported on two allocator lines). Raw `/v1/models` served `abliterated-qwen-latest-27b-nvfp4` with `max_model_len=262144`.
6. `python3 ../cn-llm-censor-research/scripts/healthcheck_vllm.py --wait 1700 chat` reached `OK  model=abliterated-qwen-latest-27b-nvfp4  tokens=2` after Guard `502` while compile/graph capture was still running. Guard (`llm-guard-proxy.service`, PID `539614`) stayed up; the ladder uses raw `:18010`, not Guard and not the localrouter fallback.
7. Live argv on PID `1635702` includes `--cpuset-cpus 5-8,15-18`, `--max-prefill-tokens 32768`, `--torch-compile-max-bs 16`, `--cuda-graph-max-bs-decode 16`. Docker `HostConfig.CpusetCpus` was still empty at inspect time: the flag is on the `docker run` argv, but the live HostConfig field is not proof that cgroup cpuset attached.
8. Focused profile tests were 29/29 before the commit; hook-enabled commit reported `Ran 684 tests in 630.112s`, `OK (skipped=1)`.

## High-confidence

1. The co-resident ladder's weak `c` scaling was prefill-queued and graph-capped: ~18k prompt, c=1 TTFT ~13.7s, decode graphs only to bs=4, KV 205216 unable to hold 16×18k (~288k) active, EOS early-stop shrinking overlap. It is not the same workload as MiaAI's short-prompt 512-token decode chart (c=1 TTFT 127ms, c=16 aggregate 227.6 tok/s).
2. Exclusive UMA plus graph-bs=16 / prefill-32k is the no-quality-loss path that can actually create decode overlap for lean sibling digests. Single-stream latency is secondary.
3. Digest-map wall-clock is only part of a full compression. The later serial main summary still dominates recent real jobs (committed median ~11 min, p75 ~15 min, common 13–30 min). Raising digest `c` cannot collapse that serial tail by itself.
4. Host `MemAvailable` after exclusive ready was ~2.1 GiB with ~15.5 GiB swap free. Exclusive KV recovery spent the co-resident headroom; do not raise mem-fraction further on this generation.

## Not proven

1. Optimum `auxiliary.compression.max_concurrency` on the exclusive generation.
2. That graph-bs=16 plus 32k prefill will make aggregate tok/s rise monotonically through c=16.
3. That Docker cpuset `5-8,15-18` is actually enforced (`HostConfig.CpusetCpus=""`).
4. That KV 1,166,643 remains after embedding/reranker are started again.
5. That localrouter will keep compression on this SGLang backend (historical `400 unknown provider` still exists on that route).

## Workload and measurement contract

First-principles workload is Hermes lean compression, not MiaAI structural decode and not the old 64K cold-prefill receipt.

| Item | Value |
|---|---|
| Chunk input | ~18k tokens (`min_prompt_tokens=18000`) |
| Chunk output budget | `max_tokens=1400` |
| EOS | enabled (real compression); not `ignore_eos` |
| Ladder | `c=1…16`, 5 waves each, 80 waves / 680 requests |
| Endpoint | raw `http://100.105.4.92:18010/v1` |
| Metrics | TTFT/prefill, post-TTFT decode, wave wall, true overlapping aggregate decode |
| Digest ETA | first chunk serial + `ceil((N-1)/c)` sibling waves |
| Full compression | digest map **plus** serial main summary |

Theoretical chunk counts at ~18k/chunk, cap 28: 240k → 14, 300k → 17. Recent real jobs more often persist ~8 segments (median). Report both.

Co-resident baseline (graph-bs=4, KV 205216, incomplete at 47/80) is comparison-only. Do not mix epochs.

## Hermes lean parallel-digest patches

These are the public fork commits that make sibling lean digests concurrent. They are **not** on `NousResearch/hermes-agent` `main` at this cut. Cite them in the later release; do not imply upstream merge.

| Role | Commit | URL |
|---|---|---|
| Parallel lean `tail_mode` chunk digest map | `89c11eb6bb3478a7156f63a4ff39d0aa5fb2db98` | https://github.com/RyderFreeman4Logos/hermes-agent/commit/89c11eb6bb3478a7156f63a4ff39d0aa5fb2db98 |
| Harvest digest futures independently | `a0559a693f2b8f8486e3ad74cdf6da490badf94c` | https://github.com/RyderFreeman4Logos/hermes-agent/commit/a0559a693f2b8f8486e3ad74cdf6da490badf94c |
| Pin digest bound, isolation, serial first chunk | `6ba0ed259cd2e3a7e55c33e5aa8918401e550fcb` | https://github.com/RyderFreeman4Logos/hermes-agent/commit/6ba0ed259cd2e3a7e55c33e5aa8918401e550fcb |
| Reuse selected lean digest fallback route | `7396b574f1056c97aec9700519867b2c6ecbab40` | https://github.com/RyderFreeman4Logos/hermes-agent/commit/7396b574f1056c97aec9700519867b2c6ecbab40 |
| Honor per-candidate `max_concurrency` | `05a5113922b37dfe733348ff81d77ac0c2d316e2` | https://github.com/RyderFreeman4Logos/hermes-agent/commit/05a5113922b37dfe733348ff81d77ac0c2d316e2 |
| Topic branch (includes the above plus later aux-concurrency work) | `topic/163-aux-concurrency-20260821` @ `05a5113922` | https://github.com/RyderFreeman4Logos/hermes-agent/tree/topic/163-aux-concurrency-20260821 |

Live Hermes still had `auxiliary.compression.max_concurrency: 5` when this diary opened. That value is a starting point, not the exclusive-generation optimum.

## Source-first decision

Keep this exclusive generation until the `c=1…16` ladder finishes or host safety requires stop.

- Do not drop GDN `float32`, context `262144`, or KV `fp8_e4m3` to buy speed.
- Do not cut `max-running-requests` below 16: short inputs keep 16-way; long chunks may queue.
- Do not raise `mem-fraction-static` above 0.90 on this box.
- Do not restart embedding/reranker until the exclusive ladder completes or the operator asks.
- Do not hot-edit GB10 files; change `gb10-services`, commit, rsync.
- Rollback: stop this unit, rsync parent `edc44e7c153498121b9755e822ce491b9e6260b5` (16-way + 0.90, graph-bs=4, no `--max-prefill-tokens`), start `--no-block`. Re-start embedding/reranker only after text is ready if co-residency is required.

Future canary for a release: same 18k/1400 ladder on the frozen exclusive argv, 0 failures, and a concurrency whose digest-map wall-clock is the minimum with non-decreasing overlapping aggregate decode through at least that `c`. Publish digest-map minutes for 240k/300k **and** state that full compression still includes the serial main summary.

## Immutable live generation at the cut

| Field | Value |
|---|---|
| Source HEAD | `0b3ebaa7adea8e1836be9a50db5246495777d7f1` |
| Parent | `edc44e7c153498121b9755e822ce491b9e6260b5` |
| Unit | `sglang-qwen38-27b.service` |
| MainPID | `1635702` |
| NRestarts | `0` |
| Container | `5c8721fb773164256c95d8ed10c324db20098a90665a65e83cbbbfe9f966cd2f` |
| Started | `2026-08-21T13:08:33.689575673Z` |
| Envelope | `--memory 69g` / `--memory-swap 69g` |
| Quality | GDN float32, context 262144, KV fp8_e4m3, DFlash, 16 running requests, 80 mamba slots |
| Scheduler experiment | graph/compile 16, `max-prefill-tokens` 32768 |
| KV tokens | 1166643 |
| Chat healthcheck | OK, 2 tokens |
| Embedding / reranker | inactive/dead |
| Guard | active, PID `539614` |
| Host MemAvailable at ready census | 2197996 kB |

## Running log

- 2026-08-21 morning: co-resident 16-way + 0.90 came up with KV 205216. Compression-shaped ladder started at `/home/obj/sglang-lean-digest-run-2026-08-21`.
- Same morning, incomplete baseline at 47/80: c=1 wall ~18s / TTFT ~14s / post-TTFT ~36 tok/s; higher `c` grew TTFT roughly linearly and did not raise overlapping decode like the MiaAI short-prompt chart.
- Operator authorized exclusive text: stop embedding/reranker, raise graph/compile to 16, set `max-prefill-tokens=32768`, rerun the ladder. Quality contract unchanged.
- Source unit + tests updated; focused 29/29; hook commit `0b3ebaa`.
- Exclusive start PID `1635702`. KV 1,166,643 before HTTP ready. Graph/compile capture delayed `/v1` until healthcheck `OK`.
- 2026-08-21 06:25 PDT: this diary opened. Exclusive 18k/1400 ladder launched in `/home/obj/sglang-lean-digest-exclusive-16graph-2026-08-21` (harness PID `2309814`, start_time `49339857`, config_epoch `03d4c5fe1ab27f597f999eb3130c2e69a0b6ac0cfad9dc56e2f1edb2df22301a`). Baseline artifacts stay. Host `MemAvailable` at launch ~1.18 GiB.
- 2026-08-21 06:41 PDT: operator requested descending `c` (warmup + fail-fast). Ascending exclusive harness SIGTERM'd at 19/80 (`c≈4`), artifacts kept. New run `/home/obj/sglang-lean-digest-exclusive-16to1-2026-08-21` (PID `2695830`, start_time `49420471`, config_epoch `8c9dc7e7db97bd3fa33ac7b3a845d5aae1c933baf8d7a51f54d52e9967bacb06`) starts at `c=16`. Same SGLang PID `1635702`. `MemAvailable` ~0.68 GiB at launch.

Do not paste prompts, completions, SSE, headers, or credentials into this file. Aggregate token/timing/status only.
