"""Tests for weatherman.py (Layer 1)."""
import datetime as dt
import math
import os
import random
import sqlite3
import tempfile
import unittest

import weatherman

HOUR = 3600
DAY = 86400
T0 = int(dt.datetime(2026, 3, 2, 5, tzinfo=dt.timezone.utc).timestamp())


class TestLocalWindows(unittest.TestCase):
    def test_offset_from_longitude(self):
        self.assertEqual(weatherman.utc_offset_hours(-73.97), -5)   # New York
        self.assertEqual(weatherman.utc_offset_hours(-118.4), -8)   # Los Angeles
        self.assertEqual(weatherman.utc_offset_hours(-87.6), -6)    # Chicago
        self.assertEqual(weatherman.utc_offset_hours(0.0), 0)


class TestLabel(unittest.TestCase):
    def cache(self, hours):
        c = weatherman.ArchiveCache()
        c.obs = hours
        return c

    def test_trace_counts_as_wet(self):
        # The rule the contracts settle on. A label keyed to measurable
        # rain would call this dry and be wrong on 10% of markets.
        c = self.cache({T0: 0.0001, T0 + HOUR: 0.0})
        self.assertEqual(weatherman.label_for_window(c, T0, T0 + DAY), 1.0)

    def test_dry_is_dry(self):
        c = self.cache({T0: 0.0, T0 + HOUR: 0.0})
        self.assertEqual(weatherman.label_for_window(c, T0, T0 + DAY), 0.0)

    def test_measurable_is_wet(self):
        c = self.cache({T0: 0.25})
        self.assertEqual(weatherman.label_for_window(c, T0, T0 + DAY), 1.0)

    def test_unobserved_window_is_none_not_dry(self):
        # "Nobody looked" and "no rain fell" are different facts.
        c = self.cache({T0 - 5 * DAY: 1.0})
        self.assertIsNone(weatherman.label_for_window(c, T0, T0 + DAY))

    def test_window_boundaries_are_half_open(self):
        # Rain at exactly T0+DAY belongs to the NEXT window, so this one
        # is dry -- but it needs an in-window observation to be observed
        # at all, otherwise the correct answer is None rather than dry.
        c = self.cache({T0 + 12 * HOUR: 0.0, T0 + DAY: 5.0})
        self.assertEqual(weatherman.label_for_window(c, T0, T0 + DAY), 0.0)
        self.assertEqual(
            weatherman.label_for_window(c, T0 + DAY, T0 + 2 * DAY), 1.0)


class TestFeaturesArePointInTime(unittest.TestCase):
    def cache(self):
        c = weatherman.ArchiveCache()
        c.obs = {T0 - HOUR * i: (0.1 if i < 5 else 0.0) for i in range(1, 80)}
        c.fc_precip = {T0 + HOUR * i: (0.5 if i in (3, 4) else 0.0)
                       for i in range(24)}
        c.fc_pop = {T0 + HOUR * i: (70.0 if i in (3, 4) else 10.0)
                    for i in range(24)}
        return c

    def test_returns_none_without_a_forecast(self):
        # Not zeros. Substituting zero teaches the model that a missing
        # forecast means dry weather.
        empty = weatherman.ArchiveCache()
        empty.obs = {T0: 0.0}
        self.assertIsNone(
            weatherman.features_for(empty, T0, T0 + DAY, 0.3, 24))

    def test_uses_only_forecasts_available_by_the_decision(self):
        c = self.cache()
        # A forecast hour is available at valid_at - lead. With a 1-hour
        # lead, almost none of the window is knowable at its start.
        short = weatherman.features_for(c, T0, T0 + DAY, 0.3, lead_hours=1)
        long = weatherman.features_for(c, T0, T0 + DAY, 0.3, lead_hours=24)
        self.assertIsNotNone(long)
        idx = weatherman.FEATURE_NAMES.index("fc_precip_hours")
        if short is not None:
            self.assertLessEqual(short[idx], long[idx])

    def test_prior_observations_come_from_before_the_window(self):
        c = self.cache()
        c.obs[T0 + 5 * HOUR] = 99.0        # inside the window: must not leak
        x = weatherman.features_for(c, T0, T0 + DAY, 0.3, 24)
        prior = x[weatherman.FEATURE_NAMES.index("obs_precip_prior_24h")]
        self.assertLess(prior, 90.0,
                        "an observation inside the window leaked into a "
                        "feature describing the run-up to it")

    def test_feature_vector_matches_the_declared_names(self):
        x = weatherman.features_for(self.cache(), T0, T0 + DAY, 0.3, 24)
        self.assertEqual(len(x), len(weatherman.FEATURE_NAMES))

    def test_seasonality_is_bounded(self):
        x = weatherman.features_for(self.cache(), T0, T0 + DAY, 0.3, 24)
        for name in ("season_sin", "season_cos"):
            v = x[weatherman.FEATURE_NAMES.index(name)]
            self.assertGreaterEqual(v, -1.0)
            self.assertLessEqual(v, 1.0)

    def test_climatology_is_passed_through(self):
        x = weatherman.features_for(self.cache(), T0, T0 + DAY, 0.42, 24)
        self.assertAlmostEqual(
            x[weatherman.FEATURE_NAMES.index("climo_wet_rate")], 0.42)


class TestLogisticRegression(unittest.TestCase):
    def _separable(self, n=400, seed=3):
        rng = random.Random(seed)
        X, Y = [], []
        for _ in range(n):
            signal = rng.gauss(0, 1)
            noise = rng.gauss(0, 1)
            X.append([signal, noise, signal * 0.5 + rng.gauss(0, 0.2)])
            Y.append(1.0 if signal + rng.gauss(0, 0.4) > 0 else 0.0)
        return X, Y

    def test_learns_a_learnable_signal(self):
        X, Y = self._separable()
        model = weatherman.fit(X, Y, iterations=800)
        preds = [model.predict(x) for x in X]
        base = sum(Y) / len(Y)
        from evaluation import stats
        self.assertLess(stats.brier_score(preds, Y),
                        stats.brier_score([base] * len(Y), Y))

    def test_finds_nothing_in_noise(self):
        rng = random.Random(9)
        X = [[rng.gauss(0, 1) for _ in range(3)] for _ in range(300)]
        Y = [1.0 if rng.random() < 0.4 else 0.0 for _ in range(300)]
        model = weatherman.fit(X, Y, iterations=800)
        preds = [model.predict(x) for x in X]
        # Should collapse toward the base rate rather than inventing skill.
        self.assertLess(max(preds) - min(preds), 0.6)

    def test_output_is_a_probability(self):
        X, Y = self._separable(n=100)
        model = weatherman.fit(X, Y, iterations=300)
        for x in X:
            p = model.predict(x)
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_weights_ignore_a_pure_noise_feature(self):
        X, Y = self._separable()
        model = weatherman.fit(X, Y, iterations=1500)
        self.assertGreater(abs(model.weights[0]), abs(model.weights[1]))

    def test_standardisation_is_recorded_and_applied(self):
        X, Y = self._separable(n=120)
        model = weatherman.fit(X, Y, iterations=200)
        self.assertEqual(len(model.mean), 3)
        self.assertTrue(all(s > 0 for s in model.std))

    def test_constant_feature_does_not_divide_by_zero(self):
        X = [[1.0, float(i % 2)] for i in range(50)]
        Y = [float(i % 2) for i in range(50)]
        model = weatherman.fit(X, Y, iterations=100)
        self.assertTrue(math.isfinite(model.predict([1.0, 1.0])))

    def test_refuses_an_empty_dataset(self):
        with self.assertRaises(ValueError):
            weatherman.fit([], [])

    def test_round_trips_through_json(self):
        X, Y = self._separable(n=120)
        model = weatherman.fit(X, Y, iterations=200)
        again = weatherman.LogisticModel.from_json(model.to_json())
        for x in X[:20]:
            self.assertAlmostEqual(model.predict(x), again.predict(x), places=12)

    def test_describe_is_ordered_by_influence(self):
        X, Y = self._separable(n=120)
        model = weatherman.fit(X, Y, iterations=200)
        model.feature_names = ("a", "b", "c")
        text = model.describe()
        self.assertIn("bias", text)
        self.assertIn("a", text)


if __name__ == "__main__":
    unittest.main()
