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


class TestOpenPositionsDetail(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("shadow_trades", "price_history")

    def test_total_count_is_accurate_beyond_the_detail_list_limit(self):
        """THE regression: the dashboard badge used to show the LENGTH of
        get_open_positions_detail()'s (necessarily capped, for page
        weight) list, which silently truncates at 300 -- meaning a real
        total above 300 would display as exactly 300, hiding the true
        number entirely."""
        for i in range(350):
            storage.log_shadow_trade("calibrated_balanced", f"T{i}", "yes", 5, 40)
        self.assertEqual(storage.get_open_shadow_position_total_count(), 350)
        self.assertEqual(len(storage.get_open_positions_detail()), 300)

    def test_total_count_matches_detail_length_when_under_the_limit(self):
        for i in range(5):
            storage.log_shadow_trade("swing", f"T{i}", "yes", 5, 40)
        self.assertEqual(storage.get_open_shadow_position_total_count(),
                          len(storage.get_open_positions_detail()))

    def test_returns_full_per_trade_detail_including_rationale_and_confidence(self):
        storage.log_shadow_trade("calibrated_balanced", "T1", "no", 10, 60,
                                   model_probability=0.35, confidence="high",
                                   rationale="station already recorded rain")
        storage.log_price_snapshot("T1", yes_ask=25, yes_bid=None)

        positions = storage.get_open_positions_detail()
        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p["current_price_cents"], 75)  # 100 - 25
        self.assertEqual(p["current_value_cents"], 750)
        self.assertEqual(p["unrealized_pnl_cents"], 150)
        self.assertEqual(p["rationale"], "station already recorded rain")
        self.assertEqual(p["confidence"], "high")

    def test_includes_exit_target_when_set(self):
        storage.log_shadow_trade("swing", "T2", "yes", 5, 45, exit_target_cents=65,
                                   rationale="entering on a dip")
        positions = storage.get_open_positions_detail()
        self.assertEqual(positions[0]["exit_target_cents"], 65)

    def test_excludes_settled_trades(self):
        tid = storage.log_shadow_trade("swing", "T3", "yes", 5, 45)
        storage.settle_shadow_trade(tid, won=True, pnl_cents=100)
        positions = storage.get_open_positions_detail()
        self.assertEqual(len(positions), 0)

    def test_orders_most_recently_opened_first(self):
        storage.log_shadow_trade("calibrated_balanced", "OLDEST", "yes", 5, 20)
        storage.log_shadow_trade("swing", "MIDDLE", "yes", 5, 20)
        storage.log_shadow_trade("longshot", "NEWEST", "yes", 5, 20)
        positions = storage.get_open_positions_detail()
        self.assertEqual(positions[0]["ticker"], "NEWEST")
        self.assertEqual(positions[-1]["ticker"], "OLDEST")

    def test_both_side_position_marks_to_locked_in_100c(self):
        storage.log_shadow_trade("arbitrage", "T4", "both", 5, 90)
        positions = storage.get_open_positions_detail()
        self.assertEqual(positions[0]["current_price_cents"], 100)


class TestOpenPositionsDetail_RefactorConsistency(unittest.TestCase):
    """Confirms the shared _current_mark_for_position() helper keeps the
    per-strategy aggregate and the per-trade detail view from ever
    disagreeing on what "current value" means for the same position."""

    def setUp(self):
        storage.init_db()
        _clear("shadow_trades", "shadow_bankroll_snapshots", "price_history")

    def test_aggregate_and_detail_agree_on_the_same_position(self):
        storage.log_price_snapshot("T1", yes_ask=None, yes_bid=55)
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 50000)

        summary_row = next(s for s in storage.get_shadow_summary() if s["strategy"] == "swing")
        detail_row = storage.get_open_positions_detail()[0]

        self.assertEqual(summary_row["current_value_cents"], detail_row["current_value_cents"])
        self.assertEqual(summary_row["unrealized_pnl_cents"], detail_row["unrealized_pnl_cents"])


class TestMarkToMarket(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("shadow_trades", "shadow_bankroll_snapshots", "price_history")

    def test_yes_position_marks_to_current_bid(self):
        storage.log_price_snapshot("T1", yes_ask=None, yes_bid=55)
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 50000)
        row = next(s for s in storage.get_shadow_summary() if s["strategy"] == "swing")
        self.assertEqual(row["open_capital_cents"], 400)
        self.assertEqual(row["current_value_cents"], 550)
        self.assertEqual(row["unrealized_pnl_cents"], 150)

    def test_no_position_marks_via_inverted_yes_ask(self):
        storage.log_price_snapshot("T2", yes_ask=25, yes_bid=None)
        storage.log_shadow_trade("calibrated_balanced", "T2", "no", 10, 60)
        storage.snapshot_shadow_bankroll("calibrated_balanced", 50000)
        row = next(s for s in storage.get_shadow_summary() if s["strategy"] == "calibrated_balanced")
        self.assertEqual(row["current_value_cents"], 750)  # 10 * (100-25)
        self.assertEqual(row["unrealized_pnl_cents"], 150)

    def test_both_side_arbitrage_position_is_locked_at_100c_per_contract(self):
        """Dutch-book arbitrage doesn't fluctuate with price the way a
        directional position does -- it's already guaranteed at entry."""
        storage.log_shadow_trade("arbitrage", "T3", "both", 5, 90)
        storage.snapshot_shadow_bankroll("arbitrage", 50000)
        row = next(s for s in storage.get_shadow_summary() if s["strategy"] == "arbitrage")
        self.assertEqual(row["open_capital_cents"], 450)
        self.assertEqual(row["current_value_cents"], 500)  # 5 * 100, unaffected by any price_history

    def test_falls_back_to_cost_basis_with_no_price_data(self):
        """No fabricated gain/loss when there's genuinely no current quote
        to mark against."""
        storage.log_shadow_trade("longshot", "T4", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("longshot", 50000)
        row = next(s for s in storage.get_shadow_summary() if s["strategy"] == "longshot")
        self.assertEqual(row["current_value_cents"], row["open_capital_cents"])
        self.assertEqual(row["unrealized_pnl_cents"], 0)

    def test_settled_trades_are_never_included_in_mark_to_market(self):
        tid = storage.log_shadow_trade("swing", "T5", "yes", 10, 40)
        storage.settle_shadow_trade(tid, won=True, pnl_cents=600)
        storage.snapshot_shadow_bankroll("swing", 50600)
        row = next(s for s in storage.get_shadow_summary() if s["strategy"] == "swing")
        self.assertEqual(row["open"], 0)
        self.assertEqual(row["current_value_cents"], 0)


class TestOpenPositionCount(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("shadow_trades", "trades")

    def test_counts_only_open_shadow_positions_for_the_named_strategy(self):
        storage.log_shadow_trade("calibrated_balanced", "T1", "yes", 10, 40)
        storage.log_shadow_trade("calibrated_balanced", "T2", "yes", 10, 40)
        tid3 = storage.log_shadow_trade("calibrated_balanced", "T3", "yes", 10, 40)
        storage.settle_shadow_trade(tid3, won=True, pnl_cents=600)  # settled, should not count
        storage.log_shadow_trade("calibrated_aggressive", "T4", "yes", 10, 40)  # different strategy

        self.assertEqual(storage.get_open_shadow_position_count("calibrated_balanced"), 2)

    def test_zero_for_a_strategy_with_no_trades(self):
        self.assertEqual(storage.get_open_shadow_position_count("nonexistent"), 0)

    def test_main_bot_version_counts_the_trades_table(self):
        storage.log_trade("M1", "yes", 5, 50, "paper", None)
        tid2 = storage.log_trade("M2", "yes", 5, 50, "paper", None)
        storage.settle_trade(tid2, won=True, pnl_cents=250)
        self.assertEqual(storage.get_open_position_count_main(), 1)


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


class TestCategoryBreakdownForCrossCategoryStrategies(unittest.TestCase):
    """Some strategies (depth_imbalance, confirmed_signal) have no fixed
    category_filter and trade both Rain and Temperature depending on
    whichever ticker triggered them. Category classification happens
    PER-TRADE via that trade's own real measure value, not per-strategy —
    this confirms a single cross-category strategy's trades correctly
    split across both categories rather than getting lumped into one."""

    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")

    def test_a_single_strategys_trades_split_correctly_by_real_measure(self):
        tid_rain = storage.log_shadow_trade("depth_imbalance", "T1", "yes", 10, 40,
                                              measure="precipitation_daily")
        storage.settle_shadow_trade(tid_rain, won=True, pnl_cents=600)
        tid_temp = storage.log_shadow_trade("depth_imbalance", "T2", "yes", 10, 40,
                                              measure="temperature_high")
        storage.settle_shadow_trade(tid_temp, won=False, pnl_cents=-400)

        breakdown = storage.get_shadow_summary_by_category()
        rain_strategies = [s["strategy"] for s in breakdown["Rain"]["strategies"]]
        temp_strategies = [s["strategy"] for s in breakdown["Temperature"]["strategies"]]
        self.assertIn("depth_imbalance", rain_strategies)
        self.assertIn("depth_imbalance", temp_strategies)

        rain_row = next(s for s in breakdown["Rain"]["strategies"] if s["strategy"] == "depth_imbalance")
        temp_row = next(s for s in breakdown["Temperature"]["strategies"] if s["strategy"] == "depth_imbalance")
        self.assertEqual(rain_row["settled"], 1)
        self.assertEqual(temp_row["settled"], 1)


class TestHasOpenPositionForEvent(unittest.TestCase):
    """Foundation for the favorites_baseline same-event fix — see
    log_shadow_trade's event_ticker column docstring for the confirmed
    bug this exists to prevent."""

    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")

    def test_detects_existing_open_exposure(self):
        storage.log_shadow_trade("favorites_baseline", "T1", "yes", 10, 98,
                                   event_ticker="KXHIGHTSAN-26SEP11")
        self.assertTrue(storage.has_open_position_for_event("favorites_baseline", "KXHIGHTSAN-26SEP11"))

    def test_is_scoped_per_strategy(self):
        storage.log_shadow_trade("favorites_baseline", "T1", "yes", 10, 98,
                                   event_ticker="KXHIGHTSAN-26SEP11")
        self.assertFalse(storage.has_open_position_for_event("longshot", "KXHIGHTSAN-26SEP11"))

    def test_is_scoped_per_event(self):
        storage.log_shadow_trade("favorites_baseline", "T1", "yes", 10, 98,
                                   event_ticker="KXHIGHTSAN-26SEP11")
        self.assertFalse(storage.has_open_position_for_event("favorites_baseline", "KXHIGHTSAN-26SEP12"))

    def test_a_settled_position_no_longer_counts(self):
        tid = storage.log_shadow_trade("favorites_baseline", "T1", "yes", 10, 98,
                                         event_ticker="KXHIGHTSAN-26SEP11")
        storage.settle_shadow_trade(tid, won=True, pnl_cents=200)
        self.assertFalse(storage.has_open_position_for_event("favorites_baseline", "KXHIGHTSAN-26SEP11"))

    def test_missing_event_ticker_fails_open_without_crashing(self):
        self.assertFalse(storage.has_open_position_for_event("favorites_baseline", ""))
        self.assertFalse(storage.has_open_position_for_event("favorites_baseline", None))


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


class TestWinRateByEdgeBucket(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")

    def test_computes_edge_correctly_for_yes_and_no_sides(self):
        # yes: model_prob=0.7, price=40c -> edge = 70-40 = 30c
        tid1 = storage.log_shadow_trade("calibrated_balanced", "T1", "yes", 10, 40, model_probability=0.7)
        storage.settle_shadow_trade(tid1, won=True, pnl_cents=600)
        # no: model_prob=0.3 (yes-side prob), price=40c -> edge = (1-0.3)*100-40 = 30c
        tid2 = storage.log_shadow_trade("calibrated_balanced", "T2", "no", 10, 40, model_probability=0.3)
        storage.settle_shadow_trade(tid2, won=False, pnl_cents=-400)

        buckets = storage.get_win_rate_by_edge_bucket()
        big = next(b for b in buckets if b["edge_bucket"] == "20-+c")
        self.assertEqual(big["trades"], 2)
        self.assertEqual(big["win_rate"], 0.5)

    def test_excludes_both_side_arbitrage_trades(self):
        tid = storage.log_shadow_trade("arbitrage", "T3", "both", 5, 90)
        storage.settle_shadow_trade(tid, won=True, pnl_cents=50)
        buckets = storage.get_win_rate_by_edge_bucket()
        total = sum(b["trades"] for b in buckets)
        self.assertEqual(total, 0, "arbitrage has no real probability estimate to bucket by edge")

    def test_excludes_trades_with_no_model_probability(self):
        tid = storage.log_shadow_trade("rain_always_trade", "T4", "yes", 5, 40, model_probability=None)
        storage.settle_shadow_trade(tid, won=True, pnl_cents=300)
        buckets = storage.get_win_rate_by_edge_bucket()
        total = sum(b["trades"] for b in buckets)
        self.assertEqual(total, 0)

    def test_empty_bucket_shows_none_win_rate_not_a_crash(self):
        buckets = storage.get_win_rate_by_edge_bucket()
        for b in buckets:
            if b["trades"] == 0:
                self.assertIsNone(b["win_rate"])


class TestWinRateByConfidence(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")

    def test_groups_by_confidence_correctly(self):
        tid1 = storage.log_shadow_trade("calibrated_balanced", "H1", "yes", 10, 40, confidence="high")
        storage.settle_shadow_trade(tid1, won=True, pnl_cents=600)
        tid2 = storage.log_shadow_trade("calibrated_balanced", "M1", "yes", 10, 40, confidence="medium")
        storage.settle_shadow_trade(tid2, won=False, pnl_cents=-400)

        breakdown = storage.get_win_rate_by_confidence()
        high = next(c for c in breakdown if c["confidence"] == "high")
        medium = next(c for c in breakdown if c["confidence"] == "medium")
        self.assertEqual(high["win_rate"], 1.0)
        self.assertEqual(medium["win_rate"], 0.0)

    def test_legacy_trades_with_no_confidence_show_as_unknown_not_dropped(self):
        tid = storage.log_shadow_trade("calibrated_balanced", "OLD1", "yes", 10, 40)  # no confidence
        storage.settle_shadow_trade(tid, won=True, pnl_cents=600)
        breakdown = storage.get_win_rate_by_confidence()
        unknown = next((c for c in breakdown if c["confidence"] == "(unknown)"), None)
        self.assertIsNotNone(unknown)
        self.assertEqual(unknown["trades"], 1)

    def test_reading_order_is_high_medium_low_unknown(self):
        for conf in ("medium", "high", "(unknown-placeholder)"):
            tid = storage.log_shadow_trade("calibrated_balanced", f"T-{conf}", "yes", 10, 40,
                                             confidence=conf if conf != "(unknown-placeholder)" else None)
            storage.settle_shadow_trade(tid, won=True, pnl_cents=600)
        breakdown = storage.get_win_rate_by_confidence()
        confidences = [c["confidence"] for c in breakdown]
        self.assertEqual(confidences.index("high"), 0)
        self.assertLess(confidences.index("medium"), confidences.index("(unknown)"))


class TestRecentStrategyPerformance(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")

    def test_aggregates_pnl_and_cost_correctly(self):
        for i in range(5):
            tid = storage.log_shadow_trade("calibrated_balanced", f"L{i}", "yes", 10, 40)
            storage.settle_shadow_trade(tid, won=False, pnl_cents=-400)
        for i in range(3):
            tid = storage.log_shadow_trade("calibrated_balanced", f"W{i}", "yes", 10, 40)
            storage.settle_shadow_trade(tid, won=True, pnl_cents=600)
        perf = storage.get_recent_strategy_performance("calibrated_balanced", lookback=20)
        self.assertEqual(perf["trades"], 8)
        self.assertEqual(perf["total_pnl_cents"], -200)
        self.assertEqual(perf["total_cost_cents"], 3200)
        self.assertAlmostEqual(perf["roi_pct"], (-200 / 3200) * 100, places=4)

    def test_lookback_limits_to_most_recent_trades(self):
        for i in range(10):
            tid = storage.log_shadow_trade("calibrated_balanced", f"T{i}", "yes", 10, 40)
            storage.settle_shadow_trade(tid, won=False, pnl_cents=-400)
        perf = storage.get_recent_strategy_performance("calibrated_balanced", lookback=3)
        self.assertEqual(perf["trades"], 3)

    def test_no_trades_returns_safe_defaults(self):
        perf = storage.get_recent_strategy_performance("nonexistent_strategy")
        self.assertEqual(perf["trades"], 0)
        self.assertIsNone(perf["roi_pct"])

    def test_open_trades_are_never_included(self):
        storage.log_shadow_trade("calibrated_balanced", "OPEN1", "yes", 10, 40)
        perf = storage.get_recent_strategy_performance("calibrated_balanced")
        self.assertEqual(perf["trades"], 0)


if __name__ == "__main__":
    unittest.main()
