use std::path::PathBuf;

use super::{
    AppConfig, CloudflareConfig, ConfigParseError, ConfigToggle, HeartbeatConfig, HeartbeatMode,
    LoopGuardConfig, MetadataConfig, ObservabilityConfig, RetentionConfig, RetryConfig,
    ServerConfig, ShieldingConfig, ThinkingConfig, ToolRequestThinkingPolicy, UpstreamConfig,
    UpstreamStallConfig,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Section {
    Root,
    Server,
    Upstream,
    UpstreamMetadata,
    Shielding,
    Observability,
    ObservabilityRetention,
    Thinking,
    LoopGuard,
    Retry,
    UpstreamStall,
    Heartbeat,
    Cloudflare,
}

pub(crate) fn parse_config_text(contents: &str) -> Result<AppConfig, ConfigParseError> {
    let mut config = AppConfig::default();
    let mut section = Section::Root;

    for (index, raw_line) in contents.lines().enumerate() {
        let line_number = index + 1;
        let line_without_comment = strip_comment(raw_line, line_number)?;
        let line = line_without_comment.trim();
        if line.is_empty() {
            continue;
        }
        if line.starts_with('[') {
            section = parse_section(line, line_number)?;
            continue;
        }
        let (key, value) = split_key_value(line, line_number)?;
        assign_value(&mut config, section, key.trim(), value.trim(), line_number)?;
    }

    Ok(config)
}

fn strip_comment(line: &str, line_number: usize) -> Result<&str, ConfigParseError> {
    let mut in_string = false;
    let mut escaped = false;
    for (index, character) in line.char_indices() {
        if character == '"' && !escaped {
            in_string = !in_string;
        }
        if character == '#' && !in_string {
            return Ok(&line[..index]);
        }
        escaped = character == '\\' && !escaped;
        if character != '\\' {
            escaped = false;
        }
    }
    if in_string {
        Err(ConfigParseError::new(line_number, "unterminated string"))
    } else {
        Ok(line)
    }
}

fn parse_section(line: &str, line_number: usize) -> Result<Section, ConfigParseError> {
    if !line.ends_with(']') {
        return Err(ConfigParseError::new(
            line_number,
            "section header must end with ]",
        ));
    }
    let section = &line[1..line.len() - 1];
    match section {
        "server" => Ok(Section::Server),
        "upstream" => Ok(Section::Upstream),
        "upstream.metadata" => Ok(Section::UpstreamMetadata),
        "shielding" => Ok(Section::Shielding),
        "observability" => Ok(Section::Observability),
        "observability.retention" => Ok(Section::ObservabilityRetention),
        "thinking" => Ok(Section::Thinking),
        "loop_guard" => Ok(Section::LoopGuard),
        "retry" => Ok(Section::Retry),
        "upstream.stall" => Ok(Section::UpstreamStall),
        "heartbeat" => Ok(Section::Heartbeat),
        "cloudflare" => Ok(Section::Cloudflare),
        _ => Err(ConfigParseError::new(
            line_number,
            format!("unknown config section [{section}]"),
        )),
    }
}

fn split_key_value(line: &str, line_number: usize) -> Result<(&str, &str), ConfigParseError> {
    let mut in_string = false;
    let mut escaped = false;
    for (index, character) in line.char_indices() {
        if character == '"' && !escaped {
            in_string = !in_string;
        }
        if character == '=' && !in_string {
            return Ok((&line[..index], &line[index + 1..]));
        }
        escaped = character == '\\' && !escaped;
        if character != '\\' {
            escaped = false;
        }
    }
    Err(ConfigParseError::new(
        line_number,
        "expected key = value entry",
    ))
}

fn assign_value(
    config: &mut AppConfig,
    section: Section,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match section {
        Section::Root => Err(ConfigParseError::new(
            line_number,
            "config keys must be inside a section",
        )),
        Section::Server => assign_server(&mut config.server, key, value, line_number),
        Section::Upstream => assign_upstream(&mut config.upstream, key, value, line_number),
        Section::UpstreamMetadata => {
            assign_metadata(&mut config.upstream.metadata, key, value, line_number)
        }
        Section::Shielding => assign_shielding(&mut config.shielding, key, value, line_number),
        Section::Observability => {
            assign_observability(&mut config.observability, key, value, line_number)
        }
        Section::ObservabilityRetention => {
            assign_retention(&mut config.observability.retention, key, value, line_number)
        }
        Section::Thinking => assign_thinking(&mut config.thinking, key, value, line_number),
        Section::LoopGuard => assign_loop_guard(&mut config.loop_guard, key, value, line_number),
        Section::Retry => assign_retry(&mut config.retry, key, value, line_number),
        Section::UpstreamStall => {
            assign_upstream_stall(&mut config.upstream_stall, key, value, line_number)
        }
        Section::Heartbeat => assign_heartbeat(&mut config.heartbeat, key, value, line_number),
        Section::Cloudflare => assign_cloudflare(&mut config.cloudflare, key, value, line_number),
    }
}

fn assign_server(
    config: &mut ServerConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "bind_host" => config.bind_host = parse_string(value, line_number)?,
        "port" => config.port = parse_u16(value, line_number, "server.port")?,
        "max_in_flight_requests" => {
            config.max_in_flight_requests =
                parse_usize(value, line_number, "server.max_in_flight_requests")?;
        }
        "max_queued_generation_requests" => {
            config.max_queued_generation_requests =
                parse_usize(value, line_number, "server.max_queued_generation_requests")?;
        }
        "generation_queue_timeout_ms" => {
            config.generation_queue_timeout_ms =
                parse_u64(value, line_number, "server.generation_queue_timeout_ms")?;
        }
        "max_control_plane_in_flight_requests" => {
            config.max_control_plane_in_flight_requests = parse_usize(
                value,
                line_number,
                "server.max_control_plane_in_flight_requests",
            )?;
        }
        "max_request_body_bytes" => {
            config.max_request_body_bytes =
                parse_usize(value, line_number, "server.max_request_body_bytes")?;
        }
        _ => return unknown_key("server", key, line_number),
    }
    Ok(())
}

fn assign_upstream(
    config: &mut UpstreamConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "base_url" => config.base_url = parse_string(value, line_number)?,
        "request_timeout_ms" => {
            config.request_timeout_ms =
                parse_u64(value, line_number, "upstream.request_timeout_ms")?;
        }
        _ => return unknown_key("upstream", key, line_number),
    }
    Ok(())
}

fn assign_metadata(
    config: &mut MetadataConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "discovery_enabled" => config.discovery_enabled = parse_bool(value, line_number)?,
        "enrich_responses" => config.enrich_responses = parse_bool(value, line_number)?,
        "refresh_interval_secs" => {
            config.refresh_interval_secs = parse_u64(
                value,
                line_number,
                "upstream.metadata.refresh_interval_secs",
            )?;
        }
        "context_length_override" => {
            config.context_length_override = Some(parse_u32(
                value,
                line_number,
                "upstream.metadata.context_length_override",
            )?);
        }
        "max_model_len_override" => {
            config.max_model_len_override = Some(parse_u32(
                value,
                line_number,
                "upstream.metadata.max_model_len_override",
            )?);
        }
        _ => return unknown_key("upstream.metadata", key, line_number),
    }
    Ok(())
}

fn assign_shielding(
    config: &mut ShieldingConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "enabled" => config.enabled = parse_bool(value, line_number)?,
        _ => return unknown_key("shielding", key, line_number),
    }
    Ok(())
}

fn assign_observability(
    config: &mut ObservabilityConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "enabled" => config.enabled = parse_bool(value, line_number)?,
        "sqlite_path" => config.sqlite_path = PathBuf::from(parse_string(value, line_number)?),
        "capture_raw_payloads" => config.capture_raw_payloads = parse_bool(value, line_number)?,
        "metrics_enabled" => {
            config.metrics_enabled = ConfigToggle::from_bool(parse_bool(value, line_number)?);
        }
        "health_upstream_probe_enabled" => {
            config.health_upstream_probe_enabled =
                ConfigToggle::from_bool(parse_bool(value, line_number)?);
        }
        "health_upstream_probe_timeout_ms" => {
            config.health_upstream_probe_timeout_ms = parse_u64(
                value,
                line_number,
                "observability.health_upstream_probe_timeout_ms",
            )?;
        }
        "debug_summary_enabled" => {
            config.debug_summary_enabled = ConfigToggle::from_bool(parse_bool(value, line_number)?);
        }
        "debug_summary_admin_token" => {
            config.debug_summary_admin_token = parse_optional_string(value, line_number)?;
        }
        "debug_summary_max_records" => {
            config.debug_summary_max_records = parse_u32(
                value,
                line_number,
                "observability.debug_summary_max_records",
            )?;
        }
        _ => return unknown_key("observability", key, line_number),
    }
    Ok(())
}

fn assign_retention(
    config: &mut RetentionConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "max_bytes" => {
            config.max_bytes = parse_u64(value, line_number, "observability.retention.max_bytes")?;
        }
        "prune_to_bytes" => {
            config.prune_to_bytes =
                parse_u64(value, line_number, "observability.retention.prune_to_bytes")?;
        }
        "max_records" => {
            config.max_records =
                parse_u64(value, line_number, "observability.retention.max_records")?;
        }
        "prune_to_records" => {
            config.prune_to_records = Some(parse_u64(
                value,
                line_number,
                "observability.retention.prune_to_records",
            )?);
        }
        _ => return unknown_key("observability.retention", key, line_number),
    }
    Ok(())
}

fn assign_thinking(
    config: &mut ThinkingConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "enabled" => config.enabled = parse_bool(value, line_number)?,
        "force_disable" => config.force_disable = parse_bool(value, line_number)?,
        "budget_tokens" => {
            config.budget_tokens = parse_u32(value, line_number, "thinking.budget_tokens")?;
        }
        "preserve_answer_budget" => config.preserve_answer_budget = parse_bool(value, line_number)?,
        "tool_request_policy" => {
            config.tool_request_policy = parse_tool_request_thinking_policy(value, line_number)?;
        }
        _ => return unknown_key("thinking", key, line_number),
    }
    Ok(())
}

fn parse_tool_request_thinking_policy(
    value: &str,
    line_number: usize,
) -> Result<ToolRequestThinkingPolicy, ConfigParseError> {
    match parse_string(value, line_number)?.trim() {
        "apply" => Ok(ToolRequestThinkingPolicy::Apply),
        "passthrough" => Ok(ToolRequestThinkingPolicy::Passthrough),
        other => Err(ConfigParseError::new(
            line_number,
            format!(
                "invalid thinking.tool_request_policy {other:?}; expected \"apply\" or \"passthrough\""
            ),
        )),
    }
}

fn assign_loop_guard(
    config: &mut LoopGuardConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "enabled" => config.enabled = parse_bool(value, line_number)?,
        "normalized_input_window_secs" => {
            config.normalized_input_window_secs = parse_u64(
                value,
                line_number,
                "loop_guard.normalized_input_window_secs",
            )?;
        }
        "max_repeated_inputs" => {
            config.max_repeated_inputs =
                parse_u32(value, line_number, "loop_guard.max_repeated_inputs")?;
        }
        "output_repeated_line_threshold" => {
            config.output_repeated_line_threshold = parse_u32(
                value,
                line_number,
                "loop_guard.output_repeated_line_threshold",
            )?;
        }
        "output_token_window_size" => {
            config.output_token_window_size =
                parse_u32(value, line_number, "loop_guard.output_token_window_size")?;
        }
        "output_repeated_token_window_threshold" => {
            config.output_repeated_token_window_threshold = parse_u32(
                value,
                line_number,
                "loop_guard.output_repeated_token_window_threshold",
            )?;
        }
        "output_suffix_cycle_threshold" => {
            config.output_suffix_cycle_threshold = parse_u32(
                value,
                line_number,
                "loop_guard.output_suffix_cycle_threshold",
            )?;
        }
        "output_low_progress_min_bytes" => {
            config.output_low_progress_min_bytes = parse_u64(
                value,
                line_number,
                "loop_guard.output_low_progress_min_bytes",
            )?;
        }
        "output_low_progress_unique_ratio_percent" => {
            config.output_low_progress_unique_ratio_percent = parse_u32(
                value,
                line_number,
                "loop_guard.output_low_progress_unique_ratio_percent",
            )?;
        }
        "input_overlap_threshold_multiplier" => {
            config.input_overlap_threshold_multiplier = parse_u32(
                value,
                line_number,
                "loop_guard.input_overlap_threshold_multiplier",
            )?;
        }
        "reasoning_semantic_detection_enabled" => {
            config.reasoning_semantic_detection_enabled = parse_bool(value, line_number)?;
        }
        "reasoning_semantic_similarity_threshold_percent" => {
            config.reasoning_semantic_similarity_threshold_percent = parse_u32(
                value,
                line_number,
                "loop_guard.reasoning_semantic_similarity_threshold_percent",
            )?;
        }
        "reasoning_semantic_window_token_count" => {
            config.reasoning_semantic_window_token_count = parse_u32(
                value,
                line_number,
                "loop_guard.reasoning_semantic_window_token_count",
            )?;
        }
        "reasoning_semantic_minimum_token_count" => {
            config.reasoning_semantic_minimum_token_count = parse_u32(
                value,
                line_number,
                "loop_guard.reasoning_semantic_minimum_token_count",
            )?;
        }
        "reasoning_semantic_history_window_count" => {
            config.reasoning_semantic_history_window_count = parse_u32(
                value,
                line_number,
                "loop_guard.reasoning_semantic_history_window_count",
            )?;
        }
        _ => return unknown_key("loop_guard", key, line_number),
    }
    Ok(())
}

fn assign_retry(
    config: &mut RetryConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "enabled" => config.enabled = parse_bool(value, line_number)?,
        "max_attempts" => {
            config.max_attempts = parse_u32(value, line_number, "retry.max_attempts")?;
        }
        "anti_loop_hint_enabled" => {
            config.anti_loop_hint_enabled = parse_bool(value, line_number)?;
        }
        _ => return unknown_key("retry", key, line_number),
    }
    Ok(())
}

fn assign_upstream_stall(
    config: &mut UpstreamStallConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "enabled" => config.enabled = parse_bool(value, line_number)?,
        "idle_timeout_ms" => {
            config.idle_timeout_ms =
                parse_u64(value, line_number, "upstream.stall.idle_timeout_ms")?;
        }
        "recovery_command" => {
            config.recovery_command = parse_string_array(value, line_number)?;
        }
        "recovery_timeout_ms" => {
            config.recovery_timeout_ms =
                parse_u64(value, line_number, "upstream.stall.recovery_timeout_ms")?;
        }
        "recovery_cooldown_ms" => {
            config.recovery_cooldown_ms =
                parse_u64(value, line_number, "upstream.stall.recovery_cooldown_ms")?;
        }
        "recovery_budget_window_ms" => {
            config.recovery_budget_window_ms = parse_u64(
                value,
                line_number,
                "upstream.stall.recovery_budget_window_ms",
            )?;
        }
        "recovery_max_per_window" => {
            config.recovery_max_per_window =
                parse_u32(value, line_number, "upstream.stall.recovery_max_per_window")?;
        }
        _ => return unknown_key("upstream.stall", key, line_number),
    }
    Ok(())
}

fn assign_heartbeat(
    config: &mut HeartbeatConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "mode" => config.mode = parse_heartbeat_mode(value, line_number)?,
        "interval_secs" => {
            config.interval_secs = parse_u64(value, line_number, "heartbeat.interval_secs")?;
        }
        _ => return unknown_key("heartbeat", key, line_number),
    }
    Ok(())
}

fn assign_cloudflare(
    config: &mut CloudflareConfig,
    key: &str,
    value: &str,
    line_number: usize,
) -> Result<(), ConfigParseError> {
    match key {
        "enabled" => config.enabled = parse_bool(value, line_number)?,
        _ => return unknown_key("cloudflare", key, line_number),
    }
    Ok(())
}

fn parse_string(value: &str, line_number: usize) -> Result<String, ConfigParseError> {
    if !value.starts_with('"') || !value.ends_with('"') || value.len() < 2 {
        return Err(ConfigParseError::new(
            line_number,
            "expected a quoted string value",
        ));
    }
    let inner = &value[1..value.len() - 1];
    let mut parsed = String::with_capacity(inner.len());
    let mut chars = inner.chars();
    while let Some(character) = chars.next() {
        if character != '\\' {
            parsed.push(character);
            continue;
        }
        let Some(escaped) = chars.next() else {
            return Err(ConfigParseError::new(
                line_number,
                "string escape sequence is incomplete",
            ));
        };
        match escaped {
            '"' => parsed.push('"'),
            '\\' => parsed.push('\\'),
            'n' => parsed.push('\n'),
            'r' => parsed.push('\r'),
            't' => parsed.push('\t'),
            _ => {
                return Err(ConfigParseError::new(
                    line_number,
                    format!("unsupported string escape \\{escaped}"),
                ));
            }
        }
    }
    Ok(parsed)
}

fn parse_optional_string(
    value: &str,
    line_number: usize,
) -> Result<Option<String>, ConfigParseError> {
    let parsed = parse_string(value, line_number)?;
    Ok((!parsed.is_empty()).then_some(parsed))
}

fn parse_string_array(value: &str, line_number: usize) -> Result<Vec<String>, ConfigParseError> {
    if !value.starts_with('[') || !value.ends_with(']') {
        return Err(ConfigParseError::new(
            line_number,
            "expected an array of quoted string values",
        ));
    }
    let inner = &value[1..value.len() - 1];
    let mut parsed = Vec::new();
    let mut cursor = 0;
    while cursor < inner.len() {
        let remaining = &inner[cursor..];
        let trimmed = remaining.trim_start();
        cursor += remaining.len() - trimmed.len();
        if cursor >= inner.len() {
            break;
        }
        if !inner[cursor..].starts_with('"') {
            return Err(ConfigParseError::new(
                line_number,
                "array entries must be quoted strings",
            ));
        }
        let mut escaped = false;
        let mut end = None;
        for (relative_index, character) in inner[cursor + 1..].char_indices() {
            if character == '"' && !escaped {
                end = Some(cursor + 1 + relative_index);
                break;
            }
            escaped = character == '\\' && !escaped;
            if character != '\\' {
                escaped = false;
            }
        }
        let Some(end_index) = end else {
            return Err(ConfigParseError::new(line_number, "unterminated string"));
        };
        parsed.push(parse_string(&inner[cursor..=end_index], line_number)?);
        cursor = end_index + 1;
        let remaining = inner[cursor..].trim_start();
        cursor = inner.len() - remaining.len();
        if cursor >= inner.len() {
            break;
        }
        if !inner[cursor..].starts_with(',') {
            return Err(ConfigParseError::new(
                line_number,
                "array entries must be separated by commas",
            ));
        }
        cursor += 1;
    }
    Ok(parsed)
}

fn parse_bool(value: &str, line_number: usize) -> Result<bool, ConfigParseError> {
    match value {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(ConfigParseError::new(
            line_number,
            "expected boolean value true or false",
        )),
    }
}

fn parse_heartbeat_mode(
    value: &str,
    line_number: usize,
) -> Result<HeartbeatMode, ConfigParseError> {
    let mode = parse_string(value, line_number)?;
    match mode.as_str() {
        "sse" => Ok(HeartbeatMode::Sse),
        "json-whitespace" => Ok(HeartbeatMode::JsonWhitespace),
        "disabled" => Ok(HeartbeatMode::Disabled),
        _ => Err(ConfigParseError::new(
            line_number,
            "heartbeat.mode must be sse, json-whitespace, or disabled",
        )),
    }
}

fn parse_u16(value: &str, line_number: usize, field: &str) -> Result<u16, ConfigParseError> {
    let number = parse_u64(value, line_number, field)?;
    u16::try_from(number).map_err(|_error| {
        ConfigParseError::new(
            line_number,
            format!("{field} must fit in an unsigned 16-bit port"),
        )
    })
}

fn parse_u32(value: &str, line_number: usize, field: &str) -> Result<u32, ConfigParseError> {
    let number = parse_u64(value, line_number, field)?;
    u32::try_from(number).map_err(|_error| {
        ConfigParseError::new(
            line_number,
            format!("{field} must fit in an unsigned 32-bit integer"),
        )
    })
}

fn parse_usize(value: &str, line_number: usize, field: &str) -> Result<usize, ConfigParseError> {
    let number = parse_u64(value, line_number, field)?;
    usize::try_from(number).map_err(|_error| {
        ConfigParseError::new(
            line_number,
            format!("{field} must fit in an unsigned pointer-sized integer"),
        )
    })
}

fn parse_u64(value: &str, line_number: usize, field: &str) -> Result<u64, ConfigParseError> {
    if value.starts_with('-') || value.starts_with('+') {
        return Err(ConfigParseError::new(
            line_number,
            format!("{field} must be an unsigned integer"),
        ));
    }
    let normalized = value.replace('_', "");
    if normalized.is_empty()
        || !normalized
            .chars()
            .all(|character| character.is_ascii_digit())
    {
        return Err(ConfigParseError::new(
            line_number,
            format!("{field} must be an unsigned integer"),
        ));
    }
    normalized.parse::<u64>().map_err(|error| {
        ConfigParseError::new(
            line_number,
            format!("{field} is outside the supported range: {error}"),
        )
    })
}

fn unknown_key<T>(section: &str, key: &str, line_number: usize) -> Result<T, ConfigParseError> {
    Err(ConfigParseError::new(
        line_number,
        format!("unknown config key {section}.{key}"),
    ))
}
