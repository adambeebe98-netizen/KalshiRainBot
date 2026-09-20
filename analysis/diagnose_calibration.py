"""Why does a 97c contract appear to resolve YES 42% of the time?

It does not. That would be the most exploitable mispricing on any
exchange, and the curve is non-monotonic besides -- 87c pays 41%, 92c
pays 77%, 97c pays 42%. Real miscalibration does not reverse twice.

Candidates:
  A. the price is STALE -- taken from far earlier than the intended
     horizon, because the query takes the last candle at or before a
     cutoff and does not check how far before
  B. close_time does not parse, so the cutoff is wrong
  C. price and result belong to different things

Look at the actual markets in the broken bucket rather than reasoning
about them.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing

from config import SETTINGS


def stamp(ts):
    if ts is None:
        return "-"
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    conn.row_factory = sqlite3.Row

    print("=== does close_time parse? ===")
    for r in conn.execute(
            "SELECT close_time, strftime('%s', close_time) parsed "
            "FROM historical_markets WHERE close_time IS NOT NULL LIMIT 3"):
        print(f"  {r['close_time']!r} -> {r['parsed']!r}")

    bad = conn.execute(
        "SELECT COUNT(*) FROM historical_markets "
        "WHERE close_time IS NOT NULL "
        "AND strftime('%s', close_time) IS NULL").fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM historical_markets "
        "WHERE close_time IS NOT NULL").fetchone()[0]
    print(f"  {bad:,} of {total:,} close_time values do NOT parse")

    print("\n=== markets in the 95-100c bucket that resolved NO ===")
    rows = conn.execute("""
        SELECT m.ticker, m.result, m.close_time,
               (SELECT p.ts FROM historical_price_points p
                 WHERE p.ticker=m.ticker
                   AND p.ts <= strftime('%s', m.close_time) - 86400
                   AND p.yes_price_cents IS NOT NULL
                 ORDER BY p.ts DESC LIMIT 1) px_ts,
               (SELECT p.yes_price_cents FROM historical_price_points p
                 WHERE p.ticker=m.ticker
                   AND p.ts <= strftime('%s', m.close_time) - 86400
                   AND p.yes_price_cents IS NOT NULL
                 ORDER BY p.ts DESC LIMIT 1) px
          FROM historical_markets m
         WHERE m.result='no' AND m.measure IN
               ('temperature_high','temperature_low')
           AND m.close_time IS NOT NULL
        LIMIT 400
    """).fetchall()
    hits = [r for r in rows if r["px"] is not None and r["px"] >= 95]
    print(f"  {len(hits)} of {len(rows)} sampled NO markets priced >=95c")

    for r in hits[:6]:
        close_ts = conn.execute(
            "SELECT strftime('%s', ?)", (r["close_time"],)).fetchone()[0]
        lag_h = ((int(close_ts) - int(r["px_ts"])) / 3600
                 if close_ts and r["px_ts"] else None)
        print(f"\n  {r['ticker']}  result={r['result']}  px={r['px']}c")
        print(f"    close {r['close_time'][:19]}   price taken "
              f"{stamp(r['px_ts'])}  ({lag_h:.1f}h before close)"
              if lag_h is not None else "    (no lag)")
        path = conn.execute(
            "SELECT ts, yes_price_cents c FROM historical_price_points "
            "WHERE ticker=? ORDER BY ts", (r["ticker"],)).fetchall()
        if path:
            head = ", ".join(f"{stamp(p['ts'])[5:]}:{p['c']}"
                             for p in path[:3])
            tail = ", ".join(f"{stamp(p['ts'])[5:]}:{p['c']}"
                             for p in path[-3:])
            print(f"    {len(path)} candles | first {head}")
            print(f"    {'':<13}| last  {tail}")

    print("\n=== how stale is the chosen price, overall? ===")
    lags = conn.execute("""
        SELECT (strftime('%s', m.close_time) -
                (SELECT p.ts FROM historical_price_points p
                  WHERE p.ticker=m.ticker
                    AND p.ts <= strftime('%s', m.close_time) - 86400
                    AND p.yes_price_cents IS NOT NULL
                  ORDER BY p.ts DESC LIMIT 1)) / 3600.0 AS lag_h
          FROM historical_markets m
         WHERE m.result IN ('yes','no') AND m.close_time IS NOT NULL
         LIMIT 4000
    """).fetchall()
    vals = sorted(r["lag_h"] for r in lags if r["lag_h"] is not None)
    if vals:
        n = len(vals)
        print(f"  {n:,} sampled; lag between the chosen price and close:")
        for q, name in ((0.5, "median"), (0.9, "p90"), (0.99, "p99")):
            print(f"    {name:<7} {vals[int(q*n)]:8.1f} h")
        print(f"    max     {vals[-1]:8.1f} h")
        print("  Intended: 24h. Anything far above that is a STALE price"
              " being compared against a much later outcome.")
