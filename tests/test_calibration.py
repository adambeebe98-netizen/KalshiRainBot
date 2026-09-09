"""Covers calibration.py's bias math: direction, clamping, and the
minimum-sample-size gate that prevents noise from a handful of settled
trades nudging real predictions."""
from __future__ import annotations

import unittest

from tests.helpers import use_temp_db

use_temp_db()

import storage  # noqa: E402
import calibration  # noqa: E402


class TestCalibrationBias(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM calibration_stats")
            conn.commit()

    def _feed(self, station, measure, predicted, actual_bool, times=1):
        for _ in range(times):
            calibration.record_outcome(station, measure, predicted, actual_bool)

    def test_no_calibration_below_minimum_sample_size(self):
        self._feed("KAUS", "precipitation_daily", 0.70, True, times=5)
        bias, note = calibration.get_bias("KAUS", "precipitation_daily")
        self.assertEqual(bias, 0.0)
        self.assertIn("need", note)

    def test_positive_bias_when_model_underestimates(self):
        """Model said 70% but it actually resolved yes 100% of the time —
        model was too LOW, bias should nudge future predictions UP."""
        self._feed("KAUS", "precipitation_daily", 0.70, True, times=25)
        bias, note = calibration.get_bias("KAUS", "precipitation_daily")
        self.assertGreater(bias, 0)

    def test_negative_bias_when_model_overestimates(self):
        """Model said 70% but it never actually resolved yes — model was
        too HIGH, bias should nudge future predictions DOWN."""
        self._feed("KAUS", "precipitation_daily", 0.70, False, times=25)
        bias, note = calibration.get_bias("KAUS", "precipitation_daily")
        self.assertLess(bias, 0)

    def test_bias_is_clamped_to_max_adjustment(self):
        """An extreme, real miscalibration (predicted 90%, actual 0%)
        should still be capped at MAX_BIAS_ADJUSTMENT, not applied raw —
        a single station's bad run shouldn't be allowed to swing
        predictions by more than a bounded amount."""
        self._feed("KAUS", "precipitation_daily", 0.90, False, times=25)
        bias, _ = calibration.get_bias("KAUS", "precipitation_daily")
        self.assertGreaterEqual(bias, -calibration.MAX_BIAS_ADJUSTMENT)

    def test_apply_calibration_keeps_result_in_valid_probability_range(self):
        self._feed("KAUS", "precipitation_daily", 0.95, False, times=25)
        adjusted, _ = calibration.apply_calibration(0.95, "KAUS", "precipitation_daily")
        self.assertGreaterEqual(adjusted, 0.01)
        self.assertLessEqual(adjusted, 0.99)

    def test_missing_station_or_measure_applies_no_calibration(self):
        bias, note = calibration.get_bias(None, "precipitation_daily")
        self.assertEqual(bias, 0.0)
        bias2, note2 = calibration.get_bias("KAUS", None)
        self.assertEqual(bias2, 0.0)

    def test_record_outcome_is_a_safe_noop_without_a_real_probability(self):
        """Strategies without a real model estimate (always_trade,
        favorites, arbitrage) pass model_probability=None — this must
        never crash or record garbage."""
        calibration.record_outcome("KAUS", "precipitation_daily", None, True)
        n, _, _ = storage.get_calibration_stats("KAUS", "precipitation_daily")
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
