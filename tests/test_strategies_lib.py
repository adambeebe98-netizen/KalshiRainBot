"""
Covers strategies_lib.py's candidate generators — had zero dedicated
tests before this despite being where every non-calibrated strategy's
core trading logic lives. Includes depth_imbalance_candidate, the new
pure market-microstructure signal, and a direct regression test for the
fee-exemption bug found while building it (see test_shadow.py's
TestFeeExemptionForNoEdgeStrategies for the full shadow.py-level test).
"""
from __future__ import annotations

import unittest

from strategies_lib import (
    arbitrage_candidate, favorites_candidate, longshot_candidate,
    depth_imbalance_candidate, StrategyCandidate,
)
from strategy import TradeSignal


class TestArbitrageCandidate(unittest.TestCase):
    def test_real_arbitrage_when_combined_price_is_under_100(self):
        c = arbitrage_candidate(yes_ask=45, no_ask=50)
        self.assertIsNotNone(c)
        self.assertEqual(c.side, "both")
        self.assertEqual(c.price_cents, 95)
        self.assertEqual(c.edge_cents, 5)

    def test_no_arbitrage_when_combined_price_is_100_or_more(self):
        self.assertIsNone(arbitrage_candidate(yes_ask=55, no_ask=50))
        self.assertIsNone(arbitrage_candidate(yes_ask=50, no_ask=50))

    def test_missing_either_price_returns_none(self):
        self.assertIsNone(arbitrage_candidate(None, 50))
        self.assertIsNone(arbitrage_candidate(50, None))


class TestFavoritesCandidate(unittest.TestCase):
    def test_at_or_above_threshold_returns_a_candidate(self):
        c = favorites_candidate(92, threshold=90)
        self.assertIsNotNone(c)
        self.assertEqual(c.side, "yes")
        self.assertEqual(c.price_cents, 92)
        self.assertEqual(c.edge_cents, 0)

    def test_below_threshold_returns_none(self):
        self.assertIsNone(favorites_candidate(85, threshold=90))

    def test_exactly_at_threshold_counts(self):
        self.assertIsNotNone(favorites_candidate(90, threshold=90))


class TestLongshotCandidate(unittest.TestCase):
    def _signal(self, side="yes", edge=20):
        return TradeSignal(ticker="T", side=side, model_probability=0.3, model_probability_yes=0.3,
                            market_implied_probability=0.1, edge_cents=edge, rationale="test")

    def test_within_the_price_band_returns_a_candidate(self):
        c = longshot_candidate(self._signal(), price_cents=5, min_price=2, max_price=15)
        self.assertIsNotNone(c)
        self.assertEqual(c.side, "yes")
        self.assertEqual(c.edge_cents, 20)

    def test_outside_the_price_band_returns_none(self):
        self.assertIsNone(longshot_candidate(self._signal(), price_cents=40, min_price=2, max_price=15))
        self.assertIsNone(longshot_candidate(self._signal(), price_cents=1, min_price=2, max_price=15))

    def test_no_signal_returns_none(self):
        self.assertIsNone(longshot_candidate(None, price_cents=5))


class TestDepthImbalanceCandidate(unittest.TestCase):
    def test_strong_yes_side_imbalance_produces_a_yes_candidate(self):
        c = depth_imbalance_candidate(yes_ask=45, no_ask=60, yes_bids=[(40, 100)], no_bids=[(50, 20)])
        self.assertIsNotNone(c)
        self.assertEqual(c.side, "yes")
        self.assertEqual(c.price_cents, 45)
        self.assertEqual(c.edge_cents, 0)

    def test_strong_no_side_imbalance_produces_a_no_candidate(self):
        c = depth_imbalance_candidate(yes_ask=45, no_ask=60, yes_bids=[(40, 20)], no_bids=[(50, 100)])
        self.assertIsNotNone(c)
        self.assertEqual(c.side, "no")
        self.assertEqual(c.price_cents, 60)

    def test_below_the_ratio_threshold_returns_none(self):
        c = depth_imbalance_candidate(yes_ask=45, no_ask=60, yes_bids=[(40, 40)], no_bids=[(50, 30)])
        self.assertIsNone(c, "1.33x ratio should not clear the default 3.0x threshold")

    def test_below_min_total_depth_returns_none_even_with_a_huge_ratio(self):
        c = depth_imbalance_candidate(yes_ask=45, no_ask=60, yes_bids=[(40, 10)], no_bids=[(50, 1)])
        self.assertIsNone(c, "total depth of 11 is below the default min_total_depth of 20")

    def test_missing_book_data_on_either_side_fails_closed(self):
        self.assertIsNone(depth_imbalance_candidate(45, 60, None, [(50, 100)]))
        self.assertIsNone(depth_imbalance_candidate(45, 60, [(40, 100)], None))
        self.assertIsNone(depth_imbalance_candidate(45, 60, [], [(50, 100)]))

    def test_zero_depth_on_one_side_returns_none(self):
        c = depth_imbalance_candidate(yes_ask=45, no_ask=60, yes_bids=[(40, 100)], no_bids=[(50, 0)])
        self.assertIsNone(c)

    def test_missing_ask_price_for_the_imbalanced_side_returns_none(self):
        c = depth_imbalance_candidate(yes_ask=None, no_ask=60, yes_bids=[(40, 100)], no_bids=[(50, 20)])
        self.assertIsNone(c)

    def test_custom_thresholds_are_respected(self):
        # A 2x ratio that would fail the default 3.0x threshold should
        # pass with a custom, looser one.
        c = depth_imbalance_candidate(yes_ask=45, no_ask=60, yes_bids=[(40, 40)], no_bids=[(50, 20)],
                                        min_imbalance_ratio=2.0)
        self.assertIsNotNone(c)


if __name__ == "__main__":
    unittest.main()
