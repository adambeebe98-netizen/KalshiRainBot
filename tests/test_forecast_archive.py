"""Tests for forecast_archive.py.

The property that matters most: a forecast row's availability must be its
valid hour minus its lead time, exactly. If that arithmetic is wrong in
the optimistic direction, the harness hands candidates a forecast they
could not have had, and every result after that is worthless.
"""
import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import forecast_archive

HOUR = 3600
# 2026-03-01T00:00Z
T0 = int(dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc).timestamp())


def _payload(n_hours=4):
    times = [dt.datetime.fromtimestamp(T0 + i * HOUR, dt.timezone.utc)
             .strftime("%Y-%m-%dT%H:%M") for i in range(n_hours)]
    return {"hourly": {
        "time": times,
        "precipitation_previous_day1": [0.0, 0.4, 1.5, None],
        "precipitation_probability_previous_day1": [5, 30, 80, None],
        "temperature_2m_previous_day1": [4.0, 4.5, 5.0, None],
        "precipitation_previous_day2": [0.0, 1.5, 0.2, 0.0],
        "precipitation_probability_previous_day2": [10, 40, 60, 20],
        "temperature_2m_previous_day2": [3.0, 3.5, 4.0, 4.2],
    }}


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class ForecastArchiveTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        forecast_archive.init(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def fetch(self, payload=None, leads=(24, 48)):
        with mock.patch.object(forecast_archive, "_get",
                               return_value=FakeResponse(payload or _payload())):
            return forecast_archive.fetch_forecasts(
                "NYC", 40.78, -73.97, dt.date(2026, 3, 1), dt.date(2026, 3, 1),
                leads_h=leads)


class TestAvailabilityArithmetic(ForecastArchiveTestCase):
    def test_availability_is_valid_hour_minus_lead(self):
        # THE test. Getting this wrong optimistically hands a candidate a
        # forecast it could not have had.
        for row in self.fetch():
            self.assertEqual(row["available_at"],
                             row["valid_at"] - row["lead_hours"] * HOUR)

    def test_a_longer_lead_is_available_earlier(self):
        rows = self.fetch()
        by_lead = {}
        for r in rows:
            by_lead.setdefault(r["lead_hours"], {})[r["valid_at"]] = r
        common = set(by_lead[24]) & set(by_lead[48])
        self.assertTrue(common)
        for valid in common:
            self.assertLess(by_lead[48][valid]["available_at"],
                            by_lead[24][valid]["available_at"])

    def test_availability_always_precedes_the_hour_forecast(self):
        for row in self.fetch():
            self.assertLess(row["available_at"], row["valid_at"])


class TestParsing(ForecastArchiveTestCase):
    def test_one_row_per_hour_per_lead(self):
        rows = self.fetch()
        # 3 usable hours at 24h lead (the fourth is all-None), 4 at 48h.
        self.assertEqual(sum(1 for r in rows if r["lead_hours"] == 24), 3)
        self.assertEqual(sum(1 for r in rows if r["lead_hours"] == 48), 4)

    def test_values_land_on_the_right_lead(self):
        rows = {(r["lead_hours"], r["valid_at"]): r for r in self.fetch()}
        self.assertEqual(rows[(24, T0 + 2 * HOUR)]["precip_mm"], 1.5)
        self.assertEqual(rows[(48, T0 + 2 * HOUR)]["precip_mm"], 0.2)
        self.assertEqual(rows[(24, T0 + 2 * HOUR)]["precip_prob_pct"], 80)

    def test_all_null_hours_are_dropped(self):
        rows = [r for r in self.fetch() if r["lead_hours"] == 24]
        self.assertNotIn(T0 + 3 * HOUR, [r["valid_at"] for r in rows])

    def test_requests_the_variables_for_each_lead(self):
        with mock.patch.object(forecast_archive, "_get",
                               return_value=FakeResponse(_payload())) as g:
            forecast_archive.fetch_forecasts(
                "NYC", 40.78, -73.97, dt.date(2026, 3, 1), dt.date(2026, 3, 1),
                leads_h=(24, 72))
        hourly = g.call_args[0][1]["hourly"]
        self.assertIn("precipitation_previous_day1", hourly)
        self.assertIn("precipitation_previous_day3", hourly)
        self.assertNotIn("precipitation_previous_day2", hourly)

    def test_asks_for_utc(self):
        with mock.patch.object(forecast_archive, "_get",
                               return_value=FakeResponse(_payload())) as g:
            forecast_archive.fetch_forecasts(
                "NYC", 40.78, -73.97, dt.date(2026, 3, 1), dt.date(2026, 3, 1))
        self.assertEqual(g.call_args[0][1]["timezone"], "UTC")

    def test_never_requests_the_historical_forecast_api(self):
        # The endpoint that would leak the answer. Named here so that
        # switching to it is a test failure rather than a quiet upgrade.
        self.assertIn("previous-runs", forecast_archive.PREVIOUS_RUNS)
        self.assertNotIn("historical-forecast", forecast_archive.PREVIOUS_RUNS)


class TestStorage(ForecastArchiveTestCase):
    def test_stores_and_counts(self):
        self.assertEqual(
            forecast_archive.store_forecasts(self.fetch(), db_path=self.db), 7)

    def test_same_hour_at_different_leads_are_separate_rows(self):
        forecast_archive.store_forecasts(self.fetch(), db_path=self.db)
        conn = sqlite3.connect(self.db)
        n = conn.execute(
            "SELECT COUNT(*) FROM wx_forecasts WHERE valid_at = ?",
            (T0 + HOUR,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 2, "24h and 48h forecasts of one hour are "
                               "different facts and must both survive")

    def test_reinserting_adds_nothing(self):
        forecast_archive.store_forecasts(self.fetch(), db_path=self.db)
        self.assertEqual(
            forecast_archive.store_forecasts(self.fetch(), db_path=self.db), 0)

    def test_empty_is_a_noop(self):
        self.assertEqual(forecast_archive.store_forecasts([], db_path=self.db), 0)

    def test_coverage_splits_by_lead(self):
        forecast_archive.store_forecasts(self.fetch(), db_path=self.db)
        cov = forecast_archive.coverage(self.db)
        self.assertEqual({r["lead_hours"] for r in cov}, {24, 48})


class TestStationCoordinates(ForecastArchiveTestCase):
    CSV = ("station,valid,lon,lat,tmpf\n"
           "NYC,2026-09-19 00:51,-73.9692,40.7794,60.0\n")

    def test_reads_coordinates_from_iem(self):
        with mock.patch.object(forecast_archive, "_get",
                               return_value=FakeResponse(None, 200, self.CSV)):
            self.assertEqual(forecast_archive.fetch_station_coordinates("CLINYC"),
                             (40.7794, -73.9692))

    def test_caches_after_the_first_fetch(self):
        with mock.patch.object(forecast_archive, "_get",
                               return_value=FakeResponse(None, 200, self.CSV)) as g:
            a = forecast_archive.station_coordinates("NYC", db_path=self.db)
            b = forecast_archive.station_coordinates("NYC", db_path=self.db)
        self.assertEqual(a, b)
        self.assertEqual(g.call_count, 1, "coordinates should be fetched once")

    def test_missing_coordinates_return_none_not_a_guess(self):
        with mock.patch.object(forecast_archive, "_get",
                               return_value=FakeResponse(None, 200,
                                                          "station,valid\n")):
            self.assertIsNone(forecast_archive.fetch_station_coordinates("NYC"))

    def test_backfill_refuses_without_coordinates(self):
        with mock.patch.object(forecast_archive, "station_coordinates",
                               return_value=None):
            out = forecast_archive.backfill_station(
                "NYC", dt.date(2026, 3, 1), dt.date(2026, 3, 2), db_path=self.db)
        self.assertEqual(out["inserted"], 0)
        self.assertIn("error", out)


class TestRateLimitHandling(ForecastArchiveTestCase):
    def test_retries_on_429(self):
        responses = [FakeResponse(None, 429), FakeResponse(_payload(), 200)]
        with mock.patch.object(forecast_archive.httpx, "get",
                               side_effect=responses) as g, \
                mock.patch.object(forecast_archive.time, "sleep"):
            resp = forecast_archive._get("http://x", {})
        self.assertEqual(g.call_count, 2)
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
