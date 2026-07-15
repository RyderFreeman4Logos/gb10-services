#![forbid(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]

mod escalation;

use escalation::{start_unit_with_timeout, validate_unit_name, EscalationEpisode};
use gb10_memory_guardian::{
    emergency_iteration, EmergencyIteration, TargetRegistrationSet, TargetTransition,
};
use gb10_memory_guardian_core::{
    effective_uid, read_mem_available_fd, should_rearm, should_shed, AttemptOutcome, CgroupTarget,
    EmergencyController, EmergencyReserve, DEFAULT_RESERVE_BYTES, DEFAULT_RETRY_MILLIS,
    DEFAULT_THRESHOLD_BYTES,
};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::PathBuf;
use std::process::ExitCode;
use std::thread;
use std::time::{Duration, Instant};

const CGROUP_ROOT: &str = "/sys/fs/cgroup";
const MEMINFO_PATH: &str = "/proc/meminfo";
const MEMINFO_BUFFER_BYTES: usize = 8 * 1024;
const CONFIG_SUBPATH: &str = "gb10-memory-guardian/config.toml";
const DEFAULT_ESCALATION_GRACE_SECONDS: u64 = 60;
const DEFAULT_ESCALATION_UNIT: &str = "gb10-stack-recovery.service";
const ESCALATION_START_TIMEOUT: Duration = Duration::from_secs(10);

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
    escalation_grace_seconds: u64,
    escalation_unit: String,
    escalation_enabled: bool,
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
        let escalation_grace_seconds = parse_positive_env(
            "GB10_MEMORY_GUARDIAN_ESCALATION_GRACE_SECONDS",
            DEFAULT_ESCALATION_GRACE_SECONDS,
        )?;
        let escalation_unit = string_env(
            "GB10_MEMORY_GUARDIAN_ESCALATION_UNIT",
            DEFAULT_ESCALATION_UNIT,
        )?;
        validate_unit_name(&escalation_unit)?;
        let escalation_enabled = parse_bool_env("GB10_MEMORY_GUARDIAN_ESCALATION_ENABLED", true)?;
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
            escalation_grace_seconds,
            escalation_unit,
            escalation_enabled,
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

fn string_env(name: &str, default: &str) -> Result<String, String> {
    let Some(raw) = env::var_os(name) else {
        return Ok(default.to_owned());
    };
    let value = raw
        .into_string()
        .map_err(|_| format!("{name} is not UTF-8"))?;
    if value.is_empty() {
        return Err(format!("{name} must not be empty"));
    }
    Ok(value)
}

fn parse_bool_env(name: &str, default: bool) -> Result<bool, String> {
    let Some(raw) = env::var_os(name) else {
        return Ok(default);
    };
    match raw.to_str() {
        Some("true") => Ok(true),
        Some("false") => Ok(false),
        Some(_) => Err(format!("{name} must be true or false")),
        None => Err(format!("{name} is not UTF-8")),
    }
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LoopAction {
    Emergency,
    Rearm,
    Healthy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct EmergencyLatch {
    latched: bool,
}

impl EmergencyLatch {
    fn new() -> Self {
        Self { latched: false }
    }

    fn is_latched(self) -> bool {
        self.latched
    }

    fn next_action(
        &mut self,
        mem_available: u64,
        threshold_bytes: u64,
        reserve_bytes: usize,
    ) -> LoopAction {
        if should_shed(mem_available, threshold_bytes) {
            self.latched = true;
            return LoopAction::Emergency;
        }
        if self.latched {
            if should_rearm(mem_available, threshold_bytes, reserve_bytes) {
                LoopAction::Rearm
            } else {
                LoopAction::Emergency
            }
        } else {
            LoopAction::Healthy
        }
    }

    fn acknowledge_rearmed(&mut self) {
        self.latched = false;
    }
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
    publish_guardian_status(&config, &targets)?;
    let initial_transition = targets.reconcile();
    if initial_transition.is_ok() {
        enforce_expected_target_identity(&targets, &config)?;
    }
    publish_guardian_status(&config, &targets)?;
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
    let mut latch = EmergencyLatch::new();
    let mut escalation = EscalationEpisode::new();
    let mut emergency_status_deferred = false;
    let mut reserve_failure_deferred = false;

    loop {
        let mem_available = match read_mem_available_fd(&meminfo, &mut meminfo_buffer) {
            Ok(value) => value,
            Err(error) => {
                if latch.is_latched() {
                    let had_target_before = targets.target().is_some();
                    let iteration =
                        emergency_iteration(&mut controller, &mut targets, elapsed_millis(started));
                    emergency_status_deferred = true;
                    maybe_escalate_after_iteration(
                        &mut escalation,
                        iteration,
                        had_target_before,
                        &config,
                    );
                } else {
                    eprintln!("gb10-memory-guardian: degraded meminfo read: {error}");
                }
                thread::sleep(config.poll_interval);
                continue;
            }
        };

        let mut had_target_before = false;
        let emergency_iteration_result = match latch.next_action(
            mem_available,
            config.threshold_bytes,
            config.reserve_bytes,
        ) {
            LoopAction::Emergency => {
                had_target_before = targets.target().is_some();
                let iteration =
                    emergency_iteration(&mut controller, &mut targets, elapsed_millis(started));
                emergency_status_deferred = true;
                Some(iteration)
            }
            LoopAction::Rearm => match controller.ensure_reserve(config.reserve_bytes) {
                Ok(_) => {
                    latch.acknowledge_rearmed();
                    escalation.reset();
                    if reserve_failure_deferred {
                        eprintln!(
                                "gb10-memory-guardian: reserve rearm recovered after a deferred failure"
                            );
                        reserve_failure_deferred = false;
                    }
                    eprintln!("gb10-memory-guardian: emergency reserve rearmed");
                    if emergency_status_deferred {
                        run_healthy_iteration(
                            &mut controller,
                            &mut targets,
                            &config,
                            &mut degraded_logged,
                        )?;
                        emergency_status_deferred = false;
                    }
                    None
                }
                Err(_) => {
                    reserve_failure_deferred = true;
                    had_target_before = targets.target().is_some();
                    let iteration =
                        emergency_iteration(&mut controller, &mut targets, elapsed_millis(started));
                    emergency_status_deferred = true;
                    Some(iteration)
                }
            },
            LoopAction::Healthy => {
                run_healthy_iteration(
                    &mut controller,
                    &mut targets,
                    &config,
                    &mut degraded_logged,
                )?;
                None
            }
        };
        if let Some(iteration) = emergency_iteration_result {
            maybe_escalate_after_iteration(&mut escalation, iteration, had_target_before, &config);
        }
        thread::sleep(config.poll_interval);
    }
}

fn maybe_escalate_after_iteration(
    escalation: &mut EscalationEpisode,
    iteration: EmergencyIteration,
    had_target_before: bool,
    config: &Config,
) {
    let action_taken = matches!(
        iteration,
        EmergencyIteration::Retry | EmergencyIteration::Verified
    ) || (had_target_before
        && matches!(iteration, EmergencyIteration::NoTarget));
    let should_trigger = escalation.after_emergency_iteration(
        Instant::now(),
        action_taken,
        config.escalation_enabled,
        Duration::from_secs(config.escalation_grace_seconds),
    );
    if !should_trigger {
        return;
    }

    eprintln!(
        "gb10-memory-guardian: escalating to Tier 2 after {}s without memory recovery",
        config.escalation_grace_seconds
    );
    if let Err(error) = start_unit_with_timeout(&config.escalation_unit, ESCALATION_START_TIMEOUT) {
        eprintln!("gb10-memory-guardian: Tier 2 escalation trigger failed: {error}");
    }
}

fn run_healthy_iteration(
    controller: &mut EmergencyController,
    targets: &mut TargetRegistrationSet,
    config: &Config,
    degraded_logged: &mut bool,
) -> Result<(), String> {
    let transition = targets.reconcile();
    if transition.is_ok() {
        enforce_expected_target_identity(targets, config)?;
    }
    publish_guardian_status(config, targets)?;
    match transition {
        Ok(TargetTransition::Armed | TargetTransition::Refreshed | TargetTransition::Swapped) => {
            controller.reset_for_target_generation();
            *degraded_logged = false;
            eprintln!(
                "gb10-memory-guardian: armed target {}",
                targets.active_label()
            );
        }
        Ok(TargetTransition::Unchanged | TargetTransition::Superseded) => {
            *degraded_logged = false;
        }
        Err(error) if !*degraded_logged => {
            eprintln!("gb10-memory-guardian: retaining last-good target: {error}");
            *degraded_logged = true;
        }
        Err(_) => {}
    }
    Ok(())
}

fn publish_guardian_status(config: &Config, targets: &TargetRegistrationSet) -> Result<(), String> {
    let status_dir = config.runtime_dir.join("gb10-memory-guardian");
    fs::create_dir_all(&status_dir)
        .map_err(|error| format!("create status directory {}: {error}", status_dir.display()))?;
    let directory_metadata = fs::symlink_metadata(&status_dir)
        .map_err(|error| format!("inspect status directory {}: {error}", status_dir.display()))?;
    if !directory_metadata.is_dir()
        || directory_metadata.file_type().is_symlink()
        || directory_metadata.uid() != effective_uid()
        || directory_metadata.mode() & 0o7777 != 0o700
    {
        return Err(format!(
            "status directory is not an owner-only real directory: {}",
            status_dir.display()
        ));
    }

    let guardian_invocation_id = env::var("INVOCATION_ID")
        .map_err(|_| "INVOCATION_ID is required for status publication".to_owned())?;
    if guardian_invocation_id.len() != 32
        || !guardian_invocation_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("INVOCATION_ID must be exactly 32 lowercase hexadecimal bytes".to_owned());
    }

    let registration_file = targets
        .active_registration_path()
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| "active registration path has no UTF-8 file name".to_owned())?;
    let (
        state,
        container_id,
        scope,
        control_group,
        registration_device,
        registration_inode,
        registration_size,
        registration_modified_seconds,
        registration_modified_nanoseconds,
        registration_changed_seconds,
        registration_changed_nanoseconds,
        device,
        inode,
    ) = if let Some(target) = targets.target() {
        let registration = targets.registration_generation().ok_or_else(|| {
            "armed target is missing its retained registration generation".to_owned()
        })?;
        let scope = target
            .scope_name()
            .map_err(|error| format!("read armed scope identity: {error}"))?;
        let container_id = scope
            .strip_prefix("docker-")
            .and_then(|value| value.strip_suffix(".scope"))
            .ok_or_else(|| "armed scope is not an exact Docker scope".to_owned())?;
        if container_id.len() != 64
            || !container_id
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err("armed scope contains an invalid container identity".to_owned());
        }
        let (device, inode) = target
            .generation_identity()
            .map_err(|error| format!("read armed cgroup generation: {error}"))?;
        (
            "armed",
            container_id.to_owned(),
            scope.to_owned(),
            format!(
                "/user.slice/user-{}.slice/user@{}.service/app.slice/{scope}",
                effective_uid(),
                effective_uid()
            ),
            registration.device,
            registration.inode,
            registration.size,
            registration.modified_seconds,
            registration.modified_nanoseconds,
            registration.changed_seconds,
            registration.changed_nanoseconds,
            device,
            inode,
        )
    } else {
        (
            "disarmed",
            "-".to_owned(),
            "-".to_owned(),
            "-".to_owned(),
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    };

    let status_path = status_dir.join("guardian-status.v2");
    let temporary_path = status_dir.join(format!(".guardian-status.v2.tmp.{}", std::process::id()));
    match fs::remove_file(&temporary_path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(format!(
                "remove stale status temporary {}: {error}",
                temporary_path.display()
            ));
        }
    }
    let mut temporary = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary_path)
        .map_err(|error| {
            format!(
                "create status temporary {}: {error}",
                temporary_path.display()
            )
        })?;
    let write_result = writeln!(temporary, "version=2")
        .and_then(|()| writeln!(temporary, "state={state}"))
        .and_then(|()| writeln!(temporary, "label={}", targets.active_label()))
        .and_then(|()| writeln!(temporary, "registration_file={registration_file}"))
        .and_then(|()| writeln!(temporary, "registration_device={registration_device}"))
        .and_then(|()| writeln!(temporary, "registration_inode={registration_inode}"))
        .and_then(|()| writeln!(temporary, "registration_size={registration_size}"))
        .and_then(|()| {
            writeln!(
                temporary,
                "registration_modified_seconds={registration_modified_seconds}"
            )
        })
        .and_then(|()| {
            writeln!(
                temporary,
                "registration_modified_nanoseconds={registration_modified_nanoseconds}"
            )
        })
        .and_then(|()| {
            writeln!(
                temporary,
                "registration_changed_seconds={registration_changed_seconds}"
            )
        })
        .and_then(|()| {
            writeln!(
                temporary,
                "registration_changed_nanoseconds={registration_changed_nanoseconds}"
            )
        })
        .and_then(|()| writeln!(temporary, "container_id={container_id}"))
        .and_then(|()| writeln!(temporary, "scope={scope}"))
        .and_then(|()| writeln!(temporary, "control_group={control_group}"))
        .and_then(|()| writeln!(temporary, "cgroup_device={device}"))
        .and_then(|()| writeln!(temporary, "cgroup_inode={inode}"))
        .and_then(|()| writeln!(temporary, "guardian_pid={}", std::process::id()))
        .and_then(|()| writeln!(temporary, "guardian_invocation_id={guardian_invocation_id}"))
        .and_then(|()| temporary.sync_all());
    if let Err(error) = write_result {
        let _ = fs::remove_file(&temporary_path);
        return Err(format!("write guardian status receipt: {error}"));
    }
    fs::rename(&temporary_path, &status_path).map_err(|error| {
        let _ = fs::remove_file(&temporary_path);
        format!("publish guardian status {}: {error}", status_path.display())
    })?;
    File::open(&status_dir)
        .and_then(|directory| directory.sync_all())
        .map_err(|error| format!("sync status directory {}: {error}", status_dir.display()))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn emergency_latch_blocks_healthy_work_until_rearm_is_acknowledged() {
        let threshold = 1_000;
        let reserve = 100;
        let mut latch = EmergencyLatch::new();

        assert_eq!(
            latch.next_action(threshold - 1, threshold, reserve),
            LoopAction::Emergency
        );
        assert!(latch.is_latched());
        assert_eq!(
            latch.next_action(threshold, threshold, reserve),
            LoopAction::Emergency,
            "the recovery band must remain emergency-only"
        );
        assert_eq!(
            latch.next_action(threshold + reserve as u64, threshold, reserve),
            LoopAction::Rearm
        );
        assert_eq!(
            latch.next_action(threshold + reserve as u64, threshold, reserve),
            LoopAction::Rearm,
            "selecting rearm must not clear the latch"
        );

        latch.acknowledge_rearmed();
        assert!(!latch.is_latched());
        assert_eq!(
            latch.next_action(threshold, threshold, reserve),
            LoopAction::Healthy
        );
    }
}
