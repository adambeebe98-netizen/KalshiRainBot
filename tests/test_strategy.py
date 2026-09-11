"""
Covers strategy.py's probability models — the actual "brain" behind every
calibrated strategy's edge calculation. Includes a defensive simplification
found while auditing: yes_edge/no_edge used to be computed via two SEPARATE
rounding operations that are only algebraically (not necessarily bit-for-bit)
equivalent — verified empirically across a million random floats with zero
mismatches, then simplified to a direct negation so the two are equal by
construction rather than by observed luck.
"""
from __future__ import annotations

import unittest

from tests.helpers import use_temp_db

use_temp_db()

import storage  # noqa: E402
from rules_extractor import MarketRules  # noqa: E402
from weather_data import StationObservation, PrecipForecast  # noqa: E402
from strategy import (  # noqa: E402
    estimate_precip_probability, estimate_temperature_probability,
    market_implied_probability, pick_relevant_forecast_temp_f,
    evaluate_market, evaluate_temperature_market, FORECAST_STD_DEV_F,
)


def make_rules(**overrides) -> MarketRules:
    defaults = dict(
        ticker="TEST", station_code="KAUS", settlement_source="test",
        measure="precipitation_daily", threshold_description="test",
        trace_counts_as_zero=True, fallback_rule=None, confidence="high",
        threshold_low_f=None, threshold_high_f=None,
    )
    defaults.update(overrides)
    return MarketRules(**defaults)


def make_observation(**overrides) -> StationObservation:
    defaults = dict(
        station_id="KAUS", precipitation_last_hour_mm=None,
        precipitation_last_3hr_mm=None, temperature_f=75.0,
        description="Clear", timestamp="2026-09-09T12:00:00Z",
    )
    defaults.update(overrides)
    return StationObservation(**defaults)


def make_forecast_period(**overrides) -> PrecipForecast:
    defaults = dict(
        period_name="Today", probability_of_precipitation_pct=30,
        short_forecast="Partly Cloudy", temperature_f=85.0, is_daytime=True,
    )
    defaults.update(overrides)
    return PrecipForecast(**defaults)


class TestMarketImpliedProbability(unittest.TestCase):
    def test_converts_cents_to_probability(self):
        self.assertEqual(market_implied_probability(61), 0.61)
        self.assertEqual(market_implied_probability(1), 0.01)
        self.assertEqual(market_implied_probability(99), 0.99)


class TestEstimatePrecipProbability(unittest.TestCase):
    def test_already_measurable_precip_gives_high_probability(self):
        obs = make_observation(precipitation_last_hour_mm=2.5)
        prob, rationale, has_signal = estimate_precip_probability(obs, [], True)
        self.assertEqual(prob, 0.97)
        self.assertIn("already recorded", rationale)
        self.assertTrue(has_signal)

    def test_falls_back_to_forecast_pop_when_nothing_observed_yet(self):
        obs = make_observation(precipitation_last_hour_mm=None, precipitation_last_3hr_mm=None)
        forecast = [make_forecast_period(probability_of_precipitation_pct=40)]
        prob, rationale, has_signal = estimate_precip_probability(obs, forecast, True)
        self.assertEqual(prob, 0.40)
        self.assertTrue(has_signal)

    def test_takes_the_max_pop_across_the_next_two_periods(self):
        obs = make_observation(precipitation_last_hour_mm=None, precipitation_last_3hr_mm=None)
        forecast = [
            make_forecast_period(probability_of_precipitation_pct=20),
            make_forecast_period(probability_of_precipitation_pct=60),
            make_forecast_period(probability_of_precipitation_pct=90),  # 3rd period, should be ignored
        ]
        prob, rationale, has_signal = estimate_precip_probability(obs, forecast, True)
        self.assertEqual(prob, 0.60)
        self.assertTrue(has_signal)

    def test_no_data_at_all_returns_genuine_uncertainty_and_flags_no_real_signal(self):
        prob, rationale, has_signal = estimate_precip_probability(None, [], None)
        self.assertEqual(prob, 0.5)
        self.assertIn("no observation or forecast", rationale)
        self.assertFalse(has_signal, "THE bug this flag exists to prevent: a 0.5 "
                          "placeholder must never be treated as a real signal")

    def test_zero_precip_observed_falls_through_to_forecast_not_treated_as_measurable(self):
        obs = make_observation(precipitation_last_hour_mm=0.0, precipitation_last_3hr_mm=0.0)
        forecast = [make_forecast_period(probability_of_precipitation_pct=25)]
        prob, rationale, has_signal = estimate_precip_probability(obs, forecast, True)
        self.assertEqual(prob, 0.25, "0.0mm precip must NOT be treated as 'already measurable'")
        self.assertTrue(has_signal)


class TestEstimateTemperatureProbability(unittest.TestCase):
    def test_no_forecast_temp_returns_genuine_uncertainty_and_flags_no_real_signal(self):
        prob, rationale, has_signal = estimate_temperature_probability(75.0, None, 80.0, 90.0)
        self.assertEqual(prob, 0.5)
        self.assertFalse(has_signal, "THE bug this flag exists to prevent — confirmed live: "
                          "this exact case traded on a fake ~48c edge against a 2c market")

    def test_no_usable_threshold_returns_genuine_uncertainty_and_flags_no_real_signal(self):
        prob, rationale, has_signal = estimate_temperature_probability(75.0, 85.0, None, None)
        self.assertEqual(prob, 0.5)
        self.assertFalse(has_signal)

    def test_forecast_dead_center_of_a_symmetric_band_gives_high_probability(self):
        # forecast exactly matches the band's midpoint -- most of the
        # normal distribution's mass should fall inside [80, 90]
        prob, _, has_signal = estimate_temperature_probability(None, 85.0, 80.0, 90.0)
        self.assertGreater(prob, 0.7)
        self.assertTrue(has_signal)

    def test_forecast_far_outside_the_band_gives_low_probability(self):
        prob, _, has_signal = estimate_temperature_probability(None, 60.0, 80.0, 90.0)
        self.assertLess(prob, 0.1)
        self.assertTrue(has_signal)

    def test_open_ended_above_threshold_only(self):
        # "above 85F" with a forecast of 90F should be quite likely
        prob, _, has_signal = estimate_temperature_probability(None, 90.0, 85.0, None)
        self.assertGreater(prob, 0.5)
        self.assertTrue(has_signal)

    def test_open_ended_below_threshold_only(self):
        # "below 85F" (only an upper bound) with a forecast of 70F should be quite likely
        prob, _, has_signal = estimate_temperature_probability(None, 70.0, None, 85.0)
        self.assertGreater(prob, 0.5)
        self.assertTrue(has_signal)

    def test_probability_is_always_clamped_to_valid_range(self):
        prob_high, _, _ = estimate_temperature_probability(None, 200.0, 80.0, 90.0)
        prob_low, _, _ = estimate_temperature_probability(None, -200.0, 80.0, 90.0)
        self.assertGreaterEqual(prob_high, 0.01)
        self.assertLessEqual(prob_high, 0.99)
        self.assertGreaterEqual(prob_low, 0.01)
        self.assertLessEqual(prob_low, 0.99)

    def test_symmetric_around_the_forecast_mean(self):
        """A band centered exactly on the forecast should give a higher
        probability than an equally-wide band shifted away from it."""
        centered, _, _ = estimate_temperature_probability(None, 85.0, 80.0, 90.0)
        shifted, _, _ = estimate_temperature_probability(None, 85.0, 90.0, 100.0)
        self.assertGreater(centered, shifted)


class TestPickRelevantForecastTempF(unittest.TestCase):
    def test_temperature_high_picks_first_daytime_period(self):
        forecast = [
            make_forecast_period(is_daytime=False, temperature_f=60.0),
            make_forecast_period(is_daytime=True, temperature_f=85.0),
        ]
        self.assertEqual(pick_relevant_forecast_temp_f("temperature_high", forecast), 85.0)

    def test_temperature_low_picks_first_nighttime_period(self):
        forecast = [
            make_forecast_period(is_daytime=True, temperature_f=85.0),
            make_forecast_period(is_daytime=False, temperature_f=60.0),
        ]
        self.assertEqual(pick_relevant_forecast_temp_f("temperature_low", forecast), 60.0)

    def test_returns_none_when_no_matching_period_exists(self):
        forecast = [make_forecast_period(is_daytime=True, temperature_f=85.0)]
        self.assertIsNone(pick_relevant_forecast_temp_f("temperature_low", forecast))

    def test_skips_periods_with_no_temperature_data(self):
        forecast = [
            make_forecast_period(is_daytime=True, temperature_f=None),
            make_forecast_period(is_daytime=True, temperature_f=85.0),
        ]
        self.assertEqual(pick_relevant_forecast_temp_f("temperature_high", forecast), 85.0)


class TestEvaluateMarketSideSelection(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM calibration_stats")
            conn.commit()

    def test_picks_yes_when_model_thinks_yes_is_underpriced(self):
        rules = make_rules()
        obs = make_observation(precipitation_last_hour_mm=5.0)  # model -> 0.97
        signal = evaluate_market("T1", yes_price_cents=40, rules=rules, observation=obs, forecast=[])
        self.assertEqual(signal.side, "yes")
        self.assertGreater(signal.edge_cents, 0)

    def test_picks_no_when_model_thinks_yes_is_overpriced(self):
        rules = make_rules()
        obs = make_observation(precipitation_last_hour_mm=None, precipitation_last_3hr_mm=None)
        forecast = [make_forecast_period(probability_of_precipitation_pct=5)]  # model -> 0.05
        signal = evaluate_market("T2", yes_price_cents=80, rules=rules, observation=obs, forecast=forecast)
        self.assertEqual(signal.side, "no")
        self.assertGreater(signal.edge_cents, 0)

    def test_model_probability_yes_is_always_the_yes_side_estimate_regardless_of_traded_side(self):
        """calibration.py needs this convention to stay consistent — see
        TradeSignal.model_probability_yes's docstring."""
        rules = make_rules()
        obs = make_observation(precipitation_last_hour_mm=None, precipitation_last_3hr_mm=None)
        forecast = [make_forecast_period(probability_of_precipitation_pct=5)]
        signal = evaluate_market("T3", yes_price_cents=80, rules=rules, observation=obs, forecast=forecast)
        self.assertEqual(signal.side, "no")
        self.assertAlmostEqual(signal.model_probability_yes, 0.05, places=2)
        # model_probability (the TRADED side's probability) should be 1 - model_probability_yes here
        self.assertAlmostEqual(signal.model_probability, 1 - signal.model_probability_yes, places=6)

    def test_temperature_market_side_selection_mirrors_the_rain_model(self):
        rules = make_rules(measure="temperature_high", threshold_low_f=80.0, threshold_high_f=90.0)
        forecast = [make_forecast_period(is_daytime=True, temperature_f=85.0)]
        signal = evaluate_temperature_market("T4", yes_price_cents=30, rules=rules,
                                               observation=None, forecast=forecast)
        # forecast dead-center of the band -> high model probability -> should favor YES at a cheap 30c price
        self.assertEqual(signal.side, "yes")

    def test_no_forecast_data_forces_edge_to_zero_not_a_fake_edge(self):
        """THE regression, reproduced exactly as seen live: a far
        out-of-the-money market (2c) with NO forecast temperature
        available used to produce a ~48c "edge" by comparing a
        meaningless 0.5 placeholder against the real market price. That
        edge was never real information — it must be forced to 0
        regardless of how extreme the market price is."""
        rules = make_rules(measure="temperature_high", threshold_low_f=91.5, threshold_high_f=None)
        obs = make_observation(temperature_f=74.0)
        signal = evaluate_temperature_market("KXHIGHNY-26SEP10-B91.5", yes_price_cents=2,
                                               rules=rules, observation=obs, forecast=[])
        self.assertEqual(signal.edge_cents, 0)
        self.assertIn("no real signal", signal.rationale)

    def test_no_usable_threshold_also_forces_edge_to_zero(self):
        rules = make_rules(measure="temperature_high", threshold_low_f=None, threshold_high_f=None)
        forecast = [make_forecast_period(is_daytime=True, temperature_f=85.0)]
        signal = evaluate_temperature_market("T5", yes_price_cents=5, rules=rules,
                                               observation=None, forecast=forecast)
        self.assertEqual(signal.edge_cents, 0)

    def test_no_precip_data_at_all_also_forces_edge_to_zero(self):
        """Same fix, same bug class, for the rain model."""
        rules = make_rules(measure="precipitation_daily")
        signal = evaluate_market("T6", yes_price_cents=3, rules=rules, observation=None, forecast=[])
        self.assertEqual(signal.edge_cents, 0)
        self.assertIn("no real signal", signal.rationale)

    def test_a_real_signal_still_produces_a_normal_edge(self):
        """Confirms the fix doesn't over-correct -- a genuine forecast-based
        signal must still compute a real, non-zero edge as before."""
        rules = make_rules(measure="precipitation_daily")
        obs = make_observation(precipitation_last_hour_mm=5.0)  # -> model_p ~0.97, real signal
        signal = evaluate_market("T7", yes_price_cents=40, rules=rules, observation=obs, forecast=[])
        self.assertGreater(signal.edge_cents, 0)
        self.assertNotIn("no real signal", signal.rationale)


class TestNearCloseObservationBlending(unittest.TestCase):
    """Covers a real gap: observed_temp_f used to be accepted but
    completely ignored in the probability math, only shown in the
    rationale text. Near settlement, a fresh observation is a much better
    predictor than an hours-old forecast — scoped to temperature_high
    specifically (see evaluate_temperature_market), since a daily low
    settles overnight and an afternoon reading says nothing about it."""

    def test_beyond_the_window_behaves_identically_to_no_blending(self):
        with_time, _, _ = estimate_temperature_probability(74.0, 85.0, 80.0, 90.0, hours_until_close=12.0)
        without_time, _, _ = estimate_temperature_probability(74.0, 85.0, 80.0, 90.0, hours_until_close=None)
        self.assertEqual(with_time, without_time)

    def test_near_close_shifts_probability_toward_the_observation(self):
        far, _, _ = estimate_temperature_probability(74.0, 85.0, 80.0, 90.0, hours_until_close=12.0)
        close, _, _ = estimate_temperature_probability(74.0, 85.0, 80.0, 90.0, hours_until_close=0.1)
        self.assertLess(close, far)
        self.assertLess(close, 0.15)

    def test_rationale_explains_the_blending_only_when_it_applies(self):
        _, rationale_close, _ = estimate_temperature_probability(74.0, 85.0, 80.0, 90.0, hours_until_close=0.1)
        _, rationale_far, _ = estimate_temperature_probability(74.0, 85.0, 80.0, 90.0, hours_until_close=12.0)
        self.assertIn("blended toward the live observation", rationale_close)
        self.assertNotIn("blended", rationale_far)

    def test_no_observation_means_no_blending_even_within_the_window(self):
        no_obs, _, _ = estimate_temperature_probability(None, 85.0, 80.0, 90.0, hours_until_close=0.5)
        far, _, _ = estimate_temperature_probability(None, 85.0, 80.0, 90.0, hours_until_close=None)
        self.assertEqual(no_obs, far)

    def test_stddev_genuinely_narrows_near_close(self):
        wide, _, _ = estimate_temperature_probability(91.0, 90.0, 80.0, 90.0, hours_until_close=12.0)
        narrow, _, _ = estimate_temperature_probability(91.0, 90.0, 80.0, 90.0, hours_until_close=0.1)
        self.assertNotAlmostEqual(wide, narrow, places=3)

    def test_temperature_high_receives_blending(self):
        rules = make_rules(measure="temperature_high", threshold_low_f=80.0, threshold_high_f=90.0)
        obs = make_observation(temperature_f=74.0)
        forecast = [make_forecast_period(is_daytime=True, temperature_f=85.0)]
        signal = evaluate_temperature_market("T1", 40, rules, obs, forecast, hours_until_close=0.5)
        self.assertIn("blended toward the live observation", signal.rationale)

    def test_temperature_low_never_receives_blending(self):
        """THE important scoping test: a daily low settles overnight, so
        an afternoon observation must never be blended in even when close
        to close by clock time."""
        rules = make_rules(measure="temperature_low", threshold_low_f=80.0, threshold_high_f=90.0)
        obs = make_observation(temperature_f=74.0)
        forecast = [make_forecast_period(is_daytime=True, temperature_f=85.0)]
        signal = evaluate_temperature_market("T2", 40, rules, obs, forecast, hours_until_close=0.5)
        self.assertNotIn("blended toward the live observation", signal.rationale)


if __name__ == "__main__":
    unittest.main()
