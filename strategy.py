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
