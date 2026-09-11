"""
Candidate generators for strategies OTHER than the calibrated model in
strategy.py. Each returns a candidate trade (side, price, edge) or None if
that strategy sees nothing to do on this market. These get combined with
risk_manager.RiskPreset + RiskManager in shadow.py to size and log
paper trades per strategy, so every strategy is directly comparable.

Being honest about each one:

- arbitrage: real, structurally sound — checks whether buying YES and NO
  together costs less than the guaranteed $1 payout. In practice this is
  RARE on Kalshi specifically because their order book links the two sides
  mechanically (buying NO is the same trade as selling YES), so a
  well-functioning market rarely lets this gap open. A strategy that mostly
  finds nothing is not a bug — the chart showing a flat, rarely-firing line
  IS the honest result.

- favorites_baseline: deliberately naive. Buys YES whenever the market
  already prices something as very likely, no model involved. This isn't
  a strategy I'd recommend — risking 90c to win 10c needs you to be right
  more than 90% of the time just to break even, and "the market already
  thinks it's likely" is not the same as an edge. It exists specifically
  as a dumb baseline: if the calibrated model can't beat this over time,
  the model isn't earning its complexity.

- longshot: the same calibrated model and calibration bias as the main
  strategy, but restricted to cheap contracts (a few cents) where a small
  stake has a large payout ratio if right. Different contract selection,
  not just a different position size.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from strategy import TradeSignal


@dataclass
class StrategyCandidate:
    side: str
    price_cents: int
    edge_cents: int
    rationale: str


def arbitrage_candidate(yes_ask: Optional[int], no_ask: Optional[int]) -> Optional[StrategyCandidate]:
    if yes_ask is None or no_ask is None:
        return None
    total_cost = yes_ask + no_ask
    if total_cost >= 100:
        return None  # no arbitrage — this is the normal case almost always
    # Buying both sides guarantees a $1 (100c) payout no matter which
    # resolves true, for a cost of total_cost. Profit is riskless (ignoring
    # fees/slippage) at (100 - total_cost) cents per pair.
    guaranteed_profit = 100 - total_cost
    return StrategyCandidate(
        side="both",  # special-cased in shadow.py: buys one unit of each side
        price_cents=total_cost,
        edge_cents=guaranteed_profit,
        rationale=f"arbitrage: yes_ask={yes_ask}c + no_ask={no_ask}c = {total_cost}c < 100c",
    )


def favorites_candidate(yes_price_cents: int, threshold: int = 90) -> Optional[StrategyCandidate]:
    if yes_price_cents >= threshold:
        return StrategyCandidate(
            side="yes", price_cents=yes_price_cents, edge_cents=0,
            rationale=f"naive baseline: price {yes_price_cents}c >= threshold {threshold}c, no model used",
        )
    return None


def longshot_candidate(signal: Optional[TradeSignal], price_cents: int,
                        min_price: int = 2, max_price: int = 15) -> Optional[StrategyCandidate]:
    """Same side/edge the calibrated model already picked (see strategy.py),
    but only kept as a candidate when the actual traded side is priced in
    the cheap 'longshot' band — small stake, large payout ratio if right."""
    if not signal:
        return None
    if not (min_price <= price_cents <= max_price):
        return None
    return StrategyCandidate(
        side=signal.side, price_cents=price_cents, edge_cents=signal.edge_cents,
        rationale=f"longshot: {signal.rationale} [price {price_cents}c in band {min_price}-{max_price}c]",
    )


def depth_imbalance_candidate(
    yes_ask: Optional[int], no_ask: Optional[int],
    yes_bids: Optional[list[tuple[int, int]]], no_bids: Optional[list[tuple[int, int]]],
    min_total_depth: int = 20, min_imbalance_ratio: float = 3.0,
) -> Optional[StrategyCandidate]:
    """
    A pure market-microstructure signal, deliberately independent of the
    weather model entirely: heavy resting buy-side depth on one side of
    the book relative to the other can reflect informed positioning, in
    the same direction limit-order-book imbalance is commonly used as a
    short-term price predictor in market microstructure research. Trades
    WITH the imbalance (the side with more resting depth), crossing the
    spread at that side's current ask to actually enter, same as every
    other directional strategy here.

    Requires BOTH sides' real depth data to be present — fails CLOSED,
    not open, on missing data. That's a genuinely different posture from
    most strategies here, since without real numbers on both sides
    there's no signal to compute at all, not just a less-good one.

    min_total_depth guards against a thin, quiet book producing a
    meaningless ratio from tiny absolute numbers (5 contracts vs 1 is a
    "5x imbalance" that means nothing on an illiquid market).
    min_imbalance_ratio is how lopsided the two sides need to be before
    this counts as a real signal rather than ordinary noise.
    """
    if not yes_bids or not no_bids:
        return None
    yes_depth = sum(size for _, size in yes_bids)
    no_depth = sum(size for _, size in no_bids)
    if yes_depth + no_depth < min_total_depth:
        return None
    if yes_depth == 0 or no_depth == 0:
        # A truly empty side is more likely just unquoted right now than a
        # genuine, tradeable imbalance signal.
        return None

    if yes_depth >= no_depth * min_imbalance_ratio:
        side, price = "yes", yes_ask
    elif no_depth >= yes_depth * min_imbalance_ratio:
        side, price = "no", no_ask
    else:
        return None  # not lopsided enough to count as a real signal

    if price is None:
        return None
    return StrategyCandidate(
        side=side, price_cents=price, edge_cents=0,
        rationale=f"depth imbalance: yes_bid_depth={yes_depth} no_bid_depth={no_depth} "
                  f"({side}-heavy, ratio >= {min_imbalance_ratio}x) — pure order-book signal, "
                  f"no probability model used",
    )
