"""Backfill the trade log for every weather series in the archive.

Series names come from the archive itself rather than a hand-written
list, because a hand-written list is how the rain scrape ended up
missing 344 of 895 markets in the first place.

The disk guard and adaptive pacing live in throttle.py; this only
decides WHAT to fetch. If the disk runs low the job aborts rather than
starving the live collectors, whose data -- unlike this -- cannot be
re-fetched at any price.
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing

import backfill_trades
import throttle as th
from config import SETTINGS
from kalshi_client import KalshiClient

with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    rows = conn.execute(
        "SELECT substr(ticker, 1, instr(ticker || '-', '-') - 1) fam, "
        "COUNT(*) n FROM historical_markets "
        "WHERE result IN ('yes','no') GROUP BY fam HAVING n > 0 "
        "ORDER BY n DESC").fetchall()

series = [r[0] for r in rows]
total = sum(r[1] for r in rows)
print(f"{len(series)} weather series, {total:,} settled markets")
for fam, n in rows[:8]:
    print(f"   {fam:<22} {n:>7,}")
if len(rows) > 8:
    print(f"   ... and {len(rows)-8} more")

kalshi = KalshiClient()
pacer = th.Throttle(interval=0.05)
guard = th.DiskGuard(min_free_gb=2.0)
guard.check(force=True)
print(f"\ndisk {guard.free_gb:.1f} GB free; aborting below 2.0 GB")
print(f"pacing {pacer.interval:.3f}s, adapts on 429\n")

import time
t0 = time.time()
grand = {"markets": 0, "trades": 0, "contracts": 0.0, "no_trades": 0}

for name in series:
    try:
        s = backfill_trades.backfill_series(
            kalshi, name, "data/trades", None, pacer, guard)
    except RuntimeError as exc:
        print(f"\nABORTED: {exc}")
        break
    except Exception as exc:
        print(f"  {name:<22} FAILED {type(exc).__name__}: {exc}")
        continue
    for k in grand:
        grand[k] += s.get(k, 0)
    print(f"  {name:<22} markets={s['markets']:>6,} "
          f"skipped={s['skipped']:>6,} untraded={s['no_trades']:>5,} "
          f"trades={s['trades']:>9,} "
          f"[live {s['live']}, hist {s['historical']}]  "
          f"disk {guard.free_gb:.1f} GB", flush=True)

mins = (time.time() - t0) / 60
print(f"\n{grand['markets']:,} markets, {grand['trades']:,} trades, "
      f"{grand['contracts']:,.0f} contracts in {mins:.1f} min")
print(f"  {pacer.requests:,} requests, {pacer.throttles} throttled, "
      f"final pacing {pacer.interval:.3f}s")
print(f"  disk {guard.free_gb:.1f} GB free")
