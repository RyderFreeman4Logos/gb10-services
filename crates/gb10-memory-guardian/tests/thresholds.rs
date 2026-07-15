use gb10_memory_guardian::{HotReloadableConfig, Thresholds};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

struct TempConfig {
    root: PathBuf,
}

impl TempConfig {
    fn new(contents: &str) -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock before epoch")
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "gb10-memory-guardian-threshold-test-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&root).expect("create temp config directory");
        let config = Self { root };
        config.replace(contents);
        config
    }

    fn path(&self) -> PathBuf {
        self.root.join("config.toml")
    }

    fn replace(&self, contents: &str) {
        let replacement = self.root.join("config.toml.new");
        fs::write(&replacement, contents).expect("write replacement config");
        fs::set_permissions(&replacement, fs::Permissions::from_mode(0o600))
            .expect("chmod replacement config");
        fs::rename(replacement, self.path()).expect("publish replacement config");
    }
}

impl Drop for TempConfig {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn config(thresholds: &str) -> String {
    format!(
        "schema_version = 1\n\n[target]\nlabel = \"aeon-text\"\nregistration_file = \"text-cgroup.v1\"\n{thresholds}"
    )
}

fn thresholds(mem_avail_stop_gib: u64, reserve_mib: u64) -> Thresholds {
    Thresholds::new(mem_avail_stop_gib, reserve_mib).expect("valid thresholds")
}

fn wait_for_reload(config: &HotReloadableConfig) -> Result<Thresholds, String> {
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        match config.reload_if_changed() {
            Ok(Some(reloaded)) => return Ok(reloaded),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            Ok(None) => return Err("notify event did not trigger a threshold reload".to_owned()),
            Err(error) => return Err(error.to_string()),
        }
    }
}

#[test]
fn absent_thresholds_use_environment_defaults() {
    let file = TempConfig::new(&config(""));
    let defaults = thresholds(3, 96);

    let hot = HotReloadableConfig::new(&file.path(), defaults).expect("load config");

    assert_eq!(hot.current(), defaults);
}

#[test]
fn present_thresholds_override_defaults_per_key() {
    let full = TempConfig::new(&config(
        "\n[thresholds]\nmem_avail_stop_gib = 1\nreserve_mib = 64\n",
    ));
    let defaults = thresholds(3, 96);
    let full_hot = HotReloadableConfig::new(&full.path(), defaults).expect("load full override");
    assert_eq!(full_hot.current(), thresholds(1, 64));

    let partial = TempConfig::new(&config("\n[thresholds]\nreserve_mib = 32\n"));
    let partial_hot =
        HotReloadableConfig::new(&partial.path(), defaults).expect("load partial override");
    assert_eq!(partial_hot.current(), thresholds(3, 32));
}

#[test]
fn malformed_reload_keeps_last_good_thresholds() {
    let file = TempConfig::new(&config(
        "\n[thresholds]\nmem_avail_stop_gib = 2\nreserve_mib = 80\n",
    ));
    let hot = HotReloadableConfig::new(&file.path(), thresholds(1, 64)).expect("load config");
    let last_good = hot.current();

    file.replace("schema_version = 1\n[target\n");
    let error = wait_for_reload(&hot).expect_err("malformed reload must fail");

    assert!(error.contains("parse config"), "unexpected error: {error}");
    assert_eq!(hot.current(), last_good);
}

#[test]
fn notify_reload_publishes_both_thresholds_together() {
    let file = TempConfig::new(&config(
        "\n[thresholds]\nmem_avail_stop_gib = 1\nreserve_mib = 64\n",
    ));
    let hot = HotReloadableConfig::new(&file.path(), thresholds(9, 9)).expect("load config");

    file.replace(&config(
        "\n[thresholds]\nmem_avail_stop_gib = 4\nreserve_mib = 128\n",
    ));
    let reloaded = wait_for_reload(&hot).expect("reload thresholds");

    assert_eq!(reloaded, thresholds(4, 128));
    assert_eq!(hot.current(), reloaded);
}

#[test]
fn rejects_zero_or_overflowing_thresholds() {
    for contents in [
        config("\n[thresholds]\nmem_avail_stop_gib = 0\nreserve_mib = 64\n"),
        config("\n[thresholds]\nmem_avail_stop_gib = 1\nreserve_mib = 0\n"),
        config(&format!(
            "\n[thresholds]\nmem_avail_stop_gib = {}\nreserve_mib = 64\n",
            u64::MAX
        )),
    ] {
        let file = TempConfig::new(&contents);
        HotReloadableConfig::new(&file.path(), thresholds(1, 64))
            .expect_err("invalid thresholds must fail closed");
    }
}

fn assert_owner_only(path: &Path) {
    let mode = fs::metadata(path)
        .expect("config metadata")
        .permissions()
        .mode()
        & 0o7777;
    assert_eq!(mode, 0o600);
}

#[test]
fn test_fixture_preserves_owner_only_config_mode() {
    let file = TempConfig::new(&config(""));
    assert_owner_only(&file.path());
}
