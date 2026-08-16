import json
import os
import re
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
mkdir -p "$CARGO_TARGET_DIR/release"
printf '#!/bin/sh\\nexit 0\\n' > "$CARGO_TARGET_DIR/release/llm-guard-proxy"
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
        fetch_status: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment.copy()
        environment["FAKE_GIT_FETCH_STATUS"] = str(fetch_status)
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
