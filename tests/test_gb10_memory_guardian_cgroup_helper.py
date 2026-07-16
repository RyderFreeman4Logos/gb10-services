from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CGROUP_HELPER = ROOT / "scripts" / "gb10_enforce_docker_cgroup_limits.sh"
TEXT_UNIT = ROOT / "systemd" / "vllm-aeon-27b-dflash.service"
EMBEDDING_UNIT = ROOT / "systemd" / "vllm-embedding.service"
QUERIT_UNIT = ROOT / "systemd" / "querit-4b-reranker.service"
LEGACY_RERANKER_UNIT = ROOT / "systemd" / "vllm-qwen3-reranker-8b.service"


class RegistrationPublicationFailureTests(unittest.TestCase):
    def test_corrupt_publication_cleans_only_the_exact_launched_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            runtime = root / "runtime"
            cgroup_root = root / "cgroup"
            commands = root / "commands.log"
            bin_dir.mkdir()
            runtime.mkdir()

            container_name = "incident-text-container"
            container_id = "a" * 64
            scope = f"docker-{container_id}.scope"
            control_group = (
                f"/user.slice/user-{os.geteuid()}.slice/"
                f"user@{os.geteuid()}.service/app.slice/{scope}"
            )
            cgroup = cgroup_root / control_group.removeprefix("/")
            cgroup.mkdir(parents=True)
            (cgroup / "memory.swap.max").write_text("0\n")
            (cgroup / "memory.max").write_text(f"{69 * 1024**3}\n")

            docker = bin_dir / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'docker' >>\"$COMMAND_LOG\"\n"
                "printf ' %q' \"$@\" >>\"$COMMAND_LOG\"\n"
                "printf '\\n' >>\"$COMMAND_LOG\"\n"
                "if [[ $1 == inspect && $* == *State.Running* ]]; then "
                "[[ ${FAIL_STOP:-0} == 1 ]] && printf 'true\\n' || printf 'false\\n'; "
                "elif [[ $1 == inspect ]]; then printf '%s\\n' \"$FAKE_CONTAINER_ID\"; "
                "elif [[ $1 == stop && ${FAIL_STOP:-0} == 1 ]]; then exit 1; fi\n"
            )
            docker.chmod(0o755)

            systemctl = bin_dir / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'systemctl' >>\"$COMMAND_LOG\"\n"
                "printf ' %q' \"$@\" >>\"$COMMAND_LOG\"\n"
                "printf '\\n' >>\"$COMMAND_LOG\"\n"
                "if [[ $* == *' show '* || ${1:-} == show ]]; then "
                "printf '%s\\n' \"$FAKE_CONTROL_GROUP\"; fi\n"
            )
            systemctl.chmod(0o755)

            # Simulate a hostile filesystem boundary: rename reports success but
            # the published bytes are not the registration bytes that were written.
            mv = bin_dir / "mv"
            mv.write_text(
                "#!/usr/bin/env bash\n"
                "destination=\"${@: -1}\"\n"
                "if [[ ${CORRUPT_MV:-0} == 1 ]]; then\n"
                "  printf 'version=1\\ncontainer_id=corrupt\\n' >\"$destination\"\n"
                "  rm -f -- \"${@: -2:1}\"\n"
                "else\n"
                "  /usr/bin/mv \"$@\"\n"
                "fi\n"
            )
            mv.chmod(0o755)

            registration = runtime / "gb10-memory-guardian" / "text-cgroup.v1"
            registration.parent.mkdir(parents=True)
            cidfile = registration.parent / "aeon-text.cid"
            cidfile.write_text(f"{container_id}\n")
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{bin_dir}:{env['PATH']}",
                    "COMMAND_LOG": str(commands),
                    "FAKE_CONTAINER_ID": container_id,
                    "FAKE_CONTROL_GROUP": control_group,
                    "XDG_RUNTIME_DIR": str(runtime),
                    "GB10_CGROUP_REGISTRATION_PATH": str(registration),
                    "GB10_CONTAINER_CIDFILE": str(cidfile),
                    "GB10_CGROUP_WAIT_SECONDS": "1",
                    "GB10_DOCKER_BIN": str(docker),
                    "GB10_SYSTEMCTL_BIN": str(systemctl),
                    "GB10_CGROUP_ROOT": str(cgroup_root),
                    "CORRUPT_MV": "1",
                }
            )
            result = subprocess.run(
                ["bash", str(CGROUP_HELPER), "--publish-registration", container_name, "69"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            command_lines = commands.read_text().splitlines() if commands.exists() else []
            self.assertIn(container_id, "\n".join(command_lines))
            self.assertTrue(registration.exists())
            self.assertFalse(cidfile.exists())
            for broad_action in ("docker kill", "docker rm", "docker ps"):
                self.assertFalse(
                    any(line.startswith(broad_action) for line in command_lines),
                    command_lines,
                )

            # Rejecting the publication path is also a post-launch failure.
            # Cleanup must still use the immutable cidfile identity, never the
            # reusable container name.
            commands.write_text("")
            cidfile.write_text(f"{container_id}\n")
            env["GB10_CGROUP_REGISTRATION_PATH"] = str(
                root / "outside-runtime" / "text-cgroup.v1"
            )
            rejected = subprocess.run(
                ["bash", str(CGROUP_HELPER), "--publish-registration", container_name, "69"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            rejected_lines = commands.read_text().splitlines()
            self.assertFalse(any(container_name in line for line in rejected_lines), rejected_lines)
            self.assertFalse(
                any(line == f"docker stop --time 5 {container_name}" for line in rejected_lines),
                rejected_lines,
            )

            # A TERM-resistant launched container escalates only to an exact-CID
            # kill; no name-based or fleet-wide cleanup is permitted.
            commands.write_text("")
            cidfile.write_text(f"{container_id}\n")
            env["GB10_CGROUP_REGISTRATION_PATH"] = str(registration)
            env["FAIL_STOP"] = "1"
            resistant = subprocess.run(
                ["bash", str(CGROUP_HELPER), "--publish-registration", container_name, "69"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(resistant.returncode, 0)
            resistant_lines = commands.read_text().splitlines()
            self.assertIn(f"docker stop --time 5 {container_id}", resistant_lines)
            self.assertIn(f"docker kill {container_id}", resistant_lines)
            self.assertFalse(
                any(container_name in line for line in resistant_lines), resistant_lines
            )

            # The unchanged success path publishes the validated bytes and
            # never invokes cleanup.
            commands.write_text("")
            env.pop("FAIL_STOP")
            env["CORRUPT_MV"] = "0"
            env["GB10_CGROUP_REGISTRATION_PATH"] = str(registration)
            successful = subprocess.run(
                ["bash", str(CGROUP_HELPER), "--publish-registration", container_name, "69"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(successful.returncode, 0, successful.stdout + successful.stderr)
            self.assertEqual(
                registration.read_text(),
                "version=1\n"
                f"container_id={container_id}\n"
                f"scope={scope}\n"
                f"control_group={control_group}\n",
            )
            success_lines = commands.read_text().splitlines()
            self.assertFalse(
                any(line.startswith(("docker stop ", "docker kill ")) for line in success_lines),
                success_lines,
            )


class ExactCleanupTriStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.bin = self.root / "bin"
        self.cgroup_root = self.root / "cgroup"
        self.commands = self.root / "commands.log"
        self.state = self.root / "docker-state"
        self.bin.mkdir()
        self.runtime.mkdir()
        self.container_id = "c" * 64
        self.scope = f"docker-{self.container_id}.scope"
        self.control_group = (
            f"/user.slice/user-{os.geteuid()}.slice/"
            f"user@{os.geteuid()}.service/app.slice/{self.scope}"
        )
        self.cgroup = self.cgroup_root / self.control_group.removeprefix("/")
        self.cgroup.mkdir(parents=True)
        (self.cgroup / "cgroup.kill").write_text("")
        (self.cgroup / "cgroup.events").write_text("populated 1\n")
        identity_dir = self.runtime / "gb10-memory-guardian"
        identity_dir.mkdir()
        self.registration = identity_dir / "text-cgroup.v1"
        self.cidfile = identity_dir / "aeon-text.cid"
        self.registration.write_text(
            "version=1\n"
            f"container_id={self.container_id}\n"
            f"scope={self.scope}\n"
            f"control_group={self.control_group}\n"
        )
        self.cidfile.write_text(f"{self.container_id}\n")
        self._write_fake_docker()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_fake_docker(self) -> None:
        docker = self.bin / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as sink:\n"
            "    sink.write('docker ' + ' '.join(args) + '\\n')\n"
            "mode = os.environ.get('DOCKER_MODE', 'stopped')\n"
            "state = Path(os.environ['DOCKER_STATE'])\n"
            "if args[0] == 'inspect':\n"
            "    rewrite_path = os.environ.get('REWRITE_REGISTRATION_PATH')\n"
            "    rewrite_cid = os.environ.get('REWRITE_REGISTRATION_CID')\n"
            "    if rewrite_path and rewrite_cid:\n"
            "        scope = 'docker-' + rewrite_cid + '.scope'\n"
            "        control_group = (f'/user.slice/user-{os.geteuid()}.slice/'\n"
            "            f'user@{os.geteuid()}.service/app.slice/{scope}')\n"
            "        Path(rewrite_path).write_text('version=1\\n' +\n"
            "            f'container_id={rewrite_cid}\\nscope={scope}\\n' +\n"
            "            f'control_group={control_group}\\n')\n"
            "    if mode == 'absent':\n"
            "        print('Error: No such object: ' + args[-1], file=sys.stderr)\n"
            "        raise SystemExit(1)\n"
            "    if mode == 'unknown':\n"
            "        print('Cannot connect to the Docker daemon', file=sys.stderr)\n"
            "        raise SystemExit(1)\n"
            "    print('false' if state.exists() or mode == 'stopped' else 'true')\n"
            "elif args[0] == 'stop':\n"
            "    if os.environ.get('FAIL_STOP') == '1':\n"
            "        print('transport failure', file=sys.stderr)\n"
            "        raise SystemExit(1)\n"
            "    state.write_text('stopped')\n"
            "elif args[0] == 'kill':\n"
            "    if os.environ.get('FAIL_KILL') == '1':\n"
            "        print('transport failure', file=sys.stderr)\n"
            "        raise SystemExit(1)\n"
            "    state.write_text('stopped')\n"
            "else:\n"
            "    raise SystemExit(2)\n"
        )
        docker.chmod(0o755)

    def _run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_RUNTIME_DIR": str(self.runtime),
                "GB10_CGROUP_REGISTRATION_PATH": str(self.registration),
                "GB10_CONTAINER_CIDFILE": str(self.cidfile),
                "GB10_DOCKER_BIN": str(self.bin / "docker"),
                "GB10_CGROUP_ROOT": str(self.cgroup_root),
                "GB10_DOCKER_TIMEOUT_SECONDS": "1",
                "GB10_CGROUP_WAIT_SECONDS": "1",
                "COMMAND_LOG": str(self.commands),
                "DOCKER_STATE": str(self.state),
                **overrides,
            }
        )
        return subprocess.run(
            ["bash", str(CGROUP_HELPER), "--cleanup-registration"],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def _assert_identity_retained(self) -> None:
        self.assertTrue(self.registration.exists())
        self.assertTrue(self.cidfile.exists())

    def test_missing_or_malformed_cid_never_erases_registration(self) -> None:
        self.cidfile.unlink()
        missing = self._run(DOCKER_MODE="absent")
        self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)
        self.assertTrue(self.registration.exists())

        self.cidfile.write_text("not-a-container-id\n")
        malformed = self._run(DOCKER_MODE="absent")
        self.assertNotEqual(malformed.returncode, 0, malformed.stdout + malformed.stderr)
        self._assert_identity_retained()

    def test_docker_unknown_stop_and_kill_failures_retain_identity(self) -> None:
        scenarios = (
            {"DOCKER_MODE": "unknown"},
            {"DOCKER_MODE": "running", "FAIL_STOP": "1", "FAIL_KILL": "1"},
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                (self.cgroup / "cgroup.events").write_text("populated 1\n")
                result = self._run(**scenario)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self._assert_identity_retained()

    def test_exact_cgroup_fallback_removes_identity_only_after_empty_proof(self) -> None:
        (self.cgroup / "cgroup.events").write_text("populated 0\n")
        result = self._run(DOCKER_MODE="unknown")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.cgroup / "cgroup.kill").read_text(), "1")
        self.assertFalse(self.registration.exists())
        self.assertFalse(self.cidfile.exists())

    def test_confirmed_docker_absence_removes_exact_identity(self) -> None:
        result = self._run(DOCKER_MODE="absent")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.registration.exists())
        self.assertFalse(self.cidfile.exists())

    def test_invalid_registration_destination_still_cleans_exact_cid_with_docker(self) -> None:
        outside = self.root / "outside-runtime" / "text-cgroup.v1"
        outside.parent.mkdir()
        outside.write_text("outside identity must survive\n")
        result = self._run(
            DOCKER_MODE="running",
            GB10_CGROUP_REGISTRATION_PATH=str(outside),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.commands.read_text().splitlines() if self.commands.exists() else []
        self.assertEqual(
            lines,
            [
                f"docker inspect -f {{{{.State.Running}}}} {self.container_id}",
                f"docker stop --time 5 {self.container_id}",
                f"docker inspect -f {{{{.State.Running}}}} {self.container_id}",
            ],
        )
        self.assertEqual(outside.read_text(), "outside identity must survive\n")
        self.assertFalse(self.cidfile.exists())
        self.assertEqual((self.cgroup / "cgroup.kill").read_text(), "")

    def test_mismatched_registration_b_survives_after_cid_a_is_proved_absent(self) -> None:
        replacement_id = "d" * 64
        replacement_scope = f"docker-{replacement_id}.scope"
        replacement_control_group = (
            f"/user.slice/user-{os.geteuid()}.slice/"
            f"user@{os.geteuid()}.service/app.slice/{replacement_scope}"
        )
        replacement = (
            "version=1\n"
            f"container_id={replacement_id}\n"
            f"scope={replacement_scope}\n"
            f"control_group={replacement_control_group}\n"
        )
        self.registration.write_text(replacement)
        result = self._run(DOCKER_MODE="absent")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.registration.read_text(), replacement)
        self.assertFalse(self.cidfile.exists())
        self.assertEqual((self.cgroup / "cgroup.kill").read_text(), "")
        self.assertEqual(
            self.commands.read_text().splitlines(),
            [f"docker inspect -f {{{{.State.Running}}}} {self.container_id}"],
        )

    def test_matching_registration_rewritten_during_docker_proof_survives(self) -> None:
        replacement_id = "f" * 64
        replacement_scope = f"docker-{replacement_id}.scope"
        replacement_control_group = (
            f"/user.slice/user-{os.geteuid()}.slice/"
            f"user@{os.geteuid()}.service/app.slice/{replacement_scope}"
        )
        replacement = (
            "version=1\n"
            f"container_id={replacement_id}\n"
            f"scope={replacement_scope}\n"
            f"control_group={replacement_control_group}\n"
        )
        result = self._run(
            DOCKER_MODE="absent",
            REWRITE_REGISTRATION_PATH=str(self.registration),
            REWRITE_REGISTRATION_CID=replacement_id,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.registration.read_text(), replacement)
        self.assertFalse(self.cidfile.exists())
        self.assertEqual((self.cgroup / "cgroup.kill").read_text(), "")

class CapOnlyEnforcementTests(unittest.TestCase):
    # Embedding no longer uses cgroup enforce — it uses --enforce-eager (stable
    # memory) and a readiness poll instead (v0.25 image starts too slowly for
    # the cap-only docker inspect race).
    CALLERS = (
        (QUERIT_UNIT, "querit-4b-reranker", 18),
        (LEGACY_RERANKER_UNIT, "vllm-qwen3-reranker-8b", 24),
    )

    def test_all_cap_only_callers_set_only_the_exact_cid_scope(self) -> None:
        for unit_path, name, gib in self.CALLERS:
            with self.subTest(unit=unit_path.name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runtime = root / "runtime"
                    cgroup_root = root / "cgroup"
                    bin_dir = root / "bin"
                    commands = root / "commands.log"
                    runtime.mkdir()
                    bin_dir.mkdir()
                    container_id = "e" * 64
                    scope = f"docker-{container_id}.scope"
                    control_group = (
                        f"/user.slice/user-{os.geteuid()}.slice/"
                        f"user@{os.geteuid()}.service/app.slice/{scope}"
                    )
                    cgroup = cgroup_root / control_group.removeprefix("/")
                    cgroup.mkdir(parents=True)
                    (cgroup / "memory.swap.max").write_text("0\n")
                    (cgroup / "memory.max").write_text(f"{gib * 1024**3}\n")

                    docker = bin_dir / "docker"
                    docker.write_text(
                        "#!/usr/bin/env python3\n"
                        "import os, sys\n"
                        "args = sys.argv[1:]\n"
                        "with open(os.environ['COMMAND_LOG'], 'a') as sink:\n"
                        "    sink.write('docker ' + ' '.join(args) + '\\n')\n"
                        "if args[0] != 'inspect' or args[-1] != os.environ['CONTAINER_NAME']:\n"
                        "    raise SystemExit(2)\n"
                        "print(os.environ['FAKE_CONTAINER_ID'])\n"
                    )
                    docker.chmod(0o755)
                    systemctl = bin_dir / "systemctl"
                    systemctl.write_text(
                        "#!/usr/bin/env python3\n"
                        "import os, sys\n"
                        "args = sys.argv[1:]\n"
                        "with open(os.environ['COMMAND_LOG'], 'a') as sink:\n"
                        "    sink.write('systemctl ' + ' '.join(args) + '\\n')\n"
                        "if 'show' in args:\n"
                        "    print(os.environ['FAKE_CONTROL_GROUP'])\n"
                    )
                    systemctl.chmod(0o755)

                    env = os.environ.copy()
                    env.pop("GB10_CGROUP_REGISTRATION_PATH", None)
                    env.pop("GB10_CONTAINER_CIDFILE", None)
                    env.update(
                        {
                            "XDG_RUNTIME_DIR": str(runtime),
                            "GB10_DOCKER_BIN": str(docker),
                            "GB10_SYSTEMCTL_BIN": str(systemctl),
                            "GB10_CGROUP_ROOT": str(cgroup_root),
                            "GB10_CGROUP_WAIT_SECONDS": "1",
                            "GB10_DOCKER_TIMEOUT_SECONDS": "1",
                            "GB10_SYSTEMCTL_TIMEOUT_SECONDS": "1",
                            "COMMAND_LOG": str(commands),
                            "CONTAINER_NAME": name,
                            "FAKE_CONTAINER_ID": container_id,
                            "FAKE_CONTROL_GROUP": control_group,
                        }
                    )
                    result = subprocess.run(
                        ["bash", str(CGROUP_HELPER), name, str(gib)],
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    lines = commands.read_text().splitlines()
                    self.assertEqual(
                        [line for line in lines if line.startswith("docker ")],
                        [f"docker inspect -f {{{{.Id}}}} {name}"],
                    )
                    self.assertIn(
                        f"systemctl --user show -p ControlGroup --value {scope}", lines
                    )
                    self.assertIn(
                        f"systemctl --user set-property --runtime {scope} "
                        f"MemoryMax={gib}G MemorySwapMax=0",
                        lines,
                    )
                    self.assertFalse(
                        any(" stop " in f" {line} " or " kill " in f" {line} " for line in lines),
                        lines,
                    )
                    self.assertFalse((runtime / "gb10-memory-guardian").exists())
                    self.assertEqual((cgroup / "memory.swap.max").read_text(), "0\n")
                    self.assertEqual(
                        (cgroup / "memory.max").read_text(), f"{gib * 1024**3}\n"
                    )

    def test_cap_only_rejects_a_non_unique_or_malformed_container_id(self) -> None:
        for output in ("short-id\n", f"{'a' * 64}\n{'b' * 64}\n"):
            with self.subTest(output=output):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runtime = root / "runtime"
                    identity_dir = runtime / "gb10-memory-guardian"
                    cgroup_root = root / "cgroup"
                    identity_dir.mkdir(parents=True)
                    container_id = "a" * 64
                    scope = f"docker-{container_id}.scope"
                    control_group = (
                        f"/user.slice/user-{os.geteuid()}.slice/"
                        f"user@{os.geteuid()}.service/app.slice/{scope}"
                    )
                    cgroup = cgroup_root / control_group.removeprefix("/")
                    cgroup.mkdir(parents=True)
                    (cgroup / "memory.swap.max").write_text("0\n")
                    (cgroup / "memory.max").write_text(f"{1024**3}\n")
                    registration = identity_dir / "text-cgroup.v1"
                    cidfile = identity_dir / "aeon-text.cid"
                    cidfile.write_text(f"{container_id}\n")

                    docker = root / "docker"
                    docker.write_text(
                        "#!/usr/bin/env bash\n"
                        "printf '%s' \"$FAKE_INSPECT_OUTPUT\"\n"
                    )
                    docker.chmod(0o755)
                    systemctl = root / "systemctl"
                    systemctl.write_text(
                        "#!/usr/bin/env bash\n"
                        "if [[ $* == *' show '* || ${1:-} == show ]]; then "
                        "printf '%s\\n' \"$FAKE_CONTROL_GROUP\"; fi\n"
                    )
                    systemctl.chmod(0o755)
                    env = os.environ.copy()
                    env.update(
                        {
                            "XDG_RUNTIME_DIR": str(runtime),
                            "GB10_CGROUP_REGISTRATION_PATH": str(registration),
                            "GB10_CONTAINER_CIDFILE": str(cidfile),
                            "GB10_DOCKER_BIN": str(docker),
                            "GB10_SYSTEMCTL_BIN": str(systemctl),
                            "GB10_CGROUP_ROOT": str(cgroup_root),
                            "GB10_CGROUP_WAIT_SECONDS": "1",
                            "GB10_DOCKER_TIMEOUT_SECONDS": "1",
                            "GB10_SYSTEMCTL_TIMEOUT_SECONDS": "1",
                            "FAKE_INSPECT_OUTPUT": output,
                            "FAKE_CONTROL_GROUP": control_group,
                        }
                    )
                    result = subprocess.run(
                        ["bash", str(CGROUP_HELPER), "cap-only", "1"],
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(registration.exists())
                    self.assertEqual(cidfile.read_text(), f"{container_id}\n")


class CgroupHelperStructureTests(unittest.TestCase):
    def test_cgroup_helper_modes_and_registration_contract_are_explicit(self) -> None:
        helper = CGROUP_HELPER.read_text()
        text_unit = TEXT_UNIT.read_text()
        self.assertIn("GB10_CGROUP_REGISTRATION_PATH", helper)
        self.assertIn("GB10_CONTAINER_CIDFILE", helper)
        self.assertIn("GB10_CONTAINER_CIDFILE", text_unit)
        self.assertIn("--cidfile=", text_unit)
        self.assertIn("container_id=", helper)
        self.assertIn("control_group=", helper)
        self.assertRegex(helper, r"mktemp|\.tmp")
        self.assertRegex(helper, r"mv\s")
        self.assertIn('chmod 0600 "$registration_tmp"', helper)
        self.assertIn("fail_closed_registration", helper)
        self.assertRegex(helper, r"run_docker stop")
        self.assertIn("^[0-9a-f]{64}$", helper)
        self.assertIn("app.slice/${scope}", helper)
        self.assertIn("--publish-registration", helper)
        self.assertIn("--cleanup-registration", helper)
        self.assertIn(
            "gb10_enforce_docker_cgroup_limits.sh --publish-registration "
            "vllm-aeon-27b-dflash-n12 69",
            text_unit,
        )
        cleanup_trap = helper.index("trap fail_closed_registration EXIT")
        self.assertLess(
            cleanup_trap,
            helper.index("acquire_launch_cid", cleanup_trap),
            "the cleanup trap must be installed before immutable CID acquisition",
        )
        self.assertNotIn('run_docker stop --time 5 "$name"', helper)
        cleanup = helper.split("cleanup_exact_identity()", 1)[1].split(
            "fail_closed_registration()", 1
        )[0]
        self.assertNotIn("|| true", cleanup)
        self.assertNotIn("|| exit 0", text_unit)
        self.assertNotIn("ExecStopPost=/bin/bash", text_unit)
        self.assertNotRegex(text_unit, r"(?m)^ExecStopPost=/usr/bin/rm ")
        self.assertGreaterEqual(text_unit.count("--cleanup-registration"), 2)
        for hardcoded_target in (
            "querit-4b-reranker",
            "vllm-aeon-27b-dflash",
            "vllm-embedding",
            "vllm-qwen3-reranker",
        ):
            self.assertNotIn(hardcoded_target, helper.lower())

    def test_only_text_publishes_and_all_other_callers_remain_cap_only(self) -> None:
        text = TEXT_UNIT.read_text()
        self.assertIn("--publish-registration", text)
        for path, name, gib in CapOnlyEnforcementTests.CALLERS:
            unit = path.read_text()
            invocation = f"gb10_enforce_docker_cgroup_limits.sh {name} {gib}"
            self.assertIn(invocation, unit)
            self.assertNotIn("--publish-registration", unit)
            self.assertNotIn("GB10_CGROUP_REGISTRATION_PATH", unit)
            self.assertNotIn("GB10_CONTAINER_CIDFILE", unit)


if __name__ == "__main__":
    unittest.main()
