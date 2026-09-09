"""
Covers fees.py's taker fee formula, verified against Kalshi's published
fee schedule (0.07 * contracts * price * (1-price), rounded up, capped
at $1.75 per order) via multiple independent current sources. The cap
was MISSING entirely before this — a real gap, since without it, large
orders (exactly what tonight's depth-aware sizing work now enables) had
their fees overstated, which could reject trades that are actually
profitable under Kalshi's real, capped fee.
"""
from __future__ import annotations

import unittest

import fees


class TestTakerFeeCents(unittest.TestCase):
    def test_kalshis_own_published_example_10_cents(self):
        self.assertEqual(fees.taker_fee_cents(100, 10), 63)

    def test_kalshis_own_published_example_50_cents(self):
        self.assertEqual(fees.taker_fee_cents(100, 50), 175)

    def test_symmetric_around_50_cents(self):
        self.assertEqual(fees.taker_fee_cents(100, 20), fees.taker_fee_cents(100, 80))
        self.assertEqual(fees.taker_fee_cents(100, 10), fees.taker_fee_cents(100, 90))

    def test_peaks_at_50_cents(self):
        fee_50 = fees.taker_fee_cents(100, 50)
        for price in (10, 20, 30, 40, 60, 70, 80, 90):
            with self.subTest(price=price):
                self.assertLessEqual(fees.taker_fee_cents(100, price), fee_50)

    def test_cap_applies_to_large_orders(self):
        """THE regression: without the cap, this would compute to 350c
        (double the real $1.75 cap) via the raw formula alone."""
        self.assertEqual(fees.taker_fee_cents(200, 50), fees.MAX_FEE_CENTS_PER_ORDER)

    def test_cap_applies_at_any_price_for_a_large_enough_order(self):
        self.assertEqual(fees.taker_fee_cents(10000, 50), fees.MAX_FEE_CENTS_PER_ORDER)

    def test_zero_or_negative_contracts_is_zero_fee(self):
        self.assertEqual(fees.taker_fee_cents(0, 50), 0)
        self.assertEqual(fees.taker_fee_cents(-5, 50), 0)

    def test_zero_or_negative_price_is_zero_fee(self):
        self.assertEqual(fees.taker_fee_cents(100, 0), 0)
        self.assertEqual(fees.taker_fee_cents(100, -5), 0)

    def test_extreme_prices_have_a_small_but_nonzero_fee(self):
        fee_at_1c = fees.taker_fee_cents(100, 1)
        fee_at_99c = fees.taker_fee_cents(100, 99)
        self.assertGreater(fee_at_1c, 0)
        self.assertEqual(fee_at_1c, fee_at_99c)
        self.assertLess(fee_at_1c, fees.taker_fee_cents(100, 50))

    def test_fee_never_exceeds_the_cap_across_a_wide_range(self):
        for contracts in (1, 10, 100, 500, 1000):
            for price in range(1, 100):
                with self.subTest(contracts=contracts, price=price):
                    self.assertLessEqual(fees.taker_fee_cents(contracts, price),
                                          fees.MAX_FEE_CENTS_PER_ORDER)


if __name__ == "__main__":
    unittest.main()
