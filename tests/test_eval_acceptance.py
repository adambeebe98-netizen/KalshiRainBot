"""
The tests that justify the harness existing.

A harness is only worth having if it can tell noise from edge. So:

1. Feed it strategies that provably cannot know anything, and require it
   to report NO EDGE. If random guessing can earn a PASS, every number the
   harness ever produces is worthless, and this test failing is the whole
   point of it being here.
2. Feed it a strategy that peeks at the answer, and require it to notice.
   A harness that rejects everything is also useless -- it would be
   "safe" in the way a broken thermometer reading 20 degrees is safe.

The oracle gets its peek through an explicitly named OracleForecaster that
exists only in this file. There is no route from a PointInTimeView to a
label, so a candidate cannot do this by accident.
"""
import os
import random
import sqlite3
import tempfile
import unittest

from evaluation import execution, harness, registry

DAY = 86400
HOUR = 3600
BASE = 1_700_000_000


def _build_world(path, n_days=240, markets_per_day=2, yes_rate=0.44, seed=11):
    """A synthetic exchange with no predictable structure.

    Outcomes are drawn from a fixed coin. Prices are drawn around the base
    rate with noise and carry no information about the outcome, so there
    is genuinely nothing here to find. Any candidate that 'discovers' edge
    is measuring the harness, not the data.
    """
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
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
        CREATE INDEX idx_hpp ON historical_price_points(ticker, ts);
    """)
    import datetime as dt

    def iso(t):
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    markets, candles = [], []
    for day in range(n_days):
        close = BASE + day * DAY + 12 * HOUR
        open_ = close - 39 * HOUR
        for j in range(markets_per_day):
            ticker = f"SYN-{day:03d}-{j}"
            result = "yes" if rng.random() < yes_rate else "no"
            markets.append((ticker, "SYN", f"EV-{day}", "STN",
                            "precipitation_daily", None, 0.0,
                            "strictly greater than 0 inches of precipitation",
                            iso(open_), iso(close), result, 0.0, 0))
            # Hourly candles over the market's life. The mid wanders around
            # the base rate and is deliberately uninformative.
            mid = yes_rate * 100
            for h in range(40):
                ts = open_ + h * HOUR
                mid = max(8, min(92, mid + rng.gauss(0, 3)))
                bid = int(mid - 2)
                ask = int(mid + 2)
                candles.append((ticker, ts, int(mid), 400, bid, ask))
    conn.executemany(
        "INSERT INTO historical_markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        markets)
    conn.executemany(
        "INSERT INTO historical_price_points "
        "(ticker, ts, yes_price_cents, volume, yes_bid_cents, yes_ask_cents) "
        "VALUES (?,?,?,?,?,?)", candles)
    conn.commit()
    conn.close()
    return [dict(zip(
        ("ticker", "series_ticker", "event_ticker", "station_code", "measure",
         "threshold_low_f", "threshold_high_f", "threshold_description",
         "open_time", "close_time", "result", "expiration_value",
         "backfilled_ts"), m)) for m in markets]


class RandomTrader:
    """Decides by coin flip. Cannot know anything, by construction."""

    def __init__(self, seed):
        self.name = f"random-{seed}"
        self._rng = random.Random(seed)

    def decide(self, view, terms, as_of):
        roll = self._rng.random()
        if roll < 0.40:
            side = "yes"
        elif roll < 0.80:
            side = "no"
        else:
            return execution.Decision(terms.ticker, "yes", 0, as_of)
        return execution.Decision(terms.ticker, side, 10, as_of)


class OracleTrader:
    """Peeks at the settled outcome. Exists only in this file.

    It reaches the label through pit.label_for directly, which is the
    harness's scoring path -- deliberately not reachable from a
    PointInTimeView, so a real candidate cannot do this by accident.
    """

    name = "oracle"

    def __init__(self, db_path):
        self.db_path = db_path

    def decide(self, view, terms, as_of):
        from evaluation import pit
        label = pit.label_for(terms.ticker, db_path=self.db_path)
        if label is None:
            return execution.Decision(terms.ticker, "yes", 0, as_of)
        return execution.Decision(terms.ticker, label.result, 10, as_of)


class AcceptanceTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(cls.db)
        cls.markets = _build_world(cls.db)
        registry.init(cls.db)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    def evaluate(self, trader, **kw):
        kw.setdefault("n_folds", 3)
        # Fewer resamples than production. The bootstrap dominates the cost
        # of a 120-candidate sweep, and these tests assert a verdict rather
        # than a precise p-value -- a gate that only flips between 400 and
        # 2000 resamples was not deciding anything.
        kw.setdefault("bootstrap_resamples", 400)
        return harness.evaluate(
            trader, self.markets,
            execution.HourlyCandleExecution(participation_rate=0.10),
            config={"candidate": trader.name}, seed=7, db_path=self.db, **kw)


class TestRandomStrategiesShowNoEdge(AcceptanceTestCase):
    """The headline acceptance test."""

    def test_a_population_of_random_strategies_produces_no_pass(self):
        # Not literally 1000 -- each evaluation walks three folds of real
        # fold machinery, and 120 is enough to catch a harness that passes
        # noise at any rate worth worrying about. If the true pass rate
        # were the 5% a broken gate might give, seeing zero in 120 would
        # happen about twice in a thousand runs.
        passes = []
        for seed in range(120):
            report = self.evaluate(RandomTrader(seed))
            if report.verdict.passed:
                passes.append((seed, report.report()))
        self.assertEqual(
            passes, [],
            "the harness passed a provably random strategy -- every number "
            "it produces is worthless until this test passes again:\n"
            + "\n\n".join(r for _, r in passes[:3]))

    def test_each_failure_states_a_reason(self):
        report = self.evaluate(RandomTrader(999))
        self.assertFalse(report.verdict.passed)
        self.assertTrue(report.verdict.reasons)
        self.assertNotIn("promising", str(report.verdict).lower())

    def test_random_strategies_do_trade(self):
        # Guards against a vacuous pass of the test above: if nothing ever
        # filled, "no edge found" would be true and meaningless.
        report = self.evaluate(RandomTrader(1))
        self.assertGreater(report.result.n_trades, 0,
                           "no trades filled, so the acceptance test proves "
                           "nothing about the gates")


class TestOracleIsDetected(AcceptanceTestCase):
    """The inverse. A harness that rejects everything is also broken."""

    def test_a_strategy_that_knows_the_answer_is_wildly_profitable(self):
        report = self.evaluate(OracleTrader(self.db))
        self.assertGreater(report.result.net_pnl_cents, 0,
                           "the harness cannot see edge that is definitely "
                           "there:\n" + report.report())
        self.assertGreater(report.result.win_rate, 0.95)

    def test_the_oracle_beats_every_baseline(self):
        report = self.evaluate(OracleTrader(self.db))
        for name, base in report.baseline_results.items():
            self.assertGreater(report.result.net_pnl_cents, base.net_pnl_cents,
                               f"oracle failed to beat baseline {name}")


class TestTheGatesThemselves(AcceptanceTestCase):
    def test_trials_accumulate_across_evaluations(self):
        before = registry.trials_to_date(self.db)
        self.evaluate(RandomTrader(5001))
        self.evaluate(RandomTrader(5002))
        self.assertEqual(registry.trials_to_date(self.db), before + 2)

    def test_the_luck_threshold_rises_as_the_search_widens(self):
        first = self.evaluate(RandomTrader(6001))
        for seed in range(6002, 6012):
            self.evaluate(RandomTrader(seed))
        later = self.evaluate(RandomTrader(6099))
        self.assertGreater(later.trials_to_date, first.trials_to_date)
        self.assertGreaterEqual(later.luck_threshold, first.luck_threshold)

    def test_report_states_the_trial_count_and_the_assumptions(self):
        text = self.evaluate(RandomTrader(7001)).report()
        self.assertIn("trials to date", text)
        self.assertIn("by luck alone", text)
        self.assertIn("NOT modelled", text)

    def test_report_says_only_two_baselines_were_used(self):
        # The missing heuristic baseline must be visible in the output, not
        # only in a docstring nobody rereads.
        text = self.evaluate(RandomTrader(7002)).report()
        self.assertIn("two baselines, not three", text)

    def test_no_reporting_surface_leaks_a_gross_figure(self):
        text = self.evaluate(RandomTrader(7003)).report()
        self.assertNotIn("gross", text.lower())


if __name__ == "__main__":
    unittest.main()
