"""Covers categories.py's measure-to-category mapping, including the
fallback behavior for None/unrecognized measures."""
from __future__ import annotations

import unittest

from categories import category_for, CATEGORY_BY_MEASURE, CATEGORY_ORDER, MIN_SAMPLE_SIZE


class TestCategoryFor(unittest.TestCase):
    def test_precipitation_measures_map_to_rain(self):
        self.assertEqual(category_for("precipitation_daily"), "Rain")
        self.assertEqual(category_for("precipitation_monthly"), "Rain")

    def test_temperature_measures_map_to_temperature(self):
        self.assertEqual(category_for("temperature_high"), "Temperature")
        self.assertEqual(category_for("temperature_low"), "Temperature")

    def test_other_maps_to_other(self):
        self.assertEqual(category_for("other"), "Other")

    def test_none_falls_back_to_other(self):
        """This matters specifically for bracket_arbitrage's disabled
        historical rows, which have measure=None."""
        self.assertEqual(category_for(None), "Other")

    def test_unrecognized_measure_falls_back_to_other_rather_than_crashing(self):
        self.assertEqual(category_for("some_future_measure_type"), "Other")

    def test_every_mapped_category_is_in_the_display_order(self):
        for category in set(CATEGORY_BY_MEASURE.values()):
            with self.subTest(category=category):
                self.assertIn(category, CATEGORY_ORDER)

    def test_min_sample_size_is_a_positive_int(self):
        self.assertIsInstance(MIN_SAMPLE_SIZE, int)
        self.assertGreater(MIN_SAMPLE_SIZE, 0)


if __name__ == "__main__":
    unittest.main()
