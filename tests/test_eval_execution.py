"""Tests for evaluation/execution.py and evaluation/objective.py."""
import os
import sqlite3
import tempfile
import unittest

import fees
from evaluation import execution, objective
from evaluation.pit import PointInTimeView

HOUR = 3600


def _make_db(path, candles):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE historical_price_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, ts INTEGER,
            yes_price_cents INTEGER, volume INTEGER,
            yes_bid_cents INTEGER, yes_ask_cents INTEGER,
            open_interest INTEGER);
    """)
    conn.executemany(
        "INSERT INTO historical_price_points "
        "(ticker, ts, yes_price_cents, volume, yes_bid_cents, yes_ask_cents) "
        "VALUES (?,?,?,?,?,?)", candles)
    conn.commit()
    conn.close()


class ExecutionTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        _make_db(self.db, [
            # ticker, ts, close, volume, bid, ask
            ("MKT", 1 * HOUR, 50, 100, 48, 52),
            ("MKT", 2 * HOUR, 55, 200, 53, 57),
            ("MKT", 3 * HOUR, 60, 0, 58, 62),      # zero volume -> no fill
            ("MKT", 4 * HOUR, 61, 50, 59, 63),
            ("NOBOOK", 1 * HOUR, 50, 100, None, None),
        ])

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def view(self, as_of):
        return PointInTimeView(as_of, sources=["historical_price_points"],
                               db_path=self.db)


class TestDecision(unittest.TestCase):
    def test_rejects_bad_side(self):
        with self.assertRaises(ValueError):
            execution.Decision("MKT", "maybe", 10, 0)

    def test_rejects_negative_size(self):
        with self.assertRaises(ValueError):
            execution.Decision("MKT", "yes", -1, 0)

    def test_zero_contracts_is_abstain(self):
        self.assertTrue(execution.Decision("MKT", "yes", 0, 0).is_abstain)


class TestHourlyCandleExecution(ExecutionTestCase):
    def test_fills_at_the_next_candles_ask_not_its_close(self):
        model = execution.HourlyCandleExecution()
        d = execution.Decision("MKT", "yes", 10, 1 * HOUR)
        fill = model.execute(d, self.view(1 * HOUR))
        self.assertIsNotNone(fill)
        self.assertEqual(fill.filled_at, 2 * HOUR)
        self.assertEqual(fill.price_cents, 57)       # the ask, not close 55

    def test_buying_no_costs_one_hundred_minus_bid(self):
        model = execution.HourlyCandleExecution()
        d = execution.Decision("MKT", "no", 10, 1 * HOUR)
        fill = model.execute(d, self.view(1 * HOUR))
        self.assertEqual(fill.price_cents, 100 - 53)

    def test_zero_volume_hour_does_not_fill(self):
        model = execution.HourlyCandleExecution()
        d = execution.Decision("MKT", "yes", 10, 2 * HOUR)
        self.assertIsNone(model.execute(d, self.view(2 * HOUR)))

    def test_partial_fill_is_capped_by_participation(self):
        model = execution.HourlyCandleExecution(participation_rate=0.10)
        d = execution.Decision("MKT", "yes", 100, 1 * HOUR)
        fill = model.execute(d, self.view(1 * HOUR))
        self.assertEqual(fill.contracts, 20)         # 10% of volume 200
        self.assertEqual(fill.requested_contracts, 100)
        self.assertTrue(fill.was_partial)

    def test_size_is_never_more_than_requested(self):
        model = execution.HourlyCandleExecution(participation_rate=1.0)
        d = execution.Decision("MKT", "yes", 5, 1 * HOUR)
        self.assertEqual(model.execute(d, self.view(1 * HOUR)).contracts, 5)

    def test_missing_book_side_does_not_fill(self):
        model = execution.HourlyCandleExecution()
        d = execution.Decision("NOBOOK", "yes", 10, 0)
        self.assertIsNone(model.execute(d, self.view(0)))

    def test_abstention_never_fills(self):
        model = execution.HourlyCandleExecution()
        d = execution.Decision("MKT", "yes", 0, 1 * HOUR)
        self.assertIsNone(model.execute(d, self.view(1 * HOUR)))

    def test_latency_pushes_the_fill_to_a_later_candle(self):
        model = execution.HourlyCandleExecution(latency_seconds=2 * HOUR)
        d = execution.Decision("MKT", "yes", 5, 1 * HOUR)
        fill = model.execute(d, self.view(1 * HOUR))
        self.assertEqual(fill.filled_at, 4 * HOUR)   # 3h candle has no volume

    def test_no_future_candle_means_no_fill(self):
        model = execution.HourlyCandleExecution()
        d = execution.Decision("MKT", "yes", 5, 99 * HOUR)
        self.assertIsNone(model.execute(d, self.view(99 * HOUR)))

    def test_rejects_bad_parameters(self):
        with self.assertRaises(ValueError):
            execution.HourlyCandleExecution(participation_rate=0.0)
        with self.assertRaises(ValueError):
            execution.HourlyCandleExecution(participation_rate=1.5)
        with self.assertRaises(ValueError):
            execution.HourlyCandleExecution(latency_seconds=-1)


class TestAssumptionsAreDeclared(unittest.TestCase):
    def test_every_unmodelled_thing_is_stated(self):
        a = execution.HourlyCandleExecution().assumptions()
        self.assertFalse(a.uses_depth)
        self.assertFalse(a.models_queue_position)
        self.assertFalse(a.models_market_impact)
        self.assertFalse(a.models_adverse_selection)
        self.assertEqual(a.latency_resolution_seconds, 3600)
        self.assertTrue(a.notes)

    def test_description_says_what_is_not_modelled(self):
        text = execution.HourlyCandleExecution().assumptions().describe()
        self.assertIn("NOT modelled", text)
        self.assertIn("depth", text)

    def test_assumptions_are_hashable_and_comparable(self):
        a = execution.HourlyCandleExecution().assumptions()
        b = execution.HourlyCandleExecution().assumptions()
        c = execution.HourlyCandleExecution(latency_seconds=60).assumptions()
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class TestCoverageRefusal(ExecutionTestCase):
    def test_raises_when_the_period_has_no_data(self):
        cov = execution.ExecutionCoverage(execution.HourlyCandleExecution(),
                                           "historical_price_points")
        with self.assertRaises(execution.NoExecutionDataError):
            cov.require(500 * HOUR, 600 * HOUR, db_path=self.db)

    def test_passes_when_data_exists(self):
        cov = execution.ExecutionCoverage(execution.HourlyCandleExecution(),
                                           "historical_price_points")
        self.assertGreater(cov.require(0, 10 * HOUR, db_path=self.db), 0)


class TestSettlementAccounting(unittest.TestCase):
    def _fill(self, side="yes", price=40, n=10):
        return execution.Fill(ticker="MKT", side=side, contracts=n,
                              price_cents=price, filled_at=0,
                              requested_contracts=n)

    def test_winning_yes_pays_the_complement_less_fees(self):
        t = objective.settle(self._fill("yes", 40, 10), outcome="yes")
        self.assertEqual(t.gross_pnl_cents, (100 - 40) * 10)
        self.assertEqual(t.entry_fee_cents, fees.taker_fee_cents(10, 40))
        self.assertEqual(t.net_pnl_cents, t.gross_pnl_cents - t.entry_fee_cents)

    def test_losing_yes_loses_what_was_paid_plus_fees(self):
        t = objective.settle(self._fill("yes", 40, 10), outcome="no")
        self.assertEqual(t.gross_pnl_cents, -400)
        self.assertLess(t.net_pnl_cents, t.gross_pnl_cents)

    def test_no_side_wins_when_the_event_does_not_happen(self):
        t = objective.settle(self._fill("no", 60, 10), outcome="no")
        self.assertEqual(t.gross_pnl_cents, (100 - 60) * 10)

    def test_held_to_settlement_pays_one_fee_not_two(self):
        t = objective.settle(self._fill("yes", 50, 10), outcome="yes")
        self.assertEqual(t.exit_fee_cents, 0,
                         "Kalshi charges on execution, nothing at settlement")

    def test_sold_early_pays_two_fees(self):
        t = objective.settle(self._fill("yes", 50, 10), outcome="yes",
                             exit_price_cents=70)
        self.assertGreater(t.exit_fee_cents, 0)
        self.assertEqual(t.gross_pnl_cents, (70 - 50) * 10)

    def test_selling_early_ignores_the_eventual_outcome(self):
        a = objective.settle(self._fill("yes", 50, 10), "yes", exit_price_cents=70)
        b = objective.settle(self._fill("yes", 50, 10), "no", exit_price_cents=70)
        self.assertEqual(a.net_pnl_cents, b.net_pnl_cents)

    def test_conservative_fees_charges_an_exit_that_never_happened(self):
        normal = objective.settle(self._fill("yes", 50, 10), "yes")
        cons = objective.settle(self._fill("yes", 50, 10), "yes",
                                conservative_fees=True)
        self.assertLess(cons.net_pnl_cents, normal.net_pnl_cents)

    def test_fee_cap_is_respected_on_large_orders(self):
        t = objective.settle(self._fill("yes", 50, 10_000), outcome="yes")
        self.assertEqual(t.entry_fee_cents, fees.MAX_FEE_CENTS_PER_ORDER)

    def test_return_is_on_capital_risked_not_notional(self):
        t = objective.settle(self._fill("yes", 5, 10), outcome="yes")
        # Risked 50c, made 950c gross. Return should be near 19x, not near 0.95.
        self.assertGreater(t.net_return, 15.0)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            objective.settle(self._fill(), outcome="maybe")
        with self.assertRaises(ValueError):
            objective.settle(self._fill(n=0), outcome="yes")


class TestResultReportsNetOnly(unittest.TestCase):
    def _result(self, label="r"):
        fills = [execution.Fill("MKT", "yes", 10, 40, 0, 10),
                 execution.Fill("MKT2", "yes", 10, 60, 0, 10)]
        trades = (objective.settle(fills[0], "yes"),
                  objective.settle(fills[1], "no"))
        return objective.Result(trades=trades,
                                assumptions=execution.HourlyCandleExecution().assumptions(),
                                n_decisions=10, label=label)

    def test_no_reporting_surface_emits_a_gross_figure(self):
        # The structural guarantee. Gross exists for diagnosis and is
        # absent from anything anyone reads.
        r = self._result()
        gross = r._diagnostic_gross_pnl_cents
        for surface in (r.summary(), str(r), r.report()):
            self.assertNotIn("gross", surface.lower())
            self.assertNotIn(f"{gross / 100:+.2f}", surface)

    def test_gross_is_still_available_for_diagnosis(self):
        r = self._result()
        self.assertEqual(r._diagnostic_gross_pnl_cents,
                         sum(t.gross_pnl_cents for t in r.trades))

    def test_headline_is_net_and_differs_from_gross(self):
        r = self._result()
        self.assertNotEqual(r.net_pnl_cents, r._diagnostic_gross_pnl_cents)
        self.assertLess(r.net_pnl_cents, r._diagnostic_gross_pnl_cents)

    def test_summary_carries_fees_and_fill_rate(self):
        s = self._result().summary()
        self.assertIn("fees", s)
        self.assertIn("drag", s)
        self.assertIn("filled", s)

    def test_report_includes_the_assumptions(self):
        self.assertIn("NOT modelled", self._result().report())

    def test_result_requires_assumptions(self):
        with self.assertRaises(TypeError):
            objective.Result(trades=())

    def test_sharpe_is_none_rather_than_raising_on_thin_results(self):
        one = objective.Result(
            trades=(objective.settle(
                execution.Fill("M", "yes", 1, 50, 0, 1), "yes"),),
            assumptions=execution.HourlyCandleExecution().assumptions())
        self.assertIsNone(one.net_sharpe())

    def test_fee_drag_is_reported(self):
        self.assertGreater(self._result().fee_drag(), 0.0)

    def test_fill_rate_uses_decisions_not_trades(self):
        self.assertAlmostEqual(self._result().fill_rate, 0.2)


class TestCombine(unittest.TestCase):
    def _result(self, assumptions=None):
        trades = (objective.settle(execution.Fill("M", "yes", 10, 40, 0, 10),
                                    "yes"),)
        return objective.Result(
            trades=trades,
            assumptions=assumptions or execution.HourlyCandleExecution().assumptions(),
            n_decisions=5)

    def test_pools_trades_and_decisions(self):
        c = objective.combine([self._result(), self._result()])
        self.assertEqual(c.n_trades, 2)
        self.assertEqual(c.n_decisions, 10)

    def test_refuses_to_mix_execution_assumptions(self):
        other = execution.HourlyCandleExecution(participation_rate=0.5).assumptions()
        with self.assertRaises(ValueError):
            objective.combine([self._result(), self._result(other)])

    def test_refuses_empty(self):
        with self.assertRaises(ValueError):
            objective.combine([])


if __name__ == "__main__":
    unittest.main()
