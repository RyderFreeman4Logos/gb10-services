#![forbid(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]

use gb10_memory_guardian::{TargetRegistrationSet, TargetTransition};
use gb10_memory_guardian_core::{
    effective_uid, read_mem_available_fd, should_rearm, should_shed, AttemptOutcome, CgroupTarget,
    EmergencyController, EmergencyReserve, DEFAULT_RESERVE_BYTES, DEFAULT_RETRY_MILLIS,
    DEFAULT_THRESHOLD_BYTES,
};
use std::env;
use std::fs::File;
use std::path::PathBuf;
use std::process::ExitCode;
use std::thread;
use std::time::{Duration, Instant};

const CGROUP_ROOT: &str = "/sys/fs/cgroup";
const MEMINFO_PATH: &str = "/proc/meminfo";
const MEMINFO_BUFFER_BYTES: usize = 8 * 1024;
const CONFIG_SUBPATH: &str = "gb10-memory-guardian/config.toml";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Production,
    DisposableCanary,
    KillConfiguredTarget,
}

#[derive(Debug)]
struct Config {
    reserve_bytes: usize,
    threshold_bytes: u64,
    poll_interval: Duration,
    retry_millis: u64,
    meminfo_path: PathBuf,
    cgroup_root: PathBuf,
    runtime_dir: PathBuf,
    target_config_path: PathBuf,
    expected_target_label: Option<String>,
    expected_registration_file: Option<String>,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("gb10-memory-guardian: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let mode = parse_mode()?;
    let config = Config::from_environment()?;
    match mode {
        Mode::Production => run_production(config),
        Mode::DisposableCanary => run_disposable_canary(config),
        Mode::KillConfiguredTarget => run_configured_target_test(config),
    }
}

fn parse_mode() -> Result<Mode, String> {
    let mut arguments = env::args().skip(1);
    let mode = match arguments.next().as_deref() {
        None => Mode::Production,
        Some("--disposable-canary") => Mode::DisposableCanary,
        Some("--kill-configured-target") => Mode::KillConfiguredTarget,
        Some(argument) => return Err(format!("unsupported argument: {argument}")),
    };
    if arguments.next().is_some() {
        return Err("only one fixed mode flag is accepted".to_owned());
    }
    Ok(mode)
}

impl Config {
    fn from_environment() -> Result<Self, String> {
        let reserve_mib = parse_positive_env("GB10_MEMORY_GUARDIAN_RESERVE_MIB", 64)?;
        let threshold_gib = parse_positive_env("GB10_MEMORY_GUARDIAN_MEM_AVAIL_STOP_GIB", 1)?;
        let poll_seconds = parse_positive_env("GB10_MEMORY_GUARDIAN_POLL_SECONDS", 1)?;
        let retry_seconds = parse_positive_env("GB10_MEMORY_GUARDIAN_RETRY_SECONDS", 5)?;
        let reserve_bytes = usize::try_from(reserve_mib)
            .ok()
            .and_then(|value| value.checked_mul(1024 * 1024))
            .ok_or_else(|| "reserve size overflows usize".to_owned())?;
        let threshold_bytes = threshold_gib
            .checked_mul(1024 * 1024 * 1024)
            .ok_or_else(|| "memory threshold overflows u64".to_owned())?;
        let retry_millis = retry_seconds
            .checked_mul(1_000)
            .ok_or_else(|| "retry interval overflows u64".to_owned())?;
        let runtime_dir = env::var_os("XDG_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(format!("/run/user/{}", effective_uid())));
        Ok(Self {
            reserve_bytes,
            threshold_bytes,
            poll_interval: Duration::from_secs(poll_seconds),
            retry_millis,
            meminfo_path: path_env("GB10_MEMORY_GUARDIAN_MEMINFO_PATH", MEMINFO_PATH),
            cgroup_root: path_env("GB10_MEMORY_GUARDIAN_CGROUP_ROOT", CGROUP_ROOT),
            runtime_dir,
            target_config_path: target_config_path()?,
            expected_target_label: optional_string_env("GB10_MEMORY_GUARDIAN_EXPECTED_LABEL")?,
            expected_registration_file: optional_string_env(
                "GB10_MEMORY_GUARDIAN_EXPECTED_REGISTRATION_FILE",
            )?,
        })
    }
}

fn target_config_path() -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("GB10_MEMORY_GUARDIAN_CONFIG_PATH") {
        return Ok(PathBuf::from(path));
    }
    if let Some(config_home) = env::var_os("XDG_CONFIG_HOME") {
        return Ok(PathBuf::from(config_home).join(CONFIG_SUBPATH));
    }
    env::var_os("HOME")
        .map(PathBuf::from)
        .map(|home| home.join(".config").join(CONFIG_SUBPATH))
        .ok_or_else(|| {
            "GB10_MEMORY_GUARDIAN_CONFIG_PATH, XDG_CONFIG_HOME, or HOME is required".to_owned()
        })
}

fn path_env(name: &str, default: &str) -> PathBuf {
    env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(default))
}

fn optional_string_env(name: &str) -> Result<Option<String>, String> {
    let Some(raw) = env::var_os(name) else {
        return Ok(None);
    };
    let value = raw
        .into_string()
        .map_err(|_| format!("{name} is not UTF-8"))?;
    if value.is_empty() {
        return Err(format!("{name} must not be empty"));
    }
    Ok(Some(value))
}

fn parse_positive_env(name: &str, default: u64) -> Result<u64, String> {
    let Some(raw) = env::var_os(name) else {
        return Ok(default);
    };
    let value = raw
        .to_str()
        .ok_or_else(|| format!("{name} is not UTF-8"))?
        .parse::<u64>()
        .map_err(|_| format!("{name} must be a positive integer"))?;
    if value == 0 {
        return Err(format!("{name} must be a positive integer"));
    }
    Ok(value)
}

fn run_production(config: Config) -> Result<(), String> {
    debug_assert_eq!(DEFAULT_RESERVE_BYTES, 64 * 1024 * 1024);
    debug_assert_eq!(DEFAULT_THRESHOLD_BYTES, 1024 * 1024 * 1024);
    debug_assert_eq!(DEFAULT_RETRY_MILLIS, 5_000);

    let meminfo = File::open(&config.meminfo_path)
        .map_err(|error| format!("open {}: {error}", config.meminfo_path.display()))?;
    let reserve = EmergencyReserve::new(config.reserve_bytes)
        .map_err(|error| format!("allocate and touch reserve: {error}"))?;
    let mut controller = EmergencyController::new(reserve, config.retry_millis);
    let mut targets = TargetRegistrationSet::new(
        &config.target_config_path,
        &config.runtime_dir,
        &config.cgroup_root,
        effective_uid(),
    )
    .map_err(|error| {
        format!(
            "load target config {}: {error}",
            config.target_config_path.display()
        )
    })?;
    enforce_expected_target_identity(&targets, &config)?;
    let initial_transition = targets.reconcile();
    if initial_transition.is_ok() {
        enforce_expected_target_identity(&targets, &config)?;
    }
    let mut degraded_logged = match initial_transition {
        Ok(TargetTransition::Armed | TargetTransition::Refreshed | TargetTransition::Swapped) => {
            controller.reset_for_target_generation();
            eprintln!(
                "gb10-memory-guardian: armed target {}",
                targets.active_label()
            );
            false
        }
        Ok(TargetTransition::Unchanged | TargetTransition::Superseded) => false,
        Err(error) => {
            eprintln!("gb10-memory-guardian: waiting for initial target registration: {error}");
            true
        }
    };
    let started = Instant::now();
    let mut meminfo_buffer = [0_u8; MEMINFO_BUFFER_BYTES];

    loop {
        let mem_available = match read_mem_available_fd(&meminfo, &mut meminfo_buffer) {
            Ok(value) => value,
            Err(error) => {
                eprintln!("gb10-memory-guardian: degraded meminfo read: {error}");
                thread::sleep(config.poll_interval);
                continue;
            }
        };

        if should_shed(mem_available, config.threshold_bytes) {
            controller.enter_emergency();
            if let Some(target) = targets.target() {
                let now = elapsed_millis(started);
                if matches!(
                    controller.attempt(now, target),
                    AttemptOutcome::Verified { .. }
                ) {
                    targets.disarm();
                }
            }
        } else {
            let transition = targets.reconcile();
            if transition.is_ok() {
                enforce_expected_target_identity(&targets, &config)?;
            }
            match transition {
                Ok(
                    TargetTransition::Armed
                    | TargetTransition::Refreshed
                    | TargetTransition::Swapped,
                ) => {
                    controller.reset_for_target_generation();
                    degraded_logged = false;
                    eprintln!(
                        "gb10-memory-guardian: armed target {}",
                        targets.active_label()
                    );
                }
                Ok(TargetTransition::Unchanged | TargetTransition::Superseded) => {
                    degraded_logged = false;
                }
                Err(error) if !degraded_logged => {
                    eprintln!("gb10-memory-guardian: retaining last-good target: {error}");
                    degraded_logged = true;
                }
                Err(_) => {}
            }
            if !controller.reserve().is_allocated()
                && should_rearm(mem_available, config.threshold_bytes, config.reserve_bytes)
            {
                match controller.ensure_reserve(config.reserve_bytes) {
                    Ok(true) => {
                        eprintln!("gb10-memory-guardian: emergency reserve rearmed");
                    }
                    Ok(false) => {}
                    Err(error) => {
                        eprintln!(
                            "gb10-memory-guardian: degraded reserve could not be rearmed: {error}"
                        );
                    }
                }
            }
        }
        thread::sleep(config.poll_interval);
    }
}

fn enforce_expected_target_identity(
    targets: &TargetRegistrationSet,
    config: &Config,
) -> Result<(), String> {
    let expected_label = config.expected_target_label.as_deref().ok_or_else(|| {
        "GB10_MEMORY_GUARDIAN_EXPECTED_LABEL is required in production".to_owned()
    })?;
    let expected_registration_file =
        config
            .expected_registration_file
            .as_deref()
            .ok_or_else(|| {
                "GB10_MEMORY_GUARDIAN_EXPECTED_REGISTRATION_FILE is required in production"
                    .to_owned()
            })?;
    let active_registration_file = targets
        .active_registration_path()
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| "active registration path has no UTF-8 file name".to_owned())?;
    if targets.active_label() != expected_label
        || active_registration_file != expected_registration_file
    {
        return Err(format!(
            "configured target identity mismatch: expected {expected_label}/{expected_registration_file}, got {}/{active_registration_file}",
            targets.active_label()
        ));
    }
    Ok(())
}

fn elapsed_millis(started: Instant) -> u64 {
    u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX)
}

fn run_disposable_canary(config: Config) -> Result<(), String> {
    let target = CgroupTarget::open_disposable_canary(&config.cgroup_root, effective_uid())
        .map_err(|error| format!("open rigid disposable canary cgroup: {error}"))?;
    run_bounded_direct_test(config, &target, "disposable canary")
}

fn run_configured_target_test(config: Config) -> Result<(), String> {
    let mut targets = TargetRegistrationSet::new(
        &config.target_config_path,
        &config.runtime_dir,
        &config.cgroup_root,
        effective_uid(),
    )
    .map_err(|error| format!("configured-target initialization failed: {error}"))?;
    targets
        .reconcile()
        .map_err(|error| format!("configured-target validation failed: {error}"))?;
    let label = targets.active_label().to_string();
    let target = targets
        .target()
        .ok_or_else(|| "configured target is not armed".to_string())?;
    run_bounded_direct_test(config, target, &label)
}

fn run_bounded_direct_test(
    config: Config,
    target: &CgroupTarget,
    label: &str,
) -> Result<(), String> {
    let reserve = EmergencyReserve::new(config.reserve_bytes)
        .map_err(|error| format!("allocate and touch reserve: {error}"))?;
    let mut controller = EmergencyController::new(reserve, config.retry_millis);
    let mut now = 0;
    for attempt in 1..=4 {
        let outcome = controller.attempt(now, target);
        // This message is emitted only after the direct cgroup.kill attempt.
        eprintln!(
            "gb10-memory-guardian: {label} direct kill attempt {attempt}: {outcome:?}; reserve_allocated={}",
            controller.reserve().is_allocated()
        );
        if matches!(outcome, AttemptOutcome::Verified { .. }) {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(config.retry_millis));
        now = now.saturating_add(config.retry_millis);
    }
    Err(format!("{label} target was not verified empty or gone"))
}
