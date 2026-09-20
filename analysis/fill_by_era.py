"""Is the trade-based edge real, or is it just 2021?

The trade test says buying a print at or below 10c returns +5.08c a
contract. The snapshot calibration on the same family says longshots
are OVERpriced. The two disagree -- but they were not measured on the
same years. The trade archive reaches back to 2021; the candle data
this project has always used starts in 2025.

A young exchange being inefficient and then not being inefficient is
the single most ordinary explanation for a backtest that works. It is
also the most dangerous, because the profit is real in the data and
entirely unavailable now.

So: the same rule, split by year. If the edge is flat across the years
it is worth taking seriously. If it lives in 2021-2022 and dies, that
is a museum piece.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
from collections import defaultdict

import fees


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


ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--series", default="KXRAINNYC")
ap.add_argument("--threshold", type=int, default=10)
args = ap.parse_args()

by_year = defaultdict(list)
for path in sorted(glob.glob(f"data/trades/{args.series}/*.jsonl.gz")):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("result") not in ("yes", "no"):
                continue
            trades = sorted(rec.get("trades") or [],
                            key=lambda t: t.get("created_time", ""))
            year = (rec.get("close_time") or "")[:4]
            for t in trades:
                px = fp(t.get("yes_price_dollars"), -1) * 100
                if 0 <= px <= args.threshold:
                    fee = fees.taker_fee_cents(1, int(round(px)))
                    pnl = ((100 if rec["result"] == "yes" else 0)
                           - px - fee)
                    by_year[year].append(
                        (1 if rec["result"] == "yes" else 0, px, pnl))
                    break

print(f"{args.series}, buy the first print at or below "
      f"{args.threshold}c, hold to settlement\n")
print(f"{'year':<7} {'fills':>7} {'YES%':>7} {'95% CI':>15} {'avg px':>7} "
      f"{'P&L/contract':>13}")
print("-" * 62)

all_rows = []
for year in sorted(by_year):
    rows = by_year[year]
    n = len(rows)
    all_rows += rows
    if n < 15:
        print(f"{year:<7} {n:>7,}   (too few)")
        continue
    k = sum(r[0] for r in rows)
    lo, hi = wilson(k, n)
    avg = sum(r[1] for r in rows) / n
    per = sum(r[2] for r in rows) / n
    mark = "  <--" if per > 0 else ""
    print(f"{year:<7} {n:>7,} {100.0*k/n:>6.1f}% "
          f"{100*lo:>6.1f}-{100*hi:<7.1f} {avg:>6.1f}c "
          f"{per:>+12.2f}c{mark}")

if all_rows:
    n = len(all_rows)
    k = sum(r[0] for r in all_rows)
    per = sum(r[2] for r in all_rows) / n
    print(f"\n{'ALL':<7} {n:>7,} {100.0*k/n:>6.1f}% "
          f"{'':>15} {sum(r[1] for r in all_rows)/n:>6.1f}c "
          f"{per:>+12.2f}c")

recent = [r for y in by_year for r in by_year[y] if y >= "2025"]
if recent:
    n = len(recent)
    k = sum(r[0] for r in recent)
    per = sum(r[2] for r in recent) / n
    lo, hi = wilson(k, n)
    print(f"\n2025+ only: {n:,} fills, {100.0*k/n:.1f}% YES "
          f"({100*lo:.1f}-{100*hi:.1f}), {per:+.2f}c per contract")
    print("  This is the era the candle-based studies covered, and the")
    print("  only one that says anything about trading the market today.")
