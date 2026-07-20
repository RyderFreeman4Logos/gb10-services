from __future__ import annotations

import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_UNIT = ROOT / "systemd" / "vllm-querit-4b-reranker.service"
LEGACY_TRANSFORMERS_UNIT = ROOT / "systemd" / "querit-4b-reranker.service"
LEGACY_QWEN_UNIT = ROOT / "systemd" / "vllm-qwen3-reranker-8b.service"
README = ROOT / "README.md"
DEPLOYMENT_AGENTS = ROOT / "docs" / "deployment" / "AGENTS.md"
MODEL_DIR = "/home/obj/models/querit-4b-vllm"
NO_SWAP_PREFIX = [
    "/usr/bin/env",
    "-i",
    "HOME=/home/obj",
    "PATH=/usr/bin:/bin",
    "LC_ALL=C",
    "DOCKER_HOST=unix:///run/user/1001/docker.sock",
    "/usr/bin/bash",
    "--noprofile",
    "--norc",
    "/home/obj/.local/bin/gb10_verify_vllm_no_swap.sh",
]
IMAGE = (
    "ghcr.io/aeon-7/aeon-vllm-ultimate@"
    "sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d"
)
EXCLUSIVE_PRODUCTION_OWNERS = {
    "vllm-querit-4b-reranker.service",
    "vllm-qwen3-reranker-8b.service",
}


def _logical_argv(unit: str, directive: str) -> list[list[str]]:
    commands: list[list[str]] = []
    pending: list[str] = []
    prefix = f"{directive}="
    for raw_line in unit.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if pending:
            value = line
        elif line.startswith(prefix):
            value = line[len(prefix) :]
        else:
            continue
        continued = value.endswith("\\")
        if continued:
            value = value[:-1].rstrip()
        pending.append(value)
        if not continued:
            commands.append(shlex.split(" ".join(pending), posix=True))
            pending = []
    if pending:
        raise AssertionError(f"unterminated {directive} continuation")
    return commands


def _unit_directive_values(unit: str, directive: str) -> list[str]:
    prefix = f"{directive}="
    return [
        line[len(prefix) :]
        for line in unit.splitlines()
        if line.startswith(prefix)
    ]


def _docker_host_and_vllm_options(unit: str) -> tuple[list[str], dict[str, list[str]]]:
    starts = _logical_argv(unit, "ExecStart")
    if len(starts) != 1 or starts[0][:2] != ["/usr/bin/docker", "run"]:
        raise AssertionError("production unit must have one canonical docker ExecStart")
    argv = starts[0]
    if argv.count(IMAGE) != 1:
        raise AssertionError("production unit must use the exact digest-pinned image once")
    image_at = argv.index(IMAGE)
    command = argv[image_at + 1 :]
    prefix = ["/usr/local/bin/vllm", "serve", MODEL_DIR]
    if command[: len(prefix)] != prefix:
        raise AssertionError("production unit has the wrong vLLM command or model path")
    arities = {
        "--host": 1,
        "--port": 1,
        "--served-model-name": 3,
        "--runner": 1,
        "--dtype": 1,
        "--max-model-len": 1,
        "--gpu-memory-utilization": 1,
        "--kv-cache-memory-bytes": 1,
        "--kv-cache-dtype": 1,
        "--tensor-parallel-size": 1,
        "--pipeline-parallel-size": 1,
        "--cpu-offload-gb": 1,
        "--max-num-batched-tokens": 1,
        "--max-num-seqs": 1,
        "--enable-chunked-prefill": 0,
        "--max-num-partial-prefills": 1,
        "--max-long-partial-prefills": 1,
        "--long-prefill-token-threshold": 1,
        "--enforce-eager": 0,
        "--chat-template": 1,
    }
    options: dict[str, list[str]] = {}
    index = len(prefix)
    while index < len(command):
        option = command[index]
        if option not in arities or option in options:
            raise AssertionError(f"unexpected or duplicate vLLM option: {option}")
        arity = arities[option]
        values = command[index + 1 : index + 1 + arity]
        if len(values) != arity or any(value.startswith("-") for value in values):
            raise AssertionError(f"wrong arity for vLLM option: {option}")
        options[option] = values
        index += 1 + arity
    if set(options) != set(arities):
        raise AssertionError("production unit has incomplete vLLM options")
    return argv[2:image_at], options


class QueritVllmProductionContractTests(unittest.TestCase):
    def test_production_unit_preserves_the_proven_vllm_profile(self) -> None:
        unit = PRODUCTION_UNIT.read_text()
        host, options = _docker_host_and_vllm_options(unit)
        self.assertEqual(
            host,
            [
                "--rm", "--cidfile=%t/gb10-vllm-cids/vllm-querit-4b-reranker.cid",
                "--name", "querit-4b-vllm", "--cgroup-parent", "app.slice",
                "--gpus", "all", "--ipc", "host", "-p", "100.105.4.92:18013:8000",
                "-v", f"{MODEL_DIR}:{MODEL_DIR}:ro", "-e", "HF_HUB_OFFLINE=1",
                "-e", "TRANSFORMERS_OFFLINE=1", "--memory", "18g", "--memory-swap",
                "18g", "--memory-swappiness", "0", "--oom-score-adj", "500",
                "--entrypoint", "python3",
            ],
        )
        self.assertEqual(
            options,
            {
                "--host": ["0.0.0.0"], "--port": ["8000"],
                "--served-model-name": ["Querit/Querit-4B", "Qwen/Qwen3-Reranker-8B", "qwen3-reranker-8b"],
                "--runner": ["pooling"], "--dtype": ["bfloat16"],
                "--max-model-len": ["32768"], "--gpu-memory-utilization": ["0.17"],
                "--kv-cache-memory-bytes": ["4800M"], "--kv-cache-dtype": ["auto"],
                "--tensor-parallel-size": ["1"], "--pipeline-parallel-size": ["1"],
                "--cpu-offload-gb": ["0"],
                "--max-num-batched-tokens": ["16384"], "--max-num-seqs": ["32"],
                "--enable-chunked-prefill": [],
                "--max-num-partial-prefills": ["1"], "--max-long-partial-prefills": ["1"],
                "--long-prefill-token-threshold": ["8192"], "--enforce-eager": [],
                "--chat-template": [f"{MODEL_DIR}/querit-rerank.jinja"],
            },
        )
        self.assertIn("MemoryMax=256M", unit)
        self.assertNotIn("MemorySwapMax=0", unit)
        self.assertIn("Restart=no", unit)
        self.assertEqual(_unit_directive_values(unit, "TimeoutStartSec"), ["1800"])

    def test_legacy_transformers_production_unit_is_deleted(self) -> None:
        self.assertFalse(LEGACY_TRANSFORMERS_UNIT.exists())

    def test_production_readiness_and_enablement_are_canonical(self) -> None:
        unit = PRODUCTION_UNIT.read_text()
        expected_unit = "/home/obj/.config/systemd/user/vllm-querit-4b-reranker.service"
        self.assertEqual(
            _logical_argv(unit, "ExecCondition"),
            [NO_SWAP_PREFIX + ["--unit", expected_unit]],
        )
        self.assertEqual(
            _logical_argv(unit, "ExecStartPost"),
            [
                ["/home/obj/.local/bin/gb10_service_ready.sh", "rerank", "http://100.105.4.92:18013", "Querit/Querit-4B", "--deadline", "1800"],
            ],
        )
        self.assertIn("[Install]", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_operational_docs_derive_scheduler_values_from_production_unit(self) -> None:
        _host, options = _docker_host_and_vllm_options(PRODUCTION_UNIT.read_text())
        scheduler_literals = {
            option: f"{option} {options[option][0]}"
            for option in (
                "--max-num-batched-tokens",
                "--max-num-seqs",
                "--max-num-partial-prefills",
                "--max-long-partial-prefills",
            )
        }
        expected_counts = {
            README: {
                "--max-num-batched-tokens": 2,
                "--max-num-seqs": 2,
                "--max-num-partial-prefills": 1,
                "--max-long-partial-prefills": 1,
            },
            DEPLOYMENT_AGENTS: {option: 1 for option in scheduler_literals},
        }
        for path, option_counts in expected_counts.items():
            text = path.read_text()
            for option, expected_count in option_counts.items():
                with self.subTest(path=path.name, option=option):
                    self.assertEqual(
                        text.count(scheduler_literals[option]), expected_count
                    )

    def test_production_start_pre_removes_stale_cidfile_after_generation_cleanup(self) -> None:
        unit = PRODUCTION_UNIT.read_text()
        self.assertEqual(
            _logical_argv(unit, "ExecStartPre")[-1],
            [
                "-/bin/rm",
                "-f",
                "%t/gb10-vllm-cids/vllm-querit-4b-reranker.cid",
            ],
        )

    def test_canary_units_and_lifecycle_machinery_are_deleted(self) -> None:
        retired_paths = (
            "systemd/vllm-querit-4b-canary-backend.service",
            "systemd/vllm-querit-4b-canary.service",
            "scripts/gb10_querit_canary_deploy.py",
            "scripts/gb10_querit_canary_lifecycle.py",
            "scripts/gb10_querit_canary_preflight.py",
            "scripts/querit_canary_deployment.py",
            "scripts/querit_canary_lifecycle.py",
            "scripts/querit_canary_runtime.py",
            "scripts/querit_canary_transaction.py",
        )
        for relative_path in retired_paths:
            with self.subTest(path=relative_path):
                self.assertFalse((ROOT / relative_path).exists())

    def test_current_production_paths_do_not_reference_canary_units(self) -> None:
        for path in (
            ROOT / "README.md",
            ROOT / "docs" / "deployment" / "AGENTS.md",
        ):
            with self.subTest(path=path.name):
                self.assertNotIn("vllm-querit-4b-canary", path.read_text())

    def test_production_owner_uses_the_canonical_name_and_port(self) -> None:
        production_start = _logical_argv(PRODUCTION_UNIT.read_text(), "ExecStart")[0]
        production_name = production_start[production_start.index("--name") + 1]
        production_publish = production_start[production_start.index("-p") + 1]
        self.assertEqual(production_name, "querit-4b-vllm")
        self.assertEqual(production_publish, "100.105.4.92:18013:8000")

    def test_production_reranker_conflicts_are_symmetric(self) -> None:
        units = {
            "vllm-querit-4b-reranker.service": PRODUCTION_UNIT.read_text(),
            "vllm-qwen3-reranker-8b.service": LEGACY_QWEN_UNIT.read_text(),
        }
        for name, unit in units.items():
            conflicts = set(" ".join(_unit_directive_values(unit, "Conflicts")).split())
            self.assertTrue(EXCLUSIVE_PRODUCTION_OWNERS - {name} <= conflicts, name)

    def test_all_reranker_publishes_are_single_address_bindings(self) -> None:
        for path in (PRODUCTION_UNIT, LEGACY_QWEN_UNIT):
            publishes = [
                argv[index + 1]
                for argv in _logical_argv(path.read_text(), "ExecStart")
                for index, token in enumerate(argv)
                if token == "-p"
            ]
            self.assertEqual(len(publishes), 1, path.name)
            self.assertEqual(len(publishes), len(set(publishes)), path.name)
            for binding in publishes:
                address, _port, container_port = binding.rsplit(":", 2)
                self.assertIn(address, {"100.105.4.92", "127.0.0.1"}, path.name)
                self.assertEqual(container_port, "8000", path.name)

    def test_every_reranker_vllm_unit_omits_unsupported_swap_space_flag(self) -> None:
        for path in (PRODUCTION_UNIT, LEGACY_QWEN_UNIT):
            argv = _logical_argv(path.read_text(), "ExecStart")
            self.assertEqual(len(argv), 1, path.name)
            normalized_swap = [
                token
                for token in argv[0]
                if token.split("=", 1)[0].replace("_", "-") == "--swap-space"
            ]
            self.assertEqual(normalized_swap, [], path.name)


if __name__ == "__main__":
    unittest.main()
