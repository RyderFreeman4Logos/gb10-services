# llm-guard-proxy

`llm-guard-proxy` is an Apache-2.0 Rust OpenAI-compatible guard proxy for local and GB10 vLLM deployments.

The proxy sits between OpenAI-compatible clients and an upstream LLM service so debuggability, retries, loop protection, heartbeat behavior, and observability can be added without changing model quality.

## Non-Goals

- Generic `/v1/...` request forwarding is implemented. Non-streaming `/v1/chat/completions` requests use the shielded upstream streaming core by default.
- Configuration is loaded and hot-reloadable. Observability metadata storage exists for retries, loop detection, thinking policy, heartbeat behavior, and upstream metadata discovery.
- This bootstrap does not change upstream OpenAI-compatible semantics.

## Workspace Layout

- `crates/llm-guard-proxy-core`: headless core types shared by service code.
- `crates/llm-guard-proxy`: binary/service entry point.
- `.github/workflows/ci.yml`: GitHub Actions quality gates.
- `justfile` and `lefthook.yml`: local formatting, lint, test, and hook wiring.

## Local Quality Gates

Run the same core checks as CI:

```bash
just fmt
just clippy
just test
```

The aggregate local gates are:

```bash
just pre-commit-fast
just pre-commit
```

The local completion gates are:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
```

## GB10 Compatibility Smoke

Run the live GB10 compatibility smoke from this repository with:

```bash
just smoke-gb10
```

Prerequisites:

- `gb10:18009` is reachable from the local machine.
- `curl`, `python3`, and Cargo are available.
- The command runs the proxy locally against `http://gb10:18009/v1`; it does not deploy to GB10.

The harness creates a private temporary run directory, generates a local
`config.toml`, starts `llm-guard-proxy` on an available localhost port, and
enables SQLite observability. The observability database lives at
`$run_dir/observability.sqlite3`, where `$run_dir` is printed by the command.
The run directory is removed on success or failure unless
`LLM_GUARD_PROXY_SMOKE_KEEP=1` is set.

Checks performed:

- `GET /v1/models` through the proxy must return `200`, parse as JSON, and
  preserve or enrich model context metadata when upstream exposes it.
- `POST /v1/chat/completions` must succeed as a real OpenAI-compatible
  non-streaming chat request. The smoke payload sets
  `chat_template_kwargs.enable_thinking=false` for low-token AEON/vLLM runs.
- `POST /v1/chat/completions` with `stream=true` must return
  `text/event-stream` data.
- `POST /v1/completions`, `POST /v1/embeddings`, and `POST /v1/rerank` are
  probed and reported with the exact upstream status. Current GB10 observations
  classify `/v1/embeddings` and `/v1/rerank` as upstream-unsupported when they
  return `404`.
- `GET /v1/../admin` is sent with `curl --path-as-is` and must be rejected by
  the proxy with `400` before any upstream attempt.
- SQLite observability must contain one request row per smoke call, one attempt
  row per forwarded call, and no attempt row for the rejected dot-segment path.

Useful overrides:

```bash
LLM_GUARD_PROXY_SMOKE_PORT=19009 just smoke-gb10
LLM_GUARD_PROXY_SMOKE_MODEL=aeon-ultimate just smoke-gb10
LLM_GUARD_PROXY_SMOKE_UPSTREAM_BASE_URL=http://gb10:18009/v1 just smoke-gb10
LLM_GUARD_PROXY_BIN=target/debug/llm-guard-proxy just smoke-gb10
LLM_GUARD_PROXY_SMOKE_KEEP=1 just smoke-gb10
```

The shielded chat core is enabled by default for non-streaming chat completions:

- Downstream non-stream chat requests are sent upstream with `stream=true`.
- The default downstream response is `text/event-stream`. While the shielded upstream attempt is pending, the proxy emits comment heartbeats shaped as `: llm-guard-proxy heartbeat`. After the attempt is accepted, it emits `event: final` with the accepted OpenAI-compatible chat completion JSON in the `data:` field.
- If the same normalized input fingerprint repeats within the loop-guard window, the downstream response switches to `application/json` with leading whitespace heartbeat bytes before the final JSON body. Standard JSON parsers accept the leading whitespace.
- The same `[loop_guard]` section also inspects shielded upstream SSE deltas for loops in hidden reasoning fields, visible content, and tool-call argument fragments. Detected output loops abort the shielded attempt through the existing upstream-body error path and record only bounded hashes/counters in observability metadata by default.
- `heartbeat.mode = "disabled"` keeps the legacy buffered JSON response for shielded non-stream chat completions.
- Attempt observability records include first-byte latency, first-token latency, finish reason, parsed content/reasoning/tool-call delta counters, and `loop_*` diagnostics when loop guard aborts a shielded attempt.
- Downstream `stream=true` chat requests currently stay on the generic streaming path to preserve first-chunk timing and backpressure behavior while later issues add release-after-inspection streaming.
- Set `[shielding] enabled = false` and hot reload the config to fall back to generic forwarding for rollback or compatibility testing.

## Operational Endpoints

- `GET /health` returns a small JSON status object with `process = "alive"` and upstream readiness. By default it performs a bounded `GET /v1/models` readiness probe against the configured upstream and returns `503` when the proxy process is alive but upstream is unavailable. Set `observability.health_upstream_probe_enabled = false` to skip the upstream probe and report `upstream = "not_checked"`.
- `GET /metrics` returns Prometheus-style text metrics when `observability.metrics_enabled = true` (the default). Metrics are aggregated from the bounded SQLite observability store and use low-cardinality labels only: request/attempt status, upstream/downstream mode, HTTP status class, heartbeat mode, upstream error kind, retry/loop observations, current-retained latency distribution gauges, and monotonic storage pruning counters. Metrics derived from retained rows are named `llm_guard_proxy_current_retained_*` and emitted as gauges because retention pruning can remove old rows; only cumulative storage pruning metrics use `*_total` counters. Metrics never include raw prompts, raw bodies, request headers, query strings, model IDs, or request IDs.
- `GET /debug/recent-requests?limit=N` is disabled by default. Set `observability.debug_summary_enabled = true` to expose bounded recent request summaries; optionally set `observability.debug_summary_admin_token` and pass the same admin value in the `Authorization: Bearer ...` header or the `x-admin-token` header. Output is clamped by `observability.debug_summary_max_records`, omits raw prompt/output payloads, omits sensitive metadata keys, and redacts sensitive-looking values.

## Deployment Hardening

The built-in defaults are intended for local, LAN, Tailscale, or Cloudflare-fronted testing where an upstream OpenAI-compatible service already enforces its own model and account policy. They do not replace network ACLs, TLS termination, upstream authentication, or host firewall rules.

- Request body buffering is capped by `server.max_request_body_bytes` before upstream work starts. Rejections return an OpenAI-shaped `413` error and do not create upstream attempts.
- Proxied request admission is capped by `server.max_in_flight_requests`. The proxy returns `503 proxy_in_flight_limit_exceeded` before reading the body when capacity is exhausted. Permits are held until the downstream response body completes or is dropped.
- Control-plane `GET /v1/models` forwarding uses the independent `server.max_control_plane_in_flight_requests` cap, defaulting to `128` so operator and monitoring bursts fit under the bound. It does not consume generation request slots; excess control-plane requests fail fast with `503 proxy_control_plane_in_flight_limit_exceeded`.
- Upstream requests use `upstream.request_timeout_ms` as a per-attempt total timeout, including streamed response body reads. Timeout errors are recorded with bounded error kinds such as `timeout_failure`, not raw URLs or headers.
- Ctrl-C and SIGTERM start graceful shutdown: the listener stops accepting new connections and in-flight response bodies are allowed to finish or be canceled by downstream drop.
- Request forwarding strips hop-by-hop headers, `Host`, `Content-Length`, and the admin-only `x-admin-token` header. OpenAI-compatible `Authorization` and `x-api-key` headers are forwarded to the upstream for normal `/v1/...` calls, but are redacted from logs, metrics, debug summaries, and observability metadata.
- Raw prompts, raw outputs, reasoning text, and tool arguments are not persisted by default. Keep `observability.capture_raw_payloads = false` for shared LAN or Cloudflare exposure unless you have a separate data-handling reason to enable it.
- `heartbeat.mode = "sse"` is the default downstream liveness mode for shielded non-stream chat and is suitable for Cloudflare-style idle timeout protection. Repeated normalized inputs switch to JSON whitespace heartbeat to preserve parseable non-stream JSON.

## Configuration

The default config path is:

```text
~/.config/llm-guard-proxy/config.toml
```

Development and tests can use an explicit path:

```bash
llm-guard-proxy --config ./config.toml
```

If the default file is absent, the service uses built-in defaults. An explicit `--config` path must exist and must validate.

Sample config:

```toml
[server]
bind_host = "127.0.0.1"
port = 18009
max_in_flight_requests = 16
max_control_plane_in_flight_requests = 128
max_request_body_bytes = 67108864

[upstream]
base_url = "http://gb10:18009/v1"
request_timeout_ms = 120000

[upstream.metadata]
discovery_enabled = true
enrich_responses = true
refresh_interval_secs = 60
# context_length_override = 256000
# max_model_len_override = 256000

[shielding]
enabled = true

[observability]
enabled = true
sqlite_path = "~/.local/state/llm-guard-proxy/observability.sqlite3"
capture_raw_payloads = false
metrics_enabled = true
health_upstream_probe_enabled = true
health_upstream_probe_timeout_ms = 500
debug_summary_enabled = false
# debug_summary_admin_token = "change-me-for-admin-debug"
debug_summary_max_records = 25

[observability.retention]
max_bytes = 1073741824
prune_to_bytes = 805306368
max_records = 100000
prune_to_records = 80000

[thinking]
enabled = true
force_disable = false
budget_tokens = 32768
preserve_answer_budget = true
# "apply" keeps the regular thinking rewrite for every chat request.
# "passthrough" leaves caller-provided thinking fields untouched when a
# request carries tool/function-calling hints.
tool_request_policy = "apply"

[loop_guard]
enabled = true
normalized_input_window_secs = 120
max_repeated_inputs = 1
output_repeated_line_threshold = 24
output_token_window_size = 12
output_repeated_token_window_threshold = 32
output_suffix_cycle_threshold = 32
output_low_progress_min_bytes = 4096
output_low_progress_unique_ratio_percent = 15
input_overlap_threshold_multiplier = 4
reasoning_semantic_detection_enabled = true
reasoning_semantic_similarity_threshold_percent = 55
reasoning_semantic_window_token_count = 24
reasoning_semantic_minimum_token_count = 8
reasoning_semantic_history_window_count = 16

# Semantic loop detection is reasoning-only and compares bounded normalized
# token/ngram windows with Jaccard similarity. The default is enabled with a
# conservative majority-overlap threshold; raise the threshold to reduce false
# positives, lower it to catch looser paraphrases, or disable it to keep only
# the hash, suffix-cycle, and low-progress detectors.

[retry]
enabled = true
max_attempts = 5
anti_loop_hint_enabled = true

[heartbeat]
mode = "sse" # sse, json-whitespace, disabled
interval_secs = 15

[cloudflare]
enabled = true
```

Retention byte limits apply to actual SQLite page storage. SQLite has a
schema/page-size minimum footprint, so limits below that floor prune retained
rows but cannot shrink the database file below the empty-store minimum.
Record-count retention prunes from `max_records` down to `prune_to_records`;
when omitted, `prune_to_records` defaults to 80% of `max_records`.

For shielded chat requests, the thinking policy injects or raises known
`thinking.budget_tokens`, `thinking_token_budget`, and chat-template budget
fields unless the caller explicitly disables thinking or sets a zero budget. When
`preserve_answer_budget` is enabled, numeric `max_tokens`,
`max_completion_tokens`, and `max_output_tokens` fields are increased by the
thinking-budget delta so the caller's answer-token reserve is preserved.
Set `thinking.tool_request_policy = "passthrough"` to make requests with
`tools`, legacy `functions`, `tool_choice`, or `function_call` bypass the
thinking rewrite entirely; the proxy then forwards any caller-provided thinking
parameters as-is while still applying the regular thinking policy to non-tool
chat requests. Set hot-reloadable `thinking.force_disable = true` to override
all caller and proxy thinking budgets with zero, normalize known
`enable_thinking`/`enabled` markers to `false`, and leave answer-token fields
unchanged. Force-disable takes precedence over `thinking.enabled`,
`thinking.budget_tokens`, zero-budget opt-outs, caller disable markers, and
`thinking.tool_request_policy = "passthrough"`.

Reloadable fields:

- `server.max_in_flight_requests`
- `server.max_request_body_bytes`
- `shielding.enabled`
- `observability.enabled`
- `observability.capture_raw_payloads`
- `observability.metrics_enabled`
- `observability.health_upstream_probe_enabled`
- `observability.health_upstream_probe_timeout_ms`
- `observability.debug_summary_enabled`
- `observability.debug_summary_admin_token`
- `observability.debug_summary_max_records`
- `observability.retention.max_bytes`
- `observability.retention.prune_to_bytes`
- `observability.retention.max_records`
- `observability.retention.prune_to_records`
- `thinking.enabled`
- `thinking.force_disable`
- `thinking.budget_tokens`
- `thinking.preserve_answer_budget`
- `thinking.tool_request_policy`
- `loop_guard.enabled`
- `loop_guard.normalized_input_window_secs`
- `loop_guard.max_repeated_inputs`
- `loop_guard.output_repeated_line_threshold`
- `loop_guard.output_token_window_size`
- `loop_guard.output_repeated_token_window_threshold`
- `loop_guard.output_suffix_cycle_threshold`
- `loop_guard.output_low_progress_min_bytes`
- `loop_guard.output_low_progress_unique_ratio_percent`
- `loop_guard.input_overlap_threshold_multiplier`
- `loop_guard.reasoning_semantic_detection_enabled`
- `loop_guard.reasoning_semantic_similarity_threshold_percent`
- `loop_guard.reasoning_semantic_window_token_count`
- `loop_guard.reasoning_semantic_minimum_token_count`
- `loop_guard.reasoning_semantic_history_window_count`
- `retry.enabled`
- `retry.max_attempts`
- `retry.anti_loop_hint_enabled`
- `heartbeat.mode`
- `heartbeat.interval_secs`
- `cloudflare.enabled`
- `upstream.request_timeout_ms`
- `upstream.metadata.discovery_enabled`
- `upstream.metadata.enrich_responses`
- `upstream.metadata.refresh_interval_secs`
- `upstream.metadata.context_length_override`
- `upstream.metadata.max_model_len_override`

Restart-required fields:

- `server.bind_host`
- `server.port`
- `upstream.base_url`
- `observability.sqlite_path`
