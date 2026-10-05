# Native 1024 CPU canary (opt-in only)

This profile does **not** replace `qwen3-embedding-8b`, add a Guard route, install a
systemd unit, or change EverOS configuration/data. It exposes a temporary
OpenAI-compatible `/v1/embeddings` on GB10 **127.0.0.1:18016**; use an SSH tunnel
for local clients. No cloud fallback or GPU is used.

- Model: [`intfloat/multilingual-e5-large-instruct`](https://huggingface.co/intfloat/multilingual-e5-large-instruct/tree/274baa43b0e13e37fafa6428dbc7938e62e5c439), MIT,
  revision `274baa43b0e13e37fafa6428dbc7938e62e5c439`.
- Native XLM-R hidden size / mean-pooled output: **1024**. Normalize the complete
  vector; **no dimension slicing, projector, or Matryoshka override**.
- Weights SHA-256: `dd6b6e4f52db0a7aff83a13d10e6c5342ef9f6ab799bad3221f4b35ef390fa85`;
  cached safetensors size 1,119,825,680 bytes. Hash checked before load.
- Required existing optional runtime: Torch and Transformers in the pinned AEON
  image below. Missing dependencies are a blocker, never dummy embeddings.
- **512-token** native limit, versus incumbent 32K. Oversize requests are rejected,
  never silently truncated. One synchronous worker, at most two texts/request,
  32 KiB request body, bounded 600s lifetime (maximum 900s).
- As trained, retrieval **queries** need `Instruct: <task>\nQuery: <query>`;
  documents need no prefix. The API does not guess which inputs are queries.
  Existing consumers are not drop-in compatible solely because dimensions match.

## Bounded launch on GB10

From a committed source, checksum-copy only `scripts/native1024_canary.py` to a
fresh owner-only directory under GB10 `~/tmp`. Record source HEAD/tree/hash,
protected text/reranker/embedding container IDs, PIDs and start times before and
after. Confirm port 18016 and container name are unused. Do not copy production
configs, reload systemd, restart a model, or modify the HF cache.

Require **11 GiB MemAvailable** immediately before launch: 5 GiB candidate cap +
6 GiB host reserve. The server rejects inference/exits below 6 GiB host available.
This is gentle admission, not an intentional OOM experiment. Capture actual
container `memory.peak`, `memory.events`, RSS, latency and protected generations.
The probe already loaded the cached model on CPU without a download; never
interpret a cap as measured residency.

```bash
# Run on GB10, replacing SOURCE with the verified private script path.
DOCKER_HOST=unix:///run/user/1001/docker.sock docker run --rm \
  --name gb10-native1024-canary --network bridge -p 127.0.0.1:18016:18016 \
  --memory 5g --memory-swap 5g --cpus 2 --pids-limit 128 --oom-score-adj 1000 \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -e TOKENIZERS_PARALLELISM=false -e OMP_NUM_THREADS=2 \
  -v /home/obj/.cache/huggingface/hub:/cache:ro \
  -v SOURCE:/native1024_canary.py:ro --entrypoint python3 \
  ghcr.io/aeon-7/aeon-vllm-ultimate@sha256:2421bb1228a85370c1c50adb31f605c4361acf4d48d65282fcb919e74f34fae7 \
  /native1024_canary.py --container-publish-loopback --model-path \
  /cache/models--intfloat--multilingual-e5-large-instruct/snapshots/274baa43b0e13e37fafa6428dbc7938e62e5c439
```

The whole Hub root is read-only to preserve existing snapshot→blob symlinks;
a snapshot-only mount breaks those links. No CUDA devices are granted.
Only synthetic data is authorized for this canary. Verify API model identity,
1024 finite/unit-normalized outputs, repeat stability, semantic controls,
matched local-incumbent quality/latency, and a fresh isolated LanceDB
`fixed_size_list[1024]` write→commit→flush→new-process readback. Never mix these
vectors into existing Qwen-prefix or other-model collections: same size does
not mean same vector space. EverOS adoption and any persistent routing change
require separate explicit approval and corpus-level acceptance.

## Executed acceptance evidence (2026-10-05)

The pinned cached model was exercised on GB10 CPU through the loopback-published
API, not just imported. Explicit `dimensions=1024` and omitted dimensions both
returned finite, unit-normalized **1024** outputs with repeat cosine **1.0**.
Wrong dimensions and over-512-token inputs returned HTTP 400. Eight bilingual /
cross-language synthetic retrieval queries achieved top-1 and MRR **1.0**, as did
the unchanged Qwen 4096 baseline. In a fresh isolated LanceDB 0.34.0 store,
eight native vectors were committed as `fixed_size_list[1024]`, fsynced, then
read and searched in a fresh process (version 2, eight rows, dimension 1024).
No canonical EverOS store/config/service was accessed or changed.

| Measured control (batch 2, serial, shared-host observation) | Candidate CPU | Qwen incumbent GPU |
|---|---:|---:|
| STS17 English Spearman, first 24 committed pairs | 0.810106 | 0.906360 |
| HTTP latency p50 | 0.345355 s | 0.996097 s |
| Maximum observed HTTP latency | 0.662545 s | 17.948027 s |
| Observed serial texts/s | 5.675909 | 1.285950 |
| Request/text count | 34 / 66 | 32 / 64 |

These sequential samples include shared-route contention; they are **not** an
isolated throughput benchmark or evidence that CPU is generally faster. Candidate
cgroup peak was **3,468,701,696 bytes** under a 5 GiB cap; `memory.swap.max/current`
and every `oom`/`oom_kill` counter were zero. Incumbent cgroup current was
4,121,583,616 bytes after the trial; its 16,961,908,736-byte lifetime peak is not a
trial delta and cgroup accounting is not complete GPU/UMA residency. The canary
was stopped/removed by its exact recorded CID; all three production container
IDs/PIDs/start times and Guard/model unit generations remained unchanged.

**Issue #117 remains open:** this delivers real opt-in 1024 API/storage capability,
not incumbent-quality equivalence or EverOS adoption. English STS regressed by
0.096255 Spearman; the attempted long Chinese STS22 pair was rejected at the
native 512-token boundary. Do not truncate those documents to manufacture parity.
A corpus-appropriate quality decision and separately approved consumer adoption
are still required before a persistent replacement route can be recommended.
