from __future__ import annotations

import json
import subprocess
from pathlib import Path

from vllm_no_swap_fixtures import VERIFIER, VllmNoSwapFixture


DFLASH_CONTAINERS = (
    "vllm-aeon-27b-dflash-n12",
    "vllm-aeon-27b-dflash-hikv",
    "vllm-aeon-27b-dflash",
)


class VllmNoSwapCleanupTests(VllmNoSwapFixture):
    def _seed_cleanup(self, *, cid: str, name: str = "vllm-test") -> Path:
        cidfile = self.root / "runtime" / "vllm-test.cid"
        cidfile.parent.mkdir(mode=0o700, exist_ok=True)
        cidfile.write_text(f"{cid}\n")
        cidfile.chmod(0o600)
        fixture_name = name if name in self.identifiers else "vllm-test"
        payload = self._inspect(fixture_name, identifier=cid)
        payload["Name"] = f"/{name}"
        self.cleanup_state.write_text(
            json.dumps(
                {
                    "names": {name: cid},
                    "objects": {cid: payload},
                    "removed": [],
                    "stopped": [],
                },
                sort_keys=True,
            )
        )
        return cidfile

    def _run_cleanup(
        self, cidfile: Path, *, names: tuple[str, ...] = ("vllm-test",)
    ) -> subprocess.CompletedProcess[str]:
        environment = self._test_environment(docker_mode="cleanup")
        argv = [
            "/usr/bin/env",
            "-i",
            *[f"{key}={value}" for key, value in environment.items()],
            "/usr/bin/bash",
            "--noprofile",
            "--norc",
            str(VERIFIER),
            "--test-only",
            "--cleanup",
        ]
        for name in names:
            argv.extend(["--container", name])
        argv.extend(["--cidfile", str(cidfile)])
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )

    def test_cleanup_validates_full_cid_and_name_before_bounded_stop_remove(self) -> None:
        identifier = self.identifiers["vllm-test"]
        cidfile = self._seed_cleanup(cid=identifier)
        result = self._run_cleanup(cidfile)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(self.cleanup_state.read_text())
        self.assertEqual(state["stopped"], [identifier])
        self.assertEqual(state["removed"], [identifier])
        self.assertFalse(cidfile.exists())
        log = self.command_log.read_text()
        self.assertIn(f"docker stop --time 20 {identifier}", log)
        self.assertIn(f"docker rm -f {identifier}", log)
        second = self._run_cleanup(cidfile)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_reboot_retains_cid_authority_for_a_retained_text_container(self) -> None:
        from vllm_no_swap_fixtures import ROOT
        from test_vllm_no_swap_unit_contracts import _logical_argv

        unit = (ROOT / "profile/aeon-ultimate-uncensored-nvfp4"
                / "vllm-aeon-ultimate-uncensored-nvfp4.service").read_text()
        start = _logical_argv(unit, "ExecStart")[0]
        configured = next(arg.split("=", 1)[1] for arg in start if arg.startswith("--cidfile="))
        self.assertNotIn("--rm", start)
        self.assertTrue(configured.startswith("/home/obj/.local/state/"))
        identifier = self.identifiers["vllm-test"]
        volatile = self._seed_cleanup(cid=identifier)
        durable = self.root / configured.removeprefix("/home/obj/")
        durable.parent.mkdir(parents=True, mode=0o700)
        volatile.rename(durable)
        volatile.parent.rmdir()  # Reboot discards runtime state, not Docker metadata.
        state = json.loads(self.cleanup_state.read_text())
        state["objects"][identifier]["State"].update(Running=False, Status="exited", Pid=0)
        self.cleanup_state.write_text(json.dumps(state))
        volatile.parent.mkdir(mode=0o700)
        lost_authority = self._run_cleanup(volatile)
        self.assertNotEqual(lost_authority.returncode, 0)
        self.assertIn("without its private cidfile authority", lost_authority.stderr)
        self.assertEqual(json.loads(self.cleanup_state.read_text())["removed"], [])
        result = self._run_cleanup(durable)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.cleanup_state.read_text())["removed"], [identifier])
        self.assertFalse(durable.exists())

    def test_cleanup_accepts_each_approved_dflash_generation_from_one_allowlist(self) -> None:
        identifier = self.identifiers["vllm-test"]
        for name in DFLASH_CONTAINERS:
            with self.subTest(name=name):
                self.command_log.unlink(missing_ok=True)
                cidfile = self._seed_cleanup(cid=identifier, name=name)
                result = self._run_cleanup(cidfile, names=DFLASH_CONTAINERS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                state = json.loads(self.cleanup_state.read_text())
                self.assertEqual(state["stopped"], [identifier])
                self.assertEqual(state["removed"], [identifier])
                self.assertFalse(cidfile.exists())

    def test_cleanup_allowlist_rejects_an_unapproved_generation(self) -> None:
        identifier = self.identifiers["vllm-test"]
        cidfile = self._seed_cleanup(cid=identifier, name="vllm-unapproved")
        result = self._run_cleanup(cidfile, names=DFLASH_CONTAINERS)
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text() if self.command_log.exists() else ""
        self.assertNotIn("docker stop", log)
        self.assertNotIn("docker rm", log)

    def test_cleanup_allowlist_rejects_multiple_approved_generations(self) -> None:
        identifier = self.identifiers["vllm-test"]
        cidfile = self._seed_cleanup(cid=identifier, name=DFLASH_CONTAINERS[0])
        state = json.loads(self.cleanup_state.read_text())
        second_identifier = self.identifiers["vllm-second"]
        second = self._inspect("vllm-second", identifier=second_identifier)
        second["Name"] = f"/{DFLASH_CONTAINERS[1]}"
        state["names"][DFLASH_CONTAINERS[1]] = second_identifier
        state["objects"][second_identifier] = second
        self.cleanup_state.write_text(json.dumps(state, sort_keys=True))

        result = self._run_cleanup(cidfile, names=DFLASH_CONTAINERS)
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text()
        self.assertNotIn("docker stop", log)
        self.assertNotIn("docker rm", log)

    def test_cleanup_fails_closed_on_malformed_stale_or_replacement_authority(self) -> None:
        identifier = self.identifiers["vllm-test"]
        cidfile = self._seed_cleanup(cid=identifier)
        cidfile.write_text("short-id\n")
        result = self._run_cleanup(cidfile)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(
            "docker stop",
            self.command_log.read_text() if self.command_log.exists() else "",
        )

        self.command_log.unlink(missing_ok=True)
        cidfile.write_text(identifier + "\n")
        replacement = "c" * 64
        replacement_payload = self._inspect("vllm-test", identifier=replacement)
        self.cleanup_state.write_text(
            json.dumps(
                {
                    "names": {"vllm-test": replacement},
                    "objects": {replacement: replacement_payload},
                    "removed": [],
                    "stopped": [],
                },
                sort_keys=True,
            )
        )
        result = self._run_cleanup(cidfile)
        self.assertNotEqual(result.returncode, 0)
        log = self.command_log.read_text()
        self.assertNotIn("docker stop", log)
        self.assertNotIn("docker rm", log)

        self.command_log.unlink(missing_ok=True)
        cidfile.unlink()
        result = self._run_cleanup(cidfile)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("docker stop", self.command_log.read_text())

    def test_failed_verification_can_be_contained_by_generation_bound_cleanup(self) -> None:
        scope = self.cgroup_root / self.scopes["vllm-test"].removeprefix("/")
        (scope / "memory.swap.current").write_text("1\n")
        self.assert_rejected()
        cidfile = self._seed_cleanup(cid=self.identifiers["vllm-test"])
        result = self._run_cleanup(cidfile)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(self.cleanup_state.read_text())
        self.assertEqual(state["objects"], {})
        self.assertEqual(state["names"], {})
