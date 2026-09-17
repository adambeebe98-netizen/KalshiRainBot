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

    def test_translates_kalshi_raw_station_code_before_the_station_reference_lookup(self):
        """CONFIRMED REAL BUG, caught on a live backfill run: every single
        station failed with "no coordinates known" because this function
        was looking up STATION_REFERENCE directly by the raw "CLI" code
        (e.g. "CLIAUS"), which is never a key in that table --
        STATION_REFERENCE is keyed by the real ICAO code ("KAUS"). This
        test reproduces the exact confirmed-failing stations from that
        run directly, with NOTHING mocked between the raw code and the
        real kalshi_station_to_nws_id + STATION_REFERENCE lookup, since
        the original bug slipped through specifically because other
        tests mock backfill_weather_for_station itself and never
        exercise this real interaction."""
        for raw_code in ["CLISFO", "CLISEA", "CLIAUS", "CLIPHX", "CLIDEN"]:
            with self.subTest(raw_code=raw_code):
                with patch("historical_weather.get_historical_forecast_hourly") as mock_forecast, \
                     patch("historical_weather.get_historical_observation_hourly") as mock_obs:
                    mock_forecast.return_value = [
                        HistoricalHourlyPoint(timestamp="2024-07-15T00:00", temperature_f=85.0,
                                                precipitation_mm=0.0, precipitation_probability_pct=10.0),
                    ]
                    mock_obs.return_value = [
                        HistoricalHourlyPoint(timestamp="2024-07-15T00:00", temperature_f=84.0, precipitation_mm=0.0),
                    ]
                    result = hb.backfill_weather_for_station(raw_code, 1000, 2000)
                self.assertTrue(result, f"{raw_code} should resolve via translation, matching the live confirmed fix")

    def test_weather_points_are_stored_under_the_raw_code_not_the_translated_one(self):
        """Deliberate design choice, not an oversight: historical_markets
        stores the RAW code (matching what the live bot itself stores in
        market_snapshots/trades), so historical_weather_points must use
        the same raw form as its key, or a future join between the two
        tables would never match."""
        with patch("historical_weather.get_historical_forecast_hourly") as mock_forecast, \
             patch("historical_weather.get_historical_observation_hourly") as mock_obs:
            mock_forecast.return_value = [
                HistoricalHourlyPoint(timestamp="2024-07-15T00:00", temperature_f=85.0, precipitation_mm=0.0),
            ]
            mock_obs.return_value = [
                HistoricalHourlyPoint(timestamp="2024-07-15T00:00", temperature_f=84.0, precipitation_mm=0.0),
            ]
            hb.backfill_weather_for_station("CLIAUS", 1000, 2000)

        with storage.get_conn() as conn:
            raw_row = conn.execute("SELECT COUNT(*) FROM historical_weather_points WHERE station_code='CLIAUS'").fetchone()
            translated_row = conn.execute("SELECT COUNT(*) FROM historical_weather_points WHERE station_code='KAUS'").fetchone()
        self.assertEqual(raw_row[0], 1)
        self.assertEqual(translated_row[0], 0)


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
        rules.settlement_source = "NWS"
        rules.threshold_description = "x"
        rules.confidence = "high"
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
            hb.backfill_one_market(kalshi, extractor, market_obj, series_ticker="KXHIGHTEST")

        market = storage.get_historical_market("KXHIGHNY-24JUL15-T90")
        self.assertEqual(market["station_code"], "KAUS")
        self.assertEqual(market["result"], "yes")
        self.assertEqual(market["series_ticker"], "KXHIGHTEST")

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
            hb.backfill_one_market(kalshi, extractor, market_obj, series_ticker="KXHIGHTEST")

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
            hb.backfill_one_market(kalshi, extractor, market_obj, series_ticker="KXHIGHTEST")

        mock_weather.assert_not_called()


class TestSkipAlreadyStoredMarket(unittest.TestCase):
    """CONFIRMED LIVE: restarting the backfill script for a code fix
    re-discovers all series fresh every time and has no memory of which
    series already finished in an earlier run -- observed directly:
    Austin's ~4,000 markets started reprocessing from scratch after a
    routine restart, wasting real Kalshi/LLM API calls redoing
    identical work. This is what makes every future restart pick back
    up near where it left off instead."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM historical_markets")
            conn.commit()

    def test_second_call_on_the_same_ticker_skips_expensive_work(self):
        from rules_extractor import MarketRules

        kalshi = MagicMock()
        kalshi.get_historical_market_rules_text.return_value = "some rules text"
        kalshi.get_historical_candlesticks.return_value = {"candlesticks": []}
        extractor = MagicMock()
        extractor.extract.return_value = MarketRules(
            ticker="T1", station_code="CLIAUS", settlement_source="NWS", measure="temperature_high",
            threshold_description="x", trace_counts_as_zero=None, fallback_rule=None,
            confidence="high", threshold_low_f=90.0001, threshold_high_f=None,
        )
        market_obj = {"ticker": "T1", "open_time": "2026-01-01T00:00:00Z", "close_time": "2026-01-02T00:00:00Z",
                       "occurrence_datetime": "2026-01-01T14:00:00Z", "result": "yes"}

        with patch.object(hb, "backfill_weather_for_station"):
            hb.backfill_one_market(kalshi, extractor, market_obj, "KXHIGHAUS")

        self.assertEqual(kalshi.get_historical_market_rules_text.call_count, 1)
        self.assertEqual(kalshi.get_historical_candlesticks.call_count, 1)

        with patch.object(hb, "backfill_weather_for_station") as mock_weather2:
            hb.backfill_one_market(kalshi, extractor, market_obj, "KXHIGHAUS")

        self.assertEqual(kalshi.get_historical_market_rules_text.call_count, 1,
                          "should not re-fetch rules text for an already-stored ticker")
        self.assertEqual(kalshi.get_historical_candlesticks.call_count, 1,
                          "should not re-fetch candlesticks for an already-stored ticker")
        self.assertEqual(mock_weather2.call_count, 1,
                          "should still defensively re-check weather coverage even on a skip")


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
                                                       threshold_low_f=None, threshold_high_f=None,
                                                       settlement_source="NWS", threshold_description="x", confidence="high")
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time") as mock_time:  # skip real sleeps in the test
            result = hb.backfill_series(kalshi, extractor, "KXHIGHNY")

        self.assertEqual(result, {"processed": 2, "failed": 0, "skipped_too_old": 0})
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
                               threshold_low_f=None, threshold_high_f=None,
                                                       settlement_source="NWS", threshold_description="x", confidence="high")
        extractor.extract.side_effect = extract_side_effect
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time"):
            result = hb.backfill_series(kalshi, extractor, "KXHIGHNY")

        self.assertEqual(result, {"processed": 1, "failed": 1, "skipped_too_old": 0})
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
                                                       threshold_low_f=None, threshold_high_f=None,
                                                       settlement_source="NWS", threshold_description="x", confidence="high")
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time"):
            result = hb.backfill_series(kalshi, extractor, "KXHIGHNY", max_markets=2)

        self.assertEqual(result["processed"] + result["failed"], 2)

    def test_empty_first_page_returns_zero_without_crashing(self):
        kalshi = MagicMock()
        kalshi.get_historical_markets.return_value = {"markets": [], "cursor": ""}
        extractor = MagicMock()

        result = hb.backfill_series(kalshi, extractor, "KXHIGHNY")
        self.assertEqual(result, {"processed": 0, "failed": 0, "skipped_too_old": 0})


class TestApplyStationOverride(unittest.TestCase):
    """CONFIRMED LIVE via a real backfill run: rules_extractor's LLM
    hallucinated two different wrong station codes for Austin markets
    within the same series (CLIAIS, then CLIAYC — neither is CLIAUS),
    and separately produced CLIPHO for Phoenix (should be CLIPHX). These
    are the exact real failures this override was built to fix."""

    def test_corrects_the_confirmed_austin_failures(self):
        self.assertEqual(hb.apply_station_override("KXHIGHAUS", "CLIAIS"), "CLIAUS")
        self.assertEqual(hb.apply_station_override("KXHIGHAUS", "CLIAYC"), "CLIAUS")

    def test_corrects_the_confirmed_phoenix_failure(self):
        self.assertEqual(hb.apply_station_override("KXHIGHTPHX", "CLIPHO"), "CLIPHX")

    def test_matching_extraction_is_left_unchanged(self):
        self.assertEqual(hb.apply_station_override("KXHIGHCHI", "CLIMDW"), "CLIMDW")

    def test_series_not_in_the_table_passes_through_unchanged(self):
        self.assertEqual(hb.apply_station_override("KXHIGHTVABB", "CLIVABB"), "CLIVABB")
        self.assertIsNone(hb.apply_station_override("KXHIGHTVABB", None))

    def test_rotating_city_series_are_excluded_from_the_table(self):
        """A series-level override would be actively wrong for these --
        the station genuinely differs market to market within one series."""
        self.assertNotIn("KXRAIN", hb.CONFIRMED_SERIES_STATION_OVERRIDES)
        self.assertNotIn("KXRAINWKND", hb.CONFIRMED_SERIES_STATION_OVERRIDES)


class TestBackfillSeriesMinOpenTimeCutoff(unittest.TestCase):
    """CONFIRMED LIVE: Kalshi's own weather-market history for at least
    one series extends nearly 2 years back -- far more than needed for
    calibration and likely spanning market regimes with very different
    liquidity than today's. min_open_time bounds this."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM historical_markets")
            conn.commit()

    def _make_extractor(self):
        extractor = MagicMock()
        extractor.extract.return_value = MagicMock(station_code=None, measure="temperature_high",
                                                       threshold_low_f=None, threshold_high_f=None,
                                                       settlement_source="NWS", threshold_description="x", confidence="high")
        return extractor

    def test_markets_older_than_cutoff_are_skipped_not_stored(self):
        kalshi = MagicMock()
        kalshi.get_historical_markets.return_value = {
            "markets": [
                {"ticker": "NEW1", "open_time": "2026-06-01T00:00:00Z", "close_time": None, "result": "yes"},
                {"ticker": "OLD1", "open_time": "2024-01-01T00:00:00Z", "close_time": None, "result": "yes"},
            ],
            "cursor": "",
        }
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time"):
            result = hb.backfill_series(kalshi, self._make_extractor(), "T",
                                          min_open_time="2025-03-17T00:00:00Z")

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["skipped_too_old"], 1)
        self.assertIsNotNone(storage.get_historical_market("NEW1"))
        self.assertIsNone(storage.get_historical_market("OLD1"), "an old market should never be stored at all")

    def test_pagination_stops_once_an_entire_page_is_past_cutoff(self):
        kalshi = MagicMock()
        kalshi.get_historical_markets.side_effect = [
            {"markets": [{"ticker": "A", "open_time": "2026-01-01T00:00:00Z", "close_time": None, "result": "yes"}],
             "cursor": "page2"},
            {"markets": [{"ticker": "B", "open_time": "2020-01-01T00:00:00Z", "close_time": None, "result": "yes"}],
             "cursor": "page3"},
        ]
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time"):
            hb.backfill_series(kalshi, self._make_extractor(), "T", min_open_time="2025-03-17T00:00:00Z")

        self.assertEqual(kalshi.get_historical_markets.call_count, 2,
                          "should stop after the first entirely-old page, never fetching a third")

    def test_locally_jumbled_page_does_not_trigger_early_stop(self):
        """Real pagination has been observed to NOT be in strict date
        order within a page (an Apr-08 market appearing between Apr-27
        and Apr-26 entries in a real run) -- a single old market must
        not stop the whole series if the same page also has an in-range
        market."""
        kalshi = MagicMock()
        kalshi.get_historical_markets.return_value = {
            "markets": [
                {"ticker": "MIXED_OLD", "open_time": "2020-01-01T00:00:00Z", "close_time": None, "result": "yes"},
                {"ticker": "MIXED_NEW", "open_time": "2026-01-01T00:00:00Z", "close_time": None, "result": "yes"},
            ],
            "cursor": "",
        }
        kalshi.get_historical_market_rules_text.return_value = ""

        with patch.object(hb, "time"):
            result = hb.backfill_series(kalshi, self._make_extractor(), "T", min_open_time="2025-03-17T00:00:00Z")

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["skipped_too_old"], 1)

    def test_no_min_open_time_is_backward_compatible(self):
        kalshi = MagicMock()
        kalshi.get_historical_markets.return_value = {
            "markets": [{"ticker": "T", "open_time": "2020-01-01T00:00:00Z", "close_time": None, "result": "yes"}],
            "cursor": "",
        }
        kalshi.get_historical_market_rules_text.return_value = ""
        with patch.object(hb, "time"):
            result = hb.backfill_series(kalshi, self._make_extractor(), "T")
        self.assertEqual(result, {"processed": 1, "failed": 0, "skipped_too_old": 0})


class TestSeriesTemplateIntegration(unittest.TestCase):
    """Adam's insight, confirmed correct and confirmed valuable: within
    one series, every market's rules text is the same template with
    only the date and threshold substituted. Confirmed live tonight
    that calling the LLM separately for every market was wasteful past
    the first one (Austin alone: 3,700+ distinct tickers)."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM historical_markets")
            conn.commit()

    def test_only_one_real_llm_call_across_three_markets_in_a_series(self):
        from rules_extractor import MarketRules

        kalshi = MagicMock()
        kalshi.get_historical_candlesticks.return_value = {"candlesticks": []}
        texts = {
            "M1": ("If the highest temperature recorded in Central Park, New York for July 16, 2026 "
                   "as reported by the National Weather Service's Climatological Report (Daily), "
                   "is greater than 96°, then the market resolves to Yes."),
            "M2": ("If the highest temperature recorded in Central Park, New York for July 17, 2026 "
                   "as reported by the National Weather Service's Climatological Report (Daily), "
                   "is greater than 89°, then the market resolves to Yes."),
            "M3": ("If the highest temperature recorded in Central Park, New York for July 18, 2026 "
                   "as reported by the National Weather Service's Climatological Report (Daily), "
                   "is greater than 92°, then the market resolves to Yes."),
        }
        kalshi.get_historical_market_rules_text.side_effect = lambda t: texts[t]

        extractor = MagicMock()
        extractor.extract.return_value = MarketRules(
            ticker="M1", station_code="CLINYC", settlement_source="NWS", measure="temperature_high",
            threshold_description="x", trace_counts_as_zero=None, fallback_rule=None,
            confidence="high", threshold_low_f=96.0001, threshold_high_f=None,
        )

        markets = [
            {"ticker": "M1", "open_time": "2026-07-15T14:00:00Z", "close_time": "2026-07-17T04:59:00Z",
             "occurrence_datetime": "2026-07-16T14:00:00Z", "result": "no"},
            {"ticker": "M2", "open_time": "2026-07-16T14:00:00Z", "close_time": "2026-07-18T04:59:00Z",
             "occurrence_datetime": "2026-07-17T14:00:00Z", "result": "yes"},
            {"ticker": "M3", "open_time": "2026-07-17T14:00:00Z", "close_time": "2026-07-19T04:59:00Z",
             "occurrence_datetime": "2026-07-18T14:00:00Z", "result": "no"},
        ]

        templates = None
        with patch.object(hb, "backfill_weather_for_station"):
            for m in markets:
                templates = hb.backfill_one_market(kalshi, extractor, m, "KXHIGHNY", templates=templates)

        self.assertEqual(extractor.extract.call_count, 1, "should only call the LLM once, for the first market")

        for ticker, expected in [("M1", 96.0001), ("M2", 89.0001), ("M3", 92.0001)]:
            stored = storage.get_historical_market(ticker)
            self.assertIsNotNone(stored)
            self.assertAlmostEqual(stored["threshold_low_f"], expected, places=3)
            self.assertEqual(stored["station_code"], "CLINYC")

    def test_template_is_learned_and_threaded_through_backfill_series(self):
        """The template must actually flow through backfill_series'
        own loop, not just work when called directly."""
        from rules_extractor import MarketRules

        kalshi = MagicMock()
        kalshi.get_historical_candlesticks.return_value = {"candlesticks": []}
        texts = {
            "M1": "If the high temp for Sep 17, 2026 is greater than 90, resolves Yes.",
            "M2": "If the high temp for Sep 18, 2026 is greater than 85, resolves Yes.",
        }
        kalshi.get_historical_market_rules_text.side_effect = lambda t: texts[t]
        kalshi.get_historical_markets.return_value = {
            "markets": [
                {"ticker": "M1", "open_time": "2026-09-16T00:00:00Z", "close_time": "2026-09-18T00:00:00Z",
                 "occurrence_datetime": "2026-09-17T00:00:00Z", "result": "yes"},
                {"ticker": "M2", "open_time": "2026-09-17T00:00:00Z", "close_time": "2026-09-19T00:00:00Z",
                 "occurrence_datetime": "2026-09-18T00:00:00Z", "result": "no"},
            ],
            "cursor": "",
        }

        extractor = MagicMock()
        extractor.extract.return_value = MarketRules(
            ticker="M1", station_code="CLIAUS", settlement_source="NWS", measure="temperature_high",
            threshold_description="x", trace_counts_as_zero=None, fallback_rule=None,
            confidence="high", threshold_low_f=90.0001, threshold_high_f=None,
        )

        with patch.object(hb, "time"), patch.object(hb, "backfill_weather_for_station"):
            hb.backfill_series(kalshi, extractor, "KXHIGHAUS")

        self.assertEqual(extractor.extract.call_count, 1)
        stored_m2 = storage.get_historical_market("M2")
        self.assertAlmostEqual(stored_m2["threshold_low_f"], 85.0001, places=3)

    def test_interleaved_wordings_are_both_remembered_not_flip_flopped(self):
        """CONFIRMED LIVE: a single-remembered-template design caused
        Chicago's real throughput to drop to ~35 markets/minute versus
        Austin's ~200/minute -- directly confirmed via LLM cache growth
        that roughly a third of Chicago's markets still triggered a
        fresh LLM call, consistent with two genuinely different
        wordings (T-style vs B-style tickers) being interleaved and
        repeatedly overwriting a single remembered template. This
        reproduces that exact interleaved pattern directly."""
        from rules_extractor import MarketRules

        kalshi = MagicMock()
        kalshi.get_historical_candlesticks.return_value = {"candlesticks": []}
        texts = {
            "T1": "If the high temp for Sep 17, 2026 is greater than 90, resolves Yes.",
            "B1": "Settles YES if the high temp for Sep 17, 2026 falls between 88 and 90.",
            "T2": "If the high temp for Sep 16, 2026 is greater than 85, resolves Yes.",
            "B2": "Settles YES if the high temp for Sep 16, 2026 falls between 82 and 84.",
            "T3": "If the high temp for Sep 15, 2026 is greater than 80, resolves Yes.",
        }
        kalshi.get_historical_market_rules_text.side_effect = lambda t: texts[t]

        extractor = MagicMock()
        call_log = []

        def fake_extract(ticker, rules_text, force=False):
            call_log.append(ticker)
            if ticker == "T1":
                return MarketRules(ticker=ticker, station_code="CLIORD", settlement_source="NWS",
                                     measure="temperature_high", threshold_description="x",
                                     trace_counts_as_zero=None, fallback_rule=None, confidence="high",
                                     threshold_low_f=90.0001, threshold_high_f=None)
            elif ticker == "B1":
                return MarketRules(ticker=ticker, station_code="CLIORD", settlement_source="NWS",
                                     measure="temperature_high", threshold_description="x",
                                     trace_counts_as_zero=None, fallback_rule=None, confidence="high",
                                     threshold_low_f=88.0, threshold_high_f=90.0)
        extractor.extract.side_effect = fake_extract

        markets = [
            {"ticker": "T1", "open_time": "2026-09-16T00:00:00Z", "close_time": "2026-09-18T00:00:00Z",
             "occurrence_datetime": "2026-09-17T00:00:00Z", "result": "yes"},
            {"ticker": "B1", "open_time": "2026-09-16T00:00:00Z", "close_time": "2026-09-18T00:00:00Z",
             "occurrence_datetime": "2026-09-17T00:00:00Z", "result": "no"},
            {"ticker": "T2", "open_time": "2026-09-15T00:00:00Z", "close_time": "2026-09-17T00:00:00Z",
             "occurrence_datetime": "2026-09-16T00:00:00Z", "result": "no"},
            {"ticker": "B2", "open_time": "2026-09-15T00:00:00Z", "close_time": "2026-09-17T00:00:00Z",
             "occurrence_datetime": "2026-09-16T00:00:00Z", "result": "yes"},
            {"ticker": "T3", "open_time": "2026-09-14T00:00:00Z", "close_time": "2026-09-16T00:00:00Z",
             "occurrence_datetime": "2026-09-15T00:00:00Z", "result": "no"},
        ]

        templates = None
        with patch.object(hb, "backfill_weather_for_station"):
            for m in markets:
                templates = hb.backfill_one_market(kalshi, extractor, m, "KXHIGHCHI", templates=templates)

        self.assertEqual(call_log, ["T1", "B1"], "each wording should only need one real LLM call, ever")

        for ticker, expected in [("T2", 85.0001), ("T3", 80.0001)]:
            stored = storage.get_historical_market(ticker)
            self.assertAlmostEqual(stored["threshold_low_f"], expected, places=3)

        stored_b2 = storage.get_historical_market("B2")
        self.assertEqual(stored_b2["threshold_low_f"], 82.0)
        self.assertEqual(stored_b2["threshold_high_f"], 84.0)

    def test_ten_distinct_wordings_interleaved_thrash_free_with_raised_cap(self):
        """CONFIRMED LIVE: Chicago genuinely has 10 distinct wordings
        across its history (found by grouping real cached extractions
        by description shape) -- its 3 dominant shapes alone accounted
        for 863 separately re-cached tickers under the old design,
        meaning the same wordings were being relearned via fresh LLM
        calls over and over rather than reused. Root cause, confirmed
        directly: eviction used templates.pop(0) -- oldest-ADDED, not
        least-recently-USED -- so a frequently-needed template learned
        early could be evicted by a newer, rarer one and then need
        relearning the very next time it was needed. This reproduces
        all 10 real confirmed shapes interleaved and verifies each is
        only ever learned once."""
        from rules_extractor import MarketRules
        import re as re_module

        shapes = [
            ("high temperature strictly less than {}°F", "high"),
            ("strictly greater than {}°F", "low"),
            ("high temperature between {} and {} degrees Fahrenheit (inclusive)", "both"),
            ("strictly greater than {} degrees Fahrenheit", "low"),
            ("maximum temperature strictly less than {}°F", "high"),
            ("high temperature between {} and {}°F inclusive", "both"),
            ("high temperature between {} and {}°F (inclusive)", "both"),
            ("maximum temperature between {} and {} degrees Fahrenheit (inclusive)", "both"),
            ("daily high temperature strictly less than {}°F", "high"),
            ("high temperature between {}°F and {}°F (inclusive)", "both"),
        ]

        kalshi = MagicMock()
        kalshi.get_historical_candlesticks.return_value = {"candlesticks": []}

        texts, markets_list = {}, []
        for day in range(30):
            template_text, kind = shapes[day % 10]
            ticker = f"M{day}"
            val = 70 + day
            date_day = (day % 28) + 1
            if kind == "both":
                text = f"Settles YES if the {template_text.format(val, val + 2)} for Aug {date_day}, 2026."
            else:
                text = f"If the {template_text.format(val)} for Aug {date_day}, 2026, resolves Yes."
            texts[ticker] = text
            markets_list.append({
                "ticker": ticker, "open_time": f"2026-08-{date_day:02d}T00:00:00Z",
                "close_time": f"2026-08-{date_day:02d}T00:00:00Z",
                "occurrence_datetime": f"2026-08-{date_day:02d}T14:00:00Z", "result": "yes",
            })

        kalshi.get_historical_market_rules_text.side_effect = lambda t: texts[t]

        extractor = MagicMock()
        call_log = []
        num_re = re_module.compile(r"\d+(?:\.\d+)?")

        def fake_extract(ticker, rules_text, force=False):
            call_log.append(ticker)
            for template_text, kind in shapes:
                if template_text.split("{}")[0] in rules_text:
                    nums = [float(n) for n in num_re.findall(rules_text) if float(n) < 200]
                    if kind == "low":
                        return MarketRules(ticker=ticker, station_code="CLIMDW", settlement_source="NWS",
                                             measure="temperature_high", threshold_description="x",
                                             trace_counts_as_zero=None, fallback_rule=None, confidence="high",
                                             threshold_low_f=nums[0] + 0.0001, threshold_high_f=None)
                    elif kind == "high":
                        return MarketRules(ticker=ticker, station_code="CLIMDW", settlement_source="NWS",
                                             measure="temperature_high", threshold_description="x",
                                             trace_counts_as_zero=None, fallback_rule=None, confidence="high",
                                             threshold_low_f=None, threshold_high_f=nums[0] - 0.0001)
                    else:
                        return MarketRules(ticker=ticker, station_code="CLIMDW", settlement_source="NWS",
                                             measure="temperature_high", threshold_description="x",
                                             trace_counts_as_zero=None, fallback_rule=None, confidence="high",
                                             threshold_low_f=nums[0], threshold_high_f=nums[1])
            raise Exception(f"couldn't classify: {rules_text}")

        extractor.extract.side_effect = fake_extract

        templates = None
        with patch.object(hb, "backfill_weather_for_station"):
            for m in markets_list:
                templates = hb.backfill_one_market(kalshi, extractor, m, "KXHIGHCHI", templates=templates)

        self.assertEqual(len(call_log), 10, "each of the 10 distinct shapes should only ever need one real LLM call")
        self.assertEqual(len(templates), 10, "all 10 templates should fit comfortably under the raised cap")


class TestExpirationValueAndBidAskIntegration(unittest.TestCase):
    """expiration_value, bid/ask, and open_interest were already present
    in data fetched every time, previously discarded -- confirmed
    end-to-end through the real backfill_one_market path, not just at
    the storage layer in isolation."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM historical_markets")
            conn.execute("DELETE FROM historical_price_points")
            conn.commit()

    def test_all_three_flow_through_the_real_backfill_path(self):
        from rules_extractor import MarketRules

        kalshi = MagicMock()
        kalshi.get_historical_market_rules_text.return_value = "If the high temp for Sep 17, 2026 is greater than 95, resolves Yes."
        kalshi.get_historical_candlesticks.return_value = {
            "candlesticks": [
                {"end_period_ts": 1000, "price": {"close": "0.4500"}, "yes_bid": {"close": "0.4300"},
                 "yes_ask": {"close": "0.4600"}, "volume": "10.00", "open_interest": "250.00"},
            ]
        }
        extractor = MagicMock()
        extractor.extract.return_value = MarketRules(
            ticker="T1", station_code="CLIAUS", settlement_source="NWS", measure="temperature_high",
            threshold_description="strictly greater than 95F", trace_counts_as_zero=None, fallback_rule=None,
            confidence="high", threshold_low_f=95.0001, threshold_high_f=None,
        )
        market_obj = {"ticker": "T1", "open_time": "2026-09-16T00:00:00Z", "close_time": "2026-09-18T00:00:00Z",
                       "occurrence_datetime": "2026-09-17T00:00:00Z", "result": "yes", "expiration_value": "97.00"}

        with patch.object(hb, "backfill_weather_for_station"):
            hb.backfill_one_market(kalshi, extractor, market_obj, "KXHIGHAUS")

        market = storage.get_historical_market("T1")
        self.assertEqual(market["expiration_value"], 97.0)

        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT yes_price_cents, yes_bid_cents, yes_ask_cents, open_interest, volume "
                "FROM historical_price_points WHERE ticker='T1' AND ts=1000"
            ).fetchone()
        self.assertEqual(row, (45, 43, 46, 250, 10))


if __name__ == "__main__":
    unittest.main()
