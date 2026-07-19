from __future__ import annotations

import json
import subprocess
from pathlib import Path

from vllm_no_swap_fixtures import VERIFIER, VllmNoSwapFixture


class VllmNoSwapCleanupTests(VllmNoSwapFixture):
    def _seed_cleanup(self, *, cid: str, name: str = "vllm-test") -> Path:
        cidfile = self.root / "runtime" / "vllm-test.cid"
        cidfile.parent.mkdir(mode=0o700)
        cidfile.write_text(f"{cid}\n")
        cidfile.chmod(0o600)
        payload = self._inspect(name, identifier=cid)
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
        self, cidfile: Path, *, name: str = "vllm-test"
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
            "--container",
            name,
            "--cidfile",
            str(cidfile),
        ]
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
