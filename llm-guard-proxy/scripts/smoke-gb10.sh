#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

UPSTREAM_BASE_URL="${LLM_GUARD_PROXY_SMOKE_UPSTREAM_BASE_URL:-http://gb10:18009/v1}"
BIND_HOST="${LLM_GUARD_PROXY_SMOKE_HOST:-127.0.0.1}"
PORT="${LLM_GUARD_PROXY_SMOKE_PORT:-}"
MODEL="${LLM_GUARD_PROXY_SMOKE_MODEL:-aeon-ultimate}"
REQUEST_TIMEOUT_SECS="${LLM_GUARD_PROXY_SMOKE_REQUEST_TIMEOUT_SECS:-120}"
CONNECT_TIMEOUT_SECS="${LLM_GUARD_PROXY_SMOKE_CONNECT_TIMEOUT_SECS:-5}"
READY_TIMEOUT_SECS="${LLM_GUARD_PROXY_SMOKE_READY_TIMEOUT_SECS:-120}"
KEEP_RUN_DIR="${LLM_GUARD_PROXY_SMOKE_KEEP:-0}"
ADMIN_TOKEN="${LLM_GUARD_PROXY_SMOKE_ADMIN_TOKEN:-}"

proxy_pid=""
run_dir=""

fail() {
    printf 'smoke-gb10: %s\n' "$*" >&2
    if [[ -n "${run_dir}" && -f "${run_dir}/proxy.stderr.log" ]]; then
        printf 'smoke-gb10: proxy stderr tail follows\n' >&2
        tail -n 80 "${run_dir}/proxy.stderr.log" >&2 || true
    fi
    exit 1
}

cleanup() {
    local status=$?
    terminate_proxy
    if [[ -n "${run_dir}" ]]; then
        if [[ "${KEEP_RUN_DIR}" == "1" ]]; then
            printf 'smoke-gb10: kept run_dir=%s\n' "${run_dir}" >&2
        else
            rm -rf "${run_dir}"
        fi
    fi
    return "${status}"
}
trap cleanup EXIT

process_state() {
    ps -o stat= -p "$1" 2>/dev/null | tr -d '[:space:]' || true
}

terminate_proxy() {
    local state

    if [[ -z "${proxy_pid}" ]]; then
        return 0
    fi

    if kill -0 "${proxy_pid}" 2>/dev/null; then
        kill "${proxy_pid}" 2>/dev/null || true
        for _ in {1..50}; do
            state="$(process_state "${proxy_pid}")"
            if [[ -z "${state}" || "${state}" == Z* ]]; then
                break
            fi
            sleep 0.1
        done
        state="$(process_state "${proxy_pid}")"
        if [[ -n "${state}" && "${state}" != Z* ]]; then
            kill -KILL "${proxy_pid}" 2>/dev/null || true
        fi
    fi

    wait "${proxy_pid}" 2>/dev/null || true
    proxy_pid=""
}

require_command() {
    local command_name="$1"
    command -v "${command_name}" >/dev/null 2>&1 \
        || fail "required command not found: ${command_name}"
}

resolve_proxy_binary() {
    local target_dir
    local binary_path

    if [[ -n "${LLM_GUARD_PROXY_BIN:-}" ]]; then
        binary_path="${LLM_GUARD_PROXY_BIN}"
    else
        cargo build --quiet -p llm-guard-proxy --bin llm-guard-proxy \
            || fail "failed to build proxy binary"
        target_dir="${CARGO_TARGET_DIR:-${repo_root}/target}"
        if [[ "${target_dir}" != /* ]]; then
            target_dir="${repo_root}/${target_dir}"
        fi
        if [[ -n "${CARGO_BUILD_TARGET:-}" ]]; then
            target_dir="${target_dir}/${CARGO_BUILD_TARGET}"
        fi
        binary_path="${target_dir}/debug/llm-guard-proxy"
    fi

    if [[ ! -x "${binary_path}" ]]; then
        fail "proxy binary is not executable: ${binary_path}"
    fi

    printf '%s\n' "${binary_path}"
}

toml_quote() {
    python3 - "$1" <<'PY'
import json
import sys

print(json.dumps(sys.argv[1]))
PY
}

redacted_url() {
    python3 - "$1" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

raw_url = sys.argv[1]
try:
    parsed = urlsplit(raw_url)
except ValueError:
    print("[invalid URL]")
    raise SystemExit

if parsed.scheme not in {"http", "https"} or not parsed.netloc:
    print("[invalid URL]")
    raise SystemExit

host = parsed.hostname or ""
try:
    port = parsed.port
except ValueError:
    print("[invalid URL]")
    raise SystemExit
if port is not None:
    host = f"{host}:{port}"
userinfo = "redacted:redacted@" if parsed.username or parsed.password else ""
query = "redacted" if parsed.query else ""
print(urlunsplit((parsed.scheme, f"{userinfo}{host}", parsed.path, query, "")))
PY
}

choose_port() {
    python3 - "${BIND_HOST}" <<'PY'
import socket
import sys

host = sys.argv[1]
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind((host, 0))
    print(sock.getsockname()[1])
PY
}

require_http_status() {
    local label="$1"
    local observed="$2"
    local expected="$3"
    if [[ "${observed}" != "${expected}" ]]; then
        fail "${label} returned HTTP ${observed}; expected ${expected}"
    fi
}

http_request() {
    local method="$1"
    local url="$2"
    local request_body_path="$3"
    local response_body_path="$4"
    local response_headers_path="$5"
    local label="$6"
    local path_as_is="${7:-false}"
    local accept_header="${8:-application/json}"
    local extra_header="${9:-}"
    local stderr_path="${run_dir}/curl-${label//[^A-Za-z0-9_.-]/_}.stderr"
    local curl_exit
    local http_status
    local curl_args=(
        --silent
        --show-error
        --max-time "${REQUEST_TIMEOUT_SECS}"
        --connect-timeout "${CONNECT_TIMEOUT_SECS}"
        --request "${method}"
        --output "${response_body_path}"
        --dump-header "${response_headers_path}"
        --write-out "%{http_code}"
        --header "accept: ${accept_header}"
    )

    if [[ "${path_as_is}" == "true" ]]; then
        curl_args+=(--path-as-is)
    fi
    if [[ -n "${extra_header}" ]]; then
        curl_args+=(--header "${extra_header}")
    fi
    if [[ "${request_body_path}" != "-" ]]; then
        curl_args+=(--header "content-type: application/json" --data-binary "@${request_body_path}")
    fi

    set +e
    http_status="$(curl "${curl_args[@]}" "${url}" 2>"${stderr_path}")"
    curl_exit=$?
    set -e

    if [[ "${curl_exit}" -ne 0 ]]; then
        fail "${label} curl failed with exit=${curl_exit}: $(tr '\n' ' ' <"${stderr_path}" | head -c 240)"
    fi
    if [[ ! "${http_status}" =~ ^[0-9]{3}$ || "${http_status}" == "000" ]]; then
        fail "${label} did not return a usable HTTP status: ${http_status}"
    fi
    printf '%s' "${http_status}"
}

write_payloads() {
    local chat_payload="$1"
    local stream_payload="$2"
    local completions_payload="$3"
    local embeddings_payload="$4"
    local rerank_payload="$5"

    python3 - "${MODEL}" "${chat_payload}" "${stream_payload}" \
        "${completions_payload}" "${embeddings_payload}" "${rerank_payload}" <<'PY'
import json
import sys
from pathlib import Path

model, chat_path, stream_path, completions_path, embeddings_path, rerank_path = sys.argv[1:]

chat = {
    "model": model,
    "messages": [
        {"role": "user", "content": "Reply with exactly one word: pong"}
    ],
    "temperature": 0,
    "max_tokens": 16,
    "stream": False,
    "chat_template_kwargs": {"enable_thinking": False},
}
stream = dict(chat)
stream["stream"] = True
stream["max_tokens"] = 8
completions = {
    "model": model,
    "prompt": "Reply with exactly one word: pong",
    "temperature": 0,
    "max_tokens": 8,
}
embeddings = {"model": model, "input": "ping"}
rerank = {"model": model, "query": "ping", "documents": ["pong"]}

for path, payload in [
    (chat_path, chat),
    (stream_path, stream),
    (completions_path, completions),
    (embeddings_path, embeddings),
    (rerank_path, rerank),
]:
    Path(path).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
PY
}

validate_models_response() {
    python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

if not isinstance(payload, dict):
    raise SystemExit("/v1/models response is not a JSON object")
models = payload.get("data")
if not isinstance(models, list) or not models:
    raise SystemExit("/v1/models response data must be a non-empty array")

context_summaries = []
for model in models:
    if not isinstance(model, dict):
        raise SystemExit("/v1/models data entries must be JSON objects")
    model_id = model.get("id", "<missing>")
    max_model_len = model.get("max_model_len")
    if isinstance(max_model_len, int):
        if model.get("context_length") != max_model_len:
            raise SystemExit(f"{model_id} context_length does not match max_model_len")
        if model.get("max_context_length") != max_model_len:
            raise SystemExit(f"{model_id} max_context_length does not match max_model_len")
        context_summaries.append(
            f"{model_id}:max_model_len={max_model_len}:context_length={model.get('context_length')}"
        )

context_summary = ",".join(context_summaries) if context_summaries else "absent"
print(f"models_count={len(models)} context_metadata={context_summary}")
PY
}

validate_config_summary() {
    python3 - "$1" <<'PY'
import sys

text = open(sys.argv[1], "r", encoding="utf-8").read()
for needle in ["llm-guard-proxy", "readiness=ready", "observability_enabled=true"]:
    if needle not in text:
        raise SystemExit(f"/config-summary missing {needle}")
if "Bearer " in text or "x-admin-token" in text:
    raise SystemExit("/config-summary leaked auth-looking text")
print("summary=ok")
PY
}

validate_health_response() {
    python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("service") != "llm-guard-proxy":
    raise SystemExit("/health service mismatch")
if payload.get("process") != "alive":
    raise SystemExit("/health process is not alive")
if payload.get("upstream") != "ready":
    raise SystemExit(f"/health upstream={payload.get('upstream')!r}; expected ready")
if payload.get("upstream_probe_enabled") is not True:
    raise SystemExit("/health upstream probe should be enabled")
print("process=alive upstream=ready")
PY
}

validate_metrics_response() {
    python3 - "$1" <<'PY'
import sys

text = open(sys.argv[1], "r", encoding="utf-8").read()
for needle in [
    "llm_guard_proxy_current_retained_requests",
    "llm_guard_proxy_current_retained_attempts",
    "llm_guard_proxy_storage_pruning_events_total",
]:
    if needle not in text:
        raise SystemExit(f"/metrics missing {needle}")
for forbidden in ["Bearer ", "Authorization", "x-admin-token", "api_key"]:
    if forbidden in text:
        raise SystemExit(f"/metrics leaked {forbidden!r}")
metric_lines = sum(1 for line in text.splitlines() if line and not line.startswith("#"))
print(f"metric_lines={metric_lines}")
PY
}

validate_debug_summary() {
    python3 - "$1" "$2" <<'PY'
import json
import sys

body_path, token = sys.argv[1:]
raw = open(body_path, "r", encoding="utf-8").read()
if token and token in raw:
    raise SystemExit("/debug/recent-requests leaked the admin token")
payload = json.loads(raw)
if not isinstance(payload, dict):
    raise SystemExit("/debug/recent-requests response is not a JSON object")
if payload.get("limit") != 20:
    raise SystemExit(f"/debug/recent-requests limit={payload.get('limit')!r}; expected 20")
request_count = payload.get("request_count")
if not isinstance(request_count, int) or request_count < 1:
    raise SystemExit("/debug/recent-requests request_count must be a positive integer")
if "redaction" not in payload:
    raise SystemExit("/debug/recent-requests missing redaction statement")
if not isinstance(payload.get("requests"), list):
    raise SystemExit("/debug/recent-requests requests must be a list")
print(f"request_count={request_count} redaction=present")
PY
}

validate_chat_response() {
    python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

headers_path, body_path = sys.argv[1:]
headers = Path(headers_path).read_text(encoding="utf-8", errors="replace").lower()
body = Path(body_path).read_bytes()

def final_payload_from_sse(raw_body):
    text = raw_body.decode("utf-8")
    for event in text.split("\n\n"):
        event_name = ""
        data_lines = []
        for line in event.splitlines():
            line = line.rstrip("\r")
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        if event_name == "final":
            return json.loads("\n".join(data_lines))
    raise SystemExit("/v1/chat/completions SSE response did not include a final event")

if "text/event-stream" in headers:
    payload = final_payload_from_sse(body)
    downstream = "sse-final"
else:
    payload = json.loads(body.decode("utf-8"))
    downstream = "json"

if not isinstance(payload, dict):
    raise SystemExit("/v1/chat/completions response is not a JSON object")
choices = payload.get("choices")
if not isinstance(choices, list) or not choices:
    raise SystemExit("/v1/chat/completions response must include non-empty choices")
print(f"downstream={downstream} choices={len(choices)}")
PY
}

validate_json_if_success() {
    local status="$1"
    local body_path="$2"
    local label="$3"
    if [[ "${status}" =~ ^2 ]]; then
        python3 - "${body_path}" "${label}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        json.load(handle)
except Exception as error:
    raise SystemExit(f"{sys.argv[2]} returned 2xx with invalid JSON: {error}") from error
PY
    fi
}

validate_stream_response() {
    python3 - "$1" "$2" <<'PY'
import sys

headers_path, body_path = sys.argv[1:]
headers = open(headers_path, "r", encoding="utf-8", errors="replace").read().lower()
body = open(body_path, "rb").read()

if "content-type:" not in headers or "text/event-stream" not in headers:
    raise SystemExit("/v1/chat/completions streaming response is not text/event-stream")
if b"data:" not in body:
    raise SystemExit("/v1/chat/completions streaming response did not include SSE data frames")
print("sse_data=true")
PY
}

verify_observability() {
    python3 - "$1" "$2" <<'PY'
import json
import sqlite3
import sys

db_path = sys.argv[1]
expected_forwarded = int(sys.argv[2])

connection = sqlite3.connect(db_path)
request_rows = connection.execute(
    "SELECT request_id, request_metadata_json FROM requests"
).fetchall()
attempt_rows = connection.execute(
    "SELECT request_id FROM attempts"
).fetchall()

paths_by_request = {}
for request_id, metadata_json in request_rows:
    metadata = json.loads(metadata_json)
    paths_by_request[request_id] = metadata.get("path")

dot_segment_request_ids = [
    request_id for request_id, path in paths_by_request.items() if path == "/v1/../admin"
]
dot_segment_attempts = sum(
    1 for (request_id,) in attempt_rows if request_id in set(dot_segment_request_ids)
)

if len(request_rows) != expected_forwarded + 1:
    raise SystemExit(
        f"observability request rows={len(request_rows)}; expected {expected_forwarded + 1}"
    )
if len(attempt_rows) != expected_forwarded:
    raise SystemExit(
        f"observability attempt rows={len(attempt_rows)}; expected {expected_forwarded}"
    )
if len(dot_segment_request_ids) != 1:
    raise SystemExit(
        f"dot-segment request rows={len(dot_segment_request_ids)}; expected 1"
    )
if dot_segment_attempts != 0:
    raise SystemExit(
        f"dot-segment attempt rows={dot_segment_attempts}; expected 0"
    )

print(
    "requests={requests} attempts={attempts} forwarded_attempts={attempts} "
    "dot_segment_requests={dot_requests} dot_segment_attempts={dot_attempts}".format(
        requests=len(request_rows),
        attempts=len(attempt_rows),
        dot_requests=len(dot_segment_request_ids),
        dot_attempts=dot_segment_attempts,
    )
)
PY
}

wait_for_readiness() {
    local base_url="$1"
    local deadline=$((SECONDS + READY_TIMEOUT_SECS))
    local status
    while (( SECONDS < deadline )); do
        if ! kill -0 "${proxy_pid}" 2>/dev/null; then
            fail "proxy exited before readiness"
        fi
        set +e
        status="$(curl --silent --show-error --max-time 2 --output /dev/null --write-out "%{http_code}" "${base_url}/config-summary" 2>/dev/null)"
        set -e
        if [[ "${status}" == "200" ]]; then
            return 0
        fi
        sleep 0.25
    done
    fail "proxy did not become ready within ${READY_TIMEOUT_SECS}s"
}

require_command python3
require_command curl
if [[ -z "${LLM_GUARD_PROXY_BIN:-}" ]]; then
    require_command cargo
fi
if ! proxy_binary="$(resolve_proxy_binary)"; then
    exit 1
fi

if [[ -z "${ADMIN_TOKEN}" ]]; then
    ADMIN_TOKEN="$(python3 - <<'PY'
import secrets

print(secrets.token_urlsafe(24))
PY
)"
fi

if [[ -z "${PORT}" ]]; then
    PORT="$(choose_port)"
fi

run_dir="$(mktemp -d "${TMPDIR:-/tmp}/llm-guard-proxy-gb10-smoke.XXXXXX")"
chmod 700 "${run_dir}"

config_path="${run_dir}/config.toml"
sqlite_path="${run_dir}/observability.sqlite3"
proxy_log="${run_dir}/proxy.stderr.log"
base_url="http://${BIND_HOST}:${PORT}"
redacted_upstream_base_url="$(redacted_url "${UPSTREAM_BASE_URL}")"

cat >"${config_path}" <<EOF
[server]
bind_host = $(toml_quote "${BIND_HOST}")
port = ${PORT}
max_in_flight_requests = 8

[upstream]
base_url = $(toml_quote "${UPSTREAM_BASE_URL}")

[upstream.metadata]
discovery_enabled = true
enrich_responses = true
refresh_interval_secs = 60

[shielding]
enabled = true

[observability]
enabled = true
sqlite_path = $(toml_quote "${sqlite_path}")
capture_raw_payloads = false
metrics_enabled = true
health_upstream_probe_enabled = true
health_upstream_probe_timeout_ms = 500
debug_summary_enabled = true
debug_summary_admin_token = $(toml_quote "${ADMIN_TOKEN}")
debug_summary_max_records = 20

[observability.retention]
max_bytes = 1073741824
prune_to_bytes = 805306368
max_records = 100000
prune_to_records = 80000

[thinking]
enabled = true
budget_tokens = 32768
preserve_answer_budget = true
tool_request_policy = "passthrough"

[loop_guard]
enabled = true
normalized_input_window_secs = 120
max_repeated_inputs = 1

[retry]
enabled = true
max_attempts = 2

[heartbeat]
mode = "sse"
interval_secs = 15

[cloudflare]
enabled = true
EOF

printf 'smoke-gb10: run_dir=%s\n' "${run_dir}"
printf 'smoke-gb10: observability_db=%s\n' "${sqlite_path}"
printf 'smoke-gb10: proxy_base_url=%s upstream_base_url=%s model=%s\n' \
    "${base_url}" "${redacted_upstream_base_url}" "${MODEL}"

# Own the server PID directly. Using `cargo run &` would make $! point at
# Cargo's wrapper process, leaving the proxy child outside cleanup ownership.
"${proxy_binary}" --config "${config_path}" >"${proxy_log}" 2>&1 &
proxy_pid=$!

wait_for_readiness "${base_url}"
printf 'smoke-gb10: readiness=ready\n'

config_summary_body="${run_dir}/config-summary.body.txt"
config_summary_headers="${run_dir}/config-summary.headers"
config_summary_status="$(http_request GET "${base_url}/config-summary" - "${config_summary_body}" "${config_summary_headers}" "config-summary")"
require_http_status "GET /config-summary" "${config_summary_status}" "200"
config_summary="$(validate_config_summary "${config_summary_body}")"
printf 'smoke-gb10: endpoint=/config-summary status=%s %s\n' \
    "${config_summary_status}" "${config_summary}"

health_body="${run_dir}/health.body.json"
health_headers="${run_dir}/health.headers"
health_status="$(http_request GET "${base_url}/health" - "${health_body}" "${health_headers}" "health")"
require_http_status "GET /health" "${health_status}" "200"
health_summary="$(validate_health_response "${health_body}")"
printf 'smoke-gb10: endpoint=/health status=%s %s\n' \
    "${health_status}" "${health_summary}"

chat_payload="${run_dir}/chat.json"
stream_payload="${run_dir}/chat-stream.json"
completions_payload="${run_dir}/completions.json"
embeddings_payload="${run_dir}/embeddings.json"
rerank_payload="${run_dir}/rerank.json"
write_payloads "${chat_payload}" "${stream_payload}" "${completions_payload}" \
    "${embeddings_payload}" "${rerank_payload}"

forwarded_calls=0

models_body="${run_dir}/models.body.json"
models_headers="${run_dir}/models.headers"
models_status="$(http_request GET "${base_url}/v1/models" - "${models_body}" "${models_headers}" "models")"
require_http_status "GET /v1/models" "${models_status}" "200"
models_summary="$(validate_models_response "${models_body}")"
forwarded_calls=$((forwarded_calls + 1))
printf 'smoke-gb10: endpoint=/v1/models status=%s %s\n' "${models_status}" "${models_summary}"

chat_body="${run_dir}/chat.body.json"
chat_headers="${run_dir}/chat.headers"
chat_status="$(http_request POST "${base_url}/v1/chat/completions" "${chat_payload}" "${chat_body}" "${chat_headers}" "chat")"
require_http_status "POST /v1/chat/completions" "${chat_status}" "200"
chat_summary="$(validate_chat_response "${chat_headers}" "${chat_body}")"
forwarded_calls=$((forwarded_calls + 1))
printf 'smoke-gb10: endpoint=/v1/chat/completions mode=non_stream status=%s %s\n' \
    "${chat_status}" "${chat_summary}"

stream_body="${run_dir}/chat-stream.body"
stream_headers="${run_dir}/chat-stream.headers"
stream_status="$(http_request POST "${base_url}/v1/chat/completions" "${stream_payload}" "${stream_body}" "${stream_headers}" "chat-stream" false "text/event-stream")"
require_http_status "POST /v1/chat/completions stream" "${stream_status}" "200"
stream_summary="$(validate_stream_response "${stream_headers}" "${stream_body}")"
forwarded_calls=$((forwarded_calls + 1))
printf 'smoke-gb10: endpoint=/v1/chat/completions mode=stream status=%s %s\n' \
    "${stream_status}" "${stream_summary}"

for probe in \
    "completions:/v1/completions:${completions_payload}" \
    "embeddings:/v1/embeddings:${embeddings_payload}" \
    "rerank:/v1/rerank:${rerank_payload}"
do
    label="${probe%%:*}"
    rest="${probe#*:}"
    endpoint="${rest%%:*}"
    payload="${rest#*:}"
    body="${run_dir}/${label}.body.json"
    headers="${run_dir}/${label}.headers"
    status="$(http_request POST "${base_url}${endpoint}" "${payload}" "${body}" "${headers}" "${label}")"
    validate_json_if_success "${status}" "${body}" "${endpoint}"
    forwarded_calls=$((forwarded_calls + 1))
    if [[ "${status}" =~ ^2 ]]; then
        printf 'smoke-gb10: endpoint=%s status=%s result=success\n' "${endpoint}" "${status}"
    else
        printf 'smoke-gb10: endpoint=%s status=%s result=upstream_non_success\n' \
            "${endpoint}" "${status}"
    fi
done

dot_body="${run_dir}/dot-segment.body.json"
dot_headers="${run_dir}/dot-segment.headers"
dot_status="$(http_request GET "${base_url}/v1/../admin" - "${dot_body}" "${dot_headers}" "dot-segment" true)"
require_http_status "GET /v1/../admin" "${dot_status}" "400"
printf 'smoke-gb10: endpoint=/v1/../admin status=%s result=rejected_before_upstream\n' "${dot_status}"

metrics_body="${run_dir}/metrics.body.txt"
metrics_headers="${run_dir}/metrics.headers"
metrics_status="$(http_request GET "${base_url}/metrics" - "${metrics_body}" "${metrics_headers}" "metrics" false "text/plain")"
require_http_status "GET /metrics" "${metrics_status}" "200"
metrics_summary="$(validate_metrics_response "${metrics_body}")"
printf 'smoke-gb10: endpoint=/metrics status=%s %s\n' \
    "${metrics_status}" "${metrics_summary}"

debug_unauth_body="${run_dir}/debug-unauth.body.json"
debug_unauth_headers="${run_dir}/debug-unauth.headers"
debug_unauth_status="$(http_request GET "${base_url}/debug/recent-requests" - "${debug_unauth_body}" "${debug_unauth_headers}" "debug-unauth")"
require_http_status "GET /debug/recent-requests unauthenticated" "${debug_unauth_status}" "401"
printf 'smoke-gb10: endpoint=/debug/recent-requests auth=missing status=%s result=rejected\n' \
    "${debug_unauth_status}"

debug_wrong_body="${run_dir}/debug-wrong.body.json"
debug_wrong_headers="${run_dir}/debug-wrong.headers"
debug_wrong_status="$(http_request GET "${base_url}/debug/recent-requests" - "${debug_wrong_body}" "${debug_wrong_headers}" "debug-wrong" false "application/json" "authorization: Bearer wrong-admin-token")"
require_http_status "GET /debug/recent-requests wrong token" "${debug_wrong_status}" "401"
printf 'smoke-gb10: endpoint=/debug/recent-requests auth=wrong status=%s result=rejected\n' \
    "${debug_wrong_status}"

debug_bearer_body="${run_dir}/debug-bearer.body.json"
debug_bearer_headers="${run_dir}/debug-bearer.headers"
debug_bearer_status="$(http_request GET "${base_url}/debug/recent-requests?limit=50" - "${debug_bearer_body}" "${debug_bearer_headers}" "debug-bearer" false "application/json" "authorization: Bearer ${ADMIN_TOKEN}")"
require_http_status "GET /debug/recent-requests bearer" "${debug_bearer_status}" "200"
debug_bearer_summary="$(validate_debug_summary "${debug_bearer_body}" "${ADMIN_TOKEN}")"
printf 'smoke-gb10: endpoint=/debug/recent-requests auth=bearer status=%s %s\n' \
    "${debug_bearer_status}" "${debug_bearer_summary}"

debug_header_body="${run_dir}/debug-header.body.json"
debug_header_headers="${run_dir}/debug-header.headers"
debug_header_status="$(http_request GET "${base_url}/debug/recent-requests" - "${debug_header_body}" "${debug_header_headers}" "debug-header" false "application/json" "x-admin-token: ${ADMIN_TOKEN}")"
require_http_status "GET /debug/recent-requests x-admin-token" "${debug_header_status}" "200"
debug_header_summary="$(validate_debug_summary "${debug_header_body}" "${ADMIN_TOKEN}")"
printf 'smoke-gb10: endpoint=/debug/recent-requests auth=x-admin-token status=%s %s\n' \
    "${debug_header_status}" "${debug_header_summary}"

observability_summary="$(verify_observability "${sqlite_path}" "${forwarded_calls}")"
printf 'smoke-gb10: observability %s\n' "${observability_summary}"
printf 'smoke-gb10: result=ok forwarded_calls=%s\n' "${forwarded_calls}"
