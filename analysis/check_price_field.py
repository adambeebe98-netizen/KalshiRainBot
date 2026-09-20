"""Is yes_price_cents a tradeable price, or a stale last trade?

The calibration curve said 97c contracts resolve YES 42% of the time,
which is not a thing that happens. The horizon logic checked out --
prices are taken a tight 25h before close -- so the suspect is the
PRICE FIELD itself.

historical_price_points stores yes_price_cents (the candlestick close,
i.e. the last trade) alongside yes_bid_cents, yes_ask_cents and volume.
For a thin bracket leg those can disagree enormously: a leg that last
traded at 97c three weeks ago and has had no bid since is recorded at
97c and is worth nothing. Calibrating against that measures the stale
print, not the market.

Same lesson that dropped three soccer series from live collection: a
displayed price with nothing behind it is not data.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from config import SETTINGS

SQL = """
    SELECT m.ticker, m.result, p.yes_price_cents px,
           p.yes_bid_cents bid, p.yes_ask_cents ask,
           p.volume vol, p.open_interest oi
      FROM historical_markets m
      JOIN historical_price_points p ON p.ticker = m.ticker
     WHERE m.result IN ('yes','no')
       AND m.measure IN ('temperature_high','temperature_low')
       AND m.close_time IS NOT NULL
       AND p.ts <= strftime('%s', m.close_time) - 86400
       AND p.ts >  strftime('%s', m.close_time) - 90000
       AND p.yes_price_cents IS NOT NULL
"""

with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL).fetchall()

print(f"{len(rows):,} (market, candle) observations near the 24h mark\n")

high = [r for r in rows if r["px"] >= 95]
print(f"observations with last-trade price >= 95c: {len(high):,}")
if high:
    yes = sum(1 for r in high if r["result"] == "yes")
    print(f"  of which resolved YES: {yes:,} ({100.0*yes/len(high):.1f}%)")

    with_bid = [r for r in high if r["bid"] is not None and r["bid"] > 0]
    print(f"  with a real bid (>0): {len(with_bid):,} "
          f"({100.0*len(with_bid)/len(high):.1f}%)")
    with_vol = [r for r in high if (r["vol"] or 0) > 0]
    print(f"  with volume in that candle: {len(with_vol):,} "
          f"({100.0*len(with_vol)/len(high):.1f}%)")

    if with_bid:
        yes_b = sum(1 for r in with_bid if r["result"] == "yes")
        print(f"\n  RESTRICTED TO OBSERVATIONS WITH A REAL BID:")
        print(f"    {len(with_bid):,} observations, "
              f"{100.0*yes_b/len(with_bid):.1f}% resolved YES")

    print("\n  sample of >=95c last-trade observations:")
    print(f"    {'result':<7} {'last':>5} {'bid':>5} {'ask':>5} "
          f"{'vol':>8} {'oi':>9}")
    for r in high[:10]:
        print(f"    {r['result']:<7} {r['px']:>5} "
              f"{str(r['bid']):>5} {str(r['ask']):>5} "
              f"{str(r['vol']):>8} {str(r['oi']):>9}")

print("\n=== how often does last trade disagree with the book? ===")
both = [r for r in rows
        if r["bid"] is not None and r["ask"] is not None
        and 0 <= r["bid"] <= 100 and 0 <= r["ask"] <= 100 and r["ask"] > 0]
print(f"  {len(both):,} observations have both a bid and an ask")
if both:
    gaps = sorted(abs(r["px"] - (r["bid"] + r["ask"]) / 2.0) for r in both)
    n = len(gaps)
    print(f"  |last trade - mid| :  median {gaps[n//2]:.1f}c   "
          f"p90 {gaps[int(0.9*n)]:.1f}c   max {gaps[-1]:.1f}c")
    far = sum(1 for g in gaps if g > 10)
    print(f"  more than 10c away from the mid: {far:,} "
          f"({100.0*far/n:.1f}%)")

zero_bid = sum(1 for r in rows if (r["bid"] or 0) == 0)
print(f"\n  observations with NO bid at all: {zero_bid:,} of {len(rows):,} "
      f"({100.0*zero_bid/max(len(rows),1):.1f}%)")
