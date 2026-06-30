use std::{
    fmt::{self, Display, Formatter},
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

use super::error::ObservabilityError;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

/// Stable identifier for one downstream request observed by the proxy.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct RequestId(String);

impl RequestId {
    /// Generates a process-local request id suitable for logs and responses.
    #[must_use]
    pub fn generate() -> Self {
        let sequence = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let millis = unix_time_millis();
        Self(format!("req-{millis}-{sequence}"))
    }

    /// Builds a request id from an existing non-empty value.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError::EmptyIdentifier`] when the value is empty.
    pub fn from_string(value: impl Into<String>) -> Result<Self, ObservabilityError> {
        let value = value.into();
        if value.trim().is_empty() {
            return Err(ObservabilityError::EmptyIdentifier { kind: "request" });
        }
        Ok(Self(value))
    }

    /// Returns the request id as a string slice.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Display for RequestId {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Stable identifier for one upstream attempt associated with a request.
#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct AttemptId(String);

impl AttemptId {
    /// Builds an attempt id from an existing non-empty value.
    ///
    /// # Errors
    ///
    /// Returns [`ObservabilityError::EmptyIdentifier`] when the value is empty.
    pub fn from_string(value: impl Into<String>) -> Result<Self, ObservabilityError> {
        let value = value.into();
        if value.trim().is_empty() {
            return Err(ObservabilityError::EmptyIdentifier { kind: "attempt" });
        }
        Ok(Self(value))
    }

    /// Derives an attempt id from the request id and one-based attempt number.
    #[must_use]
    pub fn for_request(request_id: &RequestId, attempt_number: u32) -> Self {
        Self(format!("{}-attempt-{attempt_number}", request_id.as_str()))
    }

    /// Returns the attempt id as a string slice.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Display for AttemptId {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

fn unix_time_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_millis())
}
