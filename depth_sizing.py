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


def max_contracts_within_slippage(ask_levels: list[tuple[int, int]], max_slippage_cents: int) -> int:
    """
    How many contracts can be bought while keeping the average fill price
    within max_slippage_cents of the best/cheapest quote — for strategies
    that don't carry a real probability estimate to run
    find_max_profitable_size's profitability search against (favorites,
    always_trade), but should still respect the same price-impact
    discipline as the profit-aware strategies do, just without a
    profitability gate on top of it.
    """
    if not ask_levels:
        return 0
    best_price = ask_levels[0][0]
    total_cost = 0
    filled = 0
    for price_cents, level_count in ask_levels:
        candidate_cost = total_cost + price_cents * level_count
        candidate_filled = filled + level_count
        candidate_avg = candidate_cost / candidate_filled
        if candidate_avg - best_price > max_slippage_cents:
            break  # stop at the level BEFORE this one pushed the average out of tolerance
        total_cost, filled = candidate_cost, candidate_filled
    return filled


def find_max_profitable_size(ask_levels: list[tuple[int, int]], fee_fn, model_probability: float,
                              max_contracts_cap: int, min_net_edge_cents: int,
                              max_slippage_cents: int | None = None) -> FillEstimate | None:
    """
    The actual "throw $1 vs throw $20" search: tries increasing sizes,
    stops at the largest one whose NET (post-fee, real-fill-price) expected
    edge is still positive above min_net_edge_cents. Returns None if not
    even the smallest step clears the bar.

    max_slippage_cents (optional): a SEPARATE stopping condition from the
    profitability check above — caps how far the average fill price is
    allowed to drift from the best/cheapest quoted price (ask_levels[0]),
    regardless of whether a bigger size would still be nominally
    profitable. This is the more precise version of "don't chase a big
    position and hurt your own fill" — a fixed contract-count cap doesn't
    know whether it's walking through a deep, evenly-priced book (where
    slippage barely moves) or a thin one (where it moves fast), but
    slippage measures the actual thing being protected against directly.
    An aggressive risk tier passing a larger value here is deliberately
    tolerating more price impact in exchange for size, same as it already
    tolerates smaller min_net_edge_cents.

    fee_fn: a callable(contracts, price_cents) -> fee_cents, e.g.
    fees.taker_fee_cents, passed in rather than imported directly so this
    module stays independently testable.

    This is a straightforward increasing search, not a binary search —
    order book levels are few enough (tens, not thousands) that this is
    plenty fast, and a simple linear walk is easier to verify correct than
    a cleverer search would be.
    """
    best: FillEstimate | None = None
    best_price_cents = ask_levels[0][0] if ask_levels else None
    # max_contracts_cap comes from the bankroll/position-pct math and knows
    # nothing about how much is actually sitting in the book — clamping it
    # to the real total depth first means the step below is always sized to
    # what's realistically fillable. Without this, a thin book (say 35
    # contracts total) against a large cap (say 750, from a healthy
    # bankroll) produces a step of 750//20=37 — bigger than the ENTIRE
    # book — so the very first size tried already exceeds available depth,
    # the loop breaks immediately, and a genuinely profitable 35-contract
    # fill is missed entirely and this returns None instead. Found while
    # extending this to shadow strategies (many more callers exercising
    # thin books), but this same gap has been live in the main bot's real
    # order-sizing path the whole time — it just needed a book thinner than
    # bankroll/20 to trigger, which is rarer there than across 16+ shadow
    # strategies each with their own bankroll.
    total_book_depth = sum(count for _, count in ask_levels)
    max_contracts_cap = min(max_contracts_cap, total_book_depth)
    if max_contracts_cap < 1:
        return None
    # Step through in reasonable increments rather than every integer —
    # every contract count would be needless precision for a decision this
    # coarse-grained anyway.
    step = max(1, max_contracts_cap // 20)

    size = step
    while size <= max_contracts_cap:
        fill = estimate_fill(ask_levels, size)
        if fill.contracts_fillable < size:
            break  # book exhausted before reaching this size — no point trying bigger

        if max_slippage_cents is not None and best_price_cents is not None:
            if fill.avg_price_cents - best_price_cents > max_slippage_cents:
                break  # this size already drifts further from the best quote
                        # than this tier tolerates — bigger only drifts further

        expected_value_cents = model_probability * 100 - fill.avg_price_cents
        fee_cents = fee_fn(fill.contracts_fillable, round(fill.avg_price_cents))
        net_edge_total = expected_value_cents * fill.contracts_fillable - fee_cents

        if net_edge_total >= min_net_edge_cents:
            best = fill  # this size still clears the bar — keep it, try bigger
        else:
            break  # this size no longer profitable — bigger will only be worse

        size += step

    return best
