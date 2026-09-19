"""Tests for the Layer 1 harness adapter.

The two that matter: it must not be able to see inside the window it is
predicting, and it must go through the PointInTimeView like any other
candidate rather than reaching around it.
"""
import datetime as dt
import os
import sqlite3
import tempfile
import unittest

import weatherman
import weatherman_forecaster
from evaluation.pit import PointInTimeView

DAY = 86400
HOUR = 3600
CLOSE = int(dt.datetime(2026, 3, 3, 5, tzinfo=dt.timezone.utc).timestamp())
LO = CLOSE - DAY


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class ForecasterTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE wx_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT,
                valid_at INTEGER, available_at INTEGER, source TEXT,
                temp_f REAL, precip_in REAL, is_trace INTEGER DEFAULT 0);
            CREATE TABLE wx_forecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT,
                valid_at INTEGER, lead_hours INTEGER, available_at INTEGER,
                source TEXT, precip_mm REAL, precip_prob_pct REAL, temp_c REAL);
            CREATE TABLE historical_markets (
                ticker TEXT PRIMARY KEY, series_ticker TEXT, event_ticker TEXT,
                station_code TEXT, measure TEXT, threshold_low_f REAL,
                threshold_high_f REAL, threshold_description TEXT,
                open_time TEXT, close_time TEXT, result TEXT,
                expiration_value REAL, backfilled_ts INTEGER);
        """)
        # Observations for the three days leading up to the window, plus
        # -- deliberately -- a soaking inside the window itself.
        obs = [("NYC", LO - i * HOUR, LO - i * HOUR + 1500, "iem", 50.0, 0.0, 0)
               for i in range(1, 73)]
        obs += [("NYC", LO + i * HOUR, LO + i * HOUR + 1500, "iem", 50.0, 9.9, 0)
                for i in range(24)]
        conn.executemany(
            "INSERT INTO wx_observations (station, valid_at, available_at, "
            "source, temp_f, precip_in, is_trace) VALUES (?,?,?,?,?,?,?)", obs)
        fcs = [("NYC", LO + i * HOUR, 24, LO + i * HOUR - 24 * HOUR, "om",
                0.5 if i in (3, 4) else 0.0, 60.0 if i in (3, 4) else 5.0, 8.0)
               for i in range(24)]
        conn.executemany(
            "INSERT INTO wx_forecasts (station, valid_at, lead_hours, "
            "available_at, source, precip_mm, precip_prob_pct, temp_c) "
            "VALUES (?,?,?,?,?,?,?,?)", fcs)
        conn.execute(
            "INSERT INTO historical_markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("KXRAINNYC-X", "KXRAINNYC", "EV", "CLINYC", "precipitation_daily",
             None, 0.0, "strictly greater than 0 inches of precipitation",
             _iso(LO - 2 * DAY), _iso(CLOSE), "yes", 0.5, 0))
        conn.commit()
        conn.close()

        self.model = weatherman.LogisticModel(
            weights=[0.0] * len(weatherman.FEATURE_NAMES), bias=0.0,
            mean=[0.0] * len(weatherman.FEATURE_NAMES),
            std=[1.0] * len(weatherman.FEATURE_NAMES))

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def view(self, as_of):
        return PointInTimeView(
            as_of, sources=["wx_observations", "wx_forecasts"], db_path=self.db)

    def terms(self):
        v = PointInTimeView(10 ** 10, sources=["wx_observations"], db_path=self.db)
        return v.market("KXRAINNYC-X")

    def forecaster(self, station_of=lambda c: "NYC"):
        return weatherman_forecaster.WeathermanForecaster(
            self.model, lead_hours=24, station_of=station_of)


class TestItCannotSeeInsideTheWindow(ForecasterTestCase):
    def test_declines_when_asked_after_the_window_opens(self):
        # The fixture soaks the window with 9.9in an hour. A forecaster
        # that answered here would be reporting the outcome.
        f = self.forecaster()
        self.assertIsNone(f.probability(self.view(LO + 6 * HOUR),
                                        self.terms(), LO + 6 * HOUR))

    def test_answers_at_the_window_boundary(self):
        f = self.forecaster()
        self.assertIsNotNone(f.probability(self.view(LO), self.terms(), LO))

    def test_prior_rain_feature_excludes_the_window(self):
        # Build features directly at the decision moment and confirm the
        # 9.9in inside the window never reaches them.
        view = self.view(LO)
        cache = weatherman.ArchiveCache()
        for row in view.observations("NYC"):
            cache.obs[row["valid_at"]] = row["precip_in"]
        self.assertTrue(cache.obs)
        self.assertEqual(max(cache.obs.values()), 0.0,
                         "an in-window observation was visible at the "
                         "decision moment")


class TestItGoesThroughTheView(ForecasterTestCase):
    def test_uses_only_forecasts_already_issued(self):
        view = self.view(LO)
        rows = view.forecasts_at_lead("NYC", 24)
        self.assertTrue(rows)
        for r in rows:
            self.assertLessEqual(r["available_at"], LO)

    def test_an_undeclared_source_is_refused(self):
        v = PointInTimeView(LO, sources=["wx_forecasts"], db_path=self.db)
        from evaluation import pit
        with self.assertRaises(pit.SourceNotDeclaredError):
            v.observations("NYC")

    def test_returns_none_for_an_unmappable_station(self):
        f = self.forecaster(station_of=lambda c: None)
        self.assertIsNone(f.probability(self.view(LO), self.terms(), LO))

    def test_returns_a_probability_in_range(self):
        p = self.forecaster().probability(self.view(LO), self.terms(), LO)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)

    def test_declines_without_forecast_data(self):
        f = self.forecaster(station_of=lambda c: "NOWHERE")
        self.assertIsNone(f.probability(self.view(LO), self.terms(), LO))

    def test_has_a_stable_kind_for_baseline_grouping(self):
        self.assertEqual(self.forecaster().kind, "weatherman")
        self.assertIn("lead=24h", self.forecaster().name)


if __name__ == "__main__":
    unittest.main()
