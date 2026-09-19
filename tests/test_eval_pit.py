"""Tests for evaluation/pit.py.

These build a throwaway database rather than touching bot_state.db, so the
assertions are about the filter's behaviour and not about whatever happens
to be in production today.
"""
import os
import sqlite3
import tempfile
import unittest

from evaluation import pit


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE historical_price_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, ts INTEGER,
            yes_price_cents INTEGER, volume INTEGER);
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, ticker TEXT,
            yes_bid INTEGER, forecast_temp_f REAL);
        CREATE TABLE forecast_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, ticker TEXT,
            forecast_temp_f REAL);
        CREATE TABLE realtime_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, channel TEXT,
            ts INTEGER, received_ts INTEGER, yes_price_cents INTEGER);
        CREATE TABLE realtime_weather_obs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, station_code TEXT, ts TEXT,
            received_ts INTEGER, temp_f REAL);
        CREATE TABLE historical_weather_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT, station_code TEXT, ts INTEGER,
            forecast_temp_f REAL);
        CREATE TABLE historical_markets (
            ticker TEXT PRIMARY KEY, series_ticker TEXT, event_ticker TEXT,
            station_code TEXT, measure TEXT, threshold_low_f REAL,
            threshold_high_f REAL, threshold_description TEXT,
            open_time TEXT, close_time TEXT, result TEXT,
            expiration_value REAL, backfilled_ts INTEGER);
    """)
    # Candles every 1000s from t=1000 to t=10000.
    conn.executemany(
        "INSERT INTO historical_price_points (ticker, ts, yes_price_cents, volume) "
        "VALUES (?,?,?,?)",
        [("MKT-A", t, t // 100, 5) for t in range(1000, 10001, 1000)])
    conn.executemany(
        "INSERT INTO market_snapshots (ts, ticker, yes_bid, forecast_temp_f) "
        "VALUES (?,?,?,?)",
        [(t, "MKT-A", 50, 70.0) for t in range(1000, 10001, 1000)])
    conn.executemany(
        "INSERT INTO forecast_history (ts, ticker, forecast_temp_f) VALUES (?,?,?)",
        [(t, "MKT-A", 72.0) for t in range(1000, 10001, 1000)])
    # A tick whose event time is early but which we received late -- the
    # case that separates MEASURED from DECLARED.
    conn.executemany(
        "INSERT INTO realtime_ticks (ticker, channel, ts, received_ts, yes_price_cents) "
        "VALUES (?,?,?,?,?)",
        [("MKT-A", "ticker", 1000, 1000, 40),
         ("MKT-A", "ticker", 2000, 9000, 41)])
    conn.executemany(
        "INSERT INTO realtime_weather_obs (station_code, ts, received_ts, temp_f) "
        "VALUES (?,?,?,?)",
        [("KNYC", "2026-01-01T00:00:00+00:00", 1000, 40.0),
         ("KNYC", "2026-01-01T01:00:00+00:00", 9000, 41.0)])
    conn.executemany(
        "INSERT INTO historical_weather_points (station_code, ts, forecast_temp_f) "
        "VALUES (?,?,?)",
        [("KNYC", t, 70.0) for t in range(1000, 5001, 1000)])
    conn.executemany(
        "INSERT INTO historical_markets (ticker, series_ticker, event_ticker, "
        "station_code, measure, threshold_low_f, threshold_high_f, "
        "threshold_description, open_time, close_time, result, "
        "expiration_value, backfilled_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # Opens 1970-01-01T00:16:40Z (t=1000), closes t=10000.
            ("MKT-A", "KXRAINNYC", "EV-A", "CLINYC", "precipitation_daily",
             None, 0.0, "strictly greater than 0 inches of precipitation",
             "1970-01-01T00:16:40Z", "1970-01-01T02:46:40Z", "yes", 0.0, 0),
            # Opens much later -- invisible to an early view.
            ("MKT-LATER", "KXRAINNYC", "EV-B", "CLINYC", "precipitation_daily",
             None, 0.0, "strictly greater than 0 inches of precipitation",
             "1970-01-01T02:46:40Z", "1970-01-01T05:00:00Z", "no", 0.0, 0),
            # Resolves 'scalar' -- must be excluded from labels explicitly.
            ("MKT-SCALAR", "KXOTHER", "EV-C", None, "other", None, None, None,
             "1970-01-01T00:16:40Z", "1970-01-01T02:46:40Z", "scalar", 1.0, 0),
        ])
    conn.commit()
    conn.close()


class PitTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        _make_db(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def view(self, as_of, **kw):
        kw.setdefault("db_path", self.db)
        return pit.PointInTimeView(as_of, **kw)


class TestTheCoreProperty(PitTestCase):
    def test_future_candles_are_not_returned(self):
        v = self.view(5000)
        pts = v.price_points("MKT-A")
        self.assertTrue(pts)
        self.assertTrue(all(p["ts"] <= 5000 for p in pts))
        self.assertEqual(max(p["ts"] for p in pts), 5000)

    def test_earlier_view_sees_strictly_less(self):
        self.assertLess(len(self.view(3000).price_points("MKT-A")),
                        len(self.view(8000).price_points("MKT-A")))

    def test_view_before_any_data_sees_nothing(self):
        self.assertEqual(self.view(500).price_points("MKT-A"), [])

    def test_boundary_is_inclusive(self):
        # A candle whose availability equals as_of exactly IS available.
        self.assertIn(3000, [p["ts"] for p in self.view(3000).price_points("MKT-A")])

    def test_candle_publish_lag_delays_availability(self):
        # With a 500s publish lag the t=3000 candle is not available until
        # t=3500, so a view at 3000 must not see it.
        v = self.view(3000, candle_publish_lag_s=500)
        self.assertNotIn(3000, [p["ts"] for p in v.price_points("MKT-A")])
        self.assertIn(2000, [p["ts"] for p in v.price_points("MKT-A")])

    def test_measured_availability_uses_receipt_not_event_time(self):
        # The second tick has event time 2000 but was received at 9000.
        # A view at 5000 must not see it, even though "it happened" by then.
        v = self.view(5000)
        tick_events = [t["ts"] for t in v.ticks("MKT-A")]
        self.assertEqual(tick_events, [1000])
        self.assertEqual([t["ts"] for t in self.view(9000).ticks("MKT-A")],
                         [1000, 2000])

    def test_measured_weather_observation_uses_receipt(self):
        self.assertEqual(len(self.view(5000).weather_obs("KNYC")), 1)
        self.assertEqual(len(self.view(9000).weather_obs("KNYC")), 2)

    def test_snapshots_and_forecasts_are_filtered_too(self):
        self.assertTrue(all(r["ts"] <= 4000
                            for r in self.view(4000).snapshots("MKT-A")))
        self.assertTrue(all(r["ts"] <= 4000
                            for r in self.view(4000).forecasts("MKT-A")))


class TestTheLabelIsUnreachable(PitTestCase):
    def test_view_has_no_label_accessor(self):
        v = self.view(5000)
        for name in ("label", "labels", "result", "outcome", "expiration_value"):
            self.assertFalse(hasattr(v, name),
                             f"PointInTimeView unexpectedly exposes {name!r}")

    def test_market_terms_carry_no_outcome(self):
        terms = self.view(5000).market("MKT-A")
        self.assertIsNotNone(terms)
        self.assertEqual(terms.ticker, "MKT-A")
        self.assertFalse(hasattr(terms, "result"))
        self.assertFalse(hasattr(terms, "expiration_value"))
        with self.assertRaises(AttributeError):
            _ = terms.result

    def test_terms_that_should_be_visible_are(self):
        terms = self.view(5000).market("MKT-A")
        self.assertEqual(terms.station_code, "CLINYC")
        self.assertEqual(terms.measure, "precipitation_daily")
        self.assertIn("greater than 0 inches", terms.threshold_description)

    def test_label_is_available_separately_for_the_harness(self):
        lab = pit.label_for("MKT-A", db_path=self.db)
        self.assertEqual(lab.result, "yes")
        self.assertEqual(lab.available_at, 10000)   # close_time

    def test_label_excludes_scalar_resolutions(self):
        self.assertIsNone(pit.label_for("MKT-SCALAR", db_path=self.db))

    def test_label_of_unknown_ticker_is_none(self):
        self.assertIsNone(pit.label_for("NOPE", db_path=self.db))


class TestMarketVisibility(PitTestCase):
    def test_market_not_yet_open_is_invisible(self):
        self.assertIsNone(self.view(5000).market("MKT-LATER"))

    def test_market_becomes_visible_once_open(self):
        self.assertIsNotNone(self.view(11000).market("MKT-LATER"))

    def test_open_markets_excludes_unopened_and_closed(self):
        tickers = [m.ticker for m in self.view(5000).open_markets()]
        self.assertIn("MKT-A", tickers)
        self.assertNotIn("MKT-LATER", tickers)
        # After MKT-A closes it is no longer open.
        self.assertNotIn("MKT-A", [m.ticker for m in self.view(11000).open_markets()])

    def test_open_markets_filters_by_measure(self):
        got = self.view(5000).open_markets(measure="precipitation_daily")
        self.assertEqual([m.ticker for m in got], ["MKT-A"])
        self.assertEqual(self.view(5000).open_markets(measure="nope"), [])


class TestUnknownAvailability(PitTestCase):
    def test_unknown_source_is_refused_by_default(self):
        v = self.view(5000, sources=["historical_weather_points"])
        with self.assertRaises(pit.UnavailableSourceError):
            v.weather_points("KNYC")

    def test_refusal_explains_itself(self):
        v = self.view(5000, sources=["historical_weather_points"])
        with self.assertRaises(pit.UnavailableSourceError) as ctx:
            v.weather_points("KNYC")
        self.assertIn("nowcast", str(ctx.exception))

    def test_allowed_with_a_reason_and_logged(self):
        v = self.view(5000, sources=["historical_weather_points"],
                      allow_unverified=True,
                      reason="baseline sanity check, results marked unverified")
        rows = v.weather_points("KNYC")
        self.assertTrue(rows)
        self.assertTrue(all(r["ts"] <= 5000 for r in rows))
        conn = sqlite3.connect(self.db)
        logged = conn.execute(
            "SELECT reason, source, as_of, rows_returned "
            "FROM eval_unverified_access").fetchall()
        conn.close()
        self.assertEqual(len(logged), 1)
        self.assertIn("baseline sanity check", logged[0][0])
        self.assertEqual(logged[0][1], "historical_weather_points")
        self.assertEqual(logged[0][2], 5000)
        self.assertEqual(logged[0][3], len(rows))

    def test_allow_unverified_without_a_reason_is_rejected(self):
        for bad in (None, "", "   "):
            with self.assertRaises(ValueError):
                self.view(5000, allow_unverified=True, reason=bad)

    def test_allow_unverified_does_not_disable_the_time_filter(self):
        # The escape hatch is about provenance, not about time travel.
        v = self.view(3000, sources=["historical_weather_points"],
                      allow_unverified=True, reason="checking the filter still applies")
        self.assertTrue(all(r["ts"] <= 3000 for r in v.weather_points("KNYC")))


class TestSourceDeclaration(PitTestCase):
    def test_undeclared_source_raises(self):
        v = self.view(5000, sources=["historical_price_points"])
        with self.assertRaises(pit.SourceNotDeclaredError):
            v.snapshots("MKT-A")

    def test_unknown_source_name_raises_at_construction(self):
        with self.assertRaises(pit.SourceNotDeclaredError):
            self.view(5000, sources=["table_that_does_not_exist"])

    def test_as_of_must_be_an_int(self):
        with self.assertRaises(TypeError):
            self.view(5000.5)

    def test_repr_shows_the_moment_and_the_sources(self):
        r = repr(self.view(5000, sources=["historical_price_points"]))
        self.assertIn("5000", r)
        self.assertIn("historical_price_points", r)


class TestNoVaultReach(unittest.TestCase):
    """The harness must not be able to read the holdout, even by accident.

    splits.load(allow_vault=True) is the only door to the vault, so the
    invariant is that nothing under evaluation/ imports splits or passes
    that keyword. Checked against the parsed AST rather than by grepping
    the text: an earlier version of this test searched for the string and
    failed on a docstring explaining that the module does NOT use it,
    which is the wrong thing to be sensitive to.
    """

    def _package_files(self):
        import evaluation
        directory = os.path.dirname(evaluation.__file__)
        return [os.path.join(directory, f) for f in sorted(os.listdir(directory))
                if f.endswith(".py")]

    def test_pit_does_not_import_splits(self):
        # Narrower than the allow_vault ban below, and deliberately so.
        # folds.py DOES import splits -- it has to, since folds are defined
        # over TRAIN and DEV and splits.load is the sanctioned read path.
        # pit.py is the data access layer and has no business knowing about
        # splits at all, so for this module the import itself is the smell.
        import ast
        import evaluation.pit as mod
        with open(mod.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=mod.__file__)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "splits")
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotEqual(node.module.split(".")[0], "splits")

    def test_no_module_in_evaluation_passes_allow_vault(self):
        import ast
        for path in self._package_files():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        self.assertNotEqual(
                            kw.arg, "allow_vault",
                            f"{os.path.basename(path)} passes allow_vault")


class TestIsoParsing(unittest.TestCase):
    def test_parses_z_and_offsets_and_fractions(self):
        self.assertEqual(pit.iso_to_epoch("1970-01-01T00:16:40Z"), 1000)
        self.assertEqual(pit.iso_to_epoch("1970-01-01T00:16:40+00:00"), 1000)
        self.assertIsNotNone(pit.iso_to_epoch("2026-01-05T04:59:00.123456Z"))

    def test_bad_input_is_none_not_an_exception(self):
        for bad in (None, "", "not a date", 12345):
            self.assertIsNone(pit.iso_to_epoch(bad))


if __name__ == "__main__":
    unittest.main()
