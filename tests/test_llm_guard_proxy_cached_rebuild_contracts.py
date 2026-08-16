import json
import os
import re
import select
import signal
import stat
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REBUILD_SCRIPT = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.sh"
GUARD_CONFIG = ROOT / "config" / "llm-guard-proxy" / "config.toml"
FULL_SHA = "8adcce30c6a3264751439a73f04aa30ab4fc2392"


class CachedRebuildFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.command_log = self.root / "commands.log"
        self.systemctl_log = self.root / "systemctl.log"
        self.service_bin = self.bin / "llm-guard-proxy"
        self.prior_bin = self.root / "prior-llm-guard-proxy"
        self.prior_bin.write_text("prior\n")
        self.prior_bin.chmod(0o755)
        self.service_bin.symlink_to(self.prior_bin)
        self._write_fakes()

        self.environment = os.environ.copy()
        self.environment.update(
            {
                "HOME": str(self.home),
                "FAKE_COMMAND_LOG": str(self.command_log),
                "FAKE_GIT_SHA": FULL_SHA,
                "FAKE_SYSTEMCTL_LOG": str(self.systemctl_log),
                "SOURCE_REPO": "https://example.invalid/not-reviewed",
            }
        )

    def __enter__(self) -> "CachedRebuildFixture":
        return self

    def __exit__(self, *_args: object) -> None:
        self.temporary.cleanup()

    def _write_executable(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(source)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _write_fakes(self) -> None:
        self._write_executable(
            "git",
            """#!/usr/bin/env bash
printf 'git %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ " $* " == *" clone "* ]]; then
    destination="${@: -1}"
    mkdir -p "$destination/.git"
    : > "$destination/Cargo.toml"
elif [[ " $* " == *" fetch "* ]]; then
    exit "${FAKE_GIT_FETCH_STATUS:-0}"
elif [[ " $* " == *" status "* ]]; then
    printf '%s' "${FAKE_GIT_STATUS:-}"
elif [[ " $* " == *" archive "* ]]; then
    /usr/bin/tar -C "$2" --exclude=.git -cf - .
elif [[ " $* " == *" rev-parse "* ]]; then
    printf '%s\\n' "$FAKE_GIT_SHA"
fi
""",
        )
        self._write_executable(
            "cargo",
            """#!/usr/bin/env bash
printf 'cargo %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ "${1:-}" == "--version" ]]; then
    printf 'cargo 1.0.0 (fake)\\n'
    exit 0
fi
if [[ -n "${FAKE_BUILD_ENTERED:-}" ]]; then
    printf 'entered\\n' > "$FAKE_BUILD_ENTERED"
    IFS= read -r _ < "$FAKE_BUILD_RELEASE"
fi
mkdir -p "$CARGO_TARGET_DIR/release"
if [[ -n "${FAKE_BUILD_RELATIVE_FILE:-}" ]]; then
    manifest_path=""
    while (( $# )); do
        if [[ "$1" == "--manifest-path" ]]; then
            manifest_path="$2"
            break
        fi
        shift
    done
    cat "$(dirname "$manifest_path")/$FAKE_BUILD_RELATIVE_FILE" > "$CARGO_TARGET_DIR/release/llm-guard-proxy"
else
    printf '#!/bin/sh\\nexit 0\\n' > "$CARGO_TARGET_DIR/release/llm-guard-proxy"
fi
chmod 0755 "$CARGO_TARGET_DIR/release/llm-guard-proxy"
""",
        )
        self._write_executable(
            "systemctl",
            """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "$*" in
    '--user is-active --quiet llm-guard-proxy.service') exit 0 ;;
    '--user show -p MainPID --value llm-guard-proxy.service') printf '4242\\n' ;;
    *) exit 0 ;;
esac
""",
        )
        self._write_executable(
            "file",
            """#!/usr/bin/env bash
printf 'file %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ -n "${FAKE_POST_SWAP_ENTERED:-}" ]]; then
    printf 'entered\\n' > "$FAKE_POST_SWAP_ENTERED"
    IFS= read -r _ < "$FAKE_POST_SWAP_RELEASE"
fi
exit "${FAKE_FILE_STATUS:-0}"
""",
        )
        self._write_executable(
            "readlink",
            """#!/usr/bin/env bash
case "${1:-}" in
    /proc/*/exe) printf '/old/llm-guard-proxy (deleted)\\n' ;;
    *) exec /usr/bin/readlink "$@" ;;
esac
""",
        )
        for name in ("curl", "sleep"):
            self._write_executable(
                name,
                f"#!/bin/sh\nprintf '{name} %s\\n' \"$*\" >> \"$FAKE_COMMAND_LOG\"\n",
            )

    def run(
        self,
        *arguments: str,
        fail_after_receipt_replace: bool = False,
        fetch_status: int = 0,
        file_status: int = 0,
        git_status: str = "",
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment.copy()
        environment["FAKE_GIT_FETCH_STATUS"] = str(fetch_status)
        environment["FAKE_FILE_STATUS"] = str(file_status)
        environment["FAKE_GIT_STATUS"] = git_status
        if fail_after_receipt_replace:
            (self.root / "sitecustomize.py").write_text(
                """import os
import stat

_real_fsync = os.fsync

def _fail_directory_fsync(descriptor):
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
        open(os.environ["FAKE_RECEIPT_REPLACED_MARKER"], "w").close()
        raise OSError("injected directory fsync failure")
    return _real_fsync(descriptor)

os.fsync = _fail_directory_fsync
"""
            )
            environment["PYTHONPATH"] = str(self.root)
            environment["FAKE_RECEIPT_REPLACED_MARKER"] = str(
                self.root / "receipt-replaced"
            )
        return subprocess.run(
            ["/usr/bin/bash", str(REBUILD_SCRIPT), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def receipts(self) -> list[Path]:
        state = self.home / ".local" / "state" / "llm-guard-proxy-rebuild"
        return list(state.glob("completion-*.json")) if state.exists() else []


class GuardProductionFeatureContractTests(unittest.TestCase):
    def test_cached_rebuild_enables_guard_feature_explicitly(self) -> None:
        script = REBUILD_SCRIPT.read_text()
        build = next(
            line
            for line in script.splitlines()
            if "cargo build" in line and "-p llm-guard-proxy" in line
        )
        self.assertRegex(build, r"--features(?:=|\s+)guard(?:\s|$)")

    def test_production_config_bounds_workflow_executions(self) -> None:
        # Active [guard_workflows] is temporarily commented: the installed
        # binary fail-closes on unknown sections. Keep the intended bound in
        # comments so deploy does not forget the value when re-enabled.
        text = GUARD_CONFIG.read_text()
        config = tomllib.loads(text)
        self.assertNotIn("guard_workflows", config)
        self.assertRegex(
            text,
            r"(?m)^# \[guard_workflows\]\s*$",
        )
        self.assertRegex(
            text,
            r"(?m)^# max_in_flight_executions = 4\s*$",
        )

    def test_pinned_deferred_mode_never_calls_systemctl_for_deleted_guard(self) -> None:
        with CachedRebuildFixture() as fixture:
            result = fixture.run("--install-pinned-deferred", FULL_SHA)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(fixture.systemctl_log.exists())
            [receipt_path] = fixture.receipts()
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["requested_source_sha"], FULL_SHA)
            self.assertEqual(receipt["verified_source_sha"], FULL_SHA)
            self.assertEqual(
                receipt["source_repository"],
                "https://github.com/RyderFreeman4Logos/llm-guard-proxy",
            )
            self.assertEqual(receipt["activation"], "deferred")
            self.assertFalse(receipt["activation_actions_performed"])
            self.assertEqual(
                receipt["candidate_artifact"]["sha256"],
                receipt["runtime_artifact"]["sha256"],
            )
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)

    def test_pinned_deferred_mode_rejects_non_full_sha_before_publication(self) -> None:
        for source_sha in ("8adcce30c6a3", "not-a-sha"):
            with self.subTest(source_sha=source_sha), CachedRebuildFixture() as fixture:
                result = fixture.run("--install-pinned-deferred", source_sha)

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(fixture.service_bin.resolve(), fixture.prior_bin)
                self.assertFalse(fixture.command_log.exists())
                self.assertFalse(fixture.systemctl_log.exists())
                self.assertEqual(fixture.receipts(), [])

    def test_pinned_deferred_mode_rejects_unavailable_sha_before_publication(self) -> None:
        with CachedRebuildFixture() as fixture:
            result = fixture.run(
                "--install-pinned-deferred",
                FULL_SHA,
                fetch_status=42,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(fixture.service_bin.resolve(), fixture.prior_bin)
            self.assertFalse(fixture.systemctl_log.exists())
            self.assertEqual(fixture.receipts(), [])

    def test_pinned_deferred_mode_rejects_dirty_source_before_build(self) -> None:
        with CachedRebuildFixture() as fixture:
            result = fixture.run(
                "--install-pinned-deferred",
                FULL_SHA,
                git_status=" M src/main.rs\n",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("cargo build", fixture.command_log.read_text())
            self.assertEqual(fixture.service_bin.resolve(), fixture.prior_bin)
            self.assertEqual(fixture.receipts(), [])

    def test_pinned_build_uses_requested_commit_bytes_with_hidden_tracked_edit(self) -> None:
        with CachedRebuildFixture() as fixture:
            upstream = fixture.root / "upstream"
            source = fixture.root / "source"
            upstream.mkdir()
            git_environment = os.environ.copy()
            git_environment.update(
                {
                    "GIT_AUTHOR_NAME": "Fixture",
                    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                    "GIT_COMMITTER_NAME": "Fixture",
                    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                }
            )
            subprocess.run(["/usr/bin/git", "init", "-q", str(upstream)], check=True)
            (upstream / "Cargo.toml").write_text("[workspace]\n")
            (upstream / "source-marker").write_text("committed bytes\n")
            subprocess.run(
                ["/usr/bin/git", "-C", str(upstream), "add", "Cargo.toml", "source-marker"],
                check=True,
            )
            subprocess.run(
                ["/usr/bin/git", "-C", str(upstream), "commit", "-qm", "fixture"],
                check=True,
                env=git_environment,
            )
            source_sha = subprocess.run(
                ["/usr/bin/git", "-C", str(upstream), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["/usr/bin/git", "clone", "-q", str(upstream), str(source)], check=True
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(source),
                    "update-index",
                    "--assume-unchanged",
                    "source-marker",
                ],
                check=True,
            )
            (source / "source-marker").write_text("hidden dirty bytes\n")
            fixture._write_executable(
                "git",
                """#!/usr/bin/env bash
printf 'git %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
arguments=()
for argument in "$@"; do
    if [[ "$argument" == "https://github.com/RyderFreeman4Logos/llm-guard-proxy" ]]; then
        arguments+=("$FAKE_SOURCE_REPO")
    else
        arguments+=("$argument")
    fi
done
exec /usr/bin/git "${arguments[@]}"
""",
            )
            fixture.environment.update(
                {
                    "SOURCE_DIR": str(source),
                    "FAKE_SOURCE_REPO": str(upstream),
                    "FAKE_BUILD_RELATIVE_FILE": "source-marker",
                }
            )

            result = fixture.run("--install-pinned-deferred", source_sha)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            [receipt_path] = fixture.receipts()
            candidate = Path(json.loads(receipt_path.read_text())["candidate_artifact"]["path"])
            self.assertEqual(candidate.read_text(), "committed bytes\n")

    def test_rebuild_rejects_overlapping_invocation(self) -> None:
        with CachedRebuildFixture() as fixture:
            entered = fixture.root / "build-entered.fifo"
            release = fixture.root / "build-release.fifo"
            os.mkfifo(entered)
            os.mkfifo(release)
            entered_fd = os.open(entered, os.O_RDWR | os.O_NONBLOCK)
            release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            environment = fixture.environment.copy()
            environment.update(
                {
                    "FAKE_BUILD_ENTERED": str(entered),
                    "FAKE_BUILD_RELEASE": str(release),
                    "FAKE_GIT_FETCH_STATUS": "0",
                    "FAKE_GIT_STATUS": "",
                }
            )
            first = subprocess.Popen(
                [
                    "/usr/bin/bash",
                    str(REBUILD_SCRIPT),
                    "--install-pinned-deferred",
                    FULL_SHA,
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                ready, _, _ = select.select([entered_fd], [], [], 5)
                self.assertEqual(ready, [entered_fd], "first build did not reach marker")
                self.assertEqual(os.read(entered_fd, 8), b"entered\n")
                overlapping = fixture.run("--install-pinned-deferred", FULL_SHA)

                self.assertNotEqual(overlapping.returncode, 0)
                self.assertEqual(fixture.service_bin.resolve(), fixture.prior_bin)
                self.assertEqual(fixture.receipts(), [])
            finally:
                os.write(release_fd, b"release\n")
                stdout, stderr = first.communicate(timeout=10)
                os.close(entered_fd)
                os.close(release_fd)
                self.assertEqual(first.returncode, 0, stdout + stderr)

    def test_custom_source_dir_reaches_legacy_build_on_fresh_home(self) -> None:
        with CachedRebuildFixture() as fixture:
            fixture.environment["SOURCE_DIR"] = str(fixture.root / "source" / "guard")

            result = fixture.run()

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("cargo build", fixture.command_log.read_text())

    def test_pinned_deferred_mode_rolls_back_post_publication_failure(self) -> None:
        with CachedRebuildFixture() as fixture:
            result = fixture.run(
                "--install-pinned-deferred",
                FULL_SHA,
                file_status=42,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("file ", fixture.command_log.read_text())
            self.assertEqual(fixture.service_bin.resolve(), fixture.prior_bin)
            self.assertEqual(fixture.receipts(), [])

    def test_same_target_rebuild_restores_previous_artifact_bytes(self) -> None:
        with CachedRebuildFixture() as fixture:
            build_bin = (
                fixture.home
                / ".cache"
                / "cargo-target"
                / f"llm-guard-proxy-{FULL_SHA}"
                / "release"
                / "llm-guard-proxy"
            )
            build_bin.parent.mkdir(parents=True)
            build_bin.write_text("previous artifact bytes\n")
            build_bin.chmod(0o755)
            fixture.service_bin.unlink()
            fixture.service_bin.symlink_to(build_bin)

            result = fixture.run(
                "--install-pinned-deferred",
                FULL_SHA,
                file_status=42,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(fixture.service_bin.resolve(), build_bin)
            self.assertEqual(build_bin.read_text(), "previous artifact bytes\n")
            self.assertEqual(fixture.receipts(), [])

    def test_term_after_publication_restores_previous_link(self) -> None:
        with CachedRebuildFixture() as fixture:
            entered = fixture.root / "post-swap-entered.fifo"
            release = fixture.root / "post-swap-release.fifo"
            os.mkfifo(entered)
            os.mkfifo(release)
            entered_fd = os.open(entered, os.O_RDWR | os.O_NONBLOCK)
            release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            environment = fixture.environment.copy()
            environment.update(
                {
                    "FAKE_GIT_FETCH_STATUS": "0",
                    "FAKE_GIT_STATUS": "",
                    "FAKE_POST_SWAP_ENTERED": str(entered),
                    "FAKE_POST_SWAP_RELEASE": str(release),
                }
            )
            process = subprocess.Popen(
                [
                    "/usr/bin/bash",
                    str(REBUILD_SCRIPT),
                    "--install-pinned-deferred",
                    FULL_SHA,
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                ready, _, _ = select.select([entered_fd], [], [], 5)
                self.assertEqual(ready, [entered_fd], "build did not reach post-swap marker")
                self.assertEqual(os.read(entered_fd, 8), b"entered\n")
                self.assertNotEqual(fixture.service_bin.resolve(), fixture.prior_bin)
                process.send_signal(signal.SIGTERM)
                os.write(release_fd, b"release\n")
                stdout, stderr = process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual(
                    fixture.service_bin.resolve(), fixture.prior_bin, stdout + stderr
                )
                self.assertEqual(fixture.receipts(), [])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=10)
                os.close(entered_fd)
                os.close(release_fd)

    def test_pinned_deferred_mode_removes_receipt_when_commit_fails(self) -> None:
        with CachedRebuildFixture() as fixture:
            result = fixture.run(
                "--install-pinned-deferred",
                FULL_SHA,
                fail_after_receipt_replace=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((fixture.root / "receipt-replaced").exists())
            self.assertEqual(fixture.service_bin.resolve(), fixture.prior_bin)
            self.assertEqual(fixture.receipts(), [])

    def test_no_argument_mode_keeps_deleted_guard_restart_contract(self) -> None:
        with CachedRebuildFixture() as fixture:
            result = fixture.run()

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(
                "--user restart llm-guard-proxy.service\n",
                fixture.systemctl_log.read_text(),
            )
            self.assertEqual(fixture.receipts(), [])


if __name__ == "__main__":
    unittest.main()
