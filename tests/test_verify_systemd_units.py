"""Keep offline unit verification strict about external dependencies."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_systemd_units", ROOT / "scripts/verify_systemd_units.py"
)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class SystemdVerificationTests(unittest.TestCase):
    def test_only_declared_external_docker_dependency_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile/test"
            profile.mkdir(parents=True)
            for dependency, expected in (("docker.service", 0), ("unknown.service", 1)):
                with self.subTest(dependency=dependency):
                    (profile / "test.service").write_text(
                        f"[Unit]\nRequires={dependency}\nAfter={dependency}\n"
                        "[Service]\nExecStart=/usr/bin/true\n"
                    )
                    with patch.object(VERIFIER, "UNIT_SOURCE", root / "profile"):
                        self.assertEqual(VERIFIER.main(), expected)
