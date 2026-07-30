from __future__ import annotations

import json
import re
import shlex
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE_REPOSITORY = "ghcr.io/aeon-7/aeon-vllm-ultimate"


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
    date="2026-07-27",
    version="v0.26.0",
    digest="sha256:1aa47363e4c9cfa0a85411c669d39b7f9fa3adb3e735ef1ca5760be3044dacd7",
)
PREVIOUS_RELEASE = ImageRelease(
    date="2026-07-16",
    version="v0.25.1",
    digest="sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d",
)
SUPERSEDED_MARKERS = (
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
    r"(?P<version>v\d+\.\d+\.\d+); "
    r"immutable digest: (?P<digest>sha256:[0-9a-f]{64})$",
    re.MULTILINE,
)


def _operational_files() -> list[Path]:
    files: list[Path] = []
    for directory in (ROOT / "scripts", ROOT / "systemd", ROOT / "tests"):
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
        units = sorted((ROOT / "systemd").glob("*.service"))
        aeon_units = [
            path for path in units if "aeon-vllm-ultimate" in path.read_text()
        ]
        self.assertEqual(
            {path.name for path in aeon_units},
            {
                "vllm-aeon-27b-dflash.service",
                "vllm-aeon-27b-dflash-hikv.service",
                "vllm-embedding.service",
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
        host_cache = "/home/obj/.cache/vllm-compile/aeon-qwen36-v0260-1aa473"
        container_cache = "/var/cache/vllm/aeon-qwen36-v0260"
        for unit_name in (
            "vllm-aeon-27b-dflash.service",
            "vllm-aeon-27b-dflash-hikv.service",
        ):
            text = (ROOT / "systemd" / unit_name).read_text()
            with self.subTest(unit=unit_name):
                self.assertIn(f"ExecStartPre=/usr/bin/install -d -m 0700 {host_cache}", text)
                self.assertIn(f"-v {host_cache}:{container_cache}", text)
                self.assertIn(
                    f'\\"cache_dir\\":\\"{container_cache}\\"',
                    text,
                )
                self.assertNotIn("aeon-qwen36-v0251-c15e2c", text)
                self.assertNotIn("/var/cache/vllm/aeon-qwen36-v0251", text)

    def test_current_docs_publish_one_coherent_release_identity(self) -> None:
        readme = (ROOT / "README.md").read_text()
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

    def test_aeon_tracked_runtime_profile_matches_deployment_reference(self) -> None:
        unit = ROOT / "systemd" / "vllm-aeon-27b-dflash.service"
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
        self.assertEqual(option_value("--gpu-memory-utilization"), "0.355")
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
            "tracked v0.26.0 source profile",
            "DFlash n=10",
            "kv-cache-dtype=fp8_e4m3",
            "attention-backend=TRITON_ATTN",
            "max-model-len=262144",
            "max-num-seqs=16",
            "max-num-batched-tokens=4096",
            "AUTO KV",
            "gpu-memory-utilization=0.355",
            "no explicit `kv-cache-memory-bytes`",
            "286,962 KV tokens",
            "not a live-production activation claim",
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


if __name__ == "__main__":
    unittest.main()
