#![forbid(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]

use gb10_memory_guardian_core::{
    effective_uid, read_mem_available_fd, should_rearm, should_shed, target_state_requires_disarm,
    AttemptOutcome, CgroupTarget, EmergencyController, EmergencyReserve, RefreshStatus,
    RegistrationManager, TargetState, DEFAULT_RESERVE_BYTES, DEFAULT_RETRY_MILLIS,
    DEFAULT_THRESHOLD_BYTES,
};
use std::env;
use std::fs::File;
use std::path::PathBuf;
use std::process::ExitCode;
use std::thread;
use std::time::{Duration, Instant};

const CGROUP_ROOT: &str = "/sys/fs/cgroup";
const REGISTRATION_NAME: &str = "gb10-memory-guardian/querit-cgroup.v1";
const MEMINFO_PATH: &str = "/proc/meminfo";
const MEMINFO_BUFFER_BYTES: usize = 8 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Production,
    DisposableCanary,
    ShedRegisteredQuerit,
}

#[derive(Debug)]
struct Config {
    reserve_bytes: usize,
    threshold_bytes: u64,
    poll_interval: Duration,
    retry_millis: u64,
    meminfo_path: PathBuf,
    cgroup_root: PathBuf,
    registration_path: PathBuf,
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
        Mode::ShedRegisteredQuerit => run_registered_shed(config),
    }
}

fn parse_mode() -> Result<Mode, String> {
    let mut arguments = env::args().skip(1);
    let mode = match arguments.next().as_deref() {
        None => Mode::Production,
        Some("--disposable-canary") => Mode::DisposableCanary,
        Some("--shed-registered-querit") => Mode::ShedRegisteredQuerit,
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
        let default_registration = runtime_dir.join(REGISTRATION_NAME);
        Ok(Self {
            reserve_bytes,
            threshold_bytes,
            poll_interval: Duration::from_secs(poll_seconds),
            retry_millis,
            meminfo_path: path_env("GB10_MEMORY_GUARDIAN_MEMINFO_PATH", MEMINFO_PATH),
            cgroup_root: path_env("GB10_MEMORY_GUARDIAN_CGROUP_ROOT", CGROUP_ROOT),
            registration_path: env::var_os("GB10_MEMORY_GUARDIAN_REGISTRATION_PATH")
                .map(PathBuf::from)
                .unwrap_or(default_registration),
        })
    }
}

fn path_env(name: &str, default: &str) -> PathBuf {
    env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(default))
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
    let mut registrations = RegistrationManager::new(
        &config.registration_path,
        &config.cgroup_root,
        effective_uid(),
    );
    let started = Instant::now();
    let mut meminfo_buffer = [0_u8; MEMINFO_BUFFER_BYTES];
    let mut degraded_logged = false;

    loop {
        let refresh = registrations.refresh();
        if matches!(refresh, Ok(RefreshStatus::Replaced)) {
            controller.reset_for_target_generation();
            degraded_logged = false;
        }

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
            if let Some(target) = registrations.target() {
                let now = elapsed_millis(started);
                let outcome = controller.attempt(now, target);
                // Emergency logging is intentionally after the direct write attempt.
                if outcome.attempted() {
                    eprintln!(
                        "gb10-memory-guardian: direct Querit kill attempt: {outcome:?}; reserve_allocated={}",
                        controller.reserve().is_allocated()
                    );
                }
                if matches!(outcome, AttemptOutcome::Verified { .. }) {
                    registrations.clear();
                }
            } else if let Err(error) = &refresh {
                if !degraded_logged {
                    eprintln!(
                        "gb10-memory-guardian: degraded registration {}: {error}",
                        config.registration_path.display()
                    );
                    degraded_logged = true;
                }
            }
        } else {
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
            if let Some(target) = registrations.target() {
                let target_state = target.state();
                if matches!(target_state, Ok(TargetState::Populated)) {
                    controller.reset_for_target_generation();
                } else if target_state_requires_disarm(&target_state) {
                    registrations.clear();
                    if !degraded_logged {
                        eprintln!("gb10-memory-guardian: registered cgroup is stale; disarmed");
                        degraded_logged = true;
                    }
                } else if !degraded_logged {
                    eprintln!(
                        "gb10-memory-guardian: registered cgroup state unreadable; retaining armed target"
                    );
                    degraded_logged = true;
                }
            }
            if let Err(error) = &refresh {
                if !degraded_logged {
                    eprintln!(
                        "gb10-memory-guardian: degraded registration {}: {error}",
                        config.registration_path.display()
                    );
                    degraded_logged = true;
                }
            }
        }
        thread::sleep(config.poll_interval);
    }
}

fn elapsed_millis(started: Instant) -> u64 {
    u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX)
}

fn run_disposable_canary(config: Config) -> Result<(), String> {
    let target = CgroupTarget::open_disposable_canary(&config.cgroup_root, effective_uid())
        .map_err(|error| format!("open rigid disposable canary cgroup: {error}"))?;
    run_bounded_direct_test(config, &target, "disposable canary")
}

fn run_registered_shed(config: Config) -> Result<(), String> {
    let mut registrations = RegistrationManager::new(
        &config.registration_path,
        &config.cgroup_root,
        effective_uid(),
    );
    registrations
        .refresh()
        .map_err(|error| format!("open strict Querit registration: {error}"))?;
    let target = registrations
        .target()
        .ok_or_else(|| "strict Querit registration has no armed target".to_owned())?;
    run_bounded_direct_test(config, target, "registered Querit")
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
