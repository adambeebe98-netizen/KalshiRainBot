"""Tests for weather_archive.py.

Network-free: fetch_observations is exercised against a captured IEM
response rather than by calling IEM in a test run.
"""
import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import weather_archive

# A real IEM response shape, including a trace row (0.0001), a measurable
# row, a dry row and a missing row.
SAMPLE_CSV = """station,valid,tmpf,p01i
NYC,2026-09-01 00:51,71.00,0.0001
NYC,2026-09-01 01:51,71.00,0.01
NYC,2026-09-01 02:51,70.00,0.00
NYC,2026-09-01 03:51,,M
NYC,2026-09-01 04:51,70.00,0.02
"""


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        return None


class WeatherArchiveTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        weather_archive.init(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def fetch(self, csv_text=SAMPLE_CSV):
        with mock.patch.object(weather_archive.httpx, "get",
                               return_value=FakeResponse(csv_text)):
            return weather_archive.fetch_observations(
                "NYC", dt.date(2026, 9, 1), dt.date(2026, 9, 1))


class TestStationMapping(unittest.TestCase):
    def test_cli_codes_map_to_asos_ids(self):
        self.assertEqual(weather_archive.asos_id("CLINYC"), "NYC")
        self.assertEqual(weather_archive.asos_id("CLIAUS"), "AUS")

    def test_the_codes_that_do_not_reduce_by_stripping(self):
        # The reason this is a reviewed table rather than string surgery.
        # Stripping "CLI" from CLIHOB gives HOB, which is a real ASOS
        # station -- Lea County Regional, New Mexico -- and fetching it
        # for Houston markets produced 16,787 rows of plausible, wrong
        # weather that nothing downstream could have detected.
        self.assertEqual(weather_archive.asos_id("CLIHOB"), "HOU")
        self.assertEqual(weather_archive.asos_id("CLINOL"), "MSY")
        self.assertEqual(weather_archive.asos_id("CLIPHO"), "PHX")

    def test_icao_codes_reduce_too(self):
        self.assertEqual(weather_archive.asos_id("KMDW"), "MDW")
        self.assertEqual(weather_archive.asos_id("KLAX"), "LAX")

    def test_bare_ids_pass_through(self):
        self.assertEqual(weather_archive.asos_id("ORD"), "ORD")

    def test_handles_case_and_whitespace(self):
        self.assertEqual(weather_archive.asos_id("  clinyc "), "NYC")

    def test_an_unmapped_cli_code_raises_rather_than_guessing(self):
        with self.assertRaises(weather_archive.UnknownStationError):
            weather_archive.asos_id("CLIZZZ")
        with self.assertRaises(weather_archive.UnknownStationError):
            weather_archive.asos_id(None)

    def test_the_ambiguous_code_is_absent_on_purpose(self):
        # CLINEW could be Newark or New Orleans Lakefront. Both are real
        # airports, so a guess is undetectable if wrong.
        self.assertNotIn("CLINEW", weather_archive.STATION_MAP)
        with self.assertRaises(weather_archive.UnknownStationError):
            weather_archive.asos_id("CLINEW")

    def test_every_mapped_value_is_a_three_letter_id(self):
        for code, sid in weather_archive.STATION_MAP.items():
            self.assertRegex(sid, r"^[A-Z]{3}$", f"{code} -> {sid}")

    def test_austin_maps_to_the_airport_that_started_this(self):
        self.assertEqual(weather_archive.asos_id("CLIAUS"), "AUS")


class TestParsing(WeatherArchiveTestCase):
    def test_parses_every_usable_row(self):
        rows = self.fetch()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["station"], "NYC")

    def test_trace_is_detected_and_flagged(self):
        # THE property this module exists for: a trace report must arrive
        # as something distinguishable from both zero and from 0.01.
        rows = self.fetch()
        trace = rows[0]
        self.assertEqual(trace["precip_in"], weather_archive.TRACE_INCHES)
        self.assertEqual(trace["is_trace"], 1)
        self.assertGreater(trace["precip_in"], 0.0,
                           "trace must be strictly greater than zero, which "
                           "is exactly what the contract settles on")

    def test_measurable_precip_is_not_flagged_as_trace(self):
        rows = self.fetch()
        self.assertEqual(rows[1]["precip_in"], 0.01)
        self.assertEqual(rows[1]["is_trace"], 0)

    def test_dry_is_zero_not_trace(self):
        rows = self.fetch()
        self.assertEqual(rows[2]["precip_in"], 0.0)
        self.assertEqual(rows[2]["is_trace"], 0)

    def test_missing_becomes_none_not_zero(self):
        # 'M' meaning "not reported" must not be read as "no rain fell".
        rows = self.fetch()
        self.assertIsNone(rows[3]["precip_in"])
        self.assertIsNone(rows[3]["temp_f"])
        self.assertEqual(rows[3]["is_trace"], 0)

    def test_timestamps_are_utc(self):
        rows = self.fetch()
        self.assertEqual(
            rows[0]["valid_at"],
            int(dt.datetime(2026, 9, 1, 0, 51, tzinfo=dt.timezone.utc).timestamp()))

    def test_unparseable_rows_are_skipped_not_fatal(self):
        rows = self.fetch("station,valid,tmpf,p01i\nNYC,garbage,1,2\n"
                          "NYC,2026-09-01 00:51,71.00,0.01\n")
        self.assertEqual(len(rows), 1)

    def test_empty_response_is_empty(self):
        self.assertEqual(self.fetch("station,valid,tmpf,p01i\n"), [])

    def test_trace_parameter_is_sent_explicitly(self):
        with mock.patch.object(weather_archive.httpx, "get",
                               return_value=FakeResponse(SAMPLE_CSV)) as g:
            weather_archive.fetch_observations(
                "CLINYC", dt.date(2026, 9, 1), dt.date(2026, 9, 2))
        params = g.call_args.kwargs["params"]
        self.assertEqual(params["trace"], str(weather_archive.TRACE_INCHES))
        self.assertEqual(params["station"], "NYC")
        self.assertEqual(params["tz"], "Etc/UTC")


class TestRateLimitHandling(WeatherArchiveTestCase):
    def test_retries_on_429_then_succeeds(self):
        responses = [FakeResponse("", 429), FakeResponse("", 429),
                     FakeResponse(SAMPLE_CSV, 200)]
        with mock.patch.object(weather_archive.httpx, "get",
                               side_effect=responses) as g, \
                mock.patch.object(weather_archive.time, "sleep"):
            rows = weather_archive.fetch_observations(
                "NYC", dt.date(2026, 9, 1), dt.date(2026, 9, 1))
        self.assertEqual(g.call_count, 3)
        self.assertEqual(len(rows), 5)

    def test_backs_off_between_attempts(self):
        delays = []
        with mock.patch.object(weather_archive.httpx, "get",
                               return_value=FakeResponse("", 429)), \
                mock.patch.object(weather_archive.time, "sleep",
                                  side_effect=delays.append):
            weather_archive.fetch_observations(
                "NYC", dt.date(2026, 9, 1), dt.date(2026, 9, 1))
        self.assertGreater(len(delays), 1)
        self.assertEqual(delays, sorted(delays), "backoff must increase")


class TestStorage(WeatherArchiveTestCase):
    def test_stores_and_counts(self):
        self.assertEqual(
            weather_archive.store_observations(self.fetch(), db_path=self.db), 5)

    def test_availability_is_later_than_the_observation(self):
        weather_archive.store_observations(self.fetch(), db_path=self.db,
                                            availability_lag_s=1500)
        conn = sqlite3.connect(self.db)
        valid, available = conn.execute(
            "SELECT valid_at, available_at FROM wx_observations "
            "ORDER BY valid_at LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(available - valid, 1500)

    def test_reinserting_the_same_hours_adds_nothing(self):
        weather_archive.store_observations(self.fetch(), db_path=self.db)
        self.assertEqual(
            weather_archive.store_observations(self.fetch(), db_path=self.db), 0)

    def test_trace_flag_survives_the_round_trip(self):
        weather_archive.store_observations(self.fetch(), db_path=self.db)
        conn = sqlite3.connect(self.db)
        traces = conn.execute(
            "SELECT COUNT(*) FROM wx_observations WHERE is_trace = 1").fetchone()[0]
        wet = conn.execute(
            "SELECT COUNT(*) FROM wx_observations WHERE precip_in > 0").fetchone()[0]
        conn.close()
        self.assertEqual(traces, 1)
        # The contract's condition: trace counts as wet.
        self.assertEqual(wet, 3)

    def test_empty_input_is_a_noop(self):
        self.assertEqual(weather_archive.store_observations([], db_path=self.db), 0)

    def test_coverage_reports_per_station(self):
        weather_archive.store_observations(self.fetch(), db_path=self.db)
        cov = weather_archive.coverage(self.db)
        self.assertEqual(len(cov), 1)
        self.assertEqual(cov[0]["station"], "NYC")
        self.assertEqual(cov[0]["n"], 5)
        self.assertEqual(cov[0]["traces"], 1)
        self.assertEqual(cov[0]["wet_hours"], 3)
        self.assertEqual(cov[0]["with_precip"], 4)   # one row is missing


class TestBackfillChunking(WeatherArchiveTestCase):
    def test_walks_the_range_in_chunks(self):
        calls = []

        def fake_fetch(station, start, end, **kw):
            calls.append((start, end))
            return []

        with mock.patch.object(weather_archive, "fetch_observations", fake_fetch):
            weather_archive.backfill_station(
                "NYC", dt.date(2026, 1, 1), dt.date(2026, 6, 30),
                chunk_days=60, db_path=self.db, sleep_between=0)
        self.assertGreater(len(calls), 2)
        self.assertEqual(calls[0][0], dt.date(2026, 1, 1))
        self.assertEqual(calls[-1][1], dt.date(2026, 6, 30))

    def test_chunks_do_not_overlap_or_leave_gaps(self):
        calls = []

        def fake_fetch(station, start, end, **kw):
            calls.append((start, end))
            return []

        with mock.patch.object(weather_archive, "fetch_observations", fake_fetch):
            weather_archive.backfill_station(
                "NYC", dt.date(2026, 1, 1), dt.date(2026, 3, 31),
                chunk_days=30, db_path=self.db, sleep_between=0)
        for (_, prev_end), (next_start, _) in zip(calls, calls[1:]):
            self.assertEqual(next_start - prev_end, dt.timedelta(days=1),
                             "chunks must be contiguous")


if __name__ == "__main__":
    unittest.main()
