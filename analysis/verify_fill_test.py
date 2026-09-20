"""Verify the trade-based fill result, which contradicts the calibration.

The trade test says a print at ~5.7c resolves YES 11.8% of the time and
every threshold is profitable. The snapshot calibration on the same
family says the 7.5c bucket resolves YES 4.8% and longshots are
OVERpriced. Both cannot be true, and the trade version is the one that
looks too good.

Four things to check, cheapest first:

  A. is `result` attached to the right market
  B. is yes_price_dollars really the YES side
  C. is the market POPULATION different -- 854 traded markets against
     568 with quoted candles, and base rates of 41.3% vs 46.8%
  D. is "first print at or below X" selecting differently from "first
     ASK at or below X" -- a trade at 10c needs a willing BUYER, while
     an ask at 10c needs only a seller, so the two may genuinely sample
     different markets

D would make the result real but fragile. A, B or C would make it
wrong. Print the evidence rather than reason about it.
"""
from __future__ import annotations

import glob
import gzip
import json
import sqlite3
from contextlib import closing

from config import SETTINGS


def fp(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


books = {}
for path in sorted(glob.glob("data/trades/KXRAINNYC/*.jsonl.gz")):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rec = json.loads(line)
                books[rec["ticker"]] = rec

print(f"{len(books):,} markets in the trade archive\n")

# --- A: does `result` match the database? --------------------------------
print("=== A. result field, cross-checked against historical_markets ===")
mismatch = checked = 0
with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    for ticker, rec in books.items():
        row = conn.execute(
            "SELECT result FROM historical_markets WHERE ticker=?",
            (ticker,)).fetchone()
        if not row or row[0] not in ("yes", "no"):
            continue
        checked += 1
        if row[0] != rec.get("result"):
            mismatch += 1
print(f"  {checked:,} cross-checked, {mismatch:,} disagree")

# --- B: is yes_price the YES side? ---------------------------------------
print("\n=== B. yes_price + no_price should sum to 1.00 ===")
bad = tot = 0
for rec in list(books.values())[:50]:
    for t in (rec.get("trades") or [])[:40]:
        y = fp(t.get("yes_price_dollars"), -1)
        n = fp(t.get("no_price_dollars"), -1)
        if y < 0 or n < 0:
            continue
        tot += 1
        if abs((y + n) - 1.0) > 0.001:
            bad += 1
print(f"  {tot:,} trades checked, {bad:,} do not sum to 1.00")

# --- C: population difference --------------------------------------------
print("\n=== C. do the last trades agree with the outcome? ===")
print("  A market settling YES should end with prints near 100c.")
agree = disagree = 0
examples = []
for ticker, rec in books.items():
    trades = rec.get("trades") or []
    if not trades:
        continue
    trades.sort(key=lambda t: t.get("created_time", ""))
    last_px = fp(trades[-1].get("yes_price_dollars"), -1) * 100
    if last_px < 0:
        continue
    settled_yes = rec.get("result") == "yes"
    if (last_px > 50) == settled_yes:
        agree += 1
    else:
        disagree += 1
        if len(examples) < 5:
            examples.append((ticker, rec.get("result"), last_px,
                             len(trades)))
print(f"  last print agrees with outcome: {agree:,}")
print(f"  DISAGREES:                      {disagree:,}")
for t, r, px, n in examples:
    print(f"    {t:<26} result={r:<4} last print {px:>5.1f}c  "
          f"({n} trades)")

# --- D: what does the <=10c rule actually select? ------------------------
print("\n=== D. markets where a print hit <=10c, and what happened ===")
fired = []
for ticker, rec in books.items():
    trades = sorted(rec.get("trades") or [],
                    key=lambda t: t.get("created_time", ""))
    for i, t in enumerate(trades):
        px = fp(t.get("yes_price_dollars"), -1) * 100
        if 0 <= px <= 10:
            fired.append((ticker, rec.get("result"), px, i, len(trades),
                          t.get("created_time", "")[:19],
                          rec.get("close_time", "")[:19]))
            break

yes_n = sum(1 for f in fired if f[1] == "yes")
print(f"  {len(fired):,} markets, {yes_n:,} resolved YES "
      f"({100.0*yes_n/max(len(fired),1):.1f}%)")
print(f"\n  {'ticker':<26} {'res':<4} {'px':>5} {'trade#':>7} "
      f"{'of':>6}  entry time          close")
for f in [x for x in fired if x[1] == "yes"][:8]:
    print(f"  {f[0]:<26} {f[1]:<4} {f[2]:>5.1f} {f[3]:>7,} {f[4]:>6,}  "
          f"{f[5]}  {f[6]}")

print("\n  If the YES cases entered at trade #0 of a long series, the")
print("  entry is early and the market later recovered -- real, but")
print("  path-dependent. If they entered near the END, something is")
print("  wrong with the ordering.")
