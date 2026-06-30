use std::collections::BTreeMap;

use super::{error::ObservabilityError, model::RawPayloads};

const REDACTED: &str = "[REDACTED]";
const SENSITIVE_KEY_MARKERS: &[&str] = &[
    "authorization",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
];
const SENSITIVE_ASSIGNMENT_KEYS: &[&str] = &[
    "authorization",
    "api-key",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
];

pub(super) fn redacted_metadata_json(
    metadata: &BTreeMap<String, String>,
    field: &'static str,
) -> Result<String, ObservabilityError> {
    let redacted = redacted_metadata_map(metadata);
    serde_json::to_string(&redacted)
        .map_err(|source| ObservabilityError::SerializeMetadata { field, source })
}

pub(super) fn redacted_metadata_map(
    metadata: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    metadata
        .iter()
        .map(|(key, value)| {
            let value = if is_sensitive_key(key) || looks_sensitive(value) {
                REDACTED.to_owned()
            } else {
                value.clone()
            };
            (key.clone(), value)
        })
        .collect()
}

pub(super) fn debug_safe_metadata_map(
    metadata: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    metadata
        .iter()
        .filter(|(key, _value)| !is_sensitive_key(key))
        .map(|(key, value)| {
            let value = if looks_sensitive(value) {
                REDACTED.to_owned()
            } else {
                value.clone()
            };
            (key.clone(), value)
        })
        .collect()
}

pub(super) fn sanitize_raw_payloads(raw: &RawPayloads, capture_raw_payloads: bool) -> RawPayloads {
    if !capture_raw_payloads {
        return RawPayloads::default();
    }
    RawPayloads {
        input: sanitize_optional_text(raw.input.as_ref()),
        output: sanitize_optional_text(raw.output.as_ref()),
        reasoning: sanitize_optional_text(raw.reasoning.as_ref()),
        tool_calls: sanitize_optional_text(raw.tool_calls.as_ref()),
    }
}

pub(super) fn sanitize_optional_text(value: Option<&String>) -> Option<String> {
    value.map(|value| {
        if looks_sensitive(value) {
            REDACTED.to_owned()
        } else {
            value.clone()
        }
    })
}

fn is_sensitive_key(key: &str) -> bool {
    let normalized = normalize_key(key);
    if is_non_secret_token_metric(&normalized) {
        return false;
    }
    SENSITIVE_KEY_MARKERS
        .iter()
        .any(|marker| normalized.contains(marker))
}

fn is_non_secret_token_metric(normalized_key: &str) -> bool {
    matches!(
        normalized_key,
        "firsttokenlatencyms"
            | "prompttokens"
            | "completiontokens"
            | "totaltokens"
            | "thinkingpolicybudgettokens"
            | "thinkingbudgetprevioustokens"
            | "thinkingbudgetfinaltokens"
            | "thinkinganswerbudgetdeltatokens"
    )
}

fn looks_sensitive(value: &str) -> bool {
    let normalized = value.to_ascii_lowercase();
    normalized.contains("bearer ")
        || normalized.contains("api_key")
        || normalized.contains("api-key")
        || normalized.contains("x-api-key")
        || normalized.contains("authorization")
        || normalized.contains("sk-")
        || contains_sensitive_assignment(&normalized)
}

fn normalize_key(key: &str) -> String {
    key.chars()
        .filter(char::is_ascii_alphanumeric)
        .flat_map(char::to_lowercase)
        .collect()
}

fn contains_sensitive_assignment(value: &str) -> bool {
    let compact = value
        .chars()
        .filter(|character| {
            !character.is_ascii_whitespace()
                && !matches!(character, '"' | '\'' | '`' | '{' | '}' | '[' | ']')
        })
        .collect::<String>();

    SENSITIVE_ASSIGNMENT_KEYS
        .iter()
        .any(|key| compact.contains(&format!("{key}:")) || compact.contains(&format!("{key}=")))
}
