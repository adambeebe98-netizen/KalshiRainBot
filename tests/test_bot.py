"""
Covers bot.py's pure, testable helper functions. bot.py's main() loop
itself isn't unit-tested here — it needs real Kalshi/Anthropic API
mocking at a scale better suited to a dedicated integration-test effort
— but hours_until_close() and is_far_future_rain() are genuinely
testable in isolation and had zero dedicated coverage before this.
"""
from __future__ import annotations

import datetime
import unittest

from tests.helpers import use_temp_db

use_temp_db()

import bot  # noqa: E402


def _iso_hours_from_now(hours: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class TestHoursUntilClose(unittest.TestCase):
    def test_computes_a_positive_value_for_a_future_close_time(self):
        result = bot.hours_until_close(_iso_hours_from_now(3.0))
        self.assertAlmostEqual(result, 3.0, places=1)

    def test_computes_a_negative_value_for_a_past_close_time(self):
        result = bot.hours_until_close(_iso_hours_from_now(-2.0))
        self.assertAlmostEqual(result, -2.0, places=1)

    def test_none_input_returns_none_not_a_crash(self):
        self.assertIsNone(bot.hours_until_close(None))

    def test_malformed_input_returns_none_not_a_crash(self):
        self.assertIsNone(bot.hours_until_close("not-a-real-date"))
        self.assertIsNone(bot.hours_until_close(""))


class TestIsFarFutureRain(unittest.TestCase):
    def test_beyond_the_horizon_is_far_future(self):
        far = _iso_hours_from_now(bot.RAIN_MAX_HORIZON_HOURS + 5)
        self.assertTrue(bot.is_far_future_rain(far))

    def test_within_the_horizon_is_not_far_future(self):
        near = _iso_hours_from_now(5.0)
        self.assertFalse(bot.is_far_future_rain(near))

    def test_exactly_at_the_horizon_is_not_far_future(self):
        """Boundary is exclusive (> not >=) -- exactly at the cutoff still
        counts as tradeable, not yet "too far out.\""""
        at_boundary = _iso_hours_from_now(bot.RAIN_MAX_HORIZON_HOURS)
        self.assertFalse(bot.is_far_future_rain(at_boundary))

    def test_missing_close_time_fails_open_not_far_future(self):
        """A parsing hiccup should never make a real, tradeable same-day
        market silently disappear -- fails OPEN (False), not closed."""
        self.assertFalse(bot.is_far_future_rain(None))

    def test_malformed_close_time_fails_open(self):
        self.assertFalse(bot.is_far_future_rain("garbage-not-a-date"))


if __name__ == "__main__":
    unittest.main()
