#![forbid(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]

use gb10_memory_guardian::{
    emergency_iteration, EmergencyIteration, TargetRegistrationSet, TargetTransition,
};
use gb10_memory_guardian_core::{EmergencyController, EmergencyReserve};
use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

const UID: u32 = 1001;
const RETRY_MILLIS: u64 = 5_000;

struct CountingAllocator;

thread_local! {
    static TRACKING: Cell<bool> = const { Cell::new(false) };
    static ALLOCATIONS: Cell<usize> = const { Cell::new(0) };
}

fn count_allocation() {
    if TRACKING.try_with(Cell::get).unwrap_or(false) {
        let _ = ALLOCATIONS.try_with(|count| count.set(count.get().saturating_add(1)));
    }
}

// SAFETY: Every allocation operation is delegated unchanged to System. The
// thread-local counter only observes allocation entry points on the test thread.
unsafe impl GlobalAlloc for CountingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        count_allocation();
        // SAFETY: The caller supplied a valid Layout under GlobalAlloc's contract.
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: ptr and layout came from the delegated System allocator.
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        count_allocation();
        // SAFETY: The caller supplied a valid Layout under GlobalAlloc's contract.
        unsafe { System.alloc_zeroed(layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        count_allocation();
        // SAFETY: ptr and layout came from System and new_size is forwarded unchanged.
        unsafe { System.realloc(ptr, layout, new_size) }
    }
}

#[global_allocator]
static GLOBAL: CountingAllocator = CountingAllocator;

struct StopTracking;

impl Drop for StopTracking {
    fn drop(&mut self) {
        TRACKING.with(|tracking| tracking.set(false));
    }
}

fn measure<T>(operation: impl FnOnce() -> T) -> (T, usize) {
    ALLOCATIONS.with(|count| count.set(0));
    TRACKING.with(|tracking| tracking.set(true));
    let stop = StopTracking;
    let result = operation();
    drop(stop);
    let allocations = ALLOCATIONS.with(Cell::get);
    (result, allocations)
}

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
            "gb10-memory-guardian-emergency-no-alloc-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&root).expect("create temp tree");
        let tree = Self { root };
        tree.write_config();
        fs::create_dir_all(tree.app_slice()).expect("create fixed app.slice authority");
        tree
    }

    fn config_path(&self) -> PathBuf {
        self.root.join("config.toml")
    }

    fn runtime_dir(&self) -> PathBuf {
        self.root.join("runtime")
    }

    fn registration_dir(&self) -> PathBuf {
        self.runtime_dir().join("gb10-memory-guardian")
    }

    fn registration_path(&self) -> PathBuf {
        self.registration_dir().join("text-cgroup.v1")
    }

    fn cgroup_root(&self) -> PathBuf {
        self.root.join("cgroup")
    }

    fn app_slice(&self) -> PathBuf {
        self.cgroup_root().join(format!(
            "user.slice/user-{UID}.slice/user@{UID}.service/app.slice"
        ))
    }

    fn scope(&self, id_byte: u8) -> PathBuf {
        let id_bytes = [id_byte; 64];
        let id = std::str::from_utf8(&id_bytes).expect("ASCII id");
        self.app_slice().join(format!("docker-{id}.scope"))
    }

    fn write_config(&self) {
        fs::write(
            self.config_path(),
            "schema_version = 1\n\n[target]\nlabel = \"aeon-text\"\nregistration_file = \"text-cgroup.v1\"\n",
        )
        .expect("write config");
        fs::set_permissions(self.config_path(), fs::Permissions::from_mode(0o600))
            .expect("chmod config");
        fs::create_dir_all(self.registration_dir()).expect("create registration parent");
    }

    fn add_scope(&self, id_byte: u8, events: &[u8]) {
        let scope = self.scope(id_byte);
        fs::create_dir_all(&scope).expect("create scope");
        fs::write(scope.join("cgroup.kill"), b"").expect("create cgroup.kill");
        fs::write(scope.join("cgroup.events"), events).expect("create cgroup.events");
    }

    fn publish(&self, id_byte: u8) {
        let id_bytes = [id_byte; 64];
        let id = std::str::from_utf8(&id_bytes).expect("ASCII id");
        let scope = format!("docker-{id}.scope");
        let control_group =
            format!("/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/{scope}");
        let temporary = self.registration_dir().join("registration.tmp");
        fs::write(
            &temporary,
            format!("version=1\ncontainer_id={id}\nscope={scope}\ncontrol_group={control_group}\n"),
        )
        .expect("write registration");
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))
            .expect("chmod registration");
        fs::rename(temporary, self.registration_path()).expect("publish registration");
    }

    fn publish_malformed(&self) {
        let temporary = self.registration_dir().join("registration.tmp");
        fs::write(
            &temporary,
            b"version=1\ncontainer_id=../../not-a-container\n",
        )
        .expect("write malformed registration");
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))
            .expect("chmod malformed registration");
        fs::rename(temporary, self.registration_path()).expect("publish malformed registration");
    }

    fn replace_scope(&self, id_byte: u8, events: &[u8]) {
        let scope = self.scope(id_byte);
        fs::remove_file(scope.join("cgroup.kill")).expect("remove old kill");
        fs::remove_file(scope.join("cgroup.events")).expect("remove old events");
        fs::remove_dir(&scope).expect("remove old scope");
        self.add_scope(id_byte, events);
    }

    fn read_kill(&self, id_byte: u8) -> Vec<u8> {
        fs::read(self.scope(id_byte).join("cgroup.kill")).expect("read cgroup.kill")
    }

    fn set_events(&self, id_byte: u8, events: &[u8]) {
        fs::write(self.scope(id_byte).join("cgroup.events"), events).expect("write events");
    }
}

impl Drop for TempTree {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn configured_targets(tree: &TempTree) -> TargetRegistrationSet {
    TargetRegistrationSet::new(
        &tree.config_path(),
        &tree.runtime_dir(),
        &tree.cgroup_root(),
        UID,
    )
    .expect("load target config")
}

fn arm(tree: &TempTree, id_byte: u8) -> TargetRegistrationSet {
    tree.add_scope(id_byte, b"populated 1\nfrozen 0\n");
    tree.publish(id_byte);
    let mut targets = configured_targets(tree);
    assert_eq!(
        targets.reconcile().expect("arm target"),
        TargetTransition::Armed
    );
    targets
}

fn controller() -> EmergencyController {
    EmergencyController::new(
        EmergencyReserve::with_page_size(4096, 4096).expect("reserve"),
        RETRY_MILLIS,
    )
}

fn assert_no_allocations(allocations: usize) {
    assert_eq!(allocations, 0, "whole emergency iteration allocated");
}

#[test]
fn a_to_b_before_a_is_empty_is_attacked_in_one_allocation_free_iteration() {
    let tree = TempTree::new();
    let mut targets = arm(&tree, b'a');
    tree.add_scope(b'b', b"populated 1\nfrozen 0\n");
    tree.publish(b'b');
    let mut controller = controller();

    let (result, allocations) = measure(|| emergency_iteration(&mut controller, &mut targets, 0));

    assert!(matches!(result, EmergencyIteration::Retry));
    assert_no_allocations(allocations);
    assert_eq!(tree.read_kill(b'a'), b"1");
    assert_eq!(tree.read_kill(b'b'), b"1");
    assert!(!controller.reserve().is_allocated());
}

#[test]
fn recreated_same_scope_is_reopened_and_attacked_without_allocation() {
    let tree = TempTree::new();
    let mut targets = arm(&tree, b'a');
    let mut controller = controller();
    assert!(matches!(
        emergency_iteration(&mut controller, &mut targets, 0),
        EmergencyIteration::Retry
    ));
    tree.replace_scope(b'a', b"populated 1\nfrozen 0\n");

    let (result, allocations) =
        measure(|| emergency_iteration(&mut controller, &mut targets, RETRY_MILLIS));

    assert!(matches!(result, EmergencyIteration::Retry));
    assert_no_allocations(allocations);
    assert_eq!(tree.read_kill(b'a'), b"1");
    assert!(targets.target().is_some());
}

#[test]
fn retry_waiting_and_due_retry_remain_allocation_free() {
    let tree = TempTree::new();
    let mut targets = arm(&tree, b'a');
    let mut controller = controller();

    let (first, first_allocations) =
        measure(|| emergency_iteration(&mut controller, &mut targets, 0));
    let (waiting, waiting_allocations) =
        measure(|| emergency_iteration(&mut controller, &mut targets, RETRY_MILLIS - 1));
    let (due, due_allocations) =
        measure(|| emergency_iteration(&mut controller, &mut targets, RETRY_MILLIS));

    assert!(matches!(first, EmergencyIteration::Retry));
    assert!(matches!(waiting, EmergencyIteration::Waiting));
    assert!(matches!(due, EmergencyIteration::Retry));
    assert_no_allocations(first_allocations);
    assert_no_allocations(waiting_allocations);
    assert_no_allocations(due_allocations);
    assert_eq!(tree.read_kill(b'a'), b"11");
}

#[test]
fn verified_a_followed_by_b_is_attacked_without_a_healthy_iteration() {
    let tree = TempTree::new();
    let mut targets = arm(&tree, b'a');
    tree.set_events(b'a', b"populated 0\nfrozen 0\n");
    tree.add_scope(b'b', b"populated 1\nfrozen 0\n");
    tree.publish(b'b');
    let mut controller = controller();

    let (result, allocations) = measure(|| emergency_iteration(&mut controller, &mut targets, 0));

    assert!(matches!(result, EmergencyIteration::Retry));
    assert_no_allocations(allocations);
    assert_eq!(tree.read_kill(b'a'), b"1");
    assert_eq!(tree.read_kill(b'b'), b"1");
    assert!(targets.target().is_some());
}

#[test]
fn malformed_events_retry_without_allocating_or_false_verification() {
    let tree = TempTree::new();
    let mut targets = arm(&tree, b'a');
    tree.set_events(b'a', b"populated maybe\n");
    let mut controller = controller();

    let (result, allocations) = measure(|| emergency_iteration(&mut controller, &mut targets, 0));

    assert!(matches!(result, EmergencyIteration::Retry));
    assert_no_allocations(allocations);
    assert_eq!(tree.read_kill(b'a'), b"1");
    assert!(targets.target().is_some());
}

#[test]
fn malformed_registration_degrades_to_allocation_free_no_target() {
    let tree = TempTree::new();
    let mut targets = arm(&tree, b'a');
    tree.publish_malformed();
    let mut controller = controller();

    let (result, allocations) = measure(|| emergency_iteration(&mut controller, &mut targets, 0));

    assert!(matches!(result, EmergencyIteration::NoTarget));
    assert_no_allocations(allocations);
    assert_eq!(tree.read_kill(b'a'), b"1");
    assert!(targets.target().is_none());
}

#[test]
fn missing_registration_is_allocation_free_no_target() {
    let tree = TempTree::new();
    let mut targets = configured_targets(&tree);
    let mut controller = controller();

    let (result, allocations) = measure(|| emergency_iteration(&mut controller, &mut targets, 0));

    assert!(matches!(result, EmergencyIteration::NoTarget));
    assert_no_allocations(allocations);
    assert!(!controller.reserve().is_allocated());
    assert!(targets.target().is_none());
}
