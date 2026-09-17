"""
Covers two real, previously-shipped bugs so neither can silently come back:

1. market_price_cents(): Kalshi's /markets list endpoint returns prices as
   "<field>_dollars" decimal strings (e.g. "yes_ask_dollars": "0.6100"),
   NOT plain "<field>" integer-cents fields. Every price read in this
   codebase silently returned None for one full night before this was
   found — confirmed against a live raw response dump.

2. The path-doubling regression: kalshi_client._request() must sign the
   full /trade-api/v2-prefixed path but send the actual HTTP request to
   the bare path alone, since KALSHI_BASE_URL already includes that
   prefix. This exact bug shipped TWICE in one session — once from a
   demo-test config that didn't match production's base_url shape, and
   again from editing a stale local copy of the file. This test exists
   specifically so a third occurrence fails CI instead of production.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.helpers import use_temp_db

use_temp_db()

from kalshi_client import market_price_cents, KalshiClient  # noqa: E402


class TestMarketPriceCents(unittest.TestCase):
    def test_real_houston_market_data(self):
        """The exact raw market object pulled live for KXRAIN-26SEP09-HOU."""
        real_market = {
            "close_time": "2026-09-10T06:00:00Z",
            "last_price_dollars": "0.6100",
            "no_ask_dollars": "0.4100",
            "no_bid_dollars": "0.3900",
            "ticker": "KXRAIN-26SEP09-HOU",
            "yes_ask_dollars": "0.6100",
            "yes_bid_dollars": "0.5900",
        }
        self.assertEqual(market_price_cents(real_market, "yes_ask"), 61)
        self.assertEqual(market_price_cents(real_market, "yes_bid"), 59)
        self.assertEqual(market_price_cents(real_market, "no_ask"), 41)
        self.assertEqual(market_price_cents(real_market, "no_bid"), 39)
        self.assertEqual(market_price_cents(real_market, "last_price"), 61)

    def test_missing_field_returns_none(self):
        self.assertIsNone(market_price_cents({"ticker": "X"}, "yes_ask"))

    def test_malformed_value_fails_open(self):
        self.assertIsNone(market_price_cents({"yes_ask_dollars": "not-a-number"}, "yes_ask"))

    def test_plain_unsuffixed_field_is_never_read(self):
        """The bug: this field name doesn't exist on the real endpoint at
        all. If someone "fixes" this function to also check the plain
        key, this test should still pass (dollars-suffixed takes
        priority) — but the real regression this guards is the OPPOSITE
        of what it looks like: don't let market_price_cents start
        preferring the plain key over the _dollars one."""
        market = {"yes_ask": 999, "yes_ask_dollars": "0.4500"}
        self.assertEqual(market_price_cents(market, "yes_ask"), 45)


class TestNoPathDoubling(unittest.TestCase):
    """kalshi_client._request must send the HTTP request to the bare path,
    signing the /trade-api/v2-prefixed path separately — never both."""

    def test_get_markets_does_not_double_the_path(self):
        captured = {}

        def fake_request(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url

            class FakeResp:
                status_code = 200

                def json(self):
                    return {"markets": []}
            return FakeResp()

        with patch("httpx.Client.request", fake_request):
            kc = KalshiClient()
            kc.get_markets(series_ticker="KXRAIN", status="open")

        self.assertEqual(captured["url"], "/markets",
                          "request path must be bare — KALSHI_BASE_URL already "
                          "carries /trade-api/v2, doubling it 404s every single call")

    def test_get_balance_does_not_double_the_path(self):
        captured = {}

        def fake_request(self, method, url, **kwargs):
            captured["url"] = url

            class FakeResp:
                status_code = 200

                def json(self):
                    return {"balance": 0}
            return FakeResp()

        with patch("httpx.Client.request", fake_request):
            kc = KalshiClient()
            kc.get_balance()

        self.assertEqual(captured["url"], "/portfolio/balance")


class TestHistoricalEndpoints(unittest.TestCase):
    """Foundation for retrospective backtesting against years of real
    weather-market history, not just data collected going forward.
    Explicitly requested: "if we go back, extract all of that available
    data that is applicable to the trades." """

    def _capture_request(self, response_json):
        captured = {}

        def fake_request(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["params"] = kwargs.get("params")

            class FakeResp:
                status_code = 200

                def json(self):
                    return response_json
            return FakeResp()
        return captured, fake_request

    def test_get_historical_markets_hits_the_bare_historical_path(self):
        captured, fake_request = self._capture_request({"markets": [], "cursor": ""})
        with patch("httpx.Client.request", fake_request):
            kc = KalshiClient()
            kc.get_historical_markets(series_ticker="KXHIGHNY")
        self.assertEqual(captured["url"], "/historical/markets")
        self.assertEqual(captured["params"]["series_ticker"], "KXHIGHNY")
        self.assertEqual(captured["params"]["status"], "settled")

    def test_get_historical_markets_supports_pagination_cursor(self):
        captured, fake_request = self._capture_request({"markets": [], "cursor": ""})
        with patch("httpx.Client.request", fake_request):
            kc = KalshiClient()
            kc.get_historical_markets(cursor="abc123")
        self.assertEqual(captured["params"]["cursor"], "abc123")

    def test_get_historical_candlesticks_hits_the_bare_historical_path(self):
        captured, fake_request = self._capture_request({"candlesticks": []})
        with patch("httpx.Client.request", fake_request):
            kc = KalshiClient()
            kc.get_historical_candlesticks("KXHIGHNY", "KXHIGHNY-26JUL15-T90", 1000, 2000)
        self.assertEqual(captured["url"], "/historical/markets/KXHIGHNY-26JUL15-T90/candlesticks")
        self.assertEqual(captured["params"]["start_ts"], 1000)
        self.assertEqual(captured["params"]["end_ts"], 2000)
        self.assertEqual(captured["params"]["period_interval"], 60)


if __name__ == "__main__":
    unittest.main()
