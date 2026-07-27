# Improving Arm A quality (research notes)

Context: after Guard bugfix, A and C both mean **0.9308** on suite-v2 (290).
A is **much faster and cheaper in tokens** than C, but **not higher quality**.
User expectation: loop-cut should reduce context rot ⇒ **both faster and better**. Speed holds; quality uplift does not yet.

## What the data already proves

1. **Loop / budget control works on cost**: A mean tokens 1304 vs C 10267; e2e mean 86s vs 237s; A faster on 224/288 paired cases.
2. **Thinking still matters vs no-think**: A/C beat B by ~2.5pp mean.
3. **A’s quality policy is not free lunch**: 18 A–C disagreements net **9:9**. Salvage helps and hurts in equal measure on this suite.
4. **A has unique availability loss**: 2× `TimeoutError` gen_errors (0 points). C completed those cases.

## Failure taxonomy for A (non-pass, n=21 including null)

| Class | Count (approx) | Examples | Implication |
|---|---:|---|---|
| Runner timeout | 2 | `math.nt.factor.0001`, `reasoning.hard.dice-stop-expected-sum` | Guard/request path or client timeout; pure availability bug |
| Constrained writing / lipogram | many | `instruction.*`, `prose.*`, `creativity.lipogram.*` | Final answer format; thinking may not help; salvage→no-think may hurt |
| Numeric parse / multi-number pollution | several | `math.seq.0004`, `math.easy.*` | Model dumps intermediate numbers; evaluator slotting fails |
| Shared hard misses with C | several | `math.linear.0002`, `reasoning.easy.transitive-ranking` | Base model limit, not Guard-specific |
| Subjective null | 1 | `prose.ocean3` | Needs frontier judge (all arms) |

## Hypotheses ranked by expected ROI

### H1 — Timeout / deadline alignment (highest, mechanical)

**Observation**: only A has timeouts; C finished same cases (slowly).
**Hypothesis**: client timeout or Guard path on `:18014` is tighter / more overloaded under quality-first ladder+embedding loop detector.
**Actions**:

1. Compare A vs C request wall times on the two timeout case_ids from speed_json / Guard access logs (no content).
2. Raise runner timeout only if Guard still healthy; better: ensure quality-first does not stall on embedding loop-detector queue (`on_queue_full = "block"` is a red flag under load).
3. Confirm `request_timeout_ms` / `request_deadline_ms` for quality-first vs raw-max listeners.

**Success metric**: 0 gen_error timeouts on 290-case rerun; mean ≥ C with same quality.

### H2 — Salvage last-rung is too aggressive (quality-critical)

**Config today** (quality-first ladder ends with):

- thinking budgets: 32768 → 16384 → 8192 → **force_disable max_tokens=1024**
- `on_reasoning_loop = bounded_answer_from_cot`
- cot salvage prefix 32KiB, salvage thinking budget 8192

**Hypothesis**: when loop fires late, forcing no-think + 1024 answer tokens **throws away** a near-correct long solution that C keeps refining.
**Actions** (config experiments, A/B only on disagreement set first):

1. Replace final rung `max_tokens=1024` with a larger answer budget (e.g. 4096–8192) while keeping force_disable.
2. Prefer **truncate-and-continue with smaller thinking budget** over immediate force_disable when pre-loop CoT already contains a candidate boxed answer.
3. Emit structured telemetry: `loop_detected`, `salvage_used`, `ladder_rung` per request (content-free counters) so we can correlate with A-only misses.

**Success metric**: on the 9 C-win cases, A recovers ≥3–4 without regressing the 9 A-win cases.

### H3 — Param override breaks temperature contract

**Risk**: quality-first upstream `param_override.temperature = 0.6` while runner claims 0.0.
If override wins, A is **not** the same sampling contract as B/C runners.
**Actions**:

1. Log/verify actual upstream sampling params on `:18014`.
2. For deterministic bench, force quality-first `temperature=0.0` (or disable override on that profile).
3. Re-run A only; compare mean and A–C disagreements.

**Success metric**: if temp was 0.6, expect reduced variance and possibly higher pass on numeric/format tasks.

### H4 — Loop detector false positives on legitimate repetition

Constrained prose/instruction tasks **require** repeated structure (acrostics, refrains). Semantic/token-window detectors may fire and force salvage mid-answer.
**Actions**:

1. Content-free: rate of loop triggers by category (Prose/Instruction vs Math).
2. Soften thresholds only for known structured tasks **or** delay semantic detection until after N useful unique tokens.
3. Do not disable loop guard globally.

### H5 — Answer extraction / box discipline

Several math fails show multi-number pollution (`got [1,2,3,...]`).
Guard could add a **final-answer only** post-pass (or stronger anti-loop on answer channel only) without cutting thinking early.
This is model+prompt territory more than loop-cut, but A can inject a short answer-format hint on salvage retry.

### H6 — Suite ceiling / judge gap

At ~93% mean, remaining fails are hard constraints + a few reasoning items. Frontier judge for prose could change ranking slightly but should not drive infra work.

## Recommended experiment sequence (cheap → expensive)

| Step | Change | Scope | Est. cost |
|---|---|---|---|
| 1 | Audit live temp/top_p on A; fix to 0.0 if overridden | config | 1 A rerun or canary 30 cases |
| 2 | Fix timeouts (embedding queue block / client timeout) | config + maybe Guard | canary hard cases + full A |
| 3 | Soften final salvage rung (larger answer budget; avoid premature force_disable) | config | disagreement-set replay (18 cases) then full A |
| 4 | Add loop/salvage telemetry counters | Guard code | 1 deploy + A rerun |
| 5 | Only then consider detector threshold tuning | config | full A vs C |

Do **not** start with raising thinking budget further—A already spends far fewer tokens than C with equal mean; more budget without better salvage policy likely moves A toward C’s cost without guaranteed quality.

## What “success” looks like

- Quality: A mean **≥ 0.945** or A–C paired wins−losses **≥ +5** on same suite/contract.
- Efficiency: keep A e2e mean **≤ 0.5 × C** (currently ~0.37× — preserve this).
- Availability: **0** runner timeouts on 290.

## Non-goals

- Beating B on pure latency (B has no thinking).
- Changing suite mid-experiment without re-running all arms.
- Publishing raw CoT to debug—use counters and score-only disagreement sets.
