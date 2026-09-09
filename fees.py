"""
Kalshi's real taker fee formula (per their published fee schedule, last
revised July 7, 2026 — confirmed against multiple independent current
sources as of Sept 2026, current as of this writing, but re-verify
against https://kalshi.com/fee-schedule periodically since Kalshi has
changed this before and can again):

    fee = round_up( 0.07 * contracts * price * (1 - price) )  dollars,
    capped at $1.75 PER ORDER regardless of size.

where price is in dollars (0.00-1.00). The formula peaks at 50c (a coin
flip costs the most to trade) and shrinks toward both extremes. Without
the cap, fee scales linearly with contract count forever — at 50c, 100
contracts already hits exactly $1.75 (0.07*100*0.5*0.5), so ANY order
past that size at a similar price is charged the real capped $1.75, not
whatever the uncapped formula would keep growing to (200 contracts at
50c would compute to $3.50 uncapped, overstating the real fee by 2x).
This matters more now than it used to: tonight's depth-aware sizing work
enables much larger real positions (arbitrage scaling to 100+ contracts,
bracket sets similarly) than this bot ever traded before — without the
cap, those larger trades would get rejected as "edge doesn't survive
fees" using an overstated fee that isn't what Kalshi would actually
charge, when the real, capped fee might well leave them profitable.

This is the TAKER fee — charged when your order crosses the spread and
fills immediately, which is what a limit order placed at the current ask
effectively does. Maker fees (a limit order that rests and waits, live
since Kalshi turned them on August 19, 2026) are typically much lower —
but resting orders risk not filling before a real mispricing corrects
itself, so this module models the conservative (taker) case everywhere.
If you later add maker-order logic, model fees separately for that path
rather than assuming the discount applies.

Also out of scope here: Kalshi uses a HIGHER multiplier than 0.07 for
certain premium categories (e.g. crypto) — this bot only trades weather
markets, which use the standard 0.07 rate, so that's not modeled.
"""
from __future__ import annotations

import math

MAX_FEE_CENTS_PER_ORDER = 175  # Kalshi's per-order cap, in cents ($1.75)


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
    fee_cents = min(fee_cents, MAX_FEE_CENTS_PER_ORDER)
    return max(0, fee_cents)


if __name__ == "__main__":
    # Sanity checks against Kalshi's own published examples — run this file
    # directly (`python fees.py`) any time you touch the formula.
    assert taker_fee_cents(100, 10) == 63, "100 contracts @ 10c should cost 63c in fees"
    assert taker_fee_cents(100, 50) == 175, "100 contracts @ 50c should cost 175c ($1.75) in fees"
    assert taker_fee_cents(200, 50) == 175, "200 contracts @ 50c should still cap at 175c, not double to 350c"
    print("fee formula checks passed")

