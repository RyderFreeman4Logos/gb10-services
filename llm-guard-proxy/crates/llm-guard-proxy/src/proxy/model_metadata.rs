use axum::body::Bytes;
use llm_guard_proxy_core::MetadataConfig;
use serde_json::{Map, Number, Value};

/// Enriches OpenAI-compatible model list responses with normalized context metadata.
///
/// Invalid JSON, non-list payloads, and responses without usable model metadata are returned
/// byte-for-byte so generic proxy behavior stays pass-through unless enrichment can help.
pub(super) fn enrich_models_body(config: &MetadataConfig, body: Bytes) -> Bytes {
    if !config.discovery_enabled || !config.enrich_responses {
        return body;
    }

    let Ok(mut value) = serde_json::from_slice::<Value>(&body) else {
        return body;
    };

    let Some(models) = value.get_mut("data").and_then(Value::as_array_mut) else {
        return body;
    };

    let mut changed = false;
    for model in models {
        if let Some(record) = model.as_object_mut() {
            changed |= enrich_model_record(config, record);
        }
    }

    if !changed {
        return body;
    }

    serde_json::to_vec(&value).map_or(body, Bytes::from)
}

fn enrich_model_record(config: &MetadataConfig, record: &mut Map<String, Value>) -> bool {
    let Some(context_length) =
        discovered_context_length(record).or_else(|| fallback_context_length(config))
    else {
        return false;
    };

    let mut changed = false;
    for field in ["context_length", "max_context_length", "max_model_len"] {
        changed |= set_u64_field(record, field, context_length);
    }
    changed
}

fn discovered_context_length(record: &Map<String, Value>) -> Option<u64> {
    numeric_field(record, "max_model_len")
        .or_else(|| numeric_field(record, "context_length"))
        .or_else(|| numeric_field(record, "max_context_length"))
}

fn fallback_context_length(config: &MetadataConfig) -> Option<u64> {
    config
        .context_length_override
        .or(config.max_model_len_override)
        .map(u64::from)
}

fn numeric_field(record: &Map<String, Value>, key: &str) -> Option<u64> {
    record.get(key).and_then(Value::as_u64)
}

fn set_u64_field(record: &mut Map<String, Value>, key: &str, value: u64) -> bool {
    let value = Value::Number(Number::from(value));
    if record.get(key) == Some(&value) {
        return false;
    }

    record.insert(key.to_owned(), value);
    true
}
