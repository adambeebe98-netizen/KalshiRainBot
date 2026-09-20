"""Do rain contracts ever sit in the middle of the range with a real book?

The 24h calibration for daily rain produced buckets at 0-15c and
90-100c and essentially nothing between. Either the market is simply
confident a day out -- it usually knows whether rain is coming -- or
the mid-range markets exist and were filtered out for having a wide
book.

That distinction decides whether the question "is a 40% rain contract
really 40%" is even answerable from this data. So count the middle
directly, across horizons and spread limits, before drawing a curve
through it.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from config import SETTINGS

MEASURES = ("precipitation_daily", "precipitation_monthly")

SQL = """
    SELECT m.measure, m.result, p.yes_bid_cents bid, p.yes_ask_cents ask
      FROM historical_markets m
      JOIN historical_price_points p ON p.ticker = m.ticker
     WHERE m.result IN ('yes','no')
       AND m.measure = ?
       AND m.close_time IS NOT NULL
       AND p.ts = (SELECT q.ts FROM historical_price_points q
                    WHERE q.ticker = m.ticker
                      AND q.ts <= strftime('%s', m.close_time) - ?
                    ORDER BY q.ts DESC LIMIT 1)
"""

print(f"{'measure':<24} {'horizon':>8} {'quoted':>7} {'mid 20-80c':>11} "
      f"{'median spread':>14}")
print("-" * 70)

with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    for measure in MEASURES:
        for hours in (24, 12, 6, 2, 0.5):
            rows = conn.execute(
                SQL, (measure, int(hours * 3600))).fetchall()
            quoted, middle, spreads = 0, 0, []
            mids_yes = []
            for _m, result, bid, ask in rows:
                if bid is None or ask is None:
                    continue
                if not (0 < bid <= 100 and 0 < ask <= 100 and ask >= bid):
                    continue
                quoted += 1
                spreads.append(ask - bid)
                mid = (bid + ask) / 2.0
                if 20 <= mid <= 80:
                    middle += 1
                    mids_yes.append((mid, 1 if result == "yes" else 0))
            med = (sorted(spreads)[len(spreads) // 2] if spreads else 0)
            print(f"{measure:<24} {hours:>7}h {quoted:>7,} {middle:>11,} "
                  f"{med:>13}c")

    print("\n=== the mid-range, pooled across horizons, daily rain ===")
    pooled = []
    for hours in (24, 12, 6, 2):
        rows = conn.execute(
            SQL, ("precipitation_daily", int(hours * 3600))).fetchall()
        for _m, result, bid, ask in rows:
            if bid is None or ask is None:
                continue
            if not (0 < bid <= 100 and 0 < ask <= 100 and ask >= bid):
                continue
            if ask - bid > 20:
                continue
            mid = (bid + ask) / 2.0
            if 20 <= mid <= 80:
                pooled.append((mid, 1 if result == "yes" else 0))

    print(f"  {len(pooled):,} observations "
          f"(NOT independent -- the same market appears at several"
          f" horizons)")
    if pooled:
        for lo, hi in ((20, 35), (35, 50), (50, 65), (65, 80)):
            sel = [y for m, y in pooled if lo <= m < hi]
            if len(sel) >= 10:
                print(f"    {lo:>3}-{hi:<3}c  n={len(sel):>4}  "
                      f"actual YES {100.0*sum(sel)/len(sel):>5.1f}%  "
                      f"(price implies ~{(lo+hi)/2:.0f}%)")
            else:
                print(f"    {lo:>3}-{hi:<3}c  n={len(sel):>4}  too few")
