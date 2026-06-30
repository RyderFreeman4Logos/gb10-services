//! `SQLite`-backed observability storage for request and attempt metadata.
//!
//! The store is intentionally headless: callers provide already-derived
//! request metadata, attempt metadata, timing, modes, and payload fragments.
//! Redaction and raw-payload capture policy are enforced at this boundary before
//! anything reaches `SQLite`.

mod error;
mod ids;
mod model;
mod redaction;
mod store;

#[cfg(test)]
mod tests;

pub use error::ObservabilityError;
pub use ids::{AttemptId, RequestId};
pub use model::{
    AttemptMetricCount, AttemptRecord, AttemptStatus, DebugRequestSummary, DownstreamMode,
    HeartbeatModeMetricCount, HistogramBucket, LatencyHistogram, ObservabilityMetricsSnapshot,
    RawPayloads, RequestMetricCount, RequestRecord, RequestStatus, RetentionPruningStats,
    RetentionUsage, StoreWrite, UpstreamErrorMetricCount, UpstreamMode,
};
pub use store::ObservabilityStore;
