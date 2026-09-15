from __future__ import annotations

import json
import re
import shlex
import unittest
from dataclasses import dataclass
from pathlib import Path

from test_querit_vllm_production_contracts import _unit_directive_values


ROOT = Path(__file__).resolve().parents[1]
IMAGE_REPOSITORY = "ghcr.io/aeon-7/aeon-vllm-ultimate"
UNIT_PATHS = {
    "vllm-aeon-27b-dflash.service": ROOT
    / "profile"
    / "qwen3.6-27b-decensor-by-aeon"
    / "vllm-aeon-27b-dflash.service",
    "vllm-embedding.service": ROOT
    / "profile"
    / "qwen3-embedding-8b"
    / "vllm-embedding.service",
    "vllm-querit-4b-reranker.service": ROOT
    / "profile"
    / "querit-4b-reranker"
    / "vllm-querit-4b-reranker.service",
    "vllm-qwen3-reranker-8b.service": ROOT
    / "profile"
    / "qwen3-reranker-8b"
    / "vllm-qwen3-reranker-8b.service",
}


@dataclass(frozen=True)
class ImageRelease:
    date: str
    version: str
    digest: str

    @property
    def tag(self) -> str:
        return f"{self.date}-{self.version}"

    @property
    def image_reference(self) -> str:
        return f"{IMAGE_REPOSITORY}@{self.digest}"


CURRENT_RELEASE = ImageRelease(
    date="2026-09-11",
    version="v0.29.0-omni",
    digest="sha256:2421bb1228a85370c1c50adb31f605c4361acf4d48d65282fcb919e74f34fae7",
)
PREVIOUS_RELEASE = ImageRelease(
    date="2026-07-16",
    version="v0.25.1",
    digest="sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d",
)
SUPERSEDED_MARKERS = (
    "2026-08-24-v0.27.1-omni",
    "2026-08-17-v0.27.1-slim",
    "e62ac10d744ed7c8f3dd4d5631be0f7615870a88c327db9c1d382a27b36a61ee",
    "2fb855ffd6fbf4330cf9f4653c09d3e6584d197acba8e9e93a032da36bb4559f",
    "2026-07-14-v0.25.0",
    "18c09e6b",
    "0.25.0+aeon.sm121a.dflash",
    "v0.25.0",
)
CURRENT_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "deployment" / "AGENTS.md",
)
UNIT_RELEASE_ANNOTATION = re.compile(
    r"^# AEON image release: "
    r"(?P<date>\d{4}-\d{2}-\d{2})-"
    r"(?P<version>v\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?); "
    r"immutable digest: (?P<digest>sha256:[0-9a-f]{64})$",
    re.MULTILINE,
)


def _operational_files() -> list[Path]:
    files: list[Path] = []
    for directory in (ROOT / "scripts", ROOT / "profile", ROOT / "tests"):
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path != Path(__file__)
        )
    return sorted(files)


def _release_annotations(text: str) -> list[ImageRelease]:
    return [
        ImageRelease(
            date=match.group("date"),
            version=match.group("version"),
            digest=match.group("digest"),
        )
        for match in UNIT_RELEASE_ANNOTATION.finditer(text)
    ]


class VllmImageIdentityContractTests(unittest.TestCase):
    def test_operational_tree_cannot_use_superseded_image_identity(self) -> None:
        for path in _operational_files():
            text = path.read_text()
            for marker in SUPERSEDED_MARKERS:
                with self.subTest(path=path.relative_to(ROOT), marker=marker):
                    self.assertNotIn(marker, text)

    def test_current_docs_mark_every_retained_old_identity_as_rollback(self) -> None:
        for path in CURRENT_DOCS:
            for line_number, line in enumerate(path.read_text().splitlines(), 1):
                if any(marker in line for marker in SUPERSEDED_MARKERS):
                    with self.subTest(
                        path=path.relative_to(ROOT), line_number=line_number
                    ):
                        self.assertRegex(line.lower(), r"rollback|superseded")

    def test_historical_v0251_evidence_retains_the_previous_digest(self) -> None:
        querit_history = (
            ROOT / "docs" / "research" / "2026-07-16-querit-vllm-migration.md"
        ).read_text()
        self.assertIn(
            "Image tag `2026-07-16-v0.25.1` resolves to repository digest "
            f"`{PREVIOUS_RELEASE.digest}`",
            querit_history,
        )

        text_history = (
            ROOT
            / "docs"
            / "research"
            / "2026-07-17-aeon-text-post-ready-uma-high-water.md"
        ).read_text()
        self.assertIn(f"- digest: `{PREVIOUS_RELEASE.digest}`", text_history)
        self.assertIn("- version family: v0.25.1", text_history)

    def test_every_aeon_unit_binds_version_label_and_digest_to_one_release(self) -> None:
        units = sorted((ROOT / "profile").glob("*/*.service"))
        aeon_units = [
            path
            for path in units
            if not path.is_symlink() and "aeon-vllm-ultimate" in path.read_text()
        ]
        self.assertEqual(
            {path.name for path in aeon_units},
            {
                "vllm-aeon-27b-dflash.service",
                "vllm-embedding.service",
                "vllm-aeon-qwen38-dflash.service",
                "vllm-aeon-ultimate-uncensored-nvfp4.service",
                "vllm-qwen3-reranker-8b.service",
                "vllm-querit-4b-reranker.service",
            },
        )
        for path in aeon_units:
            text = path.read_text()
            with self.subTest(path=path.relative_to(ROOT)):
                annotations = _release_annotations(text)
                self.assertEqual(annotations, [CURRENT_RELEASE])
                self.assertEqual(
                    re.findall(
                        rf"{re.escape(IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}",
                        text,
                    ),
                    [CURRENT_RELEASE.image_reference],
                )
                descriptions = [
                    line
                    for line in text.splitlines()
                    if line.startswith("Description=")
                ]
                self.assertEqual(len(descriptions), 1)
                self.assertIn(CURRENT_RELEASE.version, descriptions[0])
                self.assertNotRegex(text, r"aeon-vllm-ultimate:[^\s\\]+")

    def test_aeon_compile_cache_namespace_rotates_with_the_release(self) -> None:
        host_cache = "/home/obj/.cache/vllm-compile/aeon-qwen36-v0290-2421bb"
        container_cache = "/var/cache/vllm/aeon-qwen36-v0290"
        for unit_name in ("vllm-aeon-27b-dflash.service",):
            text = UNIT_PATHS[unit_name].read_text()
            with self.subTest(unit=unit_name):
                self.assertIn(f"ExecStartPre=/usr/bin/install -d -m 0700 {host_cache}", text)
                self.assertIn(f"-v {host_cache}:{container_cache}", text)
                self.assertIn(
                    f'\\"cache_dir\\":\\"{container_cache}\\"',
                    text,
                )
                self.assertNotIn("aeon-qwen36-v0251-c15e2c", text)
                self.assertNotIn("/var/cache/vllm/aeon-qwen36-v0251", text)

    def test_aeon_text_pins_v2_runner_for_native_thinking_budget(self) -> None:
        text = UNIT_PATHS["vllm-aeon-27b-dflash.service"].read_text()
        self.assertIn(
            "  -e AEON_DEFAULT_THINKING_TOKEN_BUDGET=32768 \\\n"
            "  -e VLLM_USE_V2_MODEL_RUNNER=0 \\\n",
            text,
        )
        for unit_name in (
            "vllm-embedding.service",
            "vllm-querit-4b-reranker.service",
            "vllm-qwen3-reranker-8b.service",
        ):
            self.assertNotIn(
                "-e VLLM_USE_V2_MODEL_RUNNER=0",
                UNIT_PATHS[unit_name].read_text(),
            )

    def test_aeon_ultimate_uses_the_v2_runner_for_native_thinking_budgets(self) -> None:
        unit = (
            ROOT
            / "profile"
            / "aeon-ultimate-uncensored-nvfp4"
            / "vllm-aeon-ultimate-uncensored-nvfp4.service"
        ).read_text()
        self.assertRegex(
            unit,
            re.compile(
                r"(?m)^  -e VLLM_USE_V2_MODEL_RUNNER=1 \\\n"
                r"  --memory-swappiness 0"
            ),
        )
        self.assertEqual(
            re.findall(
                r"(?m)^\s*-e (VLLM_USE_V2_MODEL_RUNNER=[^\s\\]+)\s*\\$", unit
            ),
            ["VLLM_USE_V2_MODEL_RUNNER=1"],
        )

        guide = (ROOT / "docs" / "deployment" / "AGENTS.md").read_text()
        self.assertIn("`VLLM_USE_V2_MODEL_RUNNER=1`", guide)
        self.assertIn("`thinking_token_budget`", guide)

    def test_current_docs_publish_one_coherent_release_identity(self) -> None:
        readme = (ROOT / "README.md").read_text()
        self.assertIn("pinned AEON v0.29.0-omni GB10 Docker image", readme)
        self.assertNotIn("pinned AEON v0.25 GB10 Docker image", readme)
        self.assertRegex(
            readme,
            re.compile(
                rf"friendly tag: {re.escape(IMAGE_REPOSITORY)}:"
                rf"{re.escape(CURRENT_RELEASE.tag)}\n"
                rf"repository digest: {re.escape(CURRENT_RELEASE.digest)}\n"
                r"rollback/superseded: .*\n"
                rf"runtime version: {re.escape(CURRENT_RELEASE.version)}\b"
            ),
        )

        guide = (ROOT / "docs" / "deployment" / "AGENTS.md").read_text()
        self.assertIn(
            f"`{IMAGE_REPOSITORY}:{CURRENT_RELEASE.tag}` "
            f"(`{CURRENT_RELEASE.digest}`; runtime `{CURRENT_RELEASE.version}`)",
            guide,
        )

    def test_current_deployment_docs_install_the_canonical_aeon_unit_and_alias(self) -> None:
        readme = (ROOT / "README.md").read_text()
        self.assertIn(
            "profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service",
            readme,
        )
        self.assertIn("profile/qwen3-embedding-8b/vllm-embedding.service", readme)
        self.assertIn(
            "profile/querit-4b-reranker/vllm-querit-4b-reranker.service",
            readme,
        )
        self.assertIn("vllm-aeon-27b-dflash-hikv.service", readme)
        self.assertIn("active.env", readme)

        guide = (ROOT / "docs" / "deployment" / "AGENTS.md").read_text()
        self.assertIn(
            "profile/aeon-ultimate-uncensored-nvfp4/vllm-aeon-ultimate-uncensored-nvfp4.service",
            guide,
        )
        self.assertIn("profile/qwen3-embedding-8b/vllm-embedding.service", guide)
        self.assertIn(
            "profile/querit-4b-reranker/vllm-querit-4b-reranker.service",
            guide,
        )
        self.assertIn(
            "config/aeon-dflash-profiles/aeon-ultimate-uncensored-nvfp4.env",
            guide,
        )
        self.assertIn(
            "install -m 0755 scripts/gb10_service_ready.sh "
            "/home/obj/.local/bin/gb10_service_ready.sh",
            guide,
        )
        self.assertRegex(
            guide,
            r"(?m)^\s*\* `18010`: `vllm-aeon-ultimate-uncensored-nvfp4.service`",
        )
        enable_section = guide.split("### 6. Enable and Start Services", 1)[1].split(
            "### Model lifecycle audit and investigation lock", 1
        )[0]
        self.assertIn(
            "systemctl --user enable --now vllm-aeon-ultimate-uncensored-nvfp4.service",
            enable_section,
        )
        self.assertNotIn(
            "systemctl --user enable --now vllm-aeon-27b-dflash.service",
            enable_section,
        )
        self.assertLess(
            enable_section.find("systemctl --user enable --now vllm-embedding.service"),
            enable_section.find(
                "systemctl --user enable --now vllm-querit-4b-reranker.service"
            ),
        )
        self.assertLess(
            enable_section.find(
                "systemctl --user enable --now vllm-querit-4b-reranker.service"
            ),
            enable_section.find(
                "systemctl --user enable --now vllm-aeon-ultimate-uncensored-nvfp4.service"
            ),
        )
        for command in (
            "/home/obj/.local/bin/gb10_lifecycle.sh stop \\\n"
            "  --unit vllm-aeon-ultimate-uncensored-nvfp4.service",
            "systemctl --user status vllm-embedding vllm-aeon-ultimate-uncensored-nvfp4 "
            "vllm-querit-4b-reranker",
            "journalctl --user -u vllm-aeon-ultimate-uncensored-nvfp4.service -n 50 --no-pager",
        ):
            self.assertIn(command, guide)
        recovery = guide.split("### 1. CUDA Hang or Service Crash", 1)[1].split(
            "### 2. Generation-bound cleanup failures", 1
        )[0]
        self.assertIn("systemctl --user stop llm-guard-proxy.service", recovery)
        self.assertNotIn("systemctl --user disable", recovery)
        self.assertNotIn("investigation-", recovery)
        self.assertIn(
            "/home/obj/.local/bin/gb10_lifecycle.sh stop \\\n"
            "  --unit vllm-aeon-ultimate-uncensored-nvfp4.service",
            recovery,
        )
        self.assertIn("systemctl --user start llm-guard-proxy.service", recovery)
        fallback = guide.split("### 27B DFlash fallback", 1)[1].split("### ", 1)[0]
        self.assertIn(
            "profile/qwen3.6-27b-decensor-by-aeon/vllm-aeon-27b-dflash.service",
            fallback,
        )
        self.assertIn("vllm-aeon-27b-dflash-hikv.service", fallback)
        self.assertIn("active.env", fallback)
        self.assertIn("explicit non-default fallback", fallback)

        helper = (ROOT / "scripts" / "aeon_text_stop_start.sh").read_text()
        self.assertIn(
            "# Recycles the canonical Ultimate :18010 owner; legacy 27B active.env is not a selector.",
            helper,
        )
        self.assertNotIn(
            "Profile selection is the installed aeon-dflash-profiles/active.env symlink.",
            helper,
        )

    def test_aeon_tracked_runtime_profile_matches_deployment_reference(self) -> None:
        unit = UNIT_PATHS["vllm-aeon-27b-dflash.service"]
        unit_text = unit.read_text()
        unit_lines = unit_text.splitlines()
        start = next(
            index for index, line in enumerate(unit_lines) if line.startswith("ExecStart=")
        )
        command_lines = [unit_lines[start].removeprefix("ExecStart=")]
        while command_lines[-1].rstrip().endswith("\\"):
            start += 1
            self.assertLess(start, len(unit_lines))
            command_lines.append(unit_lines[start].strip())

        command = " ".join(
            line.rstrip().removesuffix("\\").rstrip() for line in command_lines
        )
        argv = shlex.split(command)
        serve_index = argv.index("serve")
        runtime_argv = argv[serve_index + 1 :]

        def option_value(option: str) -> str:
            self.assertIn(option, runtime_argv)
            index = runtime_argv.index(option)
            self.assertLess(index + 1, len(runtime_argv))
            return runtime_argv[index + 1]

        self.assertEqual(option_value("--max-model-len"), "262144")
        self.assertEqual(option_value("--max-num-seqs"), "16")
        self.assertEqual(option_value("--max-num-batched-tokens"), "4096")
        self.assertEqual(option_value("--gpu-memory-utilization"), "${AEON_GPU_MEMORY_UTILIZATION}")
        self.assertEqual(option_value("--kv-cache-dtype"), "fp8_e4m3")
        self.assertEqual(option_value("--attention-backend"), "TRITON_ATTN")
        served_name_index = runtime_argv.index("--served-model-name")
        self.assertEqual(
            runtime_argv[served_name_index + 1 : served_name_index + 4],
            [
                "aeon-ultimate",
                "qwen3.6-27b-decensor-by-aeon",
                "qwen3.6-27b-decensored",
            ],
        )
        speculative = json.loads(option_value("--speculative-config"))
        self.assertEqual(speculative["method"], "dflash")
        self.assertEqual(speculative["num_speculative_tokens"], 10)
        self.assertNotIn("--kv-cache-memory-bytes", runtime_argv)

        guide = (ROOT / "docs" / "deployment" / "AGENTS.md").read_text()
        reference_rows = [
            line
            for line in guide.splitlines()
            if line.startswith("* `vllm-aeon-27b-dflash.service`")
        ]
        self.assertEqual(len(reference_rows), 1)
        reference_row = reference_rows[0]
        for expected in (
            "retained 27B DFlash fallback",
            "DFlash n=10",
            "kv-cache-dtype=fp8_e4m3",
            "attention-backend=TRITON_ATTN",
            "max-model-len=262144",
            "max-num-seqs=16",
            "max-num-batched-tokens=4096",
            "baseline.env",
            "gpu-memory-utilization=0.355",
            "hikv.env",
            "286,962 KV tokens",
            "record a v0.29.0 live receipt",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, reference_row)
        self.assertNotIn("max-num-batched-tokens=32768", reference_row)
        self.assertNotIn("kv-cache-memory-bytes=15360M", reference_row)
        self.assertNotIn("269,589 KV tokens", reference_row)
        self.assertNotIn("pinned KV 15G", unit_text)
        self.assertNotIn("15GiB KV verified 269589", unit_text)
        for alias in (
            "aeon-ultimate",
            "qwen3.6-27b-decensor-by-aeon",
            "qwen3.6-27b-decensored",
        ):
            with self.subTest(alias=alias):
                self.assertIn(alias, guide)

    def test_qwen38_uses_in_checkpoint_mtp_k5_not_dflash(self) -> None:
        unit = (
            ROOT
            / "profile"
            / "qwen3.8-27b-nvfp4-vllm"
            / "vllm-aeon-qwen38-dflash.service"
        ).read_text()
        unit_lines = unit.splitlines()
        start = next(
            index for index, line in enumerate(unit_lines) if line.startswith("ExecStart=")
        )
        command_lines = [unit_lines[start].removeprefix("ExecStart=")]
        while command_lines[-1].rstrip().endswith("\\"):
            start += 1
            self.assertLess(start, len(unit_lines))
            command_lines.append(unit_lines[start].strip())
        command = " ".join(
            line.rstrip().removesuffix("\\").rstrip() for line in command_lines
        )
        argv = shlex.split(command)
        runtime = argv[argv.index("serve") + 1 :]
        speculative = json.loads(runtime[runtime.index("--speculative-config") + 1])
        self.assertEqual(speculative["method"], "qwen3_5_mtp")
        self.assertEqual(speculative["num_speculative_tokens"], 5)
        self.assertNotIn("model", speculative)
        self.assertNotIn("/draft", unit)
        self.assertIn("--enable-prefix-caching", runtime)
        self.assertNotIn("--no-enable-prefix-caching", runtime)
        conflicts = set(" ".join(_unit_directive_values(unit, "Conflicts")).split())
        self.assertIn("sglang-qwen38-27b.service", conflicts)
        self.assertIn("vllm-aeon-27b-dflash.service", conflicts)


if __name__ == "__main__":
    unittest.main()
