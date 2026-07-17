# Research diary convention

`docs/research/` stores tracked, durable operational research that must survive chat or agent context compression.

Each dated English note should:

1. state the date, question, scope, and whether live systems were changed;
2. separate directly observed facts, calculations/inferences, and unknowns;
3. cite immutable upstream commits/tags and primary issue or pull-request URLs;
4. summarize only aggregate, non-secret runtime evidence;
5. record the source-first decision, future canary gates, and exact rollback trigger;
6. keep a running-log section for later evidence without rewriting historical conclusions silently.

Do not paste prompts, private transcripts, credentials, raw request payloads, or large command outputs. Temporary/raw investigation material belongs outside this tracked directory.

## Research index

Newest first. These are dated observations and decisions, not substitutes for the current tracked deployment source; later entries may supersede earlier conclusions.

- [2026-07-17 — AEON text post-ready UMA high-water](2026-07-17-aeon-text-post-ready-uma-high-water.md): same-PID retained high-water, request-boundary gaps, predictive admission, and cold-canary acceptance plan.
- [2026-07-16 — GB10 OOM crash analysis](2026-07-16-gb10-oom-crash-analysis.md): pre-freeze memory/thermal timeline and historical mitigation recommendations.
- [2026-07-16 — Querit vLLM migration](2026-07-16-querit-vllm-migration.md): source-controlled reranker migration research and implementation plan.
- [2026-07-15 — Qwen3-Embedding-8B cloud/local endpoint equivalence](2026-07-15-embedding-endpoint-equivalence.md): deterministic endpoint-equivalence evidence.
- [2026-07-15 — GB10 post-reboot memory baseline](2026-07-15-post-reboot-memory-baseline.md): host, cgroup, RSS, and NVML accounting baseline after recovery.
- [2026-07-14 — Querit serving-engine matrix](2026-07-14-querit-serving-engine-matrix.md): source-only serving-engine compatibility and replay decision.
- [2026-07-14 — vLLM upgrade and embedding memory profile](2026-07-14-vllm-upgrade-and-embedding-memory.md): v0.25.x source audit and embedding memory-profile decision.
