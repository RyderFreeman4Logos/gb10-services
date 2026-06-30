//! Typed configuration loading and hot reload support.
//!
//! This module keeps configuration policy in the headless core so the binary,
//! future proxy server, and tests all consume the same validated model.

mod error;
mod model;
mod parse;
mod reload;

#[cfg(test)]
mod tests;

pub use error::{ConfigError, ConfigParseError, ValidationError};
pub use model::{
    AppConfig, CloudflareConfig, ConfigToggle, HeartbeatConfig, HeartbeatMode, LoopGuardConfig,
    MetadataConfig, ObservabilityConfig, RestartRequiredChange, RetentionConfig, RetryConfig,
    ServerConfig, ShieldingConfig, ThinkingConfig, ToolRequestThinkingPolicy, UpstreamConfig,
    UpstreamStallConfig, redact_upstream_base_url, validate_upstream_base_url,
};
pub use reload::{
    ConfigHandle, ConfigManager, MissingConfigPolicy, ReloadOutcome, ReloadWatcher,
    default_config_path,
};

/// Default config location relative to the user's home directory.
pub const DEFAULT_CONFIG_RELATIVE_PATH: &str = ".config/llm-guard-proxy/config.toml";

/// Fields that can be changed by reloading the config file.
pub const RELOADABLE_FIELDS: &[&str] = &[
    "server.max_in_flight_requests",
    "server.max_queued_generation_requests",
    "server.generation_queue_timeout_ms",
    "server.max_control_plane_in_flight_requests",
    "server.max_request_body_bytes",
    "shielding.enabled",
    "observability.enabled",
    "observability.capture_raw_payloads",
    "observability.metrics_enabled",
    "observability.health_upstream_probe_enabled",
    "observability.health_upstream_probe_timeout_ms",
    "observability.debug_summary_enabled",
    "observability.debug_summary_admin_token",
    "observability.debug_summary_max_records",
    "observability.retention.max_bytes",
    "observability.retention.prune_to_bytes",
    "observability.retention.max_records",
    "observability.retention.prune_to_records",
    "thinking.enabled",
    "thinking.force_disable",
    "thinking.budget_tokens",
    "thinking.preserve_answer_budget",
    "thinking.tool_request_policy",
    "loop_guard.enabled",
    "loop_guard.normalized_input_window_secs",
    "loop_guard.max_repeated_inputs",
    "loop_guard.output_repeated_line_threshold",
    "loop_guard.output_token_window_size",
    "loop_guard.output_repeated_token_window_threshold",
    "loop_guard.output_suffix_cycle_threshold",
    "loop_guard.output_low_progress_min_bytes",
    "loop_guard.output_low_progress_unique_ratio_percent",
    "loop_guard.input_overlap_threshold_multiplier",
    "loop_guard.reasoning_semantic_detection_enabled",
    "loop_guard.reasoning_semantic_similarity_threshold_percent",
    "loop_guard.reasoning_semantic_window_token_count",
    "loop_guard.reasoning_semantic_minimum_token_count",
    "loop_guard.reasoning_semantic_history_window_count",
    "retry.enabled",
    "retry.max_attempts",
    "retry.anti_loop_hint_enabled",
    "upstream.stall.enabled",
    "upstream.stall.idle_timeout_ms",
    "upstream.stall.recovery_command",
    "upstream.stall.recovery_timeout_ms",
    "upstream.stall.recovery_cooldown_ms",
    "upstream.stall.recovery_budget_window_ms",
    "upstream.stall.recovery_max_per_window",
    "heartbeat.mode",
    "heartbeat.interval_secs",
    "cloudflare.enabled",
    "upstream.request_timeout_ms",
    "upstream.metadata.discovery_enabled",
    "upstream.metadata.enrich_responses",
    "upstream.metadata.refresh_interval_secs",
    "upstream.metadata.context_length_override",
    "upstream.metadata.max_model_len_override",
];

/// Fields read at process startup that require a restart when changed.
pub const RESTART_REQUIRED_FIELDS: &[&str] = &[
    "server.bind_host",
    "server.port",
    "upstream.base_url",
    "observability.sqlite_path",
];
