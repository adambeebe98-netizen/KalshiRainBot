"""Tests for evaluation/baselines.py."""
import os
import sqlite3
import tempfile
import unittest

from evaluation import baselines
from evaluation.pit import PointInTimeView

HOUR = 3600


class BaselineTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE historical_price_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, ts INTEGER,
                yes_price_cents INTEGER, volume INTEGER,
                yes_bid_cents INTEGER, yes_ask_cents INTEGER);
            CREATE TABLE historical_markets (
                ticker TEXT PRIMARY KEY, series_ticker TEXT, event_ticker TEXT,
                station_code TEXT, measure TEXT, threshold_low_f REAL,
                threshold_high_f REAL, threshold_description TEXT,
                open_time TEXT, close_time TEXT, result TEXT,
                expiration_value REAL, backfilled_ts INTEGER);
        """)
        conn.executemany(
            "INSERT INTO historical_price_points "
            "(ticker, ts, yes_price_cents, volume, yes_bid_cents, yes_ask_cents) "
            "VALUES (?,?,?,?,?,?)",
            [("MKT", 1 * HOUR, 30, 100, 28, 32),
             ("MKT", 2 * HOUR, 31, 100, 29, 33),
             ("NOBOOK", 1 * HOUR, 40, 100, None, None)])
        # 7 yes, 3 no -> base rate 0.70
        rows = []
        for i in range(10):
            rows.append((f"T{i}", "S", "E", "ST", "m", None, None, None,
                         "1970-01-01T00:00:00Z", "1970-01-02T00:00:00Z",
                         "yes" if i < 7 else "no", 0.0, 0))
        rows.append(("SCALARMKT", "S", "E", "ST", "m", None, None, None,
                     "1970-01-01T00:00:00Z", "1970-01-02T00:00:00Z",
                     "scalar", 0.0, 0))
        rows.append(("MKT", "S", "E", "ST", "m", None, None, None,
                     "1970-01-01T00:00:00Z", "1970-01-03T00:00:00Z",
                     "yes", 0.0, 0))
        rows.append(("NOBOOK", "S", "E", "ST", "m", None, None, None,
                     "1970-01-01T00:00:00Z", "1970-01-03T00:00:00Z",
                     "no", 0.0, 0))
        conn.executemany(
            "INSERT INTO historical_markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def view(self, as_of=2 * HOUR):
        return PointInTimeView(as_of, sources=["historical_price_points"],
                               db_path=self.db)

    def terms(self, ticker="MKT"):
        v = PointInTimeView(10 ** 9, sources=["historical_price_points"],
                            db_path=self.db)
        return v.market(ticker)


class TestMarketForecaster(BaselineTestCase):
    def test_probability_is_the_mid_of_the_touch(self):
        p = baselines.MarketForecaster().probability(
            self.view(), self.terms(), 2 * HOUR)
        self.assertAlmostEqual(p, (29 + 33) / 200.0)

    def test_falls_back_to_close_when_the_book_is_absent(self):
        p = baselines.MarketForecaster().probability(
            self.view(), self.terms("NOBOOK"), 2 * HOUR)
        self.assertAlmostEqual(p, 0.40)

    def test_declines_when_there_is_no_price_at_all(self):
        early = PointInTimeView(0, sources=["historical_price_points"],
                                db_path=self.db)
        self.assertIsNone(
            baselines.MarketForecaster().probability(early, self.terms(), 0))

    def test_market_forecaster_never_finds_edge_against_itself(self):
        # By construction: it forecasts the mid, and the mid is inside the
        # spread, so it can never clear the threshold on either side.
        trader = baselines.ProbabilityTrader(baselines.MarketForecaster())
        d = trader.decide(self.view(), self.terms(), 2 * HOUR)
        self.assertTrue(d.is_abstain)


class TestConstantBaseRate(BaselineTestCase):
    def test_fits_on_supplied_tickers_only(self):
        rate = baselines.fit_base_rate([f"T{i}" for i in range(10)],
                                        db_path=self.db)
        self.assertAlmostEqual(rate, 0.70)

    def test_scalar_resolutions_are_excluded_from_the_fit(self):
        with_scalar = baselines.fit_base_rate(
            [f"T{i}" for i in range(10)] + ["SCALARMKT"], db_path=self.db)
        self.assertAlmostEqual(with_scalar, 0.70)

    def test_refuses_to_invent_a_rate_from_nothing(self):
        with self.assertRaises(ValueError):
            baselines.fit_base_rate(["NOT-A-TICKER"], db_path=self.db)

    def test_the_fitted_value_is_frozen(self):
        f = baselines.ConstantBaseRateForecaster.fit(
            [f"T{i}" for i in range(10)], db_path=self.db)
        # The guarantee: identical across every market and every moment,
        # so it cannot drift toward the window it is grading.
        seen = {f.probability(self.view(t), self.terms(), t)
                for t in (0, HOUR, 2 * HOUR, 10 ** 9)}
        self.assertEqual(seen, {0.70})

    def test_name_records_the_fitted_value(self):
        f = baselines.ConstantBaseRateForecaster(0.44)
        self.assertIn("0.44", f.name)

    def test_rejects_a_rate_outside_zero_to_one(self):
        for bad in (-0.1, 1.5):
            with self.assertRaises(ValueError):
                baselines.ConstantBaseRateForecaster(bad)


class TestHeuristicBaselineIsHonestlyAbsent(BaselineTestCase):
    def test_it_raises_rather_than_returning_a_plausible_number(self):
        with self.assertRaises(NotImplementedError) as ctx:
            baselines.HeuristicBaseline().probability(
                self.view(), self.terms(), 2 * HOUR)
        self.assertIn("not the live strategy", str(ctx.exception))

    def test_standard_baselines_excludes_it_by_default(self):
        got = baselines.standard_baselines([f"T{i}" for i in range(10)],
                                            db_path=self.db)
        self.assertEqual([b.name for b in got][0], "market_price")
        self.assertEqual(len(got), 2)

    def test_asking_for_all_three_fails_loudly(self):
        # A run that believes it compared against three baselines must not
        # be able to quietly compare against two.
        got = baselines.standard_baselines([f"T{i}" for i in range(10)],
                                            db_path=self.db,
                                            include_heuristic=True)
        with self.assertRaises(NotImplementedError):
            got[-1].decide(self.view(), self.terms(), 2 * HOUR)


class TestProbabilityTrader(BaselineTestCase):
    def test_buys_yes_when_the_forecast_beats_the_ask(self):
        class Bullish(baselines.Forecaster):
            name = "bullish"

            def probability(self, view, terms, as_of):
                return 0.90

        d = baselines.ProbabilityTrader(Bullish()).decide(
            self.view(), self.terms(), 2 * HOUR)
        self.assertEqual(d.side, "yes")
        self.assertEqual(d.contracts, 10)

    def test_buys_no_when_the_forecast_is_below_the_bid(self):
        class Bearish(baselines.Forecaster):
            name = "bearish"

            def probability(self, view, terms, as_of):
                return 0.05

        d = baselines.ProbabilityTrader(Bearish()).decide(
            self.view(), self.terms(), 2 * HOUR)
        self.assertEqual(d.side, "no")

    def test_compares_against_the_payable_price_not_the_mid(self):
        # Ask 33, mid 31. A forecast of 0.36 beats the mid by 5 points but
        # only beats the ask by 3, so it must NOT trade. Booking edge
        # against the mid is edge that could not have been captured.
        class Marginal(baselines.Forecaster):
            name = "marginal"

            def probability(self, view, terms, as_of):
                return 0.36

        d = baselines.ProbabilityTrader(Marginal()).decide(
            self.view(), self.terms(), 2 * HOUR)
        self.assertTrue(d.is_abstain)

    def test_declining_to_forecast_means_abstaining(self):
        class Silent(baselines.Forecaster):
            name = "silent"

            def probability(self, view, terms, as_of):
                return None

        self.assertTrue(baselines.ProbabilityTrader(Silent()).decide(
            self.view(), self.terms(), 2 * HOUR).is_abstain)

    def test_extreme_prices_are_skipped(self):
        class Bullish(baselines.Forecaster):
            name = "b"

            def probability(self, view, terms, as_of):
                return 0.999

        cfg = baselines.TraderConfig(max_price_cents=10)
        d = baselines.ProbabilityTrader(Bullish(), cfg).decide(
            self.view(), self.terms(), 2 * HOUR)
        self.assertTrue(d.is_abstain)

    def test_every_forecaster_shares_one_trading_rule(self):
        # The comparison is only about forecasting if the trading logic is
        # identical, so the trader is the same object for all of them.
        a = baselines.ProbabilityTrader(baselines.MarketForecaster())
        b = baselines.ProbabilityTrader(
            baselines.ConstantBaseRateForecaster(0.5))
        self.assertIs(type(a), type(b))
        self.assertEqual(a.config, b.config)


if __name__ == "__main__":
    unittest.main()
