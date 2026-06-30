use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

#[cfg(unix)]
use std::os::unix::fs::{PermissionsExt, symlink};

use rusqlite::params;

use super::{
    AttemptId, AttemptRecord, AttemptStatus, DownstreamMode, ObservabilityStore, RawPayloads,
    RequestId, RequestRecord, RequestStatus, StoreWrite, UpstreamMode, error::ObservabilityError,
};
use crate::ConfigManager;

const TEST_MAX_BYTES: u64 = 1_000_000;
const TEST_PRUNE_TO_BYTES: u64 = 800_000;
const TEST_MAX_RECORDS: u64 = 100;

#[test]
fn creates_sqlite_schema_in_test_temp_directory() {
    let fixture = StoreFixture::new("schema");
    let store = fixture.open_store(true, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);

    assert_eq!(store.schema_version().expect("schema version"), 2);
    assert!(fixture.sqlite_path.exists());

    let connection = store.lock_connection().expect("connection lock");
    let request_table_count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'requests'",
            [],
            |row| row.get(0),
        )
        .expect("requests table should exist");
    let attempt_table_count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'attempts'",
            [],
            |row| row.get(0),
        )
        .expect("attempts table should exist");

    assert_eq!(request_table_count, 1);
    assert_eq!(attempt_table_count, 1);
}

#[cfg(unix)]
#[test]
fn creates_sqlite_store_with_owner_only_permissions() {
    let fixture = StoreFixture::new("create-permissions");
    let store = fixture.open_store(true, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);

    assert_eq!(file_mode(&fixture.storage_dir()), 0o700);
    assert_eq!(file_mode(&fixture.sqlite_path), 0o600);

    drop(store);
}

#[cfg(unix)]
#[test]
fn tightens_existing_sqlite_store_permissions() {
    let fixture = StoreFixture::new("tighten-permissions");
    fs::create_dir_all(fixture.storage_dir()).expect("storage directory should be created");
    fs::set_permissions(fixture.storage_dir(), fs::Permissions::from_mode(0o700))
        .expect("storage directory should be owner-only");
    fs::write(&fixture.sqlite_path, []).expect("existing sqlite file should be created");
    fs::set_permissions(&fixture.sqlite_path, fs::Permissions::from_mode(0o666))
        .expect("sqlite file permissions should be broadened");

    let store = fixture.open_store(true, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);

    assert_eq!(file_mode(&fixture.storage_dir()), 0o700);
    assert_eq!(file_mode(&fixture.sqlite_path), 0o600);

    drop(store);
}

#[cfg(unix)]
#[test]
fn rejects_existing_shared_parent_without_chmodding_it() {
    let root = unique_test_dir("shared-parent");
    let shared_parent = root.join("shared");
    fs::create_dir_all(&shared_parent).expect("shared parent should be created");
    fs::set_permissions(&shared_parent, fs::Permissions::from_mode(0o777))
        .expect("shared parent permissions should be broadened");
    let original_mode = file_mode(&shared_parent);

    let sqlite_path = shared_parent.join("observability.sqlite3");
    let config_path = root.join("config.toml");
    write_config_file(
        &config_path,
        &sqlite_path,
        true,
        false,
        TEST_MAX_BYTES,
        TEST_PRUNE_TO_BYTES,
    );
    let manager = ConfigManager::from_explicit_path(&config_path).expect("config should load");

    let error =
        ObservabilityStore::open(manager.handle()).expect_err("shared parent should be rejected");

    match error {
        ObservabilityError::UnsafeStoragePath { path, reason } => {
            assert_eq!(path, shared_parent);
            assert!(reason.contains("group or other"));
        }
        other => panic!("unexpected error: {other}"),
    }
    assert_eq!(file_mode(&shared_parent), original_mode);
    assert!(!sqlite_path.exists());

    remove_dir_all(&root);
}

#[cfg(unix)]
#[test]
fn rejects_missing_parent_under_shared_ancestor_without_creating_it() {
    let root = unique_test_dir("shared-ancestor");
    let private_root = root.join("private");
    let shared_ancestor = private_root.join("shared");
    fs::create_dir_all(&shared_ancestor).expect("shared ancestor should be created");
    fs::set_permissions(&private_root, fs::Permissions::from_mode(0o700))
        .expect("private root should be owner-only");
    fs::set_permissions(&shared_ancestor, fs::Permissions::from_mode(0o777))
        .expect("shared ancestor permissions should be broadened");
    let original_mode = file_mode(&shared_ancestor);

    let missing_parent = shared_ancestor.join("storage");
    let sqlite_path = missing_parent.join("observability.sqlite3");
    let config_path = root.join("config.toml");
    write_config_file(
        &config_path,
        &sqlite_path,
        true,
        false,
        TEST_MAX_BYTES,
        TEST_PRUNE_TO_BYTES,
    );
    let manager = ConfigManager::from_explicit_path(&config_path).expect("config should load");

    let error = ObservabilityStore::open(manager.handle())
        .expect_err("shared ancestor should be rejected before directory creation");

    match error {
        ObservabilityError::UnsafeStoragePath { path, reason } => {
            assert_eq!(path, shared_ancestor);
            assert!(reason.contains("group/other-writable"));
        }
        other => panic!("unexpected error: {other}"),
    }
    assert_eq!(file_mode(&shared_ancestor), original_mode);
    assert!(!missing_parent.exists());
    assert!(!sqlite_path.exists());

    remove_dir_all(&root);
}

#[cfg(unix)]
#[test]
fn creates_sqlite_store_when_parent_is_missing_under_private_ancestor() {
    let root = unique_test_dir("private-ancestor");
    fs::create_dir_all(&root).expect("private ancestor should be created");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
        .expect("private ancestor should be owner-only");

    let sqlite_path = root.join("state").join("observability.sqlite3");
    let config_path = root.join("config.toml");
    write_config_file(
        &config_path,
        &sqlite_path,
        true,
        false,
        TEST_MAX_BYTES,
        TEST_PRUNE_TO_BYTES,
    );
    let manager = ConfigManager::from_explicit_path(&config_path).expect("config should load");

    let store = ObservabilityStore::open(manager.handle()).expect("store should open");

    assert_eq!(
        file_mode(sqlite_path.parent().expect("sqlite parent")),
        0o700
    );
    assert_eq!(file_mode(&sqlite_path), 0o600);

    drop(store);
    remove_dir_all(&root);
}

#[cfg(unix)]
#[test]
fn rejects_symlink_sqlite_path_without_chmodding_target() {
    let root = unique_test_dir("sqlite-symlink");
    let storage_dir = root.join("storage");
    fs::create_dir_all(&storage_dir).expect("storage directory should be created");
    fs::set_permissions(&storage_dir, fs::Permissions::from_mode(0o700))
        .expect("storage directory should be owner-only");

    let target_path = root.join("target.sqlite3");
    fs::write(&target_path, []).expect("target file should be created");
    fs::set_permissions(&target_path, fs::Permissions::from_mode(0o666))
        .expect("target file permissions should be broadened");
    let original_target_mode = file_mode(&target_path);

    let sqlite_path = storage_dir.join("observability.sqlite3");
    symlink(&target_path, &sqlite_path).expect("sqlite symlink should be created");

    let config_path = root.join("config.toml");
    write_config_file(
        &config_path,
        &sqlite_path,
        true,
        false,
        TEST_MAX_BYTES,
        TEST_PRUNE_TO_BYTES,
    );
    let manager = ConfigManager::from_explicit_path(&config_path).expect("config should load");

    let error =
        ObservabilityStore::open(manager.handle()).expect_err("sqlite symlink should be rejected");

    match error {
        ObservabilityError::UnsafeStoragePath { path, reason } => {
            assert_eq!(path, sqlite_path);
            assert!(reason.contains("symlink"));
        }
        other => panic!("unexpected error: {other}"),
    }
    assert_eq!(file_mode(&target_path), original_target_mode);

    remove_dir_all(&root);
}

#[test]
fn writes_success_and_failure_request_and_attempt_rows() {
    let fixture = StoreFixture::new("success-failure");
    let store = fixture.open_store(true, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);

    let success = request_record("req-success", RequestStatus::Succeeded, 1_000);
    let success_attempt = attempt_record(
        "attempt-success",
        &success.request_id,
        AttemptStatus::Succeeded,
        1,
        1_010,
    );
    let failure = RequestRecord {
        status: RequestStatus::Failed,
        http_status: Some(502),
        error_reason: Some(String::from("upstream timeout")),
        ..request_record("req-failure", RequestStatus::Failed, 2_000)
    };
    let failure_attempt = AttemptRecord {
        error_reason: Some(String::from("connection reset")),
        retry_reason: Some(String::from("transport error")),
        ..attempt_record(
            "attempt-failure",
            &failure.request_id,
            AttemptStatus::Failed,
            1,
            2_010,
        )
    };

    assert_eq!(
        store
            .record_request(&success)
            .expect("success request write"),
        StoreWrite::Written
    );
    assert_eq!(
        store
            .record_attempt(&success_attempt)
            .expect("success attempt write"),
        StoreWrite::Written
    );
    assert_eq!(
        store
            .record_request(&failure)
            .expect("failure request write"),
        StoreWrite::Written
    );
    assert_eq!(
        store
            .record_attempt(&failure_attempt)
            .expect("failure attempt write"),
        StoreWrite::Written
    );

    let connection = store.lock_connection().expect("connection lock");
    let succeeded_requests = count_rows(
        &connection,
        "SELECT COUNT(*) FROM requests WHERE status = 'succeeded'",
    );
    let failed_requests = count_rows(
        &connection,
        "SELECT COUNT(*) FROM requests WHERE status = 'failed'",
    );
    let succeeded_attempts = count_rows(
        &connection,
        "SELECT COUNT(*) FROM attempts WHERE status = 'succeeded'",
    );
    let failed_attempts = count_rows(
        &connection,
        "SELECT COUNT(*) FROM attempts WHERE status = 'failed'",
    );

    assert_eq!(succeeded_requests, 1);
    assert_eq!(failed_requests, 1);
    assert_eq!(succeeded_attempts, 1);
    assert_eq!(failed_attempts, 1);
}

#[test]
fn updating_request_preserves_existing_attempt_rows() {
    let fixture = StoreFixture::new("request-update-preserves-attempts");
    let store = fixture.open_store(true, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);
    let initial_request = RequestRecord {
        finished_at_unix_ms: None,
        status: RequestStatus::Failed,
        http_status: Some(500),
        error_reason: Some(String::from("in flight")),
        ..request_record("req-update", RequestStatus::Failed, 1_000)
    };
    let attempt = attempt_record(
        "attempt-update",
        &initial_request.request_id,
        AttemptStatus::Succeeded,
        1,
        1_010,
    );

    store
        .record_request(&initial_request)
        .expect("initial request write");
    store.record_attempt(&attempt).expect("attempt write");

    let final_request = RequestRecord {
        finished_at_unix_ms: Some(1_300),
        status: RequestStatus::Succeeded,
        http_status: Some(200),
        error_reason: None,
        response_metadata: BTreeMap::from([(String::from("server"), String::from("updated"))]),
        ..initial_request
    };
    store
        .record_request(&final_request)
        .expect("final request update");

    let connection = store.lock_connection().expect("connection lock");
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM attempts WHERE request_id = 'req-update' AND attempt_number = 1",
        ),
        1
    );

    let (status, finished_at_unix_ms, error_reason, response_metadata_json): (
        String,
        i64,
        Option<String>,
        String,
    ) = connection
        .query_row(
            r"
SELECT status, finished_at_unix_ms, error_reason, response_metadata_json
FROM requests
WHERE request_id = 'req-update'
",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .expect("updated request row should exist");
    assert_eq!(status, "succeeded");
    assert_eq!(finished_at_unix_ms, 1_300);
    assert_eq!(error_reason, None);
    assert!(response_metadata_json.contains("updated"));
}

#[test]
fn redacts_authorization_and_api_key_like_values_before_persistence() {
    let fixture = StoreFixture::new("redaction");
    let store = fixture.open_store(true, true, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);
    let mut request = request_record("req-redaction", RequestStatus::Succeeded, 1_000);
    request.request_metadata = BTreeMap::from([
        (
            String::from("authorization"),
            String::from("Bearer sk-fixture-secret"),
        ),
        (
            String::from("x-api-key"),
            String::from("fixture-api-key-value"),
        ),
        (
            String::from("content-type"),
            String::from("application/json"),
        ),
        (String::from("first_token_latency_ms"), String::from("12")),
    ]);
    request.raw_payloads = RawPayloads {
        input: Some(String::from(r#"{"api_key":"sk-fixture-secret"}"#)),
        output: Some(String::from("model output without credentials")),
        reasoning: Some(String::from("reasoning with token=fixture-secret")),
        tool_calls: None,
    };

    store
        .record_request(&request)
        .expect("redacted request write");

    let connection = store.lock_connection().expect("connection lock");
    let (metadata_json, raw_input, raw_output, raw_reasoning): (
        String,
        Option<String>,
        Option<String>,
        Option<String>,
    ) = connection
        .query_row(
            r"
SELECT request_metadata_json, raw_input, raw_output, raw_reasoning
FROM requests
WHERE request_id = 'req-redaction'
",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .expect("redacted row should exist");

    assert!(!metadata_json.contains("sk-fixture-secret"));
    assert!(!metadata_json.contains("fixture-api-key-value"));
    assert!(metadata_json.contains("[REDACTED]"));
    assert!(metadata_json.contains(r#""first_token_latency_ms":"12""#));
    assert_eq!(raw_input.as_deref(), Some("[REDACTED]"));
    assert_eq!(
        raw_output.as_deref(),
        Some("model output without credentials")
    );
    assert_eq!(raw_reasoning.as_deref(), Some("[REDACTED]"));
}

#[test]
fn redacts_common_raw_payload_secret_forms_before_persistence() {
    let fixture = StoreFixture::new("raw-secret-forms");
    let store = fixture.open_store(true, true, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);
    let mut request = request_record("req-raw-secrets", RequestStatus::Succeeded, 1_000);
    request.raw_payloads = RawPayloads {
        input: Some(String::from(r#"{"password":"fixture-password"}"#)),
        output: Some(String::from("credential = fixture-credential")),
        reasoning: Some(String::from("secret: fixture-secret")),
        tool_calls: Some(String::from(r#"{"arguments":{"passwd":"fixture-passwd"}}"#)),
    };
    let mut attempt = attempt_record(
        "attempt-raw-secrets",
        &request.request_id,
        AttemptStatus::Succeeded,
        1,
        1_010,
    );
    attempt.raw_payloads = RawPayloads {
        input: Some(String::from("authorization: Bearer fixture-bearer")),
        output: Some(String::from("token=fixture-token")),
        reasoning: Some(String::from(r#"{"api_key":"fixture-api-key"}"#)),
        tool_calls: Some(String::from("credential: fixture-tool-credential")),
    };

    store
        .record_request(&request)
        .expect("redacted raw request write");
    store
        .record_attempt(&attempt)
        .expect("redacted raw attempt write");

    let connection = store.lock_connection().expect("connection lock");
    assert_redacted_raw_payloads(
        &connection,
        "SELECT raw_input, raw_output, raw_reasoning, raw_tool_calls FROM requests WHERE request_id = 'req-raw-secrets'",
    );
    assert_redacted_raw_payloads(
        &connection,
        "SELECT raw_input, raw_output, raw_reasoning, raw_tool_calls FROM attempts WHERE attempt_id = 'attempt-raw-secrets'",
    );
}

#[test]
fn retention_deletes_oldest_requests_until_actual_storage_under_prune_target() {
    let fixture = StoreFixture::new("retention");
    let store = fixture.open_store(true, true, 120_000, 80_000);
    let sqlite_floor_bytes = store
        .retention_usage()
        .expect("initial retention usage")
        .observed_bytes;

    for index in 0..8 {
        let mut request = request_record(
            &format!("req-retention-{index}"),
            RequestStatus::Succeeded,
            1_000 + index,
        );
        request.raw_payloads = RawPayloads {
            input: Some("x".repeat(40_000)),
            output: Some("y".repeat(40_000)),
            reasoning: None,
            tool_calls: None,
        };
        store
            .record_request(&request)
            .expect("retention request write");
    }

    let usage = store.retention_usage().expect("retention usage");
    let expected_cap = 80_000_u64.max(sqlite_floor_bytes);
    assert!(
        usage.observed_bytes <= expected_cap,
        "actual SQLite bytes {} exceeded cap {}",
        usage.observed_bytes,
        expected_cap
    );
    assert_eq!(
        usage.observed_bytes,
        fixture
            .sqlite_path
            .metadata()
            .expect("sqlite file metadata")
            .len()
    );

    let connection = store.lock_connection().expect("connection lock");
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM requests WHERE request_id = 'req-retention-0'",
        ),
        0
    );
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM requests WHERE request_id = 'req-retention-2'",
        ),
        0
    );
    assert!(count_rows(&connection, "SELECT COUNT(*) FROM requests") < 8);
}

#[test]
fn retention_max_records_counts_requests_and_attempts() {
    let fixture = StoreFixture::new("retention-record-count");
    let manager = fixture.manager_with_max_records(
        true,
        false,
        TEST_MAX_BYTES,
        TEST_PRUNE_TO_BYTES,
        TEST_MAX_RECORDS,
    );
    let store = ObservabilityStore::open(manager.handle()).expect("store should open");

    let first = request_record("req-record-count-old", RequestStatus::Succeeded, 1_000);
    store
        .record_request(&first)
        .expect("request should be written");
    for index in 0..7 {
        let attempt = attempt_record(
            &format!("attempt-record-count-{index}"),
            &first.request_id,
            AttemptStatus::Succeeded,
            index + 1,
            1_010 + u64::from(index),
        );
        store
            .record_attempt(&attempt)
            .expect("attempt should be written");
    }

    let before = store.retention_usage().expect("retention usage before");
    assert_eq!(before.request_count, 1);
    assert_eq!(before.attempt_count, 7);
    assert_eq!(before.record_count, 8);

    fixture.write_config_with_max_records(true, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES, 5);
    let outcome = manager.reload().expect("config reload should succeed");
    assert!(outcome.applied);

    let second = request_record("req-record-count-new", RequestStatus::Succeeded, 2_000);
    store
        .record_request(&second)
        .expect("retention trigger should be written");

    let after = store.retention_usage().expect("retention usage after");
    assert_eq!(after.request_count, 1);
    assert_eq!(after.attempt_count, 0);
    assert_eq!(after.record_count, 1);
    assert!(after.record_count <= 5);

    let connection = store.lock_connection().expect("connection lock");
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM requests WHERE request_id = 'req-record-count-old'",
        ),
        0
    );
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM requests WHERE request_id = 'req-record-count-new'",
        ),
        1
    );
}

#[test]
fn retention_max_records_prunes_to_configured_record_hysteresis() {
    let fixture = StoreFixture::new("retention-record-hysteresis-configured");
    let manager = fixture.manager_with_record_hysteresis(
        true,
        false,
        TEST_MAX_BYTES,
        TEST_PRUNE_TO_BYTES,
        10,
        6,
    );
    let store = ObservabilityStore::open(manager.handle()).expect("store should open");

    for index in 0_u64..=10 {
        let request = request_record(
            &format!("req-record-hysteresis-configured-{index}"),
            RequestStatus::Succeeded,
            1_000 + index,
        );
        store
            .record_request(&request)
            .expect("request should be written");
    }

    let usage = store.retention_usage().expect("retention usage after");
    assert_eq!(usage.request_count, 6);
    assert_eq!(usage.attempt_count, 0);
    assert_eq!(usage.record_count, 6);

    let snapshot = store.metrics_snapshot().expect("metrics snapshot");
    assert_eq!(snapshot.pruning.prune_events, 1);
    assert_eq!(snapshot.pruning.pruned_requests, 5);
    assert_eq!(snapshot.pruning.pruned_attempts, 0);

    let connection = store.lock_connection().expect("connection lock");
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM requests WHERE request_id = 'req-record-hysteresis-configured-0'",
        ),
        0
    );
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM requests WHERE request_id = 'req-record-hysteresis-configured-4'",
        ),
        0
    );
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM requests WHERE request_id = 'req-record-hysteresis-configured-5'",
        ),
        1
    );
}

#[test]
fn retention_default_record_hysteresis_skips_pruning_during_headroom_writes() {
    let fixture = StoreFixture::new("retention-record-hysteresis-default");
    let manager =
        fixture.manager_with_max_records(true, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES, 10);
    let store = ObservabilityStore::open(manager.handle()).expect("store should open");

    for index in 0_u64..=10 {
        let request = request_record(
            &format!("req-record-hysteresis-default-{index}"),
            RequestStatus::Succeeded,
            1_000 + index,
        );
        store
            .record_request(&request)
            .expect("request should be written");
    }

    let usage = store.retention_usage().expect("retention usage after");
    assert_eq!(usage.record_count, 8);
    let snapshot = store.metrics_snapshot().expect("metrics snapshot");
    assert_eq!(snapshot.pruning.prune_events, 1);
    assert_eq!(snapshot.pruning.pruned_requests, 3);

    for index in 11_u64..=12 {
        let request = request_record(
            &format!("req-record-hysteresis-default-{index}"),
            RequestStatus::Succeeded,
            1_000 + index,
        );
        store
            .record_request(&request)
            .expect("headroom request should be written");
    }

    let usage = store
        .retention_usage()
        .expect("retention usage during headroom");
    assert_eq!(usage.record_count, 10);
    let snapshot = store.metrics_snapshot().expect("metrics snapshot");
    assert_eq!(snapshot.pruning.prune_events, 1);
    assert_eq!(snapshot.pruning.pruned_requests, 3);

    let request = request_record(
        "req-record-hysteresis-default-13",
        RequestStatus::Succeeded,
        1_013,
    );
    store
        .record_request(&request)
        .expect("next over-cap request should be written");

    let usage = store
        .retention_usage()
        .expect("retention usage after retrigger");
    assert_eq!(usage.record_count, 8);
    let snapshot = store.metrics_snapshot().expect("metrics snapshot");
    assert_eq!(snapshot.pruning.prune_events, 2);
    assert_eq!(snapshot.pruning.pruned_requests, 6);
}

#[test]
fn hot_reload_disabled_setting_stops_new_writes() {
    let fixture = StoreFixture::new("hot-reload-disable");
    let manager = fixture.manager(true, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);
    let store = ObservabilityStore::open(manager.handle()).expect("store should open");

    let first = request_record("req-before-disable", RequestStatus::Succeeded, 1_000);
    assert_eq!(
        store.record_request(&first).expect("first request write"),
        StoreWrite::Written
    );

    fixture.write_config(false, false, TEST_MAX_BYTES, TEST_PRUNE_TO_BYTES);
    let outcome = manager.reload().expect("reload should succeed");
    assert!(outcome.applied);

    let second = request_record("req-after-disable", RequestStatus::Succeeded, 2_000);
    assert_eq!(
        store.record_request(&second).expect("disabled write"),
        StoreWrite::Disabled
    );

    let connection = store.lock_connection().expect("connection lock");
    assert_eq!(count_rows(&connection, "SELECT COUNT(*) FROM requests"), 1);
    assert_eq!(
        count_rows(
            &connection,
            "SELECT COUNT(*) FROM requests WHERE request_id = 'req-after-disable'",
        ),
        0
    );
}

struct StoreFixture {
    root: PathBuf,
    config_path: PathBuf,
    sqlite_path: PathBuf,
}

impl StoreFixture {
    fn new(name: &str) -> Self {
        let root = unique_test_dir(name);
        fs::create_dir_all(&root).expect("test root should be created");
        Self {
            config_path: root.join("config.toml"),
            sqlite_path: root.join("storage").join("observability.sqlite3"),
            root,
        }
    }

    fn storage_dir(&self) -> PathBuf {
        self.sqlite_path
            .parent()
            .expect("sqlite path should have a parent")
            .to_path_buf()
    }

    fn open_store(
        &self,
        enabled: bool,
        capture_raw_payloads: bool,
        max_bytes: u64,
        prune_to_bytes: u64,
    ) -> ObservabilityStore {
        let manager = self.manager(enabled, capture_raw_payloads, max_bytes, prune_to_bytes);
        ObservabilityStore::open(manager.handle()).expect("store should open")
    }

    fn manager(
        &self,
        enabled: bool,
        capture_raw_payloads: bool,
        max_bytes: u64,
        prune_to_bytes: u64,
    ) -> ConfigManager {
        self.manager_with_max_records(
            enabled,
            capture_raw_payloads,
            max_bytes,
            prune_to_bytes,
            TEST_MAX_RECORDS,
        )
    }

    fn manager_with_max_records(
        &self,
        enabled: bool,
        capture_raw_payloads: bool,
        max_bytes: u64,
        prune_to_bytes: u64,
        max_records: u64,
    ) -> ConfigManager {
        self.write_config_with_max_records(
            enabled,
            capture_raw_payloads,
            max_bytes,
            prune_to_bytes,
            max_records,
        );
        ConfigManager::from_explicit_path(&self.config_path).expect("config should load")
    }

    fn manager_with_record_hysteresis(
        &self,
        enabled: bool,
        capture_raw_payloads: bool,
        max_bytes: u64,
        prune_to_bytes: u64,
        max_records: u64,
        prune_to_records: u64,
    ) -> ConfigManager {
        write_config_file_with_retention(
            &self.config_path,
            &self.sqlite_path,
            enabled,
            capture_raw_payloads,
            TestRetentionLimits {
                max_bytes,
                prune_to_bytes,
                max_records,
                prune_to_records: Some(prune_to_records),
            },
        );
        ConfigManager::from_explicit_path(&self.config_path).expect("config should load")
    }

    fn write_config(
        &self,
        enabled: bool,
        capture_raw_payloads: bool,
        max_bytes: u64,
        prune_to_bytes: u64,
    ) {
        self.write_config_with_max_records(
            enabled,
            capture_raw_payloads,
            max_bytes,
            prune_to_bytes,
            TEST_MAX_RECORDS,
        );
    }

    fn write_config_with_max_records(
        &self,
        enabled: bool,
        capture_raw_payloads: bool,
        max_bytes: u64,
        prune_to_bytes: u64,
        max_records: u64,
    ) {
        write_config_file_with_max_records(
            &self.config_path,
            &self.sqlite_path,
            enabled,
            capture_raw_payloads,
            max_bytes,
            prune_to_bytes,
            max_records,
        );
    }
}

impl Drop for StoreFixture {
    fn drop(&mut self) {
        remove_dir_all(&self.root);
    }
}

fn request_record(id: &str, status: RequestStatus, started_at_unix_ms: u64) -> RequestRecord {
    RequestRecord {
        request_id: RequestId::from_string(id).expect("test request id should be valid"),
        started_at_unix_ms,
        finished_at_unix_ms: Some(started_at_unix_ms + 100),
        downstream_mode: DownstreamMode::Streaming,
        upstream_mode: UpstreamMode::Streaming,
        model_id: Some(String::from("aeon-ultimate")),
        input_fingerprint: Some(format!("fingerprint-{id}")),
        status,
        http_status: Some(200),
        error_reason: None,
        abort_reason: None,
        request_metadata: BTreeMap::from([(
            String::from("content-type"),
            String::from("application/json"),
        )]),
        response_metadata: BTreeMap::from([(String::from("server"), String::from("vllm"))]),
        raw_payloads: RawPayloads::default(),
    }
}

fn attempt_record(
    id: &str,
    request_id: &RequestId,
    status: AttemptStatus,
    attempt_number: u32,
    started_at_unix_ms: u64,
) -> AttemptRecord {
    AttemptRecord {
        attempt_id: AttemptId::from_string(id).expect("test attempt id should be valid"),
        request_id: request_id.clone(),
        attempt_number,
        started_at_unix_ms,
        finished_at_unix_ms: Some(started_at_unix_ms + 80),
        upstream_mode: UpstreamMode::Streaming,
        status,
        http_status: Some(200),
        error_reason: None,
        retry_reason: None,
        abort_reason: None,
        request_metadata: BTreeMap::from([(
            String::from("accept"),
            String::from("text/event-stream"),
        )]),
        response_metadata: BTreeMap::from([(String::from("server"), String::from("vllm"))]),
        raw_payloads: RawPayloads::default(),
    }
}

fn count_rows(connection: &rusqlite::Connection, sql: &str) -> i64 {
    connection
        .query_row(sql, params![], |row| row.get(0))
        .expect("count query should succeed")
}

fn write_config_file(
    config_path: &Path,
    sqlite_path: &Path,
    enabled: bool,
    capture_raw_payloads: bool,
    max_bytes: u64,
    prune_to_bytes: u64,
) {
    write_config_file_with_max_records(
        config_path,
        sqlite_path,
        enabled,
        capture_raw_payloads,
        max_bytes,
        prune_to_bytes,
        TEST_MAX_RECORDS,
    );
}

fn write_config_file_with_max_records(
    config_path: &Path,
    sqlite_path: &Path,
    enabled: bool,
    capture_raw_payloads: bool,
    max_bytes: u64,
    prune_to_bytes: u64,
    max_records: u64,
) {
    write_config_file_with_retention(
        config_path,
        sqlite_path,
        enabled,
        capture_raw_payloads,
        TestRetentionLimits {
            max_bytes,
            prune_to_bytes,
            max_records,
            prune_to_records: None,
        },
    );
}

#[derive(Clone, Copy)]
struct TestRetentionLimits {
    max_bytes: u64,
    prune_to_bytes: u64,
    max_records: u64,
    prune_to_records: Option<u64>,
}

fn write_config_file_with_retention(
    config_path: &Path,
    sqlite_path: &Path,
    enabled: bool,
    capture_raw_payloads: bool,
    retention: TestRetentionLimits,
) {
    let sqlite_path = sqlite_path.display();
    let prune_to_records_entry = retention
        .prune_to_records
        .map(|value| format!("prune_to_records = {value}\n"))
        .unwrap_or_default();
    let max_bytes = retention.max_bytes;
    let prune_to_bytes = retention.prune_to_bytes;
    let max_records = retention.max_records;
    fs::write(
        config_path,
        format!(
            r#"
[observability]
enabled = {enabled}
sqlite_path = "{sqlite_path}"
capture_raw_payloads = {capture_raw_payloads}

[observability.retention]
max_bytes = {max_bytes}
prune_to_bytes = {prune_to_bytes}
max_records = {max_records}
{prune_to_records_entry}
"#
        ),
    )
    .expect("test config should be written");
}

fn assert_redacted_raw_payloads(connection: &rusqlite::Connection, sql: &str) {
    let raw_payloads: (
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
    ) = connection
        .query_row(sql, [], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
        })
        .expect("raw payload row should exist");

    assert_eq!(raw_payloads.0.as_deref(), Some("[REDACTED]"));
    assert_eq!(raw_payloads.1.as_deref(), Some("[REDACTED]"));
    assert_eq!(raw_payloads.2.as_deref(), Some("[REDACTED]"));
    assert_eq!(raw_payloads.3.as_deref(), Some("[REDACTED]"));
}

#[cfg(unix)]
fn file_mode(path: &Path) -> u32 {
    fs::metadata(path)
        .expect("path metadata should be readable")
        .permissions()
        .mode()
        & 0o777
}

fn unique_test_dir(name: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("llm-guard-proxy-{nanos}-{name}"))
}

fn remove_dir_all(path: &Path) {
    if let Err(error) = fs::remove_dir_all(path) {
        assert_eq!(error.kind(), std::io::ErrorKind::NotFound);
    }
}
