#![forbid(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]

use gb10_memory_guardian_core::{
    kill_direct, read_mem_available_fd, EmergencyReserve, MemInfoError, RegistrationManager,
};
use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

struct CountingAllocator;

std::thread_local! {
    static TRACKED_ALLOCATIONS: Cell<Option<usize>> = const { Cell::new(None) };
}

fn record_allocation() {
    let _ = TRACKED_ALLOCATIONS.try_with(|counter| {
        if let Some(count) = counter.get() {
            counter.set(Some(count.saturating_add(1)));
        }
    });
}

fn start_tracking_allocations() {
    TRACKED_ALLOCATIONS.with(|counter| counter.set(Some(0)));
}

fn stop_tracking_allocations() -> usize {
    TRACKED_ALLOCATIONS.with(|counter| {
        let count = counter.get().unwrap_or(usize::MAX);
        counter.set(None);
        count
    })
}

// SAFETY: This delegates every operation to the process-wide System allocator
// and only updates the calling test thread's counter before allocation entry points.
unsafe impl GlobalAlloc for CountingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        record_allocation();
        // SAFETY: The caller supplied a valid Layout under GlobalAlloc's contract.
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: ptr and layout came from the delegated System allocator.
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        record_allocation();
        // SAFETY: The caller supplied a valid Layout under GlobalAlloc's contract.
        unsafe { System.alloc_zeroed(layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        record_allocation();
        // SAFETY: ptr and layout came from System and new_size is forwarded unchanged.
        unsafe { System.realloc(ptr, layout, new_size) }
    }
}

#[global_allocator]
static GLOBAL: CountingAllocator = CountingAllocator;

fn unique_temp_dir() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock before epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "gb10-memory-guardian-no-alloc-{}-{nonce}",
        std::process::id()
    ))
}

fn make_target(root: &Path, registration: &Path, uid: u32, id: &str) {
    let scope = format!("docker-{id}.scope");
    let control_group =
        format!("/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{scope}");
    let directory = root.join(control_group.trim_start_matches('/'));
    fs::create_dir_all(&directory).expect("create fake cgroup");
    fs::write(directory.join("cgroup.kill"), b"").expect("create cgroup.kill");
    fs::write(directory.join("cgroup.events"), b"populated 1\nfrozen 0\n")
        .expect("create cgroup.events");
    fs::write(
        registration,
        format!("version=1\ncontainer_id={id}\nscope={scope}\ncontrol_group={control_group}\n"),
    )
    .expect("write registration");
    fs::set_permissions(registration, fs::Permissions::from_mode(0o600))
        .expect("chmod registration");
}

#[test]
fn reserve_release_and_direct_write_allocate_nothing() {
    let root = unique_temp_dir();
    fs::create_dir_all(&root).expect("create root");
    let registration = root.join("target-cgroup.v1");
    let uid = 1001;
    let id = "a".repeat(64);
    make_target(&root, &registration, uid, &id);

    let mut manager = RegistrationManager::new(&registration, &root, uid);
    manager.refresh().expect("refresh target");
    let target = manager.target().expect("target");
    let mut reserve = EmergencyReserve::with_page_size(16 * 1024, 4096).expect("allocate reserve");

    start_tracking_allocations();
    let result = kill_direct(&mut reserve, target);
    let allocations = stop_tracking_allocations();

    assert!(result.is_ok());
    assert_eq!(allocations, 0, "direct emergency function allocated");
    assert!(!reserve.is_allocated());
    fs::remove_dir_all(root).expect("remove fake cgroup tree");
}

#[test]
fn malformed_meminfo_error_allocates_nothing() {
    let root = unique_temp_dir();
    fs::create_dir_all(&root).expect("create root");
    let path = root.join("meminfo");
    fs::write(&path, b"malformed\n").expect("write malformed meminfo");
    let file = fs::File::open(&path).expect("open malformed meminfo");
    let mut buffer = [0_u8; 128];

    start_tracking_allocations();
    let result = read_mem_available_fd(&file, &mut buffer);
    let allocations = stop_tracking_allocations();

    assert_eq!(result, Err(MemInfoError::InvalidData));
    assert_eq!(allocations, 0, "compact meminfo failure allocated");
    fs::remove_dir_all(root).expect("remove meminfo fixture");
}
