"""How much data was the original weather scrape actually missing?

Answered with arithmetic rather than a shrug. Three things were left
behind, and only one of them was ever unavailable:

  TRADES    never fetched at all, because the client had no method
  MINUTES   fetched at hourly resolution when 1-minute exists
  MARKETS   the listing itself came back short

The rain backfill gives real per-market rates to extrapolate from, so
these are measured ratios applied to a known market count, not guesses.
Extrapolation is still extrapolation: rain trades more heavily per
market than a temperature bracket leg, so the trade estimate is an
upper bound and is labelled as one.
"""
from __future__ import annotations

import glob
import gzip
import json
import sqlite3
from contextlib import closing

from config import SETTINGS

# --- what the original scrape holds --------------------------------------
with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT COUNT(*) markets FROM historical_markets").fetchone()
    markets = r["markets"]
    candles = conn.execute(
        "SELECT COUNT(*) FROM historical_price_points").fetchone()[0]
    vol = conn.execute(
        "SELECT SUM(volume) FROM historical_price_points").fetchone()[0] or 0

print("=" * 70)
print("WHAT THE ORIGINAL WEATHER SCRAPE HOLDS")
print("=" * 70)
print(f"  markets                     {markets:>14,}")
print(f"  price candles (hourly)      {candles:>14,}")
print(f"  contracts traded in them    {vol:>14,.0f}")

# --- measured rates from the rain backfill -------------------------------
def count_jsonl(pattern, field=None):
    n_rows = 0
    total = 0
    for path in glob.glob(pattern):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                n_rows += 1
                if field:
                    total += len(json.loads(line).get(field) or [])
    return n_rows, total

tr_markets, tr_trades = count_jsonl("data/trades/*/*.jsonl.gz", "trades")
wx_markets, _ = count_jsonl("data/backfill_wx/*/*.jsonl.gz")

mins = 0
hours = 0
for path in glob.glob("data/backfill_wx/*/*.jsonl.gz"):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                d = json.loads(line)
                mins += len(d.get("candles_minute") or [])
                hours += len(d.get("candles_hourly") or [])

print("\n" + "=" * 70)
print("MEASURED RATES, from the rain re-fetch")
print("=" * 70)
if tr_markets:
    print(f"  trades per market           {tr_trades/tr_markets:>14,.0f}"
          f"   ({tr_trades:,} over {tr_markets:,} markets)")
if wx_markets:
    print(f"  candles per market, new     "
          f"{(mins+hours)/wx_markets:>14,.0f}   "
          f"({mins:,} minute + {hours:,} hourly)")
    print(f"  candles per market, old     {candles/markets:>14,.0f}")

# --- the gap -------------------------------------------------------------
print("\n" + "=" * 70)
print("ESTIMATED GAP ACROSS THE 59k WEATHER ARCHIVE")
print("=" * 70)
if tr_markets and wx_markets:
    est_trades = markets * (tr_trades / tr_markets)
    est_candles = markets * ((mins + hours) / wx_markets)
    print(f"  trades never fetched        {est_trades:>14,.0f}   "
          f"(upper bound -- rain trades more than a bracket leg)")
    print(f"  candles at full resolution  {est_candles:>14,.0f}")
    print(f"  candles actually held       {candles:>14,.0f}")
    print(f"  candle shortfall            {est_candles-candles:>14,.0f}")

print("\n  Markets missed by the listing itself, where measured:")
print("    daily rain (KXRAINNYC)    551 held  vs  895 available")
print("\n  The trades were never unavailable. The client simply had no")
print("  method for the endpoint, so nothing could ask.")
