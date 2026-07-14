//! Transactional configuration snapshots for the GB10 memory guardian.
//!
//! Filesystem notifications only produce candidates. The production loop must
//! validate and arm the candidate registration before explicitly committing it.

use gb10_memory_guardian_core::{CgroupTarget, GuardianError, RefreshStatus, RegistrationManager};
use notify::{Event, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Deserialize;
use std::env;
use std::error::Error;
use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Component, Path, PathBuf};
use std::sync::mpsc::{self, Receiver, TryRecvError};

const CONFIG_SCHEMA_VERSION: u32 = 1;
const RUNTIME_SUBDIRECTORY: &str = "gb10-memory-guardian";
const MAX_LABEL_BYTES: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TargetSnapshot {
    label: String,
    registration_path: PathBuf,
    generation: FileGeneration,
}

impl TargetSnapshot {
    pub fn label(&self) -> &str {
        &self.label
    }

    pub fn registration_path(&self) -> &Path {
        &self.registration_path
    }
}

#[derive(Debug)]
pub enum ConfigError {
    Read {
        path: PathBuf,
        source: std::io::Error,
    },
    Parse(toml::de::Error),
    Invalid(String),
    Watch(notify::Error),
    WatchEvent(notify::Error),
    MissingParent(PathBuf),
    CurrentDirectory(std::io::Error),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read { path, source } => {
                write!(formatter, "read config {}: {source}", path.display())
            }
            Self::Parse(error) => write!(formatter, "parse config: {error}"),
            Self::Invalid(message) => write!(formatter, "invalid config: {message}"),
            Self::Watch(error) => write!(formatter, "watch config: {error}"),
            Self::WatchEvent(error) => write!(formatter, "config watch event: {error}"),
            Self::MissingParent(path) => {
                write!(formatter, "config path has no parent: {}", path.display())
            }
            Self::CurrentDirectory(error) => write!(formatter, "read current directory: {error}"),
        }
    }
}

impl Error for ConfigError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Read { source, .. } | Self::CurrentDirectory(source) => Some(source),
            Self::Parse(source) => Some(source),
            Self::Watch(source) | Self::WatchEvent(source) => Some(source),
            Self::Invalid(_) | Self::MissingParent(_) => None,
        }
    }
}

#[derive(Debug)]
pub enum TargetRegistrationError {
    Config(ConfigError),
    Registration(GuardianError),
}

impl fmt::Display for TargetRegistrationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Config(error) => write!(formatter, "target config: {error}"),
            Self::Registration(error) => write!(formatter, "target registration: {error}"),
        }
    }
}

impl Error for TargetRegistrationError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Config(error) => Some(error),
            Self::Registration(error) => Some(error),
        }
    }
}

impl From<ConfigError> for TargetRegistrationError {
    fn from(error: ConfigError) -> Self {
        Self::Config(error)
    }
}

impl From<GuardianError> for TargetRegistrationError {
    fn from(error: GuardianError) -> Self {
        Self::Registration(error)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetTransition {
    Unchanged,
    Armed,
    Refreshed,
    Swapped,
    Superseded,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileConfig {
    schema_version: u32,
    target: FileTarget,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileTarget {
    label: String,
    registration_file: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FileGeneration {
    device: u64,
    inode: u64,
    size: u64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
}

impl FileGeneration {
    fn from_file(file: &File) -> Result<Self, std::io::Error> {
        let metadata = file.metadata()?;
        if !metadata.file_type().is_file() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "config must be a regular file",
            ));
        }
        Ok(Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            size: metadata.size(),
            modified_seconds: metadata.mtime(),
            modified_nanoseconds: metadata.mtime_nsec(),
        })
    }
}

/// Watches the config's parent directory so atomic rename replacements are seen.
pub struct TargetConfigMonitor {
    config_path: PathBuf,
    runtime_dir: PathBuf,
    active: TargetSnapshot,
    pending: Option<TargetSnapshot>,
    receiver: Receiver<notify::Result<Event>>,
    _watcher: RecommendedWatcher,
}

impl fmt::Debug for TargetConfigMonitor {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("TargetConfigMonitor")
            .field("config_path", &self.config_path)
            .field("runtime_dir", &self.runtime_dir)
            .field("active", &self.active)
            .field("pending", &self.pending)
            .finish_non_exhaustive()
    }
}

impl TargetConfigMonitor {
    pub fn new(config_path: &Path, runtime_dir: &Path) -> Result<Self, ConfigError> {
        let config_path = absolute_path(config_path)?;
        let runtime_dir = absolute_path(runtime_dir)?;
        let parent = config_path
            .parent()
            .ok_or_else(|| ConfigError::MissingParent(config_path.clone()))?;
        let (sender, receiver) = mpsc::channel();
        let mut watcher = notify::recommended_watcher(move |event| {
            let _ = sender.send(event);
        })
        .map_err(ConfigError::Watch)?;
        watcher
            .watch(parent, RecursiveMode::NonRecursive)
            .map_err(ConfigError::Watch)?;
        let active = load_snapshot(&config_path, &runtime_dir)?;
        Ok(Self {
            config_path,
            runtime_dir,
            active,
            pending: None,
            receiver,
            _watcher: watcher,
        })
    }

    pub fn active(&self) -> &TargetSnapshot {
        &self.active
    }

    /// Return the newest parsed candidate without changing the active snapshot.
    pub fn pending_snapshot(&mut self) -> Result<Option<TargetSnapshot>, ConfigError> {
        self.reload_pending()?;
        Ok(self.pending.clone())
    }

    /// Commit only if no newer filesystem generation superseded the candidate.
    pub fn try_commit(&mut self, candidate: TargetSnapshot) -> Result<bool, ConfigError> {
        self.reload_pending()?;
        if self.pending.as_ref() != Some(&candidate)
            || current_generation(&self.config_path)? != candidate.generation
        {
            return Ok(false);
        }
        self.active = candidate;
        self.pending = None;
        Ok(true)
    }

    fn reload_pending(&mut self) -> Result<(), ConfigError> {
        let mut relevant_change = false;
        loop {
            match self.receiver.try_recv() {
                Ok(Ok(event)) => {
                    if event_is_relevant(&event, &self.config_path) {
                        relevant_change = true;
                    }
                }
                Ok(Err(error)) => {
                    self.pending = None;
                    return Err(ConfigError::WatchEvent(error));
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    self.pending = None;
                    return Err(ConfigError::Invalid(
                        "config notification channel disconnected".to_owned(),
                    ));
                }
            }
        }

        if relevant_change {
            match load_snapshot(&self.config_path, &self.runtime_dir) {
                Ok(snapshot) => self.pending = Some(snapshot),
                Err(error) => {
                    self.pending = None;
                    return Err(error);
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug)]
struct PendingRegistration {
    snapshot: TargetSnapshot,
    registration: RegistrationManager,
}

/// Owns the last-good armed registration and validates hot-reload candidates
/// in isolation before replacing it.
#[derive(Debug)]
pub struct TargetRegistrationSet {
    monitor: TargetConfigMonitor,
    active: RegistrationManager,
    pending: Option<PendingRegistration>,
    cgroup_root: PathBuf,
    uid: u32,
}

impl TargetRegistrationSet {
    pub fn new(
        config_path: &Path,
        runtime_dir: &Path,
        cgroup_root: &Path,
        uid: u32,
    ) -> Result<Self, ConfigError> {
        let monitor = TargetConfigMonitor::new(config_path, runtime_dir)?;
        let active =
            RegistrationManager::new(monitor.active().registration_path(), cgroup_root, uid);
        Ok(Self {
            monitor,
            active,
            pending: None,
            cgroup_root: cgroup_root.to_owned(),
            uid,
        })
    }

    pub fn active_label(&self) -> &str {
        self.monitor.active().label()
    }

    pub fn pending_label(&self) -> Option<&str> {
        self.pending
            .as_ref()
            .map(|pending| pending.snapshot.label())
    }

    pub fn target(&self) -> Option<&CgroupTarget> {
        self.active.target()
    }

    pub fn disarm(&mut self) {
        self.active.clear();
    }

    /// Reconcile configuration and registration state while memory is healthy.
    pub fn reconcile(&mut self) -> Result<TargetTransition, TargetRegistrationError> {
        let candidate = match self.monitor.pending_snapshot() {
            Ok(candidate) => candidate,
            Err(error) => {
                self.pending = None;
                return Err(error.into());
            }
        };
        if let Some(candidate) = candidate {
            let replace_pending = self
                .pending
                .as_ref()
                .is_none_or(|pending| pending.snapshot != candidate);
            if replace_pending {
                self.pending = Some(PendingRegistration {
                    registration: RegistrationManager::new(
                        candidate.registration_path(),
                        &self.cgroup_root,
                        self.uid,
                    ),
                    snapshot: candidate,
                });
            }

            let pending = self.pending.as_mut().ok_or_else(|| {
                ConfigError::Invalid("candidate registration state is missing".to_owned())
            })?;
            pending.registration.refresh()?;
            if pending.registration.target().is_some() {
                let snapshot = pending.snapshot.clone();
                let committed = match self.monitor.try_commit(snapshot) {
                    Ok(committed) => committed,
                    Err(error) => {
                        self.pending = None;
                        return Err(error.into());
                    }
                };
                if !committed {
                    self.pending = None;
                    return Ok(TargetTransition::Superseded);
                }
                let pending = self.pending.take().ok_or_else(|| {
                    ConfigError::Invalid("committed registration state is missing".to_owned())
                })?;
                self.active = pending.registration;
                return Ok(TargetTransition::Swapped);
            }
        }

        let was_armed = self.active.target().is_some();
        let refresh = self.active.refresh()?;
        if matches!(refresh, RefreshStatus::Replaced) {
            if was_armed {
                Ok(TargetTransition::Refreshed)
            } else {
                Ok(TargetTransition::Armed)
            }
        } else {
            Ok(TargetTransition::Unchanged)
        }
    }
}

fn absolute_path(path: &Path) -> Result<PathBuf, ConfigError> {
    if path.is_absolute() {
        return Ok(path.to_owned());
    }
    env::current_dir()
        .map(|directory| directory.join(path))
        .map_err(ConfigError::CurrentDirectory)
}

fn load_snapshot(config_path: &Path, runtime_dir: &Path) -> Result<TargetSnapshot, ConfigError> {
    let mut config_file = open_config(config_path)?;
    let generation =
        FileGeneration::from_file(&config_file).map_err(|source| ConfigError::Read {
            path: config_path.to_owned(),
            source,
        })?;
    let mut contents = String::new();
    config_file
        .read_to_string(&mut contents)
        .map_err(|source| ConfigError::Read {
            path: config_path.to_owned(),
            source,
        })?;
    if FileGeneration::from_file(&config_file).map_err(|source| ConfigError::Read {
        path: config_path.to_owned(),
        source,
    })? != generation
    {
        return Err(ConfigError::Invalid(
            "config changed while its snapshot was being captured".to_owned(),
        ));
    }
    let file: FileConfig = toml::from_str(&contents).map_err(ConfigError::Parse)?;
    if file.schema_version != CONFIG_SCHEMA_VERSION {
        return Err(ConfigError::Invalid(format!(
            "schema_version must be {CONFIG_SCHEMA_VERSION}"
        )));
    }
    validate_label(&file.target.label)?;
    validate_registration_file(&file.target.registration_file)?;
    Ok(TargetSnapshot {
        label: file.target.label,
        registration_path: runtime_dir
            .join(RUNTIME_SUBDIRECTORY)
            .join(file.target.registration_file),
        generation,
    })
}

fn open_config(path: &Path) -> Result<File, ConfigError> {
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .map_err(|source| ConfigError::Read {
            path: path.to_owned(),
            source,
        })
}

fn current_generation(path: &Path) -> Result<FileGeneration, ConfigError> {
    let file = open_config(path)?;
    FileGeneration::from_file(&file).map_err(|source| ConfigError::Read {
        path: path.to_owned(),
        source,
    })
}

fn validate_label(label: &str) -> Result<(), ConfigError> {
    if label.is_empty()
        || label.len() > MAX_LABEL_BYTES
        || !label
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        return Err(ConfigError::Invalid(
            "target label must be 1-64 ASCII letters, digits, '.', '_' or '-'".to_owned(),
        ));
    }
    Ok(())
}

fn validate_registration_file(name: &str) -> Result<(), ConfigError> {
    let mut components = Path::new(name).components();
    let valid = matches!(components.next(), Some(Component::Normal(_)))
        && components.next().is_none()
        && !name.is_empty();
    if !valid {
        return Err(ConfigError::Invalid(
            "target registration_file must be exactly one relative filename".to_owned(),
        ));
    }
    Ok(())
}

fn event_is_relevant(event: &Event, config_path: &Path) -> bool {
    if event.kind.is_access() {
        return false;
    }
    event.paths.is_empty() || event.paths.iter().any(|path| path == config_path)
}
