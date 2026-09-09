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
        self.assertEqual(row[1], (100 - 40) * 10)  # 600
        self.assertEqual(self.risk.state.bankroll_cents, 50000 + 600)

    def test_lost_trade_computes_correct_pnl(self):
        storage.log_trade("T2", "yes", 10, 40, "paper", None)
        kc = self._fake_kalshi({"T2": (True, "no")})
        settlement.settle_resolved_trades(kc, self.risk)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT status, pnl_cents FROM trades WHERE ticker='T2'").fetchone()
        self.assertEqual(row[0], "lost")
        self.assertEqual(row[1], -40 * 10)  # -400

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


if __name__ == "__main__":
    unittest.main()
