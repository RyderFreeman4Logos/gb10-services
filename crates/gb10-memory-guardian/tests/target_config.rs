use gb10_memory_guardian::{
    emergency_iteration, EmergencyIteration, TargetConfigMonitor, TargetRegistrationSet,
    TargetSnapshot, TargetTransition,
};
use gb10_memory_guardian_core::{EmergencyController, EmergencyReserve};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const UID: u32 = 1001;

struct TempTree {
    root: PathBuf,
}

impl TempTree {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock before epoch")
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "gb10-memory-guardian-config-test-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&root).expect("create temp tree");
        Self { root }
    }

    fn config_path(&self) -> PathBuf {
        self.root.join("config.toml")
    }

    fn runtime_dir(&self) -> PathBuf {
        self.root.join("runtime")
    }

    fn cgroup_root(&self) -> PathBuf {
        self.root.join("cgroup")
    }

    fn cgroup_path(&self, id_byte: char) -> PathBuf {
        let container_id: String = std::iter::repeat_n(id_byte, 64).collect();
        self.cgroup_root().join(format!(
            "user.slice/user-{UID}.slice/user@{UID}.service/app.slice/docker-{container_id}.scope"
        ))
    }

    fn write_config(&self, label: &str, registration_file: &str) {
        fs::write(
            self.config_path(),
            format!(
                "schema_version = 1\n\n[target]\nlabel = {label:?}\nregistration_file = {registration_file:?}\n"
            ),
        )
        .expect("write config");
        fs::set_permissions(self.config_path(), fs::Permissions::from_mode(0o600))
            .expect("chmod config");
        fs::create_dir_all(self.runtime_dir().join("gb10-memory-guardian"))
            .expect("create registration authority");
        fs::create_dir_all(self.cgroup_root().join(format!(
            "user.slice/user-{UID}.slice/user@{UID}.service/app.slice"
        )))
        .expect("create cgroup authority");
    }

    fn replace_config_atomically(&self, label: &str, registration_file: &str) {
        let replacement = self.root.join("config.toml.new");
        fs::write(
            &replacement,
            format!(
                "schema_version = 1\n\n[target]\nlabel = {label:?}\nregistration_file = {registration_file:?}\n"
            ),
        )
        .expect("write replacement");
        fs::set_permissions(&replacement, fs::Permissions::from_mode(0o600))
            .expect("chmod replacement");
        fs::rename(replacement, self.config_path()).expect("replace config");
    }

    fn replace_config_contents_atomically(&self, contents: &str) {
        let replacement = self.root.join("config.toml.new");
        fs::write(&replacement, contents).expect("write replacement");
        fs::set_permissions(&replacement, fs::Permissions::from_mode(0o600))
            .expect("chmod replacement");
        fs::rename(replacement, self.config_path()).expect("replace config");
    }

    fn publish_registration(&self, registration_file: &str, id_byte: char) {
        let container_id: String = std::iter::repeat_n(id_byte, 64).collect();
        let scope = format!("docker-{container_id}.scope");
        let control_group =
            format!("/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/{scope}");
        let cgroup = self
            .cgroup_root()
            .join(control_group.trim_start_matches('/'));
        fs::create_dir_all(&cgroup).expect("create fake cgroup");
        fs::write(cgroup.join("cgroup.kill"), "").expect("create cgroup.kill");
        fs::write(cgroup.join("cgroup.events"), "populated 1\n").expect("create cgroup.events");

        let registration_dir = self.runtime_dir().join("gb10-memory-guardian");
        fs::create_dir_all(&registration_dir).expect("create registration directory");
        let temporary = registration_dir.join("registration.tmp");
        fs::write(
            &temporary,
            format!(
                "version=1\ncontainer_id={container_id}\nscope={scope}\ncontrol_group={control_group}\n"
            ),
        )
        .expect("write registration");
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))
            .expect("chmod registration");
        fs::rename(temporary, registration_dir.join(registration_file))
            .expect("publish registration");
    }
}

impl Drop for TempTree {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn wait_for_candidate(monitor: &mut TargetConfigMonitor) -> TargetSnapshot {
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        match monitor.pending_snapshot() {
            Ok(Some(candidate)) => return candidate,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            Ok(None) => panic!("notify event did not produce a config candidate"),
            Err(error) => panic!("unexpected config reload error: {error}"),
        }
    }
}

fn assert_snapshot(snapshot: &TargetSnapshot, label: &str, registration_path: &Path) {
    assert_eq!(snapshot.label(), label);
    assert_eq!(snapshot.registration_path(), registration_path);
}

#[test]
fn loads_initial_runtime_relative_target() {
    let tree = TempTree::new();
    tree.write_config("aeon-text", "text-cgroup.v1");

    let monitor =
        TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir()).expect("load config");

    assert_snapshot(
        monitor.active(),
        "aeon-text",
        &tree
            .runtime_dir()
            .join("gb10-memory-guardian/text-cgroup.v1"),
    );
}

#[test]
fn rejects_unsafe_or_ambiguous_registration_names() {
    for registration_file in [
        "../embedding-cgroup.v1",
        "/run/user/1001/embedding-cgroup.v1",
        "nested/text-cgroup.v1",
        "text cgroup.v1",
        "tèxt-cgroup.v1",
        ".",
        "",
    ] {
        let tree = TempTree::new();
        tree.write_config("aeon-text", registration_file);
        let error = TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir())
            .expect_err("unsafe registration name must fail");
        assert!(
            error.to_string().contains("registration_file"),
            "unexpected error for {registration_file:?}: {error}"
        );
    }

    let tree = TempTree::new();
    let overlong = "a".repeat(129);
    tree.write_config("aeon-text", &overlong);
    TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir())
        .expect_err("overlong registration name must fail");
}

#[test]
fn rejects_insecure_config_source_mode_and_unknown_fields() {
    let tree = TempTree::new();
    tree.write_config("aeon-text", "text-cgroup.v1");
    fs::set_permissions(tree.config_path(), fs::Permissions::from_mode(0o644))
        .expect("make config insecure");
    let mode_error = TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir())
        .expect_err("group/world-readable config must fail closed");
    assert!(mode_error.to_string().contains("mode"), "{mode_error}");

    fs::set_permissions(tree.config_path(), fs::Permissions::from_mode(0o700))
        .expect("make config executable");
    let executable_error = TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir())
        .expect_err("config mode must be exactly 0600");
    assert!(
        executable_error.to_string().contains("mode"),
        "{executable_error}"
    );

    fs::set_permissions(tree.config_path(), fs::Permissions::from_mode(0o600))
        .expect("restore secure mode");
    let hard_link = tree.root.join("config.toml.link");
    fs::hard_link(tree.config_path(), &hard_link).expect("create config hard link");
    let link_error = TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir())
        .expect_err("multiply linked config must fail closed");
    assert!(link_error.to_string().contains("hard link"), "{link_error}");
    fs::remove_file(hard_link).expect("remove config hard link");

    tree.replace_config_contents_atomically(
        "schema_version = 1\nunexpected = true\n[target]\nlabel = \"aeon-text\"\nregistration_file = \"text-cgroup.v1\"\n",
    );
    let field_error = TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir())
        .expect_err("unknown fields must fail closed");
    assert!(field_error.to_string().contains("parse"), "{field_error}");
}

#[test]
fn atomic_replacement_is_candidate_until_explicit_commit() {
    let tree = TempTree::new();
    tree.write_config("old-text", "old-cgroup.v1");
    let mut monitor =
        TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir()).expect("start monitor");

    tree.replace_config_atomically("new-text", "new-cgroup.v1");
    let candidate = wait_for_candidate(&mut monitor);

    assert_snapshot(
        monitor.active(),
        "old-text",
        &tree
            .runtime_dir()
            .join("gb10-memory-guardian/old-cgroup.v1"),
    );
    assert_snapshot(
        &candidate,
        "new-text",
        &tree
            .runtime_dir()
            .join("gb10-memory-guardian/new-cgroup.v1"),
    );

    assert!(monitor
        .try_commit(candidate)
        .expect("commit current candidate"));
    assert_snapshot(
        monitor.active(),
        "new-text",
        &tree
            .runtime_dir()
            .join("gb10-memory-guardian/new-cgroup.v1"),
    );
}

#[test]
fn malformed_replacement_never_changes_active_snapshot() {
    let tree = TempTree::new();
    tree.write_config("stable-text", "stable-cgroup.v1");
    let mut monitor =
        TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir()).expect("start monitor");

    fs::write(tree.config_path(), "schema_version = 1\n[target\n").expect("write malformed config");
    let deadline = Instant::now() + Duration::from_secs(3);
    let error = loop {
        match monitor.pending_snapshot() {
            Err(error) => break error,
            Ok(_) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            Ok(_) => panic!("malformed replacement did not produce a reload error"),
        }
    };
    assert!(
        error.to_string().contains("parse"),
        "unexpected error: {error}"
    );
    assert_snapshot(
        monitor.active(),
        "stable-text",
        &tree
            .runtime_dir()
            .join("gb10-memory-guardian/stable-cgroup.v1"),
    );

    tree.replace_config_atomically("recovered-text", "recovered-cgroup.v1");
    let candidate = wait_for_candidate(&mut monitor);
    assert!(monitor
        .try_commit(candidate)
        .expect("commit current candidate"));
    assert_snapshot(
        monitor.active(),
        "recovered-text",
        &tree
            .runtime_dir()
            .join("gb10-memory-guardian/recovered-cgroup.v1"),
    );
}

#[test]
fn stale_candidate_cannot_commit_after_newer_invalid_replacement() {
    let tree = TempTree::new();
    tree.write_config("stable-text", "stable-cgroup.v1");
    let mut monitor =
        TargetConfigMonitor::new(&tree.config_path(), &tree.runtime_dir()).expect("start monitor");

    tree.replace_config_atomically("candidate-text", "candidate-cgroup.v1");
    let candidate = wait_for_candidate(&mut monitor);
    tree.replace_config_contents_atomically("schema_version = 1\n[target\n");

    assert!(
        !monitor.try_commit(candidate).unwrap_or(false),
        "a candidate from an older file generation must never commit"
    );
    assert_snapshot(
        monitor.active(),
        "stable-text",
        &tree
            .runtime_dir()
            .join("gb10-memory-guardian/stable-cgroup.v1"),
    );
}

#[test]
fn valid_initial_config_waits_for_registration_before_arming() {
    let tree = TempTree::new();
    tree.write_config("aeon-text", "text-cgroup.v1");
    let mut targets = TargetRegistrationSet::new(
        &tree.config_path(),
        &tree.runtime_dir(),
        &tree.cgroup_root(),
        UID,
    )
    .expect("load valid target config");

    assert_eq!(targets.active_label(), "aeon-text");
    assert!(targets.target().is_none());
    assert!(targets.reconcile().is_err());
    assert!(targets.target().is_none());

    tree.publish_registration("text-cgroup.v1", 'a');
    assert_eq!(
        targets.reconcile().expect("arm initial registration"),
        TargetTransition::Armed
    );
    assert!(targets.target().is_some());
}

#[test]
fn missing_hot_reload_registration_preserves_last_good_armed_target() {
    let tree = TempTree::new();
    tree.write_config("old-text", "old-cgroup.v1");
    tree.publish_registration("old-cgroup.v1", 'a');
    let mut targets = TargetRegistrationSet::new(
        &tree.config_path(),
        &tree.runtime_dir(),
        &tree.cgroup_root(),
        UID,
    )
    .expect("load initial target config");
    assert_eq!(
        targets.reconcile().expect("arm initial registration"),
        TargetTransition::Armed
    );

    tree.replace_config_atomically("new-text", "new-cgroup.v1");
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let _ = targets.reconcile();
        if targets.pending_label() == Some("new-text") {
            break;
        }
        assert!(Instant::now() < deadline, "hot reload was not observed");
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(targets.active_label(), "old-text");
    assert!(targets.target().is_some(), "old target must remain armed");

    tree.publish_registration("new-cgroup.v1", 'b');
    let transition = loop {
        match targets.reconcile() {
            Ok(TargetTransition::Swapped) => break TargetTransition::Swapped,
            Ok(_) | Err(_) if Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(10));
            }
            result => panic!("candidate registration did not commit: {result:?}"),
        }
    };
    assert_eq!(transition, TargetTransition::Swapped);
    assert_eq!(targets.active_label(), "new-text");
    assert!(targets.pending_label().is_none());
    assert!(targets.target().is_some());
}

#[test]
fn replacement_before_old_empty_is_attacked_in_same_emergency_iteration() {
    let tree = TempTree::new();
    tree.write_config("aeon-text", "text-cgroup.v1");
    tree.publish_registration("text-cgroup.v1", 'a');
    let mut targets = TargetRegistrationSet::new(
        &tree.config_path(),
        &tree.runtime_dir(),
        &tree.cgroup_root(),
        UID,
    )
    .expect("load target config");
    assert_eq!(
        targets.reconcile().expect("arm first generation"),
        TargetTransition::Armed
    );

    let first_kill = tree.cgroup_path('a').join("cgroup.kill");
    tree.publish_registration("text-cgroup.v1", 'b');
    let reserve = EmergencyReserve::with_page_size(4096, 4096).expect("reserve");
    let mut controller = EmergencyController::new(reserve, 5_000);

    assert!(matches!(
        emergency_iteration(&mut controller, &mut targets, 0),
        EmergencyIteration::Retry
    ));
    assert_eq!(
        fs::read(first_kill).expect("read first cgroup.kill"),
        b"1",
        "the already-open generation must be attacked before registration refresh"
    );
    let replacement_kill = tree.cgroup_path('b').join("cgroup.kill");
    assert_eq!(
        fs::read(replacement_kill).expect("read replacement cgroup.kill"),
        b"1",
        "a replacement published before the old generation empties must be attacked immediately"
    );
    assert!(
        targets.target().is_some(),
        "a still-populated replacement must remain armed for bounded retries"
    );
}

#[test]
fn recreated_same_scope_generation_is_reopened_and_attacked() {
    let tree = TempTree::new();
    tree.write_config("aeon-text", "text-cgroup.v1");
    tree.publish_registration("text-cgroup.v1", 'a');
    let mut targets = TargetRegistrationSet::new(
        &tree.config_path(),
        &tree.runtime_dir(),
        &tree.cgroup_root(),
        UID,
    )
    .expect("load target config");
    assert_eq!(
        targets.reconcile().expect("arm generation"),
        TargetTransition::Armed
    );
    let first_generation = targets
        .target()
        .expect("armed generation")
        .generation_identity()
        .expect("first generation identity");
    let reserve = EmergencyReserve::with_page_size(4096, 4096).expect("reserve");
    let mut controller = EmergencyController::new(reserve, 5_000);
    assert!(matches!(
        emergency_iteration(&mut controller, &mut targets, 0),
        EmergencyIteration::Retry
    ));

    let scope = tree.cgroup_path('a');
    fs::remove_file(scope.join("cgroup.kill")).expect("remove old kill");
    fs::remove_file(scope.join("cgroup.events")).expect("remove old events");
    fs::remove_dir(&scope).expect("remove old scope");
    fs::create_dir(&scope).expect("recreate scope");
    fs::write(scope.join("cgroup.kill"), "").expect("create new kill");
    fs::write(scope.join("cgroup.events"), "populated 1\n").expect("create new events");

    assert!(matches!(
        emergency_iteration(&mut controller, &mut targets, 5_000),
        EmergencyIteration::Retry
    ));
    assert_eq!(
        fs::read(scope.join("cgroup.kill")).expect("read new kill"),
        b"1",
        "the recreated scope inode must be attacked in the detecting iteration"
    );
    let replacement_generation = targets
        .target()
        .expect("replacement remains armed")
        .generation_identity()
        .expect("replacement generation identity");
    assert_ne!(first_generation, replacement_generation);
}
