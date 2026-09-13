"""
Covers shadow.py's per-strategy filtering and the two "always_trade"
fixes: it originally only checked yes_ask, missing markets that had a
real, tradeable price on the no side only (a resting bid with no
matching ask); and it originally required a fixed max_slippage_cents,
which conflicts with the "just prove the pipeline fires" purpose once
combined with real depth-aware sizing.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402
import shadow  # noqa: E402
import calibration  # noqa: E402
from strategy import TradeSignal  # noqa: E402


def make_signal(ticker, side="yes", edge=30, prob=0.7):
    return TradeSignal(ticker=ticker, side=side, model_probability=prob,
                        model_probability_yes=prob, market_implied_probability=0.4,
                        edge_cents=edge, rationale="test")


class TestCategoryFilter(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_rain_scoped_strategy_ignores_temperature_markets(self):
        sig = make_signal("KXHIGH-1")
        shadow.evaluate_and_log("KXHIGH-1", sig, yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_high")
        with storage.get_conn() as conn:
            leak = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='rain_calibrated_balanced' AND ticker='KXHIGH-1'"
            ).fetchone()
        self.assertIsNone(leak)

    def test_temp_scoped_strategy_ignores_rain_markets(self):
        sig = make_signal("KXRAIN-1")
        shadow.evaluate_and_log("KXRAIN-1", sig, yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            leak = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='temp_calibrated_balanced' AND ticker='KXRAIN-1'"
            ).fetchone()
        self.assertIsNone(leak)

    def test_blended_strategy_trades_both_categories(self):
        shadow.evaluate_and_log("KXRAIN-2", make_signal("KXRAIN-2"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        shadow.evaluate_and_log("KXHIGH-2", make_signal("KXHIGH-2"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_high")
        with storage.get_conn() as conn:
            rain_row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='KXRAIN-2'"
            ).fetchone()
            temp_row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='KXHIGH-2'"
            ).fetchone()
        self.assertIsNotNone(rain_row)
        self.assertIsNotNone(temp_row)


class TestAlwaysTradeFallbackPricing(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_falls_back_to_no_side_when_only_that_price_is_real(self):
        """THE regression: this used to only check yes_ask and silently
        skip a market that had a real, tradeable price on the no side."""
        shadow.evaluate_and_log("KXRAIN-BIDONLY", None, yes_ask=None, no_ask=70,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT side, price_cents FROM shadow_trades "
                "WHERE strategy='rain_always_trade' AND ticker='KXRAIN-BIDONLY'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "no")
        self.assertEqual(row[1], 70)

    def test_prefers_yes_side_when_both_are_available(self):
        shadow.evaluate_and_log("KXRAIN-BOTH", None, yes_ask=40, no_ask=60,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT side, price_cents FROM shadow_trades "
                "WHERE strategy='rain_always_trade' AND ticker='KXRAIN-BOTH'"
            ).fetchone()
        self.assertEqual((row[0], row[1]), ("yes", 40))

    def test_never_fabricates_a_price_on_a_truly_empty_book(self):
        shadow.evaluate_and_log("KXRAIN-EMPTY", None, yes_ask=None, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM shadow_trades WHERE ticker='KXRAIN-EMPTY'").fetchone()
        self.assertIsNone(row)

    def test_survives_a_losing_streak_without_being_kill_switched(self):
        for _ in range(20):
            rm = shadow.get_engines()["rain_always_trade"]
            rm.record_fill(cost_cents=50)
            rm.record_settlement(-50)
        rm = shadow.get_engines()["rain_always_trade"]
        self.assertFalse(rm.state.is_kill_switch_tripped(rm.preset.max_daily_loss_pct))
        shadow.evaluate_and_log("KXRAIN-AFTERLOSS", None, yes_ask=60, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM shadow_trades WHERE ticker='KXRAIN-AFTERLOSS'").fetchone()
        self.assertIsNotNone(row, "should still trade after a losing streak, not get stuck kill-switched")


class TestArbitrageNotABetExemption(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_arbitrage_scales_beyond_the_bet_oriented_contract_cap(self):
        # Needs a bankroll where the DOLLAR-based cap alone would exceed
        # 25, or this test can't actually distinguish "exempt from the
        # 25-contract cap" from "just capped by a small dollar limit that
        # happens to be under 25 anyway" (a $500 bankroll at conservative's
        # 1.5% gives only ~8 contracts at 45c, which doesn't exercise the
        # exemption at all).
        rm = shadow.get_engines()["arbitrage"]
        rm.state.bankroll_cents = 5_000_000  # $50,000 -- dollar cap alone now far exceeds 25
        no_bids = [(55, 200)]
        yes_bids = [(55, 200)]
        shadow.evaluate_and_log("KXRAIN-ARBBIG", None, yes_ask=45, no_ask=45,
                                 station_code="KAUS", measure="precipitation_daily",
                                 yes_bids=yes_bids, no_bids=no_bids)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT count FROM shadow_trades WHERE strategy='arbitrage' AND ticker='KXRAIN-ARBBIG'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(row[0], 25, "arbitrage must not be limited by the 25-contract bet-oriented cap")

    def test_directional_strategy_still_respects_the_contract_cap(self):
        shadow.evaluate_and_log("KXRAIN-DIRBIG", make_signal("KXRAIN-DIRBIG"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily",
                                 yes_bids=[(30, 200)], no_bids=[(60, 200)])
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT count FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='KXRAIN-DIRBIG'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertLessEqual(row[0], 25, "a directional bet MUST still respect the fixed contract cap")


class TestRationaleCapture(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_calibrated_strategy_captures_the_real_signal_rationale(self):
        """Without this, a post-mortem analysis of a losing trade only has
        raw numbers to work with -- the actual reasoning generated at
        decision time (forecast data used, calibration note) is what
        makes root-cause analysis useful."""
        sig = make_signal("KXRAIN-RAT1")
        sig.rationale = "forecast max POP over next periods: 65%; calibration: n=25, bias=+0.03"
        shadow.evaluate_and_log("KXRAIN-RAT1", sig, yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT rationale FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='KXRAIN-RAT1'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("forecast max POP", row[0])

    def test_always_trade_captures_its_own_smoke_test_rationale(self):
        shadow.evaluate_and_log("KXRAIN-RAT2", None, yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT rationale FROM shadow_trades WHERE strategy='rain_always_trade' AND ticker='KXRAIN-RAT2'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("pipeline smoke test", row[0])


class TestPerformanceDampening(unittest.TestCase):
    """Covers the automatic, mechanical cold-streak circuit breaker — the
    one piece of "learning" in this system that's genuinely fully
    automatic (no human click needed), which is only safe because it can
    provably only ever REDUCE position size, never increase it above the
    normal tier baseline."""

    def test_pure_function_never_returns_above_1_across_a_wide_sweep(self):
        """THE core safety property, checked directly against the pure
        decision function (not just via integration)."""
        for trades in range(0, 50):
            for roi in (-1000, -100, -50, -20, -15, -14, -5, 0, 5, 50, 1000):
                result = shadow.performance_dampening_multiplier({"trades": trades, "roi_pct": roi})
                self.assertLessEqual(result, 1.0)

    def test_no_dampening_below_the_minimum_trade_count(self):
        self.assertEqual(
            shadow.performance_dampening_multiplier({"trades": 5, "roi_pct": -50.0}), 1.0)

    def test_no_dampening_with_healthy_performance(self):
        self.assertEqual(
            shadow.performance_dampening_multiplier({"trades": 20, "roi_pct": 10.0}), 1.0)

    def test_dampens_on_a_genuine_cold_streak(self):
        self.assertEqual(
            shadow.performance_dampening_multiplier({"trades": 20, "roi_pct": -20.0}),
            shadow.COLD_STREAK_DAMPENING_MULTIPLIER)

    def test_boundary_is_inclusive(self):
        self.assertEqual(
            shadow.performance_dampening_multiplier(
                {"trades": 20, "roi_pct": shadow.COLD_STREAK_ROI_THRESHOLD_PCT}),
            shadow.COLD_STREAK_DAMPENING_MULTIPLIER)

    def test_just_above_threshold_is_not_dampened(self):
        self.assertEqual(
            shadow.performance_dampening_multiplier(
                {"trades": 20, "roi_pct": shadow.COLD_STREAK_ROI_THRESHOLD_PCT + 0.1}),
            1.0)


class TestPerformanceDampeningIntegration(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_a_real_cold_streak_automatically_reduces_position_size(self):
        for i in range(20):
            tid = storage.log_shadow_trade("calibrated_conservative", f"COLD{i}", "yes", 10, 40)
            storage.settle_shadow_trade(tid, won=False, pnl_cents=-400)
        for i in range(20):
            tid = storage.log_shadow_trade("calibrated_aggressive", f"HOT{i}", "yes", 10, 40)
            storage.settle_shadow_trade(tid, won=True, pnl_cents=600)

        # A real 20-trade cold streak would span days or weeks, not one
        # instant — by the time a NEW trade is being evaluated, the DAILY
        # kill switch (a separate, pre-existing safety mechanism) would
        # have long since reset even though the ROLLING 20-trade window
        # this feature checks still remembers the streak. Compressing all
        # 20 into one test run would otherwise trip that unrelated daily
        # limit first and reject the trade before dampening ever gets a
        # chance to apply — simulating that time has passed since,
        # matching what a real multi-day cold streak actually looks like.
        shadow.get_engines()["calibrated_conservative"].state.realized_pnl_today_cents = 0

        no_bids = [(60, 300)]
        yes_bids = [(30, 300)]
        shadow.evaluate_and_log("KXRAIN-COLDTEST", make_signal("KXRAIN-COLDTEST"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily",
                                 yes_bids=yes_bids, no_bids=no_bids)

        with storage.get_conn() as conn:
            cold = conn.execute(
                "SELECT count FROM shadow_trades WHERE strategy='calibrated_conservative' AND ticker='KXRAIN-COLDTEST'"
            ).fetchone()
            hot = conn.execute(
                "SELECT count FROM shadow_trades WHERE strategy='calibrated_aggressive' AND ticker='KXRAIN-COLDTEST'"
            ).fetchone()

        self.assertIsNotNone(cold)
        self.assertIsNotNone(hot)
        self.assertLess(cold[0], hot[0])

    def test_dampening_never_fully_halts_a_strategy(self):
        for i in range(20):
            tid = storage.log_shadow_trade("calibrated_conservative", f"COLD{i}", "yes", 10, 40)
            storage.settle_shadow_trade(tid, won=False, pnl_cents=-400)
        # Same reasoning as above -- simulate that time has passed since
        # this streak, so only the rolling-window check (not the daily
        # kill switch) is in play here.
        shadow.get_engines()["calibrated_conservative"].state.realized_pnl_today_cents = 0

        shadow.evaluate_and_log("KXRAIN-STILLTRADES", make_signal("KXRAIN-STILLTRADES"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT count FROM shadow_trades WHERE strategy='calibrated_conservative' AND ticker='KXRAIN-STILLTRADES'"
            ).fetchone()
        self.assertIsNotNone(row, "a dampened strategy should still trade, just smaller")
        self.assertGreaterEqual(row[0], 1)


class TestOpenPositionCountSurvivesRestart(unittest.TestCase):
    """The real, confirmed production bug: get_engines() never seeded
    open_positions_count from real data, so max_open_positions never
    meaningfully bound on a bot restarted as often as this one has been —
    confirmed directly against production data showing 350-440+ "Active"
    positions per strategy, far beyond any reasonable cap."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_a_strategy_with_many_real_open_positions_correctly_blocks_new_ones_after_restart(self):
        for i in range(50):
            storage.log_shadow_trade("calibrated_conservative", f"POS{i}", "yes", 10, 40)

        # Simulate a fresh process restart.
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

        rm = shadow.get_engines()["calibrated_conservative"]
        self.assertEqual(rm.state.open_positions_count, 50)
        approved, reason = rm.approve_trade(40, 30)
        self.assertFalse(approved)
        self.assertIn("max open positions", reason)


class TestSettlementWindow(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_is_registered(self):
        self.assertIn("temp_settlement_window", shadow.ACTIVE_STRATEGIES)

    def test_fires_within_the_window_on_temperature_high(self):
        shadow.evaluate_and_log("T1", make_signal("T1"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_high", hours_until_close=1.0)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='temp_settlement_window' AND ticker='T1'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_does_not_fire_far_from_close(self):
        shadow.evaluate_and_log("T2", make_signal("T2"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_high", hours_until_close=10.0)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='temp_settlement_window' AND ticker='T2'"
            ).fetchone()
        self.assertIsNone(row)

    def test_does_not_fire_on_temperature_low_even_within_the_window(self):
        """THE important scoping test — a daily low settles overnight, so
        being close to close by clock time is not the same as being close
        to a trustworthy reading for it."""
        shadow.evaluate_and_log("T3", make_signal("T3"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_low", hours_until_close=0.5)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='temp_settlement_window' AND ticker='T3'"
            ).fetchone()
        self.assertIsNone(row)

    def test_does_not_fire_on_rain_markets(self):
        shadow.evaluate_and_log("T4", make_signal("T4"), yes_ask=40, no_ask=None,
                                 station_code="KHOU", measure="precipitation_daily", hours_until_close=0.5)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='temp_settlement_window' AND ticker='T4'"
            ).fetchone()
        self.assertIsNone(row)

    def test_does_not_fire_with_no_time_data(self):
        shadow.evaluate_and_log("T5", make_signal("T5"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_high", hours_until_close=None)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='temp_settlement_window' AND ticker='T5'"
            ).fetchone()
        self.assertIsNone(row)

    def test_other_calibrated_strategies_are_unaffected_by_this_gate(self):
        shadow.evaluate_and_log("T6", make_signal("T6"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_high", hours_until_close=10.0)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='temp_calibrated_balanced' AND ticker='T6'"
            ).fetchone()
        self.assertIsNotNone(row)


class TestRainSettlementWindow(unittest.TestCase):
    """The rain analog of temp_settlement_window — needed zero new
    dispatch logic, since bot.py already passes hours_until_close
    unconditionally for every measure, and the settlement_window kind
    itself is measure-agnostic; the scoping happens entirely through
    measure_filter in the strategy config."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_is_registered(self):
        self.assertIn("rain_settlement_window", shadow.ACTIVE_STRATEGIES)

    def test_fires_within_the_window_on_precipitation_daily(self):
        shadow.evaluate_and_log("T1", make_signal("T1"), yes_ask=40, no_ask=None,
                                 station_code="KHOU", measure="precipitation_daily", hours_until_close=1.0)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='rain_settlement_window' AND ticker='T1'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_does_not_fire_far_from_close(self):
        shadow.evaluate_and_log("T2", make_signal("T2"), yes_ask=40, no_ask=None,
                                 station_code="KHOU", measure="precipitation_daily", hours_until_close=10.0)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='rain_settlement_window' AND ticker='T2'"
            ).fetchone()
        self.assertIsNone(row)

    def test_does_not_fire_on_temperature_markets(self):
        shadow.evaluate_and_log("T3", make_signal("T3"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_high", hours_until_close=1.0)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='rain_settlement_window' AND ticker='T3'"
            ).fetchone()
        self.assertIsNone(row)

    def test_does_not_fire_on_precipitation_monthly(self):
        """A monthly market's hours_until_close reflects the end of the
        MONTH, not a same-day window -- must never be treated the same
        as precipitation_daily."""
        shadow.evaluate_and_log("T4", make_signal("T4"), yes_ask=40, no_ask=None,
                                 station_code="KHOU", measure="precipitation_monthly", hours_until_close=1.0)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='rain_settlement_window' AND ticker='T4'"
            ).fetchone()
        self.assertIsNone(row)

    def test_temp_settlement_window_remains_correctly_scoped(self):
        """Confirms adding the rain variant didn't loosen the temperature
        variant's own measure_filter."""
        shadow.evaluate_and_log("T5", make_signal("T5"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="temperature_high", hours_until_close=1.0)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='temp_settlement_window' AND ticker='T5'"
            ).fetchone()
        self.assertIsNotNone(row)


class TestDepthImbalance(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_is_registered(self):
        self.assertIn("depth_imbalance", shadow.ACTIVE_STRATEGIES)

    def test_fires_on_a_strong_real_imbalance(self):
        # yes_ask passed directly is 45c, but real depth-aware sizing (the
        # same shared mechanism every directional strategy uses) re-derives
        # the actual fill price from no_bids' implied ask levels once real
        # book data is present — no_bids=[(50, 20)] implies a real YES ask
        # of (100-50)=50c, which is what the trade should actually price
        # at, not the flat top-of-book number passed in.
        shadow.evaluate_and_log("T1", None, yes_ask=45, no_ask=60,
                                 station_code="KAUS", measure="precipitation_daily",
                                 yes_bids=[(40, 100)], no_bids=[(50, 20)])
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT side, price_cents, rationale FROM shadow_trades WHERE strategy='depth_imbalance' AND ticker='T1'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "yes")
        self.assertEqual(row[1], 50)
        self.assertIn("depth imbalance", row[2])

    def test_does_not_fire_without_book_data(self):
        shadow.evaluate_and_log("T2", None, yes_ask=45, no_ask=60,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM shadow_trades WHERE strategy='depth_imbalance' AND ticker='T2'").fetchone()
        self.assertIsNone(row)

    def test_does_not_fire_on_a_balanced_book(self):
        shadow.evaluate_and_log("T3", None, yes_ask=45, no_ask=60,
                                 station_code="KAUS", measure="precipitation_daily",
                                 yes_bids=[(40, 50)], no_bids=[(50, 45)])
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM shadow_trades WHERE strategy='depth_imbalance' AND ticker='T3'").fetchone()
        self.assertIsNone(row)

    def test_works_across_both_categories_since_it_ignores_the_weather_model(self):
        shadow.evaluate_and_log("T4", None, yes_ask=45, no_ask=60,
                                 station_code="KAUS", measure="temperature_high",
                                 yes_bids=[(40, 100)], no_bids=[(50, 20)])
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM shadow_trades WHERE strategy='depth_imbalance' AND ticker='T4'").fetchone()
        self.assertIsNotNone(row)


class TestFavoritesSameEventLimit(unittest.TestCase):
    """CONFIRMED BUG this fixes, found via real loss analysis:
    favorites_baseline's rule (buy anything priced >=threshold) has no
    concept of market structure — it bought YES on four different bucket
    markets for the same underlying temperature event simultaneously, all
    at 98c, and lost all four (~4410c combined). Directly reproduces that
    exact scenario."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_reproduces_the_exact_confirmed_scenario(self):
        shadow.evaluate_and_log("KXHIGHTSAN-26SEP11-T84", None, yes_ask=98, no_ask=None,
                                 station_code="KSAN", measure="temperature_high",
                                 event_ticker="KXHIGHTSAN-26SEP11")
        for suffix in ["B84.5", "B86.5", "B88.5", "B90.5"]:
            shadow.evaluate_and_log(f"KXHIGHTSAN-26SEP11-{suffix}", None, yes_ask=98, no_ask=None,
                                     station_code="KSAN", measure="temperature_high",
                                     event_ticker="KXHIGHTSAN-26SEP11")
        with storage.get_conn() as conn:
            rows = conn.execute(
                "SELECT ticker FROM shadow_trades WHERE strategy='favorites_baseline'"
            ).fetchall()
        self.assertEqual(len(rows), 1, "must hold exactly one position on this event, not five")
        self.assertEqual(rows[0][0], "KXHIGHTSAN-26SEP11-T84", "the FIRST market seen should be the one that trades")

    def test_a_different_event_is_unaffected(self):
        shadow.evaluate_and_log("KXHIGHTSAN-26SEP11-T84", None, yes_ask=98, no_ask=None,
                                 station_code="KSAN", measure="temperature_high",
                                 event_ticker="KXHIGHTSAN-26SEP11")
        shadow.evaluate_and_log("KXHIGHTAUS-26SEP11-T85", None, yes_ask=97, no_ask=None,
                                 station_code="KAUS", measure="temperature_high",
                                 event_ticker="KXHIGHTAUS-26SEP11")
        with storage.get_conn() as conn:
            rows = conn.execute("SELECT ticker FROM shadow_trades WHERE strategy='favorites_baseline'").fetchall()
        self.assertEqual(len(rows), 2)

    def test_a_new_position_is_allowed_once_the_first_settles(self):
        shadow.evaluate_and_log("KXHIGHTSAN-26SEP11-T84", None, yes_ask=98, no_ask=None,
                                 station_code="KSAN", measure="temperature_high",
                                 event_ticker="KXHIGHTSAN-26SEP11")
        with storage.get_conn() as conn:
            tid = conn.execute("SELECT id FROM shadow_trades WHERE ticker='KXHIGHTSAN-26SEP11-T84'").fetchone()[0]
        storage.settle_shadow_trade(tid, won=True, pnl_cents=200)
        shadow.evaluate_and_log("KXHIGHTSAN-26SEP11-B92.5", None, yes_ask=96, no_ask=None,
                                 station_code="KSAN", measure="temperature_high",
                                 event_ticker="KXHIGHTSAN-26SEP11")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM shadow_trades WHERE ticker='KXHIGHTSAN-26SEP11-B92.5'").fetchone()
        self.assertIsNotNone(row)


class TestCalibrationAwareDampening(unittest.TestCase):
    """Direct reproduction of the confirmed real-world scenario: a
    strategy trading a station/measure with zero or low calibration
    samples automatically sizes smaller than the same trade at a
    well-established station — reducing (not eliminating) the damage
    when the shared weather model is wrong for an unproven case."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions", "calibration_stats")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_zero_calibration_trade_sizes_smaller_than_full_calibration_trade(self):
        sig = TradeSignal(ticker="T", side="yes", model_probability=0.85, model_probability_yes=0.85,
                           market_implied_probability=0.16, edge_cents=30, rationale="test")

        shadow.evaluate_and_log("KXRAIN-SEA", sig, yes_ask=6, no_ask=None,
                                 station_code="KSEA", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row_zero = conn.execute(
                "SELECT count FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='KXRAIN-SEA'"
            ).fetchone()

        for i in range(25):
            calibration.record_outcome("KHOU", "precipitation_daily", 0.85, i % 10 < 8)
        shadow.evaluate_and_log("KXRAIN-HOU", sig, yes_ask=6, no_ask=None,
                                 station_code="KHOU", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row_full = conn.execute(
                "SELECT count FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='KXRAIN-HOU'"
            ).fetchone()

        self.assertIsNotNone(row_zero)
        self.assertIsNotNone(row_full)
        self.assertLess(row_zero[0], row_full[0])

    def test_depth_imbalance_is_unaffected_since_it_ignores_the_weather_model(self):
        shadow.evaluate_and_log("T1", None, yes_ask=45, no_ask=60,
                                 station_code="KSEA", measure="precipitation_daily",
                                 yes_bids=[(40, 300)], no_bids=[(50, 20)])
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT count FROM shadow_trades WHERE strategy='depth_imbalance' AND ticker='T1'"
            ).fetchone()
        self.assertIsNotNone(row)


class TestConcentrationDampeningMultiplier(unittest.TestCase):
    """Pure function tests — see the module-level constants' docstring
    above for the confirmed real-world motivation."""

    def test_zero_others_gets_full_size(self):
        self.assertEqual(shadow.concentration_dampening_multiplier(0), 1.0)

    def test_one_or_two_others_gets_moderate_dampening(self):
        self.assertEqual(shadow.concentration_dampening_multiplier(1), 0.5)
        self.assertEqual(shadow.concentration_dampening_multiplier(2), 0.5)

    def test_three_or_more_gets_severe_dampening(self):
        self.assertEqual(shadow.concentration_dampening_multiplier(3), 0.25)
        self.assertEqual(shadow.concentration_dampening_multiplier(10), 0.25)

    def test_never_exceeds_1_across_a_wide_sweep(self):
        for count in range(0, 50):
            self.assertLessEqual(shadow.concentration_dampening_multiplier(count), 1.0)


class TestConcentrationDampeningIntegration(unittest.TestCase):
    """Direct reproduction of the confirmed real-world scenario: as more
    DIFFERENT strategies pile onto the same underlying event within one
    scan cycle, later ones size down relative to what they'd have traded
    with dampening disabled. Isolated via mocking, since evaluate_and_log
    always processes every matching strategy together for a given
    ticker — there's no way to construct a genuinely "only one strategy
    trades this event" scenario to compare against directly."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions", "calibration_stats")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_later_strategies_on_a_crowded_event_size_smaller_than_undampened(self):
        sig = TradeSignal(ticker="T", side="yes", model_probability=0.85, model_probability_yes=0.85,
                           market_implied_probability=0.16, edge_cents=30, rationale="test")

        with patch.object(shadow, "concentration_dampening_multiplier", return_value=1.0):
            shadow.evaluate_and_log("EVENT1", sig, yes_ask=6, no_ask=None,
                                     station_code="KSEA", measure="precipitation_daily", event_ticker="EVENT1")
        with storage.get_conn() as conn:
            no_damp = dict(conn.execute("SELECT strategy, count FROM shadow_trades").fetchall())

        with storage.get_conn() as conn:
            conn.execute("DELETE FROM shadow_trades")
            conn.commit()

        shadow.evaluate_and_log("EVENT1", sig, yes_ask=6, no_ask=None,
                                 station_code="KSEA", measure="precipitation_daily", event_ticker="EVENT1")
        with storage.get_conn() as conn:
            with_damp = dict(conn.execute("SELECT strategy, count FROM shadow_trades").fetchall())

        reduced = sum(1 for s in no_damp if s in with_damp and with_damp[s] < no_damp[s])
        self.assertGreaterEqual(reduced, 5, "most later-evaluated strategies should show real dampening")

    def test_arbitrage_is_exempt_hedged_by_construction(self):
        for s in ["calibrated_conservative", "calibrated_balanced", "calibrated_aggressive", "longshot"]:
            storage.log_shadow_trade(s, f"T-{s}", "yes", 10, 6, event_ticker="EVENT1")
        shadow.evaluate_and_log("EVENT1", None, yes_ask=45, no_ask=52,
                                 station_code="KSEA", measure="precipitation_daily", event_ticker="EVENT1")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM shadow_trades WHERE strategy='arbitrage' AND ticker='EVENT1'").fetchone()
        self.assertIsNotNone(row, "arbitrage should still trade despite 4 other strategies already exposed")


class TestDampeningRationaleAnnotation(unittest.TestCase):
    """Every dampening mechanism (performance, calibration, concentration)
    used to silently adjust position size with no trace in the trade's
    own rationale — a human reading the dashboard had no way to tell
    WHY a position ended up small. Now each layer appends a plain note
    when it actually reduces size, so the rationale a person reads is
    the real reason, not just the base signal text."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions", "calibration_stats")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def _signal(self, ticker, prob=0.85):
        return TradeSignal(ticker=ticker, side="yes", model_probability=prob,
                            model_probability_yes=prob, market_implied_probability=0.16,
                            edge_cents=30, rationale="base rationale")

    def test_calibration_dampening_note_appears(self):
        shadow.evaluate_and_log("T1", self._signal("T1"), yes_ask=6, no_ask=None,
                                 station_code="KSEA", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT rationale FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='T1'"
            ).fetchone()
        self.assertIn("sized down", row[0])
        self.assertIn("low calibration", row[0])
        self.assertIn("KSEA/precipitation_daily", row[0])

    def test_concentration_dampening_note_appears_for_later_strategies(self):
        for i in range(25):
            calibration.record_outcome("KSEA", "precipitation_daily", 0.85, i % 10 < 8)
        shadow.evaluate_and_log("T1", self._signal("T1"), yes_ask=6, no_ask=None,
                                 station_code="KSEA", measure="precipitation_daily", event_ticker="EVENT1")
        with storage.get_conn() as conn:
            rows = conn.execute("SELECT rationale FROM shadow_trades WHERE ticker='T1'").fetchall()
        self.assertTrue(any("other strategies already exposed" in r[0] for r in rows))

    def test_performance_dampening_note_appears(self):
        for i in range(20):
            tid = storage.log_shadow_trade("calibrated_conservative", f"L{i}", "yes", 10, 40)
            storage.settle_shadow_trade(tid, won=False, pnl_cents=-400)
        for i in range(25):
            calibration.record_outcome("KHOU", "precipitation_daily", 0.9, i % 10 < 9)
        # Isolate the COLD-STREAK dampening specifically: the losses just
        # logged are real settled history (which is what the dampening
        # check reads), but without this reset they'd also count as
        # today's realized P&L and could trip the daily kill switch --
        # a separate mechanism this test isn't about.
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()
        rm = shadow.get_engines()["calibrated_conservative"]
        rm.state.realized_pnl_today_cents = 0
        shadow.evaluate_and_log("T2", self._signal("T2", prob=0.9), yes_ask=40, no_ask=None,
                                 station_code="KHOU", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT rationale FROM shadow_trades WHERE strategy='calibrated_conservative' AND ticker='T2'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("cold streak", row[0])

    def test_a_clean_trade_has_an_unmodified_rationale(self):
        for i in range(25):
            calibration.record_outcome("KAUS", "precipitation_daily", 0.9, i % 10 < 9)
        shadow.evaluate_and_log("T3", self._signal("T3", prob=0.9), yes_ask=6, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT rationale FROM shadow_trades WHERE strategy='calibrated_conservative' AND ticker='T3'"
            ).fetchone()
        self.assertEqual(row[0], "base rationale")


class TestFeeExemptionForNoEdgeStrategies(unittest.TestCase):
    """Direct regression test for a real, confirmed bug found while
    building depth_imbalance: gross_expected_cents (edge_cents * contracts)
    minus any positive fee is always <= 0 when edge_cents=0, so
    favorites_baseline's fee-survival check silently rejected EVERY
    candidate it ever found, unconditionally — not "conditions rarely
    arose," structurally impossible to ever pass. The exemption previously
    only covered always_trade despite favorites_baseline sharing the
    identical edge_cents=0 pattern."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_favorites_baseline_can_now_actually_trade(self):
        shadow.evaluate_and_log("T1", None, yes_ask=92, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='favorites_baseline' AND ticker='T1'"
            ).fetchone()
        self.assertIsNotNone(row, "favorites_baseline must be able to trade a 92c favorite")

    def test_always_trade_still_works_as_before(self):
        shadow.evaluate_and_log("T2", None, yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='rain_always_trade' AND ticker='T2'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_real_probability_based_strategies_still_correctly_enforce_the_fee_check(self):
        """THE guard against over-correcting: a genuinely tiny edge from a
        REAL probability-based strategy must still fail the fee check."""
        tiny_edge_signal = TradeSignal(ticker="T3", side="yes", model_probability=0.51,
                                         model_probability_yes=0.51, market_implied_probability=0.50,
                                         edge_cents=1, rationale="tiny edge")
        shadow.evaluate_and_log("T3", tiny_edge_signal, yes_ask=50, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='T3'"
            ).fetchone()
        self.assertIsNone(row, "a genuinely tiny edge must still fail the fee-survival check")


class TestCalibrationTrusted(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions", "calibration_stats")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_is_registered(self):
        self.assertIn("calibration_trusted", shadow.ACTIVE_STRATEGIES)

    def test_does_not_fire_without_enough_calibration_samples(self):
        shadow.evaluate_and_log("T1", make_signal("T1"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='calibration_trusted' AND ticker='T1'"
            ).fetchone()
        self.assertIsNone(row)

    def test_fires_once_well_aligned_calibration_data_exists(self):
        for i in range(20):
            calibration.record_outcome("KAUS", "precipitation_daily", 0.70, i % 10 < 7)
        shadow.evaluate_and_log("T2", make_signal("T2"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT rationale FROM shadow_trades WHERE strategy='calibration_trusted' AND ticker='T2'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("trust check", row[0])

    def test_does_not_fire_when_bias_is_too_large_even_with_enough_samples(self):
        for _ in range(20):
            calibration.record_outcome("KHOU", "precipitation_daily", 0.70, False)
        shadow.evaluate_and_log("T3", make_signal("T3"), yes_ask=40, no_ask=None,
                                 station_code="KHOU", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='calibration_trusted' AND ticker='T3'"
            ).fetchone()
        self.assertIsNone(row)

    def test_calibrated_balanced_is_unaffected_by_this_gate(self):
        for _ in range(20):
            calibration.record_outcome("KHOU", "precipitation_daily", 0.70, False)
        shadow.evaluate_and_log("T4", make_signal("T4"), yes_ask=40, no_ask=None,
                                 station_code="KHOU", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='T4'"
            ).fetchone()
        self.assertIsNotNone(row)


class TestTightSpreadCalibrated(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_is_registered(self):
        self.assertIn("tight_spread_calibrated", shadow.ACTIVE_STRATEGIES)

    def test_fires_on_a_tight_spread(self):
        shadow.evaluate_and_log("T1", make_signal("T1"), yes_ask=42, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily", yes_bid=40)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT rationale FROM shadow_trades WHERE strategy='tight_spread_calibrated' AND ticker='T1'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("spread=2c", row[0])

    def test_does_not_fire_on_a_wide_spread(self):
        shadow.evaluate_and_log("T2", make_signal("T2"), yes_ask=50, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily", yes_bid=30)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='tight_spread_calibrated' AND ticker='T2'"
            ).fetchone()
        self.assertIsNone(row)

    def test_does_not_fire_with_no_bid_data(self):
        shadow.evaluate_and_log("T3", make_signal("T3"), yes_ask=42, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily")
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='tight_spread_calibrated' AND ticker='T3'"
            ).fetchone()
        self.assertIsNone(row)

    def test_negative_spread_fails_closed(self):
        """A bid above the ask is bad/crossed quote data, not a genuinely
        tight market — must never be treated as tradeable."""
        shadow.evaluate_and_log("T4", make_signal("T4"), yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily", yes_bid=45)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='tight_spread_calibrated' AND ticker='T4'"
            ).fetchone()
        self.assertIsNone(row)

    def test_exactly_at_the_threshold_counts_as_tight(self):
        shadow.evaluate_and_log("T5", make_signal("T5"), yes_ask=45, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily", yes_bid=40)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='tight_spread_calibrated' AND ticker='T5'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_calibrated_balanced_is_unaffected_by_this_gate(self):
        shadow.evaluate_and_log("T6", make_signal("T6"), yes_ask=50, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily", yes_bid=30)
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='T6'"
            ).fetchone()
        self.assertIsNotNone(row)


class TestNoSideProbabilityDirection(unittest.TestCase):
    """CONFIRMED SEVERE BUG this fixes: find_max_profitable_size needs the
    probability that the TRADED side wins, but model_prob (always
    signal.model_probability_yes) was passed unconditionally regardless of
    side. A "no" trade with model_probability_yes=0.10 (meaning P(no)=0.90,
    a highly confident bet) was evaluated as if it only had a 10% chance
    of winning — silently making almost every genuinely profitable "no"
    trade with real depth data available look wildly unprofitable and get
    rejected. "yes" trades were never affected, since model_prob already
    IS the correct probability for that side."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "shadow_bankroll_snapshots", "decisions")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def test_a_confident_no_trade_with_real_depth_data_now_executes(self):
        sig = TradeSignal(ticker="T1", side="no", model_probability=0.9, model_probability_yes=0.1,
                           market_implied_probability=0.4, edge_cents=30, rationale="confident no bet")
        shadow.evaluate_and_log("T1", sig, yes_ask=None, no_ask=60,
                                 station_code="KAUS", measure="precipitation_daily",
                                 yes_bids=[(35, 20)], no_bids=[(55, 100)])
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT side, count FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='T1'"
            ).fetchone()
        self.assertIsNotNone(row, "a 90%-confident NO trade must not be silently rejected")
        self.assertEqual(row[0], "no")

    def test_yes_side_trades_are_unaffected_by_this_fix(self):
        sig = TradeSignal(ticker="T2", side="yes", model_probability=0.9, model_probability_yes=0.9,
                           market_implied_probability=0.4, edge_cents=30, rationale="confident yes bet")
        shadow.evaluate_and_log("T2", sig, yes_ask=40, no_ask=None,
                                 station_code="KAUS", measure="precipitation_daily",
                                 yes_bids=[(35, 100)], no_bids=[(55, 20)])
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT side FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='T2'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "yes")

    def test_a_genuinely_unprofitable_no_trade_still_correctly_gets_rejected(self):
        """Guards against over-correcting: a real 30%-confidence NO bet at
        a price that doesn't support it must still fail."""
        sig = TradeSignal(ticker="T3", side="no", model_probability=0.3, model_probability_yes=0.7,
                           market_implied_probability=0.4, edge_cents=30, rationale="bad no bet")
        shadow.evaluate_and_log("T3", sig, yes_ask=None, no_ask=60,
                                 station_code="KAUS", measure="precipitation_daily",
                                 yes_bids=[(35, 20)], no_bids=[(55, 100)])
        with storage.get_conn() as conn:
            row = conn.execute("SELECT * FROM shadow_trades WHERE strategy='calibrated_balanced' AND ticker='T3'").fetchone()
        self.assertIsNone(row, "a genuinely unprofitable NO trade must still be rejected")


if __name__ == "__main__":
    unittest.main()
