"""
Re-run the buy-at-X test against REAL TRADES, not quoted asks.

Every previous execution test assumed that a quoted ask was a price you
could have taken. That is an assumption, and it was built on candle
volume as a proxy for liquidity -- which is exactly the proxy the trade
log replaces.

THE RULE HERE NEEDS NO ASSUMPTION. A trade in the log is a transaction
that demonstrably happened, at a price somebody actually paid, for a
size that actually cleared. So: buy at the first real trade printing at
or below X cents, take that trade's own price, and cap size at what
that print cleared. Hold to settlement. Fees charged at entry;
settlement is free, since 0.07*p*(1-p) is zero at 0 and 100.

This cannot overstate fillability the way a quoted ask can. If the
answer is still negative it is negative for economic reasons rather
than for want of data, and that distinction is the whole point of
having fetched 11.8 million trades.

One caveat stated up front: joining a print does not prove OUR order
would have been the one filled. Somebody else was on the other side.
It is an upper bound on realism, not a guarantee -- but it is a far
tighter bound than "the ask said 20c".
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
from collections import defaultdict

import fees

THRESHOLDS = [10, 20, 30, 40, 50, 60, 70, 80, 90]


def fp(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def load(pattern):
    """{ticker: (result, [(time, yes_price_cents, size), ...])}"""
    out = {}
    for path in sorted(glob.glob(pattern)):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("result") not in ("yes", "no"):
                    continue
                prints = []
                for t in rec.get("trades") or []:
                    px = fp(t.get("yes_price_dollars"), -1) * 100
                    size = fp(t.get("count_fp"))
                    if px < 0 or size <= 0:
                        continue
                    prints.append((t.get("created_time", ""),
                                   int(round(px)), size))
                if prints:
                    prints.sort(key=lambda p: p[0])
                    out[rec["ticker"]] = (rec["result"], prints)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="data/trades")
    ap.add_argument("--series", default="KXRAINNYC")
    args = ap.parse_args()

    books = load(os.path.join(args.dir, args.series, "*.jsonl.gz"))
    if not books:
        print("no trade files found")
        return 1

    total_prints = sum(len(p) for _r, p in books.values())
    base = sum(1 for r, _p in books.values() if r == "yes")
    print(f"{args.series}: {len(books):,} markets, {total_prints:,} real "
          f"trades")
    print(f"base rate {100.0*base/len(books):.1f}% YES\n")

    print(f"{'buy at':<9} {'fills':>7} {'YES%':>7} {'95% CI':>14} "
          f"{'avg px':>7} {'avg size':>9} {'P&L/contract':>13} {'total':>11}")
    print("-" * 82)

    for th in THRESHOLDS:
        trades = []
        for ticker, (result, prints) in books.items():
            for when, px, size in prints:
                if px <= th:
                    fee = fees.taker_fee_cents(1, px)
                    pnl = (100 if result == "yes" else 0) - px - fee
                    trades.append((1 if result == "yes" else 0, px, size,
                                   pnl))
                    break
        n = len(trades)
        if n < 20:
            print(f"<= {th:>3}c   {n:>7,}   (too few)")
            continue
        hits = sum(t[0] for t in trades)
        lo, hi = wilson(hits, n)
        avg_px = sum(t[1] for t in trades) / n
        avg_sz = sum(t[2] for t in trades) / n
        per = sum(t[3] for t in trades) / n
        tot = sum(t[3] * min(t[2], 100) for t in trades) / 100.0
        mark = "  <-- profitable" if per > 0 else ""
        print(f"<= {th:>3}c   {n:>7,} {100.0*hits/n:>6.1f}% "
              f"{100*lo:>6.1f}-{100*hi:<6.1f} {avg_px:>6.1f}c "
              f"{avg_sz:>9,.0f} {per:>+12.2f}c {tot:>+10,.0f}${mark}")

    print("\n  Entry joins a REAL PRINT at that print's own price and size.")
    print("  No quoted-ask assumption anywhere. 'total' sizes each entry")
    print("  at the print's own size capped at 100 contracts.")
    print("\n  Still an upper bound on realism: joining a print does not")
    print("  prove our order would have been the one filled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
