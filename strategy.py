"""
This is the actual "brain" — but deliberately a simple, inspectable one.
A black-box model you can't explain is the last thing you want with real
money, because when it starts losing you need to know whether it's normal
variance or the model broke.

Model, in plain terms, for a same-day precipitation market:
  1. Pull the settlement station's latest NWS observation (has it already
     rained measurably today, per the actual feed).
  2. Pull the NWS point forecast probability-of-precipitation for the
     remaining hours of the settlement window.
  3. Combine into a rough probability that the station will show
     measurable ( > 0", non-trace) precipitation by settlement.
  4. Compare to the market's implied probability (roughly, the YES price).
  5. Only flag a trade when the gap exceeds your configured minimum edge —
     and only on markets whose rules extraction has 'high' or 'medium'
     confidence, since a low-confidence rules read means you might not
     even know what you're betting on.

This is intentionally NOT a machine-learned model. Start here, log every
decision and outcome (storage.py does this), and only add complexity once
you have real settled trades to check the simple model against. A fancier
model built before you have that data is just a fancier way to be wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist
from typing import Optional

import calibration
from rules_extractor import MarketRules
from weather_data import StationObservation, PrecipForecast


@dataclass
class TradeSignal:
    ticker: str
    side: str  # 'yes' or 'no'
    model_probability: float  # model's estimate that the traded side resolves true
    model_probability_yes: float  # model's calibrated estimate of the YES event specifically —
                                   # always stored this way in trades table so calibration.py
                                   # (which tracks "yes" outcomes) reads a consistent quantity
                                   # regardless of which side got traded
    market_implied_probability: float
    edge_cents: int
    rationale: str


def estimate_precip_probability(
    observation: Optional[StationObservation],
    forecast: list[PrecipForecast],
    trace_counts_as_zero: Optional[bool],
) -> tuple[float, str]:
    """Returns (probability measurable precip occurs, human-readable rationale)."""
    notes = []

    # Already-observed precipitation this hour/3hr strongly predicts a "yes"
    # for a daily contract, since the day's total only needs to clear a
    # small threshold once.
    already_measurable = False
    if observation:
        precip_now = observation.precipitation_last_hour_mm or observation.precipitation_last_3hr_mm
        if precip_now and precip_now > 0.0:
            already_measurable = True
            notes.append(f"station already recorded {precip_now:.2f}mm this window")
        else:
            notes.append("no measurable precip at station yet this window")

    if already_measurable:
        return 0.97, "; ".join(notes)

    # Otherwise, fall back to the forecast POP for the remaining periods
    # today, taking the max across remaining daytime/night periods since
    # the contract only needs ONE measurable event, not persistent rain.
    relevant_pops = [
        f.probability_of_precipitation_pct for f in forecast[:2]
        if f.probability_of_precipitation_pct is not None
    ]
    if relevant_pops:
        max_pop = max(relevant_pops) / 100.0
        notes.append(f"forecast max POP over next periods: {max(relevant_pops)}%")
        # POP is not literally "probability of >0 inches at this exact station,"
        # it's probability of measurable precip somewhere in the forecast area —
        # treat it as a noisy proxy, not ground truth, hence no further inflation.
        return max_pop, "; ".join(notes)

    notes.append("no observation or forecast data available")
    return 0.5, "; ".join(notes)  # genuine uncertainty — will almost never clear min edge


def market_implied_probability(yes_price_cents: int) -> float:
    return yes_price_cents / 100.0


# --- Temperature model ---
#
# Same "simple, inspectable" philosophy as the rain model above, extended to
# temperature_high/temperature_low markets — which previously had NO
# calibrated model at all (evaluate_market was being called on them by
# mistake, applying precipitation logic to a temperature question, which is
# meaningless). This fixes that by giving temperature markets their own path.
#
# The forecast NWS gives you (tomorrow's high will be ~85F) is a point
# estimate, not a probability — but NWS's own day-1/day-2 high/low forecasts
# are typically accurate to within a few degrees F. Modeling forecast error
# as roughly Normal(mu=forecast, sigma=FORECAST_STD_DEV_F) turns that point
# estimate into a probability the actual reading falls inside a given
# threshold band — coarse and deliberately inspectable, not fitted to
# historical data (there isn't any yet — that's what paper trading is for).
FORECAST_STD_DEV_F = 4.0


def estimate_temperature_probability(
    observed_temp_f: Optional[float],
    forecast_temp_f: Optional[float],
    threshold_low_f: Optional[float],
    threshold_high_f: Optional[float],
) -> tuple[float, str]:
    """
    Returns (probability the actual reading falls within
    [threshold_low_f, threshold_high_f], rationale). Either threshold bound
    can be None for an open-ended market (e.g. 'above 85F' has no upper
    bound); both None means rules_extractor couldn't parse a usable
    threshold, which returns genuine 0.5 uncertainty rather than guessing.
    """
    notes = []
    if observed_temp_f is not None:
        notes.append(f"current observed temp {observed_temp_f:.0f}F")

    if forecast_temp_f is None:
        notes.append("no forecast temperature available for the relevant period")
        return 0.5, "; ".join(notes)

    if threshold_low_f is None and threshold_high_f is None:
        notes.append("no usable threshold parsed from rules text")
        return 0.5, "; ".join(notes)

    notes.append(f"forecast temp {forecast_temp_f:.0f}F vs threshold "
                 f"[{threshold_low_f if threshold_low_f is not None else '-inf'}, "
                 f"{threshold_high_f if threshold_high_f is not None else '+inf'}], "
                 f"assumed forecast error stddev {FORECAST_STD_DEV_F:.0f}F")

    dist = NormalDist(mu=forecast_temp_f, sigma=FORECAST_STD_DEV_F)
    # +/-0.5F treats a whole-degree threshold as covering its rounding band
    # (e.g. "85F or higher" resolving true at a recorded 85 rounds to
    # covering [84.5, +inf)) — a small, deliberate, documented fudge rather
    # than a silent off-by-one at the boundary.
    if threshold_low_f is not None and threshold_high_f is not None:
        prob = dist.cdf(threshold_high_f + 0.5) - dist.cdf(threshold_low_f - 0.5)
    elif threshold_low_f is not None:
        prob = 1 - dist.cdf(threshold_low_f - 0.5)
    else:
        prob = dist.cdf(threshold_high_f + 0.5)

    prob = max(0.01, min(0.99, prob))
    return prob, "; ".join(notes)


def pick_relevant_forecast_temp_f(measure: str, forecast: list[PrecipForecast]) -> Optional[float]:
    """
    temperature_high markets settle on a daytime high, temperature_low on
    an overnight low — pick the first forecast period matching that, since
    NWS periods alternate day/night and the nearest one is the relevant
    settlement window for a same-day/next-day market.
    """
    want_daytime = measure == "temperature_high"
    for period in forecast:
        if period.is_daytime == want_daytime and period.temperature_f is not None:
            return period.temperature_f
    return None


def evaluate_temperature_market(
    ticker: str,
    yes_price_cents: int,
    rules: MarketRules,
    observation: Optional[StationObservation],
    forecast: list[PrecipForecast],
) -> TradeSignal:
    forecast_temp_f = pick_relevant_forecast_temp_f(rules.measure, forecast)
    observed_temp_f = observation.temperature_f if observation else None

    raw_model_p, rationale = estimate_temperature_probability(
        observed_temp_f, forecast_temp_f, rules.threshold_low_f, rules.threshold_high_f
    )

    model_p, calibration_note = calibration.apply_calibration(
        raw_model_p, rules.station_code, rules.measure
    )
    rationale = f"{rationale}; calibration: {calibration_note}"

    market_p = market_implied_probability(yes_price_cents)

    yes_edge = round((model_p - market_p) * 100)
    no_edge = round(((1 - model_p) - (1 - market_p)) * 100)

    if yes_edge >= no_edge:
        side, edge_cents, price = "yes", yes_edge, yes_price_cents
    else:
        side, edge_cents, price = "no", no_edge, 100 - yes_price_cents

    confidence_note = f" [rules confidence: {rules.confidence}, source: {rules.settlement_source}]"
    return TradeSignal(
        ticker=ticker,
        side=side,
        model_probability=model_p if side == "yes" else 1 - model_p,
        model_probability_yes=model_p,
        market_implied_probability=market_p if side == "yes" else 1 - market_p,
        edge_cents=edge_cents,
        rationale=rationale + confidence_note,
    )


def evaluate_market(
    ticker: str,
    yes_price_cents: int,
    rules: MarketRules,
    observation: Optional[StationObservation],
    forecast: list[PrecipForecast],
) -> TradeSignal:
    raw_model_p, rationale = estimate_precip_probability(
        observation, forecast, rules.trace_counts_as_zero
    )

    # Apply the learned per-station calibration bias (see calibration.py).
    # Early on, before enough settled trades exist, this is a no-op.
    model_p, calibration_note = calibration.apply_calibration(
        raw_model_p, rules.station_code, rules.measure
    )
    rationale = f"{rationale}; calibration: {calibration_note}"

    market_p = market_implied_probability(yes_price_cents)

    # Decide which side has the edge. If model thinks YES is more likely
    # than the market does, the edge is on buying YES; if model thinks NO
    # is more likely, edge is on NO. Price for NO is (100 - yes_price).
    yes_edge = round((model_p - market_p) * 100)
    no_edge = round(((1 - model_p) - (1 - market_p)) * 100)  # == -yes_edge, kept explicit for clarity

    if yes_edge >= no_edge:
        side, edge_cents, price = "yes", yes_edge, yes_price_cents
    else:
        side, edge_cents, price = "no", no_edge, 100 - yes_price_cents

    confidence_note = f" [rules confidence: {rules.confidence}, source: {rules.settlement_source}]"
    return TradeSignal(
        ticker=ticker,
        side=side,
        model_probability=model_p if side == "yes" else 1 - model_p,
        model_probability_yes=model_p,
        market_implied_probability=market_p if side == "yes" else 1 - market_p,
        edge_cents=edge_cents,
        rationale=rationale + confidence_note,
    )
