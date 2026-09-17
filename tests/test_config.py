"""
CONFIRMED LIVE via a real historical backfill run: the discovery keyword
"rain" (no KX prefix) is a naive case-insensitive substring match against
every series ticker AND title (see KalshiClient.discover_series_tickers),
so it doesn't just match KXRAIN variants — it also matched real, confirmed
series that have nothing to do with weather: KXDRAINTHESWAMP (contains
"rain" inside "d-RAIN-theswamp"), KXELECTUKRAINE and KXUKRAINEEU (both
contain "rain" inside "uk-RAIN-e"). "KXRAIN" instead still matches every
real rain series while excluding those.

Deliberately NOT similarly tightening KXHIGH/KXLOW — Kalshi genuinely
reuses that exact prefix for non-weather threshold markets too (also
confirmed live: KXHIGHINFLATION, KXLOW-26NOVCOMP), so no string-matching
fix distinguishes them. That's what the real rules-text measure
classification downstream is already for, and it correctly handles it: a
non-weather market gets measure="other" and is skipped (see bot.py's
scan_and_trade) before ever reaching orderbook fetching or a trading
decision — confirmed by tracing that exact code path, not assumed.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("STARTING_BANKROLL_CENTS", "50000")

from config import SETTINGS  # noqa: E402


class TestDiscoveryKeywordsDefault(unittest.TestCase):
    def test_default_uses_kxrain_not_bare_rain(self):
        self.assertIn("KXRAIN", SETTINGS.discovery_keywords)
        self.assertNotIn("rain", SETTINGS.discovery_keywords)


class TestDiscoveryKeywordMatchingAgainstConfirmedRealTickers(unittest.TestCase):
    """Reproduces the exact substring-matching logic in
    KalshiClient.discover_series_tickers directly, against tickers
    actually observed in a real historical backfill run — not
    hypothetical examples."""

    def _matches(self, keyword: str, ticker: str) -> bool:
        return keyword.lower() in ticker.lower()

    def test_kxrain_excludes_the_confirmed_false_positives(self):
        for ticker in ["KXDRAINTHESWAMP", "KXELECTUKRAINE", "KXUKRAINEEU"]:
            self.assertFalse(self._matches("KXRAIN", ticker),
                              f"{ticker} should NOT match KXRAIN, but the old bare 'rain' keyword matched it")

    def test_kxrain_still_matches_every_confirmed_real_rain_series(self):
        for ticker in ["KXRAIN", "KXRAINNYCM", "KXRAINSEAM", "KXRAINSFOM"]:
            self.assertTrue(self._matches("KXRAIN", ticker))

    def test_bare_rain_would_have_matched_the_false_positives_confirming_the_original_bug(self):
        """Not testing current behavior -- documenting exactly why the
        old default was wrong, so this regression can't silently
        reappear if someone "simplifies" the keyword back to bare 'rain'."""
        for ticker in ["KXDRAINTHESWAMP", "KXELECTUKRAINE", "KXUKRAINEEU"]:
            self.assertTrue(self._matches("rain", ticker),
                             f"confirms {ticker} really was the source of the original false-positive bug")


if __name__ == "__main__":
    unittest.main()
