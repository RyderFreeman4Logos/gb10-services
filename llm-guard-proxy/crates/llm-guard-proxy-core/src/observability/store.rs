use std::{
    collections::BTreeMap,
    env, fs,
    io::ErrorKind,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, MutexGuard},
    time::{SystemTime, UNIX_EPOCH},
};

#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};

use rusqlite::{Connection, OpenFlags, params};

use super::{
    error::ObservabilityError,
    model::{
        AttemptMetricCount, AttemptRecord, DebugRequestSummary, HeartbeatModeMetricCount,
        HistogramBucket, LatencyHistogram, ObservabilityMetricsSnapshot, RawPayloads,
        RequestMetricCount, RequestRecord, RetentionPruningStats, RetentionUsage, StoreWrite,
        UpstreamErrorMetricCount,
    },
    redaction::{
        debug_safe_metadata_map, redacted_metadata_json, sanitize_optional_text,
        sanitize_raw_payloads,
    },
};
use crate::{ConfigHandle, RetentionConfig};

const SCHEMA_VERSION: i64 = 2;
const HISTOGRAM_BUCKETS_MS: &[u64] = &[
    10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000, 60_000,
];
const MAX_DEBUG_SUMMARY_LIMIT: u32 = 100;
#[cfg(unix)]
const OBSERVABILITY_DIRECTORY_MODE: u32 = 0o700;
#[cfg(unix)]
const OBSERVABILITY_SQLITE_MODE: u32 = 0o600;

/// `SQLite`-backed observability store.
#[derive(Clone, Debug)]
pub struct ObservabilityStore {
    config: ConfigHandle,
    connection: Arc<Mutex<Connection>>,
}

impl ObservabilityStore {
    /// Opens the configured `SQLite` database and applies schema migrations.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError`] when config cannot be read, the database
    /// path cannot be prepared, `SQLite` cannot open, or migration fails.
    pub fn open(config: ConfigHandle) -> Result<Self, ObservabilityError> {
        let snapshot = config.snapshot()?;
        let sqlite_path = resolve_sqlite_path(&snapshot.observability.sqlite_path)?;
        prepare_parent_directory(&sqlite_path)?;
        prepare_sqlite_file(&sqlite_path)?;
        let connection = open_sqlite_connection(&sqlite_path)?;
        connection
            .pragma_update(None, "foreign_keys", "ON")
            .map_err(|source| ObservabilityError::Sqlite {
                action: "enable SQLite foreign keys",
                source,
            })?;
        migrate(&connection)?;

        Ok(Self {
            config,
            connection: Arc::new(Mutex::new(connection)),
        })
    }

    /// Returns the current `SQLite` schema version.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError`] when the database lock is poisoned or the
    /// schema version cannot be read.
    pub fn schema_version(&self) -> Result<u32, ObservabilityError> {
        let connection = self.lock_connection()?;
        let version = read_schema_version(&connection)?;
        u32::try_from(version).map_err(|_error| ObservabilityError::UnsupportedSchemaVersion {
            version,
            supported: SCHEMA_VERSION,
        })
    }

    /// Persists one downstream request record.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError`] when current settings cannot be read,
    /// metadata cannot serialize, the database lock is poisoned, or `SQLite`
    /// persistence fails.
    pub fn record_request(&self, record: &RequestRecord) -> Result<StoreWrite, ObservabilityError> {
        let settings = self.config.snapshot()?;
        if !settings.observability.enabled {
            return Ok(StoreWrite::Disabled);
        }

        let prepared =
            PreparedRequest::from_record(record, settings.observability.capture_raw_payloads)?;
        let mut connection = self.lock_connection()?;
        insert_request(&mut connection, &prepared)?;
        let pruning = enforce_retention(&mut connection, &settings.observability.retention)?;
        record_pruning_outcome(&mut connection, &pruning)?;
        Ok(StoreWrite::Written)
    }

    /// Persists one upstream attempt record.
    ///
    /// Callers should write the parent request row before writing attempt rows
    /// so retention can delete a request and its attempts as one unit.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError`] when current settings cannot be read,
    /// metadata cannot serialize, the database lock is poisoned, or `SQLite`
    /// persistence fails.
    pub fn record_attempt(&self, record: &AttemptRecord) -> Result<StoreWrite, ObservabilityError> {
        let settings = self.config.snapshot()?;
        if !settings.observability.enabled {
            return Ok(StoreWrite::Disabled);
        }

        let prepared =
            PreparedAttempt::from_record(record, settings.observability.capture_raw_payloads)?;
        let mut connection = self.lock_connection()?;
        insert_attempt(&mut connection, &prepared)?;
        let pruning = enforce_retention(&mut connection, &settings.observability.retention)?;
        record_pruning_outcome(&mut connection, &pruning)?;
        Ok(StoreWrite::Written)
    }

    /// Returns logical retention usage for tests and diagnostics.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError`] when the database lock is poisoned or
    /// usage cannot be read.
    pub fn retention_usage(&self) -> Result<RetentionUsage, ObservabilityError> {
        let connection = self.lock_connection()?;
        read_retention_usage(&connection)
    }

    /// Returns aggregate metrics derived from retained observability rows.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError`] when the database lock is poisoned or
    /// metric rows cannot be read.
    pub fn metrics_snapshot(&self) -> Result<ObservabilityMetricsSnapshot, ObservabilityError> {
        let connection = self.lock_connection()?;
        read_metrics_snapshot(&connection)
    }

    /// Returns bounded, redacted summaries of recent requests.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError`] when the database lock is poisoned or the
    /// summary query cannot be read.
    pub fn recent_request_summaries(
        &self,
        limit: u32,
    ) -> Result<Vec<DebugRequestSummary>, ObservabilityError> {
        let connection = self.lock_connection()?;
        read_recent_request_summaries(&connection, limit.min(MAX_DEBUG_SUMMARY_LIMIT))
    }

    pub(super) fn lock_connection(&self) -> Result<MutexGuard<'_, Connection>, ObservabilityError> {
        self.connection
            .lock()
            .map_err(|_error| ObservabilityError::LockPoisoned)
    }
}

#[derive(Debug)]
struct PreparedRequest {
    request_id: String,
    started_at_unix_ms: i64,
    finished_at_unix_ms: Option<i64>,
    duration_ms: Option<i64>,
    downstream_mode: &'static str,
    upstream_mode: &'static str,
    model_id: Option<String>,
    input_fingerprint: Option<String>,
    status: &'static str,
    http_status: Option<i64>,
    error_reason: Option<String>,
    abort_reason: Option<String>,
    request_metadata_json: String,
    response_metadata_json: String,
    raw_payloads: RawPayloads,
    estimated_bytes: i64,
}

impl PreparedRequest {
    fn from_record(
        record: &RequestRecord,
        capture_raw_payloads: bool,
    ) -> Result<Self, ObservabilityError> {
        let request_metadata_json = redacted_metadata_json(&record.request_metadata, "request")?;
        let response_metadata_json = redacted_metadata_json(&record.response_metadata, "response")?;
        let raw_payloads = sanitize_raw_payloads(&record.raw_payloads, capture_raw_payloads);
        let estimated_bytes = estimate_request_bytes(
            record,
            &request_metadata_json,
            &response_metadata_json,
            &raw_payloads,
        )?;

        Ok(Self {
            request_id: record.request_id.as_str().to_owned(),
            started_at_unix_ms: to_sqlite_i64(record.started_at_unix_ms, "started_at_unix_ms")?,
            finished_at_unix_ms: optional_to_sqlite_i64(
                record.finished_at_unix_ms,
                "finished_at_unix_ms",
            )?,
            duration_ms: optional_to_sqlite_i64(
                duration_ms(record.started_at_unix_ms, record.finished_at_unix_ms),
                "duration_ms",
            )?,
            downstream_mode: record.downstream_mode.as_str(),
            upstream_mode: record.upstream_mode.as_str(),
            model_id: sanitize_optional_text(record.model_id.as_ref()),
            input_fingerprint: sanitize_optional_text(record.input_fingerprint.as_ref()),
            status: record.status.as_str(),
            http_status: record.http_status.map(i64::from),
            error_reason: sanitize_optional_text(record.error_reason.as_ref()),
            abort_reason: sanitize_optional_text(record.abort_reason.as_ref()),
            request_metadata_json,
            response_metadata_json,
            raw_payloads,
            estimated_bytes,
        })
    }
}

#[derive(Debug)]
struct PreparedAttempt {
    attempt_id: String,
    request_id: String,
    attempt_number: i64,
    started_at_unix_ms: i64,
    finished_at_unix_ms: Option<i64>,
    duration_ms: Option<i64>,
    upstream_mode: &'static str,
    status: &'static str,
    http_status: Option<i64>,
    error_reason: Option<String>,
    retry_reason: Option<String>,
    abort_reason: Option<String>,
    request_metadata_json: String,
    response_metadata_json: String,
    raw_payloads: RawPayloads,
    estimated_bytes: i64,
}

impl PreparedAttempt {
    fn from_record(
        record: &AttemptRecord,
        capture_raw_payloads: bool,
    ) -> Result<Self, ObservabilityError> {
        let request_metadata_json =
            redacted_metadata_json(&record.request_metadata, "attempt request")?;
        let response_metadata_json =
            redacted_metadata_json(&record.response_metadata, "attempt response")?;
        let raw_payloads = sanitize_raw_payloads(&record.raw_payloads, capture_raw_payloads);
        let estimated_bytes = estimate_attempt_bytes(
            record,
            &request_metadata_json,
            &response_metadata_json,
            &raw_payloads,
        )?;

        Ok(Self {
            attempt_id: record.attempt_id.as_str().to_owned(),
            request_id: record.request_id.as_str().to_owned(),
            attempt_number: i64::from(record.attempt_number),
            started_at_unix_ms: to_sqlite_i64(record.started_at_unix_ms, "started_at_unix_ms")?,
            finished_at_unix_ms: optional_to_sqlite_i64(
                record.finished_at_unix_ms,
                "finished_at_unix_ms",
            )?,
            duration_ms: optional_to_sqlite_i64(
                duration_ms(record.started_at_unix_ms, record.finished_at_unix_ms),
                "duration_ms",
            )?,
            upstream_mode: record.upstream_mode.as_str(),
            status: record.status.as_str(),
            http_status: record.http_status.map(i64::from),
            error_reason: sanitize_optional_text(record.error_reason.as_ref()),
            retry_reason: sanitize_optional_text(record.retry_reason.as_ref()),
            abort_reason: sanitize_optional_text(record.abort_reason.as_ref()),
            request_metadata_json,
            response_metadata_json,
            raw_payloads,
            estimated_bytes,
        })
    }
}

fn migrate(connection: &Connection) -> Result<(), ObservabilityError> {
    let version = read_schema_version(connection)?;
    if version > SCHEMA_VERSION {
        return Err(ObservabilityError::UnsupportedSchemaVersion {
            version,
            supported: SCHEMA_VERSION,
        });
    }
    if version == SCHEMA_VERSION {
        return Ok(());
    }

    if version == 0 {
        create_initial_schema(connection)?;
    }
    if version < 2 {
        migrate_to_schema_v2(connection)?;
    }
    Ok(())
}

fn create_initial_schema(connection: &Connection) -> Result<(), ObservabilityError> {
    connection
        .execute_batch(
            r"
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    started_at_unix_ms INTEGER NOT NULL,
    finished_at_unix_ms INTEGER,
    duration_ms INTEGER,
    downstream_mode TEXT NOT NULL,
    upstream_mode TEXT NOT NULL,
    model_id TEXT,
    input_fingerprint TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    error_reason TEXT,
    abort_reason TEXT,
    request_metadata_json TEXT NOT NULL,
    response_metadata_json TEXT NOT NULL,
    raw_input TEXT,
    raw_output TEXT,
    raw_reasoning TEXT,
    raw_tool_calls TEXT,
    estimated_bytes INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS requests_started_at_idx
    ON requests(started_at_unix_ms, request_id);
CREATE INDEX IF NOT EXISTS requests_input_fingerprint_idx
    ON requests(input_fingerprint);
CREATE INDEX IF NOT EXISTS requests_model_id_idx
    ON requests(model_id);
CREATE INDEX IF NOT EXISTS requests_status_idx
    ON requests(status);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests(request_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    started_at_unix_ms INTEGER NOT NULL,
    finished_at_unix_ms INTEGER,
    duration_ms INTEGER,
    upstream_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    error_reason TEXT,
    retry_reason TEXT,
    abort_reason TEXT,
    request_metadata_json TEXT NOT NULL,
    response_metadata_json TEXT NOT NULL,
    raw_input TEXT,
    raw_output TEXT,
    raw_reasoning TEXT,
    raw_tool_calls TEXT,
    estimated_bytes INTEGER NOT NULL,
    UNIQUE(request_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS attempts_request_id_idx
    ON attempts(request_id);
CREATE INDEX IF NOT EXISTS attempts_status_idx
    ON attempts(status);
",
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "create SQLite observability schema",
            source,
        })
}

fn migrate_to_schema_v2(connection: &Connection) -> Result<(), ObservabilityError> {
    connection
        .execute_batch(
            r"
CREATE TABLE IF NOT EXISTS retention_pruning_stats (
    stats_key TEXT PRIMARY KEY,
    prune_events INTEGER NOT NULL,
    pruned_requests INTEGER NOT NULL,
    pruned_attempts INTEGER NOT NULL,
    last_pruned_at_unix_ms INTEGER
);

INSERT OR IGNORE INTO retention_pruning_stats (
    stats_key,
    prune_events,
    pruned_requests,
    pruned_attempts,
    last_pruned_at_unix_ms
) VALUES ('global', 0, 0, 0, NULL);

PRAGMA user_version = 2;
",
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "migrate SQLite observability schema to v2",
            source,
        })
}

fn read_schema_version(connection: &Connection) -> Result<i64, ObservabilityError> {
    connection
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read SQLite observability schema version",
            source,
        })
}

fn insert_request(
    connection: &mut Connection,
    record: &PreparedRequest,
) -> Result<(), ObservabilityError> {
    let transaction = connection
        .transaction()
        .map_err(|source| ObservabilityError::Sqlite {
            action: "start request observability transaction",
            source,
        })?;
    transaction
        .execute(
            r"
INSERT INTO requests (
    request_id,
    started_at_unix_ms,
    finished_at_unix_ms,
    duration_ms,
    downstream_mode,
    upstream_mode,
    model_id,
    input_fingerprint,
    status,
    http_status,
    error_reason,
    abort_reason,
    request_metadata_json,
    response_metadata_json,
    raw_input,
    raw_output,
    raw_reasoning,
    raw_tool_calls,
    estimated_bytes
) VALUES (
    ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10,
    ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19
)
ON CONFLICT(request_id) DO UPDATE SET
    started_at_unix_ms = excluded.started_at_unix_ms,
    finished_at_unix_ms = excluded.finished_at_unix_ms,
    duration_ms = excluded.duration_ms,
    downstream_mode = excluded.downstream_mode,
    upstream_mode = excluded.upstream_mode,
    model_id = excluded.model_id,
    input_fingerprint = excluded.input_fingerprint,
    status = excluded.status,
    http_status = excluded.http_status,
    error_reason = excluded.error_reason,
    abort_reason = excluded.abort_reason,
    request_metadata_json = excluded.request_metadata_json,
    response_metadata_json = excluded.response_metadata_json,
    raw_input = excluded.raw_input,
    raw_output = excluded.raw_output,
    raw_reasoning = excluded.raw_reasoning,
    raw_tool_calls = excluded.raw_tool_calls,
    estimated_bytes = excluded.estimated_bytes
",
            params![
                record.request_id,
                record.started_at_unix_ms,
                record.finished_at_unix_ms,
                record.duration_ms,
                record.downstream_mode,
                record.upstream_mode,
                record.model_id,
                record.input_fingerprint,
                record.status,
                record.http_status,
                record.error_reason,
                record.abort_reason,
                record.request_metadata_json,
                record.response_metadata_json,
                record.raw_payloads.input,
                record.raw_payloads.output,
                record.raw_payloads.reasoning,
                record.raw_payloads.tool_calls,
                record.estimated_bytes,
            ],
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "write request observability row",
            source,
        })?;
    transaction
        .commit()
        .map_err(|source| ObservabilityError::Sqlite {
            action: "commit request observability transaction",
            source,
        })
}

fn insert_attempt(
    connection: &mut Connection,
    record: &PreparedAttempt,
) -> Result<(), ObservabilityError> {
    let transaction = connection
        .transaction()
        .map_err(|source| ObservabilityError::Sqlite {
            action: "start attempt observability transaction",
            source,
        })?;
    transaction
        .execute(
            r"
INSERT OR REPLACE INTO attempts (
    attempt_id,
    request_id,
    attempt_number,
    started_at_unix_ms,
    finished_at_unix_ms,
    duration_ms,
    upstream_mode,
    status,
    http_status,
    error_reason,
    retry_reason,
    abort_reason,
    request_metadata_json,
    response_metadata_json,
    raw_input,
    raw_output,
    raw_reasoning,
    raw_tool_calls,
    estimated_bytes
) VALUES (
    ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10,
    ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19
)",
            params![
                record.attempt_id,
                record.request_id,
                record.attempt_number,
                record.started_at_unix_ms,
                record.finished_at_unix_ms,
                record.duration_ms,
                record.upstream_mode,
                record.status,
                record.http_status,
                record.error_reason,
                record.retry_reason,
                record.abort_reason,
                record.request_metadata_json,
                record.response_metadata_json,
                record.raw_payloads.input,
                record.raw_payloads.output,
                record.raw_payloads.reasoning,
                record.raw_payloads.tool_calls,
                record.estimated_bytes,
            ],
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "write attempt observability row",
            source,
        })?;
    transaction
        .commit()
        .map_err(|source| ObservabilityError::Sqlite {
            action: "commit attempt observability transaction",
            source,
        })
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct RetentionPruneOutcome {
    deleted_requests: u64,
    deleted_attempts: u64,
}

impl RetentionPruneOutcome {
    const fn deleted_any(self) -> bool {
        self.deleted_requests > 0 || self.deleted_attempts > 0
    }

    fn add(&mut self, other: Self) {
        self.deleted_requests = self.deleted_requests.saturating_add(other.deleted_requests);
        self.deleted_attempts = self.deleted_attempts.saturating_add(other.deleted_attempts);
    }
}

fn enforce_retention(
    connection: &mut Connection,
    retention: &RetentionConfig,
) -> Result<RetentionPruneOutcome, ObservabilityError> {
    let max_bytes = retention.max_bytes;
    let prune_to_bytes = retention.prune_to_bytes;
    let mut usage = read_retention_usage(connection)?;
    let target_bytes = if usage.observed_bytes > max_bytes {
        prune_to_bytes
    } else {
        max_bytes
    };
    let target_records = if usage.record_count > retention.max_records {
        retention.effective_prune_to_records()
    } else {
        retention.max_records
    };
    let mut outcome = RetentionPruneOutcome::default();

    while usage.observed_bytes > target_bytes || usage.record_count > target_records {
        let batch = prune_retained_rows(connection, target_records, target_bytes)?;
        if !batch.deleted_any() {
            break;
        }
        outcome.add(batch);
        vacuum_database(connection)?;
        usage = read_retention_usage(connection)?;
        if usage.record_count == 0 {
            break;
        }
    }

    Ok(outcome)
}

fn prune_retained_rows(
    connection: &mut Connection,
    max_records: u64,
    target_bytes: u64,
) -> Result<RetentionPruneOutcome, ObservabilityError> {
    let transaction = connection
        .transaction()
        .map_err(|source| ObservabilityError::Sqlite {
            action: "start observability retention transaction",
            source,
        })?;
    let mut outcome = RetentionPruneOutcome::default();
    let mut usage = read_retention_usage(&transaction)?;
    let mut logical_bytes = read_logical_observed_bytes(&transaction)?;

    while usage.record_count > max_records
        || logical_bytes > target_bytes
        || (!outcome.deleted_any() && usage.request_count > 0)
    {
        let Some(request_id) = oldest_request_id(&transaction)? else {
            break;
        };
        let attempt_count = read_attempt_count_for_request(&transaction, &request_id)?;
        transaction
            .execute(
                "DELETE FROM requests WHERE request_id = ?1",
                params![request_id],
            )
            .map_err(|source| ObservabilityError::Sqlite {
                action: "prune oldest observability request",
                source,
            })?;
        outcome.deleted_requests = outcome.deleted_requests.saturating_add(1);
        outcome.deleted_attempts = outcome.deleted_attempts.saturating_add(attempt_count);
        usage = read_retention_usage(&transaction)?;
        logical_bytes = read_logical_observed_bytes(&transaction)?;
    }

    transaction
        .commit()
        .map_err(|source| ObservabilityError::Sqlite {
            action: "commit observability retention transaction",
            source,
        })?;
    Ok(outcome)
}

fn vacuum_database(connection: &Connection) -> Result<(), ObservabilityError> {
    connection
        .execute_batch("VACUUM")
        .map_err(|source| ObservabilityError::Sqlite {
            action: "vacuum SQLite observability store",
            source,
        })
}

fn oldest_request_id(connection: &Connection) -> Result<Option<String>, ObservabilityError> {
    let mut statement = connection
        .prepare(
            r"
SELECT request_id
FROM requests
ORDER BY started_at_unix_ms ASC, request_id ASC
LIMIT 1
",
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "prepare oldest observability request query",
            source,
        })?;
    let mut rows = statement
        .query([])
        .map_err(|source| ObservabilityError::Sqlite {
            action: "query oldest observability request",
            source,
        })?;
    let Some(row) = rows.next().map_err(|source| ObservabilityError::Sqlite {
        action: "read oldest observability request",
        source,
    })?
    else {
        return Ok(None);
    };
    row.get(0)
        .map(Some)
        .map_err(|source| ObservabilityError::Sqlite {
            action: "decode oldest observability request",
            source,
        })
}

fn read_retention_usage(connection: &Connection) -> Result<RetentionUsage, ObservabilityError> {
    let request_count = read_request_count(connection)?;
    let attempt_count = read_attempt_count(connection)?;
    let record_count = request_count.saturating_add(attempt_count);
    let observed_bytes = read_sqlite_storage_bytes(connection)?;

    Ok(RetentionUsage {
        request_count,
        attempt_count,
        record_count,
        observed_bytes,
    })
}

fn read_request_count(connection: &Connection) -> Result<u64, ObservabilityError> {
    let request_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM requests", [], |row| row.get(0))
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read observability request count",
            source,
        })?;
    Ok(nonnegative_i64_to_u64(request_count))
}

fn read_attempt_count(connection: &Connection) -> Result<u64, ObservabilityError> {
    let attempt_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM attempts", [], |row| row.get(0))
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read observability attempt count",
            source,
        })?;
    Ok(nonnegative_i64_to_u64(attempt_count))
}

fn read_attempt_count_for_request(
    connection: &Connection,
    request_id: &str,
) -> Result<u64, ObservabilityError> {
    let attempt_count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM attempts WHERE request_id = ?1",
            params![request_id],
            |row| row.get(0),
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read observability attempt count for pruned request",
            source,
        })?;
    Ok(nonnegative_i64_to_u64(attempt_count))
}

fn record_pruning_outcome(
    connection: &mut Connection,
    outcome: &RetentionPruneOutcome,
) -> Result<(), ObservabilityError> {
    if !outcome.deleted_any() {
        return Ok(());
    }
    let deleted_requests = to_sqlite_i64(outcome.deleted_requests, "pruned_requests")?;
    let deleted_attempts = to_sqlite_i64(outcome.deleted_attempts, "pruned_attempts")?;
    let now = to_sqlite_i64(unix_time_millis(), "last_pruned_at_unix_ms")?;
    connection
        .execute(
            r"
UPDATE retention_pruning_stats
SET prune_events = prune_events + 1,
    pruned_requests = pruned_requests + ?1,
    pruned_attempts = pruned_attempts + ?2,
    last_pruned_at_unix_ms = ?3
WHERE stats_key = 'global'
",
            params![deleted_requests, deleted_attempts, now],
        )
        .map(|_updated| ())
        .map_err(|source| ObservabilityError::Sqlite {
            action: "record observability retention pruning stats",
            source,
        })
}

fn read_metrics_snapshot(
    connection: &Connection,
) -> Result<ObservabilityMetricsSnapshot, ObservabilityError> {
    Ok(ObservabilityMetricsSnapshot {
        request_counts: read_request_metric_counts(connection)?,
        attempt_counts: read_attempt_metric_counts(connection)?,
        retry_count: read_retry_count(connection)?,
        loop_abort_count: read_loop_abort_count(connection)?,
        upstream_error_counts: read_upstream_error_counts(connection)?,
        first_token_latency_ms: read_first_token_latency_histogram(connection)?,
        total_latency_ms: read_total_latency_histogram(connection)?,
        heartbeat_mode_counts: read_heartbeat_mode_counts(connection)?,
        retention_usage: read_retention_usage(connection)?,
        pruning: read_pruning_stats(connection)?,
    })
}

fn read_request_metric_counts(
    connection: &Connection,
) -> Result<Vec<RequestMetricCount>, ObservabilityError> {
    let mut statement = connection
        .prepare(
            r"
SELECT status, downstream_mode, upstream_mode, http_status, COUNT(*)
FROM requests
GROUP BY status, downstream_mode, upstream_mode, http_status
ORDER BY status, downstream_mode, upstream_mode, http_status
",
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "prepare request metric counts query",
            source,
        })?;
    let mut rows = statement
        .query([])
        .map_err(|source| ObservabilityError::Sqlite {
            action: "query request metric counts",
            source,
        })?;
    let mut counts = BTreeMap::<(String, String, String, String), u64>::new();
    while let Some(row) = rows.next().map_err(|source| ObservabilityError::Sqlite {
        action: "read request metric count row",
        source,
    })? {
        let status: String = row.get(0).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request metric status",
            source,
        })?;
        let downstream_mode: String = row.get(1).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request metric downstream mode",
            source,
        })?;
        let upstream_mode: String = row.get(2).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request metric upstream mode",
            source,
        })?;
        let http_status: Option<i64> = row.get(3).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request metric HTTP status",
            source,
        })?;
        let count: i64 = row.get(4).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request metric count",
            source,
        })?;
        let key = (
            status,
            downstream_mode,
            upstream_mode,
            http_status_class(http_status),
        );
        let entry = counts.entry(key).or_default();
        *entry = entry.saturating_add(nonnegative_i64_to_u64(count));
    }
    Ok(counts
        .into_iter()
        .map(
            |((status, downstream_mode, upstream_mode, http_status_class), count)| {
                RequestMetricCount {
                    status,
                    downstream_mode,
                    upstream_mode,
                    http_status_class,
                    count,
                }
            },
        )
        .collect())
}

fn read_attempt_metric_counts(
    connection: &Connection,
) -> Result<Vec<AttemptMetricCount>, ObservabilityError> {
    let mut statement = connection
        .prepare(
            r"
SELECT status, upstream_mode, http_status, COUNT(*)
FROM attempts
GROUP BY status, upstream_mode, http_status
ORDER BY status, upstream_mode, http_status
",
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "prepare attempt metric counts query",
            source,
        })?;
    let mut rows = statement
        .query([])
        .map_err(|source| ObservabilityError::Sqlite {
            action: "query attempt metric counts",
            source,
        })?;
    let mut counts = BTreeMap::<(String, String, String), u64>::new();
    while let Some(row) = rows.next().map_err(|source| ObservabilityError::Sqlite {
        action: "read attempt metric count row",
        source,
    })? {
        let status: String = row.get(0).map_err(|source| ObservabilityError::Sqlite {
            action: "decode attempt metric status",
            source,
        })?;
        let upstream_mode: String = row.get(1).map_err(|source| ObservabilityError::Sqlite {
            action: "decode attempt metric upstream mode",
            source,
        })?;
        let http_status: Option<i64> = row.get(2).map_err(|source| ObservabilityError::Sqlite {
            action: "decode attempt metric HTTP status",
            source,
        })?;
        let count: i64 = row.get(3).map_err(|source| ObservabilityError::Sqlite {
            action: "decode attempt metric count",
            source,
        })?;
        let key = (status, upstream_mode, http_status_class(http_status));
        let entry = counts.entry(key).or_default();
        *entry = entry.saturating_add(nonnegative_i64_to_u64(count));
    }
    Ok(counts
        .into_iter()
        .map(
            |((status, upstream_mode, http_status_class), count)| AttemptMetricCount {
                status,
                upstream_mode,
                http_status_class,
                count,
            },
        )
        .collect())
}

fn read_retry_count(connection: &Connection) -> Result<u64, ObservabilityError> {
    let count: i64 = connection
        .query_row(
            r"
SELECT COUNT(*)
FROM attempts
WHERE status = 'retried' OR retry_reason IS NOT NULL
",
            [],
            |row| row.get(0),
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read retry metric count",
            source,
        })?;
    Ok(nonnegative_i64_to_u64(count))
}

fn read_loop_abort_count(connection: &Connection) -> Result<u64, ObservabilityError> {
    let count: i64 = connection
        .query_row(
            r"
SELECT
    (SELECT COUNT(*) FROM attempts
     WHERE abort_reason = 'loop_guard'
        OR response_metadata_json LIKE '%loop_detected%true%')
    +
    (SELECT COUNT(*) FROM requests
     WHERE abort_reason = 'loop_guard'
        OR response_metadata_json LIKE '%loop_detected%true%')
",
            [],
            |row| row.get(0),
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read loop abort metric count",
            source,
        })?;
    Ok(nonnegative_i64_to_u64(count))
}

fn read_upstream_error_counts(
    connection: &Connection,
) -> Result<Vec<UpstreamErrorMetricCount>, ObservabilityError> {
    let mut statement = connection
        .prepare(
            r"
SELECT status, http_status, COUNT(*)
FROM attempts
WHERE status != 'succeeded' OR http_status >= 500 OR http_status IS NULL
GROUP BY status, http_status
ORDER BY status, http_status
",
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "prepare upstream error metric query",
            source,
        })?;
    let mut rows = statement
        .query([])
        .map_err(|source| ObservabilityError::Sqlite {
            action: "query upstream error metrics",
            source,
        })?;
    let mut counts = BTreeMap::<(String, String), u64>::new();
    while let Some(row) = rows.next().map_err(|source| ObservabilityError::Sqlite {
        action: "read upstream error metric row",
        source,
    })? {
        let status: String = row.get(0).map_err(|source| ObservabilityError::Sqlite {
            action: "decode upstream error attempt status",
            source,
        })?;
        let http_status: Option<i64> = row.get(1).map_err(|source| ObservabilityError::Sqlite {
            action: "decode upstream error HTTP status",
            source,
        })?;
        let count: i64 = row.get(2).map_err(|source| ObservabilityError::Sqlite {
            action: "decode upstream error count",
            source,
        })?;
        let key = (
            upstream_error_kind(&status, http_status).to_owned(),
            http_status_class(http_status),
        );
        let entry = counts.entry(key).or_default();
        *entry = entry.saturating_add(nonnegative_i64_to_u64(count));
    }
    Ok(counts
        .into_iter()
        .map(
            |((kind, http_status_class), count)| UpstreamErrorMetricCount {
                kind,
                http_status_class,
                count,
            },
        )
        .collect())
}

fn read_first_token_latency_histogram(
    connection: &Connection,
) -> Result<LatencyHistogram, ObservabilityError> {
    let mut statement = connection
        .prepare("SELECT response_metadata_json FROM attempts")
        .map_err(|source| ObservabilityError::Sqlite {
            action: "prepare first-token latency query",
            source,
        })?;
    let mut rows = statement
        .query([])
        .map_err(|source| ObservabilityError::Sqlite {
            action: "query first-token latency metadata",
            source,
        })?;
    let mut values = Vec::new();
    while let Some(row) = rows.next().map_err(|source| ObservabilityError::Sqlite {
        action: "read first-token latency metadata row",
        source,
    })? {
        let metadata_json: String = row.get(0).map_err(|source| ObservabilityError::Sqlite {
            action: "decode first-token latency metadata",
            source,
        })?;
        if let Some(latency) = metadata_value_as_u64(&metadata_json, "first_token_latency_ms") {
            values.push(latency);
        }
    }
    Ok(latency_histogram(&values))
}

fn read_total_latency_histogram(
    connection: &Connection,
) -> Result<LatencyHistogram, ObservabilityError> {
    let mut statement = connection
        .prepare("SELECT duration_ms FROM requests WHERE duration_ms IS NOT NULL")
        .map_err(|source| ObservabilityError::Sqlite {
            action: "prepare total latency query",
            source,
        })?;
    let mut rows = statement
        .query([])
        .map_err(|source| ObservabilityError::Sqlite {
            action: "query total latency values",
            source,
        })?;
    let mut values = Vec::new();
    while let Some(row) = rows.next().map_err(|source| ObservabilityError::Sqlite {
        action: "read total latency row",
        source,
    })? {
        let latency: i64 = row.get(0).map_err(|source| ObservabilityError::Sqlite {
            action: "decode total latency value",
            source,
        })?;
        values.push(nonnegative_i64_to_u64(latency));
    }
    Ok(latency_histogram(&values))
}

fn read_heartbeat_mode_counts(
    connection: &Connection,
) -> Result<Vec<HeartbeatModeMetricCount>, ObservabilityError> {
    let mut statement = connection
        .prepare("SELECT request_metadata_json, response_metadata_json FROM requests")
        .map_err(|source| ObservabilityError::Sqlite {
            action: "prepare heartbeat mode metric query",
            source,
        })?;
    let mut rows = statement
        .query([])
        .map_err(|source| ObservabilityError::Sqlite {
            action: "query heartbeat mode metrics",
            source,
        })?;
    let mut counts = BTreeMap::<String, u64>::new();
    while let Some(row) = rows.next().map_err(|source| ObservabilityError::Sqlite {
        action: "read heartbeat mode metric row",
        source,
    })? {
        let request_metadata_json: String =
            row.get(0).map_err(|source| ObservabilityError::Sqlite {
                action: "decode heartbeat request metadata",
                source,
            })?;
        let response_metadata_json: String =
            row.get(1).map_err(|source| ObservabilityError::Sqlite {
                action: "decode heartbeat response metadata",
                source,
            })?;
        let mode = metadata_value(&response_metadata_json, "downstream_liveness_mode")
            .or_else(|| metadata_value(&request_metadata_json, "downstream_liveness_mode"));
        if let Some(mode) = mode {
            let entry = counts.entry(mode).or_default();
            *entry = entry.saturating_add(1);
        }
    }
    Ok(counts
        .into_iter()
        .map(|(mode, count)| HeartbeatModeMetricCount { mode, count })
        .collect())
}

fn read_pruning_stats(
    connection: &Connection,
) -> Result<RetentionPruningStats, ObservabilityError> {
    let (prune_events, pruned_requests, pruned_attempts, last_pruned_at_unix_ms): (
        i64,
        i64,
        i64,
        Option<i64>,
    ) = connection
        .query_row(
            r"
SELECT prune_events, pruned_requests, pruned_attempts, last_pruned_at_unix_ms
FROM retention_pruning_stats
WHERE stats_key = 'global'
",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read retention pruning stats",
            source,
        })?;
    Ok(RetentionPruningStats {
        prune_events: nonnegative_i64_to_u64(prune_events),
        pruned_requests: nonnegative_i64_to_u64(pruned_requests),
        pruned_attempts: nonnegative_i64_to_u64(pruned_attempts),
        last_pruned_at_unix_ms: last_pruned_at_unix_ms.map(nonnegative_i64_to_u64),
    })
}

fn read_recent_request_summaries(
    connection: &Connection,
    limit: u32,
) -> Result<Vec<DebugRequestSummary>, ObservabilityError> {
    let mut statement = connection
        .prepare(
            r"
SELECT
    r.request_id,
    r.started_at_unix_ms,
    r.finished_at_unix_ms,
    r.duration_ms,
    r.downstream_mode,
    r.upstream_mode,
    r.model_id,
    r.status,
    r.http_status,
    r.error_reason,
    r.abort_reason,
    r.request_metadata_json,
    r.response_metadata_json,
    (SELECT COUNT(*) FROM attempts a WHERE a.request_id = r.request_id),
    (SELECT COUNT(*) FROM attempts a
     WHERE a.request_id = r.request_id
       AND (a.status = 'retried' OR a.retry_reason IS NOT NULL)),
    EXISTS(
        SELECT 1 FROM attempts a
        WHERE a.request_id = r.request_id
          AND (a.abort_reason = 'loop_guard'
               OR a.response_metadata_json LIKE '%loop_detected%true%')
    ) OR r.response_metadata_json LIKE '%loop_detected%true%'
FROM requests r
ORDER BY r.started_at_unix_ms DESC, r.request_id DESC
LIMIT ?1
",
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "prepare recent request summary query",
            source,
        })?;
    let mut rows = statement
        .query(params![i64::from(limit)])
        .map_err(|source| ObservabilityError::Sqlite {
            action: "query recent request summaries",
            source,
        })?;
    let mut summaries = Vec::new();
    while let Some(row) = rows.next().map_err(|source| ObservabilityError::Sqlite {
        action: "read recent request summary row",
        source,
    })? {
        summaries.push(decode_recent_request_summary(row)?);
    }
    Ok(summaries)
}

fn decode_recent_request_summary(
    row: &rusqlite::Row<'_>,
) -> Result<DebugRequestSummary, ObservabilityError> {
    let request_metadata_json: String =
        row.get(11).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request summary request metadata",
            source,
        })?;
    let response_metadata_json: String =
        row.get(12).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request summary response metadata",
            source,
        })?;
    Ok(DebugRequestSummary {
        request_id: row.get(0).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request summary id",
            source,
        })?,
        started_at_unix_ms: read_u64_column(row, 1, "decode request summary start time")?,
        finished_at_unix_ms: read_optional_u64_column(
            row,
            2,
            "decode request summary finish time",
        )?,
        duration_ms: read_optional_u64_column(row, 3, "decode request summary duration")?,
        downstream_mode: row.get(4).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request summary downstream mode",
            source,
        })?,
        upstream_mode: row.get(5).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request summary upstream mode",
            source,
        })?,
        model_id: sanitize_optional_text(
            row.get::<_, Option<String>>(6)
                .map_err(|source| ObservabilityError::Sqlite {
                    action: "decode request summary model id",
                    source,
                })?
                .as_ref(),
        ),
        status: row.get(7).map_err(|source| ObservabilityError::Sqlite {
            action: "decode request summary status",
            source,
        })?,
        http_status: read_optional_u16_column(row, 8, "decode request summary HTTP status")?,
        error_reason: sanitize_optional_text(
            row.get::<_, Option<String>>(9)
                .map_err(|source| ObservabilityError::Sqlite {
                    action: "decode request summary error reason",
                    source,
                })?
                .as_ref(),
        ),
        abort_reason: sanitize_optional_text(
            row.get::<_, Option<String>>(10)
                .map_err(|source| ObservabilityError::Sqlite {
                    action: "decode request summary abort reason",
                    source,
                })?
                .as_ref(),
        ),
        attempt_count: read_u64_column(row, 13, "decode request summary attempt count")?,
        retry_count: read_u64_column(row, 14, "decode request summary retry count")?,
        loop_detected: read_bool_column(row, 15, "decode request summary loop flag")?,
        request_metadata: debug_metadata_map_from_json(&request_metadata_json),
        response_metadata: debug_metadata_map_from_json(&response_metadata_json),
    })
}

fn http_status_class(status: Option<i64>) -> String {
    match status.and_then(|status| u16::try_from(status).ok()) {
        Some(100..=199) => String::from("1xx"),
        Some(200..=299) => String::from("2xx"),
        Some(300..=399) => String::from("3xx"),
        Some(400..=499) => String::from("4xx"),
        Some(500..=599) => String::from("5xx"),
        Some(_) => String::from("other"),
        None => String::from("none"),
    }
}

fn upstream_error_kind(status: &str, http_status: Option<i64>) -> &'static str {
    let code = http_status.and_then(|status| u16::try_from(status).ok());
    match code {
        None => "transport",
        Some(500..=599) => "http_5xx",
        Some(408 | 429) => "http_retryable",
        Some(_) if status == "retried" => "retry",
        Some(_) => "attempt_failed",
    }
}

fn latency_histogram(values: &[u64]) -> LatencyHistogram {
    let buckets = HISTOGRAM_BUCKETS_MS
        .iter()
        .map(|le_ms| HistogramBucket {
            le_ms: *le_ms,
            count: values
                .iter()
                .filter(|value| **value <= *le_ms)
                .count()
                .try_into()
                .unwrap_or(u64::MAX),
        })
        .collect();
    LatencyHistogram {
        buckets,
        count: values.len().try_into().unwrap_or(u64::MAX),
        sum_ms: values
            .iter()
            .fold(0_u64, |sum, value| sum.saturating_add(*value)),
    }
}

fn metadata_value_as_u64(metadata_json: &str, key: &str) -> Option<u64> {
    metadata_value(metadata_json, key).and_then(|value| value.parse::<u64>().ok())
}

fn metadata_value(metadata_json: &str, key: &str) -> Option<String> {
    metadata_map_from_json(metadata_json).remove(key)
}

fn debug_metadata_map_from_json(metadata_json: &str) -> BTreeMap<String, String> {
    debug_safe_metadata_map(&metadata_map_from_json(metadata_json))
}

fn metadata_map_from_json(metadata_json: &str) -> BTreeMap<String, String> {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(metadata_json) else {
        return BTreeMap::new();
    };
    let Some(object) = value.as_object() else {
        return BTreeMap::new();
    };
    object
        .iter()
        .filter_map(|(key, value)| value.as_str().map(|value| (key.clone(), value.to_owned())))
        .collect()
}

fn read_u64_column(
    row: &rusqlite::Row<'_>,
    index: usize,
    action: &'static str,
) -> Result<u64, ObservabilityError> {
    let value: i64 = row
        .get(index)
        .map_err(|source| ObservabilityError::Sqlite { action, source })?;
    Ok(nonnegative_i64_to_u64(value))
}

fn read_optional_u64_column(
    row: &rusqlite::Row<'_>,
    index: usize,
    action: &'static str,
) -> Result<Option<u64>, ObservabilityError> {
    let value: Option<i64> = row
        .get(index)
        .map_err(|source| ObservabilityError::Sqlite { action, source })?;
    Ok(value.map(nonnegative_i64_to_u64))
}

fn read_optional_u16_column(
    row: &rusqlite::Row<'_>,
    index: usize,
    action: &'static str,
) -> Result<Option<u16>, ObservabilityError> {
    let value: Option<i64> = row
        .get(index)
        .map_err(|source| ObservabilityError::Sqlite { action, source })?;
    Ok(value.and_then(|value| u16::try_from(value).ok()))
}

fn read_bool_column(
    row: &rusqlite::Row<'_>,
    index: usize,
    action: &'static str,
) -> Result<bool, ObservabilityError> {
    let value: i64 = row
        .get(index)
        .map_err(|source| ObservabilityError::Sqlite { action, source })?;
    Ok(value != 0)
}

fn read_sqlite_storage_bytes(connection: &Connection) -> Result<u64, ObservabilityError> {
    let page_count = read_sqlite_pragma_u64(connection, "page_count")?;
    let page_size = read_sqlite_pragma_u64(connection, "page_size")?;
    Ok(page_count.saturating_mul(page_size))
}

fn read_sqlite_pragma_u64(
    connection: &Connection,
    pragma: &'static str,
) -> Result<u64, ObservabilityError> {
    let sql = format!("PRAGMA {pragma}");
    let value: i64 = connection
        .query_row(&sql, [], |row| row.get(0))
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read SQLite storage usage",
            source,
        })?;
    Ok(nonnegative_i64_to_u64(value))
}

fn read_logical_observed_bytes(connection: &Connection) -> Result<u64, ObservabilityError> {
    let observed_bytes: i64 = connection
        .query_row(
            r"
SELECT
    COALESCE((SELECT SUM(estimated_bytes) FROM requests), 0)
    + COALESCE((SELECT SUM(estimated_bytes) FROM attempts), 0)
",
            [],
            |row| row.get(0),
        )
        .map_err(|source| ObservabilityError::Sqlite {
            action: "read observability logical bytes",
            source,
        })?;
    Ok(nonnegative_i64_to_u64(observed_bytes))
}

fn estimate_request_bytes(
    record: &RequestRecord,
    request_metadata_json: &str,
    response_metadata_json: &str,
    raw_payloads: &RawPayloads,
) -> Result<i64, ObservabilityError> {
    let mut bytes = 0_u64;
    add_len(&mut bytes, record.request_id.as_str());
    add_len(&mut bytes, record.downstream_mode.as_str());
    add_len(&mut bytes, record.upstream_mode.as_str());
    add_optional_len(&mut bytes, record.model_id.as_ref());
    add_optional_len(&mut bytes, record.input_fingerprint.as_ref());
    add_len(&mut bytes, record.status.as_str());
    add_optional_len(&mut bytes, record.error_reason.as_ref());
    add_optional_len(&mut bytes, record.abort_reason.as_ref());
    add_len(&mut bytes, request_metadata_json);
    add_len(&mut bytes, response_metadata_json);
    add_payload_lens(&mut bytes, raw_payloads);
    to_sqlite_i64(bytes, "estimated_bytes")
}

fn estimate_attempt_bytes(
    record: &AttemptRecord,
    request_metadata_json: &str,
    response_metadata_json: &str,
    raw_payloads: &RawPayloads,
) -> Result<i64, ObservabilityError> {
    let mut bytes = 0_u64;
    add_len(&mut bytes, record.attempt_id.as_str());
    add_len(&mut bytes, record.request_id.as_str());
    add_len(&mut bytes, record.upstream_mode.as_str());
    add_len(&mut bytes, record.status.as_str());
    add_optional_len(&mut bytes, record.error_reason.as_ref());
    add_optional_len(&mut bytes, record.retry_reason.as_ref());
    add_optional_len(&mut bytes, record.abort_reason.as_ref());
    add_len(&mut bytes, request_metadata_json);
    add_len(&mut bytes, response_metadata_json);
    add_payload_lens(&mut bytes, raw_payloads);
    to_sqlite_i64(bytes, "estimated_bytes")
}

fn add_len(bytes: &mut u64, value: &str) {
    let len = u64::try_from(value.len()).unwrap_or(u64::MAX);
    *bytes = bytes.saturating_add(len);
}

fn add_optional_len(bytes: &mut u64, value: Option<&String>) {
    if let Some(value) = value {
        add_len(bytes, value);
    }
}

fn add_payload_lens(bytes: &mut u64, raw_payloads: &RawPayloads) {
    add_optional_len(bytes, raw_payloads.input.as_ref());
    add_optional_len(bytes, raw_payloads.output.as_ref());
    add_optional_len(bytes, raw_payloads.reasoning.as_ref());
    add_optional_len(bytes, raw_payloads.tool_calls.as_ref());
}

fn duration_ms(started_at_unix_ms: u64, finished_at_unix_ms: Option<u64>) -> Option<u64> {
    finished_at_unix_ms.and_then(|finished| finished.checked_sub(started_at_unix_ms))
}

fn to_sqlite_i64(value: u64, field: &'static str) -> Result<i64, ObservabilityError> {
    i64::try_from(value).map_err(|_error| ObservabilityError::IntegerOutOfRange { field })
}

fn optional_to_sqlite_i64(
    value: Option<u64>,
    field: &'static str,
) -> Result<Option<i64>, ObservabilityError> {
    value.map(|value| to_sqlite_i64(value, field)).transpose()
}

fn nonnegative_i64_to_u64(value: i64) -> u64 {
    u64::try_from(value.max(0)).unwrap_or(u64::MAX)
}

fn unix_time_millis() -> u64 {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_millis());
    u64::try_from(millis).unwrap_or(u64::MAX)
}

fn resolve_sqlite_path(path: &Path) -> Result<PathBuf, ObservabilityError> {
    let path_text = path.to_string_lossy();
    let Some(rest) = path_text.strip_prefix("~/") else {
        return Ok(path.to_path_buf());
    };
    let Some(home) = env::var_os("HOME") else {
        return Err(ObservabilityError::HomeDirectoryUnavailable);
    };
    Ok(PathBuf::from(home).join(rest))
}

fn prepare_parent_directory(path: &Path) -> Result<(), ObservabilityError> {
    if path == Path::new(":memory:") {
        return Ok(());
    }
    let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    else {
        return Ok(());
    };
    if let Some(metadata) = inspect_path(parent)? {
        validate_existing_parent_ancestors(parent)?;
        validate_existing_storage_directory(parent, &metadata)
    } else {
        validate_existing_ancestor_chain(parent)?;
        create_private_directory_all(parent)?;
        validate_storage_directory(parent)
    }
}

fn prepare_sqlite_file(path: &Path) -> Result<(), ObservabilityError> {
    if path == Path::new(":memory:") {
        return Ok(());
    }
    validate_sqlite_file_path(path)?;
    create_secure_sqlite_file(path)
}

fn open_sqlite_connection(path: &Path) -> Result<Connection, ObservabilityError> {
    Connection::open_with_flags(path, OpenFlags::default() | OpenFlags::SQLITE_OPEN_NOFOLLOW)
        .map_err(|source| ObservabilityError::Sqlite {
            action: "open SQLite observability store",
            source,
        })
}

fn inspect_path(path: &Path) -> Result<Option<fs::Metadata>, ObservabilityError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => Ok(Some(metadata)),
        Err(source) if source.kind() == ErrorKind::NotFound => Ok(None),
        Err(source) => Err(ObservabilityError::InspectPath {
            path: path.to_path_buf(),
            source,
        }),
    }
}

fn validate_storage_directory(path: &Path) -> Result<(), ObservabilityError> {
    let Some(metadata) = inspect_path(path)? else {
        return Err(unsafe_storage_path(
            path,
            "directory disappeared while preparing observability storage",
        ));
    };
    validate_existing_storage_directory(path, &metadata)
}

fn validate_existing_storage_directory(
    path: &Path,
    metadata: &fs::Metadata,
) -> Result<(), ObservabilityError> {
    validate_directory_shape(path, metadata)?;
    validate_existing_storage_directory_permissions(path, metadata)
}

fn validate_existing_ancestor_chain(path: &Path) -> Result<(), ObservabilityError> {
    for ancestor in path
        .ancestors()
        .filter(|ancestor| !ancestor.as_os_str().is_empty())
    {
        if let Some(metadata) = inspect_path(ancestor)? {
            validate_existing_ancestor_directory(ancestor, &metadata)?;
        }
    }
    Ok(())
}

fn validate_existing_parent_ancestors(path: &Path) -> Result<(), ObservabilityError> {
    for ancestor in path
        .ancestors()
        .skip(1)
        .filter(|ancestor| !ancestor.as_os_str().is_empty())
    {
        if let Some(metadata) = inspect_path(ancestor)? {
            validate_existing_ancestor_directory(ancestor, &metadata)?;
        }
    }
    Ok(())
}

fn validate_existing_ancestor_directory(
    path: &Path,
    metadata: &fs::Metadata,
) -> Result<(), ObservabilityError> {
    validate_directory_shape(path, metadata)?;
    validate_existing_ancestor_directory_permissions(path, metadata)
}

fn validate_directory_shape(
    path: &Path,
    metadata: &fs::Metadata,
) -> Result<(), ObservabilityError> {
    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        return Err(unsafe_storage_path(
            path,
            "observability directory must not be a symlink",
        ));
    }
    if !file_type.is_dir() {
        return Err(unsafe_storage_path(
            path,
            "observability directory path must be a directory",
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn validate_existing_storage_directory_permissions(
    path: &Path,
    metadata: &fs::Metadata,
) -> Result<(), ObservabilityError> {
    let mode = metadata.permissions().mode() & 0o777;
    if mode & 0o077 != 0 {
        return Err(unsafe_storage_path(
            path,
            "existing observability directory must not grant group or other permissions",
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn validate_existing_storage_directory_permissions(
    _path: &Path,
    _metadata: &fs::Metadata,
) -> Result<(), ObservabilityError> {
    Ok(())
}

#[cfg(unix)]
fn validate_existing_ancestor_directory_permissions(
    path: &Path,
    metadata: &fs::Metadata,
) -> Result<(), ObservabilityError> {
    let mode = metadata.permissions().mode();
    let shared_writable = mode & 0o022 != 0;
    let sticky = mode & 0o1000 != 0;
    if shared_writable && !sticky {
        return Err(unsafe_storage_path(
            path,
            "existing observability ancestor must not be group/other-writable unless sticky",
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn validate_existing_ancestor_directory_permissions(
    _path: &Path,
    _metadata: &fs::Metadata,
) -> Result<(), ObservabilityError> {
    Ok(())
}

fn create_private_directory_all(path: &Path) -> Result<(), ObservabilityError> {
    if let Some(metadata) = inspect_path(path)? {
        return validate_existing_ancestor_directory(path, &metadata);
    }
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        create_private_directory_all(parent)?;
    }
    create_private_directory(path)?;
    restrict_directory_permissions(path)
}

#[cfg(unix)]
fn create_private_directory(path: &Path) -> Result<(), ObservabilityError> {
    let mut builder = fs::DirBuilder::new();
    builder.mode(OBSERVABILITY_DIRECTORY_MODE);
    match builder.create(path) {
        Ok(()) => Ok(()),
        Err(source) if source.kind() == ErrorKind::AlreadyExists => {
            validate_storage_directory(path)
        }
        Err(source) => Err(ObservabilityError::CreateDirectory {
            path: path.to_path_buf(),
            source,
        }),
    }
}

#[cfg(not(unix))]
fn create_private_directory(path: &Path) -> Result<(), ObservabilityError> {
    match fs::create_dir(path) {
        Ok(()) => Ok(()),
        Err(source) if source.kind() == ErrorKind::AlreadyExists => {
            validate_storage_directory(path)
        }
        Err(source) => Err(ObservabilityError::CreateDirectory {
            path: path.to_path_buf(),
            source,
        }),
    }
}

fn validate_sqlite_file_path(path: &Path) -> Result<(), ObservabilityError> {
    let Some(metadata) = inspect_path(path)? else {
        return Ok(());
    };
    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        return Err(unsafe_storage_path(
            path,
            "observability SQLite file must not be a symlink",
        ));
    }
    if !file_type.is_file() {
        return Err(unsafe_storage_path(
            path,
            "observability SQLite path must be a regular file",
        ));
    }
    Ok(())
}

fn unsafe_storage_path(path: &Path, reason: &'static str) -> ObservabilityError {
    ObservabilityError::UnsafeStoragePath {
        path: path.to_path_buf(),
        reason,
    }
}

#[cfg(unix)]
fn create_secure_sqlite_file(path: &Path) -> Result<(), ObservabilityError> {
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(OBSERVABILITY_SQLITE_MODE)
        .open(path)
        .map_err(|source| ObservabilityError::RestrictPermissions {
            path: path.to_path_buf(),
            source,
        })?;
    file.set_permissions(fs::Permissions::from_mode(OBSERVABILITY_SQLITE_MODE))
        .map_err(|source| ObservabilityError::RestrictPermissions {
            path: path.to_path_buf(),
            source,
        })
}

#[cfg(not(unix))]
fn create_secure_sqlite_file(path: &Path) -> Result<(), ObservabilityError> {
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .open(path)
        .map(|_file| ())
        .map_err(|source| ObservabilityError::RestrictPermissions {
            path: path.to_path_buf(),
            source,
        })
}

#[cfg(unix)]
fn restrict_directory_permissions(path: &Path) -> Result<(), ObservabilityError> {
    fs::set_permissions(
        path,
        fs::Permissions::from_mode(OBSERVABILITY_DIRECTORY_MODE),
    )
    .map_err(|source| ObservabilityError::RestrictPermissions {
        path: path.to_path_buf(),
        source,
    })
}

#[cfg(not(unix))]
fn restrict_directory_permissions(_path: &Path) -> Result<(), ObservabilityError> {
    Ok(())
}
