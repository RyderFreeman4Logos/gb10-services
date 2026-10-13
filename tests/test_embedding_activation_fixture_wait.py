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
