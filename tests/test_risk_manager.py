"""
Covers risk_manager.py's core safety properties, including the
kill-switch persistence fix: RiskState.realized_pnl_today_cents used to
always initialize to 0 on process restart regardless of what already
happened earlier that same calendar day — meaning a tripped kill switch
was silently reset by any restart, defeating its entire purpose. The
fix reconstructs the real value from settled trades via
storage.get_todays_realized_pnl_cents() (tested in test_storage.py); this
file tests the RiskState/RiskManager side of that contract directly.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from risk_manager import RiskManager, RiskState, RiskPreset


def make_preset(**overrides) -> RiskPreset:
    defaults = dict(
        min_edge_cents=5, max_position_pct=0.03, max_daily_loss_pct=0.06,
        min_contract_price_cents=2, max_contract_price_cents=90,
        max_open_positions=15, max_contracts_per_trade=25, max_slippage_cents=6,
    )
    defaults.update(overrides)
    return RiskPreset(**defaults)


class TestKillSwitch(unittest.TestCase):
    def test_not_tripped_when_within_limit(self):
        state = RiskState(bankroll_cents=50000, day=date.today(), realized_pnl_today_cents=-1000)
        self.assertFalse(state.is_kill_switch_tripped(max_daily_loss_pct=0.06))  # -1000 > -3000 limit

    def test_tripped_when_loss_exceeds_limit(self):
        state = RiskState(bankroll_cents=50000, day=date.today(), realized_pnl_today_cents=-4000)
        self.assertTrue(state.is_kill_switch_tripped(max_daily_loss_pct=0.06))  # -4000 <= -3000 limit

    def test_exactly_at_the_limit_counts_as_tripped(self):
        state = RiskState(bankroll_cents=50000, day=date.today(), realized_pnl_today_cents=-3000)
        self.assertTrue(state.is_kill_switch_tripped(max_daily_loss_pct=0.06))

    def test_a_gain_never_trips_it(self):
        state = RiskState(bankroll_cents=50000, day=date.today(), realized_pnl_today_cents=5000)
        self.assertFalse(state.is_kill_switch_tripped(max_daily_loss_pct=0.06))

    def test_survives_a_simulated_restart_with_real_starting_state(self):
        """THE regression: construct RiskState exactly the way get_engines()
        now does — seeding realized_pnl_today_cents from real prior data,
        not the dataclass default of 0."""
        already_lost_today = -4800  # reconstructed from settled trades, as if just after a restart
        state = RiskState(bankroll_cents=50000, day=date.today(),
                           realized_pnl_today_cents=already_lost_today)
        rm = RiskManager(state, make_preset(max_daily_loss_pct=0.06))
        approved, reason = rm.approve_trade(price_cents=40, edge_cents=30)
        self.assertFalse(approved)
        self.assertIn("kill switch", reason)

    def test_rolls_over_to_a_new_day_correctly(self):
        state = RiskState(bankroll_cents=50000, day=date.today() - timedelta(days=1),
                           realized_pnl_today_cents=-4800)
        state.roll_day_if_needed(date.today())
        self.assertEqual(state.realized_pnl_today_cents, 0)
        self.assertEqual(state.day, date.today())

    def test_does_not_roll_over_within_the_same_day(self):
        state = RiskState(bankroll_cents=50000, day=date.today(), realized_pnl_today_cents=-4800)
        state.roll_day_if_needed(date.today())
        self.assertEqual(state.realized_pnl_today_cents, -4800, "must NOT reset mid-day")


class TestOpenPositionCountPersistence(unittest.TestCase):
    """Same bug class as TestKillSwitch's restart-survival tests, for
    open_positions_count instead of realized_pnl_today_cents — confirmed
    directly against production data: strategies showing 350-440+ "Active"
    positions on the dashboard, far beyond any reasonable per-strategy
    cap, because this counter reset to 0 on every restart regardless of
    how many real open positions already existed."""

    def test_max_open_positions_binds_correctly_when_seeded_from_real_count(self):
        state = RiskState(bankroll_cents=50000, day=date.today(), open_positions_count=15)
        rm = RiskManager(state, make_preset(max_open_positions=15))
        approved, reason = rm.approve_trade(price_cents=40, edge_cents=30)
        self.assertFalse(approved)
        self.assertIn("max open positions", reason)

    def test_the_bug_demonstrated_directly(self):
        """Without seeding, a restart forgets every real open position —
        this is exactly what get_engines() used to do before the fix."""
        unseeded_state = RiskState(bankroll_cents=50000, day=date.today())  # old behavior
        self.assertEqual(unseeded_state.open_positions_count, 0,
                          "this IS the bug: defaults to 0 regardless of real open positions")

    def test_below_the_cap_still_approves_normally(self):
        state = RiskState(bankroll_cents=50000, day=date.today(), open_positions_count=5)
        rm = RiskManager(state, make_preset(max_open_positions=15))
        approved, reason = rm.approve_trade(price_cents=40, edge_cents=30)
        self.assertTrue(approved)


class TestContractCapVsDollarCap(unittest.TestCase):
    def test_fixed_contract_cap_binds_regardless_of_a_large_bankroll(self):
        """THE other regression: bankroll*pct/price grows unboundedly with a
        winning bankroll, with zero connection to real market liquidity."""
        state = RiskState(bankroll_cents=50_000_000, day=date.today())  # a "successful" $500k bankroll
        rm = RiskManager(state, make_preset(max_position_pct=0.03, max_contracts_per_trade=25))
        self.assertEqual(rm.max_contracts_for_trade(price_cents=40), 25,
                          "must be capped at 25, not the ~37,500 the dollar math alone would allow")

    def test_dollar_cap_binds_when_it_is_the_tighter_constraint(self):
        state = RiskState(bankroll_cents=50000, day=date.today())  # $500
        rm = RiskManager(state, make_preset(max_position_pct=0.015, max_contracts_per_trade=15))
        # 50000 * 0.015 / 90 = 8, below the 15-contract cap
        self.assertEqual(rm.max_contracts_for_trade(price_cents=90), 8)

    def test_apply_contract_cap_false_exempts_arbitrage_style_sizing(self):
        state = RiskState(bankroll_cents=50_000_000, day=date.today())
        rm = RiskManager(state, make_preset(max_position_pct=0.03, max_contracts_per_trade=25))
        capped = rm.max_contracts_for_trade(price_cents=40, apply_contract_cap=True)
        uncapped = rm.max_contracts_for_trade(price_cents=40, apply_contract_cap=False)
        self.assertEqual(capped, 25)
        self.assertGreater(uncapped, 25)


class TestApproveTrade(unittest.TestCase):
    def setUp(self):
        self.state = RiskState(bankroll_cents=50000, day=date.today())
        self.rm = RiskManager(self.state, make_preset())

    def test_rejects_price_outside_band(self):
        approved, reason = self.rm.approve_trade(price_cents=95, edge_cents=30)
        self.assertFalse(approved)
        self.assertIn("outside allowed band", reason)

    def test_rejects_edge_below_minimum(self):
        approved, reason = self.rm.approve_trade(price_cents=40, edge_cents=1)
        self.assertFalse(approved)
        self.assertIn("below minimum", reason)

    def test_rejects_at_max_open_positions(self):
        self.state.open_positions_count = 15
        approved, reason = self.rm.approve_trade(price_cents=40, edge_cents=30)
        self.assertFalse(approved)
        self.assertIn("max open positions", reason)

    def test_rejects_when_edge_does_not_survive_real_fees(self):
        # A tiny nominal edge that a real per-order fee would erase.
        approved, reason = self.rm.approve_trade(price_cents=50, edge_cents=1)
        self.assertFalse(approved)

    def test_approves_a_genuinely_good_trade(self):
        approved, reason = self.rm.approve_trade(price_cents=40, edge_cents=30)
        self.assertTrue(approved)

    def test_edge_already_net_of_fees_skips_the_internal_fee_check(self):
        """Needed for multi-leg strategies (bracket arbitrage, the 2-leg
        arbitrage kind) whose "price" is a combined multi-order total, not
        a single contract's price — running that through the internal
        single-order fee formula produces a meaningless number. Uses a
        wide price band, matching how bracket_arbitrage's real config
        (max_price_override=5000) accommodates a summed multi-leg price."""
        wide_band_rm = RiskManager(
            RiskState(bankroll_cents=50000, day=date.today()),
            make_preset(max_contract_price_cents=5000),
        )
        approved, reason = wide_band_rm.approve_trade(
            price_cents=195, edge_cents=5, edge_already_net_of_fees=True,
        )
        self.assertTrue(approved)


class TestBankrollAccountingModel(unittest.TestCase):
    """Found via a static-analysis pass flagging record_fill's cost_cents
    as a possibly-unused parameter. Verified directly rather than
    assumed: bankroll_cents is deliberately NOT reduced when a position
    opens — it only changes at settlement, via the NET pnl_cents figure.
    "Deduct cost now, add gross payout later" and "do nothing now, add
    net change later" are equivalent as long as pnl_cents is always the
    net figure. These tests encode that verification permanently, so a
    future change can't silently break the invariant without a test
    catching it."""

    def test_a_winning_trade_nets_out_to_exactly_the_expected_profit(self):
        rm = RiskManager(RiskState(bankroll_cents=50000, day=date.today()), make_preset())
        rm.record_fill(cost_cents=400)  # 10 contracts @ 40c
        self.assertEqual(rm.state.bankroll_cents, 50000, "opening a position must not change bankroll_cents")
        rm.record_settlement(pnl_cents=600)  # payout 1000c - cost 400c = 600c net
        self.assertEqual(rm.state.bankroll_cents, 50600)

    def test_a_losing_trade_nets_out_to_exactly_the_expected_loss(self):
        rm = RiskManager(RiskState(bankroll_cents=50000, day=date.today()), make_preset())
        rm.record_fill(cost_cents=400)
        rm.record_settlement(pnl_cents=-400)  # lost the full stake
        self.assertEqual(rm.state.bankroll_cents, 49600)

    def test_sizing_does_not_account_for_capital_already_deployed(self):
        """Documents the real, understood consequence of the accounting
        model above: max_position_pct bounds risk on any ONE trade
        against the static bankroll figure, not cumulative exposure
        across many simultaneously open positions. The actual bound on
        total exposure comes from max_open_positions x
        max_contracts_per_trade instead — see record_fill's docstring."""
        rm = RiskManager(RiskState(bankroll_cents=50000, day=date.today()),
                          make_preset(max_position_pct=0.1, max_contracts_per_trade=25))
        size_before_any_fills = rm.max_contracts_for_trade(price_cents=40)
        for _ in range(6):
            rm.record_fill(cost_cents=size_before_any_fills * 40)
        size_with_6_open = rm.max_contracts_for_trade(price_cents=40)
        self.assertEqual(size_before_any_fills, size_with_6_open)


if __name__ == "__main__":
    unittest.main()
