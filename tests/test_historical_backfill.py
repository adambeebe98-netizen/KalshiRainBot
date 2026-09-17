"""
Foundation for retrospective backtesting, explicitly requested: "if we
go back, extract all of that available data that is applicable to the
trades." These tests verify the orchestration logic end to end with
mocked Kalshi/rules-extraction/weather calls, since the real external
APIs could not be reached from the environment that wrote this (see
historical_backfill.py's own docstring for the confirmed limitation).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tests.helpers import use_temp_db

use_temp_db()

import storage  # noqa: E402
import historical_backfill as hb  # noqa: E402
from historical_weather import HistoricalHourlyPoint  # noqa: E402


class TestExtractCandlestickPriceCents(unittest.TestCase):
    def test_prefers_actual_traded_price(self):
        candle = {"price": {"close": "0.4500"}, "yes_ask": {"close": "0.5000"}, "yes_bid": {"close": "0.4000"}}
        self.assertEqual(hb._extract_candlestick_price_cents(candle), 45)

    def test_falls_back_to_yes_ask_when_no_trade_occurred(self):
        candle = {"price": {"close": None}, "yes_ask": {"close": "0.6200"}, "yes_bid": {"close": "0.5800"}}
        self.assertEqual(hb._extract_candlestick_price_cents(candle), 62)

    def test_falls_back_to_yes_bid_when_ask_also_missing(self):
        candle = {"price": {}, "yes_ask": {}, "yes_bid": {"close": "0.3300"}}
        self.assertEqual(hb._extract_candlestick_price_cents(candle), 33)

    def test_returns_none_when_nothing_available(self):
        candle = {"price": {}, "yes_ask": {}, "yes_bid": {}}
        self.assertIsNone(hb._extract_candlestick_price_cents(candle))

    def test_malformed_price_string_does_not_crash(self):
        candle = {"price": {"close": "not-a-number"}, "yes_ask": {"close": "0.5000"}, "yes_bid": {}}
        self.assertEqual(hb._extract_candlestick_price_cents(candle), 50)


class TestExtractVolume(unittest.TestCase):
    def test_parses_fixed_point_string(self):
        self.assertEqual(hb._extract_volume({"volume": "10.00"}), 10)

    def test_missing_volume_returns_none(self):
        self.assertIsNone(hb._extract_volume({}))


class TestParseIsoToUnix(unittest.TestCase):
    def test_parses_zulu_timestamp(self):
        result = hb._parse_iso_to_unix("2024-07-15T00:00:00Z")
        self.assertIsInstance(result, int)

    def test_none_input_returns_none(self):
        self.assertIsNone(hb._parse_iso_to_unix(None))

    def test_malformed_input_returns_none_not_raise(self):
        self.assertIsNone(hb._parse_iso_to_unix("not-a-timestamp"))


class TestBackfillWeatherForStation(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM historical_weather_points")
            conn.commit()

    def test_skips_when_already_covered(self):
        storage.save_historical_weather_points("KAUS", [(1000, 88.0, 20.0, 87.0, 0.0)])
        with patch("historical_weather.get_historical_forecast_hourly") as mock_forecast:
            result = hb.backfill_weather_for_station("KAUS", 900, 1100)
        self.assertFalse(result)
        mock_forecast.assert_not_called()

    def test_unknown_station_is_skipped_gracefully(self):
        result = hb.backfill_weather_for_station("ZZZZ_NOT_A_REAL_STATION", 1000, 2000)
        self.assertFalse(result)

    def test_merges_forecast_and_observation_by_timestamp(self):
        with patch("historical_weather.get_historical_forecast_hourly") as mock_forecast, \
             patch("historical_weather.get_historical_observation_hourly") as mock_obs:
            mock_forecast.return_value = [
                HistoricalHourlyPoint(timestamp="2024-07-15T00:00", temperature_f=88.0,
                                        precipitation_mm=0.0, precipitation_probability_pct=20.0),
            ]
            mock_obs.return_value = [
                HistoricalHourlyPoint(timestamp="2024-07-15T00:00", temperature_f=87.5, precipitation_mm=0.1),
            ]
            result = hb.backfill_weather_for_station("KAUS", 1000, 2000)

        self.assertTrue(result)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT forecast_temp_f, forecast_precip_pop_pct, observed_temp_f, observed_precip_mm "
                "FROM historical_weather_points WHERE station_code='KAUS'"
            ).fetchone()
        self.assertEqual(row, (88.0, 20.0, 87.5, 0.1))

    def test_mismatched_timestamps_between_forecast_and_observation_do_not_crash(self):
        """The two API calls are independent -- one could have a data gap
        the other doesn't. Each timestamp should still get whatever data
        IS available for it, not be dropped entirely."""
        with patch("historical_weather.get_historical_forecast_hourly") as mock_forecast, \
             patch("historical_weather.get_historical_observation_hourly") as mock_obs:
            mock_forecast.return_value = [
                HistoricalHourlyPoint(timestamp="2024-07-15T00:00", temperature_f=88.0, precipitation_mm=0.0),
            ]
            mock_obs.return_value = [
                HistoricalHourlyPoint(timestamp="2024-07-15T01:00", temperature_f=90.0, precipitation_mm=0.0),
            ]
            hb.backfill_weather_for_station("KAUS", 1000, 2000)

        with storage.get_conn() as conn:
            rows = conn.execute("SELECT ts, forecast_temp_f, observed_temp_f FROM historical_weather_points "
                                  "WHERE station_code='KAUS' ORDER BY ts").fetchall()
        self.assertEqual(len(rows), 2, "both distinct timestamps should be saved, not merged away")


class TestBackfillOneMarket(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM historical_markets")
            conn.execute("DELETE FROM historical_price_points")
            conn.execute("DELETE FROM historical_weather_points")
            conn.commit()

    def _make_rules(self, station_code="KAUS", measure="temperature_high", low=85.0, high=95.0):
        rules = MagicMock()
        rules.station_code = station_code
        rules.measure = measure
        rules.threshold_low_f = low
        rules.threshold_high_f = high
        return rules

    def test_full_backfill_of_one_market_stores_everything(self):
        kalshi = MagicMock()
        kalshi.get_historical_market_rules_text.return_value = "Settles YES if high temp >= 90F"
        kalshi.get_historical_candlesticks.return_value = {
            "candlesticks": [
                {"end_period_ts": 1000, "price": {"close": "0.4000"}, "yes_ask": {}, "yes_bid": {}, "volume": "5.00"},
                {"end_period_ts": 2000, "price": {"close": "0.6000"}, "yes_ask": {}, "yes_bid": {}, "volume": "8.00"},
            ]
        }
        extractor = MagicMock()
        extractor.extract.return_value = self._make_rules()

        market_obj = {
            "ticker": "KXHIGHNY-24JUL15-T90", "event_ticker": "KXHIGHNY-24JUL15",
            "open_time": "2024-07-14T00:00:00Z", "close_time": "2024-07-15T23:00:00Z",
            "result": "yes",
        }

        with patch.object(hb, "backfill_weather_for_station") as mock_weather:
            hb.backfill_one_market(kalshi, extractor, market_obj, series_ticker="KXHIGHNY")

        market = storage.get_historical_market("KXHIGHNY-24JUL15-T90")
        self.assertEqual(market["station_code"], "KAUS")
        self.assertEqual(market["result"], "yes")
        self.assertEqual(market["series_ticker"], "KXHIGHNY")

        with storage.get_conn() as conn:
            price_rows = conn.execute(
                "SELECT ts, yes_price_cents, volume FROM historical_price_points "
                "WHERE ticker='KXHIGHNY-24JUL15-T90' ORDER BY ts"
            ).fetchall()
        self.assertEqual(price_rows, [(1000, 40, 5), (2000, 60, 8)])
        mock_weather.assert_called_once()

    def test_missing_close_time_skips_price_and_weather_but_still_saves_market(self):
        kalshi = MagicMock()
        kalshi.get_historical_market_rules_text.return_value = "text"
        extractor = MagicMock()
        extractor.extract.return_value = self._make_rules()

        market_obj = {"ticker": "T2", "open_time": "2024-07-14T00:00:00Z", "close_time": None, "result": "no"}

        with patch.object(hb, "backfill_weather_for_station") as mock_weather:
            hb.backfill_one_market(kalshi, extractor, market_obj, series_ticker="KXHIGHNY")

        market = storage.get_historical_market("T2")
        self.assertIsNotNone(market, "the market itself should still be saved even without a usable close_time")
        kalshi.get_historical_candlesticks.assert_not_called()
        mock_weather.assert_not_called()

    def test_no_station_code_skips_weather_backfill(self):
        kalshi = MagicMock()
        kalshi.get_historical_market_rules_text.return_value = "text"
        kalshi.get_historical_candlesticks.return_value = {"candlesticks": []}
        extractor = MagicMock()
        extractor.extract.return_value = self._make_rules(station_code=None)

        market_obj = {"ticker": "T3", "open_time": "2024-07-14T00:00:00Z",
                       "close_time": "2024-07-15T00:00:00Z", "result": "yes"}

        with patch.object(hb, "backfill_weather_for_station") as mock_weather:
            hb.backfill_one_market(kalshi, extractor, market_obj, series_ticker="KXHIGHNY")

        mock_weather.assert_not_called()


class TestBackfillSeries(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM historical_markets")
            conn.commit()

    def test_pages_through_multiple_pages(self):
        kalshi = MagicMock()
        kalshi.get_historical_markets.side_effect = [
            {"markets": [{"ticker": "T1", "open_time": None, "close_time": None, "result": "yes"}], "cursor": "page2"},
            {"markets": [{"ticker": "T2", "open_time": None, "close_time": None, "result": "no"}], "cursor": ""},
        ]
        extractor = MagicMock()
        extractor.extract.return_value = MagicMock(station_code=None, measure="temperature_high",
                                                       threshold_low_f=None, threshold_high_f=None)
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time") as mock_time:  # skip real sleeps in the test
            result = hb.backfill_series(kalshi, extractor, "KXHIGHNY")

        self.assertEqual(result, {"processed": 2, "failed": 0})
        self.assertEqual(kalshi.get_historical_markets.call_count, 2)

    def test_one_markets_failure_does_not_abort_the_series(self):
        kalshi = MagicMock()
        kalshi.get_historical_markets.return_value = {
            "markets": [
                {"ticker": "BAD", "open_time": None, "close_time": None, "result": "yes"},
                {"ticker": "GOOD", "open_time": None, "close_time": None, "result": "no"},
            ],
            "cursor": "",
        }
        extractor = MagicMock()

        def extract_side_effect(ticker, rules_text, force=False):
            if ticker == "BAD":
                raise Exception("simulated extraction failure")
            return MagicMock(station_code=None, measure="temperature_high",
                               threshold_low_f=None, threshold_high_f=None)
        extractor.extract.side_effect = extract_side_effect
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time"):
            result = hb.backfill_series(kalshi, extractor, "KXHIGHNY")

        self.assertEqual(result, {"processed": 1, "failed": 1})
        self.assertIsNotNone(storage.get_historical_market("GOOD"))

    def test_max_markets_stops_early(self):
        kalshi = MagicMock()
        kalshi.get_historical_markets.return_value = {
            "markets": [
                {"ticker": f"T{i}", "open_time": None, "close_time": None, "result": "yes"} for i in range(5)
            ],
            "cursor": "",
        }
        extractor = MagicMock()
        extractor.extract.return_value = MagicMock(station_code=None, measure="temperature_high",
                                                       threshold_low_f=None, threshold_high_f=None)
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time"):
            result = hb.backfill_series(kalshi, extractor, "KXHIGHNY", max_markets=2)

        self.assertEqual(result["processed"] + result["failed"], 2)

    def test_empty_first_page_returns_zero_without_crashing(self):
        kalshi = MagicMock()
        kalshi.get_historical_markets.return_value = {"markets": [], "cursor": ""}
        extractor = MagicMock()

        result = hb.backfill_series(kalshi, extractor, "KXHIGHNY")
        self.assertEqual(result, {"processed": 0, "failed": 0})


if __name__ == "__main__":
    unittest.main()
