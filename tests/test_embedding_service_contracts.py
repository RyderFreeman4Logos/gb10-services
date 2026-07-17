from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EMBEDDING_UNIT = ROOT / "systemd" / "vllm-embedding.service"
README = ROOT / "README.md"
AGENT_PLAYBOOK = ROOT / "docs" / "deployment" / "AGENTS.md"
EMBEDDING_IMAGE = (
    "ghcr.io/aeon-7/aeon-vllm-ultimate@"
    "sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d"
)

VALIDATED_KV_MIB = 5_820
VALIDATED_KV_TOKENS = 41_376
CONTRACT_TOKENS = 32_768
MIN_KV_MARGIN_BPS = 400


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
            logical_command = " ".join(pending)
            try:
                commands.append(shlex.split(logical_command, posix=True))
            except ValueError as error:
                raise AssertionError(
                    f"invalid {directive} shell syntax: {error}"
                ) from error
            pending = []

    if pending:
        raise AssertionError(f"unterminated {directive} continuation")
    return commands


_DOCKER_HOST_OPTION_ARITY = {
    "--rm": 0,
    "--name": 1,
    "--cgroup-parent": 1,
    "--gpus": 1,
    "--ipc": 1,
    "--dns": 1,
    "-p": 1,
    "-v": 1,
    "-e": 1,
    "--memory": 1,
    "--memory-swap": 1,
    "--memory-swappiness": 1,
    "--oom-score-adj": 1,
    "--entrypoint": 1,
}


def _split_docker_run_argv(
    argv: list[str], expected_image: str
) -> tuple[list[str], list[str]]:
    if argv[:2] != ["/usr/bin/docker", "run"]:
        raise AssertionError("ExecStart must be the canonical docker run command")
    host: list[str] = []
    index = 2
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            break
        if token.startswith("--cidfile="):
            if token == "--cidfile=":
                raise AssertionError("empty --cidfile assignment")
            host.append(token)
            index += 1
            continue
        if "=" in token or token not in _DOCKER_HOST_OPTION_ARITY:
            raise AssertionError(f"unknown or noncanonical Docker host option: {token}")
        arity = _DOCKER_HOST_OPTION_ARITY[token]
        values = argv[index + 1 : index + 1 + arity]
        if len(values) != arity or any(value.startswith("-") for value in values):
            raise AssertionError(f"wrong arity for Docker host option: {token}")
        host.extend([token, *values])
        index += 1 + arity
    if index >= len(argv) or argv[index] != expected_image:
        actual = argv[index] if index < len(argv) else "<missing>"
        raise AssertionError(f"wrong Docker image boundary: {actual}")
    container = argv[index + 1 :]
    docker_aliases = {"-m", "--memory-reservation", *list(_DOCKER_HOST_OPTION_ARITY)}
    for token in container:
        lowered = token.lower()
        if lowered in docker_aliases or any(
            lowered.startswith(f"{flag}=") for flag in docker_aliases if flag.startswith("--")
        ):
            raise AssertionError(f"Docker host option after image boundary: {token}")
    return host, container


def _exact_container_options(
    argv: list[str], prefix: list[str], arities: dict[str, int]
) -> dict[str, list[str]]:
    if argv[: len(prefix)] != prefix:
        raise AssertionError(f"wrong container command prefix: {argv[:len(prefix)]}")
    options: dict[str, list[str]] = {}
    index = len(prefix)
    while index < len(argv):
        token = argv[index]
        if token not in arities:
            raise AssertionError(f"unknown option, positional alias, or spelling: {token}")
        if token in options:
            raise AssertionError(f"duplicate container option: {token}")
        arity = arities[token]
        values = argv[index + 1 : index + 1 + arity]
        if len(values) != arity or any(value.startswith("-") for value in values):
            raise AssertionError(f"wrong arity for container option: {token}")
        options[token] = values
        index += 1 + arity
    if set(options) != set(arities):
        missing = sorted(set(arities) - set(options))
        raise AssertionError(f"missing exact container options: {missing}")
    return options


def _option_values(argv: list[str], flag: str, count: int = 1) -> list[str]:
    indices: list[int] = []
    canonical_prefix = f"{flag}="
    for index, token in enumerate(argv):
        if token == flag:
            indices.append(index)
        elif token.lower() == flag.lower() or token.lower().startswith(
            canonical_prefix.lower()
        ):
            raise AssertionError(f"noncanonical option spelling: {token}")

    if len(indices) != 1:
        raise AssertionError(
            f"expected exactly one {flag}, found {len(indices)}"
        )
    start = indices[0] + 1
    values = argv[start : start + count]
    if len(values) != count or any(value.startswith("-") for value in values):
        raise AssertionError(f"missing value for {flag}")
    return values


def _standalone_option(argv: list[str], flag: str) -> None:
    matches = [token for token in argv if token == flag]
    noncanonical = [
        token
        for token in argv
        if token != flag
        and (
            token.lower() == flag.lower()
            or token.lower().startswith(f"{flag.lower()}=")
        )
    ]
    if noncanonical:
        raise AssertionError(f"noncanonical option spelling: {noncanonical[0]}")
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {flag}, found {len(matches)}"
        )


def _embedding_contract(unit: str) -> dict[str, int]:
    exec_starts = _logical_directive_argv(unit, "ExecStart")
    exec_start_posts = _logical_directive_argv(unit, "ExecStartPost")
    if len(exec_starts) != 1:
        raise AssertionError(
            f"expected exactly one ExecStart, found {len(exec_starts)}"
        )
    if len(exec_start_posts) != 1:
        raise AssertionError(
            f"expected exactly one ExecStartPost, found {len(exec_start_posts)}"
        )

    argv = exec_starts[0]
    host_argv, container_argv = _split_docker_run_argv(argv, EMBEDDING_IMAGE)
    expected_host_options = {
        "--name": ["vllm-embedding"],
        "-p": ["100.105.4.92:18012:8000"],
        "--memory-swappiness": ["0"],
        "--oom-score-adj": ["0"],
    }
    for flag, expected in expected_host_options.items():
        actual = _option_values(host_argv, flag, len(expected))
        if actual != expected:
            raise AssertionError(f"{flag} must be {expected}, found {actual}")
    for forbidden in ("--cgroup-parent", "--dns"):
        if any(
            token.lower() == forbidden
            or token.lower().startswith(f"{forbidden}=")
            for token in host_argv
        ):
            raise AssertionError(f"forbidden Docker host option: {forbidden}")

    option_arities = {
        "--host": 1,
        "--port": 1,
        "--served-model-name": 2,
        "--convert": 1,
        "--dtype": 1,
        "--max-model-len": 1,
        "--max-num-batched-tokens": 1,
        "--max-num-seqs": 1,
        "--kv-cache-memory-bytes": 1,
        "--gpu-memory-utilization": 1,
        "--enforce-eager": 0,
    }
    model_command = [
        "/usr/local/bin/vllm",
        "serve",
        "Qwen/Qwen3-Embedding-8B",
    ]
    parsed_options = _exact_container_options(
        container_argv, model_command, option_arities
    )
    expected_container_options = {
        "--host": ["0.0.0.0"],
        "--port": ["8000"],
        "--served-model-name": [
            "qwen3-embedding-8b",
            "Qwen/Qwen3-Embedding-8B",
        ],
        "--convert": ["embed"],
        "--dtype": ["bfloat16"],
        "--max-model-len": ["32768"],
        "--max-num-batched-tokens": ["8192"],
        "--max-num-seqs": ["64"],
        "--kv-cache-memory-bytes": ["4800M"],
        "--gpu-memory-utilization": ["0.15"],
        "--enforce-eager": [],
    }
    for flag, expected in expected_container_options.items():
        actual = parsed_options[flag]
        if actual != expected:
            raise AssertionError(f"{flag} must be {expected}, found {actual}")

    forbidden_options = ("--quantization", "--kv-cache-dtype", "--truncate-dim")
    for token in container_argv:
        lowered = token.lower()
        for forbidden in forbidden_options:
            if lowered == forbidden or lowered.startswith(f"{forbidden}="):
                raise AssertionError(f"forbidden embedding option: {token}")

    readiness = exec_start_posts[0]
    if not readiness or not readiness[0].endswith("gb10_service_ready.sh"):
        raise AssertionError(
            "ExecStartPost must use gb10_service_ready.sh"
        )
    readiness_args = readiness[1:]
    if "embedding" not in readiness_args:
        raise AssertionError("readiness script must check embedding kind")
    if not any("18012" in a for a in readiness_args):
        raise AssertionError("readiness script must target port 18012")
    if not any("qwen3-embedding-8b" in a for a in readiness_args):
        raise AssertionError("readiness script must use qwen3-embedding-8b")

    start_limit_bursts = re.findall(r"(?m)^StartLimitBurst=(\S+)$", unit)
    if start_limit_bursts != ["5"]:
        raise AssertionError(
            f"StartLimitBurst must be exactly 5, found {start_limit_bursts}"
        )
    if re.search(r"(?m)^Environment=GB10_CGROUP_REGISTRATION_PATH=", unit):
        raise AssertionError("embedding must not publish a cgroup registration")

    return {
        "model_len": int(expected_container_options["--max-model-len"][0]),
        "kv_mib": int(expected_container_options["--kv-cache-memory-bytes"][0][:-1]),
        "batched_tokens": int(expected_container_options["--max-num-batched-tokens"][0]),
        "seqs": int(expected_container_options["--max-num-seqs"][0]),
    }


class EmbeddingServiceContractTests(unittest.TestCase):
    def test_32k_profile_has_bounded_kv_for_coresident_stability(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        contract = _embedding_contract(unit)
        model_len = contract["model_len"]
        kv_mib = contract["kv_mib"]

        self.assertEqual(model_len, CONTRACT_TOKENS)
        self.assertEqual(kv_mib, 4_800)

        projected_kv_tokens = kv_mib * VALIDATED_KV_TOKENS // VALIDATED_KV_MIB
        required_kv_tokens = (
            CONTRACT_TOKENS * (10_000 + MIN_KV_MARGIN_BPS) + 9_999
        ) // 10_000
        self.assertGreaterEqual(projected_kv_tokens, required_kv_tokens)

    def test_quality_and_throughput_semantics_remain_unchanged(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        contract = _embedding_contract(unit)
        self.assertEqual(contract["batched_tokens"], 8_192)
        self.assertEqual(contract["seqs"], 64)


class HostileEmbeddingUnitMutationTests(unittest.TestCase):
    CONTRACT_TESTS = (
        "test_32k_profile_has_bounded_kv_for_coresident_stability",
        "test_quality_and_throughput_semantics_remain_unchanged",
    )

    def assert_contract_rejects(self, unit: str) -> None:
        suite = unittest.TestSuite(
            EmbeddingServiceContractTests(test_name)
            for test_name in self.CONTRACT_TESTS
        )
        result = unittest.TestResult()
        with patch.object(Path, "read_text", autospec=True, return_value=unit):
            suite.run(result)
        self.assertGreater(
            len(result.failures) + len(result.errors),
            0,
            "mutated unit unexpectedly satisfied the embedding contract",
        )

    @staticmethod
    def append_execstart_arg(unit: str, argument: str) -> str:
        return unit.replace(
            "    --gpu-memory-utilization 0.15 --enforce-eager",
            "    --gpu-memory-utilization 0.15 --enforce-eager \\\n"
            f"    {argument}",
            1,
        )

    def test_rejects_appended_conflicting_critical_options(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        for argument in (
            "--max-model-len 40960",
            "--dtype float16",
            "--gpu-memory-utilization 0.61",
        ):
            with self.subTest(argument=argument):
                self.assert_contract_rejects(
                    self.append_execstart_arg(unit, argument)
                )

    def test_rejects_comment_that_fakes_the_expected_value(self) -> None:
        unit = EMBEDDING_UNIT.read_text().replace(
            "--max-model-len 32768",
            "# --max-model-len 32768\n    --max-model-len 40960",
            1,
        )
        self.assert_contract_rejects(unit)

    def test_rejects_noncanonical_case(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        mutations = (
            unit.replace("--dtype bfloat16", "--dtype BFLOAT16", 1),
            unit.replace("--enforce-eager", "--ENFORCE-EAGER", 1),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assert_contract_rejects(mutation)

    def test_rejects_duplicate_post_start_readiness_poll(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        readiness = next(
            line for line in unit.splitlines() if line.startswith("ExecStartPost=")
        )
        self.assert_contract_rejects(f"{unit}\n{readiness}\n")

    def test_rejects_guardian_registration_from_embedding(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        unit = re.sub(
            r"(?m)^ExecStartPost=.*$",
            "ExecStartPost=/home/obj/.local/bin/"
            "llm_guard_proxy_publish_cgroup_registration.sh",
            unit,
            count=1,
        )
        self.assert_contract_rejects(unit)

    def test_rejects_forbidden_docker_host_options(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        image_line = f"  {EMBEDDING_IMAGE} \\\n"
        for argument in (
            "--cgroup-parent app.slice",
            "--dns 8.8.8.8",
        ):
            with self.subTest(argument=argument):
                mutation = unit.replace(
                    image_line,
                    f"  {argument} \\\n{image_line}",
                    1,
                )
                self.assertNotEqual(mutation, unit)
                self.assert_contract_rejects(mutation)

    def test_rejects_changed_start_limit_burst(self) -> None:
        unit = EMBEDDING_UNIT.read_text().replace(
            "StartLimitBurst=5", "StartLimitBurst=3", 1
        )
        self.assert_contract_rejects(unit)

    def test_rejects_changed_published_port(self) -> None:
        unit = EMBEDDING_UNIT.read_text().replace(
            "100.105.4.92:18012:8000",
            "100.105.4.92:18014:8000",
            1,
        )
        self.assert_contract_rejects(unit)

    def test_rejects_changed_oom_score_priority(self) -> None:
        prefix, separator, suffix = EMBEDDING_UNIT.read_text().rpartition(
            "--oom-score-adj 0"
        )
        self.assertTrue(separator)
        unit = prefix + "--oom-score-adj 100" + suffix
        self.assert_contract_rejects(unit)

    def test_rejects_docker_memory_option_after_image_boundary(self) -> None:
        unit = EMBEDDING_UNIT.read_text()
        image_line = f"  {EMBEDDING_IMAGE} \\\n"
        unit = unit.replace(
            image_line,
            image_line + "  --memory 20g \\\n",
            1,
        )
        self.assert_contract_rejects(unit)

    def test_rejects_extra_docker_memory_alias(self) -> None:
        unit = EMBEDDING_UNIT.read_text().replace(
            "  --entrypoint python3 \\\n",
            "  -m 24g \\\n"
            "  --entrypoint python3 \\\n",
            1,
        )
        self.assert_contract_rejects(unit)

    def test_rejects_extra_served_model_alias(self) -> None:
        unit = EMBEDDING_UNIT.read_text().replace(
            "--served-model-name qwen3-embedding-8b Qwen/Qwen3-Embedding-8B",
            "--served-model-name qwen3-embedding-8b Qwen/Qwen3-Embedding-8B extra-alias",
            1,
        )
        self.assert_contract_rejects(unit)


class EmbeddingDeploymentContractTests(unittest.TestCase):
    FORBIDDEN_NEIGHBORS = (
        "vllm-aeon-27b-dflash.service",
        "querit-4b-reranker.service",
        "vllm-qwen3-reranker-8b.service",
    )

    @staticmethod
    def section(text: str, start: str, end_pattern: str) -> str:
        start_at = text.find(start)
        if start_at < 0:
            raise AssertionError(f"missing deployment section marker: {start}")
        remainder = text[start_at:]
        end = re.search(end_pattern, remainder[len(start) :], re.MULTILINE)
        if end is None:
            return remainder
        return remainder[: len(start) + end.start()]

    @staticmethod
    def shell_commands(section: str) -> str:
        blocks = re.findall(r"```(?:bash|sh)\n(.*?)```", section, re.DOTALL)
        return "\n".join(blocks).replace("\\\n", " ")

    @staticmethod
    def systemctl_mutations(commands: str) -> list[tuple[str, str]]:
        actions = {"start", "stop", "restart", "try-restart", "reload-or-restart"}
        mutations: list[tuple[str, str]] = []
        for raw_line in commands.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "systemctl --user" not in line:
                continue
            tail = line.split("systemctl --user", 1)[1]
            try:
                argv = shlex.split(tail, comments=True, posix=True)
            except ValueError as error:
                raise AssertionError(
                    f"invalid documented systemctl command: {line}"
                ) from error
            action_index = next(
                (index for index, token in enumerate(argv) if token in actions),
                None,
            )
            if action_index is None:
                continue
            action = argv[action_index]
            for token in argv[action_index + 1 :]:
                target = token.rstrip(";")
                if target.endswith(".service"):
                    mutations.append((action, target))
        return mutations

    def test_documented_activation_and_rollback_mutate_only_embedding(self) -> None:
        sections = (
            self.section(
                README.read_text(),
                "### Embedding 32K profile activation and rollback",
                r"^### ",
            ),
            self.section(
                AGENT_PLAYBOOK.read_text(),
                "For updates on an already-running GB10",
                r"^---$",
            ),
        )
        for section in sections:
            commands = self.shell_commands(section)
            self.assertIn("scripts/gb10_activate_embedding_profile.sh", commands)
            self.assertNotIn("install -m 0644 systemd/vllm-embedding.service", commands)
            self.assertNotIn("systemctl --user --no-block restart", commands)


if __name__ == "__main__":
    unittest.main()
