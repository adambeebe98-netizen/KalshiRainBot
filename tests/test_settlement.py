"""
Covers settlement.py's orchestration: real-trade win/loss/pnl math, the
calibration feedback loop, deduplicated settlement checks across real and
shadow trades, and — most importantly — that the sequential ordering
(main trades -> swing exits -> legacy bracket settlement -> bracket
offload -> generic shadow settle) never double-processes the same trade,
since each step re-queries "open" status reflecting prior steps' effects
within the same cycle.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import fees  # noqa: E402
import storage  # noqa: E402
import shadow  # noqa: E402
from risk_manager import RiskManager, RiskState  # noqa: E402
from datetime import date  # noqa: E402
import settlement  # noqa: E402


class TestSettleResolvedTrades(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "trades", "shadow_trades", "shadow_bankroll_snapshots",
                         "calibration_stats", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()
        self.risk = RiskManager(RiskState(bankroll_cents=50000, day=date.today()))

    def _fake_kalshi(self, results: dict):
        """results: {ticker: (is_settled, 'yes'|'no'|None)}"""
        kc = MagicMock()
        kc.get_market_settlement.side_effect = lambda t: results.get(t, (False, None))
        return kc

    def test_won_trade_computes_correct_pnl_and_updates_bankroll(self):
        storage.log_trade("T1", "yes", 10, 40, "paper", None,
                           model_probability=0.7, station_code="KAUS", measure="precipitation_daily")
        kc = self._fake_kalshi({"T1": (True, "yes")})
        settled = settlement.settle_resolved_trades(kc, self.risk)
        self.assertEqual(settled, 1)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT status, pnl_cents FROM trades WHERE ticker='T1'").fetchone()
        self.assertEqual(row[0], "won")
        # NET of the real entry fee, not the gross 600c — the taker fee was
        # really paid to open this position and never comes back.
        fee = fees.taker_fee_cents(10, 40)
        self.assertEqual(row[1], (100 - 40) * 10 - fee)
        self.assertEqual(self.risk.state.bankroll_cents, 50000 + 600 - fee)

    def test_lost_trade_computes_correct_pnl(self):
        storage.log_trade("T2", "yes", 10, 40, "paper", None)
        kc = self._fake_kalshi({"T2": (True, "no")})
        settlement.settle_resolved_trades(kc, self.risk)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT status, pnl_cents FROM trades WHERE ticker='T2'").fetchone()
        self.assertEqual(row[0], "lost")
        # A loser pays the fee too — it's charged on the way in, so the
        # loss is bigger than the contracts' cost alone.
        self.assertEqual(row[1], -40 * 10 - fees.taker_fee_cents(10, 40))

    def test_settlement_charges_the_real_recorded_fee_over_the_estimate(self):
        """When the fee really charged at decision time was stored on the
        trade (bot.py passes find_max_profitable_size's own number), that
        exact figure is what settlement deducts — the estimate is only a
        fallback for rows that predate the column being filled in."""
        storage.log_trade("T5", "yes", 10, 40, "paper", None, fee_cents_paid=999)
        kc = self._fake_kalshi({"T5": (True, "yes")})
        settlement.settle_resolved_trades(kc, self.risk)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT pnl_cents, fee_cents_paid, fees_applied_to_pnl FROM trades "
                "WHERE ticker='T5'").fetchone()
        self.assertEqual(row[0], (100 - 40) * 10 - 999)
        self.assertEqual(row[1], 999)
        self.assertEqual(row[2], 1)  # marked so rederive_fees.py never re-charges it

    def test_unsettled_market_is_left_open(self):
        storage.log_trade("T3", "yes", 10, 40, "paper", None)
        kc = self._fake_kalshi({"T3": (False, None)})
        settled = settlement.settle_resolved_trades(kc, self.risk)
        self.assertEqual(settled, 0)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT status FROM trades WHERE ticker='T3'").fetchone()
        self.assertEqual(row[0], "open")

    def test_feeds_calibration_with_the_yes_event_convention(self):
        """Regardless of which side was traded, calibration must track
        P(yes), not P(traded side) — see strategy.TradeSignal's docstring."""
        storage.log_trade("T4", "no", 10, 60, "paper", None,
                           model_probability=0.35,  # this is model_probability_yes at decision time
                           station_code="KAUS", measure="precipitation_daily")
        kc = self._fake_kalshi({"T4": (True, "no")})  # NO side won -> actual outcome was NOT yes
        settlement.settle_resolved_trades(kc, self.risk)
        n, avg_predicted, avg_actual = storage.get_calibration_stats("KAUS", "precipitation_daily")
        self.assertEqual(n, 1)
        self.assertAlmostEqual(avg_predicted, 0.35)
        self.assertEqual(avg_actual, 0.0, "result was 'no', so the yes-event actually occurred 0 times")

    def test_settlement_checks_are_deduplicated_across_real_and_shadow_trades(self):
        """A ticker that's both a real trade AND held by several shadow
        strategies should only trigger ONE settlement API call."""
        storage.log_trade("SHARED", "yes", 5, 40, "paper", None)
        storage.log_shadow_trade("calibrated_balanced", "SHARED", "yes", 10, 40)
        storage.log_shadow_trade("calibrated_aggressive", "SHARED", "yes", 10, 40)
        kc = self._fake_kalshi({"SHARED": (True, "yes")})
        settlement.settle_resolved_trades(kc, self.risk)
        self.assertEqual(kc.get_market_settlement.call_count, 1)

    def test_a_settlement_check_failure_does_not_crash_the_whole_cycle(self):
        storage.log_trade("BAD", "yes", 5, 40, "paper", None)
        storage.log_trade("GOOD", "yes", 5, 40, "paper", None)
        kc = MagicMock()
        def flaky(ticker):
            if ticker == "BAD":
                raise RuntimeError("simulated API failure")
            return (True, "yes")
        kc.get_market_settlement.side_effect = flaky
        settled = settlement.settle_resolved_trades(kc, self.risk)
        self.assertEqual(settled, 1)  # GOOD settles fine despite BAD's failure
        with storage.get_conn() as conn:
            bad_row = conn.execute("SELECT status FROM trades WHERE ticker='BAD'").fetchone()
        self.assertEqual(bad_row[0], "open", "a failed check should leave the trade open, not crash or corrupt it")

    def test_shadow_trades_settle_alongside_real_trades_in_one_pass(self):
        storage.log_trade("BOTH1", "yes", 5, 40, "paper", None)
        storage.log_shadow_trade("calibrated_balanced", "BOTH1", "yes", 10, 40)
        kc = self._fake_kalshi({"BOTH1": (True, "yes")})
        settlement.settle_resolved_trades(kc, self.risk)
        with storage.get_conn() as conn:
            real_row = conn.execute("SELECT status FROM trades WHERE ticker='BOTH1'").fetchone()
            shadow_row = conn.execute(
                "SELECT status FROM shadow_trades WHERE ticker='BOTH1' AND strategy='calibrated_balanced'"
            ).fetchone()
        self.assertEqual(real_row[0], "won")
        self.assertEqual(shadow_row[0], "won")


class TestBackfillMarketOutcomes(unittest.TestCase):
    """Foundation for retrospective backtesting, explicitly requested:
    collect data across every scanned market, then later analyze it to
    find where a profitable trade existed that current strategies
    missed. That question can only be answered once the real outcome is
    known for markets nobody ever traded, which is exactly what this
    fills in."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "market_snapshots", "market_outcomes")

    def _fake_kalshi(self, result_tuple=None, raises=None):
        kc = MagicMock()
        if raises:
            kc.get_market_settlement.side_effect = raises
        else:
            kc.get_market_settlement.return_value = result_tuple
        return kc

    def test_a_real_settled_market_gets_its_outcome_recorded(self):
        storage.log_market_snapshot("T1", close_time="2020-01-01T00:00:00Z")
        kc = self._fake_kalshi((True, "yes"))
        recorded = settlement.backfill_market_outcomes(kc)
        self.assertEqual(recorded, 1)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT result FROM market_outcomes WHERE ticker='T1'").fetchone()
        self.assertEqual(row[0], "yes")

    def test_a_market_not_actually_settled_yet_is_not_falsely_recorded(self):
        storage.log_market_snapshot("T2", close_time="2020-01-01T00:00:00Z")
        kc = self._fake_kalshi((False, None))
        recorded = settlement.backfill_market_outcomes(kc)
        self.assertEqual(recorded, 0)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM market_outcomes WHERE ticker='T2'").fetchone()
        self.assertIsNone(row)

    def test_an_api_error_is_caught_and_the_ticker_stays_pending(self):
        storage.log_market_snapshot("T3", close_time="2020-01-01T00:00:00Z")
        kc = self._fake_kalshi(raises=Exception("404"))
        recorded = settlement.backfill_market_outcomes(kc)
        self.assertEqual(recorded, 0)
        pending = storage.get_tickers_needing_outcome_backfill()
        self.assertIn("T3", [r["ticker"] for r in pending])

    def test_limit_is_passed_through_correctly(self):
        for i in range(10):
            storage.log_market_snapshot(f"MANY-{i}", close_time="2020-01-01T00:00:00Z")
        kc = self._fake_kalshi((True, "no"))
        recorded = settlement.backfill_market_outcomes(kc, limit=3)
        self.assertEqual(recorded, 3)


if __name__ == "__main__":
    unittest.main()
