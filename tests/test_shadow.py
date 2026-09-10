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

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402
import shadow  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
