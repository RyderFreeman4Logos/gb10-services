from __future__ import annotations

import json
import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRENT_TAG = "2026-07-16-v0.25.1"
CURRENT_DIGEST = (
    "sha256:c15e2c4b767c611fc739046129d550d0c347c906a3c9020888acc981f55f137d"
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
    ROOT / "docs" / "research" / "2026-07-16-querit-vllm-migration.md",
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

    def test_every_aeon_unit_uses_the_current_repository_digest(self) -> None:
        units = sorted((ROOT / "systemd").glob("*.service"))
        aeon_units = [
            path for path in units if "aeon-vllm-ultimate" in path.read_text()
        ]
        self.assertGreaterEqual(len(aeon_units), 5)
        for path in aeon_units:
            text = path.read_text()
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn(
                    f"ghcr.io/aeon-7/aeon-vllm-ultimate@{CURRENT_DIGEST}", text
                )
                self.assertNotRegex(text, r"aeon-vllm-ultimate:[^\s\\]+")

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
            "tracked clean-start v0.25.1 reference",
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

    def test_current_docs_publish_friendly_tag_and_immutable_digest(self) -> None:
        for path in CURRENT_DOCS:
            text = path.read_text()
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn(CURRENT_TAG, text)
                self.assertIn(CURRENT_DIGEST, text)


if __name__ == "__main__":
    unittest.main()
