from __future__ import annotations

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

    def test_current_docs_publish_friendly_tag_and_immutable_digest(self) -> None:
        for path in CURRENT_DOCS:
            text = path.read_text()
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn(CURRENT_TAG, text)
                self.assertIn(CURRENT_DIGEST, text)


if __name__ == "__main__":
    unittest.main()
