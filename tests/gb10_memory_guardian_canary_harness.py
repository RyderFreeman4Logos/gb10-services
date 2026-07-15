from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "scripts" / "gb10_memory_guardian_canary.sh"
TEXT_UNIT = "vllm-aeon-27b-dflash.service"
GUARDIAN_UNIT = "gb10-memory-guardian.service"
TEXT_INVOCATION = "1" * 32
GUARDIAN_INVOCATION = "2" * 32


class CanaryHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.runtime = self.root / "runtime"
        self.bin = self.root / "bin"
        self.states = self.root / "states"
        self.cgroup_root = self.root / "cgroup"
        self.commands = self.root / "commands.log"
        self.guardian_invocations = self.root / "guardian-invocations.log"
        self.journal = self.root / "journal.log"
        for directory in (self.home, self.runtime, self.bin, self.states, self.cgroup_root):
            directory.mkdir(parents=True)

        self.guardian = self.bin / "gb10-memory-guardian"
        self.guardian.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >>\"$GUARDIAN_INVOCATIONS\"\n"
        )
        self.guardian.chmod(0o755)
        self._write_fake_systemctl()
        self._write_fake_systemd_run()
        self._write_fake_journalctl()
        self._write_config("aeon-text", "text-cgroup.v1")
        self._write_registration("text-cgroup.v1")
        self._write_cgroup()
        self._write_guardian_status("armed")
        self._write_standard_states()
        self.journal.write_text("gb10-memory-guardian: armed target aeon-text\n")
        stamp = self.runtime / "gb10-memory-guardian" / "disposable-canary.passed"
        stamp.write_text(
            f"binary_sha256={hashlib.sha256(self.guardian.read_bytes()).hexdigest()}\n"
            f"passed_epoch={int(time.time())}\n"
        )
        stamp.chmod(0o600)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_fake_systemctl(self) -> None:
        script = self.bin / "systemctl"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "root = Path(os.environ['STATE_ROOT'])\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as sink:\n"
            "    sink.write('systemctl ' + ' '.join(args) + '\\n')\n"
            "while args and args[0].startswith('--'):\n"
            "    args.pop(0)\n"
            "if not args:\n"
            "    raise SystemExit(2)\n"
            "action = args.pop(0)\n"
            "unit = next((arg for arg in args if arg.endswith('.service')), '')\n"
            "if action == 'show':\n"
            "    if unit == 'gb10-memory-guardian-disposable-canary.service':\n"
            "        suffix = 'after' if (root / 'driver-started').exists() else 'before'\n"
            "        path = root / f'{unit}.{suffix}.show'\n"
            "    else:\n"
            "        count_path = root / f'{unit}.show-count'\n"
            "        count = int(count_path.read_text()) if count_path.exists() else 0\n"
            "        count_path.write_text(str(count + 1))\n"
            "        after = root / f'{unit}.after.show'\n"
            "        path = after if count > 0 and after.exists() else root / f'{unit}.show'\n"
            "    if not path.exists():\n"
            "        raise SystemExit(4)\n"
            "    sys.stdout.write(path.read_text())\n"
            "elif action == 'is-active':\n"
            "    if unit == 'gb10-memory-guardian-disposable-canary.service':\n"
            "        active = (root / 'canary-created').exists() and not (root / 'driver-started').exists()\n"
            "    else:\n"
            "        active = 'ActiveState=active\\n' in (root / f'{unit}.show').read_text()\n"
            "    print('active' if active else 'inactive')\n"
            "    raise SystemExit(0 if active else 3)\n"
            "elif action == 'start' and unit == 'gb10-memory-guardian-canary.service':\n"
            "    (root / 'driver-started').write_text('1')\n"
            "elif action == 'stop' and unit == 'gb10-memory-guardian-disposable-canary.service':\n"
            "    (root / 'canary-created').unlink(missing_ok=True)\n"
            "elif action in {'reset-failed', 'revert'}:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(5)\n"
        )
        script.chmod(0o755)

    def _write_fake_systemd_run(self) -> None:
        script = self.bin / "systemd-run"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'systemd-run %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
            "printf 1 >\"$STATE_ROOT/canary-created\"\n"
        )
        script.chmod(0o755)

    def _write_fake_journalctl(self) -> None:
        script = self.bin / "journalctl"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'journalctl %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
            "cat \"$JOURNAL_FIXTURE\"\n"
        )
        script.chmod(0o755)

    def _write_config(self, label: str, registration_file: str) -> None:
        config = self.home / ".config" / "gb10-memory-guardian" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            f'schema_version = 1\n[target]\nlabel = "{label}"\n'
            f'registration_file = "{registration_file}"\n'
        )
        config.chmod(0o600)

    def _write_registration(self, name: str) -> None:
        directory = self.runtime / "gb10-memory-guardian"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        registration = directory / name
        registration.write_text(
            "version=1\n"
            f"container_id={'a' * 64}\n"
            f"scope=docker-{'a' * 64}.scope\n"
            f"control_group=/user.slice/user-{os.geteuid()}.slice/"
            f"user@{os.geteuid()}.service/"
            f"app.slice/docker-{'a' * 64}.scope\n"
        )
        registration.chmod(0o600)

    def _control_group(self, container_id: str = "a" * 64) -> str:
        return (
            f"/user.slice/user-{os.geteuid()}.slice/"
            f"user@{os.geteuid()}.service/app.slice/docker-{container_id}.scope"
        )

    def _write_cgroup(self, container_id: str = "a" * 64) -> Path:
        cgroup = self.cgroup_root / self._control_group(container_id).removeprefix("/")
        cgroup.mkdir(parents=True, exist_ok=True)
        (cgroup / "cgroup.events").write_text("populated 1\n")
        return cgroup

    def _write_guardian_status(self, state: str, container_id: str = "a" * 64) -> None:
        cgroup = self.cgroup_root / self._control_group(container_id).removeprefix("/")
        metadata = cgroup.stat() if cgroup.exists() else None
        registration = self.runtime / "gb10-memory-guardian" / "text-cgroup.v1"
        registration_metadata = registration.stat() if registration.exists() else None
        status = self.runtime / "gb10-memory-guardian" / "guardian-status.v2"
        status.write_text(
            "version=2\n"
            f"state={state}\n"
            "label=aeon-text\n"
            "registration_file=text-cgroup.v1\n"
            f"registration_device={registration_metadata.st_dev if registration_metadata else 0}\n"
            f"registration_inode={registration_metadata.st_ino if registration_metadata else 0}\n"
            f"registration_size={registration_metadata.st_size if registration_metadata else 0}\n"
            f"registration_modified_seconds={registration_metadata.st_mtime_ns // 1_000_000_000 if registration_metadata else 0}\n"
            f"registration_modified_nanoseconds={registration_metadata.st_mtime_ns % 1_000_000_000 if registration_metadata else 0}\n"
            f"registration_changed_seconds={registration_metadata.st_ctime_ns // 1_000_000_000 if registration_metadata else 0}\n"
            f"registration_changed_nanoseconds={registration_metadata.st_ctime_ns % 1_000_000_000 if registration_metadata else 0}\n"
            f"container_id={container_id}\n"
            f"scope=docker-{container_id}.scope\n"
            f"control_group={self._control_group(container_id)}\n"
            f"cgroup_device={metadata.st_dev if metadata else 0}\n"
            f"cgroup_inode={metadata.st_ino if metadata else 0}\n"
            "guardian_pid=102\n"
            f"guardian_invocation_id={GUARDIAN_INVOCATION}\n"
        )
        status.chmod(0o600)

    @staticmethod
    def _state(
        *,
        load: str = "loaded",
        active: str,
        sub: str,
        pid: int,
        result: str = "success",
        exit_status: int = 0,
        restarts: int = 0,
        restart: str = "no",
        environment: str = "",
        invocation_id: str = "3" * 32,
    ) -> str:
        return (
            f"LoadState={load}\n"
            f"ActiveState={active}\n"
            f"SubState={sub}\n"
            f"MainPID={pid}\n"
            f"Result={result}\n"
            "ExecMainCode=0\n"
            f"ExecMainStatus={exit_status}\n"
            f"NRestarts={restarts}\n"
            f"Restart={restart}\n"
            f"Environment={environment}\n"
            f"InvocationID={invocation_id}\n"
        )

    def _write_standard_states(self) -> None:
        registration = self.runtime / "gb10-memory-guardian" / "text-cgroup.v1"
        self._write_state(
            TEXT_UNIT,
            self._state(
                active="active",
                sub="running",
                pid=101,
                restarts=2,
                restart="on-failure",
                environment=f"GB10_CGROUP_REGISTRATION_PATH={registration}",
                invocation_id=TEXT_INVOCATION,
            ),
        )
        self._write_state(
            GUARDIAN_UNIT,
            self._state(
                active="active",
                sub="running",
                pid=102,
                restarts=1,
                restart="always",
                invocation_id=GUARDIAN_INVOCATION,
            ),
        )
        protected = {
            "vllm-embedding.service": (201, "active", "running"),
            "querit-4b-reranker.service": (202, "active", "running"),
            "vllm-qwen3-reranker-8b.service": (0, "inactive", "dead"),
            "llm-guard-proxy.service": (203, "active", "running"),
        }
        for unit, (pid, active, sub) in protected.items():
            self._write_state(unit, self._state(active=active, sub=sub, pid=pid))
        self._write_state(
            "gb10-memory-guardian-disposable-canary.service.before",
            self._state(active="active", sub="running", pid=301),
            literal=True,
        )
        self._write_state(
            "gb10-memory-guardian-disposable-canary.service.after",
            self._state(
                active="failed",
                sub="failed",
                pid=0,
                result="signal",
                exit_status=9,
            ).replace("ExecMainCode=0\n", "ExecMainCode=2\n"),
            literal=True,
        )
        self._write_state(
            "gb10-memory-guardian-canary.service",
            self._state(active="inactive", sub="dead", pid=0).replace(
                "ExecMainCode=0\n", "ExecMainCode=1\n"
            ),
        )

    def _write_state(self, unit: str, contents: str, *, literal: bool = False) -> None:
        suffix = unit if literal else f"{unit}.show"
        if literal:
            suffix += ".show"
        (self.states / suffix).write_text(contents)

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "PATH": f"{self.bin}:{env['PATH']}",
                "STATE_ROOT": str(self.states),
                "COMMAND_LOG": str(self.commands),
                "GUARDIAN_INVOCATIONS": str(self.guardian_invocations),
                "JOURNAL_FIXTURE": str(self.journal),
                "GB10_BENCHMARK_EXCLUDED": "YES",
                "GB10_MEMORY_GUARDIAN_BIN": str(self.guardian),
                "GB10_MEMORY_GUARDIAN_CGROUP_ROOT": str(self.cgroup_root),
                "GB10_MEMORY_GUARDIAN_SYSTEMCTL_BIN": str(self.bin / "systemctl"),
                "GB10_MEMORY_GUARDIAN_SYSTEMD_RUN_BIN": str(self.bin / "systemd-run"),
                "GB10_MEMORY_GUARDIAN_JOURNALCTL_BIN": str(self.bin / "journalctl"),
                "GB10_MEMORY_GUARDIAN_CANARY_TARGET_UNIT": TEXT_UNIT,
                "GB10_MEMORY_GUARDIAN_JOURNAL_SINCE": "2026-07-14T10:09:15-07:00",
            }
        )
        return env

    def _run(self, mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(CANARY), mode],
            env=self._environment(),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
