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
from unittest.mock import patch

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


class TestMarketSnapshotsAndOutcomes(unittest.TestCase):
    """Foundation for retrospective backtesting, explicitly requested:
    collect data across every scanned market -- regardless of whether
    any strategy trades it -- then later analyze it to find where a
    profitable trade existed that current strategies missed."""

    def setUp(self):
        storage.init_db()
        _clear("market_snapshots")
        _clear("market_outcomes")

    def test_log_market_snapshot_stores_everything(self):
        storage.log_market_snapshot(
            "T1", event_ticker="EVENT1", station_code="KAUS", measure="temperature_high",
            yes_ask=40, yes_bid=38, no_ask=62, no_bid=60,
            observed_temp_f=88.0, forecast_temp_f=90.0, threshold_low_f=85.0, threshold_high_f=95.0,
            hours_until_close=2.0, model_probability_yes=0.7, close_time="2020-01-01T00:00:00Z",
        )
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT event_ticker, station_code, observed_temp_f, close_time "
                "FROM market_snapshots WHERE ticker='T1'"
            ).fetchone()
        self.assertEqual(row, ("EVENT1", "KAUS", 88.0, "2020-01-01T00:00:00Z"))

    def test_omitting_fields_leaves_them_null(self):
        storage.log_market_snapshot("T2")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT event_ticker, observed_temp_f FROM market_snapshots WHERE ticker='T2'").fetchone()
        self.assertEqual(row, (None, None))

    def test_past_close_time_with_no_outcome_is_eligible_for_backfill(self):
        storage.log_market_snapshot("T1", close_time="2020-01-01T00:00:00Z")
        pending = storage.get_tickers_needing_outcome_backfill()
        self.assertEqual([r["ticker"] for r in pending], ["T1"])

    def test_future_close_time_is_not_eligible_yet(self):
        storage.log_market_snapshot("T2", close_time="2099-01-01T00:00:00Z")
        pending = storage.get_tickers_needing_outcome_backfill()
        self.assertNotIn("T2", [r["ticker"] for r in pending])

    def test_missing_close_time_is_never_eligible(self):
        storage.log_market_snapshot("T3")
        pending = storage.get_tickers_needing_outcome_backfill()
        self.assertNotIn("T3", [r["ticker"] for r in pending])

    def test_recording_an_outcome_removes_it_from_backfill_list(self):
        storage.log_market_snapshot("T1", close_time="2020-01-01T00:00:00Z")
        storage.record_market_outcome("T1", "yes")
        pending = storage.get_tickers_needing_outcome_backfill()
        self.assertNotIn("T1", [r["ticker"] for r in pending])

    def test_recording_twice_does_not_overwrite_the_first_result(self):
        storage.record_market_outcome("T1", "yes")
        storage.record_market_outcome("T1", "no")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT result FROM market_outcomes WHERE ticker='T1'").fetchone()
        self.assertEqual(row[0], "yes")

    def test_limit_is_respected(self):
        for i in range(10):
            storage.log_market_snapshot(f"MANY-{i}", close_time="2020-01-01T00:00:00Z")
        pending = storage.get_tickers_needing_outcome_backfill(limit=3)
        self.assertEqual(len(pending), 3)


class TestClearOverride(unittest.TestCase):
    """The safety valve paired with advisor.auto_apply_pending_suggestions
    (see that function's docstring) — nothing requires a human to approve
    a suggestion before it takes effect anymore, but anything can be
    reverted to its hardcoded default with one call."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM strategy_overrides")
            conn.commit()

    def test_clears_an_existing_override(self):
        storage.set_override("swing", "exit_offset", 12)
        storage.clear_override("swing", "exit_offset")
        overrides = storage.get_overrides()
        self.assertNotIn("exit_offset", overrides.get("swing", {}))

    def test_clearing_a_nonexistent_override_is_a_safe_noop(self):
        storage.clear_override("longshot", "min_price")  # never set

    def test_clearing_one_param_leaves_others_for_the_same_strategy_intact(self):
        storage.set_override("swing", "exit_offset", 12)
        storage.set_override("swing", "entry_max", 35)
        storage.clear_override("swing", "exit_offset")
        overrides = storage.get_overrides()
        self.assertNotIn("exit_offset", overrides.get("swing", {}))
        self.assertEqual(overrides["swing"]["entry_max"], 35)


class TestNewWeatherAndFeeColumns(unittest.TestCase):
    """Explicitly requested: 'data is the absolute most important thing to
    log and store.' Covers the storage layer for observed_temp_f,
    forecast_temp_f, precip_pop_pct, observed_precip_mm, threshold_low_f/
    high_f, fee_cents_paid, and yes/no_bid_depth_total."""

    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")
        _clear("trades")

    def test_shadow_trade_stores_all_new_fields(self):
        tid = storage.log_shadow_trade(
            "swing", "T1", "yes", 10, 40,
            observed_temp_f=88.0, forecast_temp_f=90.0, precip_pop_pct=None,
            observed_precip_mm=None, threshold_low_f=85.0, threshold_high_f=95.0,
            fee_cents_paid=13, yes_bid_depth_total=500, no_bid_depth_total=120,
        )
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT observed_temp_f, forecast_temp_f, precip_pop_pct, observed_precip_mm, "
                "threshold_low_f, threshold_high_f, fee_cents_paid, yes_bid_depth_total, "
                "no_bid_depth_total FROM shadow_trades WHERE id=?", (tid,)
            ).fetchone()
        self.assertEqual(row, (88.0, 90.0, None, None, 85.0, 95.0, 13, 500, 120))

    def test_main_trades_table_stores_weather_and_fee_fields(self):
        tid = storage.log_trade("T2", "yes", 10, 40, "paper", None,
                                  observed_temp_f=88.0, forecast_temp_f=90.0,
                                  threshold_low_f=85.0, threshold_high_f=95.0, fee_cents_paid=13)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT observed_temp_f, forecast_temp_f, threshold_low_f, threshold_high_f, "
                "fee_cents_paid FROM trades WHERE id=?", (tid,)
            ).fetchone()
        self.assertEqual(row, (88.0, 90.0, 85.0, 95.0, 13))

    def test_omitting_new_fields_leaves_them_null(self):
        tid = storage.log_shadow_trade("swing", "T3", "yes", 10, 40)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT observed_temp_f, fee_cents_paid, yes_bid_depth_total "
                "FROM shadow_trades WHERE id=?", (tid,)
            ).fetchone()
        self.assertEqual(row, (None, None, None))


class TestBotVersionColumn(unittest.TestCase):
    """CONFIRMED REAL MOTIVATION: a loss-analysis review flagged a
    favorites_baseline failure that looked identical to an already-fixed
    bug, with no way to tell from the trade data alone whether the fix
    was live yet when the trade happened."""

    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")
        _clear("trades")

    def test_shadow_trade_stores_bot_version(self):
        tid = storage.log_shadow_trade("swing", "T1", "yes", 10, 40, bot_version="abc1234")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT bot_version FROM shadow_trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], "abc1234")

    def test_main_trades_table_stores_bot_version(self):
        tid = storage.log_trade("T2", "yes", 10, 40, "paper", None, bot_version="abc1234")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT bot_version FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], "abc1234")

    def test_omitting_bot_version_leaves_it_null(self):
        tid = storage.log_shadow_trade("swing", "T3", "yes", 10, 40)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT bot_version FROM shadow_trades WHERE id=?", (tid,)).fetchone()
        self.assertIsNone(row[0])


class TestNewAnalysisColumnsSchema(unittest.TestCase):
    """Direct migration-safety test for this session's data-extraction
    push — confirms all six new columns exist after init_db() and that
    log_shadow_trade correctly stores and retrieves each one."""

    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")

    def test_all_new_columns_exist_after_init_db(self):
        with storage.get_conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(shadow_trades)")}
        for c in ["market_implied_probability", "raw_model_probability",
                  "hours_until_close_at_decision", "performance_dampening_multiplier",
                  "calibration_dampening_multiplier", "concentration_dampening_multiplier"]:
            self.assertIn(c, cols)

    def test_log_shadow_trade_stores_and_retrieves_all_new_fields(self):
        tid = storage.log_shadow_trade(
            "calibrated_balanced", "T1", "yes", 10, 40,
            market_implied_probability=0.4, raw_model_probability=0.4,
            hours_until_close_at_decision=2.5,
            performance_dampening_multiplier=0.5,
            calibration_dampening_multiplier=1.0,
            concentration_dampening_multiplier=0.5,
        )
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT market_implied_probability, raw_model_probability, hours_until_close_at_decision, "
                "performance_dampening_multiplier, calibration_dampening_multiplier, "
                "concentration_dampening_multiplier FROM shadow_trades WHERE id=?", (tid,)
            ).fetchone()
        self.assertEqual(row, (0.4, 0.4, 2.5, 0.5, 1.0, 0.5))

    def test_omitting_the_new_fields_leaves_them_null_not_an_error(self):
        tid = storage.log_shadow_trade("favorites_baseline", "T2", "yes", 10, 95)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT market_implied_probability, raw_model_probability "
                "FROM shadow_trades WHERE id=?", (tid,)
            ).fetchone()
        self.assertEqual(row, (None, None))


class TestCountOpenPositionsForStrategyAndEvent(unittest.TestCase):
    """Foundation for shadow.py's self-concentration dampening — the
    complement to count_distinct_strategies_exposed_to_event, which
    deliberately excludes a strategy's own prior positions. This counts
    the opposite risk: a single strategy repeatedly betting on the same
    underlying outcome through different thresholds within one event."""

    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")

    def test_reproduces_the_exact_las_vegas_scenario(self):
        storage.log_shadow_trade("temp_forecast_momentum", "T1", "no", 10, 40, event_ticker="EVENT1")
        self.assertEqual(storage.count_open_positions_for_strategy_and_event("temp_forecast_momentum", "EVENT1"), 1)
        storage.log_shadow_trade("temp_forecast_momentum", "T2", "no", 10, 40, event_ticker="EVENT1")
        self.assertEqual(storage.count_open_positions_for_strategy_and_event("temp_forecast_momentum", "EVENT1"), 2)
        storage.log_shadow_trade("temp_forecast_momentum", "T3", "no", 10, 40, event_ticker="EVENT1")
        self.assertEqual(storage.count_open_positions_for_strategy_and_event("temp_forecast_momentum", "EVENT1"), 3)

    def test_a_different_strategy_on_the_same_event_is_not_counted(self):
        storage.log_shadow_trade("temp_forecast_momentum", "T1", "no", 10, 40, event_ticker="EVENT1")
        self.assertEqual(storage.count_open_positions_for_strategy_and_event("calibrated_balanced", "EVENT1"), 0)

    def test_a_settled_position_no_longer_counts(self):
        tid = storage.log_shadow_trade("temp_forecast_momentum", "T1", "no", 10, 40, event_ticker="EVENT1")
        storage.log_shadow_trade("temp_forecast_momentum", "T2", "no", 10, 40, event_ticker="EVENT1")
        storage.settle_shadow_trade(tid, won=False, pnl_cents=-400)
        self.assertEqual(storage.count_open_positions_for_strategy_and_event("temp_forecast_momentum", "EVENT1"), 1)

    def test_missing_event_ticker_returns_zero_without_crashing(self):
        self.assertEqual(storage.count_open_positions_for_strategy_and_event("swing", None), 0)
        self.assertEqual(storage.count_open_positions_for_strategy_and_event("swing", ""), 0)


class TestCountDistinctStrategiesExposedToEvent(unittest.TestCase):
    """Foundation for shadow.py's concentration dampening — see its
    docstring for the confirmed real-world motivation (20+ trades across
    nearly every strategy all buying the same losing side of the same
    underlying market, because they all share the same weather model)."""

    def setUp(self):
        storage.init_db()
        _clear("shadow_trades")

    def test_zero_when_nothing_is_open(self):
        self.assertEqual(storage.count_distinct_strategies_exposed_to_event("EVENT1"), 0)

    def test_counts_distinct_strategies_not_trades(self):
        storage.log_shadow_trade("calibrated_conservative", "T1", "yes", 10, 40, event_ticker="EVENT1")
        storage.log_shadow_trade("calibrated_conservative", "T2", "yes", 10, 40, event_ticker="EVENT1")
        self.assertEqual(storage.count_distinct_strategies_exposed_to_event("EVENT1"), 1)

    def test_multiple_different_strategies_increment_the_count(self):
        storage.log_shadow_trade("calibrated_conservative", "T1", "yes", 10, 40, event_ticker="EVENT1")
        storage.log_shadow_trade("longshot", "T2", "yes", 10, 5, event_ticker="EVENT1")
        storage.log_shadow_trade("swing", "T3", "yes", 10, 30, event_ticker="EVENT1")
        self.assertEqual(storage.count_distinct_strategies_exposed_to_event("EVENT1"), 3)

    def test_exclude_strategy_omits_the_current_strategy(self):
        storage.log_shadow_trade("calibrated_conservative", "T1", "yes", 10, 40, event_ticker="EVENT1")
        storage.log_shadow_trade("longshot", "T2", "yes", 10, 5, event_ticker="EVENT1")
        self.assertEqual(
            storage.count_distinct_strategies_exposed_to_event("EVENT1", exclude_strategy="calibrated_conservative"),
            1,
        )

    def test_a_different_event_is_unaffected(self):
        storage.log_shadow_trade("calibrated_conservative", "T1", "yes", 10, 40, event_ticker="EVENT1")
        self.assertEqual(storage.count_distinct_strategies_exposed_to_event("EVENT2"), 0)

    def test_a_settled_position_no_longer_counts(self):
        tid = storage.log_shadow_trade("calibrated_conservative", "T1", "yes", 10, 40, event_ticker="EVENT1")
        storage.settle_shadow_trade(tid, won=True, pnl_cents=50)
        self.assertEqual(storage.count_distinct_strategies_exposed_to_event("EVENT1"), 0)

    def test_missing_event_ticker_returns_zero_without_crashing(self):
        self.assertEqual(storage.count_distinct_strategies_exposed_to_event(None), 0)
        self.assertEqual(storage.count_distinct_strategies_exposed_to_event(""), 0)


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


class TestHistoricalMarketSettlementSourceFields(unittest.TestCase):
    """settlement_source, threshold_description, and confidence were
    extracted by rules_extractor all along but discarded rather than
    stored -- added per explicit request, going forward only (not
    backfilled onto already-processed markets)."""

    def setUp(self):
        storage.init_db()

    def test_all_three_new_fields_save_and_load_correctly(self):
        storage.save_historical_market(
            "T1", station_code="CLINYC", measure="temperature_high",
            threshold_low_f=96.0001, settlement_source="NWS",
            threshold_description="strictly greater than 96F", confidence="high",
        )
        market = storage.get_historical_market("T1")
        self.assertEqual(market["settlement_source"], "NWS")
        self.assertEqual(market["threshold_description"], "strictly greater than 96F")
        self.assertEqual(market["confidence"], "high")

    def test_omitting_the_new_fields_defaults_to_none_not_an_error(self):
        storage.save_historical_market("T2", station_code="CLIAUS", measure="temperature_high")
        market = storage.get_historical_market("T2")
        self.assertIsNone(market["settlement_source"])
        self.assertIsNone(market["threshold_description"])
        self.assertIsNone(market["confidence"])


class TestHistoricalPriceAndWeatherUniqueness(unittest.TestCase):
    """CONFIRMED NEEDED: unlike historical_markets (a true PRIMARY KEY on
    ticker since it was created), historical_price_points and
    historical_weather_points only ever had id INTEGER PRIMARY KEY
    AUTOINCREMENT as their key -- the (ticker, ts) / (station_code, ts)
    index existed for query speed only, never uniqueness. Reprocessing
    the same ticker before the application-level "already stored, skip"
    guard existed (which happened across several restarts on the live
    droplet tonight) could insert duplicate rows with no error at all."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM historical_price_points")
            conn.execute("DELETE FROM historical_weather_points")
            conn.commit()

    def test_price_points_unique_index_actually_exists(self):
        with storage.get_conn() as conn:
            idx = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_historical_price_points_ticker_ts'"
            ).fetchone()
        self.assertIn("UNIQUE", idx[0].upper())

    def test_weather_points_unique_index_actually_exists(self):
        with storage.get_conn() as conn:
            idx = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_historical_weather_points_station_ts'"
            ).fetchone()
        self.assertIn("UNIQUE", idx[0].upper())

    def test_reinserting_the_same_price_point_is_silently_ignored(self):
        storage.save_historical_price_points("T1", [(1000, 50, 10)])
        storage.save_historical_price_points("T1", [(1000, 999, 999)])  # same key, different values
        with storage.get_conn() as conn:
            rows = conn.execute(
                "SELECT yes_price_cents, volume FROM historical_price_points WHERE ticker='T1' AND ts=1000"
            ).fetchall()
        self.assertEqual(len(rows), 1, "should never have more than one row for the same (ticker, ts)")
        self.assertEqual(rows[0], (50, 10), "the FIRST insert should win; a duplicate insert is ignored, not applied")

    def test_reinserting_the_same_weather_point_is_silently_ignored(self):
        storage.save_historical_weather_points("KAUS", [(1000, 85.0, 10.0, 84.0, 0.0)])
        storage.save_historical_weather_points("KAUS", [(1000, 999.0, 999.0, 999.0, 999.0)])
        with storage.get_conn() as conn:
            rows = conn.execute(
                "SELECT forecast_temp_f FROM historical_weather_points WHERE station_code='KAUS' AND ts=1000"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 85.0)

    def test_preexisting_duplicates_do_not_crash_init_db(self):
        """The live trading bot calls init_db() on every single startup
        -- this migration must NEVER be able to break that, even on a
        database that already has real duplicate rows from before this
        protection existed. Uses its own dedicated database file rather
        than the shared test fixture, since the shared one already has
        the UNIQUE index from earlier tests in this class -- inserting a
        duplicate directly into it would correctly fail at the insert
        itself, never reaching the scenario this test needs."""
        import os
        import sqlite3
        import tempfile
        import dataclasses
        import storage as storage_module

        tmp_path = tempfile.mktemp(suffix=".db")
        try:
            conn = sqlite3.connect(tmp_path)
            conn.executescript(storage_module.SCHEMA)
            conn.execute("INSERT INTO historical_price_points (ticker, ts, yes_price_cents, volume) "
                          "VALUES ('DUPE', 5000, 10, 1)")
            conn.execute("INSERT INTO historical_price_points (ticker, ts, yes_price_cents, volume) "
                          "VALUES ('DUPE', 5000, 10, 1)")
            conn.commit()
            conn.close()

            patched_settings = dataclasses.replace(storage_module.SETTINGS, db_path=tmp_path)
            with patch.object(storage_module, "SETTINGS", patched_settings):
                storage_module.init_db()  # must not raise
                with storage_module.get_conn() as conn2:
                    count = conn2.execute(
                        "SELECT COUNT(*) FROM historical_price_points WHERE ticker='DUPE' AND ts=5000"
                    ).fetchone()[0]
            self.assertEqual(count, 2, "existing duplicate data must be left alone, not silently deleted")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


if __name__ == "__main__":
    unittest.main()
