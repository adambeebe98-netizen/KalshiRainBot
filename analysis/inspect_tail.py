"""Do the Kalshi candles actually reach settlement?

A market that settled YES shows a last hourly close of 0.02. Two very
different readings of that:

  A. the candle series STOPS before the game ends, so the last candle is
     a mid-game price and any label built from it is misaligned in time.
  B. the series does reach the end, and 0.02 is simply where the market
     was at the final hour of a genuine comeback.

These call for opposite responses -- A is a windowing bug to fix, B is
the single most valuable row in the dataset. Print the tail with
timestamps against the market's own close_time and settle it.
"""
from __future__ import annotations

import datetime as dt
import glob
import gzip
import json
import sys

TARGET = sys.argv[1] if len(sys.argv) > 1 else "25OCT19NYGDEN-DEN"


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def ts(value) -> str:
    if value is None:
        return "-"
    return dt.datetime.fromtimestamp(int(value),
                                     dt.timezone.utc).strftime("%m-%d %H:%M")


found = None
for path in sorted(glob.glob("data/backfill/KXNFLGAME/*.jsonl.gz")):
    for m in read_jsonl(path):
        if TARGET in m.get("ticker", ""):
            found = m
            break
    if found:
        break

if not found:
    print(f"no market matching {TARGET}")
    raise SystemExit(1)

m = found
print(f"{m['ticker']}   result={m['result']}")
print(f"open_time   {m.get('open_time')}")
print(f"close_time  {m.get('close_time')}")
print(f"settlement  {m.get('settlement_ts')} "
      f"value={m.get('settlement_value_dollars')}")
print(f"volume {m.get('volume'):,.0f}  open_interest "
      f"{m.get('open_interest'):,.0f}")

hourly = m.get("candles_hourly") or []
minute = m.get("candles_minute") or []
print(f"\n{len(hourly)} hourly candles, {len(minute)} minute candles")

print("\nlast 8 HOURLY candles:")
print(f"  {'end':<12} {'close':>7} {'high':>6} {'low':>6} {'bid':>6} "
      f"{'ask':>6} {'volume':>12}")
for c in hourly[-8:]:
    p = c.get("price") or {}
    b = c.get("yes_bid") or {}
    a = c.get("yes_ask") or {}
    print(f"  {ts(c.get('end_period_ts')):<12} "
          f"{str(p.get('close')):>7} {str(p.get('high')):>6} "
          f"{str(p.get('low')):>6} {str(b.get('close')):>6} "
          f"{str(a.get('close')):>6} "
          f"{float(c.get('volume') or 0):>12,.0f}")

print("\nlast 10 MINUTE candles:")
for c in minute[-10:]:
    p = c.get("price") or {}
    print(f"  {ts(c.get('end_period_ts')):<12} close={str(p.get('close')):>7} "
          f"vol={float(c.get('volume') or 0):>10,.0f}")

if minute:
    last_min = minute[-1]
    p = (last_min.get("price") or {}).get("close")
    print(f"\nFINAL MINUTE candle close: {p}")
    print(f"result: {m['result']}")
    print("\nIf the minute series reaches settlement and its close agrees")
    print("with the result, the hourly tail was simply the wrong field to")
    print("read -- not a windowing bug.")
