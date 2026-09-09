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


if __name__ == "__main__":
    unittest.main()
