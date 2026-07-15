from __future__ import annotations

import fcntl
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gb10_stack_recovery.sh"
UNIT = ROOT / "systemd" / "gb10-stack-recovery.service"

EMBEDDING = "vllm-embedding.service"
RERANKER = "querit-4b-reranker.service"
TEXT = "vllm-aeon-27b-dflash.service"
UNITS = (EMBEDDING, RERANKER, TEXT)


class StackRecoveryFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.state = self.root / "state"
        self.fake_state = self.root / "fake-state"
        self.proc = self.root / "proc"
        self.cgroup = self.root / "cgroup"
        self.bin = self.root / "bin"
        for directory in (
            self.runtime,
            self.state,
            self.fake_state,
            self.proc,
            self.cgroup,
            self.bin,
        ):
            directory.mkdir()
        self.commands = self.root / "commands.log"
        self.meminfo = self.root / "meminfo"
        self.boot_id = self.root / "boot_id"
        self.boot_id.write_text("11111111-2222-4333-8444-555555555555\n")
        self.set_available_gib(64)
        self._write_systemctl()
        self._write_docker()
        self._write_curl()
        self._seed_active_state()

        self.env = os.environ.copy()
        self.env.update(
            {
                "COMMAND_LOG": str(self.commands),
                "FAKE_STATE": str(self.fake_state),
                "FAKE_PROC": str(self.proc),
                "FAKE_CGROUP": str(self.cgroup),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "XDG_STATE_HOME": str(self.state),
                "GB10_STACK_RECOVERY_SYSTEMCTL_BIN": str(self.bin / "systemctl"),
                "GB10_STACK_RECOVERY_DOCKER_BIN": str(self.bin / "docker"),
                "GB10_STACK_RECOVERY_CURL_BIN": str(self.bin / "curl"),
                "GB10_STACK_RECOVERY_FLOCK_BIN": "/usr/bin/flock",
                "GB10_STACK_RECOVERY_TIMEOUT_BIN": "/usr/bin/timeout",
                "GB10_STACK_RECOVERY_MEMINFO_PATH": str(self.meminfo),
                "GB10_STACK_RECOVERY_BOOT_ID_PATH": str(self.boot_id),
                "GB10_STACK_RECOVERY_PROC_ROOT": str(self.proc),
                "GB10_STACK_RECOVERY_CGROUP_ROOT": str(self.cgroup),
                "GB10_STACK_RECOVERY_DOCKER_CGROUP_PREFIX": "/docker",
                "GB10_STACK_RECOVERY_MIN_MEM_AVAILABLE_GIB": "40",
                "GB10_STACK_RECOVERY_ENDPOINT_HOST": "127.0.0.1",
                "GB10_STACK_RECOVERY_COMMAND_TIMEOUT_SECONDS": "2",
                "GB10_STACK_RECOVERY_STOP_TIMEOUT_SECONDS": "2",
                "GB10_STACK_RECOVERY_START_TIMEOUT_SECONDS": "2",
                "GB10_STACK_RECOVERY_READINESS_TIMEOUT_SECONDS": "2",
                "GB10_STACK_RECOVERY_PROBE_TIMEOUT_SECONDS": "1",
                "GB10_STACK_RECOVERY_POLL_SECONDS": "1",
                "GB10_STACK_RECOVERY_KILL_AFTER_SECONDS": "1",
            }
        )

    def cleanup(self) -> None:
        self.temporary.cleanup()

    def set_available_gib(self, gib: int) -> None:
        self.meminfo.write_text(
            "MemTotal: 134217728 kB\n"
            f"MemAvailable: {gib * 1_048_576} kB\n"
            "SwapTotal: 0 kB\n"
            "SwapFree: 0 kB\n"
        )

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env.update(overrides)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def command_lines(self) -> list[str]:
        if not self.commands.exists():
            return []
        return self.commands.read_text().splitlines()

    def _seed_active_state(self) -> None:
        for unit in UNITS:
            (self.fake_state / f"{unit}.active").write_text("active\n")
            pid = self._main_pid(unit)
            engine_pid = self._engine_pid(unit)
            (self.proc / str(pid)).mkdir()
            (self.proc / str(engine_pid)).mkdir()
            unit_cgroup = self.cgroup / "models" / unit.removesuffix(".service")
            docker_cgroup = self.cgroup / "docker" / self._cid(unit)
            for directory, pids in (
                (unit_cgroup, (pid,)),
                (docker_cgroup, (engine_pid,)),
            ):
                directory.mkdir(parents=True)
                (directory / "cgroup.events").write_text("populated 1\n")
                (directory / "cgroup.procs").write_text(
                    "".join(f"{value}\n" for value in pids)
                )

    @staticmethod
    def _main_pid(unit: str) -> int:
        return {EMBEDDING: 101, RERANKER: 201, TEXT: 301}[unit]

    @staticmethod
    def _new_main_pid(unit: str) -> int:
        return {EMBEDDING: 111, RERANKER: 211, TEXT: 311}[unit]

    @staticmethod
    def _engine_pid(unit: str) -> int:
        return {EMBEDDING: 102, RERANKER: 202, TEXT: 302}[unit]

    @staticmethod
    def _new_engine_pid(unit: str) -> int:
        return {EMBEDDING: 112, RERANKER: 212, TEXT: 312}[unit]

    @staticmethod
    def _cid(unit: str) -> str:
        return {EMBEDDING: "a" * 64, RERANKER: "b" * 64, TEXT: "c" * 64}[unit]

    def _write_systemctl(self) -> None:
        path = self.bin / "systemctl"
        path.write_text(
            """#!/usr/bin/env bash
set -u
printf 'systemctl %s\n' "$*" >>"$COMMAND_LOG"
args="$*"
unit="${@: -1}"
main_pid() {
  case "$1" in
    vllm-embedding.service) printf '101\n' ;;
    querit-4b-reranker.service) printf '201\n' ;;
    vllm-aeon-27b-dflash.service) printf '301\n' ;;
  esac
}
new_main_pid() {
  case "$1" in
    vllm-embedding.service) printf '111\n' ;;
    querit-4b-reranker.service) printf '211\n' ;;
    vllm-aeon-27b-dflash.service) printf '311\n' ;;
  esac
}
engine_pid() {
  case "$1" in
    vllm-embedding.service) printf '102\n' ;;
    querit-4b-reranker.service) printf '202\n' ;;
    vllm-aeon-27b-dflash.service) printf '302\n' ;;
  esac
}
set_empty() {
  local directory="$1"
  [[ "${PRESERVE_CGROUP_UNIT:-}" == "$unit" ]] && return 0
  printf 'populated 0\n' >"$directory/cgroup.events"
  : >"$directory/cgroup.procs"
}
case "$args" in
  '--user show --property=LoadState --property=ActiveState --property=SubState --property=MainPID --property=NRestarts --property=ControlGroup '*'.service')
    active=inactive
    sub=dead
    pid=0
    restarts=0
    if [[ -e "$FAKE_STATE/$unit.active" ]]; then
      active=active
      sub=running
      if [[ -e "$FAKE_STATE/$unit.restarted" ]]; then
        pid="$(new_main_pid "$unit")"
        restarts=1
      else
        pid="$(main_pid "$unit")"
      fi
    fi
    printf 'LoadState=loaded\nActiveState=%s\nSubState=%s\nMainPID=%s\nNRestarts=%s\nControlGroup=/models/%s\n' \
      "$active" "$sub" "$pid" "$restarts" "${unit%.service}"
    ;;
  '--user stop '*)
    if [[ "${SLOW_STOP_UNIT:-}" == "$unit" ]]; then sleep 5; fi
    if [[ "${FAIL_STOP_UNIT:-}" == "$unit" ]]; then exit 1; fi
    rm -f "$FAKE_STATE/$unit.active"
    rm -rf "$FAKE_PROC/$(main_pid "$unit")" "$FAKE_PROC/$(new_main_pid "$unit")"
    rm -rf "$FAKE_PROC/$(engine_pid "$unit")"
    set_empty "$FAKE_CGROUP/models/${unit%.service}"
    cid=a; [[ "$unit" == querit-4b-reranker.service ]] && cid=b
    [[ "$unit" == vllm-aeon-27b-dflash.service ]] && cid=c
    set_empty "$FAKE_CGROUP/docker/$(printf '%0.s'$cid {1..64})"
    ;;
  '--user start '*)
    [[ "${FAIL_START_UNIT:-}" == "$unit" ]] && exit 1
    : >"$FAKE_STATE/$unit.active"
    : >"$FAKE_STATE/$unit.restarted"
    mkdir -p "$FAKE_PROC/$(new_main_pid "$unit")"
    ;;
  '--user is-active --quiet '*)
    [[ -e "$FAKE_STATE/$unit.active" ]]
    ;;
  *)
    printf 'unexpected systemctl invocation: %s\n' "$args" >&2
    exit 64
    ;;
esac
"""
        )
        path.chmod(0o755)

    def _write_docker(self) -> None:
        path = self.bin / "docker"
        path.write_text(
            """#!/usr/bin/env bash
set -u
printf 'docker %s\n' "$*" >>"$COMMAND_LOG"
[[ "${DOCKER_FAILURE:-0}" == 1 ]] && exit 1
unit=''
case "$*" in
  *vllm-embedding*) unit=vllm-embedding.service; cid_char=a; pid=112 ;;
  *querit-4b-reranker*) unit=querit-4b-reranker.service; cid_char=b; pid=212 ;;
  *vllm-aeon-27b-dflash-n12*) unit=vllm-aeon-27b-dflash.service; cid_char=c; pid=312 ;;
  *aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa*) unit=vllm-embedding.service; cid_char=a; pid=112 ;;
  *bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb*) unit=querit-4b-reranker.service; cid_char=b; pid=212 ;;
  *cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc*) unit=vllm-aeon-27b-dflash.service; cid_char=c; pid=312 ;;
  *) exit 64 ;;
esac
cid="$(printf '%0.s'$cid_char {1..64})"
case "$1" in
  ps)
    if [[ -e "$FAKE_STATE/$unit.active" ]]; then printf '%s\n' "$cid"; fi
    ;;
  inspect)
    if [[ -e "$FAKE_STATE/$unit.active" ]]; then
      if [[ ! -e "$FAKE_STATE/$unit.restarted" ]]; then
        case "$unit" in
          vllm-embedding.service) pid=102 ;;
          querit-4b-reranker.service) pid=202 ;;
          vllm-aeon-27b-dflash.service) pid=302 ;;
        esac
      fi
      printf 'id=%s pid=%s running=true\n' "$cid" "$pid"
    else
      printf 'id=%s pid=0 running=false\n' "$cid"
    fi
    ;;
  *) exit 64 ;;
esac
"""
        )
        path.chmod(0o755)

    def _write_curl(self) -> None:
        path = self.bin / "curl"
        path.write_text(
            """#!/usr/bin/env bash
printf 'curl %s\n' "$*" >>"$COMMAND_LOG"
if [[ -n "${FAIL_READY_PORT:-}" && "$*" == *":${FAIL_READY_PORT}/v1/models"* ]]; then
  exit 22
fi
exit 0
"""
        )
        path.chmod(0o755)


class StackRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = StackRecoveryFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_success_stops_all_then_restarts_in_priority_order(self) -> None:
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.fixture.command_lines()
        stops = [line for line in lines if line.startswith("systemctl --user stop ")]
        starts = [line for line in lines if line.startswith("systemctl --user start ")]
        self.assertEqual(
            stops[:3],
            [
                f"systemctl --user stop {TEXT}",
                f"systemctl --user stop {RERANKER}",
                f"systemctl --user stop {EMBEDDING}",
            ],
        )
        self.assertEqual(
            starts,
            [
                f"systemctl --user start {EMBEDDING}",
                f"systemctl --user start {RERANKER}",
                f"systemctl --user start {TEXT}",
            ],
        )
        self.assertLess(lines.index(stops[-1]), lines.index(starts[0]))
        probe_lines = [line for line in lines if line.startswith("curl ")]
        self.assertEqual(len(probe_lines), 3)
        for port, line in zip((18012, 18013, 18010), probe_lines, strict=True):
            self.assertIn(f"http://127.0.0.1:{port}/v1/models", line)

        receipt = self.fixture.state / "gb10-stack-recovery" / "receipt.v1"
        self.assertTrue(receipt.is_file())
        evidence = receipt.read_text()
        for field in (
            "version=1",
            "result=success",
            "boot_id=11111111-2222-4333-8444-555555555555",
            "min_mem_available_kib=",
            "vllm_embedding_before_pid=101",
            "vllm_embedding_after_pid=111",
            "querit_4b_reranker_before_pid=201",
            "querit_4b_reranker_after_pid=211",
            "vllm_aeon_27b_dflash_before_pid=301",
            "vllm_aeon_27b_dflash_after_pid=311",
        ):
            self.assertIn(field, evidence)

    def test_low_uma_release_fails_closed_before_any_restart(self) -> None:
        self.fixture.set_available_gib(39)
        result = self.fixture.run()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.fixture.command_lines()
        self.assertEqual(sum("systemctl --user stop " in line for line in lines), 3)
        self.assertFalse(any("systemctl --user start " in line for line in lines))
        self.assertIn("release_threshold_not_met", result.stderr)

    def test_nonempty_cgroup_fails_closed_before_any_restart(self) -> None:
        result = self.fixture.run(PRESERVE_CGROUP_UNIT=RERANKER)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(
            any("systemctl --user start " in line for line in self.fixture.command_lines())
        )
        self.assertIn("cgroup_not_empty", result.stderr)

    def test_readiness_failure_stops_failed_stage_and_never_starts_text(self) -> None:
        result = self.fixture.run(FAIL_READY_PORT="18013")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.fixture.command_lines()
        self.assertIn(f"systemctl --user start {EMBEDDING}", lines)
        self.assertIn(f"systemctl --user start {RERANKER}", lines)
        self.assertNotIn(f"systemctl --user start {TEXT}", lines)
        reranker_stops = [line for line in lines if line == f"systemctl --user stop {RERANKER}"]
        self.assertEqual(len(reranker_stops), 2)
        self.assertIn("readiness_failed", result.stderr)

    def test_attempt_marker_allows_only_one_cycle_per_boot(self) -> None:
        first = self.fixture.run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        prior_lines = self.fixture.command_lines()
        second = self.fixture.run()
        self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.fixture.command_lines(), prior_lines)
        self.assertIn("already_attempted_this_boot", second.stderr)

    def test_nonblocking_lock_contender_performs_no_model_action(self) -> None:
        recovery_runtime = self.fixture.runtime / "gb10-stack-recovery"
        recovery_runtime.mkdir()
        lock_path = recovery_runtime / "coordinator.lock"
        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.command_lines(), [])
        self.assertIn("lock_busy", result.stderr)

    def test_timed_out_stop_still_attempts_all_stops_and_never_starts(self) -> None:
        started = time.monotonic()
        result = self.fixture.run(
            SLOW_STOP_UNIT=TEXT,
            GB10_STACK_RECOVERY_STOP_TIMEOUT_SECONDS="1",
        )
        elapsed = time.monotonic() - started
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(elapsed, 5)
        lines = self.fixture.command_lines()
        for unit in UNITS:
            self.assertIn(f"systemctl --user stop {unit}", lines)
        self.assertFalse(any("systemctl --user start " in line for line in lines))
        self.assertIn("stop_failed", result.stderr)

    def test_docker_control_plane_failure_is_pre_destructive(self) -> None:
        result = self.fixture.run(DOCKER_FAILURE="1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(
            any("systemctl --user stop " in line for line in self.fixture.command_lines())
        )
        self.assertIn("snapshot_failed", result.stderr)


class StackRecoverySourceContracts(unittest.TestCase):
    def test_script_wraps_every_external_control_command_in_hard_timeout(self) -> None:
        source = SCRIPT.read_text()
        self.assertIn("--signal=TERM", source)
        self.assertIn("--kill-after=", source)
        for wrapper in (
            "run_systemctl()",
            "run_docker()",
            "run_curl()",
            "run_flock()",
            "run_file_command()",
        ):
            self.assertIn(wrapper, source)
        for command in ("systemctl_bin", "docker_bin", "curl_bin", "flock_bin"):
            invocations = [line.strip() for line in source.splitlines() if f'"${{{command}}}"' in line]
            self.assertTrue(invocations, command)
            self.assertTrue(
                all("run_with_timeout" in line for line in invocations),
                f"unbounded {command}: {invocations}",
            )

    def test_unit_is_bounded_oneshot_with_protected_memory(self) -> None:
        unit = UNIT.read_text()
        for setting in (
            "Type=oneshot",
            "ExecStart=%h/.local/bin/gb10_stack_recovery.sh",
            "TimeoutStartSec=7200",
            "MemoryMax=128M",
            "MemorySwapMax=0",
            "OOMScoreAdjust=-900",
            "RuntimeDirectory=gb10-stack-recovery",
            "StateDirectory=gb10-stack-recovery",
            "GB10_STACK_RECOVERY_MIN_MEM_AVAILABLE_GIB=40",
            "GB10_STACK_RECOVERY_READINESS_TIMEOUT_SECONDS=900",
        ):
            self.assertIn(setting, unit)
        for coupling in ("Requires=", "BindsTo=", "PartOf=", "OnFailure="):
            self.assertNotIn(coupling, unit)


if __name__ == "__main__":
    unittest.main()
