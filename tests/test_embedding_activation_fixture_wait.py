"""The pause rendezvous must cover the configured activation/rollback budget."""

import unittest
from unittest.mock import patch

from embedding_activation_fixtures import ActivationFixture


class ActivationFixtureWaitTests(unittest.TestCase):
    def test_pause_after_five_seconds_within_transaction_budget_is_observed(self) -> None:
        with ActivationFixture() as fixture:
            elapsed = 0.0

            def advance(seconds: float) -> None:
                nonlocal elapsed
                elapsed += seconds
                if elapsed >= 6:
                    fixture.marker.write_text("reached\n")

            with patch("embedding_activation_fixtures.time") as clock:
                clock.monotonic.side_effect = lambda: elapsed
                clock.sleep.side_effect = advance
                fixture.wait_for_marker()
            self.assertTrue(fixture.marker.exists())

    def test_missing_pause_still_has_a_deadline(self) -> None:
        with ActivationFixture() as fixture:
            elapsed = 0.0

            def advance(seconds: float) -> None:
                nonlocal elapsed
                elapsed += seconds

            with patch("embedding_activation_fixtures.time") as clock:
                clock.monotonic.side_effect = lambda: elapsed
                clock.sleep.side_effect = advance
                with self.assertRaisesRegex(AssertionError, "pause boundary"):
                    fixture.wait_for_marker(timeout=0.1)
            self.assertLess(elapsed, 0.2)

    def test_default_missing_pause_expires_at_configured_budget(self) -> None:
        import json

        with ActivationFixture() as fixture:
            config = json.loads(fixture.config.read_text())
            self.assertEqual(
                [config[key] for key in ("command_seconds", "ready_seconds", "deadline_seconds", "rollback_seconds")],
                [2, 2, 12, 8],
            )
            elapsed = 0.0

            def advance(seconds: float) -> None:
                nonlocal elapsed
                elapsed += seconds

            with patch("embedding_activation_fixtures.time") as clock:
                clock.monotonic.side_effect = lambda: elapsed
                clock.sleep.side_effect = advance
                with self.assertRaisesRegex(AssertionError, "pause boundary"):
                    fixture.wait_for_marker()
            self.assertGreaterEqual(elapsed, 20)
            self.assertLess(elapsed, 20.03)
            self.assertEqual(json.loads(fixture.config.read_text()), config)

    def test_default_missing_pause_follows_nondefault_config_budget(self) -> None:
        import json

        with ActivationFixture() as fixture:
            config = json.loads(fixture.config.read_text())
            config.update(deadline_seconds=3, rollback_seconds=4)
            fixture.config.write_text(json.dumps(config))
            elapsed = 0.0

            def advance(seconds: float) -> None:
                nonlocal elapsed
                elapsed += seconds

            with patch("embedding_activation_fixtures.time") as clock:
                clock.monotonic.side_effect = lambda: elapsed
                clock.sleep.side_effect = advance
                with self.assertRaisesRegex(AssertionError, "pause boundary"):
                    fixture.wait_for_marker()
            self.assertGreaterEqual(elapsed, 7)
            self.assertLess(elapsed, 7.03)
            self.assertEqual(json.loads(fixture.config.read_text()), config)
