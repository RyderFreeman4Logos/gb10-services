use libc::{SIGKILL, SIGTERM};
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const PROCESS_POLL_INTERVAL: Duration = Duration::from_millis(50);
const PROCESS_TERMINATION_GRACE: Duration = Duration::from_secs(2);
const MAX_UNIT_NAME_BYTES: usize = 128;

#[derive(Debug, Default)]
pub(crate) struct EscalationEpisode {
    first_action: Option<Instant>,
    triggered: bool,
}

impl EscalationEpisode {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    pub(crate) fn after_emergency_iteration(
        &mut self,
        now: Instant,
        action_taken: bool,
        enabled: bool,
        grace: Duration,
    ) -> bool {
        if !enabled {
            return false;
        }
        if action_taken && self.first_action.is_none() {
            self.first_action = Some(now);
        }
        let Some(first_action) = self.first_action else {
            return false;
        };
        if self.triggered || now.saturating_duration_since(first_action) <= grace {
            return false;
        }
        self.triggered = true;
        true
    }

    pub(crate) fn reset(&mut self) {
        self.first_action = None;
        self.triggered = false;
    }

    #[cfg(test)]
    fn is_active(&self) -> bool {
        self.first_action.is_some()
    }

    #[cfg(test)]
    fn was_triggered(&self) -> bool {
        self.triggered
    }
}

pub(crate) fn validate_unit_name(unit: &str) -> Result<(), String> {
    let bytes = unit.as_bytes();
    let valid = !bytes.is_empty()
        && bytes.len() <= MAX_UNIT_NAME_BYTES
        && unit.ends_with(".service")
        && bytes[0].is_ascii_alphanumeric()
        && bytes.iter().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'@' | b':')
        });
    if !valid {
        return Err("escalation unit must be a safe 1-128 byte systemd .service name".to_owned());
    }
    Ok(())
}

pub(crate) fn start_unit_with_timeout(unit: &str, timeout: Duration) -> Result<(), String> {
    validate_unit_name(unit)?;
    let mut command = Command::new("systemctl");
    command
        .args(["--user", "start", "--"])
        .arg(unit)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // SAFETY: the child-side closure invokes only setsid and reads errno, both
    // valid between fork and exec. It captures no state and performs no IO.
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let child = command
        .spawn()
        .map_err(|error| format!("spawn systemctl for {unit}: {error}"))?;
    let status = BoundedChild::new(child)?.wait_with_timeout(timeout)?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("systemctl start {unit} exited with {status}"))
    }
}

struct BoundedChild {
    child: Option<Child>,
    process_group: i32,
}

impl BoundedChild {
    fn new(mut child: Child) -> Result<Self, String> {
        let process_group = match i32::try_from(child.id()) {
            Ok(process_group) => process_group,
            Err(_) => {
                let kill_error = child.kill().err();
                let wait_error = child.wait().err();
                return Err(format!(
                    "systemctl child PID exceeds pid_t; kill={kill_error:?}; reap={wait_error:?}"
                ));
            }
        };
        Ok(Self {
            child: Some(child),
            process_group,
        })
    }

    fn wait_with_timeout(mut self, timeout: Duration) -> Result<ExitStatus, String> {
        let deadline = Instant::now()
            .checked_add(timeout)
            .ok_or_else(|| "systemctl timeout deadline overflowed".to_owned())?;
        loop {
            let status = self
                .child
                .as_mut()
                .ok_or_else(|| "systemctl child was already reaped".to_owned())?
                .try_wait()
                .map_err(|error| format!("poll systemctl child: {error}"))?;
            if let Some(status) = status {
                self.child = None;
                return Ok(status);
            }

            let now = Instant::now();
            if now >= deadline {
                let cleanup_error = self.kill_and_reap().err();
                return Err(match cleanup_error {
                    Some(error) => format!(
                        "systemctl start timed out after {}s; cleanup failed: {error}",
                        timeout.as_secs()
                    ),
                    None => format!(
                        "systemctl start timed out after {}s and was killed",
                        timeout.as_secs()
                    ),
                });
            }
            thread::sleep(PROCESS_POLL_INTERVAL.min(deadline.saturating_duration_since(now)));
        }
    }

    fn kill_and_reap(&mut self) -> Result<(), String> {
        let Some(child) = self.child.as_mut() else {
            return Ok(());
        };
        // SAFETY: setsid made the retained child PID its process-group ID. The
        // unreaped Child keeps that PID from being recycled before this call.
        let group_term_result = unsafe { libc::kill(-self.process_group, SIGTERM) };
        let group_term_error = if group_term_result == 0 {
            let started = Instant::now();
            let deadline = started
                .checked_add(PROCESS_TERMINATION_GRACE)
                .unwrap_or(started);
            loop {
                let now = Instant::now();
                if now >= deadline {
                    break;
                }
                // SAFETY: the unreaped process-group leader remains the stable
                // identity anchor; signal 0 only probes group existence.
                let probe_result = unsafe { libc::kill(-self.process_group, 0) };
                if probe_result != 0
                    && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
                {
                    break;
                }
                thread::sleep(PROCESS_POLL_INTERVAL.min(deadline.saturating_duration_since(now)));
            }
            None
        } else {
            let error = std::io::Error::last_os_error();
            (error.raw_os_error() != Some(libc::ESRCH)).then_some(error)
        };

        // SAFETY: the child remains unreaped throughout the bounded TERM grace,
        // so its PID still identifies the process group created by setsid.
        let group_kill_result = unsafe { libc::kill(-self.process_group, SIGKILL) };
        let group_kill_error = if group_kill_result == 0 {
            None
        } else {
            let error = std::io::Error::last_os_error();
            (error.raw_os_error() != Some(libc::ESRCH)).then_some(error)
        };
        let direct_kill_error = if group_kill_error.is_some() {
            child.kill().err()
        } else {
            None
        };
        let wait_error = child.wait().err();
        self.child = None;
        match (
            group_term_error,
            group_kill_error,
            direct_kill_error,
            wait_error,
        ) {
            (None, None, None, None) => Ok(()),
            (term, kill, direct, wait) => Err(format!(
                "clean up systemctl process group: group_term={term:?}; group_kill={kill:?}; direct_kill={direct:?}; reap={wait:?}"
            )),
        }
    }
}

impl Drop for BoundedChild {
    fn drop(&mut self) {
        let _ = self.kill_and_reap();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const GRACE: Duration = Duration::from_secs(60);

    #[test]
    fn escalation_triggers_after_grace_when_memory_stays_low() {
        let started = Instant::now();
        let mut episode = EscalationEpisode::new();

        assert!(!episode.after_emergency_iteration(started, true, true, GRACE));
        assert!(!episode.after_emergency_iteration(started + GRACE, false, true, GRACE));
        assert!(episode.after_emergency_iteration(
            started + GRACE + Duration::from_secs(1),
            false,
            true,
            GRACE
        ));
    }

    #[test]
    fn escalation_does_not_trigger_when_memory_recovers_within_grace() {
        let started = Instant::now();
        let mut episode = EscalationEpisode::new();

        assert!(!episode.after_emergency_iteration(started, true, true, GRACE));
        episode.reset();

        assert!(!episode.after_emergency_iteration(
            started + GRACE + Duration::from_secs(1),
            false,
            true,
            GRACE
        ));
        assert!(!episode.is_active());
        assert!(!episode.was_triggered());
    }

    #[test]
    fn escalation_triggers_at_most_once_per_episode() {
        let started = Instant::now();
        let mut episode = EscalationEpisode::new();

        assert!(!episode.after_emergency_iteration(started, true, true, GRACE));
        assert!(episode.after_emergency_iteration(
            started + GRACE + Duration::from_secs(1),
            false,
            true,
            GRACE
        ));
        assert!(!episode.after_emergency_iteration(
            started + GRACE + Duration::from_secs(2),
            true,
            true,
            GRACE
        ));
        assert!(episode.was_triggered());
    }

    #[test]
    fn escalation_resets_after_rearm_for_a_new_episode() {
        let first = Instant::now();
        let mut episode = EscalationEpisode::new();

        assert!(!episode.after_emergency_iteration(first, true, true, GRACE));
        assert!(episode.after_emergency_iteration(
            first + GRACE + Duration::from_secs(1),
            false,
            true,
            GRACE
        ));
        episode.reset();

        let second = first + Duration::from_secs(120);
        assert!(!episode.after_emergency_iteration(second, true, true, GRACE));
        assert!(episode.after_emergency_iteration(
            second + GRACE + Duration::from_secs(1),
            false,
            true,
            GRACE
        ));
    }

    #[test]
    fn escalation_can_be_disabled() {
        let started = Instant::now();
        let mut episode = EscalationEpisode::new();

        assert!(!episode.after_emergency_iteration(started, true, false, GRACE));
        assert!(!episode.after_emergency_iteration(
            started + Duration::from_secs(600),
            true,
            false,
            GRACE
        ));
        assert!(!episode.is_active());
        assert!(!episode.was_triggered());
    }

    #[test]
    fn escalation_unit_name_is_restricted_to_safe_service_names() {
        assert!(validate_unit_name("gb10-stack-recovery.service").is_ok());
        for invalid in [
            "",
            "--system.service",
            "gb10-stack-recovery.timer",
            "../gb10-stack-recovery.service",
            "unit with spaces.service",
        ] {
            assert!(validate_unit_name(invalid).is_err(), "accepted {invalid}");
        }
    }
}
