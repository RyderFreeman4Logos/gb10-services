use crate::{EmergencyReserve, ReserveError};
use std::ffi::{CStr, OsStr};
use std::fmt;
use std::fs::OpenOptions;
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

pub const REGISTRATION_VERSION: &str = "1";
const REGISTRATION_LIMIT: usize = 2_048;
const EVENTS_LIMIT: usize = 512;
const NAME_LIMIT: usize = 256;
const SCOPE_LIMIT: usize = 96;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Registration {
    pub container_id: String,
    pub scope: String,
    pub control_group: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RegistrationError {
    InvalidUtf8,
    MalformedLine,
    DuplicateField,
    UnknownField,
    MissingField,
    WrongVersion,
    InvalidContainerId,
    InvalidScope,
    InvalidControlGroup,
    TooLarge,
}

impl fmt::Display for RegistrationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{self:?}")
    }
}

impl std::error::Error for RegistrationError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardianOperation {
    PrepareRegistrationParent,
    PrepareCgroupAuthority,
    OpenRegistration,
    StatRegistration,
    ReadRegistration,
    ConfirmRegistration,
    DuplicateAuthority,
    OpenScope,
    StatScope,
    OpenKill,
    OpenEvents,
    ReadEvents,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardianError {
    Registration(RegistrationError),
    Io {
        operation: GuardianOperation,
        errno: i32,
    },
    InvalidRegistrationPath,
    InvalidRegistrationMetadata,
    InvalidEvents,
    ChangedDuringRefresh,
    StaleRegistration,
}

impl GuardianError {
    fn from_io(operation: GuardianOperation, error: &io::Error) -> Self {
        Self::Io {
            operation,
            errno: error.raw_os_error().unwrap_or(libc::EIO),
        }
    }

    fn errno(self) -> Option<i32> {
        match self {
            Self::Io { errno, .. } => Some(errno),
            _ => None,
        }
    }
}

impl fmt::Display for GuardianError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{self:?}")
    }
}

impl std::error::Error for GuardianError {}

impl From<RegistrationError> for GuardianError {
    fn from(error: RegistrationError) -> Self {
        Self::Registration(error)
    }
}

mod registration;

pub use registration::{
    parse_registration, RefreshStatus, RegistrationGeneration, RegistrationManager,
};

fn decimal_bytes(value: u32, buffer: &mut [u8; 10]) -> &[u8] {
    let mut value = value;
    let mut start = buffer.len();
    loop {
        start -= 1;
        buffer[start] = b'0' + (value % 10) as u8;
        value /= 10;
        if value == 0 {
            return &buffer[start..];
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FixedName<const N: usize> {
    bytes: [u8; N],
    length: usize,
}

impl<const N: usize> FixedName<N> {
    fn new(bytes: &[u8]) -> Result<Self, GuardianError> {
        if bytes.is_empty() || bytes.contains(&0) || bytes.len() >= N {
            return Err(GuardianError::InvalidRegistrationPath);
        }
        let mut fixed = Self {
            bytes: [0; N],
            length: bytes.len(),
        };
        fixed.bytes[..bytes.len()].copy_from_slice(bytes);
        Ok(fixed)
    }

    fn from_os_str(value: &OsStr) -> Result<Self, GuardianError> {
        Self::new(value.as_bytes())
    }

    fn as_bytes(&self) -> &[u8] {
        &self.bytes[..self.length]
    }

    fn as_c_str(&self) -> &CStr {
        // SAFETY: new rejects interior NULs, length is below N, and the
        // zero-initialized byte immediately after length remains the terminator.
        unsafe { CStr::from_bytes_with_nul_unchecked(&self.bytes[..=self.length]) }
    }

    fn as_str(&self) -> io::Result<&str> {
        std::str::from_utf8(self.as_bytes()).map_err(|_| io::Error::from_raw_os_error(libc::EILSEQ))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetState {
    Populated,
    Empty,
    Gone,
    Replaced,
}

pub fn target_state_requires_disarm(state: &Result<TargetState, GuardianError>) -> bool {
    matches!(
        state,
        Ok(TargetState::Empty | TargetState::Gone | TargetState::Replaced)
    )
}

#[derive(Debug)]
pub struct CgroupTarget {
    pub(crate) app_slice: OwnedFd,
    scope: OwnedFd,
    kill: OwnedFd,
    events: OwnedFd,
    scope_name: FixedName<SCOPE_LIMIT>,
}

impl CgroupTarget {
    fn open_registered(
        app_slice: &OwnedFd,
        scope_name: FixedName<SCOPE_LIMIT>,
    ) -> Result<Self, GuardianError> {
        let retained_app_slice = duplicate_fd(app_slice.as_raw_fd())?;
        Self::open_components(retained_app_slice, scope_name)
    }

    /// Open only the rigid disposable canary unit. No caller-provided cgroup
    /// path or unit name is accepted by this mode.
    pub fn open_disposable_canary(root_path: &Path, uid: u32) -> io::Result<Self> {
        let app_slice = open_app_slice_io(root_path, uid)?;
        let scope_name = FixedName::new(b"gb10-memory-guardian-disposable-canary.service")
            .map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
        Self::open_components(app_slice, scope_name).map_err(guardian_as_io)
    }

    fn open_components(
        app_slice: OwnedFd,
        scope_name: FixedName<SCOPE_LIMIT>,
    ) -> Result<Self, GuardianError> {
        let scope = open_directory_at(
            app_slice.as_raw_fd(),
            scope_name.as_c_str(),
            GuardianOperation::OpenScope,
        )?;
        let kill = open_file_at(
            scope.as_raw_fd(),
            c"cgroup.kill",
            libc::O_WRONLY,
            GuardianOperation::OpenKill,
        )?;
        let events = open_file_at(
            scope.as_raw_fd(),
            c"cgroup.events",
            libc::O_RDONLY,
            GuardianOperation::OpenEvents,
        )?;
        Ok(Self {
            app_slice,
            scope,
            kill,
            events,
            scope_name,
        })
    }

    pub fn state(&self) -> Result<TargetState, GuardianError> {
        match self.identity_state()? {
            TargetState::Populated => {}
            state => return Ok(state),
        }
        let mut buffer = [0_u8; EVENTS_LIMIT];
        let length = pread_once(
            self.events.as_raw_fd(),
            &mut buffer,
            GuardianOperation::ReadEvents,
        )?;
        if length == buffer.len() {
            return Err(GuardianError::InvalidEvents);
        }
        let mut populated = None;
        for line in buffer[..length].split(|byte| *byte == b'\n') {
            let Some(value) = line.strip_prefix(b"populated ") else {
                continue;
            };
            if populated.is_some() {
                return Err(GuardianError::InvalidEvents);
            }
            populated = match value {
                b"0" => Some(false),
                b"1" => Some(true),
                _ => return Err(GuardianError::InvalidEvents),
            };
        }
        match populated {
            Some(true) => Ok(TargetState::Populated),
            Some(false) => Ok(TargetState::Empty),
            None => Err(GuardianError::InvalidEvents),
        }
    }

    fn identity_state(&self) -> Result<TargetState, GuardianError> {
        let opened = stat_fd(self.scope.as_raw_fd(), GuardianOperation::StatScope)?;
        match stat_at(
            self.app_slice.as_raw_fd(),
            self.scope_name.as_c_str(),
            GuardianOperation::StatScope,
        ) {
            Ok(current) => {
                if opened.st_dev != current.st_dev || opened.st_ino != current.st_ino {
                    Ok(TargetState::Replaced)
                } else {
                    Ok(TargetState::Populated)
                }
            }
            Err(GuardianError::Io {
                errno: libc::ENOENT,
                ..
            }) => Ok(TargetState::Gone),
            Err(error) => Err(error),
        }
    }

    pub fn scope_name(&self) -> io::Result<&str> {
        self.scope_name.as_str()
    }

    pub fn generation_identity(&self) -> io::Result<(u64, u64)> {
        let stat = raw_fstat(self.scope.as_raw_fd())?;
        Ok((stat.st_dev, stat.st_ino))
    }
}

fn open_app_slice(root_path: &Path, uid: u32) -> Result<OwnedFd, GuardianError> {
    let root = open_directory_path(root_path, GuardianOperation::PrepareCgroupAuthority)?;
    let user_slice = open_directory_at(
        root.as_raw_fd(),
        c"user.slice",
        GuardianOperation::PrepareCgroupAuthority,
    )?;
    let mut component = [0_u8; 32];
    let user_uid = uid_component(b"user-", uid, b".slice", &mut component)?;
    let user_uid_slice = open_directory_at(
        user_slice.as_raw_fd(),
        user_uid,
        GuardianOperation::PrepareCgroupAuthority,
    )?;
    let user_service = uid_component(b"user@", uid, b".service", &mut component)?;
    let service = open_directory_at(
        user_uid_slice.as_raw_fd(),
        user_service,
        GuardianOperation::PrepareCgroupAuthority,
    )?;
    open_directory_at(
        service.as_raw_fd(),
        c"app.slice",
        GuardianOperation::PrepareCgroupAuthority,
    )
}

fn open_app_slice_io(root_path: &Path, uid: u32) -> io::Result<OwnedFd> {
    open_app_slice(root_path, uid).map_err(guardian_as_io)
}

fn uid_component<'a>(
    prefix: &[u8],
    uid: u32,
    suffix: &[u8],
    buffer: &'a mut [u8; 32],
) -> Result<&'a CStr, GuardianError> {
    let mut digits = [0_u8; 10];
    let uid_digits = decimal_bytes(uid, &mut digits);
    let length = prefix.len() + uid_digits.len() + suffix.len();
    if length >= buffer.len() {
        return Err(GuardianError::InvalidRegistrationPath);
    }
    buffer.fill(0);
    buffer[..prefix.len()].copy_from_slice(prefix);
    buffer[prefix.len()..prefix.len() + uid_digits.len()].copy_from_slice(uid_digits);
    buffer[prefix.len() + uid_digits.len()..length].copy_from_slice(suffix);
    // SAFETY: the copied parts contain no NUL and fill left a zero terminator.
    Ok(unsafe { CStr::from_bytes_with_nul_unchecked(&buffer[..=length]) })
}

fn registration_metadata_valid(stat: &libc::stat, uid: u32) -> bool {
    stat.st_mode & libc::S_IFMT == libc::S_IFREG
        && stat.st_nlink == 1
        && stat.st_uid == uid
        && stat.st_mode & 0o7777 == 0o600
}

fn registration_error_disarms(errno: i32) -> bool {
    matches!(
        errno,
        libc::ENOENT | libc::ENOTDIR | libc::ELOOP | libc::EACCES | libc::EPERM
    )
}

fn open_directory_path(
    path: &Path,
    operation: GuardianOperation,
) -> Result<OwnedFd, GuardianError> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .map_err(|error| GuardianError::from_io(operation, &error))?;
    Ok(file.into())
}

fn open_directory_at(
    parent: RawFd,
    name: &CStr,
    operation: GuardianOperation,
) -> Result<OwnedFd, GuardianError> {
    open_at(
        parent,
        name,
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        operation,
    )
}

fn open_file_at(
    parent: RawFd,
    name: &CStr,
    access: i32,
    operation: GuardianOperation,
) -> Result<OwnedFd, GuardianError> {
    open_at(
        parent,
        name,
        access | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
        operation,
    )
}

fn open_at(
    parent: RawFd,
    name: &CStr,
    flags: i32,
    operation: GuardianOperation,
) -> Result<OwnedFd, GuardianError> {
    // SAFETY: parent is a retained directory fd and name is NUL-terminated.
    let fd = unsafe { libc::openat(parent, name.as_ptr(), flags) };
    if fd < 0 {
        let error = io::Error::last_os_error();
        return Err(GuardianError::from_io(operation, &error));
    }
    // SAFETY: successful openat returns a newly owned descriptor.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn duplicate_fd(fd: RawFd) -> Result<OwnedFd, GuardianError> {
    // SAFETY: fd is a live retained descriptor and fcntl does not borrow memory.
    let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
    if duplicate < 0 {
        let error = io::Error::last_os_error();
        return Err(GuardianError::from_io(
            GuardianOperation::DuplicateAuthority,
            &error,
        ));
    }
    // SAFETY: F_DUPFD_CLOEXEC returned a newly owned descriptor.
    Ok(unsafe { OwnedFd::from_raw_fd(duplicate) })
}

fn stat_at(
    parent: RawFd,
    name: &CStr,
    operation: GuardianOperation,
) -> Result<libc::stat, GuardianError> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: parent and name are valid and stat points to writable storage.
    let result = unsafe {
        libc::fstatat(
            parent,
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if result != 0 {
        let error = io::Error::last_os_error();
        return Err(GuardianError::from_io(operation, &error));
    }
    // SAFETY: successful fstatat initialized stat.
    Ok(unsafe { stat.assume_init() })
}

fn raw_fstat(fd: RawFd) -> io::Result<libc::stat> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: fd is live and stat points to writable storage.
    if unsafe { libc::fstat(fd, stat.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful fstat initialized stat.
    Ok(unsafe { stat.assume_init() })
}

fn stat_fd(fd: RawFd, operation: GuardianOperation) -> Result<libc::stat, GuardianError> {
    raw_fstat(fd).map_err(|error| GuardianError::from_io(operation, &error))
}

fn pread_once(
    fd: RawFd,
    buffer: &mut [u8],
    operation: GuardianOperation,
) -> Result<usize, GuardianError> {
    loop {
        // SAFETY: fd is open for reading and buffer is writable for its length.
        let result = unsafe {
            libc::pread(
                fd,
                buffer.as_mut_ptr().cast::<libc::c_void>(),
                buffer.len(),
                0,
            )
        };
        if result >= 0 {
            return Ok(result as usize);
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(GuardianError::from_io(operation, &error));
        }
    }
}

fn guardian_as_io(error: GuardianError) -> io::Error {
    io::Error::from_raw_os_error(error.errno().unwrap_or(libc::EINVAL))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KillError {
    pub errno: i32,
}

impl fmt::Display for KillError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "cgroup.kill write failed with errno {}",
            self.errno
        )
    }
}

impl std::error::Error for KillError {}

/// Release the reserve and write the one-byte kill command to the retained
/// `cgroup.kill` fd. This function performs no heap allocation and invokes no
/// subprocess or IPC mechanism.
pub fn kill_direct(reserve: &mut EmergencyReserve, target: &CgroupTarget) -> Result<(), KillError> {
    reserve.release();
    let command = *b"1";
    for _ in 0..3 {
        // SAFETY: kill is an open descriptor and command points to one readable byte.
        let written = unsafe {
            libc::write(
                target.kill.as_raw_fd(),
                command.as_ptr().cast::<libc::c_void>(),
                command.len(),
            )
        };
        if written == command.len() as isize {
            return Ok(());
        }
        let errno = if written < 0 {
            // SAFETY: libc exposes a thread-local errno pointer on Linux.
            unsafe { *libc::__errno_location() }
        } else {
            libc::EIO
        };
        if errno != libc::EINTR {
            return Err(KillError { errno });
        }
    }
    Err(KillError { errno: libc::EINTR })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AttemptOutcome {
    Waiting,
    Verified {
        write_errno: Option<i32>,
        observed: TargetState,
    },
    Retry {
        write_errno: Option<i32>,
        observed: Option<TargetState>,
    },
}

#[derive(Debug)]
pub struct EmergencyController {
    pub(crate) reserve: EmergencyReserve,
    retry_millis: u64,
    next_attempt_millis: u64,
    verified: bool,
}

impl EmergencyController {
    pub fn new(reserve: EmergencyReserve, retry_millis: u64) -> Self {
        Self {
            reserve,
            retry_millis,
            next_attempt_millis: 0,
            verified: false,
        }
    }

    pub fn reserve(&self) -> &EmergencyReserve {
        &self.reserve
    }

    pub fn reset(&mut self) {
        self.next_attempt_millis = 0;
        self.verified = false;
    }

    pub fn enter_emergency(&mut self) {
        self.reserve.release();
    }

    pub fn reset_for_target_generation(&mut self) {
        self.reset();
    }

    pub fn ensure_reserve(&mut self, bytes: usize) -> Result<bool, ReserveError> {
        if self.reserve.is_allocated() {
            return Ok(false);
        }
        self.reserve = EmergencyReserve::new(bytes)?;
        Ok(true)
    }

    pub fn attempt(&mut self, now_millis: u64, target: &CgroupTarget) -> AttemptOutcome {
        if self.verified || now_millis < self.next_attempt_millis {
            return AttemptOutcome::Waiting;
        }

        let write_errno = kill_direct(&mut self.reserve, target)
            .err()
            .map(|error| error.errno);
        let observed = target.state().ok();
        if matches!(observed, Some(TargetState::Empty | TargetState::Gone)) {
            self.verified = true;
            return AttemptOutcome::Verified {
                write_errno,
                observed: observed.expect("matched Some above"),
            };
        }
        self.next_attempt_millis = now_millis.saturating_add(self.retry_millis);
        AttemptOutcome::Retry {
            write_errno,
            observed,
        }
    }
}
