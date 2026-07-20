from __future__ import annotations

import hashlib
import os
import shlex
import stat
import unittest

from vllm_no_swap_fixtures import ROOT, VERIFIER, VERIFIER_CORE


HELPER = "/home/obj/.local/bin/gb10_verify_vllm_no_swap.sh"
UNIT_ROOT = "/home/obj/.config/systemd/user"
ROOTLESS_SOCKET = "unix:///run/user/1001/docker.sock"
PRODUCTION_PREFIX = [
    "/usr/bin/env",
    "-i",
    "HOME=/home/obj",
    "PATH=/usr/bin:/bin",
    "LC_ALL=C",
    f"DOCKER_HOST={ROOTLESS_SOCKET}",
    "/usr/bin/bash",
    "--noprofile",
    "--norc",
    HELPER,
]
SERVICE_CONTRACTS = {
    "vllm-aeon-27b-dflash.service": (
        "vllm-aeon-27b-dflash-n12",
        "%t/gb10-memory-guardian/aeon-text.cid",
    ),
    "vllm-embedding.service": (
        "vllm-embedding",
        "%t/gb10-vllm-cids/vllm-embedding.cid",
    ),
    "vllm-querit-4b-reranker.service": (
        "querit-4b-vllm",
        "%t/gb10-vllm-cids/vllm-querit-4b-reranker.cid",
    ),
    "vllm-querit-4b-canary-backend.service": (
        "vllm-querit-4b-canary",
        "%t/gb10-vllm-cids/vllm-querit-4b-canary-backend.cid",
    ),
    "vllm-qwen3-reranker-8b.service": (
        "vllm-qwen3-reranker-8b",
        "%t/gb10-vllm-cids/vllm-qwen3-reranker-8b.cid",
    ),
}


def _logical_argv(unit: str, directive: str) -> list[list[str]]:
    commands: list[list[str]] = []
    pending: list[str] = []
    prefix = f"{directive}="
    for raw in unit.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if pending:
            value = line
        elif line.startswith(prefix):
            value = line[len(prefix) :]
        else:
            continue
        continued = value.endswith("\\")
        pending.append(value[:-1].rstrip() if continued else value)
        if not continued:
            commands.append(shlex.split(" ".join(pending), posix=True))
            pending = []
    if pending:
        raise AssertionError(f"unterminated {directive} continuation")
    return commands


class VllmNoSwapUnitContractTests(unittest.TestCase):
    def test_wrapper_has_one_fixed_digest_bound_non_executable_core(self) -> None:
        core = VERIFIER_CORE.read_bytes()
        source = VERIFIER.read_text()
        self.assertEqual(stat.S_IMODE(VERIFIER_CORE.stat().st_mode), 0o644)
        self.assertFalse(os.access(VERIFIER_CORE, os.X_OK))
        self.assertIn("CORE_BASENAME=gb10_verify_vllm_no_swap_core.py", source)
        self.assertIn(f"EXPECTED_CORE_SHA256={hashlib.sha256(core).hexdigest()}", source)
        self.assertIn("os.O_NOFOLLOW", source)
        self.assertIn("st_uid != os.geteuid()", source)
        self.assertNotIn("GB10_VLLM_NO_SWAP_CORE", source)
        self.assertNotIn("import gb10_verify_vllm_no_swap_core", source)

    def test_inventory_discovers_every_and_only_tracked_vllm_backend(self) -> None:
        discovered: set[str] = set()
        for path in (ROOT / "systemd").glob("*.service"):
            starts = _logical_argv(path.read_text(), "ExecStart")
            if any(
                any(
                    token.endswith("/vllm")
                    or token.endswith("/aeon_vllm_wrapper.py")
                    for token in argv
                )
                for argv in starts
            ):
                discovered.add(path.name)
        self.assertEqual(discovered, set(SERVICE_CONTRACTS))

    def test_every_tracked_unit_has_clean_generation_bound_start_and_cleanup(self) -> None:
        for name, (container, cidfile) in SERVICE_CONTRACTS.items():
            with self.subTest(unit=name):
                unit = (ROOT / "systemd" / name).read_text()
                start = _logical_argv(unit, "ExecStart")
                self.assertEqual(len(start), 1)
                argv = start[0]
                self.assertEqual(argv[:2], ["/usr/bin/docker", "run"])
                image_at = next(
                    index
                    for index, token in enumerate(argv)
                    if "@sha256:" in token
                )
                application = argv[image_at + 1 :]
                normalized_swap = [
                    token
                    for token in application
                    if token.split("=", 1)[0].replace("_", "-") == "--swap-space"
                ]
                self.assertEqual(normalized_swap, [])
                self.assertEqual(argv.count("--memory"), 1)
                self.assertEqual(argv.count("--memory-swap"), 1)
                self.assertEqual(argv.count("--memory-swappiness"), 1)
                self.assertEqual(argv[argv.index("--memory-swappiness") + 1], "0")
                self.assertEqual(
                    argv[argv.index("--memory") + 1],
                    argv[argv.index("--memory-swap") + 1],
                )
                self.assertIn(f"--cidfile={cidfile}", argv)
                self.assertIn("UMask=0077", unit)
                self.assertNotIn("MemorySwapMax=0", unit)
                unit_path = f"{UNIT_ROOT}/{name}"
                condition = PRODUCTION_PREFIX + ["--unit", unit_path]
                self.assertEqual(_logical_argv(unit, "ExecCondition")[0], condition)
                posts = _logical_argv(unit, "ExecStartPost")
                self.assertEqual(
                    posts[0],
                    PRODUCTION_PREFIX
                    + ["--unit", unit_path, "--container", container],
                )
                cleanup = PRODUCTION_PREFIX + [
                    "--cleanup",
                    "--container",
                    container,
                    "--cidfile",
                    cidfile,
                ]
                self.assertIn(cleanup, _logical_argv(unit, "ExecStartPre"))
                self.assertEqual(_logical_argv(unit, "ExecStop"), [cleanup])
                self.assertIn(cleanup, _logical_argv(unit, "ExecStopPost"))
                for directive in ("ExecStartPre", "ExecStop", "ExecStopPost"):
                    commands = _logical_argv(unit, directive)
                    self.assertFalse(
                        any(
                            argv[:3] == ["/usr/bin/docker", "rm", "-f"]
                            or argv[:2] == ["/usr/bin/docker", "stop"]
                            or (
                                argv
                                and argv[0] == "/usr/bin/timeout"
                                and "/usr/bin/docker" in argv
                            )
                            for argv in commands
                        )
                    )

    def test_production_entry_is_ambient_environment_independent(self) -> None:
        for name in SERVICE_CONTRACTS:
            with self.subTest(unit=name):
                unit = (ROOT / "systemd" / name).read_text()
                commands = [
                    *_logical_argv(unit, "ExecCondition"),
                    *_logical_argv(unit, "ExecStartPost"),
                    *_logical_argv(unit, "ExecStartPre"),
                    *_logical_argv(unit, "ExecStop"),
                    *_logical_argv(unit, "ExecStopPost"),
                ]
                helper_commands = [argv for argv in commands if HELPER in argv]
                self.assertGreaterEqual(len(helper_commands), 4)
                for argv in helper_commands:
                    self.assertEqual(argv[: len(PRODUCTION_PREFIX)], PRODUCTION_PREFIX)
                    self.assertNotIn("--test-only", argv)

    def test_deployment_agents_names_current_guardian_and_cleanup_authority(self) -> None:
        text = (ROOT / "docs" / "deployment" / "AGENTS.md").read_text()
        for current in (
            "integrated guardian",
            'mem_threshold_gib = 5',
            "gb10_verify_vllm_no_swap.sh --cleanup",
            "ExecStopPost",
        ):
            with self.subTest(current=current):
                self.assertIn(current, text)
        for stale in (
            "2G kill threshold",
            "MemAvail < 2G",
            "Rust guardian log",
            "automatically purge hung docker containers",
            "docker rm -f",
            "vllm-querit-4b.service",
        ):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, text)

    def test_install_and_transactions_own_the_verifier_bundle(self) -> None:
        for path in (ROOT / "README.md", ROOT / "docs" / "deployment" / "AGENTS.md"):
            text = path.read_text()
            self.assertIn("scripts/gb10_verify_vllm_no_swap_core.py", text)
            self.assertIn("scripts/gb10_verify_vllm_no_swap.sh", text)
        source = (ROOT / "scripts" / "querit_canary_deployment.py").read_text()
        core = '"scripts/gb10_verify_vllm_no_swap_core.py"'
        wrapper = '"scripts/gb10_verify_vllm_no_swap.sh"'
        self.assertIn(core, source)
        self.assertIn(wrapper, source)
        self.assertLess(source.index(core), source.index(wrapper))
        storage = (ROOT / "scripts" / "gb10_embedding_activation_storage.py").read_text()
        self.assertIn('NO_SWAP_KEYS = ("core", "wrapper")', storage)
        self.assertIn('"core": "no_swap_core.before"', storage)
        self.assertIn('"wrapper": "no_swap_wrapper.before"', storage)
        activation = (ROOT / "scripts" / "gb10_embedding_activation.py").read_text()
        self.assertIn('"no_swap_artifacts": artifacts', activation)
        self.assertIn('"schema": 2', activation)

    def test_embedding_profile_contract_orders_clean_verifier_before_readiness(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import gb10_embedding_profile_contract as profile

        self.assertEqual(
            profile.EXPECTED_EXEC_START_POST[0],
            PRODUCTION_PREFIX
            + [
                "--unit",
                f"{UNIT_ROOT}/vllm-embedding.service",
                "--container",
                "vllm-embedding",
            ],
        )
        self.assertTrue(
            profile.EXPECTED_EXEC_START_POST[1][0].endswith("gb10_service_ready.sh")
        )
