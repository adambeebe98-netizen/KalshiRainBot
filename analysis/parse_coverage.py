"""How much of the archive does the ticker parser actually reach?

Tests prove the shapes I thought of work. This measures the ones I did
not, across every backfilled market -- and reports the failures by
example, because a parser that quietly reaches 80% leaves a hole that
is never random.

Two separate questions, deliberately kept apart:

  parsed    the ticker yielded a date, a fixture and an outcome
  resolved  the team blob split into two codes ESPN also knows

A market can parse and still fail to resolve, if its league's team
codes are not in the known set. That is a different problem with a
different fix, and merging the two counts would hide it.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import os
import sys

import market_join as mj

ROOT = sys.argv[1] if len(sys.argv) > 1 else r"D:\kalshi-data"


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


# Build the known-code set per league from the ESPN side.
known_by_league = {}
for league in ("nfl", "mlb", "epl", "laliga", "seriea", "bundesliga",
               "ligue1"):
    codes = set()
    for path in glob.glob(os.path.join(ROOT, "truth_backfill",
                                       f"{league}_boxscore", "*.jsonl.gz")):
        for row in read_jsonl(path):
            codes |= {c.get("abbrev") for c in (row.get("competitors") or [])
                      if c.get("abbrev")}
    known_by_league[league] = codes

SERIES_LEAGUE = {
    "KXNFLGAME": "nfl", "KXNFLSPREAD": "nfl", "KXNFLTOTAL": "nfl",
    "KXMLBGAME": "mlb", "KXMLBHIT": "mlb", "KXMLBTB": "mlb",
    "KXMLBHRR": "mlb", "KXMLBRBI": "mlb", "KXMLBSB": "mlb",
    "KXMLBKS": "mlb", "KXMLBHR": "mlb",
    "KXEPLGAME": "epl", "KXEPLSPREAD": "epl", "KXEPLTOTAL": "epl",
    "KXLALIGAGAME": "laliga", "KXLALIGATOTAL": "laliga",
    "KXSERIEAGAME": "seriea", "KXSERIEATOTAL": "seriea",
    "KXBUNDESLIGAGAME": "bundesliga", "KXLIGUE1GAME": "ligue1",
}

all_known = set().union(*known_by_league.values())

print(f"{'series':<20} {'markets':>9} {'parsed':>8} {'resolved':>9} "
      f"{'kind':<8}")
print("-" * 60)

grand = collections.Counter()
failures = collections.defaultdict(list)

for series_dir in sorted(glob.glob(os.path.join(ROOT, "backfill", "*"))):
    series = os.path.basename(series_dir)
    if series.startswith("_"):
        continue
    known = known_by_league.get(SERIES_LEAGUE.get(series), all_known)

    n = parsed = resolved = 0
    kinds = collections.Counter()
    for path in sorted(glob.glob(os.path.join(series_dir, "*.jsonl.gz"))):
        for row in read_jsonl(path):
            n += 1
            ticker = row.get("ticker", "")
            pm = mj.parse_market(ticker, row.get("yes_sub_title"))
            if pm:
                parsed += 1
                kinds[pm["kind"]] += 1
            f = mj.fixture_for(ticker, known, row.get("yes_sub_title"))
            if f:
                resolved += 1
            elif pm and len(failures[series]) < 3:
                failures[series].append(ticker)

    grand["markets"] += n
    grand["parsed"] += parsed
    grand["resolved"] += resolved
    top = kinds.most_common(1)[0][0] if kinds else "-"
    print(f"{series:<20} {n:>9,} {parsed:>8,} {resolved:>9,} {top:<8}")

print("-" * 60)
print(f"{'TOTAL':<20} {grand['markets']:>9,} {grand['parsed']:>8,} "
      f"{grand['resolved']:>9,}")
print(f"\nparsed   {100.0*grand['parsed']/max(grand['markets'],1):.1f}%"
      f"   resolved {100.0*grand['resolved']/max(grand['markets'],1):.1f}%")

print("\nparsed but could not resolve a fixture:")
for series, examples in sorted(failures.items()):
    if examples:
        print(f"  {series}: {examples[0]}")
