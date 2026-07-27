# Methodology — AEON three-arm bugfix wave (suite-v2 / 290)

## Purpose

Reproduce the A/B/C comparison under a **shared evaluation contract** after Guard infrastructure bugs were fixed. This is a quality + efficiency comparison, not a training-time eval.

> **Historical boundary:** the published 290-case wave used runner
> `temperature=0.0` and is historical evidence only. Its results must not be
> used for the next ranking. The next ranking requires a complete serial A/B/C
> re-run at the author-recommended `temperature=0.6`; Guard-overridden A/C
> routes must also pin `top_p=0.95` and `top_k=20`.

## Preconditions

1. GB10 services active: `vllm-embedding`, `vllm-querit-4b-reranker`, `vllm-aeon-27b-dflash` (baseline), `llm-guard-proxy`.
2. Guard binary includes:
   - non-stream stuck_watchdog exemption (#216)
   - `trigger_on_request_deadline = false` (#220)
   - `retry.max_attempts >= 3` (#219 mitigation)
3. Listeners:
   - A: `:18014` quality-first
   - B: `:18010` raw AEON + `enable_thinking=false`
   - C: `:18015` raw-max (thinking on)
4. Disk for Pod DB + blobs; **do not** publish those paths.

## Contract checklist (must all match)

- [ ] suite_id = `aeon-suite-v2`
- [ ] suite_hash = `015c06c71ec7f162`
- [ ] n_cases = 290
- [ ] temperature = 0.6 (runner and every Guard-overridden text route)
- [ ] Guard override sampling = `top_p=0.95`, `top_k=20`
- [ ] max_tokens = 8192 (runner budget)
- [ ] same evaluator / no frontier judge for subjective tier-1
- [ ] serial execution on one AEON backend
- [ ] arm variable only: thinking / Guard quality policy

## Smoke (before full run)

```bash
# B
curl -sf -X POST http://100.105.4.92:18010/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"aeon-ultimate","messages":[{"role":"user","content":"What is 5+5?"}],"enable_thinking":false,"stream":false,"max_tokens":64,"temperature":0.6}'

# C (use aeon-ultimate, not synthetic forced alias if AEON rejects it)
curl -sf -X POST http://100.105.4.92:18015/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"aeon-ultimate","messages":[{"role":"user","content":"What is 5+5?"}],"stream":false,"max_tokens":64,"temperature":0.6}'

# A
curl -sf -X POST http://100.105.4.92:18014/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"aeon-ultimate","messages":[{"role":"user","content":"What is 5+5?"}],"stream":false,"max_tokens":64,"temperature":0.6}'
```

Confirm HTTP 200 and expected thinking shape (B: no reasoning; A/C: reasoning allowed).

## Launch

Scripts in this package are rerun-ready **method snapshots**. They have been
updated to the author-recommended `temperature=0.6`; do not use the published
0.0 result rows as part of that next ranking.

| Script | Arm |
|---|---|
| `scripts/run_bench_A_quality_first.py` | A → `:18014` |
| `scripts/run_bench_arm_B_C.py` | `BENCH_ARM=B` or `C` |

Environment used in-wave:

```bash
export AEON_DB=/path/to/pod.sqlite
export AEON_BLOB_DIR=/path/to/blobs/
# requires Aeon-Bench-Pod mvp on PYTHONPATH / chdir as in scripts
```

Launch detached (`nohup` / `setsid`), record **PID + run_id** after first log lines. Do not run B and C concurrently with A on the same AEON.

## Monitoring

- Prefer durable SQLite row counts over monitor script health.
- Interval ≈ provider KV-cache TTL when using long-lived monitors (this wave used 3300s).
- Incomplete runs after host reboot: delete partial run_id rows and **rerun full arm**.

## Aggregation (content-free)

From Pod SQLite only select:

- `runs`: id, model, target_url, suite_*, n_cases, params_json, env_json, timestamps
- `results`: run_id, case_id, category, tier, status, score, speed_json, evidence_json (checker metadata only)

Never select `raw_output` / blob refs for publication.

Mean score = average over **non-null** scores.
Full-pass rate on planned cases counts null and timeout as non-pass when labeled as such.

## Paired analysis

Intersect case_ids with numeric scores on both arms. Report:

- mean delta, wins/losses/ties
- optional McNemar later if expanding stats

For speed: intersect case_ids with `e2e_ms` present; report mean/p50/p95 and A_faster counts.

## Publication policy

Public package may include:

- this methodology, report, improve notes
- `data/comparison.json` and `data/comparison_ledger.json`
- runner scripts (no secrets)

Must exclude:

- pod.sqlite, blobs/, raw prompts/completions, credentials, private logs with content

## Known runner quirks

1. `TargetError` → status `loop_retry_exhausted` is a **generic label**, not Guard loop detection.
2. Thinking streams may inflate `decode_tps`; prefer `e2e_ms` + `output_tokens`.
3. `OpenAITarget` does not natively pass `enable_thinking`; B/C script monkey-patches stream payload.
4. Listener synthetic model aliases may 404 at AEON; use `aeon-ultimate` after smoke.
