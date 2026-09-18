"""
Covers the testable, pure-logic parts of realtime_kalshi_ws.py: message
parsing (given the confirmed docs.kalshi.com envelope shape) and market
discovery (given a mocked KalshiClient). The live WebSocket connection
itself isn't unit-tested here -- that was verified live against the
deployed droplet instead.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from tests.helpers import use_temp_db

use_temp_db()

from realtime_kalshi_ws import ParsedTick, discover_open_market_tickers, parse_message


class TestParseMessage(unittest.TestCase):
    def test_parses_ticker_message_with_dollar_string_prices(self):
        # Confirmed shape from docs.kalshi.com's own quick-start guide --
        # the SAME "_dollars" string convention as the REST /markets
        # endpoint, not plain integer-cents fields.
        raw = json.dumps({
            "type": "ticker",
            "sid": 7,
            "msg": {
                "market_ticker": "KXRAIN-26SEP18-HOU",
                "yes_bid_dollars": "0.5400",
                "yes_ask_dollars": "0.5600",
                "price_dollars": "0.5500",
                "volume": 120,
                "open_interest": 300,
                "ts": 1000,
            },
        })
        tick, msg_type, msg = parse_message(raw, received_ts=1001)
        self.assertEqual(msg_type, "ticker")
        self.assertIsInstance(tick, ParsedTick)
        self.assertEqual(tick.ticker, "KXRAIN-26SEP18-HOU")
        self.assertEqual(tick.yes_bid_cents, 54)
        self.assertEqual(tick.yes_ask_cents, 56)
        self.assertEqual(tick.yes_price_cents, 55)
        self.assertEqual(tick.volume, 120)
        self.assertEqual(tick.open_interest, 300)
        self.assertEqual(tick.ts, 1000)

    def test_parses_trade_message(self):
        raw = json.dumps({
            "type": "trade",
            "msg": {
                "market_ticker": "KXRAIN-26SEP18-HOU",
                "yes_price_dollars": "0.6000",
                "count": 15,
                "ts": 2000,
            },
        })
        tick, msg_type, msg = parse_message(raw, received_ts=2001)
        self.assertEqual(msg_type, "trade")
        self.assertEqual(tick.yes_price_cents, 60)
        self.assertEqual(tick.volume, 15)

    def test_control_messages_return_no_tick(self):
        for msg_type in ("subscribed", "error", "ok", "ping", "pong"):
            raw = json.dumps({"type": msg_type, "msg": {}})
            tick, parsed_type, _ = parse_message(raw, received_ts=1000)
            self.assertIsNone(tick, f"{msg_type} should not produce a tick row")
            self.assertEqual(parsed_type, msg_type)

    def test_message_missing_market_ticker_returns_no_tick(self):
        raw = json.dumps({"type": "ticker", "msg": {"yes_bid_dollars": "0.50"}})
        tick, _, _ = parse_message(raw, received_ts=1000)
        self.assertIsNone(tick)

    def test_missing_ts_falls_back_to_received_ts(self):
        raw = json.dumps({
            "type": "ticker",
            "msg": {"market_ticker": "KXRAIN-26SEP18-HOU", "yes_bid_dollars": "0.50"},
        })
        tick, _, _ = parse_message(raw, received_ts=9999)
        self.assertEqual(tick.ts, 9999)

    def test_unparseable_json_raises_rather_than_silently_dropping(self):
        # The listener's own recv loop catches this and logs the raw
        # message; parse_message itself should not swallow it.
        with self.assertRaises(json.JSONDecodeError):
            parse_message("not json", received_ts=1000)


class TestDiscoverOpenMarketTickers(unittest.TestCase):
    def test_collects_tickers_across_series_and_pages(self):
        kalshi = MagicMock()
        kalshi.discover_series_tickers.return_value = ["KXRAIN"]

        def fake_get_markets(series_ticker, status="open", cursor=None):
            if cursor is None:
                return {"markets": [{"ticker": "KXRAIN-26SEP18-HOU"}], "cursor": "page2"}
            return {"markets": [{"ticker": "KXRAIN-26SEP18-SEA"}], "cursor": None}

        kalshi.get_markets.side_effect = fake_get_markets

        result = discover_open_market_tickers(kalshi)
        self.assertEqual(result, {"KXRAIN-26SEP18-HOU", "KXRAIN-26SEP18-SEA"})

    def test_survives_a_failing_series_without_losing_the_others(self):
        kalshi = MagicMock()
        kalshi.discover_series_tickers.return_value = ["KXRAIN", "KXHIGHTEMP"]

        def fake_get_markets(series_ticker, status="open", cursor=None):
            if series_ticker == "KXRAIN":
                raise RuntimeError("simulated API failure")
            return {"markets": [{"ticker": "KXHIGHTEMP-26SEP18-NYC"}], "cursor": None}

        kalshi.get_markets.side_effect = fake_get_markets

        result = discover_open_market_tickers(kalshi)
        self.assertEqual(result, {"KXHIGHTEMP-26SEP18-NYC"})


if __name__ == "__main__":
    unittest.main()
