"""
Buy the moment the price touches X, hold to settlement. Does it work?

This is the path version of the calibration question, and it is a
strictly better one to ask, because it is EXECUTABLE. "A 20c contract
resolves YES 20% of the time" is a statement about a snapshot nobody
trades. "Buy whenever the ask first drops to 20c and hold" is a rule a
bot can follow, and it either makes money or it does not.

THE SELECTION EFFECT IS THE WHOLE STORY HERE, so it is worth being
precise. "Markets that ever touched 20c" is NOT "markets priced at
20c". A contract heading for 100 passes through 20 on the way up; a
contract heading for 0 passes through it on the way down. The touch set
contains both, in whatever mixture the price paths happen to produce --
so the YES rate of that set is not the same number as the snapshot
calibration, and should not be expected to match it.

What makes the test honest anyway is that it never needs to know which
kind it caught: it pays the real ask at the moment of the touch, holds
to settlement, and reports the realised profit.

RULES, chosen to be pessimistic where there is a choice:

  * buy at the ASK actually quoted in that candle, not the midpoint
  * require a live book (bid > 0) -- an ask with nothing bid behind it
    is a price nobody was making, the same reason three soccer series
    were dropped from live collection
  * one entry per market per threshold, at the FIRST touch, so a market
    that oscillates does not get counted repeatedly
  * hold to settlement. No exit logic, no stop, nothing to tune --
    a rule with no parameters cannot be overfitted to this data
  * entry fee charged; settlement is free, since 0.07*p*(1-p) is zero
    at both 0 and 100

Every threshold tested here is a separate trial and belongs in the
registry alongside the existing 960. Nine thresholds times a few
market families is not one test, and the best-looking cell of thirty is
not a discovery.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict
from contextlib import closing

import fees
from config import SETTINGS

THRESHOLDS = [10, 20, 30, 40, 50, 60, 70, 80, 90]

FAMILIES = {
    "daily rain": ("precipitation_daily",),
    "monthly rain": ("precipitation_monthly",),
    "temperature": ("temperature_high", "temperature_low"),
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def load_paths(measures, db_path=None):
    """{ticker: (result, [(ts, bid, ask), ...] in time order)}"""
    q = ",".join("?" * len(measures))
    sql = f"""
        SELECT m.ticker, m.result, p.ts, p.yes_bid_cents, p.yes_ask_cents
          FROM historical_markets m
          JOIN historical_price_points p ON p.ticker = m.ticker
         WHERE m.result IN ('yes','no') AND m.measure IN ({q})
         ORDER BY m.ticker, p.ts
    """
    paths: dict[str, tuple[str, list]] = {}
    with closing(sqlite3.connect(db_path or SETTINGS.db_path)) as conn:
        for ticker, result, ts, bid, ask in conn.execute(sql, measures):
            if bid is None or ask is None:
                continue
            if not (0 < bid <= 100 and 0 < ask <= 100 and ask >= bid):
                continue
            entry = paths.setdefault(ticker, (result, []))
            entry[1].append((ts, int(bid), int(ask)))
    return paths


def run(paths, threshold: int):
    """First touch of `threshold`; take BOTH sides of the same moment.

    Buying YES pays the ask. Buying NO pays 100 minus the bid, because
    to be short YES you buy the NO contract and cross that spread
    instead. Both are priced from the SAME candle, so the comparison is
    the same instant seen from two directions -- and the spread is
    charged honestly to each.

    The mirror matters: if touching a low price predicts NO strongly
    enough, the losing YES trade implies a winning NO trade. Whether it
    survives paying 100-bid instead of receiving the ask is exactly
    what has to be computed rather than assumed.
    """
    trades = []
    for ticker, (result, path) in paths.items():
        for ts, bid, ask in path:
            if ask <= threshold:
                won_yes = 1 if result == "yes" else 0

                fee_y = fees.taker_fee_cents(1, ask)
                pnl_yes = (100 if won_yes else 0) - ask - fee_y

                no_cost = 100 - bid
                fee_n = fees.taker_fee_cents(1, no_cost)
                pnl_no = (100 if not won_yes else 0) - no_cost - fee_n

                trades.append((won_yes, ask, pnl_yes, no_cost, pnl_no))
                break
    return trades


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db")
    args = ap.parse_args()

    for label, measures in FAMILIES.items():
        paths = load_paths(list(measures), args.db)
        settled_yes = sum(1 for _t, (r, _p) in paths.items() if r == "yes")
        print(f"\n{'=' * 76}")
        print(f"{label.upper()}   {len(paths):,} markets with a quoted path"
              f"   base rate {100.0*settled_yes/max(len(paths),1):.1f}% YES")
        print("=" * 76)
        print(f"{'touch':<8} {'trades':>7} {'YES%':>7} {'95% CI':>14} "
              f"{'ask':>6} {'buy YES':>10} {'NO cost':>8} {'buy NO':>10}")
        print("-" * 76)

        for th in THRESHOLDS:
            trades = run(paths, th)
            n = len(trades)
            if n < 20:
                print(f"<= {th:>3}c   {n:>7,}   (too few)")
                continue
            hits = sum(t[0] for t in trades)
            lo, hi = wilson(hits, n)
            avg_ask = sum(t[1] for t in trades) / n
            per_yes = sum(t[2] for t in trades) / n
            avg_no = sum(t[3] for t in trades) / n
            per_no = sum(t[4] for t in trades) / n
            mark = ""
            if per_no > 0:
                mark = "  <-- NO profitable"
            elif per_yes > 0:
                mark = "  <-- YES profitable"
            print(f"<= {th:>3}c   {n:>7,} {100.0*hits/n:>6.1f}% "
                  f"{100*lo:>6.1f}-{100*hi:<6.1f} {avg_ask:>5.1f}c "
                  f"{per_yes:>+9.2f}c {avg_no:>7.1f}c {per_no:>+9.2f}c{mark}")

    print("\n  Entry at the real quoted ask, entry fee charged, held to")
    print("  settlement with no exit rule. 'YES%' is the rate for markets")
    print("  that TOUCHED the level, which is a different population from")
    print("  markets PRICED at it -- a contract on its way to 100 passes")
    print("  through every level below it.")
    print("\n  Each row is a separate trial. Nine levels across three")
    print("  families is 27 trials, and the best of 27 looks good by")
    print("  chance alone -- these belong in the registry with the other")
    print("  960 before any of them is believed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
