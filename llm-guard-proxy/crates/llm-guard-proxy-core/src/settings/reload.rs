use std::{
    env, fs, io,
    path::{Path, PathBuf},
    sync::{Arc, RwLock, mpsc},
    thread::{self, JoinHandle},
    time::Duration,
};

use super::{
    AppConfig, ConfigError, DEFAULT_CONFIG_RELATIVE_PATH, RestartRequiredChange,
    parse::parse_config_text,
};

/// Missing-file behavior for a config source.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MissingConfigPolicy {
    /// Missing config files are treated as the built-in defaults.
    UseDefaults,
    /// Missing config files are errors.
    RequireFile,
}

/// Thread-safe handle used by request-serving code to read current settings.
#[derive(Clone, Debug)]
pub struct ConfigHandle {
    current: Arc<RwLock<AppConfig>>,
}

impl ConfigHandle {
    /// Returns a point-in-time copy of the validated config.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::LockPoisoned`] if another thread panicked while
    /// mutating the config.
    pub fn snapshot(&self) -> Result<AppConfig, ConfigError> {
        let guard = self
            .current
            .read()
            .map_err(|_error| ConfigError::LockPoisoned)?;
        Ok(guard.clone())
    }
}

/// Config loader and hot reload manager.
#[derive(Clone, Debug)]
pub struct ConfigManager {
    path: PathBuf,
    missing_policy: MissingConfigPolicy,
    current: Arc<RwLock<AppConfig>>,
    last_error: Arc<RwLock<Option<String>>>,
}

impl ConfigManager {
    /// Loads the default config path, using built-in defaults when it is absent.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError`] when the default path cannot be resolved, or when
    /// an existing config file cannot be read, parsed, or validated.
    pub fn from_default_path() -> Result<Self, ConfigError> {
        let path = default_config_path()?;
        Self::from_path_with_policy(path, MissingConfigPolicy::UseDefaults)
    }

    /// Loads an explicit config file path.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError`] when the file is absent, unreadable, invalid
    /// TOML, or fails validation.
    pub fn from_explicit_path(path: impl Into<PathBuf>) -> Result<Self, ConfigError> {
        Self::from_path_with_policy(path, MissingConfigPolicy::RequireFile)
    }

    /// Loads a config file path with explicit missing-file behavior.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError`] when reading, parsing, or validation fails.
    pub fn from_path_with_policy(
        path: impl Into<PathBuf>,
        missing_policy: MissingConfigPolicy,
    ) -> Result<Self, ConfigError> {
        let path = path.into();
        let config = load_config(&path, missing_policy)?;
        Ok(Self {
            path,
            missing_policy,
            current: Arc::new(RwLock::new(config)),
            last_error: Arc::new(RwLock::new(None)),
        })
    }

    /// Returns the source path used by this manager.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Returns a cloneable handle for request-serving code.
    #[must_use]
    pub fn handle(&self) -> ConfigHandle {
        ConfigHandle {
            current: Arc::clone(&self.current),
        }
    }

    /// Returns the most recent background reload error, if any.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::LockPoisoned`] if the diagnostic lock is poisoned.
    pub fn last_error(&self) -> Result<Option<String>, ConfigError> {
        let guard = self
            .last_error
            .read()
            .map_err(|_error| ConfigError::LockPoisoned)?;
        Ok(guard.clone())
    }

    /// Reloads the source file and applies reloadable settings.
    ///
    /// Restart-required setting changes are reported in the returned outcome
    /// but are not applied to the live config.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError`] when reading, parsing, validation, or config
    /// state mutation fails.
    pub fn reload(&self) -> Result<ReloadOutcome, ConfigError> {
        let requested = load_config(&self.path, self.missing_policy).inspect_err(|error| {
            self.set_last_error(Some(error.to_string()));
        })?;

        let mut current = self
            .current
            .write()
            .map_err(|_error| ConfigError::LockPoisoned)?;
        let before = current.clone();
        let restart_required_changes = before.restart_required_changes(&requested);
        current.apply_reloadable_from(&requested);
        let applied = before != *current;
        self.set_last_error(None);

        Ok(ReloadOutcome {
            applied,
            restart_required_changes,
        })
    }

    /// Starts a background polling watcher that reloads this manager.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::EmptyReloadInterval`] when `interval` is zero, or
    /// [`ConfigError::WatcherStart`] if the OS cannot start the thread.
    pub fn spawn_polling(&self, interval: Duration) -> Result<ReloadWatcher, ConfigError> {
        if interval.is_zero() {
            return Err(ConfigError::EmptyReloadInterval);
        }

        let manager = self.clone();
        let path = self.path.clone();
        let (stop_tx, stop_rx) = mpsc::channel();
        let thread = thread::Builder::new()
            .name(String::from("llm-guard-proxy-config-reload"))
            .spawn(move || poll_reloads(&manager, &stop_rx, interval))
            .map_err(|source| ConfigError::WatcherStart { path, source })?;

        Ok(ReloadWatcher {
            stop_tx: Some(stop_tx),
            thread: Some(thread),
        })
    }

    fn set_last_error(&self, value: Option<String>) {
        if let Ok(mut guard) = self.last_error.write() {
            *guard = value;
        }
    }
}

/// Result of a single hot reload attempt.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReloadOutcome {
    /// True when at least one live reloadable setting changed.
    pub applied: bool,
    /// Restart-required changes detected but not applied.
    pub restart_required_changes: Vec<RestartRequiredChange>,
}

/// Background reload watcher.
#[derive(Debug)]
pub struct ReloadWatcher {
    stop_tx: Option<mpsc::Sender<()>>,
    thread: Option<JoinHandle<()>>,
}

impl ReloadWatcher {
    /// Stops the watcher and waits for the background thread to exit.
    ///
    /// # Errors
    ///
    /// Returns the panic payload if the watcher thread panicked.
    pub fn stop(mut self) -> thread::Result<()> {
        self.request_stop();
        self.join_thread()
    }

    fn request_stop(&mut self) {
        if let Some(stop_tx) = self.stop_tx.take() {
            let _sent = stop_tx.send(());
        }
    }

    fn join_thread(&mut self) -> thread::Result<()> {
        match self.thread.take() {
            Some(thread) => thread.join(),
            None => Ok(()),
        }
    }
}

impl Drop for ReloadWatcher {
    fn drop(&mut self) {
        self.request_stop();
        let _ = self.join_thread();
    }
}

/// Returns the default config path.
///
/// # Errors
///
/// Returns [`ConfigError::HomeDirectoryUnavailable`] when `HOME` is absent.
pub fn default_config_path() -> Result<PathBuf, ConfigError> {
    let Some(home) = env::var_os("HOME") else {
        return Err(ConfigError::HomeDirectoryUnavailable);
    };
    Ok(PathBuf::from(home).join(DEFAULT_CONFIG_RELATIVE_PATH))
}

fn load_config(path: &Path, missing_policy: MissingConfigPolicy) -> Result<AppConfig, ConfigError> {
    let contents = match fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(source)
            if source.kind() == io::ErrorKind::NotFound
                && missing_policy == MissingConfigPolicy::UseDefaults =>
        {
            let config = AppConfig::default();
            config.validate().map_err(|source| ConfigError::Invalid {
                path: path.to_path_buf(),
                source,
            })?;
            return Ok(config);
        }
        Err(source) => {
            return Err(ConfigError::Read {
                path: path.to_path_buf(),
                source,
            });
        }
    };

    let config = parse_config_text(&contents).map_err(|source| ConfigError::Parse {
        path: path.to_path_buf(),
        source,
    })?;
    config.validate().map_err(|source| ConfigError::Invalid {
        path: path.to_path_buf(),
        source,
    })?;
    Ok(config)
}

fn poll_reloads(manager: &ConfigManager, stop_rx: &mpsc::Receiver<()>, interval: Duration) {
    loop {
        match stop_rx.recv_timeout(interval) {
            Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if let Err(error) = manager.reload() {
                    manager.set_last_error(Some(error.to_string()));
                }
            }
        }
    }
}
