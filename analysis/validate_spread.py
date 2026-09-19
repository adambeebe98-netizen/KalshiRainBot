"""Does the game state actually settle the spread and total markets?

This is the falsifiable version of "the pipeline works". A spread market
says "NE wins by over 3.5" and settles YES exactly when the final margin
exceeds 3.5. A total says "over 37.5 points" and settles YES exactly
when the combined score does.

So take the LAST state before each market closed, apply the market's own
line, and compare to how Kalshi actually settled it. Agreement means the
join, the score series, the line parsing and the home/away orientation
are all correct together -- any one of them wrong shows up here. It is a
much harder test than "the numbers look plausible".

NFL is the case that needs it: ESPN's own spread probability quotes one
whole-number line against Kalshi's ladder of half-points, so score and
clock are the only ground truth available for these markets.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import os
import sys

import dataset as ds
import market_join as mj

ROOT = sys.argv[1] if len(sys.argv) > 1 else r"D:\kalshi-data"
SERIES = sys.argv[2] if len(sys.argv) > 2 else "KXNFLSPREAD"


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


# Core-API plays carry wallclock and scores for NFL; the site API does
# not return plays for finished NFL games at all.
plays_by_event = {}
for path in sorted(glob.glob(os.path.join(ROOT, "espn_core", "nfl_plays",
                                          "*.jsonl.gz"))):
    for row in read_jsonl(path):
        plays_by_event[row["event_id"]] = row.get("plays") or []

events_by_date, known = collections.defaultdict(list), set()
for path in sorted(glob.glob(os.path.join(ROOT, "truth_backfill",
                                          "nfl_boxscore", "*.jsonl.gz"))):
    for row in read_jsonl(path):
        events_by_date[(row.get("date") or "")[:10]].append(row)
        known |= {c.get("abbrev") for c in (row.get("competitors") or [])
                  if c.get("abbrev")}

print(f"{len(plays_by_event):,} NFL games with core-API plays")

stats = collections.Counter()
mismatches = []

for path in sorted(glob.glob(os.path.join(ROOT, "backfill", SERIES,
                                          "*.jsonl.gz"))):
    for market in read_jsonl(path):
        stats["markets"] += 1
        f = mj.fixture_for(market["ticker"], known,
                           market.get("yes_sub_title"))
        if not f:
            stats["unparsed"] += 1
            continue
        # A winner market has no line: it pays out on the sign of the
        # margin. Draws are excluded because a three-way outcome is not
        # settled by "margin > 0".
        needs_line = f["kind"] in ("spread", "total")
        if needs_line and f["line"] is None:
            stats["no_line"] += 1
            continue
        if f["kind"] == "winner" and f["is_draw"]:
            stats["draw"] += 1
            continue
        close = ds.to_epoch(market.get("close_time"))
        ev = mj.match_event_at(f, events_by_date, close)
        if not ev:
            stats["no_event"] += 1
            continue
        plays = plays_by_event.get(ev["event_id"])
        if not plays:
            stats["no_plays"] += 1
            continue

        state = ds.build_state(plays, f)
        # The FINAL state, not the state at close_time. Kalshi's
        # close_time can fall mid-game -- PHI@NYJ ran 23:36 to 02:43 and
        # records a close of 23:55, in the first quarter -- while the
        # market still settles on the final score. Settlement is the
        # thing being checked here, so the end of the game is the right
        # reference; the dataset itself still uses as_of(t) per candle,
        # which is correct at every t.
        span = state.span
        final = state.as_of(span[1]) if span else None
        if not final:
            stats["no_final_state"] += 1
            continue
        if close and span and close < span[1]:
            stats["closed_mid_game"] += 1

        if f["kind"] == "total":
            actual = final["total_score"]
        else:
            actual = final["margin_for_yes"]
        if actual is None:
            stats["no_measure"] += 1
            continue

        # Winner: the sign of the margin. Spread/total: against the line.
        threshold = 0.0 if f["kind"] == "winner" else f["line"]
        implied_yes = actual > threshold
        settled_yes = market.get("result") == "yes"
        if implied_yes == settled_yes:
            stats["agree"] += 1
        else:
            stats["DISAGREE"] += 1
            if len(mismatches) < 8:
                mismatches.append(
                    (market["ticker"], f["line"], actual,
                     market.get("result"), (market.get("yes_sub_title") or "")[:40]))

checked = stats["agree"] + stats["DISAGREE"]
print(f"\n{SERIES}: {stats['markets']:,} markets")
for key in ("unparsed", "no_line", "draw", "no_event", "no_plays",
            "no_final_state", "no_measure"):
    if stats[key]:
        print(f"  skipped, {key:<16} {stats[key]:,}")

if stats["closed_mid_game"]:
    print(f"\n  NOTE: {stats['closed_mid_game']:,} markets record a "
          f"close_time BEFORE the game ended,")
    print("  yet settle on the final score. close_time is not a reliable"
          " end-of-event marker.")

print(f"\nstate vs Kalshi settlement, on {checked:,} markets:")
print(f"  agree    {stats['agree']:>6,}")
print(f"  DISAGREE {stats['DISAGREE']:>6,}  "
      f"({100.0*stats['DISAGREE']/max(checked,1):.2f}%)")

if mismatches:
    print(f"\n  {'ticker':<34} {'line':>6} {'actual':>7} {'settled':<8} note")
    for t, line, actual, res, note in mismatches:
        print(f"  {t[:34]:<34} {line:>6.1f} {actual:>7} {str(res):<8} {note}")
else:
    print("\n  no mismatches -- the score series settles every market it"
          " was checked against")
