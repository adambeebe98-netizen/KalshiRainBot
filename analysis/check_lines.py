"""Do ESPN's spread/total probabilities refer to Kalshi's line?

The core API gives spreadCoverProbHome and totalOverProb per play, and
it is tempting to drop them straight into the spread and total markets
as the independent estimate. That would be wrong unless the LINES
agree.

Kalshi lists a ladder -- "wins by over 1.5", "over 2.5", "over 3.5" --
each its own market. ESPN publishes a probability against ONE line, its
own posted spread or total. P(NE covers 3.5) and P(NE covers 7.5) are
different numbers about different questions, and pairing a price for
one with a probability for the other produces a row that looks fine and
means nothing.

So: how many distinct lines does Kalshi list per game, what line does
ESPN quote, and how often do they coincide?
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


def _epoch(value):
    if value is None:
        return None
    try:
        return int(dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None

ROOT = sys.argv[1] if len(sys.argv) > 1 else r"D:\kalshi-data"


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


# ESPN's posted lines per event.
espn_lines = {}
for path in glob.glob(os.path.join(ROOT, "truth_backfill", "nfl_odds",
                                   "*.jsonl.gz")):
    for row in read_jsonl(path):
        books = row.get("pickcenter") or []
        if not books:
            continue
        b = books[0]
        espn_lines[row["event_id"]] = {
            "spread": b.get("spread"),
            "total": b.get("overUnder"),
            "provider": (b.get("provider") or {}).get("name"),
            "date": (row.get("date") or "")[:10],
            "competitors": row.get("competitors"),
        }

print(f"ESPN odds available for {len(espn_lines):,} NFL games")

events_by_date = collections.defaultdict(list)
for path in glob.glob(os.path.join(ROOT, "truth_backfill", "nfl_boxscore",
                                   "*.jsonl.gz")):
    for row in read_jsonl(path):
        events_by_date[(row.get("date") or "")[:10]].append(row)

for series, field in (("KXNFLSPREAD", "spread"), ("KXNFLTOTAL", "total")):
    per_game = collections.defaultdict(set)
    matched = collections.Counter()
    for path in sorted(glob.glob(os.path.join(ROOT, "backfill", series,
                                              "*.jsonl.gz"))):
        for row in read_jsonl(path):
            f = mj.fixture_for(row["ticker"], None, row.get("yes_sub_title"))
            if not f or f["line"] is None:
                continue
            ev = mj.match_event_at(f, events_by_date,
                                   _epoch(row.get("close_time")))
            if not ev:
                continue
            per_game[ev["event_id"]].add(f["line"])

    ladder = [len(v) for v in per_game.values()]
    ladder.sort()
    print(f"\n{series}: {len(per_game):,} games")
    if ladder:
        print(f"  distinct Kalshi lines per game: "
              f"median {ladder[len(ladder)//2]}, max {ladder[-1]}")

    hits = misses = no_odds = 0
    examples = []
    for eid, lines in per_game.items():
        info = espn_lines.get(eid)
        if not info or info.get(field) is None:
            no_odds += 1
            continue
        espn_line = abs(float(info[field]))
        if any(abs(l - espn_line) < 0.01 for l in lines):
            hits += 1
        else:
            misses += 1
            if len(examples) < 4:
                examples.append((eid, sorted(lines), espn_line))
    print(f"  ESPN's line appears in Kalshi's ladder: {hits:,}")
    print(f"  does NOT appear:                       {misses:,}")
    print(f"  no ESPN odds for the game:             {no_odds:,}")
    for eid, lines, espn_line in examples:
        print(f"    {eid}: kalshi {lines}  espn {espn_line}")
