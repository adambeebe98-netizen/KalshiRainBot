"""
Answers the question from our conversation directly: "throw $1 if only $1
is profitable, throw $20 if $20 is still profitable" — this is what makes
that distinction real, since flat fee-rate math alone proved size doesn't
matter at a FIXED price. What actually changes with size is the price
itself, once you buy past what's sitting at the best level.

To buy YES, you're matched against resting NO bids (since Kalshi's book is
bids-only — see kalshi_client.get_orderbook_levels). Each NO bid at price Y
represents an available YES ask at (100 - Y). Walking the NO bid levels
from best (highest Y, i.e. cheapest YES ask) downward gives the true
volume-weighted price for filling increasing size.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FillEstimate:
    contracts_fillable: int
    total_cost_cents: int
    avg_price_cents: float
    exhausted_book: bool  # True if we ran out of visible depth before filling the request


def implied_ask_levels(opposite_side_bids: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """
    Converts the OPPOSITE side's bid levels (already sorted best-first, i.e.
    highest bid first) into this side's implied ask levels, in the order
    you'd actually walk them to fill a buy: cheapest ask first.

    Example: to find YES asks, pass in NO bid levels. The highest NO bid
    (say 82c) implies the cheapest YES ask (18c) — so this naturally comes
    out in "best ask first" order once inverted, no re-sorting needed since
    inverting (100 - price) on a descending list produces an ascending one.
    """
    return [(100 - price, count) for price, count in opposite_side_bids]


def estimate_fill(ask_levels: list[tuple[int, int]], target_contracts: int) -> FillEstimate:
    """
    Walks ask_levels (best/cheapest first) accumulating contracts until
    target_contracts is filled or the book runs out. This is what turns
    "quoted top-of-book price" into "the price you'd actually pay."
    """
    remaining = target_contracts
    total_cost = 0
    filled = 0

    for price_cents, level_count in ask_levels:
        if remaining <= 0:
            break
        take = min(remaining, level_count)
        total_cost += take * price_cents
        filled += take
        remaining -= take

    avg_price = (total_cost / filled) if filled else 0.0
    return FillEstimate(
        contracts_fillable=filled,
        total_cost_cents=total_cost,
        avg_price_cents=avg_price,
        exhausted_book=(remaining > 0),
    )


def find_max_profitable_size(ask_levels: list[tuple[int, int]], fee_fn, model_probability: float,
                              max_contracts_cap: int, min_net_edge_cents: int) -> FillEstimate | None:
    """
    The actual "throw $1 vs throw $20" search: tries increasing sizes,
    stops at the largest one whose NET (post-fee, real-fill-price) expected
    edge is still positive above min_net_edge_cents. Returns None if not
    even the smallest step clears the bar.

    fee_fn: a callable(contracts, price_cents) -> fee_cents, e.g.
    fees.taker_fee_cents, passed in rather than imported directly so this
    module stays independently testable.

    This is a straightforward increasing search, not a binary search —
    order book levels are few enough (tens, not thousands) that this is
    plenty fast, and a simple linear walk is easier to verify correct than
    a cleverer search would be.
    """
    best: FillEstimate | None = None
    # Step through in reasonable increments rather than every integer —
    # every contract count would be needless precision for a decision this
    # coarse-grained anyway.
    step = max(1, max_contracts_cap // 20)

    size = step
    while size <= max_contracts_cap:
        fill = estimate_fill(ask_levels, size)
        if fill.contracts_fillable < size:
            break  # book exhausted before reaching this size — no point trying bigger

        expected_value_cents = model_probability * 100 - fill.avg_price_cents
        fee_cents = fee_fn(fill.contracts_fillable, round(fill.avg_price_cents))
        net_edge_total = expected_value_cents * fill.contracts_fillable - fee_cents

        if net_edge_total >= min_net_edge_cents:
            best = fill  # this size still clears the bar — keep it, try bigger
        else:
            break  # this size no longer profitable — bigger will only be worse

        size += step

    return best
