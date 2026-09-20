"""Does the favourite-longshot gap survive the spread and the fee?

The mid-price calibration on 43,945 weather markets shows the classic
pattern: longshots overpriced, favourites underpriced, with gaps of one
to seven cents. That is a real statistical finding and NOT yet a
tradeable one, because a gap measured at the MIDPOINT is not a gap you
can capture.

To buy YES you pay the ASK. To buy NO you pay 100 minus the BID. At 24h
before close the median weather spread is 10c, which is wider than
every edge in the table. This computes the expected profit of actually
taking the trade:

    buy YES at the ask   E = 100*q - ask - fee(ask)
    buy NO  at 100-bid   E = 100*(1-q) - (100-bid) - fee(100-bid)

where q is the realised YES rate of that bucket. Settlement itself is
free -- a contract resolving at 0 or 100 pays no fee, since
0.07*p*(1-p) is zero at both ends -- so the entry fee is the whole
cost.

One observation per market, and the bucket's q is computed from the
same markets being traded, which is optimistic: a live strategy would
have to know q in advance. If the answer is negative even under that
generosity, it is definitively negative.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from contextlib import closing

import fees
from config import SETTINGS

EDGES = [0, 5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 85, 90, 95, 100]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def bucket_of(px: float):
    for i in range(len(EDGES) - 1):
        if EDGES[i] <= px < EDGES[i + 1]:
            return i
    return None


SQL = """
    SELECT m.result, p.yes_bid_cents bid, p.yes_ask_cents ask
      FROM historical_markets m
      JOIN historical_price_points p ON p.ticker = m.ticker
     WHERE m.result IN ('yes','no') AND m.close_time IS NOT NULL
       AND p.ts = (SELECT q.ts FROM historical_price_points q
                    WHERE q.ticker = m.ticker
                      AND q.ts <= strftime('%s', m.close_time) - ?
                    ORDER BY q.ts DESC LIMIT 1)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon-hours", type=float, default=6.0)
    ap.add_argument("--max-spread", type=int, default=10)
    args = ap.parse_args()

    buckets: dict[int, list] = {}
    with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
        for result, bid, ask in conn.execute(
                SQL, (int(args.horizon_hours * 3600),)):
            if bid is None or ask is None:
                continue
            if not (0 < bid <= 100 and 0 < ask <= 100 and ask >= bid):
                continue
            if ask - bid > args.max_spread:
                continue
            mid = (bid + ask) / 2.0
            b = bucket_of(mid)
            if b is None:
                continue
            buckets.setdefault(b, []).append(
                (1 if result == "yes" else 0, int(bid), int(ask)))

    print(f"horizon {args.horizon_hours:g}h, spread <= {args.max_spread}c, "
          f"one observation per market")
    print(f"{'mid':<9} {'n':>6} {'actual':>7} {'avg ask':>8} "
          f"{'E[buy YES]':>11} {'E[buy NO]':>10}   best")
    print("-" * 66)

    total_best = 0.0
    for b in sorted(buckets):
        rows = buckets[b]
        n = len(rows)
        if n < 20:
            continue
        k = sum(r[0] for r in rows)
        q = k / n
        lo, hi = wilson(k, n)
        avg_ask = sum(r[2] for r in rows) / n
        avg_bid = sum(r[1] for r in rows) / n

        e_yes = 100 * q - avg_ask - fees.taker_fee_cents(1, round(avg_ask))
        no_cost = 100 - avg_bid
        e_no = 100 * (1 - q) - no_cost - fees.taker_fee_cents(
            1, round(no_cost))
        best = max(e_yes, e_no)
        total_best += max(best, 0.0) * n

        label = f"{EDGES[b]}-{EDGES[b+1]}"
        mark = "  <-- positive" if best > 0 else ""
        print(f"{label:<9} {n:>6,} {100*q:>6.1f}% {avg_ask:>7.1f}c "
              f"{e_yes:>+10.2f}c {e_no:>+9.2f}c{mark}")

    print("\n  E = expected cents per contract, paying the real ask or")
    print("  the real bid, net of the entry fee. Settlement is free.")
    print("  q is taken from the SAME markets being priced, which no")
    print("  live strategy could know in advance -- this is the")
    print("  optimistic bound, not an achievable return.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
