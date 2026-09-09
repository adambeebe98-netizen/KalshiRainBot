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


def find_max_bracket_size(leg_ask_levels: list[list[tuple[int, int]]], fee_fn,
                           min_net_edge_cents: int) -> FillEstimate | None:
    """
    Sizing for bracket-set arbitrage (see shadow.evaluate_bracket_set):
    buying one contract of the same side (usually NO) across every leg of
    a full bracket set guarantees a payout of 100c * (num_legs - 1) per
    set, since exactly one leg's outcome is true and every OTHER leg pays.
    Same reasoning as find_max_arbitrage_size for why there's no
    probability estimate and no slippage limit here — this isn't a bet, so
    the only thing that should stop the size from growing is real
    profitability at real depth.

    Walks ALL legs together for increasing set counts — bottlenecked by
    whichever leg has the SHALLOWEST depth, since every set needs one
    contract in EVERY included leg, not just some of them.
    """
    num_legs = len(leg_ask_levels)
    if num_legs < 2:
        return None
    total_depth = min(sum(c for _, c in levels) for levels in leg_ask_levels)
    if total_depth < 1:
        return None
    step = max(1, total_depth // 20)

    best: FillEstimate | None = None
    size = step
    while size <= total_depth:
        fills = [estimate_fill(levels, size) for levels in leg_ask_levels]
        if any(f.contracts_fillable < size for f in fills):
            break  # some leg ran out of depth before the others did

        total_cost = sum(f.total_cost_cents for f in fills)
        gross_payout = 100 * size * (num_legs - 1)
        fee_cents = sum(fee_fn(size, round(f.avg_price_cents)) for f in fills)
        net_edge_total = gross_payout - total_cost - fee_cents

        if net_edge_total >= min_net_edge_cents:
            best = FillEstimate(
                contracts_fillable=size,
                total_cost_cents=total_cost,
                avg_price_cents=total_cost / size,  # combined avg cost per set across all legs
                exhausted_book=False,
            )
        else:
            break

        size += step

    return best


def find_max_arbitrage_size(yes_ask_levels: list[tuple[int, int]], no_ask_levels: list[tuple[int, int]],
                             fee_fn, min_net_edge_cents: int) -> FillEstimate | None:
    """
    Sizing for dutch-book arbitrage: buying one YES and one NO contract on
    the SAME market together guarantees exactly 100c total payout per pair,
    regardless of which side actually resolves true — there's no
    probability estimate involved at all, unlike a directional bet. That's
    the whole reason this is a SEPARATE function from
    find_max_profitable_size rather than reusing it with model_probability
    plugged in: there's no max_slippage_cents concept here on purpose — as
    long as walking deeper into BOTH books still nets a profit after real
    fill prices and fees, there's no bet-uncertainty reason to stop early
    on a fixed price-drift budget. The profitability check itself is the
    only limit that makes sense.

    Walks BOTH sides together for increasing pair counts (need real depth
    on the yes-ask side AND the no-ask side to fill each pair), same
    increasing-search pattern as find_max_profitable_size, stopping at the
    largest pair count whose combined net profit (100c * pairs, minus both
    legs' real walked cost, minus both legs' real fees) still clears
    min_net_edge_cents.
    """
    total_depth = min(sum(c for _, c in yes_ask_levels), sum(c for _, c in no_ask_levels))
    if total_depth < 1:
        return None
    step = max(1, total_depth // 20)

    best: FillEstimate | None = None
    size = step
    while size <= total_depth:
        yes_fill = estimate_fill(yes_ask_levels, size)
        no_fill = estimate_fill(no_ask_levels, size)
        if yes_fill.contracts_fillable < size or no_fill.contracts_fillable < size:
            break  # one side ran out of depth before the other -- no point trying bigger

        total_cost = yes_fill.total_cost_cents + no_fill.total_cost_cents
        gross_payout = 100 * size
        fee_cents = (fee_fn(size, round(yes_fill.avg_price_cents)) +
                     fee_fn(size, round(no_fill.avg_price_cents)))
        net_edge_total = gross_payout - total_cost - fee_cents

        if net_edge_total >= min_net_edge_cents:
            best = FillEstimate(
                contracts_fillable=size,
                total_cost_cents=total_cost,
                avg_price_cents=total_cost / size,  # combined yes+no avg cost per pair
                exhausted_book=False,
            )
        else:
            break  # this size no longer profitable -- bigger will only be worse

        size += step

    return best


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
