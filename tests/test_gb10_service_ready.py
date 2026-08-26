"""Contracts for the unified GB10 systemd readiness probe."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
READY = ROOT / "scripts" / "gb10_service_ready.sh"


class TestGb10ServiceReadyChatProbe(unittest.TestCase):
    def test_chat_probe_accepts_reasoning_without_thinking(self) -> None:
        text = READY.read_text(encoding="utf-8")
        self.assertIn("enable_thinking", text)
        self.assertIn("reasoning_content", text)
        self.assertIn("msg.get('content')", text)


if __name__ == "__main__":
    unittest.main()
