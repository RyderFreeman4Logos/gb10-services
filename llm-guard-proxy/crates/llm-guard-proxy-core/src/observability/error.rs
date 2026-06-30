use std::path::PathBuf;

use thiserror::Error;

use crate::ConfigError;

/// Observability storage failures.
#[derive(Debug, Error)]
pub enum ObservabilityError {
    /// Current config could not be read.
    #[error("failed to read observability config: {0}")]
    Config(#[from] ConfigError),
    /// HOME is required to expand a `~/` `SQLite` path.
    #[error("could not determine home directory for observability SQLite path")]
    HomeDirectoryUnavailable,
    /// Creating the `SQLite` parent directory failed.
    #[error("failed to create observability directory {path}: {source}")]
    CreateDirectory {
        /// Directory path that could not be created.
        path: PathBuf,
        /// Source I/O error.
        source: std::io::Error,
    },
    /// Inspecting an observability storage path failed.
    #[error("failed to inspect observability storage path {path}: {source}")]
    InspectPath {
        /// Path whose metadata could not be inspected.
        path: PathBuf,
        /// Source I/O error.
        source: std::io::Error,
    },
    /// The configured observability storage path is unsafe for sensitive data.
    #[error("unsafe observability storage path {path}: {reason}")]
    UnsafeStoragePath {
        /// Unsafe configured path.
        path: PathBuf,
        /// Static reason safe to show in config errors.
        reason: &'static str,
    },
    /// Restricting observability storage permissions failed.
    #[error("failed to restrict observability storage permissions for {path}: {source}")]
    RestrictPermissions {
        /// Path whose permissions could not be restricted.
        path: PathBuf,
        /// Source I/O error.
        source: std::io::Error,
    },
    /// `SQLite` operation failed.
    #[error("failed to {action}: {source}")]
    Sqlite {
        /// Operation being performed.
        action: &'static str,
        /// Source `SQLite` error.
        source: rusqlite::Error,
    },
    /// A future schema version was found.
    #[error("unsupported observability schema version {version}; supported version is {supported}")]
    UnsupportedSchemaVersion {
        /// Version read from `SQLite`.
        version: i64,
        /// Highest version supported by this binary.
        supported: i64,
    },
    /// Metadata serialization failed.
    #[error("failed to serialize {field} metadata: {source}")]
    SerializeMetadata {
        /// Metadata field being serialized.
        field: &'static str,
        /// Source JSON error.
        source: serde_json::Error,
    },
    /// Shared `SQLite` connection state was poisoned by a panic.
    #[error("observability store lock is poisoned")]
    LockPoisoned,
    /// Caller supplied an empty typed identifier.
    #[error("observability {kind} id must not be empty")]
    EmptyIdentifier {
        /// Identifier kind.
        kind: &'static str,
    },
    /// A numeric value did not fit `SQLite`'s signed integer range.
    #[error("{field} does not fit SQLite integer range")]
    IntegerOutOfRange {
        /// Field name.
        field: &'static str,
    },
}
