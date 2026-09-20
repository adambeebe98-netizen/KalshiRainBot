"""At what resolution is the weather price history stored?

The conclusion "we must wait for tick data" rests on an assumption:
that the only fine-grained view of the final hour is the one being
captured live. That assumption is worth testing, because the sports
backfill already pulls ONE-MINUTE candles for the final six hours of
every settled market, and the same endpoint serves weather.

If historical_price_points is hourly, then the near-close question is
not blocked on collecting anything. It is blocked on a fetch nobody has
run -- which is a very different kind of waiting, and a much shorter
one.

Checks the spacing between consecutive candles, and how close to close
the last one lands.
"""
from __future__ import annotations

import collections
import sqlite3
from contextlib import closing

from config import SETTINGS

with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    conn.row_factory = sqlite3.Row

    print("=== candle spacing in historical_price_points ===")
    tickers = [r[0] for r in conn.execute(
        "SELECT DISTINCT m.ticker FROM historical_markets m "
        "JOIN historical_price_points p ON p.ticker = m.ticker "
        "WHERE m.measure = 'precipitation_daily' LIMIT 40")]

    gaps = collections.Counter()
    last_gap_to_close = []
    for t in tickers:
        rows = conn.execute(
            "SELECT ts FROM historical_price_points WHERE ticker=? "
            "ORDER BY ts", (t,)).fetchall()
        ts = [r[0] for r in rows]
        for a, b in zip(ts, ts[1:]):
            gaps[b - a] += 1
        close = conn.execute(
            "SELECT strftime('%s', close_time) FROM historical_markets "
            "WHERE ticker=?", (t,)).fetchone()[0]
        if close and ts:
            last_gap_to_close.append((int(close) - ts[-1]) / 60.0)

    print(f"  sampled {len(tickers)} daily-rain markets")
    for gap, n in gaps.most_common(6):
        print(f"    {gap:>6}s ({gap/60:>5.0f} min) between candles: "
              f"{n:>6,} times")

    if last_gap_to_close:
        last_gap_to_close.sort()
        n = len(last_gap_to_close)
        print(f"\n  minutes from the LAST candle to close:")
        print(f"    median {last_gap_to_close[n//2]:>7.1f} min")
        print(f"    best   {last_gap_to_close[0]:>7.1f} min")
        print(f"    worst  {last_gap_to_close[-1]:>7.1f} min")

    print("\n=== candles per market, by measure ===")
    for row in conn.execute(
            "SELECT m.measure, COUNT(*) candles, "
            "COUNT(DISTINCT m.ticker) mk "
            "FROM historical_markets m "
            "JOIN historical_price_points p ON p.ticker = m.ticker "
            "GROUP BY m.measure ORDER BY candles DESC"):
        per = row["candles"] / max(row["mk"], 1)
        print(f"  {str(row['measure']):<24} {row['candles']:>10,} candles "
              f"/ {row['mk']:>7,} markets = {per:>6.1f} each")

print("\n=== what the API would give at 1-minute resolution ===")
print("  backfill_sports.py already requests period_interval=1 for the")
print("  final 6 hours of every market it pulls. If the numbers above")
print("  are hourly, the fine-grained weather history has simply never")
print("  been fetched -- it is not missing from Kalshi.")
