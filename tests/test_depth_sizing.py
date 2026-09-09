"""
Covers depth_sizing.py's three sizing functions, plus one real,
previously-shipped bug: find_max_profitable_size's search step was
derived from a bankroll-based cap that knew nothing about actual book
depth — a thin book (e.g. 35 contracts total) against a healthy bankroll
cap (e.g. 750) produced a step of 750//20=37, bigger than the ENTIRE
book, so the very first size tried already exceeded available depth and
the function returned None instead of finding the smaller, genuinely
profitable fill that actually existed. Fixed by clamping the cap to real
total book depth before computing the step.
"""
from __future__ import annotations

import unittest

import depth_sizing
import fees


class TestImpliedAskLevels(unittest.TestCase):
    def test_inverts_bids_into_ask_levels_sorted_best_first(self):
        # A resting bid at price P is the same liquidity as an implied ask
        # at (100-P) on the opposite side.
        bids = [(60, 5), (55, 10), (50, 20)]
        levels = depth_sizing.implied_ask_levels(bids)
        self.assertEqual(levels, [(40, 5), (45, 10), (50, 20)])


class TestEstimateFill(unittest.TestCase):
    def test_fills_within_a_single_level(self):
        levels = [(40, 100)]
        fill = depth_sizing.estimate_fill(levels, 10)
        self.assertEqual(fill.contracts_fillable, 10)
        self.assertEqual(fill.total_cost_cents, 400)
        self.assertEqual(fill.avg_price_cents, 40)
        self.assertFalse(fill.exhausted_book)

    def test_walks_multiple_levels_for_a_realistic_avg_price(self):
        levels = [(40, 5), (45, 10), (50, 20)]
        fill = depth_sizing.estimate_fill(levels, 18)
        self.assertEqual(fill.contracts_fillable, 18)
        # 5@40 + 10@45 + 3@50 = 200+450+150 = 800
        self.assertEqual(fill.total_cost_cents, 800)
        self.assertAlmostEqual(fill.avg_price_cents, 800 / 18)

    def test_exhausted_book_flag_when_size_exceeds_depth(self):
        levels = [(40, 5)]
        fill = depth_sizing.estimate_fill(levels, 10)
        self.assertEqual(fill.contracts_fillable, 5)
        self.assertTrue(fill.exhausted_book)


class TestFindMaxProfitableSize(unittest.TestCase):
    def test_finds_the_full_profitable_depth_even_with_a_large_bankroll_cap(self):
        """THE regression: this exact scenario used to return None."""
        no_bids = [(60, 5), (55, 10), (50, 20)]  # implies YES asks 40/45/50c
        ask_levels = depth_sizing.implied_ask_levels(no_bids)
        fill = depth_sizing.find_max_profitable_size(
            ask_levels, fees.taker_fee_cents, model_probability=0.7,
            max_contracts_cap=750,  # a healthy-bankroll-derived cap, way bigger than the 35-deep book
            min_net_edge_cents=6,
        )
        self.assertIsNotNone(fill, "a genuinely profitable fill exists here — must not return None")
        self.assertEqual(fill.contracts_fillable, 35, "should find the FULL book depth, not give up early")

    def test_still_respects_a_cap_smaller_than_book_depth(self):
        no_bids = [(60, 100)]
        ask_levels = depth_sizing.implied_ask_levels(no_bids)
        fill = depth_sizing.find_max_profitable_size(
            ask_levels, fees.taker_fee_cents, model_probability=0.9,
            max_contracts_cap=10, min_net_edge_cents=1,
        )
        self.assertLessEqual(fill.contracts_fillable, 10)

    def test_returns_none_when_not_even_the_smallest_size_is_profitable(self):
        no_bids = [(1, 100)]  # implies YES ask 99c -- essentially no room for edge
        ask_levels = depth_sizing.implied_ask_levels(no_bids)
        fill = depth_sizing.find_max_profitable_size(
            ask_levels, fees.taker_fee_cents, model_probability=0.5,
            max_contracts_cap=100, min_net_edge_cents=500,
        )
        self.assertIsNone(fill)

    def test_max_slippage_cents_caps_size_before_profitability_would(self):
        no_bids = [(60, 10), (55, 10), (50, 10), (40, 10)]
        ask_levels = depth_sizing.implied_ask_levels(no_bids)
        # A very high model_probability makes every level nominally
        # profitable, so without a slippage cap it walks the whole book.
        unlimited = depth_sizing.find_max_profitable_size(
            ask_levels, fees.taker_fee_cents, model_probability=0.95,
            max_contracts_cap=40, min_net_edge_cents=1,
        )
        limited = depth_sizing.find_max_profitable_size(
            ask_levels, fees.taker_fee_cents, model_probability=0.95,
            max_contracts_cap=40, min_net_edge_cents=1, max_slippage_cents=3,
        )
        self.assertGreater(unlimited.contracts_fillable, limited.contracts_fillable)
        self.assertLessEqual(limited.avg_price_cents - 40, 3)


class TestFindMaxArbitrageSize(unittest.TestCase):
    def test_scales_beyond_a_single_pair_when_book_supports_it(self):
        yes_ask_levels = [(40, 10), (45, 10)]
        no_ask_levels = [(40, 10), (45, 10)]
        fill = depth_sizing.find_max_arbitrage_size(
            yes_ask_levels, no_ask_levels, fees.taker_fee_cents, min_net_edge_cents=1,
        )
        self.assertIsNotNone(fill)
        self.assertGreater(fill.contracts_fillable, 1)

    def test_bottlenecked_by_the_shallower_side(self):
        yes_ask_levels = [(30, 5)]
        no_ask_levels = [(30, 100)]
        fill = depth_sizing.find_max_arbitrage_size(
            yes_ask_levels, no_ask_levels, fees.taker_fee_cents, min_net_edge_cents=1,
        )
        self.assertEqual(fill.contracts_fillable, 5)

    def test_no_artificial_slippage_limit_takes_full_profitable_depth(self):
        yes_ask_levels = [(20, 50), (25, 50)]
        no_ask_levels = [(20, 50), (25, 50)]
        fill = depth_sizing.find_max_arbitrage_size(
            yes_ask_levels, no_ask_levels, fees.taker_fee_cents, min_net_edge_cents=1,
        )
        self.assertEqual(fill.contracts_fillable, 100,
                          "arbitrage should never stop early on price drift — only on profitability")


class TestFindMaxBracketSize(unittest.TestCase):
    def test_finds_the_full_depth_across_all_legs(self):
        leg1 = [(40, 20)]
        leg2 = [(45, 20)]
        leg3 = [(50, 20)]
        fill = depth_sizing.find_max_bracket_size(
            [leg1, leg2, leg3], fees.taker_fee_cents, min_net_edge_cents=1,
        )
        self.assertIsNotNone(fill)
        self.assertEqual(fill.contracts_fillable, 20)

    def test_bottlenecked_by_the_shallowest_leg(self):
        leg1 = [(40, 5)]
        leg2 = [(45, 100)]
        leg3 = [(50, 100)]
        fill = depth_sizing.find_max_bracket_size(
            [leg1, leg2, leg3], fees.taker_fee_cents, min_net_edge_cents=1,
        )
        self.assertEqual(fill.contracts_fillable, 5)

    def test_needs_at_least_two_legs(self):
        fill = depth_sizing.find_max_bracket_size([[(40, 20)]], fees.taker_fee_cents, min_net_edge_cents=1)
        self.assertIsNone(fill)


if __name__ == "__main__":
    unittest.main()
