use std::{
    error::Error,
    fmt::{self, Display, Formatter},
    io,
    path::PathBuf,
};

/// Validation failure with the field name and clear requirement.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValidationError {
    field: &'static str,
    message: String,
}

impl ValidationError {
    pub(crate) fn new(field: &'static str, message: impl Into<String>) -> Self {
        Self {
            field,
            message: message.into(),
        }
    }

    /// Returns the invalid config field.
    #[must_use]
    pub const fn field(&self) -> &'static str {
        self.field
    }

    /// Returns the validation requirement.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl Display for ValidationError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.field, self.message)
    }
}

impl Error for ValidationError {}

/// TOML parsing failure for the supported config subset.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConfigParseError {
    line: usize,
    message: String,
}

impl ConfigParseError {
    pub(crate) fn new(line: usize, message: impl Into<String>) -> Self {
        Self {
            line,
            message: message.into(),
        }
    }

    /// Returns the one-based input line number.
    #[must_use]
    pub const fn line(&self) -> usize {
        self.line
    }

    /// Returns the parse failure reason.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl Display for ConfigParseError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(formatter, "line {}: {}", self.line, self.message)
    }
}

impl Error for ConfigParseError {}

/// Configuration loading and reload failures.
#[derive(Debug)]
pub enum ConfigError {
    /// The default path could not be resolved.
    HomeDirectoryUnavailable,
    /// Reading the config file failed.
    Read { path: PathBuf, source: io::Error },
    /// Parsing TOML failed.
    Parse {
        path: PathBuf,
        source: ConfigParseError,
    },
    /// Parsed config failed validation.
    Invalid {
        path: PathBuf,
        source: ValidationError,
    },
    /// Shared config state was poisoned by a panic.
    LockPoisoned,
    /// The hot reload poll interval was zero.
    EmptyReloadInterval,
    /// The hot reload thread could not start.
    WatcherStart { path: PathBuf, source: io::Error },
}

impl Display for ConfigError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::HomeDirectoryUnavailable => {
                write!(
                    formatter,
                    "could not determine home directory for default config path"
                )
            }
            Self::Read { path, source } => {
                let path = path.display();
                write!(formatter, "failed to read config {path}: {source}")
            }
            Self::Parse { path, source } => {
                let path = path.display();
                write!(formatter, "failed to parse config {path}: {source}")
            }
            Self::Invalid { path, source } => {
                let path = path.display();
                write!(formatter, "invalid config {path}: {source}")
            }
            Self::LockPoisoned => write!(formatter, "config state lock is poisoned"),
            Self::EmptyReloadInterval => {
                write!(formatter, "reload poll interval must be greater than zero")
            }
            Self::WatcherStart { path, source } => {
                let path = path.display();
                write!(
                    formatter,
                    "failed to start config reload watcher for {path}: {source}"
                )
            }
        }
    }
}

impl Error for ConfigError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Read { source, .. } | Self::WatcherStart { source, .. } => Some(source),
            Self::Parse { source, .. } => Some(source),
            Self::Invalid { source, .. } => Some(source),
            Self::HomeDirectoryUnavailable | Self::LockPoisoned | Self::EmptyReloadInterval => None,
        }
    }
}
