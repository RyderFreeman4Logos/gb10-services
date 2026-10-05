"""Activation fixtures must not execute ambient Python startup hooks."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from embedding_activation_fixtures import ActivationFixture
import test_embedding_activation_transaction as transaction_tests


class ActivationStartupTests(unittest.TestCase):
    def test_rollback_tools_ignore_ambient_python_startup(self) -> None:
        with ActivationFixture() as fixture:
            startup = fixture.root / "ambient-python"
            startup.mkdir()
            marker = startup / "executed.json"
            # Delay only a rollback tool, before it can emit stdout or stderr.
            # The engine itself already uses -I -S and is not intercepted.
            (startup / "sitecustomize.py").write_text(
                "import json, os, sys, time\n"
                "from pathlib import Path\n"
                f"phase = Path({str(fixture.transaction() / 'phase')!r})\n"
                "args = [arg for arg in sys.argv[1:] if arg != '--user']\n"
                "if args == ['daemon-reload'] and phase.exists() and phase.read_text().strip() == 'rolling_back':\n"
                "    pid = os.getpid()\n"
                "    start = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])\n"
                f"    Path({str(marker)!r}).write_text(json.dumps([pid, start]))\n"
                "    time.sleep(30)\n"
            )
            fixture.hooks["fail_at"] = "before_receipt"
            started = time.monotonic()
            with patch.dict(os.environ, {"PYTHONPATH": str(startup)}):
                result = fixture.run(optimized=True)
            elapsed = time.monotonic() - started
            # Keep the existing 2s command / 12s operation / 8s rollback bounds.
            self.assertLess(elapsed, 20, result.stderr)
            recorded = None
            if marker.exists():
                recorded = tuple(json.loads(marker.read_text()))
                self.assertNotEqual(transaction_tests._identity(recorded[0]), recorded, result.stderr)
            self.assertIn("test-only injected failure at before_receipt", result.stderr)
            self.assertNotEqual(result.returncode, 0)
            with self.subTest(startup_identity=recorded):
                transaction_tests.EmbeddingActivationTransactionTests().assert_restored(
                    fixture, result=result
                )
            self.assertFalse(marker.exists(), "fixture executed ambient startup code")
            self.assertFalse((fixture.state_root / "activation.receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
