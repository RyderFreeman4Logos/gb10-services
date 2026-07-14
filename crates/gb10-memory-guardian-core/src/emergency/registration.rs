use super::{
    decimal_bytes, open_app_slice, open_directory_path, open_file_at, pread_once,
    registration_error_disarms, registration_metadata_valid, stat_fd, CgroupTarget, FixedName,
    GuardianError, GuardianOperation, Registration, RegistrationError, TargetState, NAME_LIMIT,
    REGISTRATION_LIMIT, REGISTRATION_VERSION,
};
use std::os::fd::{AsRawFd, OwnedFd};
use std::path::Path;

#[derive(Debug, Clone, Copy)]
struct ParsedRegistration<'a> {
    container_id: &'a str,
    scope: &'a str,
    control_group: &'a str,
}

pub fn parse_registration(input: &[u8], uid: u32) -> Result<Registration, RegistrationError> {
    let parsed = parse_registration_borrowed(input, uid)?;
    Ok(Registration {
        container_id: parsed.container_id.to_owned(),
        scope: parsed.scope.to_owned(),
        control_group: parsed.control_group.to_owned(),
    })
}

fn parse_registration_borrowed(
    input: &[u8],
    uid: u32,
) -> Result<ParsedRegistration<'_>, RegistrationError> {
    if input.len() >= REGISTRATION_LIMIT {
        return Err(RegistrationError::TooLarge);
    }
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
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    {
        return Err(RegistrationError::InvalidContainerId);
    }
    let scope = scope.ok_or(RegistrationError::MissingField)?;
    let scope_bytes = scope.as_bytes();
    if scope_bytes.len() != b"docker-".len() + 64 + b".scope".len()
        || !scope_bytes.starts_with(b"docker-")
        || !scope_bytes.ends_with(b".scope")
        || &scope_bytes[b"docker-".len()..b"docker-".len() + 64] != container_id.as_bytes()
    {
        return Err(RegistrationError::InvalidScope);
    }
    let control_group = control_group.ok_or(RegistrationError::MissingField)?;
    if !control_group_matches(control_group.as_bytes(), scope_bytes, uid) {
        return Err(RegistrationError::InvalidControlGroup);
    }

    Ok(ParsedRegistration {
        container_id,
        scope,
        control_group,
    })
}

fn control_group_matches(control_group: &[u8], scope: &[u8], uid: u32) -> bool {
    let mut expected = [0_u8; NAME_LIMIT];
    let mut length: usize = 0;
    let mut digits = [0_u8; 10];
    let uid_digits = decimal_bytes(uid, &mut digits);
    for part in [
        b"/user.slice/user-".as_slice(),
        uid_digits,
        b".slice/user@".as_slice(),
        uid_digits,
        b".service/app.slice/".as_slice(),
        scope,
    ] {
        let Some(end) = length.checked_add(part.len()) else {
            return false;
        };
        if end > expected.len() {
            return false;
        }
        expected[length..end].copy_from_slice(part);
        length = end;
    }
    control_group == &expected[..length]
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FileGeneration {
    device: u64,
    inode: u64,
    size: i64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
    changed_seconds: i64,
    changed_nanoseconds: i64,
}

impl FileGeneration {
    fn from_stat(stat: &libc::stat) -> Self {
        Self {
            device: stat.st_dev,
            inode: stat.st_ino,
            size: stat.st_size,
            modified_seconds: stat.st_mtime,
            modified_nanoseconds: stat.st_mtime_nsec,
            changed_seconds: stat.st_ctime,
            changed_nanoseconds: stat.st_ctime_nsec,
        }
    }
}
#[derive(Debug)]
struct PreparedAuthority {
    registration_parent: OwnedFd,
    registration_name: FixedName<NAME_LIMIT>,
    app_slice: OwnedFd,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefreshStatus {
    Unchanged,
    Replaced,
}

#[derive(Debug)]
pub struct RegistrationManager {
    prepared: Option<PreparedAuthority>,
    preparation_error: Option<GuardianError>,
    uid: u32,
    generation: Option<FileGeneration>,
    pub(crate) target: Option<CgroupTarget>,
    buffer: [u8; REGISTRATION_LIMIT],
}

impl RegistrationManager {
    pub fn new(registration_path: &Path, cgroup_root: &Path, uid: u32) -> Self {
        let prepared = prepare_authority(registration_path, cgroup_root, uid);
        let (prepared, preparation_error) = match prepared {
            Ok(prepared) => (Some(prepared), None),
            Err(error) => (None, Some(error)),
        };
        Self {
            prepared,
            preparation_error,
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

    pub fn refresh(&mut self) -> Result<RefreshStatus, GuardianError> {
        let prepared = self
            .prepared
            .as_ref()
            .ok_or_else(|| self.preparation_error.expect("missing preparation error"))?;
        let registration = match open_file_at(
            prepared.registration_parent.as_raw_fd(),
            prepared.registration_name.as_c_str(),
            libc::O_RDONLY,
            GuardianOperation::OpenRegistration,
        ) {
            Ok(registration) => registration,
            Err(error) => {
                if error.errno().is_some_and(registration_error_disarms) {
                    self.clear();
                }
                return Err(error);
            }
        };
        let stat = stat_fd(
            registration.as_raw_fd(),
            GuardianOperation::StatRegistration,
        )?;
        if !registration_metadata_valid(&stat, self.uid) {
            self.clear();
            return Err(GuardianError::InvalidRegistrationMetadata);
        }
        let generation = FileGeneration::from_stat(&stat);

        if self.generation == Some(generation) {
            if let Some(target) = self.target.as_ref() {
                match target.identity_state()? {
                    TargetState::Populated => return Ok(RefreshStatus::Unchanged),
                    TargetState::Empty | TargetState::Gone | TargetState::Replaced => {}
                }
            }
        }

        self.clear();
        let length = pread_once(
            registration.as_raw_fd(),
            &mut self.buffer,
            GuardianOperation::ReadRegistration,
        )?;
        let parsed = parse_registration_borrowed(&self.buffer[..length], self.uid)?;
        let scope_name = FixedName::new(parsed.scope.as_bytes())?;
        let prepared = self
            .prepared
            .as_ref()
            .expect("prepared authority disappeared");
        let candidate = CgroupTarget::open_registered(&prepared.app_slice, scope_name)?;
        if candidate.state()? != TargetState::Populated {
            return Err(GuardianError::StaleRegistration);
        }

        let confirming = open_file_at(
            prepared.registration_parent.as_raw_fd(),
            prepared.registration_name.as_c_str(),
            libc::O_RDONLY,
            GuardianOperation::ConfirmRegistration,
        )?;
        let confirming_stat = stat_fd(
            confirming.as_raw_fd(),
            GuardianOperation::ConfirmRegistration,
        )?;
        if !registration_metadata_valid(&confirming_stat, self.uid)
            || FileGeneration::from_stat(&confirming_stat) != generation
        {
            return Err(GuardianError::ChangedDuringRefresh);
        }

        self.target = Some(candidate);
        self.generation = Some(generation);
        Ok(RefreshStatus::Replaced)
    }
}

fn prepare_authority(
    registration_path: &Path,
    cgroup_root: &Path,
    uid: u32,
) -> Result<PreparedAuthority, GuardianError> {
    let parent = registration_path
        .parent()
        .ok_or(GuardianError::InvalidRegistrationPath)?;
    let file_name = registration_path
        .file_name()
        .ok_or(GuardianError::InvalidRegistrationPath)?;
    let registration_name = FixedName::from_os_str(file_name)?;
    let registration_parent =
        open_directory_path(parent, GuardianOperation::PrepareRegistrationParent)?;
    let app_slice = open_app_slice(cgroup_root, uid)?;
    Ok(PreparedAuthority {
        registration_parent,
        registration_name,
        app_slice,
    })
}
