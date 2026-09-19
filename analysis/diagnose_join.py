"""Why do 10% of joined markets disagree with their own outcome?

At settlement the Kalshi price and ESPN's final win probability should
both sit near 0 or near 1, and both should agree with the recorded
result. The median gap is 0.03, which says the join is broadly right.
The p90 is 0.52 and the max 0.98, which says something is badly wrong
for a minority -- and a minority that size will not be random.

Three candidate explanations, and they call for different fixes:

  A. the market is joined to the WRONG GAME      -> join bug, fatal
  B. ESPN's last win-prob point is not the end   -> use the result field
  C. Kalshi's last candle is not at settlement   -> windowing bug

Distinguishing them means checking each side against the OUTCOME, which
both sources report independently. If ESPN's probability agrees with
ESPN's own winner but not with Kalshi's result, the games are different.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import sys

import market_join as mj

KALSHI_DIR = sys.argv[1] if len(sys.argv) > 1 else "data/backfill/KXNFLGAME"
TRUTH_GLOB = (sys.argv[2] if len(sys.argv) > 2
              else "data/truth_backfill/nfl_winprob/*.jsonl.gz")


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


espn_by_date = collections.defaultdict(list)
known = set()
for path in sorted(glob.glob(TRUTH_GLOB)):
    for row in read_jsonl(path):
        espn_by_date[(row.get("date") or "")[:10]].append(row)
        known |= {c.get("abbrev") for c in (row.get("competitors") or [])
                  if c.get("abbrev")}

rows = []
for path in sorted(glob.glob(f"{KALSHI_DIR}/*.jsonl.gz")):
    for market in read_jsonl(path):
        f = mj.fixture_for(market.get("ticker", ""), known)
        if not f:
            continue
        event = mj.match_event(f, espn_by_date.get(f["date"], []))
        if not event:
            continue
        wp = event.get("winprobability") or []
        candles = market.get("candles_hourly") or []
        if not wp or not candles:
            continue
        price = (candles[-1].get("price") or {}).get("close")
        if price is None:
            continue
        espn_p = mj.probability_for_yes(
            wp[-1].get("homeWinPercentage") or 0, f)
        # Who does ESPN itself say won? Independent of the probability.
        winner = None
        for c in event.get("competitors") or []:
            if c.get("winner"):
                winner = c.get("abbrev")
        rows.append({
            "ticker": market["ticker"],
            "result": market.get("result"),
            "price": float(price),
            "espn_p": espn_p,
            "gap": abs(float(price) - espn_p),
            "espn_winner": winner,
            "yes_espn": f["espn_" + ("home" if f["yes_is_home"] else "away")],
            "date": f["date"],
            "wp_points": len(wp),
            "candles": len(candles),
            "close_time": market.get("close_time"),
        })

print(f"{len(rows):,} joined markets with both sides\n")

# Does ESPN's own recorded winner agree with Kalshi's settlement?
agree = disagree = unknown = 0
for r in rows:
    if r["espn_winner"] is None:
        unknown += 1
    elif (r["espn_winner"] == r["yes_espn"]) == (r["result"] == "yes"):
        agree += 1
    else:
        disagree += 1

print("Kalshi's settlement vs ESPN's recorded winner:")
print(f"  agree    {agree:>6,}")
print(f"  DISAGREE {disagree:>6,}   <- these are joined to the wrong game")
print(f"  no winner recorded {unknown:>6,}")

bad = sorted(rows, key=lambda r: -r["gap"])[:12]
print("\nworst gaps:")
print(f"  {'ticker':<34} {'res':<4} {'price':>6} {'espn':>6} {'gap':>6} "
      f"{'winner':>7} {'yes':>5} {'wp':>4}")
for r in bad:
    print(f"  {r['ticker'][:34]:<34} {str(r['result']):<4} "
          f"{r['price']:>6.2f} {r['espn_p']:>6.2f} {r['gap']:>6.2f} "
          f"{str(r['espn_winner']):>7} {r['yes_espn']:>5} "
          f"{r['wp_points']:>4}")

# If the last win-prob point is not final, its value will not be ~0/1.
extreme = sum(1 for r in rows if r["espn_p"] < 0.02 or r["espn_p"] > 0.98)
print(f"\nESPN final probability is ~0 or ~1 in {extreme:,}/{len(rows):,} "
      f"({100.0*extreme/max(len(rows),1):.1f}%)")
print("  a curve that does not end at certainty was truncated, not wrong")

price_extreme = sum(1 for r in rows if r["price"] < 0.05 or r["price"] > 0.95)
print(f"Kalshi last candle is <5c or >95c in {price_extreme:,}/{len(rows):,} "
      f"({100.0*price_extreme/max(len(rows),1):.1f}%)")

# Preseason is the other suspicion: those markets behave differently.
pre = [r for r in rows if r["date"] < "2025-09-01"]
reg = [r for r in rows if r["date"] >= "2025-09-01"]
for label, group in (("preseason (before Sep)", pre), ("regular", reg)):
    if group:
        g = sorted(x["gap"] for x in group)
        print(f"\n{label}: {len(group):,} markets, "
              f"median gap {g[len(g)//2]:.3f}, p90 {g[int(len(g)*0.9)]:.3f}")
