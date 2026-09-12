"""
This is what "the bot learns over time" actually means here, concretely:

For each (station, measure) pair — e.g. (KAUS, precipitation_daily) — it
keeps a running average of what the model predicted vs what actually
happened. If Austin rain contracts the model called "70% likely" only
resolved yes 45% of the time historically, that's a real, measurable bias:
Austin's convective, hit-or-miss summer storms are exactly the kind of
pattern that inflates a naive POP-based estimate. The bias gets subtracted
from future Austin predictions automatically.

This is deliberately NOT a neural net, gradient descent, or anything that
can quietly develop behavior you can't inspect. It's an auditable running
average. You can query calibration_stats directly and see exactly why the
bot's Austin estimates drifted. That auditability matters more than
sophistication when real money is on the line — a fancier model you can't
explain is worse, not better, when it starts doing something surprising.

Requires a minimum sample size before applying any correction, because a
"bias" computed from 4 data points is noise, not a pattern — and a false
signal from small samples nudging real bets is worse than no correction.
"""
from __future__ import annotations

import storage

MIN_SAMPLES_FOR_CALIBRATION = 20
MAX_BIAS_ADJUSTMENT = 0.20  # never let calibration alone shift a prediction by more than this


def get_bias(station_code: str | None, measure: str | None) -> tuple[float, str]:
    """
    Returns (bias, explanation). Bias is added to the raw model probability:
    a negative bias means "this station's contracts have historically
    resolved yes less often than the model expected" (e.g. Austin).
    """
    if not station_code or not measure:
        return 0.0, "no station/measure on file — no calibration applied"

    n, avg_predicted, avg_actual = storage.get_calibration_stats(station_code, measure)

    if n < MIN_SAMPLES_FOR_CALIBRATION:
        return 0.0, f"only {n} settled samples for {station_code}/{measure} (need {MIN_SAMPLES_FOR_CALIBRATION}) — no calibration yet"

    raw_bias = avg_actual - avg_predicted
    clamped = max(-MAX_BIAS_ADJUSTMENT, min(MAX_BIAS_ADJUSTMENT, raw_bias))
    return clamped, (
        f"{station_code}/{measure}: n={n}, model avg {avg_predicted:.2f} vs actual {avg_actual:.2f} "
        f"-> bias {clamped:+.2f}"
    )


def is_trusted(station_code: str | None, measure: str | None,
                max_bias_for_trust: float = 0.05) -> tuple[bool, str]:
    """
    Different question from get_bias(): not "how much should we correct
    the raw estimate" but "has this station/measure's raw estimate
    already been empirically demonstrated as well-aligned, with enough
    real data to say so." Used by shadow.py's calibration_trusted
    strategy — a strategy that ONLY trades where the model's track record
    at THIS specific station/measure is directly verified, rather than
    trusting the model everywhere uniformly the way calibrated_* does
    (those apply the SAME learned correction, but still trade regardless
    of whether that correction is based on 20 samples or 2000, or on a
    bias that's nearly zero versus one already at MAX_BIAS_ADJUSTMENT).

    Deliberately uses the RAW (unclamped) bias for the trust check, not
    get_bias()'s clamped return value — a raw bias of 0.30 that gets
    clamped to 0.20 for the correction itself should still count as "not
    yet trustworthy," not accidentally pass a 0.05 threshold check against
    the clamped number.
    """
    if not station_code or not measure:
        return False, "no station/measure on file"

    n, avg_predicted, avg_actual = storage.get_calibration_stats(station_code, measure)
    if n < MIN_SAMPLES_FOR_CALIBRATION:
        return False, f"only {n} settled samples for {station_code}/{measure} (need {MIN_SAMPLES_FOR_CALIBRATION})"

    raw_bias = avg_actual - avg_predicted
    if abs(raw_bias) > max_bias_for_trust:
        return False, (f"{station_code}/{measure}: n={n}, bias {raw_bias:+.2f} exceeds the "
                        f"{max_bias_for_trust:.2f} trust threshold — real data, but the raw model "
                        f"isn't well-aligned here yet")

    return True, (f"{station_code}/{measure}: n={n}, bias {raw_bias:+.2f} is within the "
                   f"{max_bias_for_trust:.2f} trust threshold — model empirically verified here")


# Sizing dampening based on calibration sample count — see
# calibration_dampening_multiplier's docstring. Two tiers, deliberately
# matching the simplicity of shadow.py's performance_dampening_multiplier
# (full size or a fixed fraction, no continuous scale) rather than a
# smoother function that would be harder to reason about and test.
CALIBRATION_DAMPENING_ZERO_SAMPLES_MULTIPLIER = 0.25
CALIBRATION_DAMPENING_PARTIAL_SAMPLES_MULTIPLIER = 0.5


def calibration_dampening_multiplier(station_code: str | None, measure: str | None) -> float:
    """
    Mechanical, always-safe-to-automate size reduction for trading a
    station/measure with little or no real settlement history — same
    "reduce risk, never increase it" philosophy as
    shadow.performance_dampening_multiplier, just triggered by calibration
    sample count instead of a losing streak.

    CONFIRMED REAL-WORLD MOTIVATION: a loss-analysis review found 20+
    trades across nearly every strategy all buying the same losing side
    of the same underlying market (KXRAIN-26SEP11-SEA), every one of them
    with 7-13 calibration samples — below the 20-sample threshold,
    meaning the RAW, uncorrected forecast POP was being trusted directly,
    with no station-specific base-rate correction applied yet. Because
    nearly every directional strategy shares the same underlying weather
    model, they all made the same mistake simultaneously and lost
    together — that's not diversification, it's the same bet placed many
    times. This doesn't fully decorrelate strategies that share a model
    (a real cross-strategy exposure cap is a separate, larger design
    question), but it directly shrinks the damage when that shared model
    turns out to be wrong for a specific, unproven station/measure.

    Applied uniformly to every strategy that consumes the calibrated
    probability model (see shadow.py's shared sizing block) — naturally a
    no-op for calibration_trusted's own qualifying trades, since that
    strategy already requires n>=20 before trading at all, the exact same
    threshold this function uses.

    Zero samples (literally no settlement history at all for this
    station/measure) gets dampened harder than a partial count (some real
    evidence, just not enough yet) — the SEA cluster's own worst cases
    were entering with as few as 7 samples, so "some but not enough" data
    still deserves real caution, not just a token reduction.
    """
    n, _, _ = storage.get_calibration_stats(station_code, measure)
    if n == 0:
        return CALIBRATION_DAMPENING_ZERO_SAMPLES_MULTIPLIER
    if n < MIN_SAMPLES_FOR_CALIBRATION:
        return CALIBRATION_DAMPENING_PARTIAL_SAMPLES_MULTIPLIER
    return 1.0


def apply_calibration(raw_probability: float, station_code: str | None, measure: str | None) -> tuple[float, str]:
    bias, note = get_bias(station_code, measure)
    adjusted = max(0.01, min(0.99, raw_probability + bias))
    return adjusted, note


def record_outcome(station_code: str | None, measure: str | None,
                    predicted_probability: float | None, actual_outcome: bool) -> None:
    """Call this once a market settles, feeding back the prediction that was
    actually used for that trade so future estimates for this station adjust."""
    if not station_code or not measure or predicted_probability is None:
        return
    storage.record_calibration_outcome(station_code, measure, predicted_probability, actual_outcome)
