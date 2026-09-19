"""Tests for evaluation/stats.py.

Reference values are published ones, not values this implementation
produced. A test that asserts a function still returns what it returned
yesterday verifies nothing about correctness.
"""
import math
import random
import unittest

from evaluation import stats


class TestNormalCDF(unittest.TestCase):
    def test_known_values(self):
        # Standard published quantiles of the normal distribution.
        for x, expected in [
            (0.0, 0.5),
            (1.0, 0.8413447460685429),
            (-1.0, 0.15865525393145707),
            (1.959963984540054, 0.975),
            (2.5758293035489004, 0.995),
            (-3.0, 0.0013498980316300946),
        ]:
            self.assertAlmostEqual(stats.normal_cdf(x), expected, places=12)

    def test_symmetry(self):
        for x in (0.3, 1.1, 2.7, 4.0):
            self.assertAlmostEqual(
                stats.normal_cdf(x) + stats.normal_cdf(-x), 1.0, places=14)


class TestNormalPPF(unittest.TestCase):
    def test_known_quantiles(self):
        # Published normal quantiles.
        for p, expected in [
            (0.5, 0.0),
            (0.75, 0.6744897501960817),
            (0.95, 1.6448536269514722),
            (0.975, 1.959963984540054),
            (0.99, 2.3263478740408408),
            (0.995, 2.5758293035489004),
            (0.999, 3.090232306167813),
            (0.025, -1.959963984540054),
            (0.001, -3.090232306167813),
        ]:
            self.assertAlmostEqual(stats.normal_ppf(p), expected, places=9)

    def test_round_trips_with_cdf(self):
        for p in (0.001, 0.01, 0.2, 0.5, 0.8, 0.99, 0.999):
            self.assertAlmostEqual(stats.normal_cdf(stats.normal_ppf(p)), p, places=12)

    def test_tail_branches(self):
        # Exercises both tail branches of Acklam's piecewise approximation.
        self.assertLess(stats.normal_ppf(0.001), -3.0)
        self.assertGreater(stats.normal_ppf(0.999), 3.0)

    def test_rejects_out_of_range(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                stats.normal_ppf(bad)


class TestMoments(unittest.TestCase):
    def test_mean_and_stdev(self):
        xs = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        self.assertAlmostEqual(stats.mean(xs), 5.0)
        # Population sd of this textbook sample is exactly 2.0.
        self.assertAlmostEqual(stats.stdev(xs, ddof=0), 2.0)
        # Sample sd is 2 * sqrt(8/7).
        self.assertAlmostEqual(stats.stdev(xs), 2.0 * math.sqrt(8.0 / 7.0))

    def test_symmetric_data_has_zero_skew(self):
        self.assertAlmostEqual(stats.skewness([-2.0, -1.0, 0.0, 1.0, 2.0]), 0.0)

    def test_right_skewed_data_is_positive(self):
        self.assertGreater(stats.skewness([1.0, 1.0, 1.0, 1.0, 10.0]), 0.0)

    def test_kurtosis_is_not_excess(self):
        # Normal-ish sample should sit near 3, not near 0. This is the
        # distinction the DSR formula depends on.
        rng = random.Random(7)
        xs = [rng.gauss(0.0, 1.0) for _ in range(20000)]
        self.assertGreater(stats.kurtosis(xs), 2.7)
        self.assertLess(stats.kurtosis(xs), 3.3)

    def test_uniform_kurtosis(self):
        # Continuous uniform has population kurtosis 1.8.
        rng = random.Random(11)
        xs = [rng.uniform(0.0, 1.0) for _ in range(40000)]
        self.assertAlmostEqual(stats.kurtosis(xs), 1.8, delta=0.05)

    def test_rejects_too_few_observations(self):
        with self.assertRaises(ValueError):
            stats.skewness([1.0, 2.0])
        with self.assertRaises(ValueError):
            stats.kurtosis([1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            stats.mean([])


class TestSharpe(unittest.TestCase):
    def test_basic(self):
        returns = [0.01, 0.02, -0.01, 0.03, 0.00]
        expected = stats.mean(returns) / stats.stdev(returns)
        self.assertAlmostEqual(stats.sharpe_ratio(returns), expected)

    def test_risk_free_shifts_it(self):
        returns = [0.05] * 4 + [0.03]
        self.assertGreater(stats.sharpe_ratio(returns),
                           stats.sharpe_ratio(returns, risk_free=0.02))

    def test_zero_variance_refuses(self):
        with self.assertRaises(ValueError):
            stats.sharpe_ratio([0.01] * 10)

    def test_too_few_refuses(self):
        with self.assertRaises(ValueError):
            stats.sharpe_ratio([0.01])


class TestExpectedMaximum(unittest.TestCase):
    def test_stays_below_the_sqrt_2_ln_n_upper_approximation(self):
        # sqrt(2 ln n) is the leading-order asymptotic and a loose OVER-
        # estimate at any n one actually searches: 3.04 vs a true 2.51 at
        # n=100, 5.26 vs a true 4.86 at a million. It is the number people
        # quote, so this pins the relationship rather than asserting
        # agreement with it -- an earlier version of this test wrongly
        # demanded they match and failed the correct implementation.
        for n in (100, 1_000, 10_000, 1_000_000):
            got = stats.expected_max_of_n_standard_normals(n)
            approx = math.sqrt(2.0 * math.log(n))
            self.assertLess(got, approx, msg=f"n={n}")
            self.assertGreater(got, 0.75 * approx, msg=f"n={n}")

    def test_matches_simulation_at_several_scales(self):
        # The authoritative check: simulate the quantity being estimated.
        rng = random.Random(17)
        for n, trials in ((100, 3000), (500, 2000), (2000, 1200)):
            observed = stats.mean([max(rng.gauss(0.0, 1.0) for _ in range(n))
                                   for _ in range(trials)])
            predicted = stats.expected_max_of_n_standard_normals(n)
            self.assertAlmostEqual(observed, predicted, delta=0.08,
                                   msg=f"n={n}: sim {observed:.3f} vs "
                                       f"estimate {predicted:.3f}")

    def test_the_headline_numbers(self):
        # The number that justifies the whole harness: the luckiest of a
        # million worthless strategies scores nearly 5 sigma by luck alone.
        got = stats.expected_max_of_n_standard_normals(1_000_000)
        self.assertGreater(got, 4.5)
        self.assertLess(got, 5.2)

    def test_grows_with_trials(self):
        vals = [stats.expected_max_of_n_standard_normals(n)
                for n in (10, 100, 1000, 10000)]
        self.assertEqual(vals, sorted(vals))

    def test_single_trial_has_no_selection_effect(self):
        self.assertEqual(stats.expected_max_sharpe(1, 1.0), 0.0)

    def test_scales_with_sqrt_variance(self):
        a = stats.expected_max_sharpe(1000, 1.0)
        b = stats.expected_max_sharpe(1000, 4.0)
        self.assertAlmostEqual(b / a, 2.0, places=9)

    def test_monte_carlo_agreement(self):
        # Simulate the thing the estimator estimates.
        rng = random.Random(3)
        n, trials = 500, 4000
        observed = stats.mean([max(rng.gauss(0.0, 1.0) for _ in range(n))
                               for _ in range(trials)])
        predicted = stats.expected_max_of_n_standard_normals(n)
        self.assertAlmostEqual(observed, predicted, delta=0.1)


class TestDeflatedSharpe(unittest.TestCase):
    def _returns(self, seed, mu, n=250):
        rng = random.Random(seed)
        return [rng.gauss(mu, 0.02) for _ in range(n)]

    def test_genuine_edge_survives_a_small_search(self):
        r = self._returns(1, mu=0.006)
        d = stats.deflated_sharpe_ratio(r, n_trials=10, sharpe_variance=0.01)
        self.assertTrue(d.beats_luck, f"probability={d.probability}")

    def test_same_edge_dies_under_a_huge_search(self):
        # Identical returns, but found by searching a million candidates
        # with wide dispersion. This is the entire point of the module.
        r = self._returns(1, mu=0.006)
        d = stats.deflated_sharpe_ratio(r, n_trials=1_000_000, sharpe_variance=1.0)
        self.assertFalse(d.beats_luck, f"probability={d.probability}")

    def test_noise_does_not_survive(self):
        r = self._returns(2, mu=0.0)
        d = stats.deflated_sharpe_ratio(r, n_trials=1000, sharpe_variance=0.25)
        self.assertLess(d.probability, 0.95)

    def test_more_trials_never_helps(self):
        r = self._returns(3, mu=0.004)
        probs = [stats.deflated_sharpe_ratio(
            r, n_trials=n, sharpe_variance=0.1).probability
            for n in (1, 10, 100, 10_000, 1_000_000)]
        self.assertEqual(probs, sorted(probs, reverse=True))

    def test_refuses_to_deflate_against_nothing(self):
        r = self._returns(4, mu=0.005)
        with self.assertRaises(ValueError):
            stats.deflated_sharpe_ratio(r, n_trials=100)

    def test_explicit_benchmark_is_honoured(self):
        r = self._returns(5, mu=0.005)
        d = stats.deflated_sharpe_ratio(r, n_trials=100, benchmark_sharpe=0.42)
        self.assertEqual(d.benchmark_sharpe, 0.42)

    def test_records_what_it_was_given(self):
        r = self._returns(6, mu=0.005)
        d = stats.deflated_sharpe_ratio(r, n_trials=77, sharpe_variance=0.05)
        self.assertEqual(d.n_trials, 77)
        self.assertEqual(d.n_observations, len(r))

    def test_too_few_observations_refuses(self):
        with self.assertRaises(ValueError):
            stats.deflated_sharpe_ratio([0.1, 0.2, 0.3], n_trials=5,
                                         sharpe_variance=0.1)


class TestBootstrap(unittest.TestCase):
    def test_is_deterministic_under_a_seed(self):
        xs = [float(i % 7) for i in range(200)]
        a = stats.moving_block_bootstrap(xs, 10, 100, seed=42)
        b = stats.moving_block_bootstrap(xs, 10, 100, seed=42)
        self.assertEqual(a, b)

    def test_different_seeds_differ(self):
        xs = [float(i % 7) for i in range(200)]
        self.assertNotEqual(stats.moving_block_bootstrap(xs, 10, 100, seed=1),
                            stats.moving_block_bootstrap(xs, 10, 100, seed=2))

    def test_interval_covers_the_true_mean_of_iid_data(self):
        rng = random.Random(9)
        xs = [rng.gauss(5.0, 1.0) for _ in range(500)]
        lo, hi = stats.bootstrap_confidence_interval(xs, block_size=1, seed=5)
        self.assertLess(lo, 5.0)
        self.assertGreater(hi, 5.0)

    def test_blocks_widen_the_interval_on_autocorrelated_data(self):
        # A random walk's mean is far less certain than its point count
        # suggests. Block resampling must produce a wider interval than
        # naive single-point resampling, or the harness will call
        # autocorrelated noise significant.
        rng = random.Random(13)
        xs, v = [], 0.0
        for _ in range(600):
            v += rng.gauss(0.0, 1.0)
            xs.append(v)
        naive = stats.bootstrap_confidence_interval(xs, block_size=1, seed=3)
        blocked = stats.bootstrap_confidence_interval(xs, block_size=50, seed=3)
        self.assertGreater(blocked[1] - blocked[0], naive[1] - naive[0])

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            stats.moving_block_bootstrap([], 1, 10)
        with self.assertRaises(ValueError):
            stats.moving_block_bootstrap([1.0, 2.0], 5, 10)
        with self.assertRaises(ValueError):
            stats.moving_block_bootstrap([1.0, 2.0], 1, 0)
        with self.assertRaises(ValueError):
            stats.bootstrap_confidence_interval([1.0, 2.0], 1, confidence=1.5)


class TestBrier(unittest.TestCase):
    def test_perfect_forecast_scores_zero(self):
        self.assertEqual(stats.brier_score([1.0, 0.0, 1.0], [1.0, 0.0, 1.0]), 0.0)

    def test_confidently_wrong_scores_one(self):
        self.assertEqual(stats.brier_score([0.0, 1.0], [1.0, 0.0]), 1.0)

    def test_coin_flip_scores_a_quarter(self):
        self.assertAlmostEqual(
            stats.brier_score([0.5] * 4, [1.0, 0.0, 1.0, 0.0]), 0.25)

    def test_matches_the_measured_nyc_rain_numbers(self):
        # Sanity anchor from analysis/horizon_calibration.py: a constant
        # forecast at the 44% base rate scores about 0.246 against a 44%
        # realized rate.
        outcomes = [1.0] * 44 + [0.0] * 56
        self.assertAlmostEqual(
            stats.brier_score([0.44] * 100, outcomes), 0.2464, places=4)

    def test_validates_inputs(self):
        with self.assertRaises(ValueError):
            stats.brier_score([0.5], [1.0, 0.0])
        with self.assertRaises(ValueError):
            stats.brier_score([1.5], [1.0])
        with self.assertRaises(ValueError):
            stats.brier_score([0.5], [0.5])
        with self.assertRaises(ValueError):
            stats.brier_score([], [])

    def test_skill_score_against_a_reference(self):
        outcomes = [1.0, 0.0, 1.0, 0.0]
        good = [0.9, 0.1, 0.9, 0.1]
        ref = [0.5] * 4
        self.assertGreater(stats.brier_skill_score(good, outcomes, ref), 0.0)
        self.assertAlmostEqual(
            stats.brier_skill_score(ref, outcomes, ref), 0.0, places=12)
        bad = [0.2, 0.8, 0.2, 0.8]
        self.assertLess(stats.brier_skill_score(bad, outcomes, ref), 0.0)

    def test_skill_score_requires_an_explicit_reference(self):
        # There is no default. The signature enforces it; this documents why.
        with self.assertRaises(TypeError):
            stats.brier_skill_score([0.5], [1.0])


if __name__ == "__main__":
    unittest.main()
