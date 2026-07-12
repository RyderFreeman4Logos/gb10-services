#![forbid(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]

//! Allocation-aware primitives for the GB10 memory guardian.
//!
//! Registration parsing and descriptor refresh happen while the host is
//! healthy. The emergency `kill_direct` path only releases the pre-touched
//! reserve and writes `1` to an already-open `cgroup.kill` descriptor.

use std::ffi::{CStr, CString};
use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::{self, Read};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::ptr::NonNull;

pub const DEFAULT_RESERVE_BYTES: usize = 64 * 1024 * 1024;
pub const DEFAULT_THRESHOLD_BYTES: u64 = 1024 * 1024 * 1024;
pub const DEFAULT_RETRY_MILLIS: u64 = 5_000;
pub const REGISTRATION_VERSION: &str = "1";
const REGISTRATION_LIMIT: usize = 2_048;
const EVENTS_LIMIT: usize = 512;

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

pub fn parse_registration(input: &[u8], uid: u32) -> Result<Registration, RegistrationError> {
    let text = std::str::from_utf8(input).map_err(|_| RegistrationError::InvalidUtf8)?;
    let mut version = None;
    let mut container_id = None;
    let mut scope = None;
    let mut control_group = None;

    for line in text.split_terminator('\n') {
        if line.is_empty() || line.contains('\0') || line.contains('\r') {
            return Err(RegistrationError::MalformedLine);
        }
        let (key, value) = line
            .split_once('=')
            .ok_or(RegistrationError::MalformedLine)?;
        if value.is_empty() || value.contains('=') {
            return Err(RegistrationError::MalformedLine);
        }
        let slot = match key {
            "version" => &mut version,
            "container_id" => &mut container_id,
            "scope" => &mut scope,
            "control_group" => &mut control_group,
            _ => return Err(RegistrationError::UnknownField),
        };
        if slot.replace(value).is_some() {
            return Err(RegistrationError::DuplicateField);
        }
    }

    if version != Some(REGISTRATION_VERSION) {
        return Err(if version.is_some() {
            RegistrationError::WrongVersion
        } else {
            RegistrationError::MissingField
        });
    }
    let container_id = container_id.ok_or(RegistrationError::MissingField)?;
    if container_id.len() != 64
        || !container_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(RegistrationError::InvalidContainerId);
    }
    let expected_scope = format!("docker-{container_id}.scope");
    if scope != Some(expected_scope.as_str()) {
        return Err(RegistrationError::InvalidScope);
    }
    let expected_control_group =
        format!("/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{expected_scope}");
    if control_group != Some(expected_control_group.as_str()) {
        return Err(RegistrationError::InvalidControlGroup);
    }

    Ok(Registration {
        container_id: container_id.to_owned(),
        scope: expected_scope,
        control_group: expected_control_group,
    })
}

#[derive(Debug)]
pub enum GuardianError {
    Io(io::Error),
    Registration(RegistrationError),
    ChangedDuringRefresh,
    StaleRegistration,
}

impl fmt::Display for GuardianError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "I/O error: {error}"),
            Self::Registration(error) => write!(formatter, "registration error: {error}"),
            Self::ChangedDuringRefresh => {
                formatter.write_str("registration changed during refresh")
            }
            Self::StaleRegistration => formatter.write_str("registered cgroup is not populated"),
        }
    }
}

impl std::error::Error for GuardianError {}

impl From<io::Error> for GuardianError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<RegistrationError> for GuardianError {
    fn from(error: RegistrationError) -> Self {
        Self::Registration(error)
    }
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
    fn from_file(file: &File) -> io::Result<Self> {
        let metadata = file.metadata()?;
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetState {
    Populated,
    Empty,
    Gone,
    Replaced,
}

pub fn target_state_requires_disarm(state: &io::Result<TargetState>) -> bool {
    matches!(
        state,
        Ok(TargetState::Empty | TargetState::Gone | TargetState::Replaced)
    )
}

#[derive(Debug)]
pub struct CgroupTarget {
    _root: OwnedFd,
    _user_slice: OwnedFd,
    _user_uid_slice: OwnedFd,
    _user_service: OwnedFd,
    app_slice: OwnedFd,
    scope: OwnedFd,
    kill: OwnedFd,
    events: OwnedFd,
    scope_name: CString,
}

impl CgroupTarget {
    fn open_registered(
        root_path: &Path,
        registration: &Registration,
        uid: u32,
    ) -> io::Result<Self> {
        let root = open_directory_path(root_path)?;
        Self::open_components(
            root,
            uid,
            CString::new(registration.scope.as_bytes()).map_err(invalid_input)?,
        )
    }

    /// Open only the rigid disposable canary unit. No caller-provided cgroup
    /// path or unit name is accepted by this mode.
    pub fn open_disposable_canary(root_path: &Path, uid: u32) -> io::Result<Self> {
        let root = open_directory_path(root_path)?;
        let scope_name = CString::new("gb10-memory-guardian-disposable-canary.service")
            .map_err(invalid_input)?;
        Self::open_components(root, uid, scope_name)
    }

    fn open_components(root: OwnedFd, uid: u32, scope_name: CString) -> io::Result<Self> {
        let user_slice = open_directory_at(root.as_raw_fd(), c"user.slice")?;
        let user_uid_name = CString::new(format!("user-{uid}.slice")).map_err(invalid_input)?;
        let user_uid_slice = open_directory_at(user_slice.as_raw_fd(), &user_uid_name)?;
        let user_service_name =
            CString::new(format!("user@{uid}.service")).map_err(invalid_input)?;
        let user_service = open_directory_at(user_uid_slice.as_raw_fd(), &user_service_name)?;
        let app_slice = open_directory_at(user_service.as_raw_fd(), c"app.slice")?;
        let scope = open_directory_at(app_slice.as_raw_fd(), &scope_name)?;
        let kill = open_file_at(scope.as_raw_fd(), c"cgroup.kill", libc::O_WRONLY)?;
        let events = open_file_at(scope.as_raw_fd(), c"cgroup.events", libc::O_RDONLY)?;
        Ok(Self {
            _root: root,
            _user_slice: user_slice,
            _user_uid_slice: user_uid_slice,
            _user_service: user_service,
            app_slice,
            scope,
            kill,
            events,
            scope_name,
        })
    }

    pub fn state(&self) -> io::Result<TargetState> {
        match self.identity_state()? {
            TargetState::Gone => return Ok(TargetState::Gone),
            TargetState::Replaced => return Ok(TargetState::Replaced),
            TargetState::Populated | TargetState::Empty => {}
        }
        let mut buffer = [0_u8; EVENTS_LIMIT];
        let length = pread_once(self.events.as_raw_fd(), &mut buffer)?;
        if length == buffer.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "cgroup.events exceeds fixed buffer",
            ));
        }
        parse_cgroup_events(&buffer[..length])
    }

    fn identity_state(&self) -> io::Result<TargetState> {
        let current = match stat_at(self.app_slice.as_raw_fd(), &self.scope_name) {
            Ok(stat) => stat,
            Err(error) if error.raw_os_error() == Some(libc::ENOENT) => {
                return Ok(TargetState::Gone)
            }
            Err(error) => return Err(error),
        };
        let held = stat_fd(self.scope.as_raw_fd())?;
        if current.st_dev != held.st_dev || current.st_ino != held.st_ino {
            return Ok(TargetState::Replaced);
        }
        Ok(TargetState::Populated)
    }
}

fn invalid_input<T>(_error: T) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, "path contains a NUL byte")
}

fn open_directory_path(path: &Path) -> io::Result<OwnedFd> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    Ok(file.into())
}

fn open_directory_at(parent: RawFd, name: &CStr) -> io::Result<OwnedFd> {
    open_at(
        parent,
        name,
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
    )
}

fn open_file_at(parent: RawFd, name: &CStr, access: i32) -> io::Result<OwnedFd> {
    open_at(parent, name, access | libc::O_NOFOLLOW | libc::O_CLOEXEC)
}

fn open_at(parent: RawFd, name: &CStr, flags: i32) -> io::Result<OwnedFd> {
    // SAFETY: parent is a retained directory fd and name is a valid NUL-terminated C string.
    let fd = unsafe { libc::openat(parent, name.as_ptr(), flags) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: openat returned a new owned descriptor which is transferred exactly once.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn stat_at(parent: RawFd, name: &CStr) -> io::Result<libc::stat> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: stat points to writable storage; parent and name remain valid for the call.
    let result = unsafe {
        libc::fstatat(
            parent,
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful fstatat initialized the entire stat structure.
    Ok(unsafe { stat.assume_init() })
}

fn stat_fd(fd: RawFd) -> io::Result<libc::stat> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: stat points to writable storage and fd is retained for the call.
    let result = unsafe { libc::fstat(fd, stat.as_mut_ptr()) };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: successful fstat initialized the entire stat structure.
    Ok(unsafe { stat.assume_init() })
}

fn pread_once(fd: RawFd, buffer: &mut [u8]) -> io::Result<usize> {
    loop {
        // SAFETY: buffer is writable for buffer.len() bytes and fd remains open.
        let result = unsafe {
            libc::pread(
                fd,
                buffer.as_mut_ptr().cast::<libc::c_void>(),
                buffer.len(),
                0,
            )
        };
        if result >= 0 {
            return usize::try_from(result).map_err(|_| io::Error::other("invalid read size"));
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

fn parse_cgroup_events(input: &[u8]) -> io::Result<TargetState> {
    let mut populated = None;
    for line in input.split(|byte| *byte == b'\n') {
        let Some(value) = line.strip_prefix(b"populated ") else {
            continue;
        };
        if populated.is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "duplicate populated field",
            ));
        }
        populated = match value {
            b"0" => Some(TargetState::Empty),
            b"1" => Some(TargetState::Populated),
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "malformed populated field",
                ))
            }
        };
    }
    populated.ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "cgroup.events has no populated field",
        )
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefreshStatus {
    Unchanged,
    Replaced,
}

#[derive(Debug)]
pub struct RegistrationManager {
    registration_path: PathBuf,
    cgroup_root: PathBuf,
    uid: u32,
    generation: Option<FileGeneration>,
    target: Option<CgroupTarget>,
    buffer: [u8; REGISTRATION_LIMIT],
}

impl RegistrationManager {
    pub fn new(registration_path: &Path, cgroup_root: &Path, uid: u32) -> Self {
        Self {
            registration_path: registration_path.to_owned(),
            cgroup_root: cgroup_root.to_owned(),
            uid,
            generation: None,
            target: None,
            buffer: [0; REGISTRATION_LIMIT],
        }
    }

    pub fn target(&self) -> Option<&CgroupTarget> {
        self.target.as_ref()
    }

    pub fn clear(&mut self) {
        self.target = None;
        self.generation = None;
    }

    fn handle_registration_io_error(&mut self, error: io::Error) -> GuardianError {
        let disarm = matches!(
            error.raw_os_error(),
            Some(libc::ENOENT)
                | Some(libc::ENOTDIR)
                | Some(libc::ELOOP)
                | Some(libc::EACCES)
                | Some(libc::EPERM)
        ) || matches!(
            error.kind(),
            io::ErrorKind::NotFound | io::ErrorKind::PermissionDenied | io::ErrorKind::InvalidData
        );
        if disarm {
            self.clear();
        }
        GuardianError::Io(error)
    }

    pub fn refresh(&mut self) -> Result<RefreshStatus, GuardianError> {
        let mut registration_file = match open_registration(&self.registration_path, self.uid) {
            Ok(file) => file,
            Err(error) => return Err(self.handle_registration_io_error(error)),
        };
        let generation = match FileGeneration::from_file(&registration_file) {
            Ok(generation) => generation,
            Err(error) => return Err(self.handle_registration_io_error(error)),
        };
        let generation_changed = self.generation != Some(generation);
        if !generation_changed {
            if let Some(target) = self.target.as_ref() {
                match target.identity_state() {
                    Ok(TargetState::Populated) => return Ok(RefreshStatus::Unchanged),
                    Ok(TargetState::Empty | TargetState::Gone | TargetState::Replaced) => {}
                    Err(error) => return Err(GuardianError::Io(error)),
                }
            }
        }

        // Fail closed before parsing or opening a changed generation. A bad new
        // file can therefore never leave the previous target armed.
        self.target = None;
        self.generation = Some(generation);

        let length = match read_fixed(&mut registration_file, &mut self.buffer) {
            Ok(length) => length,
            Err(error) => return Err(GuardianError::Io(error)),
        };
        let registration = parse_registration(&self.buffer[..length], self.uid)?;
        let candidate = CgroupTarget::open_registered(&self.cgroup_root, &registration, self.uid)?;
        if candidate.state()? != TargetState::Populated {
            return Err(GuardianError::StaleRegistration);
        }

        let confirming_file = open_registration(&self.registration_path, self.uid)?;
        if FileGeneration::from_file(&confirming_file)? != generation {
            self.generation = None;
            return Err(GuardianError::ChangedDuringRefresh);
        }

        self.target = Some(candidate);
        Ok(RefreshStatus::Replaced)
    }
}

fn open_registration(path: &Path, uid: u32) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.file_type().is_file()
        || metadata.uid() != uid
        || metadata.mode() & 0o7777 != 0o600
        || metadata.nlink() != 1
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "registration must be a uid-owned mode-0600 regular file with one link",
        ));
    }
    Ok(file)
}

fn read_fixed(file: &mut File, buffer: &mut [u8; REGISTRATION_LIMIT]) -> io::Result<usize> {
    let mut length = 0;
    while length < buffer.len() {
        let count = file.read(&mut buffer[length..])?;
        if count == 0 {
            return Ok(length);
        }
        length += count;
    }
    let mut extra = [0_u8; 1];
    if file.read(&mut extra)? != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            RegistrationError::TooLarge,
        ));
    }
    Ok(length)
}

/// Release reserve memory, then write directly through the retained
/// `cgroup.kill` fd. This function performs no heap allocation and invokes no
/// subprocess or IPC mechanism.
pub fn kill_direct(reserve: &mut EmergencyReserve, target: &CgroupTarget) -> io::Result<()> {
    reserve.release();
    let command = [b'1'];
    loop {
        // SAFETY: command points to one readable byte and target.kill is retained.
        let result = unsafe {
            libc::write(
                target.kill.as_raw_fd(),
                command.as_ptr().cast::<libc::c_void>(),
                command.len(),
            )
        };
        if result == 1 {
            return Ok(());
        }
        if result >= 0 {
            return Err(io::Error::from_raw_os_error(libc::EIO));
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AttemptOutcome {
    Waiting,
    Retry {
        write_errno: Option<i32>,
        observed: Option<TargetState>,
    },
    Verified {
        write_errno: Option<i32>,
        observed: TargetState,
    },
}

impl AttemptOutcome {
    pub fn attempted(self) -> bool {
        !matches!(self, Self::Waiting)
    }
}

#[derive(Debug)]
pub struct EmergencyController {
    reserve: EmergencyReserve,
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

    pub fn reset_for_target_generation(&mut self) {
        self.verified = false;
        self.next_attempt_millis = 0;
    }

    pub fn reserve(&self) -> &EmergencyReserve {
        &self.reserve
    }

    /// Release emergency headroom as soon as pressure is observed, even when
    /// no trusted kill target is currently armed. This performs no allocation.
    pub fn enter_emergency(&mut self) {
        self.reserve.release();
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
            .and_then(|error| error.raw_os_error());
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

pub fn read_mem_available_fd(file: &File, buffer: &mut [u8]) -> io::Result<u64> {
    let length = pread_once(file.as_raw_fd(), buffer)?;
    if length == buffer.len() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "/proc/meminfo exceeds fixed buffer",
        ));
    }
    parse_mem_available(&buffer[..length])
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

pub fn effective_uid() -> u32 {
    // SAFETY: geteuid has no arguments and returns the caller's effective uid.
    unsafe { libc::geteuid() }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};
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
            let registration = root.join("querit-cgroup.v1");
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
    fn registration_rejects_insecure_file_mode() {
        let tree = FakeTree::new();
        let target_id = id('a');
        tree.add_target(&target_id, "1");
        tree.publish(&target_id);
        fs::set_permissions(&tree.registration, fs::Permissions::from_mode(0o644))
            .expect("make registration insecure");

        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        assert!(manager.refresh().is_err());
        assert!(manager.target().is_none());
    }

    #[test]
    fn transient_registration_error_keeps_armed_target_but_absence_disarms() {
        let tree = FakeTree::new();
        let target_id = id('a');
        tree.add_target(&target_id, "1");
        tree.publish(&target_id);

        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        manager.refresh().expect("arm target");
        let _ = manager.handle_registration_io_error(io::Error::from_raw_os_error(libc::ENOMEM));
        assert!(manager.target().is_some(), "ENOMEM must keep the armed fd");

        let _ = manager.handle_registration_io_error(io::Error::from_raw_os_error(libc::ENOENT));
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
        assert!(matches!(error, GuardianError::Io(_)));
        assert!(
            manager.target().is_some(),
            "same-generation identity I/O failure must retain armed kill fd"
        );
    }

    #[test]
    fn transient_target_state_error_does_not_require_disarm() {
        let transient = Err(io::Error::from_raw_os_error(libc::EIO));
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
    fn fake_cgroup_kills_only_registered_querit_target() {
        let tree = FakeTree::new();
        let querit = id('a');
        let aeon = id('b');
        let embedding = id('c');
        for target in [&querit, &aeon, &embedding] {
            tree.add_target(target, "1");
        }
        tree.publish(&querit);
        let mut manager = RegistrationManager::new(&tree.registration, &tree.root, UID);
        manager.refresh().expect("refresh");
        let mut reserve = EmergencyReserve::with_page_size(8192, 4096).expect("reserve");
        kill_direct(&mut reserve, manager.target().expect("target")).expect("kill direct");

        assert_eq!(
            fs::read(tree.cgroup_path(&querit).join("cgroup.kill")).unwrap(),
            b"1"
        );
        assert!(fs::read(tree.cgroup_path(&aeon).join("cgroup.kill"))
            .unwrap()
            .is_empty());
        assert!(fs::read(tree.cgroup_path(&embedding).join("cgroup.kill"))
            .unwrap()
            .is_empty());
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
        fs::write(&temporary, b"version=1\ncontainer_id=../../aeon\n").unwrap();
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
