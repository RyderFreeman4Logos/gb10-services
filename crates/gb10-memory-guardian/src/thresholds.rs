//! Notify-backed, atomically published memory-threshold configuration.

use super::{absolute_path, load_file_config, ConfigError, FileGeneration, CONFIG_SCHEMA_VERSION};
use notify::{RecursiveMode, Watcher};
use serde::Deserialize;
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};

const MIB: u64 = 1024 * 1024;
const GIB: u64 = 1024 * MIB;

/// A validated, internally consistent pair of live memory thresholds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Thresholds {
    mem_avail_stop_gib: u64,
    reserve_mib: u64,
    threshold_bytes: u64,
    reserve_bytes: usize,
}

impl Thresholds {
    /// Validate threshold units and precompute the allocation-free loop values.
    pub fn new(mem_avail_stop_gib: u64, reserve_mib: u64) -> Result<Self, ConfigError> {
        if mem_avail_stop_gib == 0 {
            return Err(ConfigError::Invalid(
                "thresholds.mem_avail_stop_gib must be a positive integer".to_owned(),
            ));
        }
        if reserve_mib == 0 {
            return Err(ConfigError::Invalid(
                "thresholds.reserve_mib must be a positive integer".to_owned(),
            ));
        }
        let threshold_bytes = mem_avail_stop_gib.checked_mul(GIB).ok_or_else(|| {
            ConfigError::Invalid("thresholds.mem_avail_stop_gib overflows u64 bytes".to_owned())
        })?;
        let reserve_bytes_u64 = reserve_mib.checked_mul(MIB).ok_or_else(|| {
            ConfigError::Invalid("thresholds.reserve_mib overflows u64 bytes".to_owned())
        })?;
        let reserve_bytes = usize::try_from(reserve_bytes_u64).map_err(|_| {
            ConfigError::Invalid("thresholds.reserve_mib overflows usize bytes".to_owned())
        })?;
        Ok(Self {
            mem_avail_stop_gib,
            reserve_mib,
            threshold_bytes,
            reserve_bytes,
        })
    }

    /// Return the operator-facing stop threshold in GiB.
    pub fn mem_avail_stop_gib(self) -> u64 {
        self.mem_avail_stop_gib
    }

    /// Return the operator-facing emergency reserve in MiB.
    pub fn reserve_mib(self) -> u64 {
        self.reserve_mib
    }

    /// Return the stop threshold in bytes for the polling loop.
    pub fn threshold_bytes(self) -> u64 {
        self.threshold_bytes
    }

    /// Return the emergency reserve in bytes for rearm checks and allocation.
    pub fn reserve_bytes(self) -> usize {
        self.reserve_bytes
    }
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FileThresholds {
    mem_avail_stop_gib: Option<u64>,
    reserve_mib: Option<u64>,
}

impl FileThresholds {
    fn resolve(self, defaults: Thresholds) -> Result<Thresholds, ConfigError> {
        Thresholds::new(
            self.mem_avail_stop_gib
                .unwrap_or(defaults.mem_avail_stop_gib()),
            self.reserve_mib.unwrap_or(defaults.reserve_mib()),
        )
    }
}

#[derive(Debug)]
struct ThresholdStore {
    values: RwLock<Thresholds>,
}

impl ThresholdStore {
    fn new(values: Thresholds) -> Self {
        Self {
            values: RwLock::new(values),
        }
    }

    fn current(&self) -> Thresholds {
        let values = match self.values.read() {
            Ok(values) => values,
            Err(poisoned) => poisoned.into_inner(),
        };
        *values
    }

    fn replace(&self, values: Thresholds) {
        let mut live = match self.values.write() {
            Ok(live) => live,
            Err(poisoned) => poisoned.into_inner(),
        };
        *live = values;
    }
}

#[derive(Debug, Default)]
struct ReloadSignal {
    changed: AtomicBool,
    watch_error: Mutex<Option<notify::Error>>,
}

/// Owns a directory watcher while publishing only healthy-loop reloads.
pub struct HotReloadableConfig {
    config_path: PathBuf,
    defaults: Thresholds,
    live: ThresholdStore,
    signal: Arc<ReloadSignal>,
    _watcher: notify::RecommendedWatcher,
}

impl fmt::Debug for HotReloadableConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("HotReloadableConfig")
            .field("config_path", &self.config_path)
            .field("defaults", &self.defaults)
            .field("live", &self.live)
            .finish_non_exhaustive()
    }
}

impl HotReloadableConfig {
    /// Watch the config parent and load the initial TOML-over-environment values.
    pub fn new(config_path: &Path, defaults: Thresholds) -> Result<Self, ConfigError> {
        let config_path = absolute_path(config_path)?;
        let parent = config_path
            .parent()
            .ok_or_else(|| ConfigError::MissingParent(config_path.clone()))?;
        let signal = Arc::new(ReloadSignal::default());
        let callback_signal = Arc::clone(&signal);
        let watched_path = config_path.clone();
        let mut watcher: notify::RecommendedWatcher = notify::recommended_watcher(
            move |result: notify::Result<notify::Event>| match result {
                Ok(event) if event.paths.iter().any(|path| path == &watched_path) => {
                    callback_signal.changed.store(true, Ordering::Release);
                }
                Ok(_) => {}
                Err(error) => {
                    let mut slot = match callback_signal.watch_error.lock() {
                        Ok(slot) => slot,
                        Err(poisoned) => poisoned.into_inner(),
                    };
                    *slot = Some(error);
                }
            },
        )
        .map_err(ConfigError::Watch)?;
        watcher
            .watch(parent, RecursiveMode::NonRecursive)
            .map_err(ConfigError::Watch)?;
        let initial = load_thresholds(&config_path, defaults)?;
        Ok(Self {
            config_path,
            defaults,
            live: ThresholdStore::new(initial),
            signal,
            _watcher: watcher,
        })
    }

    /// Read one coherent threshold pair without parsing or filesystem access.
    pub fn current(&self) -> Thresholds {
        self.live.current()
    }

    /// Apply one pending notify event. Call this only from a healthy iteration.
    /// Invalid candidates return an error and retain the last-good values.
    pub fn reload_if_changed(&self) -> Result<Option<Thresholds>, ConfigError> {
        let watch_error = {
            let mut slot = match self.signal.watch_error.lock() {
                Ok(slot) => slot,
                Err(poisoned) => poisoned.into_inner(),
            };
            slot.take()
        };
        if let Some(error) = watch_error {
            return Err(ConfigError::WatchEvent(error));
        }
        if !self.signal.changed.swap(false, Ordering::AcqRel) {
            return Ok(None);
        }

        let reloaded = load_thresholds(&self.config_path, self.defaults)?;
        self.live.replace(reloaded);
        Ok(Some(reloaded))
    }
}

fn load_thresholds(config_path: &Path, defaults: Thresholds) -> Result<Thresholds, ConfigError> {
    let (file, _generation): (_, FileGeneration) = load_file_config(config_path)?;
    if file.schema_version != CONFIG_SCHEMA_VERSION {
        return Err(ConfigError::Invalid(format!(
            "schema_version must be {CONFIG_SCHEMA_VERSION}"
        )));
    }
    file.thresholds.resolve(defaults)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn threshold_store_never_exposes_a_mixed_pair() {
        let first = Thresholds::new(1, 64).expect("first thresholds");
        let second = Thresholds::new(4, 128).expect("second thresholds");
        let store = Arc::new(ThresholdStore::new(first));
        let writer_store = Arc::clone(&store);
        let writer = thread::spawn(move || {
            for iteration in 0..20_000 {
                let values = if iteration % 2 == 0 { second } else { first };
                writer_store.replace(values);
            }
        });
        let mut readers = Vec::new();
        for _ in 0..4 {
            let reader_store = Arc::clone(&store);
            readers.push(thread::spawn(move || {
                for _ in 0..20_000 {
                    let values = reader_store.current();
                    assert!(
                        values == first || values == second,
                        "mixed pair: {values:?}"
                    );
                }
            }));
        }

        writer.join().expect("writer thread");
        for reader in readers {
            reader.join().expect("reader thread");
        }
    }
}
