"""Measure the Kalshi-to-ESPN join against real files.

market_join.py holds the logic and its tests; this reports what the
logic actually achieves on the backfills, and -- more importantly --
WHAT IT DROPS. A join that works on the easy 80% is worse than no join,
because the 20% it loses will not be random. The first version of the
split scored 75% and every single failure was a Los Angeles fixture.
"""
from __future__ import annotations

import collections
import datetime as dt
import glob
import gzip
import json
import os
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
known_codes = set()
espn_games = 0
for path in sorted(glob.glob(TRUTH_GLOB)):
    for row in read_jsonl(path):
        espn_by_date[(row.get("date") or "")[:10]].append(row)
        known_codes |= {c.get("abbrev") for c in (row.get("competitors") or [])
                        if c.get("abbrev")}
        espn_games += 1

print(f"ESPN side  : {espn_games:,} games across {len(espn_by_date):,} dates")

stats = collections.Counter()
examples = collections.defaultdict(list)
pairs = []

files = sorted(glob.glob(os.path.join(KALSHI_DIR, "*.jsonl.gz")))
print(f"Kalshi side: {len(files)} file(s) under {KALSHI_DIR}\n")

for path in files:
    for market in read_jsonl(path):
        ticker = market.get("ticker", "")
        stats["markets"] += 1
        fixture = mj.fixture_for(ticker, known_codes)
        if not fixture:
            stats["unjoinable_ticker"] += 1
            examples["unjoinable_ticker"].append(ticker)
            continue
        # Look at the neighbouring days too: Kalshi dates tickers in US
        # local time and ESPN files events in UTC, so an evening kickoff
        # lands on the next UTC day.
        near = [d for d in (fixture["date"],) if d in espn_by_date]
        if not near and not any(
                (dt.date.fromisoformat(fixture["date"])
                 + dt.timedelta(days=k)).isoformat() in espn_by_date
                for k in (-1, 1)):
            stats["no_espn_for_date"] += 1
            examples["no_espn_for_date"].append(
                f"{ticker} ({fixture['date']})")
            continue
        event = mj.match_event_near(fixture, espn_by_date)
        if not event:
            stats["no_fixture_match"] += 1
            examples["no_fixture_match"].append(
                f"{ticker} -> {fixture['espn_away']}@{fixture['espn_home']}"
                f" on {fixture['date']}")
            continue
        stats["matched"] += 1
        pairs.append((market, event, fixture))

print(f"{'outcome':<24} {'count':>8}")
print("-" * 34)
for key in ("matched", "unjoinable_ticker", "no_espn_for_date",
            "no_fixture_match"):
    if stats[key]:
        print(f"{key:<24} {stats[key]:>8,}")
        for ex in examples[key][:3]:
            print(f"    e.g. {ex}")

attempted = stats["markets"] - stats["no_espn_for_date"]
if attempted:
    print(f"\nmatch rate where ESPN had the date: "
          f"{100.0*stats['matched']/attempted:.1f}% "
          f"({stats['matched']:,}/{attempted:,})")

# --- is the joined record any good? --------------------------------------
# A join that lines up is not the same as a join that is INFORMATIVE.
# The question this dataset exists to answer is whether an independent
# probability ever disagrees with the price, so measure that.
print("\n=== market price vs independent estimate ===")
gaps, checked = [], 0
for market, event, fixture in pairs:
    wp = event.get("winprobability") or []
    # final_price() reads the MINUTE series. The hourly tail ends on an
    # hour boundary and a market closes when the game does, so hourly
    # can miss the last 59 minutes -- and on a comeback that inverts the
    # outcome entirely.
    price = mj.final_price(market)
    if not wp or price is None:
        continue
    espn_p = mj.probability_for_yes(
        wp[-1].get("homeWinPercentage") or 0, fixture)
    gaps.append(abs(price - espn_p))
    checked += 1

if gaps:
    gaps.sort()
    print(f"  {checked:,} markets with both a price and a curve")
    print(f"  |Kalshi - ESPN| at close:  median {gaps[len(gaps)//2]:.4f}"
          f"   p90 {gaps[int(len(gaps)*0.9)]:.4f}   max {gaps[-1]:.4f}")
    print("  (both should be near 0 and 1 at settlement -- a large gap"
          " here means the join is wrong, not that there is an edge)")
else:
    print("  no markets yet carry both a price and a win-probability curve")

print("\n=== sample joined records ===")
for market, event, fixture in pairs[:3]:
    wp = event.get("winprobability") or []
    candles = market.get("candles_hourly") or []
    print(f"\n{market['ticker']}  result={market['result']}")
    print(f"  fixture: {fixture['espn_away']} @ {fixture['espn_home']}"
          f"  yes={fixture['yes_team']}"
          f"  ({'home' if fixture['yes_is_home'] else 'away'})")
    print(f"  Kalshi {len(candles)} candles | ESPN {len(wp)} winprob points")
