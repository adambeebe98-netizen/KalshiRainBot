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
from unittest.mock import patch, MagicMock

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


class TestGetGitCommit(unittest.TestCase):
    """CONFIRMED REAL MOTIVATION: a loss-analysis review flagged a
    favorites_baseline failure that looked identical to an already-fixed
    bug, with no way to tell from the trade data alone whether the fix
    was actually live when the trade happened — this exists so every
    trade can answer that question directly instead of requiring a
    separate manual check of pull/restart timing."""

    def setUp(self):
        # Reset the module-level cache before each test so mocked
        # subprocess calls actually get exercised, not skipped by an
        # earlier test's cached result.
        bot._CACHED_GIT_COMMIT = None
        bot._GIT_COMMIT_LOOKED_UP = False

    def test_returns_the_short_sha_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc1234\n")
            result = bot.get_git_commit()
        self.assertEqual(result, "abc1234")

    def test_caches_the_result_does_not_reshell_out(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc1234\n")
            first = bot.get_git_commit()
        with patch("subprocess.run") as mock_run2:
            second = bot.get_git_commit()
            mock_run2.assert_not_called()
        self.assertEqual(first, second)

    def test_returns_none_on_nonzero_exit_not_a_crash(self):
        """Not a git repo, or git not installed -- git rev-parse exits
        non-zero rather than raising; must fail gracefully, not crash."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = bot.get_git_commit()
        self.assertIsNone(result)

    def test_returns_none_on_exception_not_a_crash(self):
        """git not installed at all raises FileNotFoundError from
        subprocess.run -- must be caught, not propagated."""
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = bot.get_git_commit()
        self.assertIsNone(result)

    def test_real_environment_returns_none_gracefully_when_not_a_git_checkout(self):
        """Direct, unmocked confirmation: this test file's own directory
        is not a git checkout, so the real function (no mocking) must
        return None cleanly rather than raising."""
        result = bot.get_git_commit()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
