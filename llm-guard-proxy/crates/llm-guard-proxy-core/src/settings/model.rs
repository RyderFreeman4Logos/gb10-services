use std::path::PathBuf;

use super::ValidationError;
use url::Url;

const REDACTED_URL_PART: &str = "redacted";
const INVALID_URL_DISPLAY: &str = "[invalid URL]";
const LOOP_GUARD_MAX_SEMANTIC_WINDOW_TOKENS: u32 = 256;
const LOOP_GUARD_MAX_SEMANTIC_HISTORY_WINDOWS: u32 = 256;

/// Complete application configuration.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct AppConfig {
    /// Process listener settings. These are restart-required.
    pub server: ServerConfig,
    /// Upstream OpenAI-compatible service settings.
    pub upstream: UpstreamConfig,
    /// Client shielding behavior flags.
    pub shielding: ShieldingConfig,
    /// Observability storage and retention settings.
    pub observability: ObservabilityConfig,
    /// Thinking budget policy for later request rewriting.
    pub thinking: ThinkingConfig,
    /// Loop detection policy.
    pub loop_guard: LoopGuardConfig,
    /// Retry policy for shielded upstream attempts.
    pub retry: RetryConfig,
    /// Upstream no-progress detection and recovery policy.
    pub upstream_stall: UpstreamStallConfig,
    /// Downstream liveness policy.
    pub heartbeat: HeartbeatConfig,
    /// Cloudflare compatibility policy for later timeout shielding.
    pub cloudflare: CloudflareConfig,
}

impl AppConfig {
    /// Validates cross-field and range constraints.
    ///
    /// # Errors
    ///
    /// Returns a [`ValidationError`] when a field is empty, zero where zero is
    /// not meaningful, or violates a cross-field relation.
    pub fn validate(&self) -> Result<(), ValidationError> {
        self.server.validate()?;
        self.upstream.validate()?;
        self.observability.validate()?;
        self.loop_guard.validate()?;
        self.retry.validate()?;
        self.upstream_stall.validate()?;
        self.validate_upstream_stall_timeout_order()?;
        self.heartbeat.validate()
    }

    fn validate_upstream_stall_timeout_order(&self) -> Result<(), ValidationError> {
        if self.upstream_stall.enabled {
            require(
                self.upstream_stall.idle_timeout_ms < self.upstream.request_timeout_ms,
                "upstream.stall.idle_timeout_ms",
                "must be less than upstream.request_timeout_ms when upstream stall recovery is enabled",
            )?;
        }
        Ok(())
    }

    pub(crate) fn apply_reloadable_from(&mut self, requested: &Self) {
        self.server.max_in_flight_requests = requested.server.max_in_flight_requests;
        self.server.max_queued_generation_requests =
            requested.server.max_queued_generation_requests;
        self.server.generation_queue_timeout_ms = requested.server.generation_queue_timeout_ms;
        self.server.max_control_plane_in_flight_requests =
            requested.server.max_control_plane_in_flight_requests;
        self.server.max_request_body_bytes = requested.server.max_request_body_bytes;
        self.shielding = requested.shielding.clone();
        self.observability.enabled = requested.observability.enabled;
        self.observability.capture_raw_payloads = requested.observability.capture_raw_payloads;
        self.observability.metrics_enabled = requested.observability.metrics_enabled;
        self.observability.health_upstream_probe_enabled =
            requested.observability.health_upstream_probe_enabled;
        self.observability.health_upstream_probe_timeout_ms =
            requested.observability.health_upstream_probe_timeout_ms;
        self.observability.debug_summary_enabled = requested.observability.debug_summary_enabled;
        self.observability
            .debug_summary_admin_token
            .clone_from(&requested.observability.debug_summary_admin_token);
        self.observability.debug_summary_max_records =
            requested.observability.debug_summary_max_records;
        self.observability.retention = requested.observability.retention.clone();
        self.thinking = requested.thinking.clone();
        self.loop_guard = requested.loop_guard.clone();
        self.retry = requested.retry.clone();
        self.upstream_stall = requested.upstream_stall.clone();
        self.heartbeat = requested.heartbeat.clone();
        self.cloudflare = requested.cloudflare.clone();
        self.upstream.request_timeout_ms = requested.upstream.request_timeout_ms;
        self.upstream.metadata = requested.upstream.metadata.clone();
    }

    pub(crate) fn restart_required_changes(&self, requested: &Self) -> Vec<RestartRequiredChange> {
        let mut changes = Vec::new();
        push_change(
            &mut changes,
            "server.bind_host",
            self.server.bind_host.clone(),
            requested.server.bind_host.clone(),
        );
        push_change(
            &mut changes,
            "server.port",
            self.server.port.to_string(),
            requested.server.port.to_string(),
        );
        push_change(
            &mut changes,
            "upstream.base_url",
            self.upstream.base_url.clone(),
            requested.upstream.base_url.clone(),
        );
        push_change(
            &mut changes,
            "observability.sqlite_path",
            self.observability.sqlite_path.display().to_string(),
            requested.observability.sqlite_path.display().to_string(),
        );
        changes
    }
}

/// Listener settings read during process startup.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ServerConfig {
    /// Interface or hostname to bind.
    pub bind_host: String,
    /// TCP port for the proxy listener.
    pub port: u16,
    /// Maximum proxied requests admitted into body buffering and upstream forwarding.
    pub max_in_flight_requests: usize,
    /// Maximum generation requests allowed to wait for an in-flight slot.
    pub max_queued_generation_requests: usize,
    /// Maximum milliseconds a queued generation request may wait for capacity.
    pub generation_queue_timeout_ms: u64,
    /// Maximum `/v1/models` requests admitted into upstream forwarding.
    pub max_control_plane_in_flight_requests: usize,
    /// Maximum downstream request body bytes buffered before forwarding.
    pub max_request_body_bytes: usize,
}

impl ServerConfig {
    fn validate(&self) -> Result<(), ValidationError> {
        require(
            !self.bind_host.trim().is_empty(),
            "server.bind_host",
            "must not be empty",
        )?;
        require(self.port > 0, "server.port", "must be between 1 and 65535")?;
        require(
            self.max_in_flight_requests > 0,
            "server.max_in_flight_requests",
            "must be greater than zero",
        )?;
        require(
            self.max_queued_generation_requests <= 10_000,
            "server.max_queued_generation_requests",
            "must be less than or equal to 10000",
        )?;
        require(
            self.generation_queue_timeout_ms > 0,
            "server.generation_queue_timeout_ms",
            "must be greater than zero",
        )?;
        require(
            self.max_control_plane_in_flight_requests > 0,
            "server.max_control_plane_in_flight_requests",
            "must be greater than zero",
        )?;
        require(
            self.max_control_plane_in_flight_requests <= 1_024,
            "server.max_control_plane_in_flight_requests",
            "must be less than or equal to 1024",
        )?;
        require(
            self.max_request_body_bytes > 0,
            "server.max_request_body_bytes",
            "must be greater than zero",
        )?;
        require(
            self.max_request_body_bytes <= 1_073_741_824,
            "server.max_request_body_bytes",
            "must be less than or equal to 1073741824",
        )
    }
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            bind_host: String::from("127.0.0.1"),
            port: 18_009,
            max_in_flight_requests: 16,
            max_queued_generation_requests: 64,
            generation_queue_timeout_ms: 30_000,
            max_control_plane_in_flight_requests: 128,
            max_request_body_bytes: 67_108_864,
        }
    }
}

/// Upstream OpenAI-compatible service settings.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UpstreamConfig {
    /// Base URL for OpenAI-compatible requests.
    pub base_url: String,
    /// Total upstream request timeout, including streamed response body reads.
    pub request_timeout_ms: u64,
    /// Metadata discovery and model context enrichment policy.
    pub metadata: MetadataConfig,
}

impl UpstreamConfig {
    fn validate(&self) -> Result<(), ValidationError> {
        validate_upstream_base_url(&self.base_url)?;
        require(
            self.request_timeout_ms > 0,
            "upstream.request_timeout_ms",
            "must be greater than zero",
        )?;
        self.metadata.validate()
    }

    /// Returns a display-safe upstream base URL.
    ///
    /// Credentials and query strings are replaced and fragments are removed
    /// before the string is suitable for logs or client-visible diagnostics.
    #[must_use]
    pub fn redacted_base_url(&self) -> String {
        redact_upstream_base_url(&self.base_url)
    }
}

impl Default for UpstreamConfig {
    fn default() -> Self {
        Self {
            base_url: String::from("http://gb10:18009/v1"),
            request_timeout_ms: 120_000,
            metadata: MetadataConfig::default(),
        }
    }
}

/// Validates the configured upstream base URL.
///
/// # Errors
///
/// Returns a [`ValidationError`] when the URL is not absolute HTTP(S), includes
/// userinfo, contains query parameters, or includes a fragment.
pub fn validate_upstream_base_url(base_url: &str) -> Result<(), ValidationError> {
    let url = Url::parse(base_url).map_err(|_error| {
        ValidationError::new(
            "upstream.base_url",
            "must be a valid http:// or https:// URL",
        )
    })?;
    require(
        matches!(url.scheme(), "http" | "https"),
        "upstream.base_url",
        "must start with http:// or https://",
    )?;
    require(
        url.username().is_empty() && url.password().is_none(),
        "upstream.base_url",
        "must not contain username, password, or userinfo",
    )?;
    if url.query().is_some() {
        return Err(ValidationError::new(
            "upstream.base_url",
            "must not contain query parameters",
        ));
    }
    require(
        url.fragment().is_none(),
        "upstream.base_url",
        "must not contain URL fragments",
    )?;
    Ok(())
}

/// Returns a display-safe URL string for logs and diagnostics.
///
/// Invalid URLs are rendered as a fixed marker because preserving fragments of
/// malformed input risks echoing secrets embedded in an unparsable string.
#[must_use]
pub fn redact_upstream_base_url(base_url: &str) -> String {
    let Ok(mut url) = Url::parse(base_url) else {
        return INVALID_URL_DISPLAY.to_owned();
    };

    if !url.username().is_empty() {
        let _ignored = url.set_username(REDACTED_URL_PART);
    }
    if url.password().is_some() {
        let _ignored = url.set_password(Some(REDACTED_URL_PART));
    }
    if url.query().is_some() {
        url.set_query(Some(REDACTED_URL_PART));
    }
    url.set_fragment(None);

    url.to_string()
}

/// Upstream model metadata discovery policy.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetadataConfig {
    /// Enables polling `/v1/models` for model metadata.
    pub discovery_enabled: bool,
    /// Adds normalized context metadata to downstream model records.
    pub enrich_responses: bool,
    /// Refresh interval for metadata discovery.
    pub refresh_interval_secs: u64,
    /// Optional context length override used when upstream metadata is absent.
    pub context_length_override: Option<u32>,
    /// Optional `max_model_len` override used when upstream metadata is absent.
    pub max_model_len_override: Option<u32>,
}

impl MetadataConfig {
    fn validate(&self) -> Result<(), ValidationError> {
        require(
            self.refresh_interval_secs > 0,
            "upstream.metadata.refresh_interval_secs",
            "must be greater than zero",
        )?;
        require_optional_positive(
            self.context_length_override,
            "upstream.metadata.context_length_override",
        )?;
        require_optional_positive(
            self.max_model_len_override,
            "upstream.metadata.max_model_len_override",
        )
    }
}

impl Default for MetadataConfig {
    fn default() -> Self {
        Self {
            discovery_enabled: true,
            enrich_responses: true,
            refresh_interval_secs: 60,
            context_length_override: None,
            max_model_len_override: None,
        }
    }
}

/// Shielded-response behavior flags.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ShieldingConfig {
    /// Enables internal shielding before content is released downstream.
    pub enabled: bool,
}

impl Default for ShieldingConfig {
    fn default() -> Self {
        Self { enabled: true }
    }
}

/// Two-state config toggle used to keep endpoint switches explicit in the model.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConfigToggle {
    Disabled,
    Enabled,
}

impl ConfigToggle {
    #[must_use]
    pub const fn from_bool(enabled: bool) -> Self {
        if enabled {
            Self::Enabled
        } else {
            Self::Disabled
        }
    }

    #[must_use]
    pub const fn is_enabled(self) -> bool {
        matches!(self, Self::Enabled)
    }
}

/// Metadata capture and retention settings.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ObservabilityConfig {
    /// Enables indexed observability metadata.
    pub enabled: bool,
    /// `SQLite` metadata path. This is restart-required when changed.
    pub sqlite_path: PathBuf,
    /// Enables raw prompt/output sidecars for explicitly configured debugging.
    pub capture_raw_payloads: bool,
    /// Enables the Prometheus-compatible `/metrics` endpoint.
    pub metrics_enabled: ConfigToggle,
    /// Enables bounded upstream probing from `/health`.
    pub health_upstream_probe_enabled: ConfigToggle,
    /// Maximum time spent probing upstream readiness from `/health`.
    pub health_upstream_probe_timeout_ms: u64,
    /// Enables the gated recent-request debug summary endpoint.
    pub debug_summary_enabled: ConfigToggle,
    /// Optional bearer/admin token required for the debug summary endpoint.
    pub debug_summary_admin_token: Option<String>,
    /// Maximum recent request summaries returned by one debug response.
    pub debug_summary_max_records: u32,
    /// Retention limits for metadata and artifacts.
    pub retention: RetentionConfig,
}

impl ObservabilityConfig {
    fn validate(&self) -> Result<(), ValidationError> {
        require(
            !self.sqlite_path.as_os_str().is_empty(),
            "observability.sqlite_path",
            "must not be empty",
        )?;
        require(
            self.health_upstream_probe_timeout_ms > 0,
            "observability.health_upstream_probe_timeout_ms",
            "must be greater than zero",
        )?;
        require(
            self.health_upstream_probe_timeout_ms <= 30_000,
            "observability.health_upstream_probe_timeout_ms",
            "must be less than or equal to 30000",
        )?;
        require(
            self.debug_summary_max_records > 0,
            "observability.debug_summary_max_records",
            "must be greater than zero",
        )?;
        require(
            self.debug_summary_max_records <= 100,
            "observability.debug_summary_max_records",
            "must be less than or equal to 100",
        )?;
        if let Some(token) = &self.debug_summary_admin_token {
            require(
                !token.trim().is_empty(),
                "observability.debug_summary_admin_token",
                "must not be empty when set",
            )?;
        }
        self.retention.validate()
    }
}

impl Default for ObservabilityConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            sqlite_path: PathBuf::from("~/.local/state/llm-guard-proxy/observability.sqlite3"),
            capture_raw_payloads: false,
            metrics_enabled: ConfigToggle::Enabled,
            health_upstream_probe_enabled: ConfigToggle::Enabled,
            health_upstream_probe_timeout_ms: 500,
            debug_summary_enabled: ConfigToggle::Disabled,
            debug_summary_admin_token: None,
            debug_summary_max_records: 20,
            retention: RetentionConfig::default(),
        }
    }
}

/// Retention limits for observability records and artifacts.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetentionConfig {
    /// Hard maximum actual storage budget in bytes.
    ///
    /// `SQLite` stores schema and page metadata, so a database cannot shrink
    /// below its empty schema footprint.
    pub max_bytes: u64,
    /// Hysteresis target after pruning.
    ///
    /// Values below the empty `SQLite` footprint prune rows but cannot reduce
    /// the database file below that storage floor.
    pub prune_to_bytes: u64,
    /// Maximum indexed record count.
    pub max_records: u64,
    /// Hysteresis target after record-count pruning.
    ///
    /// When omitted, the effective target is derived from `max_records`.
    pub prune_to_records: Option<u64>,
}

impl RetentionConfig {
    /// Effective record-count pruning target.
    ///
    /// Omitted config defaults to 80% of `max_records`, with a minimum target
    /// of one retained record for very small test configurations.
    #[must_use]
    pub fn effective_prune_to_records(&self) -> u64 {
        self.prune_to_records
            .unwrap_or_else(|| default_prune_to_records(self.max_records))
    }

    fn validate(&self) -> Result<(), ValidationError> {
        require(
            self.max_bytes > 0,
            "observability.retention.max_bytes",
            "must be greater than zero",
        )?;
        require(
            self.prune_to_bytes > 0,
            "observability.retention.prune_to_bytes",
            "must be greater than zero",
        )?;
        require(
            self.prune_to_bytes <= self.max_bytes,
            "observability.retention.prune_to_bytes",
            "must be less than or equal to max_bytes",
        )?;
        require(
            self.max_records > 0,
            "observability.retention.max_records",
            "must be greater than zero",
        )?;
        if let Some(prune_to_records) = self.prune_to_records {
            require(
                prune_to_records > 0,
                "observability.retention.prune_to_records",
                "must be greater than zero",
            )?;
            require(
                prune_to_records <= self.max_records,
                "observability.retention.prune_to_records",
                "must be less than or equal to max_records",
            )?;
        }
        Ok(())
    }
}

impl Default for RetentionConfig {
    fn default() -> Self {
        Self {
            max_bytes: 1_073_741_824,
            prune_to_bytes: 805_306_368,
            max_records: 100_000,
            prune_to_records: None,
        }
    }
}

const fn default_prune_to_records(max_records: u64) -> u64 {
    let gap = max_records / 5;
    let gap = if gap == 0 { 1 } else { gap };
    let target = max_records.saturating_sub(gap);
    if target == 0 { 1 } else { target }
}

/// Thinking-budget behavior for requests that carry tool/function-calling hints.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum ToolRequestThinkingPolicy {
    /// Apply the regular thinking policy to every chat request, including tool-use.
    #[default]
    Apply,
    /// Leave caller-provided thinking fields untouched for tool-use requests.
    Passthrough,
}

impl ToolRequestThinkingPolicy {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Apply => "apply",
            Self::Passthrough => "passthrough",
        }
    }
}

/// Thinking budget policy for later request rewriting.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ThinkingConfig {
    /// Enables default thinking budget injection.
    pub enabled: bool,
    /// Forces all recognized upstream thinking budgets to zero.
    pub force_disable: bool,
    /// Thinking token budget. A zero budget disables injection.
    pub budget_tokens: u32,
    /// Adjusts `max_tokens` so callers keep their apparent answer budget.
    pub preserve_answer_budget: bool,
    /// Request-class policy for tool/function-calling requests.
    pub tool_request_policy: ToolRequestThinkingPolicy,
}

impl Default for ThinkingConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            force_disable: false,
            budget_tokens: 32_768,
            preserve_answer_budget: true,
            tool_request_policy: ToolRequestThinkingPolicy::Apply,
        }
    }
}

/// Loop detection policy for repeated normalized inputs.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LoopGuardConfig {
    /// Enables normalized repeated-input detection.
    pub enabled: bool,
    /// Time window used to compare normalized inputs.
    pub normalized_input_window_secs: u64,
    /// Repeat threshold that triggers loop-protection behavior.
    pub max_repeated_inputs: u32,
    /// Repeated complete output lines required before aborting.
    pub output_repeated_line_threshold: u32,
    /// Normalized output token window size used for repetition detection.
    pub output_token_window_size: u32,
    /// Repeated normalized token windows required before aborting.
    pub output_repeated_token_window_threshold: u32,
    /// Suffix cycle repetitions required before aborting.
    pub output_suffix_cycle_threshold: u32,
    /// Minimum channel bytes before low-progress detection can abort.
    pub output_low_progress_min_bytes: u64,
    /// Maximum unique token-window ratio allowed for low-progress detection.
    pub output_low_progress_unique_ratio_percent: u32,
    /// Threshold multiplier applied when output repetition overlaps repeated input.
    pub input_overlap_threshold_multiplier: u32,
    /// Enables semantic Jaccard detection for reasoning/thinking streams.
    pub reasoning_semantic_detection_enabled: bool,
    /// Minimum Jaccard similarity required to flag a semantic reasoning loop.
    pub reasoning_semantic_similarity_threshold_percent: u32,
    /// Significant reasoning tokens kept in each semantic comparison window.
    pub reasoning_semantic_window_token_count: u32,
    /// Significant tokens required before a partial semantic window can be compared.
    pub reasoning_semantic_minimum_token_count: u32,
    /// Maximum completed semantic windows kept for bounded history comparison.
    pub reasoning_semantic_history_window_count: u32,
}

impl LoopGuardConfig {
    fn validate(&self) -> Result<(), ValidationError> {
        require(
            self.normalized_input_window_secs > 0,
            "loop_guard.normalized_input_window_secs",
            "must be greater than zero",
        )?;
        require(
            self.max_repeated_inputs > 0,
            "loop_guard.max_repeated_inputs",
            "must be greater than zero",
        )?;
        require(
            self.output_repeated_line_threshold > 0,
            "loop_guard.output_repeated_line_threshold",
            "must be greater than zero",
        )?;
        require(
            self.output_token_window_size > 0,
            "loop_guard.output_token_window_size",
            "must be greater than zero",
        )?;
        require(
            self.output_repeated_token_window_threshold > 0,
            "loop_guard.output_repeated_token_window_threshold",
            "must be greater than zero",
        )?;
        require(
            self.output_suffix_cycle_threshold > 0,
            "loop_guard.output_suffix_cycle_threshold",
            "must be greater than zero",
        )?;
        require(
            self.output_low_progress_min_bytes > 0,
            "loop_guard.output_low_progress_min_bytes",
            "must be greater than zero",
        )?;
        require(
            self.output_low_progress_unique_ratio_percent <= 100,
            "loop_guard.output_low_progress_unique_ratio_percent",
            "must be between 0 and 100",
        )?;
        require(
            self.input_overlap_threshold_multiplier > 0,
            "loop_guard.input_overlap_threshold_multiplier",
            "must be greater than zero",
        )?;
        require(
            (1..=100).contains(&self.reasoning_semantic_similarity_threshold_percent),
            "loop_guard.reasoning_semantic_similarity_threshold_percent",
            "must be between 1 and 100",
        )?;
        require(
            self.reasoning_semantic_window_token_count > 0
                && self.reasoning_semantic_window_token_count
                    <= LOOP_GUARD_MAX_SEMANTIC_WINDOW_TOKENS,
            "loop_guard.reasoning_semantic_window_token_count",
            "must be between 1 and 256",
        )?;
        require(
            self.reasoning_semantic_minimum_token_count > 0,
            "loop_guard.reasoning_semantic_minimum_token_count",
            "must be greater than zero",
        )?;
        require(
            self.reasoning_semantic_minimum_token_count
                <= self.reasoning_semantic_window_token_count,
            "loop_guard.reasoning_semantic_minimum_token_count",
            "must be less than or equal to loop_guard.reasoning_semantic_window_token_count",
        )?;
        require(
            self.reasoning_semantic_history_window_count > 0
                && self.reasoning_semantic_history_window_count
                    <= LOOP_GUARD_MAX_SEMANTIC_HISTORY_WINDOWS,
            "loop_guard.reasoning_semantic_history_window_count",
            "must be between 1 and 256",
        )
    }
}

impl Default for LoopGuardConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            normalized_input_window_secs: 120,
            max_repeated_inputs: 1,
            output_repeated_line_threshold: 24,
            output_token_window_size: 12,
            output_repeated_token_window_threshold: 32,
            output_suffix_cycle_threshold: 32,
            output_low_progress_min_bytes: 4_096,
            output_low_progress_unique_ratio_percent: 15,
            input_overlap_threshold_multiplier: 4,
            reasoning_semantic_detection_enabled: true,
            reasoning_semantic_similarity_threshold_percent: 55,
            reasoning_semantic_window_token_count: 24,
            reasoning_semantic_minimum_token_count: 8,
            reasoning_semantic_history_window_count: 16,
        }
    }
}

/// Retry policy for shielded upstream attempts.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetryConfig {
    /// Enables retry after a bad or failed shielded attempt.
    pub enabled: bool,
    /// Total upstream attempts, including the first attempt.
    pub max_attempts: u32,
    /// Adds a bounded deterministic anti-loop hint to retries after loop aborts.
    pub anti_loop_hint_enabled: bool,
}

impl RetryConfig {
    fn validate(&self) -> Result<(), ValidationError> {
        require(
            self.max_attempts > 0,
            "retry.max_attempts",
            "must be greater than zero",
        )?;
        require(
            self.max_attempts <= 10,
            "retry.max_attempts",
            "must be less than or equal to 10",
        )
    }
}

impl Default for RetryConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            max_attempts: 5,
            anti_loop_hint_enabled: true,
        }
    }
}

/// Upstream no-progress detection and optional recovery hook.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UpstreamStallConfig {
    /// Enables shielded chat aggregation idle-timeout detection.
    pub enabled: bool,
    /// Maximum milliseconds to wait for the next upstream SSE chunk.
    pub idle_timeout_ms: u64,
    /// Optional argv command run after a stall before retrying.
    ///
    /// Empty means recovery is disabled. Each element is passed as a single
    /// argv item; the proxy never invokes a shell. A configured command must
    /// perform the complete recovery procedure, including any restart and
    /// post-restart readiness or smoke checks, because retries are allowed
    /// only after this command exits successfully.
    pub recovery_command: Vec<String>,
    /// Maximum milliseconds to wait for the recovery command.
    pub recovery_timeout_ms: u64,
    /// Minimum milliseconds between completed recovery command executions.
    pub recovery_cooldown_ms: u64,
    /// Rolling window used by the recovery execution budget.
    pub recovery_budget_window_ms: u64,
    /// Maximum recovery command executions inside one budget window.
    pub recovery_max_per_window: u32,
}

impl UpstreamStallConfig {
    fn validate(&self) -> Result<(), ValidationError> {
        require(
            self.idle_timeout_ms > 0,
            "upstream.stall.idle_timeout_ms",
            "must be greater than zero",
        )?;
        require(
            self.recovery_timeout_ms > 0,
            "upstream.stall.recovery_timeout_ms",
            "must be greater than zero",
        )?;
        require(
            self.recovery_command
                .iter()
                .all(|argument| !argument.trim().is_empty()),
            "upstream.stall.recovery_command",
            "must not contain empty argv entries",
        )?;
        require(
            self.recovery_cooldown_ms > 0,
            "upstream.stall.recovery_cooldown_ms",
            "must be greater than zero",
        )?;
        require(
            self.recovery_budget_window_ms > 0,
            "upstream.stall.recovery_budget_window_ms",
            "must be greater than zero",
        )?;
        require(
            self.recovery_budget_window_ms <= 86_400_000,
            "upstream.stall.recovery_budget_window_ms",
            "must be less than or equal to 86400000",
        )?;
        require(
            self.recovery_max_per_window > 0,
            "upstream.stall.recovery_max_per_window",
            "must be greater than zero",
        )?;
        require(
            self.recovery_max_per_window <= 100,
            "upstream.stall.recovery_max_per_window",
            "must be less than or equal to 100",
        )
    }
}

impl Default for UpstreamStallConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            idle_timeout_ms: 30_000,
            recovery_command: Vec::new(),
            recovery_timeout_ms: 300_000,
            recovery_cooldown_ms: 300_000,
            recovery_budget_window_ms: 900_000,
            recovery_max_per_window: 2,
        }
    }
}

/// Downstream heartbeat strategy.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HeartbeatConfig {
    /// Liveness mode used while the proxy shields upstream attempts.
    pub mode: HeartbeatMode,
    /// Heartbeat interval for streaming or whitespace progress.
    pub interval_secs: u64,
}

impl HeartbeatConfig {
    fn validate(&self) -> Result<(), ValidationError> {
        require(
            self.interval_secs > 0,
            "heartbeat.interval_secs",
            "must be greater than zero",
        )
    }
}

impl Default for HeartbeatConfig {
    fn default() -> Self {
        Self {
            mode: HeartbeatMode::Sse,
            interval_secs: 15,
        }
    }
}

/// Supported downstream heartbeat modes.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum HeartbeatMode {
    /// Server-sent-event heartbeat/progress frames.
    #[default]
    Sse,
    /// Leading whitespace heartbeat for non-stream JSON responses.
    JsonWhitespace,
    /// No heartbeat.
    Disabled,
}

impl HeartbeatMode {
    /// Returns the TOML-compatible mode label.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Sse => "sse",
            Self::JsonWhitespace => "json-whitespace",
            Self::Disabled => "disabled",
        }
    }
}

/// Cloudflare compatibility policy reserved for timeout-sensitive deployments.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CloudflareConfig {
    /// Enables Cloudflare-aware timeout shielding behavior in future service code.
    pub enabled: bool,
}

impl Default for CloudflareConfig {
    fn default() -> Self {
        Self { enabled: true }
    }
}

/// A restart-required field change detected during reload.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RestartRequiredChange {
    /// Config field name.
    pub field: &'static str,
    /// Currently active value.
    pub active: String,
    /// Requested value from the reloaded file.
    pub requested: String,
}

fn push_change(
    changes: &mut Vec<RestartRequiredChange>,
    field: &'static str,
    active: String,
    requested: String,
) {
    if active != requested {
        changes.push(RestartRequiredChange {
            field,
            active,
            requested,
        });
    }
}

fn require(
    condition: bool,
    field: &'static str,
    message: &'static str,
) -> Result<(), ValidationError> {
    if condition {
        Ok(())
    } else {
        Err(ValidationError::new(field, message))
    }
}

fn require_optional_positive(
    value: Option<u32>,
    field: &'static str,
) -> Result<(), ValidationError> {
    require(value.unwrap_or(1) > 0, field, "must be greater than zero")
}
