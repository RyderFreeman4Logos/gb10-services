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
