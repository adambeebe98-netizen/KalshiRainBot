"""
Statistics for the evaluation harness.

Pure Python on purpose -- numpy and scipy are not installed on the droplet
and this does not add them. Every function here is implemented directly and
tested against published reference values, because the whole point of the
harness is that its verdicts can be trusted, and a verdict computed by an
unverified statistic is not one.

The centrepiece is the deflated Sharpe ratio. A plain Sharpe ratio answers
"did this strategy do well?". After searching N candidates, that is the
wrong question -- the best of N coin flips also does well. The expected
maximum score of N *worthless* strategies is about 3.2 sigma at a thousand
trials and 4.9 at a million. DSR asks the question that survives a search:
"did this do well RELATIVE to the best we should have expected from luck
alone, given how many times we looked?"

(The familiar sqrt(2 ln N) is the leading-order asymptotic and runs high
at any N one actually searches -- 5.26 against a true 4.86 at a million.
It is fine for intuition and wrong for a threshold, which is why
expected_max_sharpe implements Bailey's estimator instead.)
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

# Euler-Mascheroni constant, used by the expected-maximum estimator below.
EULER_MASCHERONI = 0.5772156649015329


# --------------------------------------------------------------------------
# Normal distribution
# --------------------------------------------------------------------------

def normal_cdf(x: float) -> float:
    """Standard normal CDF, via the error function in the stdlib."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Acklam's rational approximation to the inverse normal CDF. Coefficients
# are his published ones; relative error is below 1.15e-9 across the open
# interval, which one refinement step of Halley's method takes to full
# double precision. Reproduced rather than imported because scipy is not
# available -- test_eval_stats checks it against published quantiles.
_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)
_P_LOW = 0.02425
_P_HIGH = 1.0 - _P_LOW


def normal_ppf(p: float) -> float:
    """Inverse standard normal CDF (the quantile function)."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"normal_ppf needs 0 < p < 1, got {p}")

    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = ((((( _C[0]*q + _C[1])*q + _C[2])*q + _C[3])*q + _C[4])*q + _C[5]) / \
            ((((_D[0]*q + _D[1])*q + _D[2])*q + _D[3])*q + 1.0)
    elif p <= _P_HIGH:
        q = p - 0.5
        r = q * q
        x = (((((_A[0]*r + _A[1])*r + _A[2])*r + _A[3])*r + _A[4])*r + _A[5])*q / \
            (((((_B[0]*r + _B[1])*r + _B[2])*r + _B[3])*r + _B[4])*r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -((((( _C[0]*q + _C[1])*q + _C[2])*q + _C[3])*q + _C[4])*q + _C[5]) / \
            ((((_D[0]*q + _D[1])*q + _D[2])*q + _D[3])*q + 1.0)

    # One Halley refinement. Cheap, and takes the approximation from
    # ~1e-9 to machine precision so tests can assert tight bounds.
    e = normal_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


# --------------------------------------------------------------------------
# Moments
# --------------------------------------------------------------------------

def mean(xs: list[float]) -> float:
    if not xs:
        raise ValueError("mean of empty sequence")
    return math.fsum(xs) / len(xs)


def stdev(xs: list[float], ddof: int = 1) -> float:
    """Sample standard deviation. ddof=1 by default -- these are samples
    from a process, not a population we have enumerated."""
    n = len(xs)
    if n - ddof < 1:
        raise ValueError(f"stdev needs more than {ddof} observations, got {n}")
    m = mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - ddof))


def skewness(xs: list[float]) -> float:
    """Population (biased, 'g1') skewness -- this is the gamma_3 that the
    DSR formula expects, not the sample-corrected G1."""
    n = len(xs)
    if n < 3:
        raise ValueError("skewness needs at least 3 observations")
    m = mean(xs)
    s = stdev(xs, ddof=0)
    if s == 0.0:
        return 0.0
    return math.fsum((x - m) ** 3 for x in xs) / (n * s ** 3)


def kurtosis(xs: list[float]) -> float:
    """Population kurtosis, NOT excess kurtosis. A normal distribution
    gives 3.0 here. The DSR formula's gamma_4 is this quantity; passing
    excess kurtosis instead silently shifts the result, so the distinction
    is worth the explicit name."""
    n = len(xs)
    if n < 4:
        raise ValueError("kurtosis needs at least 4 observations")
    m = mean(xs)
    s = stdev(xs, ddof=0)
    if s == 0.0:
        return 3.0
    return math.fsum((x - m) ** 4 for x in xs) / (n * s ** 4)


# --------------------------------------------------------------------------
# Sharpe
# --------------------------------------------------------------------------

def sharpe_ratio(returns: list[float], risk_free: float = 0.0) -> float:
    """Per-observation Sharpe ratio, NOT annualised.

    Annualising requires a periodicity, and these returns are per-trade on
    markets whose lifetimes vary from hours to a month. Multiplying by
    sqrt(252) would be inventing a number. Callers that genuinely need an
    annualised figure should annualise explicitly and say what they assumed.
    """
    if len(returns) < 2:
        raise ValueError("sharpe_ratio needs at least 2 observations")
    excess = [r - risk_free for r in returns]
    s = stdev(excess)
    if s == 0.0:
        raise ValueError("sharpe_ratio undefined for zero-variance returns")
    return mean(excess) / s


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """Expected maximum Sharpe ratio across `n_trials` strategies that all
    have zero true edge -- i.e. the score to beat before "good" means
    anything.

    Bailey & Lopez de Prado's estimator:

        E[max SR] ~= sqrt(V) * [ (1-g) * Z(1 - 1/N) + g * Z(1 - 1/(N*e)) ]

    where g is Euler-Mascheroni, Z is the inverse normal CDF, and V is the
    variance of Sharpe ratios ACROSS the trials. That last term is the one
    people get wrong: it is the dispersion of the search's own results, not
    the variance of any single strategy's returns.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if sharpe_variance < 0.0:
        raise ValueError("sharpe_variance must be >= 0")
    if n_trials == 1:
        return 0.0
    g = EULER_MASCHERONI
    z1 = normal_ppf(1.0 - 1.0 / n_trials)
    z2 = normal_ppf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sharpe_variance) * ((1.0 - g) * z1 + g * z2)


def expected_max_of_n_standard_normals(n: int) -> float:
    """The intuition pump behind all of this: how well does the luckiest of
    n worthless candidates score? Equivalent to expected_max_sharpe with
    unit variance, and close to the familiar sqrt(2 ln n)."""
    return expected_max_sharpe(n, 1.0)


@dataclass(frozen=True)
class DeflatedSharpe:
    sharpe: float
    benchmark_sharpe: float   # the luck threshold this had to clear
    n_trials: int
    n_observations: int
    skew: float
    kurt: float
    probability: float        # P(true Sharpe > 0) after deflation

    @property
    def beats_luck(self) -> bool:
        """Conventional threshold. Deliberately a property rather than a
        hidden default inside the calculation, so the choice is visible."""
        return self.probability > 0.95


def deflated_sharpe_ratio(returns: list[float], n_trials: int,
                           sharpe_variance: float | None = None,
                           benchmark_sharpe: float | None = None) -> DeflatedSharpe:
    """Probability that a strategy's true Sharpe exceeds zero, after
    accounting for how many candidates were tried and for the non-normality
    of the returns.

        DSR = Z[ (SR - SR*) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4-1)/4 * SR^2) ]

    Pass `benchmark_sharpe` to supply SR* directly, or `sharpe_variance` to
    have it estimated from the trial count. One of the two is required: a
    DSR computed with SR* = 0 is just a Sharpe ratio wearing a lab coat,
    and this refuses to produce one by accident.
    """
    t = len(returns)
    if t < 4:
        raise ValueError("deflated_sharpe_ratio needs at least 4 observations")
    if benchmark_sharpe is None:
        if sharpe_variance is None:
            raise ValueError(
                "supply benchmark_sharpe or sharpe_variance -- deflating "
                "against zero defeats the purpose")
        benchmark_sharpe = expected_max_sharpe(n_trials, sharpe_variance)

    sr = sharpe_ratio(returns)
    g3 = skewness(returns)
    g4 = kurtosis(returns)

    denom_sq = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr
    if denom_sq <= 0.0:
        # Heavy left skew with a large Sharpe can drive this non-positive,
        # at which point the normal approximation has stopped describing
        # anything. Refuse rather than return a confident number.
        raise ValueError(
            f"DSR variance term non-positive ({denom_sq:.4f}); returns are too "
            f"non-normal for this approximation (skew={g3:.3f}, kurt={g4:.3f})")

    z = (sr - benchmark_sharpe) * math.sqrt(t - 1) / math.sqrt(denom_sq)
    return DeflatedSharpe(
        sharpe=sr, benchmark_sharpe=benchmark_sharpe, n_trials=n_trials,
        n_observations=t, skew=g3, kurt=g4, probability=normal_cdf(z),
    )


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------

def moving_block_bootstrap(xs: list[float], block_size: int, n_resamples: int,
                            statistic=mean, seed: int | None = None) -> list[float]:
    """Resample in contiguous blocks rather than individual points.

    An ordinary bootstrap assumes observations are independent. These are
    not: consecutive days of weather are strongly autocorrelated, and
    several bracket markets on the same city-day share one outcome. Drawing
    single observations would treat that dependence as extra information
    and produce confidence intervals far too narrow -- exactly the error
    that makes a backtest look significant when it is not. Blocks preserve
    the local dependence structure.
    """
    n = len(xs)
    if n == 0:
        raise ValueError("cannot bootstrap an empty sequence")
    if not 1 <= block_size <= n:
        raise ValueError(f"block_size must be in 1..{n}, got {block_size}")
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")

    rng = random.Random(seed)
    n_blocks = math.ceil(n / block_size)
    max_start = n - block_size
    out = []
    for _ in range(n_resamples):
        sample: list[float] = []
        for _ in range(n_blocks):
            start = rng.randint(0, max_start)
            sample.extend(xs[start:start + block_size])
        out.append(statistic(sample[:n]))
    return out


def bootstrap_confidence_interval(xs: list[float], block_size: int,
                                   n_resamples: int = 2000,
                                   confidence: float = 0.95,
                                   statistic=mean,
                                   seed: int | None = None) -> tuple[float, float]:
    """Percentile confidence interval from the moving block bootstrap."""
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")
    samples = sorted(moving_block_bootstrap(
        xs, block_size, n_resamples, statistic=statistic, seed=seed))
    alpha = (1.0 - confidence) / 2.0
    lo = samples[max(0, int(math.floor(alpha * len(samples))))]
    hi = samples[min(len(samples) - 1, int(math.ceil((1.0 - alpha) * len(samples))) - 1)]
    return lo, hi


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def brier_score(probabilities: list[float], outcomes: list[float]) -> float:
    """Mean squared error of probabilistic forecasts. Lower is better; a
    coin flip on a 50/50 event scores 0.25.

    This is the harness's primary forecast metric rather than accuracy,
    because a trading model needs calibrated probabilities, not opinions:
    a model that says 70% must be right 70% of the time or every position
    it sizes is sized wrong.
    """
    if len(probabilities) != len(outcomes):
        raise ValueError("probabilities and outcomes must be the same length")
    if not probabilities:
        raise ValueError("brier_score of empty sequence")
    for p in probabilities:
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"probability out of range: {p}")
    for o in outcomes:
        if o not in (0.0, 1.0):
            raise ValueError(f"outcome must be 0.0 or 1.0, got {o}")
    return math.fsum((p - o) ** 2 for p, o in zip(probabilities, outcomes)) / len(outcomes)


def brier_skill_score(probabilities: list[float], outcomes: list[float],
                       reference: list[float]) -> float:
    """Brier skill relative to a reference forecast: 1.0 is perfect, 0.0 is
    no better than the reference, negative is worse.

    The reference must be supplied explicitly. Defaulting it to the sample's
    own base rate would grade a model against a number that could only be
    known in hindsight, which flatters the reference and is a mistake this
    project has already made once.
    """
    ref = brier_score(reference, outcomes)
    if ref == 0.0:
        raise ValueError("reference forecast is perfect; skill score undefined")
    return 1.0 - brier_score(probabilities, outcomes) / ref
