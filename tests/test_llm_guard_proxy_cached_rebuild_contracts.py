import hashlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REBUILD_SCRIPT = ROOT / "scripts" / "llm_guard_proxy_cached_rebuild.sh"
GUARD_CONFIG = ROOT / "config" / "llm-guard-proxy" / "config.toml"


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


class GuardRebuildProvenanceTests(unittest.TestCase):
    SOURCE_COMMIT = "1" * 40
    SOURCE_TREE = "2" * 40
    CARGO_IDENTITY = (
        "cargo 1.90.0 (fixture)\n"
        "release: 1.90.0\n"
        "host: x86_64-unknown-linux-gnu"
    )
    RUSTC_IDENTITY = (
        "rustc 1.90.0 (fixture)\n"
        "binary: rustc\n"
        "commit-hash: fixture\n"
        "host: x86_64-unknown-linux-gnu\n"
        "release: 1.90.0\n"
        "LLVM version: fixture"
    )

    @staticmethod
    def _write_fixture_dispatcher(fake_bin: Path) -> None:
        dispatcher = fake_bin / "fixture-tool"
        dispatcher.write_text(
            textwrap.dedent(
                r"""#!/usr/bin/bash
                set -euo pipefail
                case "${0##*/}" in
                  git)
                    case "$*" in
                      *" fetch --prune origin main") : ;;
                      *" checkout --detach origin/main") : ;;
                      *" rev-parse HEAD^{tree}") printf '%s\n' '2222222222222222222222222222222222222222' ;;
                      *" rev-parse HEAD") printf '%s\n' '1111111111111111111111111111111111111111' ;;
                      *" status --porcelain=v1 --untracked-files=all") : ;;
                      *) printf 'unexpected git args: %s\n' "$*" >&2; exit 90 ;;
                    esac
                    ;;
                  cargo)
                    case "${1:-}" in
                      --version)
                        printf '%s\n' \
                          'cargo 1.90.0 (fixture)' \
                          'release: 1.90.0' \
                          'host: x86_64-unknown-linux-gnu'
                        ;;
                      build)
                        /usr/bin/mkdir -p "$CARGO_TARGET_DIR/release"
                        /usr/bin/cp -- "$FIXTURE_BUILD_SOURCE" "$CARGO_TARGET_DIR/release/llm-guard-proxy"
                        /usr/bin/chmod 0755 "$CARGO_TARGET_DIR/release/llm-guard-proxy"
                        ;;
                      *) printf 'unexpected cargo args: %s\n' "$*" >&2; exit 91 ;;
                    esac
                    ;;
                  rustc)
                    if [[ "$*" != '-vV' ]]; then
                      printf 'unexpected rustc args: %s\n' "$*" >&2
                      exit 92
                    fi
                    printf '%s\n' \
                      'rustc 1.90.0 (fixture)' \
                      'binary: rustc' \
                      'commit-hash: fixture' \
                      'host: x86_64-unknown-linux-gnu' \
                      'release: 1.90.0' \
                      'LLVM version: fixture'
                    ;;
                  systemctl)
                    printf '%s\n' "$*" >>"$SYSTEMCTL_LOG"
                    case "$*" in
                      '--user is-active --quiet llm-guard-proxy.service') : ;;
                      '--user show -p MainPID --value llm-guard-proxy.service') printf '%s\n' 4242 ;;
                      *) printf 'unexpected systemctl args: %s\n' "$*" >&2; exit 93 ;;
                    esac
                    ;;
                  *) printf 'unexpected fixture tool: %s\n' "${0##*/}" >&2; exit 94 ;;
                esac
                """
            )
        )
        dispatcher.chmod(0o755)
        for name in ("git", "cargo", "rustc", "systemctl"):
            (fake_bin / name).symlink_to(dispatcher.name)

    def _run_rebuild(
        self, running_mode: str
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            fake_bin = root / "bin"
            source_dir = root / "source"
            cache_root = root / "cargo-target"
            service_bin = root / "service-bin" / "llm-guard-proxy"
            guard_config = root / "guard" / "config.toml"
            guard_unit = root / "systemd" / "llm-guard-proxy.service"
            proc_exe = root / "proc" / "4242" / "exe"
            systemctl_log = root / "systemctl.log"
            build_bin = cache_root / "release" / "llm-guard-proxy"
            build_source = Path("/usr/bin/true")

            for directory in (
                home,
                fake_bin,
                source_dir / ".git",
                service_bin.parent,
                guard_config.parent,
                guard_unit.parent,
                proc_exe.parent,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            self._write_fixture_dispatcher(fake_bin)
            guard_config.write_text('private_config_payload = "fixture-only"\n')
            guard_unit.write_text("[Service]\n# private-unit-payload\n")

            if running_mode == "exact":
                running_target = build_bin
            else:
                running_target = root / f"running-{running_mode}"
                shutil.copyfile(
                    build_source
                    if running_mode == "same-hash-stale-inode"
                    else Path("/usr/bin/false"),
                    running_target,
                )
                running_target.chmod(0o755)
            proc_exe.symlink_to(running_target)

            env = {
                "CARGO_BUILD_JOBS": "1",
                "CACHE_ROOT": str(cache_root),
                "FIXTURE_BUILD_SOURCE": str(build_source),
                "HOME": str(home),
                "LC_ALL": "C",
                "LOG_DIR": str(root / "log"),
                "LOG_FILE": str(root / "log" / "receipt.log"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "LLM_GUARD_PROXY_REBUILD_GUARD_CONFIG": str(guard_config),
                "LLM_GUARD_PROXY_REBUILD_GUARD_UNIT": str(guard_unit),
                "LLM_GUARD_PROXY_REBUILD_PROC_ROOT": str(root / "proc"),
                "SERVICE_BIN": str(service_bin),
                "SOURCE_BRANCH": "main",
                "SOURCE_DIR": str(source_dir),
                "SOURCE_REPO": "fixture-reviewed-main",
                "SYSTEMCTL_LOG": str(systemctl_log),
                "TZ": "UTC",
            }
            result = subprocess.run(
                ["/usr/bin/bash", str(REBUILD_SCRIPT)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            expected = {
                "binary_sha256": hashlib.sha256(build_source.read_bytes()).hexdigest(),
                "build_bin": str(build_bin),
                "cargo_sha256": hashlib.sha256(
                    self.CARGO_IDENTITY.encode()
                ).hexdigest(),
                "config_sha256": hashlib.sha256(guard_config.read_bytes()).hexdigest(),
                "rustc_sha256": hashlib.sha256(
                    self.RUSTC_IDENTITY.encode()
                ).hexdigest(),
                "unit_sha256": hashlib.sha256(guard_unit.read_bytes()).hexdigest(),
            }
            if build_bin.exists():
                stat = build_bin.stat()
                expected["device_inode"] = f"{stat.st_dev}:{stat.st_ino}"
            calls = systemctl_log.read_text() if systemctl_log.exists() else ""
            return result, expected, calls

    @staticmethod
    def _output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def test_running_hash_mismatch_fails_before_completion(self) -> None:
        result, _, _ = self._run_rebuild("wrong-hash")
        output = self._output(result)
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn(
            "running executable SHA-256 does not match built binary", output
        )
        self.assertNotIn("cached llm-guard-proxy workspace rebuild complete", output)

    def test_same_hash_stale_running_inode_fails_before_completion(self) -> None:
        result, _, _ = self._run_rebuild("same-hash-stale-inode")
        output = self._output(result)
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("running executable inode does not match built binary", output)
        self.assertNotIn("cached llm-guard-proxy workspace rebuild complete", output)

    def test_exact_binary_emits_content_free_bound_receipt(self) -> None:
        result, expected, systemctl_calls = self._run_rebuild("exact")
        output = self._output(result)
        self.assertEqual(result.returncode, 0, output)
        self.assertIn(f"source_commit={self.SOURCE_COMMIT}", output)
        self.assertIn(f"source_tree={self.SOURCE_TREE}", output)
        self.assertIn(
            f"cargo_identity_sha256={expected['cargo_sha256']}", output
        )
        self.assertIn(
            f"rustc_identity_sha256={expected['rustc_sha256']}", output
        )
        self.assertRegex(output, r"elf_build_id=[0-9a-f]{16,64}\b")
        self.assertIn(
            f"guard_config_sha256={expected['config_sha256']}", output
        )
        self.assertIn(f"guard_unit_sha256={expected['unit_sha256']}", output)
        for surface in ("built", "service", "running"):
            self.assertIn(
                f"{surface}_binary_sha256={expected['binary_sha256']}", output
            )
            self.assertIn(
                f"{surface}_binary_device_inode={expected['device_inode']}",
                output,
            )
        self.assertIn(f"service_symlink_target={expected['build_bin']}", output)
        self.assertIn("cached llm-guard-proxy workspace rebuild complete", output)
        self.assertNotIn("private_config_payload", output)
        self.assertNotIn("private-unit-payload", output)
        self.assertNotIn(" restart ", f" {systemctl_calls} ")


if __name__ == "__main__":
    unittest.main()
