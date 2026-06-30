use std::{collections::BTreeMap, pin::Pin, time::Duration};

use axum::body::Bytes;
use bytes::BytesMut;
use futures_util::{Stream, StreamExt};
use llm_guard_proxy_core::{RawPayloads, ThinkingConfig, ToolRequestThinkingPolicy};
use serde_json::{Map, Number, Value, json};
use tokio::time::timeout;

use super::{MAX_PROXY_BODY_BYTES, sanitized_reqwest_error, unix_time_millis};

mod loop_guard;
pub(super) use loop_guard::{AggregationError, LoopInspectionContext};
use loop_guard::{LoopChannel, LoopDetector, observe_fragment};

/// Prepared upstream request body for shielded non-stream chat completion handling.
pub(super) struct PreparedChatRequest {
    upstream_body: Bytes,
    thinking_metadata: BTreeMap<String, String>,
}

impl PreparedChatRequest {
    pub(super) fn upstream_body(&self) -> Bytes {
        self.upstream_body.clone()
    }

    pub(super) fn thinking_metadata(&self) -> &BTreeMap<String, String> {
        &self.thinking_metadata
    }
}

/// Forces upstream streaming and usage frames for non-stream chat requests parsed as JSON.
pub(super) fn prepare_non_stream_request(
    body: &Bytes,
    thinking: &ThinkingConfig,
) -> Option<PreparedChatRequest> {
    let mut value = serde_json::from_slice::<Value>(body).ok()?;
    let object = value.as_object_mut()?;
    if let Some(stream) = object.get("stream") {
        if !matches!(stream, Value::Bool(false)) {
            return None;
        }
    }
    if object
        .get("stream_options")
        .is_some_and(|stream_options| !stream_options.is_object())
    {
        return None;
    }

    let thinking_metadata = apply_thinking_policy(object, thinking);
    object.insert(String::from("stream"), Value::Bool(true));
    let stream_options = object
        .entry(String::from("stream_options"))
        .or_insert_with(|| Value::Object(Map::new()));
    if let Value::Object(stream_options) = stream_options {
        stream_options.insert(String::from("include_usage"), Value::Bool(true));
    } else {
        *stream_options = json!({ "include_usage": true });
    }
    let upstream_body = serde_json::to_vec(&value).ok()?;

    Some(PreparedChatRequest {
        upstream_body: Bytes::from(upstream_body),
        thinking_metadata,
    })
}

/// Applies thinking-budget shielding to downstream streaming chat requests.
///
/// Streaming requests keep their streaming response contract; unlike
/// [`prepare_non_stream_request`], this does not force upstream usage frames or
/// route the response through non-stream aggregation.
pub(super) fn prepare_stream_request(
    body: &Bytes,
    thinking: &ThinkingConfig,
) -> Option<PreparedChatRequest> {
    let mut value = serde_json::from_slice::<Value>(body).ok()?;
    let object = value.as_object_mut()?;
    if !matches!(object.get("stream"), Some(Value::Bool(true))) {
        return None;
    }

    let thinking_metadata = apply_thinking_policy(object, thinking);
    let upstream_body = serde_json::to_vec(&value).ok()?;

    Some(PreparedChatRequest {
        upstream_body: Bytes::from(upstream_body),
        thinking_metadata,
    })
}

/// Returns a retry request body with a bounded anti-loop system hint.
///
/// The hint is deterministic and contains only proxy retry metadata; it never
/// copies raw prompt, output, reasoning, or upstream error text.
pub(super) fn body_with_anti_loop_retry_hint(
    body: &Bytes,
    attempt_number: u32,
    max_attempts: u32,
) -> Option<Bytes> {
    let mut value = serde_json::from_slice::<Value>(body).ok()?;
    let object = value.as_object_mut()?;
    let messages = object.get_mut("messages")?.as_array_mut()?;
    messages.insert(
        0,
        json!({
            "role": "system",
            "content": anti_loop_retry_hint(attempt_number, max_attempts),
        }),
    );
    serde_json::to_vec(&value).ok().map(Bytes::from)
}

fn anti_loop_retry_hint(attempt_number: u32, max_attempts: u32) -> String {
    format!(
        "llm-guard-proxy retry hint: a prior shielded upstream attempt was aborted by loop protection. Avoid repeating the same output pattern and provide a concise fresh answer. retry_attempt={attempt_number}/{max_attempts}."
    )
}

#[derive(Clone, Copy, Debug)]
struct JsonPath {
    path: &'static [&'static str],
    variant: &'static str,
}

#[derive(Clone, Copy, Debug)]
struct BudgetObservation {
    path: JsonPath,
    state: BudgetState,
}

#[derive(Clone, Debug, Default)]
struct BudgetObservations {
    entries: Vec<BudgetObservation>,
}

#[derive(Clone, Copy, Debug)]
enum BudgetState {
    Numeric(u64),
    Malformed,
}

#[derive(Clone, Copy, Debug)]
enum DisableMarker {
    None,
    Disabled(JsonPath),
    Malformed(JsonPath),
}

#[derive(Clone, Debug, Default)]
struct AnswerBudgetDecision {
    applied: bool,
    adjusted_fields: Vec<&'static str>,
    preserved_fields: Vec<&'static str>,
    malformed_fields: Vec<&'static str>,
    overflow_fields: Vec<&'static str>,
}

const CANONICAL_THINKING_BUDGET: JsonPath = JsonPath {
    path: &["thinking", "budget_tokens"],
    variant: "canonical",
};

const ROOT_THINKING_TOKEN_BUDGET: JsonPath = JsonPath {
    path: &["thinking_token_budget"],
    variant: "root-thinking-token-budget",
};

const ROOT_THINKING_BUDGET: JsonPath = JsonPath {
    path: &["thinking_budget"],
    variant: "root-thinking-budget",
};

const ROOT_CHAT_TEMPLATE_THINKING_BUDGET: JsonPath = JsonPath {
    path: &["chat_template_kwargs", "thinking_budget"],
    variant: "chat-template-kwargs",
};

const EXTRA_BODY_THINKING_BUDGET: JsonPath = JsonPath {
    path: &["extra_body", "thinking_budget"],
    variant: "extra-body-thinking-budget",
};

const EXTRA_BODY_THINKING_TOKEN_BUDGET: JsonPath = JsonPath {
    path: &["extra_body", "thinking_token_budget"],
    variant: "extra-body-thinking-token-budget",
};

const EXTRA_BODY_CANONICAL_THINKING_BUDGET: JsonPath = JsonPath {
    path: &["extra_body", "thinking", "budget_tokens"],
    variant: "extra-body-canonical",
};

const EXTRA_BODY_CHAT_TEMPLATE_THINKING_BUDGET: JsonPath = JsonPath {
    path: &["extra_body", "chat_template_kwargs", "thinking_budget"],
    variant: "extra-body-chat-template-kwargs",
};

const BUDGET_PATHS: &[JsonPath] = &[
    CANONICAL_THINKING_BUDGET,
    ROOT_THINKING_TOKEN_BUDGET,
    ROOT_THINKING_BUDGET,
    ROOT_CHAT_TEMPLATE_THINKING_BUDGET,
    EXTRA_BODY_THINKING_TOKEN_BUDGET,
    EXTRA_BODY_THINKING_BUDGET,
    EXTRA_BODY_CANONICAL_THINKING_BUDGET,
    EXTRA_BODY_CHAT_TEMPLATE_THINKING_BUDGET,
];

const ROOT_ENABLE_THINKING: JsonPath = JsonPath {
    path: &["enable_thinking"],
    variant: "root-enable-thinking",
};

const THINKING_ENABLE_THINKING: JsonPath = JsonPath {
    path: &["thinking", "enable_thinking"],
    variant: "canonical-enable-thinking",
};

const THINKING_ENABLED: JsonPath = JsonPath {
    path: &["thinking", "enabled"],
    variant: "canonical-thinking-enabled",
};

const ROOT_CHAT_TEMPLATE_ENABLE_THINKING: JsonPath = JsonPath {
    path: &["chat_template_kwargs", "enable_thinking"],
    variant: "chat-template-kwargs-enable-thinking",
};

const EXTRA_BODY_ENABLE_THINKING: JsonPath = JsonPath {
    path: &["extra_body", "enable_thinking"],
    variant: "extra-body-enable-thinking",
};

const EXTRA_BODY_THINKING_ENABLE_THINKING: JsonPath = JsonPath {
    path: &["extra_body", "thinking", "enable_thinking"],
    variant: "extra-body-canonical-enable-thinking",
};

const EXTRA_BODY_THINKING_ENABLED: JsonPath = JsonPath {
    path: &["extra_body", "thinking", "enabled"],
    variant: "extra-body-canonical-thinking-enabled",
};

const EXTRA_BODY_CHAT_TEMPLATE_ENABLE_THINKING: JsonPath = JsonPath {
    path: &["extra_body", "chat_template_kwargs", "enable_thinking"],
    variant: "extra-body-chat-template-kwargs-enable-thinking",
};

const DISABLE_MARKER_PATHS: &[JsonPath] = &[
    ROOT_ENABLE_THINKING,
    THINKING_ENABLE_THINKING,
    THINKING_ENABLED,
    ROOT_CHAT_TEMPLATE_ENABLE_THINKING,
    EXTRA_BODY_ENABLE_THINKING,
    EXTRA_BODY_THINKING_ENABLE_THINKING,
    EXTRA_BODY_THINKING_ENABLED,
    EXTRA_BODY_CHAT_TEMPLATE_ENABLE_THINKING,
];

const ANSWER_BUDGET_FIELDS: &[&str] = &["max_tokens", "max_completion_tokens", "max_output_tokens"];

#[derive(Debug)]
struct ThinkingPolicyOutcome {
    rewrite_applied: bool,
    reason: &'static str,
    final_budget: String,
    answer_budget_delta: u64,
    rewritten_paths: Vec<JsonPath>,
    preserved_paths: Vec<JsonPath>,
    malformed_paths: Vec<JsonPath>,
    zero_paths: Vec<JsonPath>,
}

fn apply_thinking_policy(
    object: &mut Map<String, Value>,
    thinking: &ThinkingConfig,
) -> BTreeMap<String, String> {
    let configured_budget = u64::from(thinking.budget_tokens);
    let budget_observations = find_budget_observations(object);
    let disable_marker = find_disable_marker(object);
    let is_tool_request = is_tool_use_request(object);
    let mut metadata = initial_thinking_metadata(thinking, configured_budget, &budget_observations);
    metadata.insert(
        String::from("thinking_tool_request_detected"),
        is_tool_request.to_string(),
    );
    if thinking.force_disable {
        let outcome = apply_force_disable_thinking(object, &budget_observations, &mut metadata);
        let answer_budget =
            apply_answer_budget_preservation(object, false, outcome.answer_budget_delta);
        metadata.extend(thinking_outcome_metadata(&outcome, &answer_budget));
        return metadata;
    }
    if is_tool_request
        && matches!(
            thinking.tool_request_policy,
            ToolRequestThinkingPolicy::Passthrough
        )
    {
        let outcome = thinking_noop("tool_request_passthrough", &budget_observations);
        metadata.extend(thinking_outcome_metadata(
            &outcome,
            &AnswerBudgetDecision::default(),
        ));
        return metadata;
    }
    let outcome = apply_thinking_budget_policy(
        object,
        thinking,
        configured_budget,
        &budget_observations,
        disable_marker,
        &mut metadata,
    );
    let answer_budget = apply_answer_budget_preservation(
        object,
        thinking.preserve_answer_budget,
        outcome.answer_budget_delta,
    );

    metadata.extend(thinking_outcome_metadata(&outcome, &answer_budget));
    metadata
}

fn is_tool_use_request(object: &Map<String, Value>) -> bool {
    non_empty_array(object.get("tools"))
        || non_empty_array(object.get("functions"))
        || present_tool_selector(object.get("tool_choice"))
        || present_tool_selector(object.get("function_call"))
}

fn non_empty_array(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::Array(values)) if !values.is_empty())
}

fn present_tool_selector(value: Option<&Value>) -> bool {
    match value {
        Some(Value::String(selector)) => !selector.trim().eq_ignore_ascii_case("none"),
        Some(Value::Null | Value::Bool(false)) | None => false,
        Some(_) => true,
    }
}

fn initial_thinking_metadata(
    thinking: &ThinkingConfig,
    configured_budget: u64,
    budget_observations: &BudgetObservations,
) -> BTreeMap<String, String> {
    BTreeMap::from([
        (
            String::from("thinking_policy_enabled"),
            thinking.enabled.to_string(),
        ),
        (
            String::from("thinking_force_disable_enabled"),
            thinking.force_disable.to_string(),
        ),
        (
            String::from("thinking_policy_budget_tokens"),
            configured_budget.to_string(),
        ),
        (
            String::from("thinking_preserve_answer_budget_enabled"),
            thinking.preserve_answer_budget.to_string(),
        ),
        (
            String::from("thinking_tool_request_policy"),
            thinking.tool_request_policy.as_str().to_owned(),
        ),
        (
            String::from("thinking_budget_previous_state"),
            previous_budget_state(budget_observations, configured_budget),
        ),
        (
            String::from("thinking_budget_previous_tokens"),
            previous_budget_tokens(budget_observations),
        ),
        (
            String::from("thinking_schema_path"),
            budget_observations.single_path().map_or_else(
                || {
                    if budget_observations.is_empty() {
                        String::from("none")
                    } else {
                        String::from("multiple")
                    }
                },
                JsonPath::display_path,
            ),
        ),
        (
            String::from("thinking_schema_variant"),
            budget_observations.single_path().map_or_else(
                || {
                    if budget_observations.is_empty() {
                        String::from("none")
                    } else {
                        String::from("multiple")
                    }
                },
                |path| path.variant.to_owned(),
            ),
        ),
        (
            String::from("thinking_budget_observed_paths"),
            observed_budget_paths(budget_observations, configured_budget),
        ),
    ])
}

fn apply_thinking_budget_policy(
    object: &mut Map<String, Value>,
    thinking: &ThinkingConfig,
    configured_budget: u64,
    budget_observations: &BudgetObservations,
    disable_marker: DisableMarker,
    metadata: &mut BTreeMap<String, String>,
) -> ThinkingPolicyOutcome {
    if !thinking.enabled {
        return thinking_noop("policy_disabled", budget_observations);
    }
    if configured_budget == 0 {
        return thinking_noop("configured_budget_zero", budget_observations);
    }
    match disable_marker {
        DisableMarker::Disabled(path) => {
            insert_disable_marker_metadata(metadata, path);
            thinking_noop("caller_disabled_thinking", budget_observations)
        }
        DisableMarker::Malformed(path) => {
            insert_disable_marker_metadata(metadata, path);
            thinking_noop("malformed_disable_marker", budget_observations)
        }
        DisableMarker::None => {
            apply_budget_observations(object, configured_budget, budget_observations, metadata)
        }
    }
}

fn apply_force_disable_thinking(
    object: &mut Map<String, Value>,
    budget_observations: &BudgetObservations,
    metadata: &mut BTreeMap<String, String>,
) -> ThinkingPolicyOutcome {
    let disable_marker_paths = normalize_force_disable_markers(object, metadata);
    metadata.insert(
        String::from("thinking_disable_marker_rewritten_paths"),
        join_paths(&disable_marker_paths),
    );

    if budget_observations.is_empty() {
        let path = injection_path(object);
        metadata.insert(String::from("thinking_schema_path"), path.display_path());
        metadata.insert(
            String::from("thinking_schema_variant"),
            path.variant.to_owned(),
        );
        if set_budget_at_path(object, path, 0) {
            return ThinkingPolicyOutcome {
                rewrite_applied: true,
                reason: "force_disabled_thinking",
                final_budget: String::from("0"),
                answer_budget_delta: 0,
                rewritten_paths: vec![path],
                preserved_paths: Vec::new(),
                malformed_paths: Vec::new(),
                zero_paths: Vec::new(),
            };
        }
        return ThinkingPolicyOutcome {
            rewrite_applied: false,
            reason: "malformed_budget_container",
            final_budget: String::from("unknown"),
            answer_budget_delta: 0,
            rewritten_paths: Vec::new(),
            preserved_paths: Vec::new(),
            malformed_paths: Vec::new(),
            zero_paths: Vec::new(),
        };
    }

    let mut rewritten_paths = Vec::new();
    let mut preserved_paths = Vec::new();
    let mut malformed_paths = Vec::new();
    let mut zero_paths = Vec::new();
    for observation in &budget_observations.entries {
        match observation.state {
            BudgetState::Numeric(0) => {
                preserved_paths.push(observation.path);
                zero_paths.push(observation.path);
            }
            BudgetState::Numeric(_) | BudgetState::Malformed => {
                if set_budget_at_path(object, observation.path, 0) {
                    rewritten_paths.push(observation.path);
                } else {
                    malformed_paths.push(observation.path);
                }
            }
        }
    }

    let final_budget = if rewritten_paths.is_empty() && preserved_paths.is_empty() {
        String::from("unknown")
    } else {
        String::from("0")
    };
    ThinkingPolicyOutcome {
        rewrite_applied: !rewritten_paths.is_empty() || !disable_marker_paths.is_empty(),
        reason: "force_disabled_thinking",
        final_budget,
        answer_budget_delta: 0,
        rewritten_paths,
        preserved_paths,
        malformed_paths,
        zero_paths,
    }
}

fn apply_budget_observations(
    object: &mut Map<String, Value>,
    configured_budget: u64,
    budget_observations: &BudgetObservations,
    metadata: &mut BTreeMap<String, String>,
) -> ThinkingPolicyOutcome {
    if budget_observations.is_empty() {
        return inject_missing_budget(object, configured_budget, metadata);
    }

    let zero_paths = budget_observations.zero_paths();
    if !zero_paths.is_empty() {
        return ThinkingPolicyOutcome {
            rewrite_applied: false,
            reason: "existing_budget_zero",
            final_budget: final_budget_tokens(budget_observations, configured_budget),
            answer_budget_delta: 0,
            rewritten_paths: Vec::new(),
            preserved_paths: budget_observations.non_malformed_paths(),
            malformed_paths: budget_observations.malformed_paths(),
            zero_paths,
        };
    }

    let mut rewritten_paths = Vec::new();
    let mut preserved_paths = Vec::new();
    let mut malformed_paths = Vec::new();
    let mut answer_budget_delta = 0;
    for observation in &budget_observations.entries {
        match observation.state {
            BudgetState::Malformed => malformed_paths.push(observation.path),
            BudgetState::Numeric(existing_budget) if existing_budget < configured_budget => {
                if !set_budget_at_path(object, observation.path, configured_budget) {
                    return malformed_budget_container_outcome(
                        budget_observations,
                        configured_budget,
                    );
                }
                rewritten_paths.push(observation.path);
                answer_budget_delta =
                    answer_budget_delta.max(configured_budget.saturating_sub(existing_budget));
            }
            BudgetState::Numeric(_existing_budget) => preserved_paths.push(observation.path),
        }
    }

    if !rewritten_paths.is_empty() {
        return ThinkingPolicyOutcome {
            rewrite_applied: true,
            reason: "raised_smaller_budget",
            final_budget: final_budget_tokens(budget_observations, configured_budget),
            answer_budget_delta,
            rewritten_paths,
            preserved_paths,
            malformed_paths,
            zero_paths: Vec::new(),
        };
    }

    if !preserved_paths.is_empty() {
        return ThinkingPolicyOutcome {
            rewrite_applied: false,
            reason: "preserved_equal_or_larger_budget",
            final_budget: final_budget_tokens(budget_observations, configured_budget),
            answer_budget_delta: 0,
            rewritten_paths,
            preserved_paths,
            malformed_paths,
            zero_paths: Vec::new(),
        };
    }

    ThinkingPolicyOutcome {
        rewrite_applied: false,
        reason: "malformed_existing_budget",
        final_budget: final_budget_tokens(budget_observations, configured_budget),
        answer_budget_delta: 0,
        rewritten_paths,
        preserved_paths,
        malformed_paths,
        zero_paths: Vec::new(),
    }
}

fn inject_missing_budget(
    object: &mut Map<String, Value>,
    configured_budget: u64,
    metadata: &mut BTreeMap<String, String>,
) -> ThinkingPolicyOutcome {
    let path = injection_path(object);
    metadata.insert(String::from("thinking_schema_path"), path.display_path());
    metadata.insert(
        String::from("thinking_schema_variant"),
        path.variant.to_owned(),
    );
    if set_budget_at_path(object, path, configured_budget) {
        return ThinkingPolicyOutcome {
            rewrite_applied: true,
            reason: "injected_missing_budget",
            final_budget: configured_budget.to_string(),
            answer_budget_delta: configured_budget,
            rewritten_paths: vec![path],
            preserved_paths: Vec::new(),
            malformed_paths: Vec::new(),
            zero_paths: Vec::new(),
        };
    }
    ThinkingPolicyOutcome {
        rewrite_applied: false,
        reason: "malformed_budget_container",
        final_budget: String::from("unknown"),
        answer_budget_delta: 0,
        rewritten_paths: Vec::new(),
        preserved_paths: Vec::new(),
        malformed_paths: Vec::new(),
        zero_paths: Vec::new(),
    }
}

fn malformed_budget_container_outcome(
    budget_observations: &BudgetObservations,
    configured_budget: u64,
) -> ThinkingPolicyOutcome {
    ThinkingPolicyOutcome {
        rewrite_applied: false,
        reason: "malformed_budget_container",
        final_budget: final_budget_tokens(budget_observations, configured_budget),
        answer_budget_delta: 0,
        rewritten_paths: Vec::new(),
        preserved_paths: Vec::new(),
        malformed_paths: budget_observations.malformed_paths(),
        zero_paths: budget_observations.zero_paths(),
    }
}

fn thinking_noop(
    reason: &'static str,
    budget_observations: &BudgetObservations,
) -> ThinkingPolicyOutcome {
    ThinkingPolicyOutcome {
        rewrite_applied: false,
        reason,
        final_budget: current_budget_tokens(budget_observations),
        answer_budget_delta: 0,
        rewritten_paths: Vec::new(),
        preserved_paths: budget_observations.non_malformed_paths(),
        malformed_paths: budget_observations.malformed_paths(),
        zero_paths: budget_observations.zero_paths(),
    }
}

fn insert_disable_marker_metadata(metadata: &mut BTreeMap<String, String>, path: JsonPath) {
    metadata.insert(
        String::from("thinking_disable_marker_path"),
        path.display_path(),
    );
    metadata.insert(
        String::from("thinking_disable_marker_variant"),
        path.variant.to_owned(),
    );
}

fn insert_multiple_disable_marker_metadata(metadata: &mut BTreeMap<String, String>) {
    metadata.insert(
        String::from("thinking_disable_marker_path"),
        String::from("multiple"),
    );
    metadata.insert(
        String::from("thinking_disable_marker_variant"),
        String::from("multiple"),
    );
}

fn thinking_outcome_metadata(
    outcome: &ThinkingPolicyOutcome,
    answer_budget: &AnswerBudgetDecision,
) -> BTreeMap<String, String> {
    BTreeMap::from([
        (
            String::from("thinking_rewrite_applied"),
            outcome.rewrite_applied.to_string(),
        ),
        (
            String::from("thinking_rewrite_reason"),
            outcome.reason.to_owned(),
        ),
        (
            String::from("thinking_budget_final_tokens"),
            outcome.final_budget.clone(),
        ),
        (
            String::from("thinking_answer_budget_delta_tokens"),
            outcome.answer_budget_delta.to_string(),
        ),
        (
            String::from("thinking_answer_budget_preservation_applied"),
            answer_budget.applied.to_string(),
        ),
        (
            String::from("thinking_answer_budget_adjusted_fields"),
            join_fields(&answer_budget.adjusted_fields),
        ),
        (
            String::from("thinking_answer_budget_preserved_fields"),
            join_fields(&answer_budget.preserved_fields),
        ),
        (
            String::from("thinking_answer_budget_malformed_fields"),
            join_fields(&answer_budget.malformed_fields),
        ),
        (
            String::from("thinking_answer_budget_overflow_fields"),
            join_fields(&answer_budget.overflow_fields),
        ),
        (
            String::from("thinking_budget_rewritten_paths"),
            join_paths(&outcome.rewritten_paths),
        ),
        (
            String::from("thinking_budget_preserved_paths"),
            join_paths(&outcome.preserved_paths),
        ),
        (
            String::from("thinking_budget_malformed_paths"),
            join_paths(&outcome.malformed_paths),
        ),
        (
            String::from("thinking_budget_zero_paths"),
            join_paths(&outcome.zero_paths),
        ),
    ])
}

fn find_budget_observations(object: &Map<String, Value>) -> BudgetObservations {
    let mut entries = Vec::new();
    for path in BUDGET_PATHS {
        match value_at_path(object, path.path) {
            PathValue::Missing => {}
            PathValue::Malformed => {
                entries.push(BudgetObservation {
                    path: *path,
                    state: BudgetState::Malformed,
                });
            }
            PathValue::Value(value) => {
                entries.push(BudgetObservation {
                    path: *path,
                    state: token_budget_value(value),
                });
            }
        }
    }
    BudgetObservations { entries }
}

fn find_disable_marker(object: &Map<String, Value>) -> DisableMarker {
    let mut malformed = None;
    for path in DISABLE_MARKER_PATHS {
        match value_at_path(object, path.path) {
            PathValue::Malformed => {
                malformed.get_or_insert(*path);
            }
            PathValue::Value(Value::Bool(false)) => return DisableMarker::Disabled(*path),
            PathValue::Missing | PathValue::Value(Value::Bool(true)) => {}
            PathValue::Value(_value) => {
                malformed.get_or_insert(*path);
            }
        }
    }

    malformed.map_or(DisableMarker::None, DisableMarker::Malformed)
}

enum PathValue<'a> {
    Missing,
    Malformed,
    Value(&'a Value),
}

fn value_at_path<'a>(object: &'a Map<String, Value>, path: &[&str]) -> PathValue<'a> {
    let Some((&last, parents)) = path.split_last() else {
        return PathValue::Missing;
    };
    let mut current = object;
    for key in parents {
        match current.get(*key) {
            Some(Value::Object(next)) => current = next,
            Some(_value) => return PathValue::Malformed,
            None => return PathValue::Missing,
        }
    }
    current
        .get(last)
        .map_or(PathValue::Missing, PathValue::Value)
}

fn token_budget_value(value: &Value) -> BudgetState {
    match value {
        Value::Number(number) => number
            .as_u64()
            .map_or(BudgetState::Malformed, BudgetState::Numeric),
        _ => BudgetState::Malformed,
    }
}

fn injection_path(object: &Map<String, Value>) -> JsonPath {
    if object_at_path(object, &["chat_template_kwargs"]) {
        return ROOT_CHAT_TEMPLATE_THINKING_BUDGET;
    }
    if object_at_path(object, &["extra_body", "chat_template_kwargs"]) {
        return EXTRA_BODY_CHAT_TEMPLATE_THINKING_BUDGET;
    }
    if object_at_path(object, &["extra_body", "thinking"]) {
        return EXTRA_BODY_CANONICAL_THINKING_BUDGET;
    }
    CANONICAL_THINKING_BUDGET
}

fn force_disable_marker_path(object: &Map<String, Value>) -> JsonPath {
    if object_at_path(object, &["chat_template_kwargs"]) {
        return ROOT_CHAT_TEMPLATE_ENABLE_THINKING;
    }
    if object_at_path(object, &["extra_body", "chat_template_kwargs"]) {
        return EXTRA_BODY_CHAT_TEMPLATE_ENABLE_THINKING;
    }
    if object_at_path(object, &["extra_body", "thinking"]) {
        return EXTRA_BODY_THINKING_ENABLED;
    }
    if object_at_path(object, &["thinking"]) {
        return THINKING_ENABLED;
    }
    if object_at_path(object, &["extra_body"]) {
        return EXTRA_BODY_ENABLE_THINKING;
    }
    THINKING_ENABLED
}

fn object_at_path(object: &Map<String, Value>, path: &[&str]) -> bool {
    let mut current = object;
    for key in path {
        match current.get(*key) {
            Some(Value::Object(next)) => current = next,
            Some(_) | None => return false,
        }
    }
    true
}

fn normalize_force_disable_markers(
    object: &mut Map<String, Value>,
    metadata: &mut BTreeMap<String, String>,
) -> Vec<JsonPath> {
    let mut rewritten_paths = Vec::new();
    let mut disabled_marker_present = false;
    for path in DISABLE_MARKER_PATHS {
        match value_at_path(object, path.path) {
            PathValue::Value(Value::Bool(false)) => disabled_marker_present = true,
            PathValue::Value(Value::Bool(true)) => {
                if set_bool_at_path(object, *path, false) {
                    rewritten_paths.push(*path);
                }
            }
            PathValue::Missing | PathValue::Malformed | PathValue::Value(_) => {}
        }
    }

    if rewritten_paths.is_empty() && !disabled_marker_present {
        let path = force_disable_marker_path(object);
        if set_bool_at_path(object, path, false) {
            rewritten_paths.push(path);
        }
    }

    match rewritten_paths.as_slice() {
        [] => {}
        [path] => insert_disable_marker_metadata(metadata, *path),
        [_first, ..] => insert_multiple_disable_marker_metadata(metadata),
    }

    rewritten_paths
}

fn set_budget_at_path(object: &mut Map<String, Value>, path: JsonPath, budget: u64) -> bool {
    let Some((&last, parents)) = path.path.split_last() else {
        return false;
    };
    let mut current = object;
    for key in parents {
        let entry = current
            .entry((*key).to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        let Value::Object(next) = entry else {
            return false;
        };
        current = next;
    }
    current.insert(last.to_owned(), Value::Number(Number::from(budget)));
    true
}

fn set_bool_at_path(object: &mut Map<String, Value>, path: JsonPath, value: bool) -> bool {
    let Some((&last, parents)) = path.path.split_last() else {
        return false;
    };
    let mut current = object;
    for key in parents {
        let entry = current
            .entry((*key).to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        let Value::Object(next) = entry else {
            return false;
        };
        current = next;
    }
    current.insert(last.to_owned(), Value::Bool(value));
    true
}

fn apply_answer_budget_preservation(
    object: &mut Map<String, Value>,
    preserve_answer_budget: bool,
    delta: u64,
) -> AnswerBudgetDecision {
    let mut decision = AnswerBudgetDecision::default();
    for field in ANSWER_BUDGET_FIELDS {
        let Some(value) = object.get_mut(*field) else {
            continue;
        };
        if delta == 0 || !preserve_answer_budget {
            decision.preserved_fields.push(field);
            continue;
        }
        let Some(existing) = value.as_u64() else {
            decision.malformed_fields.push(field);
            continue;
        };
        let Some(adjusted) = existing.checked_add(delta) else {
            decision.overflow_fields.push(field);
            continue;
        };
        *value = Value::Number(Number::from(adjusted));
        decision.adjusted_fields.push(field);
        decision.applied = true;
    }
    decision
}

fn previous_budget_state(observations: &BudgetObservations, configured_budget: u64) -> String {
    if observations.is_empty() {
        return String::from("absent");
    }
    let mut states = observations
        .entries
        .iter()
        .map(|observation| budget_state_label(observation.state, configured_budget))
        .collect::<Vec<_>>();
    states.sort_unstable();
    states.dedup();
    if states.len() == 1 {
        states[0].to_owned()
    } else {
        String::from("mixed")
    }
}

fn previous_budget_tokens(observations: &BudgetObservations) -> String {
    if observations.is_empty() {
        return String::from("absent");
    }
    if observations.entries.len() > 1 {
        return String::from("multiple");
    }
    match observations.entries[0].state {
        BudgetState::Malformed => String::from("malformed"),
        BudgetState::Numeric(value) => value.to_string(),
    }
}

fn final_budget_tokens(observations: &BudgetObservations, configured_budget: u64) -> String {
    budget_tokens_summary(observations, |value| {
        if value > 0 && value < configured_budget {
            configured_budget
        } else {
            value
        }
    })
}

fn current_budget_tokens(observations: &BudgetObservations) -> String {
    budget_tokens_summary(observations, |value| value)
}

fn budget_tokens_summary(
    observations: &BudgetObservations,
    map_numeric: impl Fn(u64) -> u64,
) -> String {
    if observations.is_empty() {
        return String::from("absent");
    }
    let mut values = observations
        .entries
        .iter()
        .filter_map(|observation| match observation.state {
            BudgetState::Malformed => None,
            BudgetState::Numeric(value) => Some(map_numeric(value)),
        })
        .collect::<Vec<_>>();
    values.sort_unstable();
    values.dedup();
    match values.len() {
        0 => String::from("unknown"),
        1 => values[0].to_string(),
        _ => String::from("multiple"),
    }
}

fn budget_state_label(state: BudgetState, configured_budget: u64) -> &'static str {
    match state {
        BudgetState::Malformed => "malformed",
        BudgetState::Numeric(0) => "zero",
        BudgetState::Numeric(existing) if existing < configured_budget => "smaller",
        BudgetState::Numeric(existing) if existing == configured_budget => "equal",
        BudgetState::Numeric(_existing) => "larger",
    }
}

fn join_fields(fields: &[&str]) -> String {
    if fields.is_empty() {
        String::from("none")
    } else {
        fields.join(",")
    }
}

fn join_paths(paths: &[JsonPath]) -> String {
    if paths.is_empty() {
        String::from("none")
    } else {
        paths
            .iter()
            .map(|path| path.display_path())
            .collect::<Vec<_>>()
            .join(",")
    }
}

fn observed_budget_paths(observations: &BudgetObservations, configured_budget: u64) -> String {
    if observations.is_empty() {
        return String::from("none");
    }
    observations
        .entries
        .iter()
        .map(|observation| {
            format!(
                "{}={}",
                observation.path.display_path(),
                budget_state_label(observation.state, configured_budget)
            )
        })
        .collect::<Vec<_>>()
        .join(",")
}

impl JsonPath {
    fn display_path(self) -> String {
        self.path.join(".")
    }
}

impl BudgetObservations {
    fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    fn single_path(&self) -> Option<JsonPath> {
        if self.entries.len() == 1 {
            Some(self.entries[0].path)
        } else {
            None
        }
    }

    fn zero_paths(&self) -> Vec<JsonPath> {
        self.entries
            .iter()
            .filter_map(|observation| match observation.state {
                BudgetState::Numeric(0) => Some(observation.path),
                BudgetState::Numeric(_) | BudgetState::Malformed => None,
            })
            .collect()
    }

    fn malformed_paths(&self) -> Vec<JsonPath> {
        self.entries
            .iter()
            .filter_map(|observation| match observation.state {
                BudgetState::Malformed => Some(observation.path),
                BudgetState::Numeric(_) => None,
            })
            .collect()
    }

    fn non_malformed_paths(&self) -> Vec<JsonPath> {
        self.entries
            .iter()
            .filter_map(|observation| match observation.state {
                BudgetState::Numeric(_) => Some(observation.path),
                BudgetState::Malformed => None,
            })
            .collect()
    }
}

/// Accepted, aggregated OpenAI-compatible chat completion response.
pub(super) struct AggregatedChatCompletion {
    pub(super) body: Bytes,
    pub(super) response_metadata: BTreeMap<String, String>,
    pub(super) raw_payloads: RawPayloads,
}

/// Consumes upstream OpenAI-compatible chat completion SSE and aggregates it into JSON.
pub(super) async fn aggregate_stream(
    stream: impl Stream<Item = Result<Bytes, reqwest::Error>>,
    attempt_started_at_unix_ms: u64,
    request_id: &str,
    request_model_id: Option<&str>,
    loop_context: LoopInspectionContext,
    upstream_idle_timeout: Option<Duration>,
) -> Result<AggregatedChatCompletion, AggregationError> {
    let mut stream = Box::pin(stream);
    let mut buffer = BytesMut::new();
    let mut bytes_seen = 0_usize;
    let mut state = ChatAggregation::new(attempt_started_at_unix_ms, &loop_context);

    while let Some(chunk) = next_stream_chunk(&mut stream, upstream_idle_timeout).await? {
        let chunk = chunk.map_err(|error| {
            AggregationError::plain(format!(
                "upstream SSE stream failed: {}",
                sanitized_reqwest_error(&error)
            ))
        })?;
        if state.first_byte_latency_ms.is_none() {
            state.first_byte_latency_ms =
                Some(unix_time_millis().saturating_sub(attempt_started_at_unix_ms));
        }
        bytes_seen = bytes_seen
            .checked_add(chunk.len())
            .ok_or_else(|| AggregationError::plain("upstream SSE body is too large"))?;
        if bytes_seen > MAX_PROXY_BODY_BYTES {
            return Err(AggregationError::plain(format!(
                "upstream SSE body exceeded proxy limit: max_bytes={MAX_PROXY_BODY_BYTES}"
            )));
        }
        buffer.extend_from_slice(&chunk);

        while let Some(event) = next_sse_event(&mut buffer) {
            state.apply_event(&event)?;
        }
    }

    if buffer.iter().any(|byte| !byte.is_ascii_whitespace()) {
        return Err(AggregationError::plain(
            "upstream SSE ended with an unterminated event",
        ));
    }

    state.finish(request_id, request_model_id)
}

async fn next_stream_chunk(
    stream: &mut Pin<Box<impl Stream<Item = Result<Bytes, reqwest::Error>>>>,
    upstream_idle_timeout: Option<Duration>,
) -> Result<Option<Result<Bytes, reqwest::Error>>, AggregationError> {
    let Some(upstream_idle_timeout) = upstream_idle_timeout else {
        return Ok(stream.next().await);
    };
    match timeout(upstream_idle_timeout, stream.next()).await {
        Ok(next_chunk) => Ok(next_chunk),
        Err(_elapsed) => Err(AggregationError::upstream_stall(
            upstream_idle_timeout
                .as_millis()
                .try_into()
                .unwrap_or(u64::MAX),
        )),
    }
}

fn next_sse_event(buffer: &mut BytesMut) -> Option<Vec<u8>> {
    let lf_lf = buffer
        .windows(2)
        .position(|window| window == b"\n\n")
        .map(|position| (position, position + 2));
    let crlf_crlf = buffer
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|position| (position, position + 4));
    let (event_end, drain_to) = match (lf_lf, crlf_crlf) {
        (Some(lf), Some(crlf)) => {
            if lf.0 < crlf.0 {
                lf
            } else {
                crlf
            }
        }
        (Some(lf), None) => lf,
        (None, Some(crlf)) => crlf,
        (None, None) => return None,
    };

    let frame = buffer.split_to(drain_to);
    Some(frame[..event_end].to_vec())
}

#[derive(Default)]
struct ChatAggregation {
    attempt_started_at_unix_ms: u64,
    loop_detector: Option<LoopDetector>,
    id: Option<String>,
    created: Option<u64>,
    model: Option<String>,
    service_tier: Option<Value>,
    system_fingerprint: Option<String>,
    usage: Option<Value>,
    extension_fields: Map<String, Value>,
    choices: BTreeMap<u64, ChoiceBuilder>,
    saw_done: bool,
    saw_finish_reason: bool,
    first_byte_latency_ms: Option<u64>,
    first_token_latency_ms: Option<u64>,
    stats: DeltaStats,
}

impl ChatAggregation {
    fn new(attempt_started_at_unix_ms: u64, loop_context: &LoopInspectionContext) -> Self {
        Self {
            attempt_started_at_unix_ms,
            loop_detector: loop_context.detector(),
            ..Self::default()
        }
    }

    fn apply_event(&mut self, event: &[u8]) -> Result<(), AggregationError> {
        let event = std::str::from_utf8(event).map_err(|error| {
            AggregationError::plain(format!("upstream SSE event was not valid UTF-8: {error}"))
        })?;
        let data = sse_data(event);
        if data.is_empty() {
            return Ok(());
        }
        if data.trim() == "[DONE]" {
            self.saw_done = true;
            return Ok(());
        }

        let chunk = serde_json::from_str::<Value>(&data).map_err(|error| {
            AggregationError::plain(format!("upstream SSE data was not valid JSON: {error}"))
        })?;
        self.apply_chunk(&chunk)?;
        Ok(())
    }

    fn apply_chunk(&mut self, chunk: &Value) -> Result<(), AggregationError> {
        let Some(chunk_object) = chunk.as_object() else {
            return Ok(());
        };
        copy_extension_fields(
            chunk_object,
            &mut self.extension_fields,
            is_completion_owned_field,
        );

        if let Some(id) = string_field(chunk, "id") {
            self.id.get_or_insert_with(|| id.to_owned());
        }
        if let Some(created) = chunk.get("created").and_then(Value::as_u64) {
            self.created.get_or_insert(created);
        }
        if let Some(model) = string_field(chunk, "model") {
            self.model.get_or_insert_with(|| model.to_owned());
        }
        if let Some(service_tier) = chunk.get("service_tier") {
            self.service_tier
                .get_or_insert_with(|| service_tier.clone());
        }
        if let Some(system_fingerprint) = string_field(chunk, "system_fingerprint") {
            self.system_fingerprint
                .get_or_insert_with(|| system_fingerprint.to_owned());
        }
        if let Some(usage) = chunk.get("usage").filter(|value| !value.is_null()) {
            self.usage = Some(usage.clone());
        }

        let Some(choices) = chunk_object.get("choices").and_then(Value::as_array) else {
            return Ok(());
        };

        for choice in choices {
            let index = choice.get("index").and_then(Value::as_u64).unwrap_or(0);
            let builder = self.choices.entry(index).or_insert_with(|| ChoiceBuilder {
                index,
                ..ChoiceBuilder::default()
            });
            if let Some(choice_object) = choice.as_object() {
                copy_extension_fields(
                    choice_object,
                    &mut builder.extension_fields,
                    is_choice_owned_field,
                );
            }
            if let Some(finish_reason) =
                choice.get("finish_reason").filter(|value| !value.is_null())
            {
                builder.finish_reason = Some(finish_reason.clone());
                if self.stats.finish_reason.is_none() {
                    self.stats.finish_reason = finish_reason.as_str().map(str::to_owned);
                }
                self.saw_finish_reason = true;
            }
            if let Some(logprobs) = choice.get("logprobs").filter(|value| !value.is_null()) {
                builder.merge_logprobs(logprobs);
            }
            if let Some(delta) = choice.get("delta").and_then(Value::as_object) {
                self.stats.delta_count = self.stats.delta_count.saturating_add(1);
                builder.apply_delta(
                    delta,
                    &mut self.stats,
                    &mut self.first_token_latency_ms,
                    self.attempt_started_at_unix_ms,
                    &mut self.loop_detector,
                )?;
            }
        }
        Ok(())
    }

    fn finish(
        self,
        request_id: &str,
        request_model_id: Option<&str>,
    ) -> Result<AggregatedChatCompletion, AggregationError> {
        self.ensure_usable()?;
        let response_metadata = response_metadata(&self);
        let completion_fields =
            CompletionFields::from_aggregation(&self, request_id, request_model_id);
        let finalized_choices = finalize_choices(self.choices);
        let raw_payloads = finalized_choices.raw_payloads();
        let body = completion_body(completion_fields, finalized_choices.choices)
            .map_err(AggregationError::plain)?;

        Ok(AggregatedChatCompletion {
            body: Bytes::from(body),
            response_metadata,
            raw_payloads,
        })
    }

    fn ensure_usable(&self) -> Result<(), AggregationError> {
        if self.choices.is_empty() {
            return Err(AggregationError::plain(
                "upstream SSE ended without chat completion choices",
            ));
        }
        if !self.saw_done && !self.saw_finish_reason {
            return Err(AggregationError::plain(
                "upstream SSE ended before a final chat completion marker",
            ));
        }
        Ok(())
    }
}

struct CompletionFields {
    id: String,
    created: u64,
    model: String,
    service_tier: Option<Value>,
    system_fingerprint: Option<String>,
    usage: Option<Value>,
    extension_fields: Map<String, Value>,
}

impl CompletionFields {
    fn from_aggregation(
        aggregation: &ChatAggregation,
        request_id: &str,
        request_model_id: Option<&str>,
    ) -> Self {
        Self {
            id: aggregation
                .id
                .clone()
                .unwrap_or_else(|| format!("chatcmpl-{request_id}")),
            created: aggregation.created.unwrap_or_else(unix_time_secs),
            model: aggregation
                .model
                .clone()
                .or_else(|| request_model_id.map(str::to_owned))
                .unwrap_or_else(|| String::from("unknown")),
            service_tier: aggregation.service_tier.clone(),
            system_fingerprint: aggregation.system_fingerprint.clone(),
            usage: aggregation.usage.clone(),
            extension_fields: aggregation.extension_fields.clone(),
        }
    }
}

struct FinalizedChoices {
    choices: Vec<Value>,
    raw_output: String,
    raw_reasoning: String,
    raw_tool_calls: Vec<Value>,
}

impl FinalizedChoices {
    fn raw_payloads(&self) -> RawPayloads {
        let raw_tool_calls = (!self.raw_tool_calls.is_empty())
            .then(|| serde_json::to_string(&self.raw_tool_calls).ok())
            .flatten();
        RawPayloads {
            input: None,
            output: (!self.raw_output.is_empty()).then(|| self.raw_output.clone()),
            reasoning: (!self.raw_reasoning.is_empty()).then(|| self.raw_reasoning.clone()),
            tool_calls: raw_tool_calls,
        }
    }
}

fn finalize_choices(choices: BTreeMap<u64, ChoiceBuilder>) -> FinalizedChoices {
    let mut raw_output = String::new();
    let mut raw_reasoning = String::new();
    let mut raw_tool_calls = Vec::new();
    let mut final_choices = Vec::with_capacity(choices.len());

    for choice in choices.into_values() {
        raw_output.push_str(&choice.content);
        raw_reasoning.push_str(&choice.reasoning);
        let choice = choice.into_value();
        if let Some(tool_calls) = choice
            .get("message")
            .and_then(Value::as_object)
            .and_then(|message| message.get("tool_calls"))
            .and_then(Value::as_array)
        {
            raw_tool_calls.extend(tool_calls.iter().cloned());
        }
        final_choices.push(choice);
    }

    FinalizedChoices {
        choices: final_choices,
        raw_output,
        raw_reasoning,
        raw_tool_calls,
    }
}

fn completion_body(fields: CompletionFields, choices: Vec<Value>) -> Result<Vec<u8>, String> {
    let mut response = Map::from_iter([
        (String::from("id"), Value::String(fields.id)),
        (
            String::from("object"),
            Value::String(String::from("chat.completion")),
        ),
        (
            String::from("created"),
            Value::Number(Number::from(fields.created)),
        ),
        (String::from("model"), Value::String(fields.model)),
        (String::from("choices"), Value::Array(choices)),
    ]);
    insert_extension_fields(&mut response, fields.extension_fields);
    if let Some(service_tier) = fields.service_tier {
        response.insert(String::from("service_tier"), service_tier);
    }
    if let Some(system_fingerprint) = fields.system_fingerprint {
        response.insert(
            String::from("system_fingerprint"),
            Value::String(system_fingerprint),
        );
    }
    if let Some(usage) = fields.usage {
        response.insert(String::from("usage"), usage);
    }

    serde_json::to_vec(&Value::Object(response))
        .map_err(|error| format!("failed to serialize aggregated chat completion: {error}"))
}

fn copy_extension_fields(
    source: &Map<String, Value>,
    target: &mut Map<String, Value>,
    is_owned_field: fn(&str) -> bool,
) {
    for (key, value) in source {
        if is_owned_field(key) {
            continue;
        }
        target.insert(key.clone(), value.clone());
    }
}

fn insert_extension_fields(target: &mut Map<String, Value>, extension_fields: Map<String, Value>) {
    for (key, value) in extension_fields {
        target.insert(key, value);
    }
}

fn is_completion_owned_field(key: &str) -> bool {
    matches!(
        key,
        "id" | "object"
            | "created"
            | "model"
            | "choices"
            | "service_tier"
            | "system_fingerprint"
            | "usage"
    )
}

fn is_choice_owned_field(key: &str) -> bool {
    matches!(
        key,
        "index" | "delta" | "message" | "finish_reason" | "logprobs"
    )
}

fn is_message_owned_field(key: &str) -> bool {
    matches!(
        key,
        "role"
            | "content"
            | "reasoning_content"
            | "reasoning"
            | "thinking"
            | "tool_calls"
            | "function_call"
            | "refusal"
    )
}

fn response_metadata(aggregation: &ChatAggregation) -> BTreeMap<String, String> {
    let mut metadata = BTreeMap::from([
        (String::from("shielded_streaming"), String::from("true")),
        (String::from("upstream_stream_forced"), String::from("true")),
        (
            String::from("first_byte_latency_ms"),
            latency_metadata(aggregation.first_byte_latency_ms),
        ),
        (
            String::from("first_token_latency_ms"),
            latency_metadata(aggregation.first_token_latency_ms),
        ),
        (
            String::from("finish_reason"),
            aggregation
                .stats
                .finish_reason
                .clone()
                .unwrap_or_else(|| String::from("unknown")),
        ),
        (
            String::from("delta_count"),
            aggregation.stats.delta_count.to_string(),
        ),
        (
            String::from("content_delta_count"),
            aggregation.stats.content_delta_count.to_string(),
        ),
        (
            String::from("reasoning_delta_count"),
            aggregation.stats.reasoning_delta_count.to_string(),
        ),
        (
            String::from("tool_call_delta_count"),
            aggregation.stats.tool_call_delta_count.to_string(),
        ),
    ]);
    metadata.insert(
        String::from("response_header_content-type"),
        String::from("application/json"),
    );
    metadata
}

fn latency_metadata(latency_ms: Option<u64>) -> String {
    latency_ms.map_or_else(|| String::from("unknown"), |latency| latency.to_string())
}

fn sse_data(event: &str) -> String {
    event
        .lines()
        .filter_map(|line| {
            let line = line.strip_suffix('\r').unwrap_or(line);
            line.strip_prefix("data:")
                .map(|value| value.strip_prefix(' ').unwrap_or(value))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[derive(Default)]
struct ChoiceBuilder {
    index: u64,
    role: Option<String>,
    content: String,
    reasoning: String,
    finish_reason: Option<Value>,
    logprobs: Option<Value>,
    extension_fields: Map<String, Value>,
    message_extension_fields: Map<String, Value>,
    function_call: Option<FunctionCallBuilder>,
    refusal: String,
    saw_refusal: bool,
    tool_calls: BTreeMap<u64, ToolCallBuilder>,
}

impl ChoiceBuilder {
    fn merge_logprobs(&mut self, next: &Value) {
        let Some(existing) = self.logprobs.as_mut() else {
            self.logprobs = Some(next.clone());
            return;
        };
        merge_json_value(existing, next);
    }

    fn apply_delta(
        &mut self,
        delta: &Map<String, Value>,
        stats: &mut DeltaStats,
        first_token_latency_ms: &mut Option<u64>,
        attempt_started_at_unix_ms: u64,
        loop_detector: &mut Option<LoopDetector>,
    ) -> Result<(), AggregationError> {
        copy_extension_fields(
            delta,
            &mut self.message_extension_fields,
            is_message_owned_field,
        );
        if let Some(role) = delta.get("role").and_then(Value::as_str) {
            self.role.get_or_insert_with(|| role.to_owned());
        }
        if let Some(content) = delta.get("content").and_then(Value::as_str) {
            if !content.is_empty() {
                observe_fragment(loop_detector, LoopChannel::Content, content)?;
                self.content.push_str(content);
                stats.content_delta_count = stats.content_delta_count.saturating_add(1);
                mark_first_token(first_token_latency_ms, attempt_started_at_unix_ms);
            }
        }
        for field in ["reasoning_content", "reasoning", "thinking"] {
            if let Some(reasoning) = delta.get(field).and_then(Value::as_str) {
                if !reasoning.is_empty() {
                    observe_fragment(loop_detector, LoopChannel::Reasoning, reasoning)?;
                    self.reasoning.push_str(reasoning);
                    stats.reasoning_delta_count = stats.reasoning_delta_count.saturating_add(1);
                    mark_first_token(first_token_latency_ms, attempt_started_at_unix_ms);
                }
            }
        }
        if let Some(function_call) = delta.get("function_call").and_then(Value::as_object) {
            self.function_call
                .get_or_insert_with(FunctionCallBuilder::default)
                .apply_delta(function_call, loop_detector)?;
            mark_first_token(first_token_latency_ms, attempt_started_at_unix_ms);
        }
        if let Some(refusal) = delta.get("refusal") {
            self.saw_refusal = true;
            if let Some(refusal) = refusal.as_str() {
                if !refusal.is_empty() {
                    self.refusal.push_str(refusal);
                    mark_first_token(first_token_latency_ms, attempt_started_at_unix_ms);
                }
            }
        }
        if let Some(tool_calls) = delta.get("tool_calls").and_then(Value::as_array) {
            for tool_call in tool_calls {
                if let Some(tool_call) = tool_call.as_object() {
                    let index = tool_call
                        .get("index")
                        .and_then(Value::as_u64)
                        .unwrap_or(self.tool_calls.len() as u64);
                    self.tool_calls
                        .entry(index)
                        .or_insert_with(|| ToolCallBuilder {
                            index,
                            ..ToolCallBuilder::default()
                        })
                        .apply_delta(tool_call, loop_detector)?;
                    stats.tool_call_delta_count = stats.tool_call_delta_count.saturating_add(1);
                    mark_first_token(first_token_latency_ms, attempt_started_at_unix_ms);
                }
            }
        }
        Ok(())
    }

    fn into_value(self) -> Value {
        let mut message = Map::new();
        message.insert(
            String::from("role"),
            Value::String(self.role.unwrap_or_else(|| String::from("assistant"))),
        );
        if self.content.is_empty() && (!self.tool_calls.is_empty() || self.function_call.is_some())
        {
            message.insert(String::from("content"), Value::Null);
        } else {
            message.insert(String::from("content"), Value::String(self.content));
        }
        if !self.reasoning.is_empty() {
            message.insert(
                String::from("reasoning_content"),
                Value::String(self.reasoning),
            );
        }
        if let Some(function_call) = self.function_call {
            message.insert(String::from("function_call"), function_call.into_value());
        }
        if self.saw_refusal {
            let refusal = if self.refusal.is_empty() {
                Value::Null
            } else {
                Value::String(self.refusal)
            };
            message.insert(String::from("refusal"), refusal);
        }
        insert_extension_fields(&mut message, self.message_extension_fields);
        if !self.tool_calls.is_empty() {
            message.insert(
                String::from("tool_calls"),
                Value::Array(
                    self.tool_calls
                        .into_values()
                        .map(ToolCallBuilder::into_value)
                        .collect(),
                ),
            );
        }

        let mut choice = Map::from_iter([
            (
                String::from("index"),
                Value::Number(Number::from(self.index)),
            ),
            (String::from("message"), Value::Object(message)),
            (
                String::from("finish_reason"),
                self.finish_reason.unwrap_or(Value::Null),
            ),
        ]);
        if let Some(logprobs) = self.logprobs {
            choice.insert(String::from("logprobs"), logprobs);
        }
        insert_extension_fields(&mut choice, self.extension_fields);
        Value::Object(choice)
    }
}

#[derive(Default)]
struct FunctionCallBuilder {
    name: Option<String>,
    arguments: String,
    saw_arguments: bool,
}

impl FunctionCallBuilder {
    fn apply_delta(
        &mut self,
        function_call: &Map<String, Value>,
        loop_detector: &mut Option<LoopDetector>,
    ) -> Result<(), AggregationError> {
        if let Some(name) = function_call.get("name").and_then(Value::as_str) {
            self.name.get_or_insert_with(|| name.to_owned());
        }
        if let Some(arguments) = function_call.get("arguments").and_then(Value::as_str) {
            observe_fragment(loop_detector, LoopChannel::ToolCallArguments, arguments)?;
            self.saw_arguments = true;
            self.arguments.push_str(arguments);
        }
        Ok(())
    }

    fn into_value(self) -> Value {
        let mut function_call = Map::new();
        if let Some(name) = self.name {
            function_call.insert(String::from("name"), Value::String(name));
        }
        if self.saw_arguments {
            function_call.insert(String::from("arguments"), Value::String(self.arguments));
        }
        Value::Object(function_call)
    }
}

fn merge_json_value(existing: &mut Value, next: &Value) {
    match (existing, next) {
        (Value::Object(existing), Value::Object(next)) => {
            for (key, next_value) in next {
                match existing.get_mut(key) {
                    Some(existing_value) => merge_json_value(existing_value, next_value),
                    None => {
                        existing.insert(key.clone(), next_value.clone());
                    }
                }
            }
        }
        (Value::Array(existing), Value::Array(next)) => {
            existing.extend(next.iter().cloned());
        }
        (existing, next) => {
            *existing = next.clone();
        }
    }
}

#[derive(Default)]
struct ToolCallBuilder {
    index: u64,
    id: Option<String>,
    type_name: Option<String>,
    function_name: Option<String>,
    function_arguments: String,
}

impl ToolCallBuilder {
    fn apply_delta(
        &mut self,
        tool_call: &Map<String, Value>,
        loop_detector: &mut Option<LoopDetector>,
    ) -> Result<(), AggregationError> {
        if let Some(id) = tool_call.get("id").and_then(Value::as_str) {
            self.id.get_or_insert_with(|| id.to_owned());
        }
        if let Some(type_name) = tool_call.get("type").and_then(Value::as_str) {
            self.type_name.get_or_insert_with(|| type_name.to_owned());
        }
        if let Some(function) = tool_call.get("function").and_then(Value::as_object) {
            if let Some(name) = function.get("name").and_then(Value::as_str) {
                self.function_name.get_or_insert_with(|| name.to_owned());
            }
            if let Some(arguments) = function.get("arguments").and_then(Value::as_str) {
                observe_fragment(loop_detector, LoopChannel::ToolCallArguments, arguments)?;
                self.function_arguments.push_str(arguments);
            }
        }
        Ok(())
    }

    fn into_value(self) -> Value {
        json!({
            "id": self.id.unwrap_or_else(|| format!("call_{}", self.index)),
            "type": self.type_name.unwrap_or_else(|| String::from("function")),
            "function": {
                "name": self.function_name.unwrap_or_default(),
                "arguments": self.function_arguments,
            },
        })
    }
}

#[derive(Default)]
struct DeltaStats {
    delta_count: u64,
    content_delta_count: u64,
    reasoning_delta_count: u64,
    tool_call_delta_count: u64,
    finish_reason: Option<String>,
}

fn mark_first_token(first_token_latency_ms: &mut Option<u64>, attempt_started_at_unix_ms: u64) {
    if first_token_latency_ms.is_none() {
        *first_token_latency_ms =
            Some(unix_time_millis().saturating_sub(attempt_started_at_unix_ms));
    }
}

fn string_field<'value>(value: &'value Value, key: &str) -> Option<&'value str> {
    value.get(key).and_then(Value::as_str)
}

fn unix_time_secs() -> u64 {
    unix_time_millis() / 1_000
}
