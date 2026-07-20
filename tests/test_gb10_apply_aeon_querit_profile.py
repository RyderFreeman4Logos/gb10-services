from __future__ import annotations

import json
import os
import re
import signal
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from test_embedding_service_contracts import _logical_directive_argv, _option_values


ROOT = Path(__file__).resolve().parents[1]
DEPLOYER = ROOT / "scripts" / "gb10_apply_aeon_querit_profile.sh"
AEON_UNIT = ROOT / "systemd" / "vllm-aeon-27b-dflash.service"
GIB = 1024 * 1024 * 1024


def _canonical_aeon_docker_profile() -> tuple[list[str], int, int]:
    exec_starts = _logical_directive_argv(AEON_UNIT.read_text(), "ExecStart")
    if len(exec_starts) != 1:
        raise AssertionError(f"expected one AEON ExecStart, found {len(exec_starts)}")
    argv = exec_starts[0]
    if argv[:2] != ["/usr/bin/docker", "run"]:
        raise AssertionError("AEON unit must use the canonical docker run command")
    try:
        image_index = next(
            index
            for index, token in enumerate(argv)
            if token.startswith("ghcr.io/aeon-7/aeon-vllm-ultimate@sha256:")
        )
    except StopIteration as error:
        raise AssertionError("AEON unit has no immutable AEON image") from error
    host_argv = argv[2:image_index]
    command = argv[image_index + 1 :]
    memory = _option_values(host_argv, "--memory")[0]
    memory_swap = _option_values(host_argv, "--memory-swap")[0]
    if not memory.endswith("g") or not memory_swap.endswith("g"):
        raise AssertionError("AEON Docker memory values must use GiB units")
    return command, int(memory[:-1]) * GIB, int(memory_swap[:-1]) * GIB


class QueritDeployerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = DEPLOYER.read_text()

    def _make_fake_stack(
        self,
        tmp: Path,
        *,
        guard_mode: str = "fail",
        docker_mode: str = "ok",
        cgroup_version: str = "2",
        no_swap_fail_at: int = 0,
        aeon_command: list[str] | None = None,
        aeon_memory_bytes: int | None = None,
        aeon_memory_swap_bytes: int | None = None,
        initial_reranker: str = "fallback",
        initial_canaries: bool = False,
        active_canary_without_fragment: str | None = None,
        fragmentless_canary_state: str = "active",
        enabled_canary_without_fragment: str | None = None,
        canary_stop_effective: bool = True,
        canary_stop_fails: bool = False,
        canary_disable_effective: bool = True,
    ) -> tuple[dict[str, str], Path, Path, Path]:
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        calls = tmp / "calls"
        rerank_active = tmp / "rerank-active"
        fallback_active = tmp / "fallback-active"
        rerank_enabled = tmp / "rerank-enabled"
        fallback_enabled = tmp / "fallback-enabled"
        canary_adapter_installed = tmp / "canary-adapter-installed"
        canary_adapter_active = tmp / "canary-adapter-active"
        canary_adapter_enabled = tmp / "canary-adapter-enabled"
        canary_backend_installed = tmp / "canary-backend-installed"
        canary_backend_active = tmp / "canary-backend-active"
        canary_backend_enabled = tmp / "canary-backend-enabled"
        if initial_reranker == "canonical":
            rerank_active.touch()
            rerank_enabled.touch()
        elif initial_reranker == "fallback":
            fallback_active.touch()
            fallback_enabled.touch()
        else:
            raise ValueError(f"unsupported initial reranker: {initial_reranker}")
        if initial_canaries:
            for marker in (
                canary_adapter_installed,
                canary_adapter_active,
                canary_adapter_enabled,
                canary_backend_installed,
                canary_backend_active,
                canary_backend_enabled,
            ):
                marker.touch()
        if active_canary_without_fragment is not None:
            fragmentless_canary_markers = {
                "vllm-querit-4b-canary.service": canary_adapter_active,
                "vllm-querit-4b-canary-backend.service": canary_backend_active,
            }
            try:
                fragmentless_canary_markers[active_canary_without_fragment].write_text(
                    fragmentless_canary_state
                )
            except KeyError as error:
                raise ValueError(
                    "unsupported fragmentless canary: "
                    f"{active_canary_without_fragment}"
                ) from error
        if enabled_canary_without_fragment is not None:
            fragmentless_enabled_markers = {
                "vllm-querit-4b-canary.service": canary_adapter_enabled,
                "vllm-querit-4b-canary-backend.service": canary_backend_enabled,
            }
            try:
                fragmentless_enabled_markers[enabled_canary_without_fragment].touch()
            except KeyError as error:
                raise ValueError(
                    "unsupported fragmentless enabled canary: "
                    f"{enabled_canary_without_fragment}"
                ) from error
        signal_marker = tmp / "guard-score-entered"
        docker_marker = tmp / "docker-entered"
        canonical_command, canonical_memory, canonical_memory_swap = (
            _canonical_aeon_docker_profile()
        )
        aeon_command = canonical_command if aeon_command is None else aeon_command
        aeon_memory_bytes = (
            canonical_memory if aeon_memory_bytes is None else aeon_memory_bytes
        )
        aeon_memory_swap_bytes = (
            canonical_memory_swap
            if aeon_memory_swap_bytes is None
            else aeon_memory_swap_bytes
        )
        command_json = tmp / "aeon-command.json"
        command_json.write_text(json.dumps(aeon_command))

        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            f'printf \'systemctl %s\\n\' "$*" >> {calls}\n'
            'if [[ "$*" == *"show"* ]]; then\n'
            f'  if [[ "$*" == *"vllm-querit-4b-canary-backend.service"* ]]; then [[ -f {canary_backend_installed} ]] && echo loaded || echo not-found; exit 0; fi\n'
            f'  if [[ "$*" == *"vllm-querit-4b-canary.service"* ]]; then [[ -f {canary_adapter_installed} ]] && echo loaded || echo not-found; exit 0; fi\n'
            "  echo loaded; exit 0\n"
            "fi\n"
            'if [[ "$*" == *"is-enabled"* ]]; then\n'
            f'  [[ "$*" == *"vllm-querit-4b-canary-backend.service"* ]] && {{ [[ -f {canary_backend_enabled} ]] && {{ echo enabled; exit 0; }} || {{ echo disabled; exit 1; }}; }}\n'
            f'  [[ "$*" == *"vllm-querit-4b-canary.service"* ]] && {{ [[ -f {canary_adapter_enabled} ]] && {{ echo enabled; exit 0; }} || {{ echo disabled; exit 1; }}; }}\n'
            f'  [[ "$*" == *"vllm-querit-4b-reranker.service"* ]] && {{ [[ -f {rerank_enabled} ]] && {{ echo enabled; exit 0; }} || {{ echo disabled; exit 1; }}; }}\n'
            f'  [[ "$*" == *"vllm-qwen3-reranker-8b.service"* ]] && {{ [[ -f {fallback_enabled} ]] && {{ echo enabled; exit 0; }} || {{ echo disabled; exit 1; }}; }}\n'
            "fi\n"
            'if [[ "$*" == *"is-active"* ]]; then\n'
            f'  if [[ "$*" == *"vllm-querit-4b-canary-backend.service"* ]]; then [[ -f {canary_backend_active} ]] && {{ state=$(cat {canary_backend_active}); echo "${{state:-active}}"; exit 0; }} || {{ echo inactive; exit 3; }}; fi\n'
            f'  if [[ "$*" == *"vllm-querit-4b-canary.service"* ]]; then [[ -f {canary_adapter_active} ]] && {{ state=$(cat {canary_adapter_active}); echo "${{state:-active}}"; exit 0; }} || {{ echo inactive; exit 3; }}; fi\n'
            f'  if [[ "$*" == *"vllm-querit-4b-reranker.service"* ]]; then [[ -f {rerank_active} ]] && {{ echo active; exit 0; }} || {{ echo inactive; exit 3; }}; fi\n'
            f'  if [[ "$*" == *"vllm-qwen3-reranker-8b.service"* ]]; then [[ -f {fallback_active} ]] && {{ echo active; exit 0; }} || {{ echo inactive; exit 3; }}; fi\n'
            "  echo active; exit 0\n"
            "fi\n"
            'if [[ "$*" == *"start vllm-querit-4b-reranker.service"* ]]; then\n'
            f'  : > {rerank_active}\n'
            "fi\n"
            'if [[ "$*" == *"enable vllm-querit-4b-reranker.service"* ]]; then\n'
            f'  : > {rerank_enabled}\n'
            "fi\n"
            'if [[ "$*" == *"stop vllm-querit-4b-reranker.service"* ]]; then\n'
            f'  rm -f {rerank_active}\n'
            "fi\n"
            'if [[ "$*" == *"start vllm-qwen3-reranker-8b.service"* ]]; then\n'
            f'  : > {fallback_active}\n'
            "fi\n"
            'if [[ "$*" == *"stop vllm-qwen3-reranker-8b.service"* ]]; then\n'
            f'  rm -f {fallback_active}\n'
            "fi\n"
            'if [[ "$*" == *"stop vllm-querit-4b-canary.service"* ]]; then\n'
            '  [[ "${FAKE_CANARY_STOP_FAILS:-0}" == 1 ]] && exit 9\n'
            f'  [[ "${{FAKE_CANARY_STOP_EFFECTIVE:-1}}" == 1 ]] && rm -f {canary_adapter_active}\n'
            "fi\n"
            'if [[ "$*" == *"stop vllm-querit-4b-canary-backend.service"* ]]; then\n'
            '  [[ "${FAKE_CANARY_STOP_FAILS:-0}" == 1 ]] && exit 9\n'
            f'  [[ "${{FAKE_CANARY_STOP_EFFECTIVE:-1}}" == 1 ]] && rm -f {canary_backend_active}\n'
            "fi\n"
            'if [[ "$*" == *"disable vllm-querit-4b-reranker.service"* ]]; then\n'
            f'  rm -f {rerank_enabled}\n'
            "fi\n"
            'if [[ "$*" == *"enable vllm-qwen3-reranker-8b.service"* ]]; then\n'
            f'  : > {fallback_enabled}\n'
            "fi\n"
            'if [[ "$*" == *"disable vllm-qwen3-reranker-8b.service"* ]]; then\n'
            f'  rm -f {fallback_enabled}\n'
            "fi\n"
            'if [[ "$*" == *"disable vllm-querit-4b-canary.service"* ]]; then\n'
            f'  [[ "${{FAKE_CANARY_DISABLE_EFFECTIVE:-1}}" == 1 ]] && rm -f {canary_adapter_enabled}\n'
            "fi\n"
            'if [[ "$*" == *"disable vllm-querit-4b-canary-backend.service"* ]]; then\n'
            f'  [[ "${{FAKE_CANARY_DISABLE_EFFECTIVE:-1}}" == 1 ]] && rm -f {canary_backend_enabled}\n'
            "fi\n"
            "exit 0\n"
        )
        systemctl.chmod(0o755)

        docker = fake_bin / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            f'printf \'docker %s\\n\' "$*" >> {calls}\n'
            'if [[ "$*" == "info --format {{.CgroupVersion}}" ]]; then\n'
            '  printf \'%s\\n\' "${FAKE_CGROUP_VERSION-2}"\n'
            '  exit 0\n'
            'fi\n'
            'if [[ "${FAKE_DOCKER_MODE:-ok}" == hang ]]; then\n'
            f"  : > {docker_marker}\n"
            "  /bin/sleep 30\n"
            "fi\n"
            'if [[ "$*" == *"Config.Cmd"* ]]; then\n'
            f"  cat {shlex.quote(str(command_json))}\n"
            'elif [[ "$*" == *"HostConfig.MemorySwap"* ]]; then\n'
            f"  echo {aeon_memory_swap_bytes}\n"
            "else\n"
            f"  echo {aeon_memory_bytes}\n"
            "fi\n"
        )
        docker.chmod(0o755)

        curl = fake_bin / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            f'printf \'curl %s\\n\' "$*" >> {calls}\n'
            'if [[ "$*" == *"/v1/score"* ]]; then\n'
            '  if [[ "${FAKE_GUARD_MODE:-fail}" == fail ]]; then exit 22; fi\n'
            '  if [[ "${FAKE_GUARD_MODE:-fail}" == hang ]]; then\n'
            f"    : > {signal_marker}\n"
            "    /bin/sleep 30\n"
            "  fi\n"
            "  echo '{\"data\":[{\"score\":1.0}]}'\n"
            "  exit 0\n"
            "fi\n"
            'if [[ "$*" == *"/v1/rerank"* ]]; then '
            'echo \'{"results":[{"index":0,"relevance_score":1.0}]}\'; '
            "else echo '{}'; fi\n"
        )
        curl.chmod(0o755)

        python = fake_bin / "python3"
        python.write_text("#!/usr/bin/env bash\necho RAW_RERANK_OK\n")
        python.chmod(0o755)

        no_swap = fake_bin / "gb10_verify_vllm_no_swap.sh"
        no_swap_count = tmp / "no-swap-count"
        no_swap.write_text(
            "#!/usr/bin/env bash\n"
            f"count=$(cat {no_swap_count} 2>/dev/null || printf 0)\n"
            "count=$((count + 1))\n"
            f"printf '%s\\n' \"$count\" > {no_swap_count}\n"
            f"printf 'no-swap %s\\n' \"$*\" >> {calls}\n"
            'if (( count == ${FAKE_NO_SWAP_FAIL_AT:-0} )); then exit 85; fi\n'
        )
        no_swap.chmod(0o755)

        meminfo = tmp / "meminfo"
        meminfo.write_text("MemAvailable: 8388608 kB\n")
        guard_config = tmp / "guard-config.toml"
        guard_config.write_text("original-guard-config\n")
        env = os.environ | {
            "HOME": str(tmp),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TMPDIR": str(tmp),
            "GB10_MEMINFO_PATH": str(meminfo),
            "GB10_GUARD_CONFIG_PATH": str(guard_config),
            "GB10_AEON_READY_ATTEMPTS": "1",
            "GB10_RERANK_READY_ATTEMPTS": "1",
            "FAKE_GUARD_MODE": guard_mode,
            "FAKE_DOCKER_MODE": docker_mode,
            "FAKE_CGROUP_VERSION": cgroup_version,
            "FAKE_NO_SWAP_FAIL_AT": str(no_swap_fail_at),
            "FAKE_CANARY_STOP_EFFECTIVE": "1" if canary_stop_effective else "0",
            "FAKE_CANARY_STOP_FAILS": "1" if canary_stop_fails else "0",
            "FAKE_CANARY_DISABLE_EFFECTIVE": (
                "1" if canary_disable_effective else "0"
            ),
            "GB10_NO_SWAP_HELPER_TEST_PATH": str(no_swap),
            "GB10_QUERIT_PROFILE_TEST_ONLY": "1",
        }
        return env, calls, signal_marker, docker_marker

    def test_validates_the_canonical_aeon_profile_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            env, calls, _, _ = self._make_fake_stack(
                Path(raw_tmp), guard_mode="ok"
            )
            result = subprocess.run(
                ["bash", str(DEPLOYER)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DEPLOY_SUCCESS", result.stdout)
            recorded = calls.read_text().splitlines()
            aeon_no_swap = (
                "no-swap --test-only --unit "
                "/home/obj/.config/systemd/user/vllm-aeon-27b-dflash.service "
                "--container vllm-aeon-27b-dflash-n12"
            )
            self.assertGreaterEqual(recorded.count(aeon_no_swap), 2, recorded)

    def test_rejects_stale_or_noncanonical_aeon_profiles_before_mutation(self) -> None:
        command, memory_bytes, memory_swap_bytes = _canonical_aeon_docker_profile()
        utilization_index = command.index("--gpu-memory-utilization")
        wrong_utilization = [*command]
        wrong_utilization[utilization_index + 1] = "0.354"
        profiles = {
            "explicit KV pin": (
                [*command, "--kv-cache-memory-bytes", "15360M"],
                memory_bytes,
                memory_swap_bytes,
                "AEON_KV_PINNED",
            ),
            "wrong GPU utilization": (
                wrong_utilization,
                memory_bytes,
                memory_swap_bytes,
                "AEON_GPU_UTILIZATION_MISMATCH",
            ),
            "old 69g memory ceiling": (
                command,
                69 * GIB,
                69 * GIB,
                "AEON_MEMORY_MISMATCH",
            ),
            "old 69g memory swap ceiling": (
                command,
                memory_bytes,
                69 * GIB,
                "AEON_MEMORY_SWAP_MISMATCH",
            ),
        }
        for label, (
            candidate_command,
            candidate_memory,
            candidate_memory_swap,
            expected_error,
        ) in profiles.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw_tmp:
                env, calls, _, _ = self._make_fake_stack(
                    Path(raw_tmp),
                    guard_mode="ok",
                    aeon_command=candidate_command,
                    aeon_memory_bytes=candidate_memory,
                    aeon_memory_swap_bytes=candidate_memory_swap,
                )
                result = subprocess.run(
                    ["bash", str(DEPLOYER)],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertNotIn("PHASE switch_reranker", result.stdout)
                self.assertNotIn(
                    "systemctl --user stop vllm-qwen3-reranker-8b.service",
                    calls.read_text().splitlines(),
                )

    def test_preserves_text_and_never_uses_systemctl_restart(self) -> None:
        self.assertNotIn("--restart-aeon", self.source)
        self.assertNotIn('restart "$AEON_UNIT"', self.source)

    def test_production_owner_does_not_restore_canary_deploy_lifecycle(self) -> None:
        for retired_feature in (
            "gb10_querit_canary_deploy.py",
            "gb10_querit_canary_lifecycle.py",
            "querit_canary_transaction.py",
            "18014",
        ):
            self.assertNotIn(retired_feature, self.source)

    def test_upgrade_retires_installed_active_canary_units(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            env, calls, _, _ = self._make_fake_stack(
                Path(raw_tmp), guard_mode="ok", initial_canaries=True
            )
            result = subprocess.run(
                ["bash", str(DEPLOYER)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DEPLOY_SUCCESS", result.stdout)
            recorded = calls.read_text().splitlines()
            for unit in (
                "vllm-querit-4b-canary.service",
                "vllm-querit-4b-canary-backend.service",
            ):
                with self.subTest(unit=unit):
                    self.assertIn(f"systemctl --user stop {unit}", recorded)
                    self.assertIn(f"systemctl --user disable {unit}", recorded)
                    self.assertIn(f"systemctl --user is-active {unit}", recorded)
                    self.assertIn(f"systemctl --user is-enabled {unit}", recorded)

    def test_upgrade_stops_each_active_canary_when_fragment_is_missing(self) -> None:
        for unit in (
            "vllm-querit-4b-canary.service",
            "vllm-querit-4b-canary-backend.service",
        ):
            with self.subTest(unit=unit), tempfile.TemporaryDirectory() as raw_tmp:
                env, calls, _, _ = self._make_fake_stack(
                    Path(raw_tmp),
                    guard_mode="ok",
                    active_canary_without_fragment=unit,
                )
                result = subprocess.run(
                    ["bash", str(DEPLOYER)],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("DEPLOY_SUCCESS", result.stdout)
                recorded = calls.read_text().splitlines()
                self.assertIn(f"systemctl --user is-active {unit}", recorded)
                self.assertIn(f"systemctl --user stop {unit}", recorded)

    def test_fragmentless_retirement_covers_state_and_enablement_axes(self) -> None:
        units = (
            "vllm-querit-4b-canary.service",
            "vllm-querit-4b-canary-backend.service",
        )

        def run_case(**kwargs: Any) -> tuple[subprocess.CompletedProcess[str], list[str]]:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            env, calls, _, _ = self._make_fake_stack(
                Path(temporary.name), guard_mode="ok", **kwargs
            )
            result = subprocess.run(
                ["bash", str(DEPLOYER)], env=env, text=True,
                capture_output=True, timeout=5, check=False,
            )
            return result, calls.read_text().splitlines()

        for unit in units:
            for state in ("activating", "reloading", "deactivating"):
                with self.subTest(unit=unit, state=state):
                    result, calls = run_case(
                        active_canary_without_fragment=unit,
                        fragmentless_canary_state=state,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"systemctl --user stop {unit}", calls)
            with self.subTest(unit=unit, state="fragmentless-enabled"):
                result, calls = run_case(enabled_canary_without_fragment=unit)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"systemctl --user is-enabled {unit}", calls)
                self.assertIn(f"systemctl --user disable {unit}", calls)

        failure_cases = (
            ({"active_canary_without_fragment": units[1],
              "fragmentless_canary_state": "activating",
              "canary_stop_effective": False}, "remains active/non-quiescent"),
            ({"enabled_canary_without_fragment": units[0],
              "canary_disable_effective": False}, "remains enabled"),
        )
        for kwargs, expected_error in failure_cases:
            with self.subTest(expected_error=expected_error):
                result, calls = run_case(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertNotIn("DEPLOY_SUCCESS", result.stdout)
                self.assertNotIn(
                    "systemctl --user start vllm-querit-4b-reranker.service", calls
                )

    def test_upgrade_fails_closed_when_each_fragmentless_canary_stays_active(
        self,
    ) -> None:
        for unit in (
            "vllm-querit-4b-canary.service",
            "vllm-querit-4b-canary-backend.service",
        ):
            with self.subTest(unit=unit), tempfile.TemporaryDirectory() as raw_tmp:
                env, calls, _, _ = self._make_fake_stack(
                    Path(raw_tmp),
                    guard_mode="ok",
                    active_canary_without_fragment=unit,
                    canary_stop_effective=False,
                )
                result = subprocess.run(
                    ["bash", str(DEPLOYER)],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("retired canary unit remains active", result.stderr)
                self.assertNotIn("DEPLOY_SUCCESS", result.stdout)
                recorded = calls.read_text().splitlines()
                self.assertIn(f"systemctl --user is-active {unit}", recorded)
                self.assertIn(f"systemctl --user stop {unit}", recorded)
                self.assertNotIn(
                    "systemctl --user start vllm-querit-4b-reranker.service",
                    recorded,
                )

    def test_upgrade_fails_closed_when_a_canary_remains_active(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            env, calls, _, _ = self._make_fake_stack(
                Path(raw_tmp),
                guard_mode="ok",
                initial_canaries=True,
                canary_stop_effective=False,
            )
            result = subprocess.run(
                ["bash", str(DEPLOYER)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("retired canary unit remains active", result.stderr)
            self.assertNotIn("DEPLOY_SUCCESS", result.stdout)
            recorded = calls.read_text().splitlines()
            self.assertNotIn(
                "systemctl --user start vllm-querit-4b-reranker.service", recorded
            )

    def test_upgrade_fails_closed_when_canary_stop_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            env, calls, _, _ = self._make_fake_stack(
                Path(raw_tmp),
                guard_mode="ok",
                initial_canaries=True,
                canary_stop_fails=True,
            )
            result = subprocess.run(
                ["bash", str(DEPLOYER)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DEPLOY_FAILED", result.stderr)
            recorded = calls.read_text().splitlines()
            self.assertNotIn(
                "systemctl --user start vllm-querit-4b-reranker.service", recorded
            )

    def test_snapshots_and_restores_reranker_state(self) -> None:
        for contract in (
            "RERANK_UNIT=vllm-querit-4b-reranker.service",
            "FALLBACK_UNIT=vllm-qwen3-reranker-8b.service",
            "unit_enabled_state",
            "unit_active_state",
            "rollback_runtime_state",
            'run_systemctl disable "$FALLBACK_UNIT"',
            "restore_unit_enablement",
        ):
            self.assertIn(contract, self.source)
        rollback = re.search(
            r"rollback_runtime_state\(\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        if rollback is None:
            self.fail("rollback_runtime_state function missing")
        body = rollback.group("body")
        self.assertIn('restore_unit_enablement "$RERANK_UNIT"', body)
        self.assertIn('restore_unit_enablement "$FALLBACK_UNIT"', body)
        self.assertIn('run_systemctl_start start "$FALLBACK_UNIT"', body)

    def test_guard_configuration_is_not_mutated_by_the_owner_migration(self) -> None:
        self.assertNotIn("GB10_GUARD_CONFIG_PATH", self.source)
        self.assertNotIn("config/llm-guard-proxy/config.toml", self.source)

    def test_signal_and_exit_paths_share_idempotent_cleanup(self) -> None:
        for contract in (
            "trap 'cleanup_on_signal INT 130' INT",
            "trap 'cleanup_on_signal TERM 143' TERM",
            "trap cleanup_on_exit EXIT",
            "CLEANUP_STARTED=1",
        ):
            self.assertIn(contract, self.source)

    def test_external_commands_have_hard_timeouts(self) -> None:
        self.assertIn("run_systemctl()", self.source)
        self.assertIn("run_docker()", self.source)
        self.assertIn("/usr/bin/timeout --signal=TERM", self.source)
        self.assertNotIn('command_json="$(docker inspect', self.source)

    def test_no_swap_verification_uses_clean_production_environment(self) -> None:
        for contract in (
            "/usr/bin/env -i",
            "DOCKER_HOST=unix:///run/user/1001/docker.sock",
            "/usr/bin/bash --noprofile --norc",
            "NO_SWAP_HELPER=/home/obj/.local/bin/gb10_verify_vllm_no_swap.sh",
            'verify_no_swap "$RERANK_UNIT" "$RERANK_CONTAINER"',
            'verify_no_swap "$EMBEDDING_UNIT" "$EMBEDDING_CONTAINER"',
        ):
            self.assertIn(contract, self.source)

    def test_enables_querit_only_after_readiness_and_smoke(self) -> None:
        ready = self.source.index("RERANK_READY")
        smoke = self.source.index("RAW_RERANK_OK")
        enable = self.source.rindex('run_systemctl enable "$RERANK_UNIT"')
        self.assertLess(ready, smoke)
        self.assertLess(smoke, enable)

    def test_non_v2_docker_cgroup_preflight_fails_before_service_mutation(self) -> None:
        for version in ("1", "unknown", ""):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as raw_tmp:
                env, calls, _, _ = self._make_fake_stack(
                    Path(raw_tmp), cgroup_version=version
                )
                result = subprocess.run(
                    ["bash", str(DEPLOYER)],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                call_text = calls.read_text()
                self.assertIn("docker info --format {{.CgroupVersion}}", call_text)
                for mutation in (" start ", " stop ", " enable ", " disable "):
                    self.assertNotIn(mutation, call_text)

    def test_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(DEPLOYER)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_smoke_failure_restores_previous_reranker_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            env, calls, _, _ = self._make_fake_stack(Path(raw_tmp))
            result = subprocess.run(
                ["bash", str(DEPLOYER)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr.count("DEPLOY_FAILED"), 1, result.stderr)
            recorded = calls.read_text().splitlines()
            self.assertEqual(
                recorded.count("systemctl --user stop vllm-querit-4b-reranker.service"),
                2,
                recorded,
            )
            switched = recorded.index(
                "systemctl --user stop vllm-qwen3-reranker-8b.service"
            )
            started = recorded.index(
                "systemctl --user start vllm-querit-4b-reranker.service"
            )
            stopped = len(recorded) - 1 - recorded[::-1].index(
                "systemctl --user stop vllm-querit-4b-reranker.service"
            )
            disabled = len(recorded) - 1 - recorded[::-1].index(
                "systemctl --user disable vllm-querit-4b-reranker.service"
            )
            expected_restore = (
                "systemctl --user start vllm-qwen3-reranker-8b.service"
            )
            self.assertIn(
                expected_restore,
                recorded,
                f"stdout={result.stdout!r} stderr={result.stderr!r} calls={recorded!r}",
            )
            restored = len(recorded) - 1 - recorded[::-1].index(expected_restore)
            self.assertLess(switched, started)
            self.assertLess(started, stopped)
            self.assertLess(stopped, disabled)
            self.assertLess(disabled, restored)
            self.assertEqual(
                (Path(raw_tmp) / "guard-config.toml").read_text(),
                "original-guard-config\n",
            )


    def test_post_start_no_swap_failure_rolls_back_without_embedding_outage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            env, calls, _, _ = self._make_fake_stack(
                Path(raw_tmp), no_swap_fail_at=4
            )
            result = subprocess.run(
                ["bash", str(DEPLOYER)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            recorded = calls.read_text().splitlines()
            self.assertIn(
                "systemctl --user start vllm-qwen3-reranker-8b.service",
                recorded,
            )
            self.assertNotIn(
                "systemctl --user stop vllm-embedding.service",
                recorded,
            )
            self.assertGreaterEqual(
                sum(line.startswith("no-swap ") for line in recorded), 7
            )

    def test_sigterm_after_switch_rolls_back_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            env, calls, signal_marker, _ = self._make_fake_stack(
                Path(raw_tmp), guard_mode="hang"
            )
            process = subprocess.Popen(
                ["bash", str(DEPLOYER)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            deadline = time.monotonic() + 3
            while not signal_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(signal_marker.exists(), "deployer did not reach Guard smoke")
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, (stdout, stderr))
            self.assertEqual(stderr.count("DEPLOY_FAILED"), 1, stderr)
            recorded = calls.read_text().splitlines()
            self.assertEqual(
                recorded.count("systemctl --user stop vllm-querit-4b-reranker.service"),
                2,
                recorded,
            )
            self.assertIn(
                "systemctl --user start vllm-qwen3-reranker-8b.service",
                recorded,
            )

    def test_hung_docker_inspect_fails_within_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            env, calls, _, docker_marker = self._make_fake_stack(
                Path(raw_tmp), docker_mode="hang"
            )
            env["GB10_DOCKER_TIMEOUT_SECONDS"] = "1"
            started = time.monotonic()
            result = subprocess.run(
                ["bash", str(DEPLOYER)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            elapsed = time.monotonic() - started
            self.assertTrue(docker_marker.exists())
            self.assertNotEqual(result.returncode, 0)
            self.assertLess(elapsed, 4)
            self.assertEqual(result.stderr.count("DEPLOY_FAILED"), 1, result.stderr)
            self.assertNotIn(
                "systemctl --user stop vllm-qwen3-reranker-8b.service",
                calls.read_text().splitlines(),
            )


if __name__ == "__main__":
    unittest.main()
