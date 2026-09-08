"""
Kalshi's real taker fee formula (per their published fee schedule, current
as of ~Sept 2026 — this WILL change over time; re-verify against
https://kalshi.com/fee-schedule periodically rather than trusting this
forever):

    fee = round_up( 0.07 * contracts * price * (1 - price) )  dollars

where price is in dollars (0.00-1.00). The formula peaks at 50c (a coin
flip costs the most to trade) and shrinks toward both extremes.

This is the TAKER fee — charged when your order crosses the spread and
fills immediately, which is what a limit order placed at the current ask
effectively does. Maker fees (a limit order that rests and waits) are
typically much lower, often rounding to $0 for small size — but resting
orders risk not filling before a real mispricing corrects itself, so this
module models the conservative (taker) case everywhere. If you later add
maker-order logic, model fees separately for that path rather than
assuming the discount applies.
"""
from __future__ import annotations

import math


def taker_fee_cents(contracts: int, price_cents: int) -> int:
    """Fee in cents for a single order of `contracts` contracts at
    `price_cents` (1-99, or higher for a synthetic multi-contract "set"
    cost — the formula itself doesn't care, it's just arithmetic)."""
    if contracts <= 0 or price_cents <= 0:
        return 0
    p = price_cents / 100.0
    fee_dollars = 0.07 * contracts * p * (1 - p)
    # Round UP to the nearest cent — guard against float representation
    # putting an exact cent value a hair under its true value.
    fee_cents = math.ceil(fee_dollars * 100 - 1e-9)
    return max(0, fee_cents)


if __name__ == "__main__":
    # Sanity checks against Kalshi's own published examples — run this file
    # directly (`python fees.py`) any time you touch the formula.
    assert taker_fee_cents(100, 10) == 63, "100 contracts @ 10c should cost 63c in fees"
    assert taker_fee_cents(100, 50) == 175, "100 contracts @ 50c should cost 175c ($1.75) in fees"
    print("fee formula checks passed")
