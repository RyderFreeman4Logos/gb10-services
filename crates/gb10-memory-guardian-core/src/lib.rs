#![forbid(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]

//! Allocation-aware primitives for the GB10 memory guardian.
//!
//! Registration parsing and descriptor refresh happen while the host is
//! healthy. The emergency `kill_direct` path only releases the pre-touched
//! reserve and writes `1` to an already-open `cgroup.kill` descriptor.

use std::fmt;
use std::fs::File;
use std::os::fd::{AsRawFd, RawFd};
use std::ptr::NonNull;

pub const DEFAULT_RESERVE_BYTES: usize = 64 * 1024 * 1024;
pub const DEFAULT_THRESHOLD_BYTES: u64 = 1024 * 1024 * 1024;
pub const DEFAULT_RETRY_MILLIS: u64 = 5_000;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ParseError {
    MissingMemAvailable,
    DuplicateMemAvailable,
    MalformedMemAvailable,
    Overflow,
}

impl fmt::Display for ParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{self:?}")
    }
}

impl std::error::Error for ParseError {}

/// Parse the exact `MemAvailable: <integer> kB` field from `/proc/meminfo`.
pub fn parse_mem_available(meminfo: &[u8]) -> Result<u64, ParseError> {
    let mut found = None;
    for line in meminfo.split(|byte| *byte == b'\n') {
        let Some(rest) = line.strip_prefix(b"MemAvailable:") else {
            continue;
        };
        if found.is_some() {
            return Err(ParseError::DuplicateMemAvailable);
        }
        let rest = trim_ascii_whitespace(rest);
        let Some(split) = rest.iter().position(|byte| byte.is_ascii_whitespace()) else {
            return Err(ParseError::MalformedMemAvailable);
        };
        let (digits, unit) = rest.split_at(split);
        if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
            return Err(ParseError::MalformedMemAvailable);
        }
        if trim_ascii_whitespace(unit) != b"kB" {
            return Err(ParseError::MalformedMemAvailable);
        }
        let mut kib = 0_u64;
        for digit in digits {
            kib = kib
                .checked_mul(10)
                .and_then(|value| value.checked_add(u64::from(digit - b'0')))
                .ok_or(ParseError::Overflow)?;
        }
        found = Some(kib.checked_mul(1024).ok_or(ParseError::Overflow)?);
    }
    found.ok_or(ParseError::MissingMemAvailable)
}

fn trim_ascii_whitespace(mut bytes: &[u8]) -> &[u8] {
    while bytes.first().is_some_and(u8::is_ascii_whitespace) {
        bytes = &bytes[1..];
    }
    while bytes.last().is_some_and(u8::is_ascii_whitespace) {
        bytes = &bytes[..bytes.len() - 1];
    }
    bytes
}

pub fn should_shed(mem_available_bytes: u64, threshold_bytes: u64) -> bool {
    mem_available_bytes < threshold_bytes
}

pub fn should_rearm(mem_available_bytes: u64, threshold_bytes: u64, reserve_bytes: usize) -> bool {
    let reserve_bytes = u64::try_from(reserve_bytes).unwrap_or(u64::MAX);
    mem_available_bytes >= threshold_bytes.saturating_add(reserve_bytes)
}

#[derive(Debug)]
pub struct ReserveError;

impl fmt::Display for ReserveError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("unable to allocate emergency reserve")
    }
}

impl std::error::Error for ReserveError {}

#[derive(Debug)]
pub struct EmergencyReserve {
    data: Option<NonNull<u8>>,
    bytes: usize,
    page_size: usize,
    touched_pages: usize,
}

impl EmergencyReserve {
    pub fn new(bytes: usize) -> Result<Self, ReserveError> {
        // SAFETY: sysconf reads a process-wide immutable configuration value.
        let configured = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
        let page_size = usize::try_from(configured).map_err(|_| ReserveError)?;
        if page_size == 0 {
            return Err(ReserveError);
        }
        Self::with_page_size(bytes, page_size)
    }

    pub fn with_page_size(bytes: usize, page_size: usize) -> Result<Self, ReserveError> {
        if bytes == 0 || page_size == 0 {
            return Err(ReserveError);
        }
        // SAFETY: anonymous private mmap has no file backing; bytes is nonzero.
        let mapping = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                bytes,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
                -1,
                0,
            )
        };
        if mapping == libc::MAP_FAILED {
            return Err(ReserveError);
        }
        let data = match NonNull::new(mapping.cast::<u8>()) {
            Some(data) => data,
            None => {
                // SAFETY: mapping was returned by mmap for this exact length.
                unsafe { libc::munmap(mapping, bytes) };
                return Err(ReserveError);
            }
        };
        let mut touched_pages = 0;
        for offset in (0..bytes).step_by(page_size) {
            // SAFETY: mmap returned a writable `bytes`-long mapping and offset
            // is generated from the half-open range 0..bytes.
            unsafe { data.as_ptr().add(offset).write_volatile(1) };
            touched_pages += 1;
        }
        Ok(Self {
            data: Some(data),
            bytes,
            page_size,
            touched_pages,
        })
    }

    pub fn release(&mut self) {
        let Some(data) = self.data.take() else {
            return;
        };
        // SAFETY: data and bytes describe the still-owned mmap created above.
        let result = unsafe { libc::munmap(data.as_ptr().cast::<libc::c_void>(), self.bytes) };
        if result != 0 {
            // Keep ownership if the kernel refused to unmap, so a later retry
            // cannot silently forget resident emergency memory.
            self.data = Some(data);
        }
    }

    pub fn is_allocated(&self) -> bool {
        self.data.is_some()
    }

    pub fn len(&self) -> usize {
        self.data.as_ref().map_or(0, |_| self.bytes)
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn page_size(&self) -> usize {
        self.page_size
    }

    pub fn touched_pages(&self) -> usize {
        self.touched_pages
    }

    pub fn touched_byte(&self, offset: usize) -> Option<u8> {
        let data = self.data?;
        if offset >= self.bytes {
            return None;
        }
        // SAFETY: an allocated reserve owns a readable `bytes`-long mapping
        // and the bounds check above keeps offset inside it.
        Some(unsafe { data.as_ptr().add(offset).read_volatile() })
    }
}

impl Drop for EmergencyReserve {
    fn drop(&mut self) {
        self.release();
    }
}

mod emergency;

pub use emergency::{
    kill_direct, parse_registration, target_state_requires_disarm, AttemptOutcome, CgroupTarget,
    EmergencyController, GuardianError, GuardianOperation, KillError, RefreshStatus, Registration,
    RegistrationError, RegistrationGeneration, RegistrationManager, TargetState,
    REGISTRATION_VERSION,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MemInfoError {
    Read(i32),
    TooLarge,
    InvalidData,
}

impl fmt::Display for MemInfoError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(errno) => write!(formatter, "read failed with errno {errno}"),
            Self::TooLarge => formatter.write_str("/proc/meminfo exceeds fixed buffer"),
            Self::InvalidData => formatter.write_str("invalid /proc/meminfo payload"),
        }
    }
}

impl std::error::Error for MemInfoError {}

pub fn read_mem_available_fd(file: &File, buffer: &mut [u8]) -> Result<u64, MemInfoError> {
    let length = pread_once(file.as_raw_fd(), buffer)?;
    if length == buffer.len() {
        return Err(MemInfoError::TooLarge);
    }
    parse_mem_available(&buffer[..length]).map_err(|_| MemInfoError::InvalidData)
}

fn pread_once(fd: RawFd, buffer: &mut [u8]) -> Result<usize, MemInfoError> {
    for _ in 0..3 {
        // SAFETY: buffer is writable for buffer.len() bytes and fd remains
        // borrowed for the duration of the syscall.
        let read = unsafe {
            libc::pread(
                fd,
                buffer.as_mut_ptr().cast::<libc::c_void>(),
                buffer.len(),
                0,
            )
        };
        if read >= 0 {
            return Ok(read as usize);
        }
        let errno = last_errno();
        if errno != libc::EINTR {
            return Err(MemInfoError::Read(errno));
        }
    }
    Err(MemInfoError::Read(libc::EINTR))
}

fn last_errno() -> i32 {
    // SAFETY: libc exposes a thread-local errno pointer on Linux.
    unsafe { *libc::__errno_location() }
}

pub fn effective_uid() -> u32 {
    // SAFETY: geteuid has no arguments and returns the caller's effective uid.
    unsafe { libc::geteuid() }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;
    use std::fs;
    use std::io;
    use std::os::fd::OwnedFd;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    const UID: u32 = 1001;

    struct FakeTree {
        root: PathBuf,
        registration: PathBuf,
    }

    impl FakeTree {
        fn new() -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock before epoch")
                .as_nanos();
            let root = std::env::temp_dir().join(format!(
                "gb10-memory-guardian-test-{}-{nonce}",
                std::process::id()
            ));
            fs::create_dir_all(&root).expect("create fake root");
            let registration = root.join("target-cgroup.v1");
            Self { root, registration }
        }

        fn cgroup_path(&self, id: &str) -> PathBuf {
            self.root.join(format!(
                "user.slice/user-{UID}.slice/user@{UID}.service/app.slice/docker-{id}.scope"
            ))
        }

        fn add_target(&self, id: &str, populated: &str) {
            let path = self.cgroup_path(id);
            fs::create_dir_all(&path).expect("create target");
            fs::write(path.join("cgroup.kill"), b"").expect("create kill");
            fs::write(
                path.join("cgroup.events"),
                format!("populated {populated}\nfrozen 0\n"),
            )
            .expect("create events");
        }

        fn registration_text(&self, id: &str) -> String {
            format!(
                "version=1\ncontainer_id={id}\nscope=docker-{id}.scope\ncontrol_group=/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/docker-{id}.scope\n"
            )
        }

        fn publish(&self, id: &str) {
            let temporary = self.root.join("registration.tmp");
            fs::write(&temporary, self.registration_text(id)).expect("write temp registration");
            fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))
                .expect("chmod temp registration");
            fs::rename(temporary, &self.registration).expect("publish registration");
        }
    }

    impl Drop for FakeTree {
        fn drop(&mut self) {
            let _ignored = fs::remove_dir_all(&self.root);
        }
    }

    fn id(byte: char) -> String {
        std::iter::repeat_n(byte, 64).collect()
    }

    #[test]
    fn parses_mem_available_and_uses_strict_boundary() {
        let exact = parse_mem_available(b"MemTotal: 8 kB\nMemAvailable: 1048576 kB\n")
            .expect("parse exact threshold");
        assert_eq!(exact, DEFAULT_THRESHOLD_BYTES);
        assert!(!should_shed(exact, DEFAULT_THRESHOLD_BYTES));
        assert!(!should_rearm(
            DEFAULT_THRESHOLD_BYTES + DEFAULT_RESERVE_BYTES as u64 - 1,
            DEFAULT_THRESHOLD_BYTES,
            DEFAULT_RESERVE_BYTES,
        ));
        assert!(should_rearm(
            DEFAULT_THRESHOLD_BYTES + DEFAULT_RESERVE_BYTES as u64,
            DEFAULT_THRESHOLD_BYTES,
            DEFAULT_RESERVE_BYTES,
        ));

        let below = parse_mem_available(b"MemAvailable: 1048575 kB\n").expect("parse below");
        assert!(should_shed(below, DEFAULT_THRESHOLD_BYTES));
        assert_eq!(
            parse_mem_available(b"MemAvailable: 1 MB\n"),
            Err(ParseError::MalformedMemAvailable)
        );
        assert_eq!(
            parse_mem_available(b"MemAvailable: 1 kB\nMemAvailable: 2 kB\n"),
            Err(ParseError::DuplicateMemAvailable)
        );
    }

    #[test]
    fn reserve_touches_each_page_and_releases() {
        let mut reserve = EmergencyReserve::with_page_size(10_000, 4096).expect("reserve");
        assert_eq!(reserve.page_size(), 4096);
        assert_eq!(reserve.touched_pages(), 3);
        assert_eq!(reserve.touched_byte(0), Some(1));
        assert_eq!(reserve.touched_byte(4096), Some(1));
        assert_eq!(reserve.touched_byte(8192), Some(1));
        reserve.release();
        assert!(!reserve.is_allocated());
        assert!(reserve.is_empty());
    }

    #[test]
    fn reserve_release_returns_mapping_to_the_kernel() {
        let mut reserve = EmergencyReserve::with_page_size(64 * 1024 * 1024, 4096).unwrap();
        let address = reserve.data.as_ref().unwrap().as_ptr();
        let page_address = ((address as usize) & !(reserve.page_size() - 1)) as *mut libc::c_void;
        reserve.release();

        let mut residency = 0_u8;
        // SAFETY: mincore accepts an unmapped page-aligned address for the
        // explicit purpose of reporting ENOMEM; it does not dereference it.
        let result = unsafe { libc::mincore(page_address, 4096, &mut residency) };
        assert_eq!(result, -1, "released reserve remained mapped");
        assert_eq!(
            io::Error::last_os_error().raw_os_error(),
            Some(libc::ENOMEM)
        );
    }

    #[test]
    fn controller_can_rearm_reserve_only_when_requested() {
        let reserve = EmergencyReserve::with_page_size(4096, 4096).unwrap();
        let mut controller = EmergencyController::new(reserve, DEFAULT_RETRY_MILLIS);
        assert!(!controller.ensure_reserve(4096).unwrap());
        controller.reserve.release();
        assert!(controller.ensure_reserve(4096).unwrap());
        assert!(controller.reserve().is_allocated());
    }

    #[test]
    fn entering_emergency_releases_reserve_even_without_a_target() {
        let reserve = EmergencyReserve::with_page_size(4096, 4096).unwrap();
        let mut controller = EmergencyController::new(reserve, DEFAULT_RETRY_MILLIS);
        controller.enter_emergency();
        assert!(!controller.reserve().is_allocated());
    }

    #[test]
    fn registration_fifo_is_rejected_without_blocking() {
        let tree = FakeTree::new();
        tree.add_target(&id('a'), "1");
        let path = CString::new(tree.registration.as_os_str().as_bytes()).expect("fifo path");
        // SAFETY: path is a valid NUL-terminated pathname and mode is bounded.
        let created = unsafe { libc::mkfifo(path.as_ptr(), 0o600) };
        assert_eq!(created, 0, "create registration fifo");

        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        assert!(matches!(
            manager.refresh(),
            Err(GuardianError::InvalidRegistrationMetadata)
        ));
        assert!(manager.target().is_none());
    }

    #[test]
    fn registration_rejects_insecure_file_mode() {
        let tree = FakeTree::new();
        let target_id = id('a');
        tree.add_target(&target_id, "1");
        tree.publish(&target_id);
        fs::set_permissions(&tree.registration, fs::Permissions::from_mode(0o4600))
            .expect("add forbidden set-id mode bit");

        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        assert!(manager.refresh().is_err());
        assert!(manager.target().is_none());
    }

    #[test]
    fn missing_registration_disarms_the_retained_target() {
        let tree = FakeTree::new();
        let target_id = id('a');
        tree.add_target(&target_id, "1");
        tree.publish(&target_id);

        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        manager.refresh().expect("arm target");
        fs::remove_file(&tree.registration).expect("remove registration");
        assert!(manager.refresh().is_err());
        assert!(
            manager.target().is_none(),
            "ENOENT must disarm stale target"
        );
    }

    #[test]
    fn full_refresh_keeps_armed_target_on_same_generation_identity_error() {
        let tree = FakeTree::new();
        let target_id = id('a');
        tree.add_target(&target_id, "1");
        tree.publish(&target_id);

        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        manager.refresh().expect("arm target");

        let bad_app_slice: OwnedFd = fs::File::open("/dev/null").expect("open fault fd").into();
        manager.target.as_mut().expect("armed").app_slice = bad_app_slice;
        fs::rename(
            tree.root.join("user.slice"),
            tree.root.join("user.slice.off"),
        )
        .expect("make cgroup reopen fail");

        let error = manager.refresh().expect_err("identity check must fail");
        assert!(matches!(error, GuardianError::Io { .. }));
        assert!(
            manager.target().is_some(),
            "same-generation identity I/O failure must retain armed kill fd"
        );
    }

    #[test]
    fn transient_target_state_error_does_not_require_disarm() {
        let transient = Err(GuardianError::Io {
            operation: GuardianOperation::StatScope,
            errno: libc::EIO,
        });
        assert!(!target_state_requires_disarm(&transient));
        assert!(target_state_requires_disarm(&Ok(TargetState::Gone)));
        assert!(target_state_requires_disarm(&Ok(TargetState::Replaced)));
        assert!(target_state_requires_disarm(&Ok(TargetState::Empty)));
        assert!(!target_state_requires_disarm(&Ok(TargetState::Populated)));
    }

    #[test]
    fn registration_accepts_only_exact_rootless_docker_path() {
        let good_id = id('a');
        let tree = FakeTree::new();
        assert!(parse_registration(tree.registration_text(&good_id).as_bytes(), UID).is_ok());

        let invalid = [
            tree.registration_text(&good_id)
                .replace("version=1", "version=2"),
            tree.registration_text(&id('A')),
            tree.registration_text(&"a".repeat(63)),
            tree.registration_text(&good_id)
                .replace("app.slice", "../app.slice"),
            tree.registration_text(&good_id)
                .replace("user-1001", "user-1002"),
            tree.registration_text(&good_id)
                .replace("app.slice/", "app.slice/extra/"),
            tree.registration_text(&good_id)
                .replace("docker-a", "docker-A"),
            tree.registration_text(&good_id)
                .replace("scope=docker", "scope=other"),
            format!("{}unknown=value\n", tree.registration_text(&good_id)),
        ];
        for registration in invalid {
            assert!(
                parse_registration(registration.as_bytes(), UID).is_err(),
                "{registration}"
            );
        }
    }

    #[test]
    fn fake_cgroup_kills_only_registered_target() {
        let tree = FakeTree::new();
        let selected = id('a');
        let protected_one = id('b');
        let protected_two = id('c');
        for target in [&selected, &protected_one, &protected_two] {
            tree.add_target(target, "1");
        }
        tree.publish(&selected);
        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        manager.refresh().expect("refresh");
        let mut reserve = EmergencyReserve::with_page_size(8192, 4096).expect("reserve");
        kill_direct(&mut reserve, manager.target().expect("target")).expect("kill direct");

        assert_eq!(
            fs::read(tree.cgroup_path(&selected).join("cgroup.kill")).unwrap(),
            b"1"
        );
        assert!(
            fs::read(tree.cgroup_path(&protected_one).join("cgroup.kill"))
                .unwrap()
                .is_empty()
        );
        assert!(
            fs::read(tree.cgroup_path(&protected_two).join("cgroup.kill"))
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn rejects_symlinked_components_and_files() {
        let tree = FakeTree::new();
        let target = id('a');
        tree.add_target(&target, "1");
        tree.publish(&target);
        let path = tree.cgroup_path(&target);
        fs::remove_file(path.join("cgroup.kill")).expect("remove kill");
        symlink("/dev/null", path.join("cgroup.kill")).expect("symlink kill");
        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        assert!(manager.refresh().is_err());
        assert!(manager.target().is_none());

        fs::remove_file(&tree.registration).expect("remove registration");
        symlink(path.join("cgroup.events"), &tree.registration).expect("symlink registration");
        assert!(manager.refresh().is_err());
        assert!(manager.target().is_none());
    }

    #[test]
    fn rejects_symlinked_cgroup_directory_component() {
        let tree = FakeTree::new();
        let target = id('a');
        let service_directory = tree
            .root
            .join(format!("user.slice/user-{UID}.slice/user@{UID}.service"));
        let outside = tree.root.join("outside-app.slice");
        fs::create_dir_all(outside.join(format!("docker-{target}.scope"))).unwrap();
        fs::create_dir_all(&service_directory).unwrap();
        symlink(&outside, service_directory.join("app.slice")).unwrap();
        tree.publish(&target);

        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        assert!(manager.refresh().is_err());
        assert!(manager.target().is_none());
    }

    #[test]
    fn atomic_registration_replacement_changes_target() {
        let tree = FakeTree::new();
        let first = id('a');
        let second = id('b');
        tree.add_target(&first, "1");
        tree.add_target(&second, "1");
        tree.publish(&first);
        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        assert_eq!(manager.refresh().unwrap(), RefreshStatus::Replaced);
        tree.publish(&second);
        assert_eq!(manager.refresh().unwrap(), RefreshStatus::Replaced);

        let mut reserve = EmergencyReserve::with_page_size(4096, 4096).unwrap();
        kill_direct(&mut reserve, manager.target().unwrap()).unwrap();
        assert!(fs::read(tree.cgroup_path(&first).join("cgroup.kill"))
            .unwrap()
            .is_empty());
        assert_eq!(
            fs::read(tree.cgroup_path(&second).join("cgroup.kill")).unwrap(),
            b"1"
        );
    }

    #[test]
    fn changed_bad_registration_clears_old_target() {
        let tree = FakeTree::new();
        let target = id('a');
        tree.add_target(&target, "1");
        tree.publish(&target);
        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        manager.refresh().unwrap();
        assert!(manager.target().is_some());

        let temporary = tree.root.join("bad.tmp");
        fs::write(&temporary, b"version=1\ncontainer_id=../../target\n").unwrap();
        fs::rename(temporary, &tree.registration).unwrap();
        assert!(manager.refresh().is_err());
        assert!(manager.target().is_none());
    }

    #[test]
    fn retries_on_five_second_clock_until_empty() {
        let tree = FakeTree::new();
        let target = id('a');
        tree.add_target(&target, "1");
        tree.publish(&target);
        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        manager.refresh().unwrap();
        let reserve = EmergencyReserve::with_page_size(4096, 4096).unwrap();
        let mut controller = EmergencyController::new(reserve, DEFAULT_RETRY_MILLIS);

        assert!(matches!(
            controller.attempt(1_000, manager.target().unwrap()),
            AttemptOutcome::Retry { .. }
        ));
        assert_eq!(
            controller.attempt(5_999, manager.target().unwrap()),
            AttemptOutcome::Waiting
        );
        fs::write(
            tree.cgroup_path(&target).join("cgroup.events"),
            b"populated 0\nfrozen 0\n",
        )
        .unwrap();
        assert!(matches!(
            controller.attempt(6_000, manager.target().unwrap()),
            AttemptOutcome::Verified {
                observed: TargetState::Empty,
                ..
            }
        ));
    }

    #[test]
    fn already_gone_verifies_but_recreated_target_requires_refresh() {
        let tree = FakeTree::new();
        let target = id('a');
        tree.add_target(&target, "1");
        tree.publish(&target);
        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        manager.refresh().unwrap();

        let path = tree.cgroup_path(&target);
        fs::remove_file(path.join("cgroup.kill")).unwrap();
        fs::remove_file(path.join("cgroup.events")).unwrap();
        fs::remove_dir(&path).unwrap();
        let reserve = EmergencyReserve::with_page_size(4096, 4096).unwrap();
        let mut controller = EmergencyController::new(reserve, DEFAULT_RETRY_MILLIS);
        assert!(matches!(
            controller.attempt(0, manager.target().unwrap()),
            AttemptOutcome::Verified {
                observed: TargetState::Gone,
                ..
            }
        ));

        tree.add_target(&target, "1");
        assert_eq!(
            manager.target().unwrap().state().unwrap(),
            TargetState::Replaced
        );
        assert_eq!(manager.refresh().unwrap(), RefreshStatus::Replaced);
        controller.reset_for_target_generation();
        assert!(matches!(
            controller.attempt(1, manager.target().unwrap()),
            AttemptOutcome::Retry {
                observed: Some(TargetState::Populated),
                ..
            }
        ));
    }

    #[test]
    fn stale_and_malformed_events_never_arm_or_verify() {
        let tree = FakeTree::new();
        let target = id('a');
        tree.add_target(&target, "0");
        tree.publish(&target);
        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        assert!(matches!(
            manager.refresh(),
            Err(GuardianError::StaleRegistration)
        ));
        assert!(manager.target().is_none());

        fs::write(
            tree.cgroup_path(&target).join("cgroup.events"),
            b"populated maybe\n",
        )
        .unwrap();
        assert!(manager.refresh().is_err());
        assert!(manager.target().is_none());
    }
}
