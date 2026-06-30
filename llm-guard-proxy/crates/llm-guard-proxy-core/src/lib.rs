#![forbid(unsafe_code)]
//! Headless core types for the `llm-guard-proxy` service.
//!
//! Issue #1 intentionally kept this crate small. Later issues add proxy,
//! retry, and request-shielding behavior behind core interfaces.

mod observability;
mod settings;

pub use observability::{
    AttemptId, AttemptMetricCount, AttemptRecord, AttemptStatus, DebugRequestSummary,
    DownstreamMode, HeartbeatModeMetricCount, HistogramBucket, LatencyHistogram,
    ObservabilityError, ObservabilityMetricsSnapshot, ObservabilityStore, RawPayloads, RequestId,
    RequestMetricCount, RequestRecord, RequestStatus, RetentionPruningStats, RetentionUsage,
    StoreWrite, UpstreamErrorMetricCount, UpstreamMode,
};
pub use settings::{
    AppConfig, CloudflareConfig, ConfigError, ConfigHandle, ConfigManager, ConfigParseError,
    DEFAULT_CONFIG_RELATIVE_PATH, HeartbeatConfig, HeartbeatMode, LoopGuardConfig, MetadataConfig,
    MissingConfigPolicy, ObservabilityConfig, RELOADABLE_FIELDS, RESTART_REQUIRED_FIELDS,
    ReloadOutcome, ReloadWatcher, RestartRequiredChange, RetentionConfig, RetryConfig,
    ServerConfig, ShieldingConfig, ThinkingConfig, ToolRequestThinkingPolicy, UpstreamConfig,
    UpstreamStallConfig, ValidationError, default_config_path, redact_upstream_base_url,
    validate_upstream_base_url,
};

/// Public service name used by the binary and documentation.
pub const SERVICE_NAME: &str = "llm-guard-proxy";

/// Repository license identifier.
pub const LICENSE: &str = "Apache-2.0";

/// Current process readiness state.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Readiness {
    /// The placeholder process has started and can answer health checks.
    Ready,
}

impl Readiness {
    /// Returns the stable wire/display label for the readiness state.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ready => "ready",
        }
    }
}

/// Minimal health model shared by the service entry point and tests.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Health {
    readiness: Readiness,
}

impl Health {
    /// Builds the current placeholder health response.
    #[must_use]
    pub const fn current() -> Self {
        Self {
            readiness: Readiness::Ready,
        }
    }

    /// Returns the process readiness value.
    #[must_use]
    pub const fn readiness(self) -> Readiness {
        self.readiness
    }

    /// Returns whether this process is ready to accept health checks.
    #[must_use]
    pub const fn is_ready(self) -> bool {
        matches!(self.readiness, Readiness::Ready)
    }
}

impl Default for Health {
    fn default() -> Self {
        Self::current()
    }
}

#[cfg(test)]
mod tests {
    use super::{Health, LICENSE, Readiness, SERVICE_NAME};

    #[test]
    fn health_defaults_to_ready() {
        let health = Health::default();

        assert!(health.is_ready());
        assert_eq!(health.readiness(), Readiness::Ready);
        assert_eq!(health.readiness().as_str(), "ready");
    }

    #[test]
    fn service_metadata_matches_repository_contract() {
        assert_eq!(SERVICE_NAME, "llm-guard-proxy");
        assert_eq!(LICENSE, "Apache-2.0");
    }
}
