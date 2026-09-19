"""The bracket sweep as a function of size.

The size-1 result was misleading, and in the pessimistic direction. Fees
here are capped at $1.75 PER ORDER, so a sweep's cost is fixed at six
orders no matter how large each one is, while the edge scales linearly
with contracts. That inverts the conclusion: the trade is not
fee-dominated, it is fee-dominated AT SIZE ONE.

So the real questions are not about fees at all:

1. How large a sweep could actually be filled, given the volume that
   traded in that hour? A sweep that fills five legs of six is not an
   arbitrage, it is an unhedged directional bet on the leg that missed.
2. How much capital does each sweep tie up until settlement?

Both are measured here rather than assumed.
"""
import datetime as dt
import sqlite3
from collections import defaultdict

import fees
import splits
from config import SETTINGS

PARTICIPATION = 0.10      # same cap the execution model uses
HOURS = 24

conn = sqlite3.connect(SETTINGS.db_path)
conn.execute("PRAGMA busy_timeout = 30000")
conn.row_factory = sqlite3.Row

markets = [m for m in splits.load("markets", split="train+dev")
           if m.get("event_ticker") and m.get("result") in ("yes", "no")]
events = defaultdict(list)
for m in markets:
    events[m["event_ticker"]].append(m)
exclusive = [ev for ev, ms in events.items()
             if len(ms) >= 2 and sum(1 for m in ms if m["result"] == "yes") == 1]

opportunities = []
for ev in exclusive:
    ms = events[ev]
    try:
        close = int(dt.datetime.fromisoformat(
            ms[0]["close_time"].replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, AttributeError):
        continue
    as_of = close - HOURS * 3600
    legs = []
    for m in ms:
        row = conn.execute(
            "SELECT yes_bid_cents, volume FROM historical_price_points "
            "WHERE ticker = ? AND ts <= ? AND yes_bid_cents IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (m["ticker"], as_of)).fetchone()
        if row is None:
            legs = []
            break
        legs.append((row["yes_bid_cents"], row["volume"] or 0))
    if len(legs) != len(ms):
        continue
    edge = sum(b for b, _ in legs) - 100
    if edge > 0:
        opportunities.append((edge, legs))
conn.close()

print(f"{len(opportunities):,} sweeps with a positive gross edge at {HOURS}h")
if not opportunities:
    raise SystemExit(0)

mean_edge = sum(e for e, _ in opportunities) / len(opportunities)
print(f"mean edge {mean_edge:.1f}c per contract-set\n")


def sweep_pnl(edge, legs, size):
    gross = edge * size
    fee = sum(fees.taker_fee_cents(size, 100 - b) for b, _ in legs)
    cost = sum((100 - b) * size for b, _ in legs)
    return gross, fee, gross - fee, cost


print("=== IGNORING LIQUIDITY: what the fee cap alone implies ===")
print(f"{'size':>7} {'gross':>12} {'fees':>10} {'net':>12} {'capital':>12}")
for size in (1, 10, 100, 500, 2000, 10000):
    g = f = n = c = 0
    for edge, legs in opportunities:
        gg, ff, nn, cc = sweep_pnl(edge, legs, size)
        g += gg
        f += ff
        n += nn
        c = max(c, cc)
    print(f"{size:>7} {g/100:>11,.0f} {f/100:>9,.0f} {n/100:>11,.0f} "
          f"{c/100:>11,.0f}")
print("  (capital = the largest single sweep's cost, tied up to settlement)")

print("\n=== WITH LIQUIDITY: capped at "
      f"{PARTICIPATION:.0%} of the thinnest leg's hourly volume ===")
fillable = []
for edge, legs in opportunities:
    size = int(min(v for _, v in legs) * PARTICIPATION)
    if size >= 1:
        fillable.append((edge, legs, size))
print(f"  sweeps fillable at all: {len(fillable):,} of {len(opportunities):,}")
if fillable:
    sizes = sorted(s for _, _, s in fillable)
    print(f"  fillable size: median {sizes[len(sizes)//2]}, "
          f"p90 {sizes[int(len(sizes)*0.9)]}, max {sizes[-1]}")
    g = f = n = 0
    for edge, legs, size in fillable:
        gg, ff, nn, _ = sweep_pnl(edge, legs, size)
        g += gg
        f += ff
        n += nn
    print(f"  gross {g/100:+,.2f} USD   fees {f/100:,.2f} USD   "
          f"net {n/100:+,.2f} USD")
    winners = sum(1 for edge, legs, size in fillable
                  if sweep_pnl(edge, legs, size)[2] > 0)
    print(f"  sweeps net-positive: {winners:,} of {len(fillable):,} "
          f"({winners/len(fillable):.1%})")

print("\n=== AND THE LEG THAT MATTERS: can every leg fill? ===")
thin = sum(1 for _, legs in opportunities if min(v for _, v in legs) == 0)
print(f"  sweeps where at least one leg had ZERO volume that hour: "
      f"{thin:,} of {len(opportunities):,} ({thin/len(opportunities):.1%})")
print("  Those cannot be swept at any size. A five-of-six fill is not an")
print("  arbitrage, it is an unhedged bet on the leg that missed.")
