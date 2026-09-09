"""
Covers storage.py's supporting logic for two real fixes:

1. get_todays_realized_pnl_cents()/_main(): reconstructs the kill
   switch's real starting state from already-recorded settled trades,
   since RiskState used to always start at 0 on restart (see
   test_risk_manager.py for the RiskManager-side test of the same bug).

2. bracket_arbitrage's dashboard exclusion: disabled after a 0% win rate
   over 18 settled trades (structurally near-impossible for a correctly
   hedged bracket set), but its historical rows stay in the database —
   this only needs to disappear from summaries, not be deleted.
"""
from __future__ import annotations

import time
import unittest

from tests.helpers import use_temp_db

use_temp_db()

import storage  # noqa: E402


def _clear(*tables):
    with storage.get_conn() as conn:
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        conn.commit()


class TestTodaysRealizedPnl(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("shadow_trades", "trades", "shadow_bankroll_snapshots")

    def test_sums_todays_settled_trades_for_one_strategy(self):
        tid1 = storage.log_shadow_trade("calibrated_balanced", "T1", "yes", 10, 40)
        tid2 = storage.log_shadow_trade("calibrated_balanced", "T2", "yes", 10, 40)
        storage.settle_shadow_trade(tid1, won=False, pnl_cents=-400)
        storage.settle_shadow_trade(tid2, won=False, pnl_cents=-400)
        self.assertEqual(storage.get_todays_realized_pnl_cents("calibrated_balanced"), -800)

    def test_excludes_trades_settled_on_a_previous_day(self):
        old_ts = int(time.time()) - 86400 * 2
        with storage.get_conn() as conn:
            conn.execute(
                "INSERT INTO shadow_trades (ts, strategy, ticker, side, count, price_cents, status, settled_ts, pnl_cents) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (old_ts, "calibrated_balanced", "OLD", "yes", 10, 40, "lost", old_ts, -400),
            )
            conn.commit()
        self.assertEqual(storage.get_todays_realized_pnl_cents("calibrated_balanced"), 0)

    def test_isolates_by_strategy(self):
        tid = storage.log_shadow_trade("calibrated_aggressive", "T3", "yes", 10, 40)
        storage.settle_shadow_trade(tid, won=False, pnl_cents=-999)
        self.assertEqual(storage.get_todays_realized_pnl_cents("calibrated_balanced"), 0)
        self.assertEqual(storage.get_todays_realized_pnl_cents("calibrated_aggressive"), -999)

    def test_open_unsettled_trades_are_not_counted(self):
        storage.log_shadow_trade("calibrated_balanced", "OPEN1", "yes", 10, 40)
        self.assertEqual(storage.get_todays_realized_pnl_cents("calibrated_balanced"), 0)

    def test_main_bot_version_uses_the_trades_table(self):
        now = int(time.time())
        with storage.get_conn() as conn:
            conn.execute(
                "INSERT INTO trades (ts, ticker, side, count, price_cents, mode, status, settled_ts, pnl_cents) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (now, "MAIN1", "yes", 5, 50, "paper", "lost", now, -250),
            )
            conn.commit()
        self.assertEqual(storage.get_todays_realized_pnl_cents_main(), -250)


class TestShadowSummaryExclusions(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("shadow_trades", "shadow_bankroll_snapshots")

    def test_bracket_arbitrage_excluded_from_leaderboard(self):
        storage.snapshot_shadow_bankroll("bracket_arbitrage", 5000)
        storage.snapshot_shadow_bankroll("calibrated_balanced", 50000)
        names = [s["strategy"] for s in storage.get_shadow_summary()]
        self.assertNotIn("bracket_arbitrage", names)
        self.assertIn("calibrated_balanced", names)

    def test_bracket_arbitrage_excluded_from_category_breakdown(self):
        tid = storage.log_shadow_trade("bracket_arbitrage", "OLD-BA", "no", 10, 90,
                                         measure=None, station_code=None)
        storage.settle_shadow_trade(tid, won=False, pnl_cents=-900)
        cat_summary = storage.get_shadow_summary_by_category()
        for cat, data in cat_summary.items():
            names = [s["strategy"] for s in data["strategies"]]
            self.assertNotIn("bracket_arbitrage", names)

    def test_historical_bracket_arbitrage_rows_are_not_deleted(self):
        """Disabling from summaries must not touch the underlying data —
        someone may still want to debug the root cause later."""
        storage.log_shadow_trade("bracket_arbitrage", "OLD-BA-2", "no", 10, 90)
        with storage.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM shadow_trades WHERE strategy='bracket_arbitrage'"
            ).fetchone()[0]
        self.assertEqual(count, 1)


class TestCurrentOpenMarkets(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("price_history")

    def test_returns_latest_snapshot_per_ticker(self):
        storage.log_price_snapshot("A", 40, 35)
        storage.log_price_snapshot("A", 42, 37)  # more recent -- should win
        markets = storage.get_current_open_markets()
        row = next(m for m in markets if m["ticker"] == "A")
        self.assertEqual(row["yes_ask"], 42)

    def test_stale_tickers_age_out_of_the_window(self):
        stale_ts = int(time.time()) - 2000
        with storage.get_conn() as conn:
            conn.execute(
                "INSERT INTO price_history (ts, ticker, yes_ask, yes_bid) VALUES (?,?,?,?)",
                (stale_ts, "STALE", 20, 15),
            )
            conn.commit()
        markets = storage.get_current_open_markets(window_seconds=900)
        tickers = {m["ticker"] for m in markets}
        self.assertNotIn("STALE", tickers)

    def test_a_market_with_no_live_ask_still_shows_up(self):
        """The vanishing-market fix: a thin market with no ask at all must
        still be visible, not silently disappear from the dashboard."""
        storage.log_price_snapshot("THIN", None, 12)
        markets = storage.get_current_open_markets()
        row = next(m for m in markets if m["ticker"] == "THIN")
        self.assertIsNone(row["yes_ask"])
        self.assertEqual(row["yes_bid"], 12)


if __name__ == "__main__":
    unittest.main()
