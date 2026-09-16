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


class TestBiasDampeningNearExtremes(unittest.TestCase):
    """CONFIRMED REAL-WORLD MOTIVATION: a loss-analysis review found 14+
    strategies buying "yes" on a Seattle rain market with raw forecast
    POP of just 3-6% and no precipitation observed, all losing. The
    station's learned bias (+0.18, an average correction across whatever
    raw probability levels were actually seen historically) was applied
    at full strength to this extreme raw estimate, pushing model_p from
    ~0.05 to 0.24 -- a flat additive bias has a wildly disproportionate
    RELATIVE effect near the extremes of the probability scale, which is
    exactly where the raw signal is most confident and a long-run average
    correction is least justified."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM calibration_stats")
            conn.commit()

    def test_dampening_factor_peaks_at_one_half_and_fades_at_extremes(self):
        self.assertAlmostEqual(calibration._bias_dampening_factor(0.5), 1.0)
        self.assertLess(calibration._bias_dampening_factor(0.06), 0.3)
        self.assertLess(calibration._bias_dampening_factor(0.94), 0.3)
        self.assertAlmostEqual(calibration._bias_dampening_factor(0.0), 0.0)
        self.assertAlmostEqual(calibration._bias_dampening_factor(1.0), 0.0)

    def test_dampening_factor_never_exceeds_one_across_a_wide_sweep(self):
        for i in range(101):
            p = i / 100
            self.assertLessEqual(calibration._bias_dampening_factor(p), 1.0)
            self.assertGreaterEqual(calibration._bias_dampening_factor(p), 0.0)

    def test_reproduces_the_exact_confirmed_sea_scenario(self):
        for i in range(25):
            calibration.record_outcome("CLISEA", "precipitation_daily", 0.30, i % 25 < 12)
        n, avg_pred, avg_actual = storage.get_calibration_stats("CLISEA", "precipitation_daily")
        self.assertAlmostEqual(avg_actual - avg_pred, 0.18, places=2)

        raw_p = 0.06
        adjusted, note = calibration.apply_calibration(raw_p, "CLISEA", "precipitation_daily")
        bias, _ = calibration.get_bias("CLISEA", "precipitation_daily")
        old_undamped = max(0.01, min(0.99, raw_p + bias))

        self.assertAlmostEqual(old_undamped, 0.24, places=2, msg="confirms this matches the reported model_p=0.24")
        self.assertLess(adjusted, old_undamped, "the dampened result must be meaningfully lower than the old behavior")
        self.assertLess(adjusted, 0.15, "should stay much closer to the raw 0.06 estimate now")
        self.assertIn("bias dampened", note)

    def test_moderate_raw_estimates_are_only_lightly_dampened(self):
        """The PHX good case: bias and situational signal agreed, and the
        trade won. This fix must not meaningfully damage that case --
        only severe extremes should be heavily dampened."""
        for _ in range(25):
            calibration.record_outcome("CLIPHX", "precipitation_daily", 0.40, False)
        raw_p = 0.34
        adjusted, _ = calibration.apply_calibration(raw_p, "CLIPHX", "precipitation_daily")
        bias, _ = calibration.get_bias("CLIPHX", "precipitation_daily")
        old_undamped = max(0.01, min(0.99, raw_p + bias))

        self.assertLess(adjusted, raw_p, "should still meaningfully push toward NO")
        self.assertAlmostEqual(adjusted, old_undamped, delta=0.03,
                                 msg="a moderate raw estimate should be only lightly dampened, not gutted")

    def test_zero_bias_is_unaffected_by_dampening_regardless_of_raw_estimate(self):
        """No calibration data yet -- bias is 0, and 0 times any dampening
        factor is still 0. Must remain a true no-op."""
        adjusted, _ = calibration.apply_calibration(0.03, "NEVERSEEN", "precipitation_daily")
        self.assertEqual(adjusted, 0.03)

    def test_note_only_mentions_dampening_when_it_actually_applies(self):
        for i in range(25):
            calibration.record_outcome("CLISEA", "precipitation_daily", 0.30, i % 25 < 12)
        _, note_extreme = calibration.apply_calibration(0.06, "CLISEA", "precipitation_daily")
        _, note_center = calibration.apply_calibration(0.50, "CLISEA", "precipitation_daily")
        self.assertIn("bias dampened", note_extreme)
        self.assertNotIn("bias dampened", note_center, "near 0.5 the dampening factor is ~1.0, negligible correction")


class TestIsTrusted(unittest.TestCase):
    """Covers is_trusted() — a different question from get_bias(): not
    "how much correction," but "has this station/measure been directly,
    empirically verified as well-aligned," used by the calibration_trusted
    strategy."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM calibration_stats")
            conn.commit()

    def _feed(self, station, measure, predicted, actual_bool, times=1):
        for _ in range(times):
            calibration.record_outcome(station, measure, predicted, actual_bool)

    def test_not_trusted_below_minimum_samples(self):
        self._feed("KAUS", "precipitation_daily", 0.70, True, times=5)
        trusted, note = calibration.is_trusted("KAUS", "precipitation_daily")
        self.assertFalse(trusted)
        self.assertIn("only 5 settled samples", note)

    def test_trusted_with_enough_samples_and_small_bias(self):
        for i in range(20):
            calibration.record_outcome("KHOU", "precipitation_daily", 0.70, i % 10 < 7)
        trusted, note = calibration.is_trusted("KHOU", "precipitation_daily")
        self.assertTrue(trusted)

    def test_not_trusted_with_enough_samples_but_large_bias(self):
        for _ in range(20):
            calibration.record_outcome("KAUS", "precipitation_daily", 0.70, False)
        trusted, note = calibration.is_trusted("KAUS", "precipitation_daily")
        self.assertFalse(trusted)
        self.assertIn("exceeds", note)

    def test_not_trusted_with_missing_station_or_measure(self):
        trusted, _ = calibration.is_trusted(None, "precipitation_daily")
        self.assertFalse(trusted)
        trusted2, _ = calibration.is_trusted("KAUS", None)
        self.assertFalse(trusted2)

    def test_uses_raw_bias_not_the_clamped_correction_value(self):
        """A raw bias of -0.90 gets clamped to -0.20 for the actual
        correction, but the trust check must use the raw value — a
        badly miscalibrated station must never pass just because the
        correction itself is bounded."""
        for _ in range(20):
            calibration.record_outcome("KDEN", "precipitation_daily", 0.90, False)
        bias, _ = calibration.get_bias("KDEN", "precipitation_daily")
        self.assertEqual(bias, -calibration.MAX_BIAS_ADJUSTMENT)
        trusted, _ = calibration.is_trusted("KDEN", "precipitation_daily", max_bias_for_trust=0.05)
        self.assertFalse(trusted)

    def test_custom_trust_threshold_is_respected(self):
        for _ in range(20):
            calibration.record_outcome("KAUS", "precipitation_daily", 0.70, False)  # bias = -0.70
        # a very loose threshold should let even a large bias through
        trusted, _ = calibration.is_trusted("KAUS", "precipitation_daily", max_bias_for_trust=0.99)
        self.assertTrue(trusted)


class TestCalibrationDampeningMultiplier(unittest.TestCase):
    """CONFIRMED REAL-WORLD MOTIVATION: a loss-analysis review found 20+
    trades across nearly every strategy all buying the same losing side
    of the same underlying market, every one with 7-13 calibration
    samples (below the 20-sample threshold) — because nearly every
    directional strategy shares the same underlying weather model, they
    all made the same mistake simultaneously and lost together."""

    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM calibration_stats")
            conn.commit()

    def test_zero_samples_gets_the_most_severe_dampening(self):
        mult = calibration.calibration_dampening_multiplier("KSEA", "precipitation_daily")
        self.assertEqual(mult, calibration.CALIBRATION_DAMPENING_ZERO_SAMPLES_MULTIPLIER)

    def test_partial_samples_gets_moderate_dampening(self):
        """Directly matches the SEA cluster's actual 7-13 sample range."""
        for _ in range(10):
            calibration.record_outcome("KSEA", "precipitation_daily", 0.15, False)
        mult = calibration.calibration_dampening_multiplier("KSEA", "precipitation_daily")
        self.assertEqual(mult, calibration.CALIBRATION_DAMPENING_PARTIAL_SAMPLES_MULTIPLIER)

    def test_full_threshold_met_gets_no_additional_dampening(self):
        for _ in range(20):
            calibration.record_outcome("KSEA", "precipitation_daily", 0.15, False)
        mult = calibration.calibration_dampening_multiplier("KSEA", "precipitation_daily")
        self.assertEqual(mult, 1.0)

    def test_missing_station_or_measure_treated_as_zero_samples(self):
        mult = calibration.calibration_dampening_multiplier(None, "precipitation_daily")
        self.assertEqual(mult, calibration.CALIBRATION_DAMPENING_ZERO_SAMPLES_MULTIPLIER)

    def test_never_exceeds_1_across_a_wide_sweep(self):
        """Same safety property as performance_dampening_multiplier: this
        must provably only ever reduce size, never increase it."""
        stations = ["KSEA", "KHOU", "KAUS", "KDEN", "KLAS"]
        for station in stations:
            for n in range(0, 30):
                for _ in range(n):
                    calibration.record_outcome(station, "precipitation_daily", 0.5, True)
                mult = calibration.calibration_dampening_multiplier(station, "precipitation_daily")
                self.assertLessEqual(mult, 1.0)
                with storage.get_conn() as conn:
                    conn.execute("DELETE FROM calibration_stats WHERE station_code=?", (station,))
                    conn.commit()


if __name__ == "__main__":
    unittest.main()
