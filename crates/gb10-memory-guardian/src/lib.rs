//! Transactional configuration snapshots for the GB10 memory guardian.
//!
//! Healthy-loop polling only produces candidates. The production loop must
//! validate and arm the candidate registration before explicitly committing it.

use gb10_memory_guardian_core::{
    effective_uid, AttemptOutcome, CgroupTarget, EmergencyController, GuardianError, RefreshStatus,
    RegistrationGeneration, RegistrationManager,
};
use serde::Deserialize;
use std::env;
use std::error::Error;
use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EmergencyIteration {
    NoTarget,
    Waiting,
    Retry,
    Verified,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum EmergencyRefresh {
    Unchanged,
    Replaced,
    NoTarget,
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
    changed_seconds: i64,
    changed_nanoseconds: i64,
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
        if metadata.uid() != effective_uid() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "config must be owned by the effective user",
            ));
        }
        if metadata.nlink() != 1 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "config must have exactly one hard link",
            ));
        }
        if metadata.mode() & 0o7777 != 0o600 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "config mode must be exactly 0600",
            ));
        }
        Ok(Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            size: metadata.size(),
            modified_seconds: metadata.mtime(),
            modified_nanoseconds: metadata.mtime_nsec(),
            changed_seconds: metadata.ctime(),
            changed_nanoseconds: metadata.ctime_nsec(),
        })
    }
}

/// Polls the config generation only from the healthy loop. This deliberately
/// avoids a watcher thread that could allocate while the daemon is latched.
pub struct TargetConfigMonitor {
    config_path: PathBuf,
    runtime_dir: PathBuf,
    active: TargetSnapshot,
    pending: Option<TargetSnapshot>,
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
        let active = load_snapshot(&config_path, &runtime_dir)?;
        Ok(Self {
            config_path,
            runtime_dir,
            active,
            pending: None,
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
        let generation = current_generation(&self.config_path)?;
        if generation == self.active.generation
            || self
                .pending
                .as_ref()
                .is_some_and(|pending| pending.generation == generation)
        {
            return Ok(());
        }
        match load_snapshot(&self.config_path, &self.runtime_dir) {
            Ok(snapshot) => self.pending = Some(snapshot),
            Err(error) => {
                self.pending = None;
                return Err(error);
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

    pub fn active_registration_path(&self) -> &Path {
        self.monitor.active().registration_path()
    }

    pub fn pending_label(&self) -> Option<&str> {
        self.pending
            .as_ref()
            .map(|pending| pending.snapshot.label())
    }

    pub fn target(&self) -> Option<&CgroupTarget> {
        self.active.target()
    }

    pub fn registration_generation(&self) -> Option<RegistrationGeneration> {
        self.active.generation_identity()
    }

    pub fn disarm(&mut self) {
        self.active.clear();
    }

    /// Refresh only the immutable registration selected by the active config.
    /// Pending config changes are intentionally ignored during an emergency.
    pub fn reconcile_active_registration(
        &mut self,
    ) -> Result<TargetTransition, TargetRegistrationError> {
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

    /// Allocation-free registration refresh for the emergency latch. Active
    /// compact errors degrade to NoTarget only when they disarm the descriptor
    /// set; transient errors retain and continue attacking the current target.
    fn refresh_active_registration_emergency(&mut self) -> EmergencyRefresh {
        match self.active.refresh() {
            Ok(RefreshStatus::Replaced) => EmergencyRefresh::Replaced,
            Ok(RefreshStatus::Unchanged) => EmergencyRefresh::Unchanged,
            Err(_) if self.active.target().is_none() => EmergencyRefresh::NoTarget,
            Err(_) => EmergencyRefresh::Unchanged,
        }
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

        self.reconcile_active_registration()
    }
}

/// Execute one allocation-free low-memory iteration. The current generation is
/// attempted before the immutable registration is probed. A changed or
/// same-scope-recreated generation resets retry state and is attempted in this
/// same iteration.
pub fn emergency_iteration(
    controller: &mut EmergencyController,
    targets: &mut TargetRegistrationSet,
    now_millis: u64,
) -> EmergencyIteration {
    controller.enter_emergency();
    let first = match targets.target() {
        Some(target) => attempt_iteration(controller, target, now_millis),
        None => EmergencyIteration::NoTarget,
    };

    match targets.refresh_active_registration_emergency() {
        EmergencyRefresh::Replaced => {
            controller.reset_for_target_generation();
            let Some(target) = targets.target() else {
                return EmergencyIteration::NoTarget;
            };
            let replacement = attempt_iteration(controller, target, now_millis);
            if matches!(replacement, EmergencyIteration::Verified) {
                targets.disarm();
            }
            replacement
        }
        EmergencyRefresh::NoTarget => EmergencyIteration::NoTarget,
        EmergencyRefresh::Unchanged => {
            if matches!(first, EmergencyIteration::Verified) {
                targets.disarm();
            }
            first
        }
    }
}

fn attempt_iteration(
    controller: &mut EmergencyController,
    target: &CgroupTarget,
    now_millis: u64,
) -> EmergencyIteration {
    match controller.attempt(now_millis, target) {
        AttemptOutcome::Waiting => EmergencyIteration::Waiting,
        AttemptOutcome::Retry { .. } => EmergencyIteration::Retry,
        AttemptOutcome::Verified { .. } => EmergencyIteration::Verified,
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
    let mut bytes = name.bytes();
    let valid = name.len() <= 128
        && bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'));
    if !valid {
        return Err(ConfigError::Invalid(
            "target registration_file must be one safe 1-128 byte ASCII filename".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn malformed_polled_candidate_preserves_last_good_snapshot() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock before epoch")
            .as_nanos();
        let root = env::temp_dir().join(format!(
            "gb10-memory-guardian-poll-test-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&root).expect("create test root");
        let config_path = root.join("config.toml");
        fs::write(
            &config_path,
            "schema_version = 1\n[target]\nlabel = \"aeon-text\"\nregistration_file = \"text-cgroup.v1\"\n",
        )
        .expect("write config");
        fs::set_permissions(&config_path, fs::Permissions::from_mode(0o600)).expect("chmod config");

        let mut monitor =
            TargetConfigMonitor::new(&config_path, &root).expect("create config monitor");
        let last_good = monitor.active().clone();
        fs::write(&config_path, "not valid toml").expect("replace with invalid config");
        fs::set_permissions(&config_path, fs::Permissions::from_mode(0o600)).expect("chmod config");

        let _error = monitor
            .pending_snapshot()
            .expect_err("invalid polled candidate must fail closed");
        assert_eq!(monitor.active(), &last_good);

        drop(monitor);
        fs::remove_dir_all(root).expect("remove test root");
    }
}
