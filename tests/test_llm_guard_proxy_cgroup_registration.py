from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "scripts" / "llm_guard_proxy_publish_cgroup_registration.sh"
CONFIG = ROOT / "config" / "llm-guard-proxy" / "config.toml"
TEXT_UNIT = ROOT / "systemd" / "vllm-aeon-27b-dflash.service"
PROXY_UNIT = ROOT / "systemd" / "llm-guard-proxy.service"


class IntegratedGuardianRegistrationTests(unittest.TestCase):
    def test_crash_informed_integrated_guardian_is_the_only_automatic_actor(self) -> None:
        guardian = tomllib.loads(CONFIG.read_text())["guardian"]
        self.assertEqual(
            guardian,
            {
                "enabled": True,
                "target_label": "aeon-text",
                "mem_threshold_gib": 5,
                "kill_action": "cgroup-kill",
                "poll_interval_secs": 3,
                "registration_file": "text-cgroup.v1",
                "reserve_mib": 64,
                "retry_interval_secs": 5,
                "cgroup_root": "/sys/fs/cgroup",
            },
        )
        unit = TEXT_UNIT.read_text()
        self.assertIn(PUBLISHER.name, unit)
        self.assertNotIn("gb10_enforce_docker_cgroup_limits", unit)
        proxy_unit = PROXY_UNIT.read_text()
        self.assertIn(
            "--guardian-runtime-dir %t/gb10-memory-guardian", proxy_unit
        )
        self.assertIn("ReadWritePaths=", proxy_unit)
        self.assertIn("/sys/fs/cgroup", proxy_unit)
        for path in (
            ROOT / "systemd" / "gb10-memory-guardian.service",
            ROOT / "systemd" / "gb10-stack-recovery.service",
            ROOT / "systemd" / "aeon-healthcheck.timer",
            ROOT / "systemd" / "gb10-swap-guard.service",
        ):
            self.assertFalse(path.exists(), path)

    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], dict[str, str], Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        runtime = root / "runtime"
        identity = runtime / "gb10-memory-guardian"
        identity.mkdir(parents=True, mode=0o700)
        cid = "a" * 64
        cidfile = identity / "aeon-text.cid"
        cidfile.write_text(f"{cid}\n")
        registration = identity / "text-cgroup.v1"
        uid = os.geteuid()
        scope = f"docker-{cid}.scope"
        control_group = (
            f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{scope}"
        )
        cgroup = root / "cgroup" / control_group.removeprefix("/")
        cgroup.mkdir(parents=True)
        (cgroup / "cgroup.kill").write_text("")
        (cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        systemctl = root / "systemctl"
        systemctl.write_text(f"#!/bin/sh\nprintf '%s\\n' '{control_group}'\n")
        systemctl.chmod(systemctl.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env.update(
            {
                "XDG_RUNTIME_DIR": str(runtime),
                "GB10_CONTAINER_CIDFILE": str(cidfile),
                "GB10_CGROUP_REGISTRATION_PATH": str(registration),
                "GB10_CGROUP_ROOT": str(root / "cgroup"),
                "GB10_SYSTEMCTL_BIN": str(systemctl),
                "GB10_CGROUP_WAIT_SECONDS": "2",
            }
        )
        return temporary, env, registration, systemctl

    def test_publishes_exact_owner_only_registration_without_mutating_cgroup_limits(self) -> None:
        temporary, env, registration, _systemctl = self.fixture()
        self.addCleanup(temporary.cleanup)

        result = subprocess.run(
            [str(PUBLISHER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cid = "a" * 64
        uid = os.geteuid()
        scope = f"docker-{cid}.scope"
        self.assertEqual(
            registration.read_text(),
            "".join(
                (
                    "version=1\n",
                    f"container_id={cid}\n",
                    f"scope={scope}\n",
                    f"control_group=/user.slice/user-{uid}.slice/"
                    f"user@{uid}.service/app.slice/{scope}\n",
                )
            ),
        )
        self.assertEqual(stat.S_IMODE(registration.stat().st_mode), 0o600)
        source = PUBLISHER.read_text()
        self.assertNotIn("set-property", source)
        self.assertNotIn("MemoryMax", source)
        self.assertNotIn("MemorySwapMax", source)

    def test_rejects_a_systemd_control_group_outside_the_exact_docker_scope(self) -> None:
        temporary, env, registration, systemctl = self.fixture()
        self.addCleanup(temporary.cleanup)
        systemctl.write_text("#!/bin/sh\nprintf '%s\\n' '/app.slice/hostile.scope'\n")
        systemctl.chmod(systemctl.stat().st_mode | stat.S_IXUSR)

        result = subprocess.run(
            [str(PUBLISHER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(registration.exists())


if __name__ == "__main__":
    unittest.main()
