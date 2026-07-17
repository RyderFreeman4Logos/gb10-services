from __future__ import annotations

import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_UNIT = ROOT / "systemd" / "vllm-querit-4b-canary.service"
BACKEND_UNIT = ROOT / "systemd" / "vllm-querit-4b-canary-backend.service"
PRODUCTION_UNIT = ROOT / "systemd" / "querit-4b-reranker.service"
RESEARCH_NOTE = ROOT / "docs" / "research" / "2026-07-16-querit-vllm-migration.md"
MODEL_DIR = "/home/obj/models/querit-4b-vllm"
IMAGE = (
    "ghcr.io/aeon-7/aeon-vllm-ultimate@"
    "sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d"
)


def _logical_directive_argv(unit: str, directive: str) -> list[list[str]]:
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


def _split_at_image(argv: list[str]) -> tuple[list[str], list[str]]:
    if argv[:2] != ["/usr/bin/docker", "run"]:
        raise AssertionError("ExecStart must use canonical docker run")
    try:
        image_at = argv.index(IMAGE)
    except ValueError as error:
        raise AssertionError("missing exact digest-pinned image") from error
    if argv.count(IMAGE) != 1:
        raise AssertionError("image must occur exactly once")
    return argv[2:image_at], argv[image_at + 1 :]


def _exact_container_options(
    argv: list[str], prefix: list[str], arities: dict[str, int]
) -> dict[str, list[str]]:
    if argv[: len(prefix)] != prefix:
        raise AssertionError("wrong vLLM command or converted model path")
    options: dict[str, list[str]] = {}
    index = len(prefix)
    while index < len(argv):
        token = argv[index]
        if token not in arities:
            raise AssertionError(f"unknown vLLM option: {token}")
        if token in options:
            raise AssertionError(f"duplicate vLLM option: {token}")
        arity = arities[token]
        values = argv[index + 1 : index + 1 + arity]
        if len(values) != arity or any(value.startswith("-") for value in values):
            raise AssertionError(f"wrong arity for vLLM option: {token}")
        options[token] = values
        index += 1 + arity
    if set(options) != set(arities):
        missing = sorted(set(arities) - set(options))
        raise AssertionError(f"missing exact vLLM options: {missing}")
    return options


def _backend_contract(unit: str) -> None:
    starts = _logical_directive_argv(unit, "ExecStart")
    start_posts = _logical_directive_argv(unit, "ExecStartPost")
    if len(starts) != 1:
        raise AssertionError(f"expected one ExecStart, found {len(starts)}")
    if len(start_posts) != 1:
        raise AssertionError(f"expected one ExecStartPost, found {len(start_posts)}")

    host, container = _split_at_image(starts[0])
    expected_host = [
        "--rm",
        "--name",
        "vllm-querit-4b-canary",
        "--cgroup-parent",
        "app.slice",
        "--gpus",
        "all",
        "--ipc",
        "host",
        "-p",
        "127.0.0.1:18015:8000",
        "-p",
        "100.105.4.92:18015:8000",
        "-v",
        f"{MODEL_DIR}:{MODEL_DIR}:ro",
        "-e",
        "HF_HUB_OFFLINE=1",
        "-e",
        "TRANSFORMERS_OFFLINE=1",
        "--memory",
        "18g",
        "--memory-swap",
        "18g",
        "--memory-swappiness",
        "0",
        "--oom-score-adj",
        "500",
        "--entrypoint",
        "python3",
    ]
    if host != expected_host:
        raise AssertionError(f"wrong Docker host contract: {host}")

    arities = {
        "--host": 1,
        "--port": 1,
        "--served-model-name": 3,
        "--runner": 1,
        "--dtype": 1,
        "--max-model-len": 1,
        "--gpu-memory-utilization": 1,
        "--kv-cache-memory-bytes": 1,
        "--max-num-batched-tokens": 1,
        "--max-num-seqs": 1,
        "--enable-chunked-prefill": 0,
        "--max-num-partial-prefills": 1,
        "--max-long-partial-prefills": 1,
        "--long-prefill-token-threshold": 1,
        "--enforce-eager": 0,
        "--chat-template": 1,
    }
    options = _exact_container_options(
        container,
        ["/usr/local/bin/vllm", "serve", MODEL_DIR],
        arities,
    )
    expected_options = {
        "--host": ["0.0.0.0"],
        "--port": ["8000"],
        "--served-model-name": [
            "qwen3-reranker-8b",
            "Qwen/Qwen3-Reranker-8B",
            "Querit/Querit-4B",
        ],
        "--runner": ["pooling"],
        "--dtype": ["bfloat16"],
        "--max-model-len": ["32768"],
        "--gpu-memory-utilization": ["0.22"],
        "--kv-cache-memory-bytes": ["4800M"],
        "--max-num-batched-tokens": ["4096"],
        "--max-num-seqs": ["16"],
        "--enable-chunked-prefill": [],
        "--max-num-partial-prefills": ["1"],
        "--max-long-partial-prefills": ["1"],
        "--long-prefill-token-threshold": ["8192"],
        "--enforce-eager": [],
        "--chat-template": [f"{MODEL_DIR}/querit-rerank.jinja"],
    }
    if options != expected_options:
        raise AssertionError(f"wrong vLLM canary options: {options}")

    if start_posts[0] != [
        "/home/obj/.local/bin/gb10_service_ready.sh",
        "rerank",
        "http://127.0.0.1:18015",
        "qwen3-reranker-8b",
        "--deadline",
        "300",
    ]:
        raise AssertionError("readiness must probe native rerank behavior on :18014")


class QueritVllmCanaryContractTests(unittest.TestCase):
    def test_launch_profile_is_exact_and_digest_pinned(self) -> None:
        _backend_contract(BACKEND_UNIT.read_text())

    def test_public_18014_is_the_deepinfra_adapter_not_raw_vllm(self) -> None:
        unit = ADAPTER_UNIT.read_text()
        starts = _logical_directive_argv(unit, "ExecStart")
        self.assertEqual(len(starts), 1)
        self.assertEqual(
            starts[0],
            [
                "/usr/bin/python3",
                "/home/obj/.local/lib/gb10/querit_deepinfra_adapter.py",
                "--listen-host",
                "100.105.4.92",
                "--listen-port",
                "18014",
                "--backend-url",
                "http://127.0.0.1:18015",
            ],
        )
        self.assertNotIn("docker run", unit)
        self.assertIn("Requires=vllm-querit-4b-canary-backend.service", unit)
        self.assertIn("After=vllm-querit-4b-canary-backend.service", unit)
        self.assertEqual(
            _logical_directive_argv(unit, "ExecStartPost"),
            [],
            "peak readiness must run after systemctl reports an active adapter",
        )

    def test_raw_canary_publishes_loopback_and_tailnet_once_without_wildcard(
        self,
    ) -> None:
        backend = BACKEND_UNIT.read_text()
        host, _container = _split_at_image(
            _logical_directive_argv(backend, "ExecStart")[0]
        )
        publishes = [
            host[index + 1] for index, token in enumerate(host) if token == "-p"
        ]
        self.assertEqual(
            publishes,
            [
                "127.0.0.1:18015:8000",
                "100.105.4.92:18015:8000",
            ],
        )
        self.assertEqual(len(publishes), len(set(publishes)))
        for binding in publishes:
            address, host_port, container_port = binding.rsplit(":", 2)
            self.assertIn(address, {"127.0.0.1", "100.105.4.92"})
            self.assertEqual(host_port, "18015")
            self.assertEqual(container_port, "8000")

    def test_canary_is_memory_bounded_fail_closed_and_lifecycle_isolated(self) -> None:
        unit = BACKEND_UNIT.read_text()
        self.assertIn("MemoryMax=256M", unit)
        self.assertIn("MemorySwapMax=0", unit)
        self.assertIn("OOMScoreAdjust=500", unit)
        self.assertIn("TimeoutStartSec=1800", unit)
        self.assertIn("Environment=HF_HUB_OFFLINE=1", unit)
        self.assertIn("Environment=TRANSFORMERS_OFFLINE=1", unit)

        self.assertIn("Restart=no", unit)
        self.assertNotIn("Restart=on-failure", unit)
        self.assertEqual(
            _logical_directive_argv(unit, "ExecCondition"),
            [["/home/obj/.local/bin/gb10_querit_canary_preflight.py"]],
        )
        self.assertEqual(
            _logical_directive_argv(unit, "ExecStartPre"),
            [["-/usr/bin/docker", "rm", "-f", "vllm-querit-4b-canary"]],
        )
        self.assertEqual(
            _logical_directive_argv(unit, "ExecStop"),
            [
                [
                    "/usr/bin/timeout",
                    "--signal=TERM",
                    "--kill-after=5",
                    "30",
                    "/usr/bin/docker",
                    "stop",
                    "--time",
                    "20",
                    "vllm-querit-4b-canary",
                ]
            ],
        )

        adapter = ADAPTER_UNIT.read_text()
        self.assertIn("Restart=no", adapter)
        self.assertNotIn("Restart=on-failure", adapter)
        self.assertEqual(
            _logical_directive_argv(adapter, "ExecCondition"),
            [["/home/obj/.local/bin/gb10_querit_canary_preflight.py"]],
        )

        unit_section = unit.split("[Service]", 1)[0] + adapter.split("[Service]", 1)[0]
        for neighbor in (
            "vllm-embedding.service",
            "querit-4b-reranker.service",
            "vllm-qwen3-reranker-8b.service",
        ):
            self.assertNotIn(neighbor, unit_section)
        lowered = (unit + adapter).lower()
        self.assertNotIn("guardian", lowered)
        self.assertNotIn("gb10_cgroup_registration", lowered)
        self.assertNotIn("publish_cgroup_registration", lowered)

    def test_production_transformers_reranker_stays_on_18013(self) -> None:
        production = PRODUCTION_UNIT.read_text()
        self.assertIn("100.105.4.92:18013:8000", production)
        self.assertNotIn("100.105.4.92:18014:8000", production)
        self.assertIn("querit_openai_rerank_server.py", production)

    def test_research_note_records_adapter_and_transactional_operator_contract(
        self,
    ) -> None:
        note = RESEARCH_NOTE.read_text()
        self.assertIn(
            "### DeepInfra wire compatibility adapter",
            note,
        )
        for required in (
            "/v1/score",
            "/v1/inference/Qwen/Qwen3-Reranker-8B",
            "queries[]",
            "scores[]",
            "input_tokens",
            "gb10_querit_canary_lifecycle.py activate",
            "gb10_querit_canary_lifecycle.py deactivate",
            "20 GiB",
            "stop then start",
            "No production cutover",
            "127.0.0.1:18015",
            "100.105.4.92:18015",
            "without a wildcard bind",
            "--gpu-memory-utilization 0.22",
            "8,043,564,036",
            "8,043,558,914",
            "-5,122",
            "scripts/querit_replay_trust.py",
        ):
            self.assertIn(required, note)


if __name__ == "__main__":
    unittest.main()
