"""Can the win-probability curve be placed on a clock?

The whole value of the joined dataset is comparing a price at time t
with an independent probability at time t. That needs the probability
points to carry, or resolve to, a timestamp.

The live poller's own record showed winprobability entries as
{homeWinPercentage, tiePercentage, playId} -- a play reference, not a
time. If a playId cannot be resolved to a wallclock, the curve has an
ORDER but no clock, and pairing it with hourly candles would mean
assuming plays are evenly spaced. They are not: a two-minute drill and a
first-quarter possession are the same number of plays and wildly
different durations. That assumption would quietly misalign every row.

MLB stores play-by-play with wallclock, so there the mapping should
exist. Historical NFL returned plays=0, so there it may not.

Checked rather than assumed, because a misaligned dataset trains fine
and is worthless.
"""
from __future__ import annotations

import glob
import gzip
import json


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def first_row(pattern):
    for path in sorted(glob.glob(pattern)):
        for row in read_jsonl(path):
            return row
    return None


for league in ("nfl", "mlb"):
    print(f"\n{'=' * 60}\n{league.upper()}\n{'=' * 60}")

    wp_row = first_row(f"data/truth_backfill/{league}_winprob/*.jsonl.gz")
    if not wp_row:
        print("  no winprob rows yet")
        continue
    wp = wp_row.get("winprobability") or []
    print(f"  {wp_row.get('name')}  {wp_row.get('date')}")
    print(f"  {len(wp)} win-probability points")
    if wp:
        print(f"  point keys: {sorted(wp[0].keys())}")
        print(f"  first: {json.dumps(wp[0])[:160]}")
        print(f"  last : {json.dumps(wp[-1])[:160]}")
        has_time = [k for k in wp[0]
                    if "time" in k.lower() or "clock" in k.lower()
                    or "date" in k.lower()]
        print(f"  time-like keys: {has_time or 'NONE'}")

    play_row = first_row(f"data/truth_backfill/{league}_plays/*.jsonl.gz")
    if not play_row:
        print("  NO PLAYS STORED -- playId cannot be resolved to a clock")
        continue
    plays = play_row.get("plays") or []
    print(f"\n  {len(plays)} plays stored")
    if plays:
        print(f"  play keys: {sorted(plays[0].keys())[:14]}")
        wall = sum(1 for p in plays if p.get("wallclock"))
        print(f"  plays with wallclock: {wall}/{len(plays)}")
        ids = {str(p.get("id")) for p in plays}
        wp_ids = {str(p.get("playId")) for p in wp if p.get("playId")}
        if wp_ids:
            hits = len(wp_ids & ids)
            print(f"  winprob playIds resolvable to a play: "
                  f"{hits}/{len(wp_ids)}")
            if hits:
                print("  -> the curve CAN be placed on a clock")
            else:
                print("  -> playIds do not match this game's plays")
