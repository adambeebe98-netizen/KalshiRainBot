"""Why does the state read 0-0 at the close of a game that scored?

Ten markets out of 8,185 disagree with their own settlement, and they
are not scattered: six of them are one game (25AUG22PHINYJ) reading a
final total of 0 across six different lines. A 0-0 NFL game is not a
plausible reading, so the state series is being cut short for these.

Three candidates:
  A. the market's close_time falls BEFORE the game finished, so
     as_of(close) legitimately returns an early state
  B. the plays carry wallclock only up to a point, so later scoring
     never enters the series
  C. the wrong event was matched

Print the market window against the play window and the last few plays.
Whatever the cause, the fix is not to loosen the check -- it is to know
whether these rows are unusable or merely early.
"""
from __future__ import annotations

import collections
import datetime as dt
import glob
import gzip
import json
import os
import sys

import dataset as ds
import market_join as mj

ROOT = sys.argv[1] if len(sys.argv) > 1 else r"D:\kalshi-data"
TARGETS = sys.argv[2:] or ["25AUG22PHINYJ", "25AUG23LACLE"]


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def ts(v):
    return (dt.datetime.fromtimestamp(v, dt.timezone.utc).strftime("%m-%d %H:%M")
            if v else "-")


plays_by_event = {}
for path in sorted(glob.glob(os.path.join(ROOT, "espn_core", "nfl_plays",
                                          "*.jsonl.gz"))):
    for row in read_jsonl(path):
        plays_by_event[row["event_id"]] = row

events_by_date, known = collections.defaultdict(list), set()
for path in sorted(glob.glob(os.path.join(ROOT, "truth_backfill",
                                          "nfl_boxscore", "*.jsonl.gz"))):
    for row in read_jsonl(path):
        events_by_date[(row.get("date") or "")[:10]].append(row)
        known |= {c.get("abbrev") for c in (row.get("competitors") or [])
                  if c.get("abbrev")}

for target in TARGETS:
    print(f"\n{'=' * 70}\n{target}")
    found = None
    for path in sorted(glob.glob(os.path.join(ROOT, "backfill",
                                              "KXNFLTOTAL", "*.jsonl.gz"))):
        for m in read_jsonl(path):
            if target in m["ticker"]:
                found = m
                break
        if found:
            break
    if not found:
        print("  market not found")
        continue

    f = mj.fixture_for(found["ticker"], known, found.get("yes_sub_title"))
    close = ds.to_epoch(found.get("close_time"))
    print(f"  ticker     {found['ticker']}  result={found.get('result')}")
    print(f"  open_time  {found.get('open_time')}")
    print(f"  close_time {found.get('close_time')}")
    print(f"  fixture    {f['espn_away']} @ {f['espn_home']} on {f['date']}")

    ev = mj.match_event_at(f, events_by_date, close)
    if not ev:
        print("  NO EVENT MATCHED")
        continue
    print(f"  matched    {ev.get('name')}  id={ev['event_id']}  "
          f"date={ev.get('date')}")
    scores = {c.get("abbrev"): c.get("score")
              for c in ev.get("competitors") or []}
    print(f"  ESPN final score: {scores}")

    row = plays_by_event.get(ev["event_id"])
    if not row:
        print("  NO CORE PLAYS STORED")
        continue
    plays = row.get("plays") or []
    timed = [p for p in plays if p.get("wallclock")]
    print(f"  plays {len(plays)}, with wallclock {len(timed)}")
    if timed:
        first = ds.to_epoch(timed[0]["wallclock"])
        last = ds.to_epoch(timed[-1]["wallclock"])
        print(f"  play window  {ts(first)} .. {ts(last)}")
        print(f"  market close {ts(close)}")
        if close and last and close < first:
            print("  -> MARKET CLOSED BEFORE THE FIRST TIMED PLAY")
        elif close and last and close < last:
            print("  -> market closed mid-game; state at close is genuinely"
                  " early")
        else:
            print("  -> market closed after the last play; state should be"
                  " final")
    print("  last 4 plays:")
    for p in plays[-4:]:
        print(f"    seq={p.get('seq')} Q{p.get('period')} "
              f"{p.get('clock')}  {p.get('away_score')}-{p.get('home_score')}"
              f"  wall={p.get('wallclock')}  {str(p.get('type'))[:24]}")

    state = ds.build_state(plays, f)
    if len(state):
        print(f"  state points {len(state)}; span {ts(state.span[0])} .. "
              f"{ts(state.span[1])}")
        at_close = state.as_of(close) if close else None
        print(f"  state at close: {at_close}")
