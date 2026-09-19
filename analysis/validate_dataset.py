"""End-to-end check of the training-row pipeline on real MLB data.

MLB is the right place to prove this: 4,778 games with play-by-play,
win probability AND boxscore, and a KXMLBGAME ticker the join already
parses. If rows come out coherent here, the pipeline works and the
remaining gaps are parsing, not design.

What "coherent" has to mean, given how easy it is to fake:

  * the curve spans the game, not a single instant
  * pregame candles carry NO independent estimate
  * the estimate on an in-game row is one that existed at that time
  * late rows agree with the outcome more often than early ones --
    an independent estimate that does not sharpen as the game
    progresses is not tracking the game
  * price and estimate agree closely at the end

The last is the giveaway for a misalignment: if they disagree at
settlement, the row is fiction.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import sys

import dataset as ds
import market_join as mj

KALSHI_GLOB = "data/backfill/KXMLBGAME/*.jsonl.gz"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 400


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


# --- index the ESPN side by date -----------------------------------------
plays_by_event, wp_by_event = {}, {}
events_by_date = collections.defaultdict(list)
known = set()

for path in sorted(glob.glob("data/truth_backfill/mlb_plays/*.jsonl.gz")):
    for row in read_jsonl(path):
        plays_by_event[row["event_id"]] = row.get("plays") or []
for path in sorted(glob.glob("data/truth_backfill/mlb_winprob/*.jsonl.gz")):
    for row in read_jsonl(path):
        wp_by_event[row["event_id"]] = row.get("winprobability") or []
        events_by_date[(row.get("date") or "")[:10]].append(row)
        known |= {c.get("abbrev") for c in (row.get("competitors") or [])
                  if c.get("abbrev")}

print(f"ESPN: {len(plays_by_event):,} games with plays, "
      f"{len(wp_by_event):,} with win probability")

# --- walk the Kalshi side ------------------------------------------------
stats = collections.Counter()
all_rows = []
curve_spans = []

for path in sorted(glob.glob(KALSHI_GLOB)):
    if stats["markets_used"] >= LIMIT:
        break
    for market in read_jsonl(path):
        if stats["markets_used"] >= LIMIT:
            break
        stats["markets_seen"] += 1
        fixture = mj.fixture_for(market.get("ticker", ""), known)
        if not fixture:
            stats["unparseable"] += 1
            continue
        event = mj.match_event_near(fixture, events_by_date)
        if not event:
            stats["no_event"] += 1
            continue
        eid = event["event_id"]
        fixture["event_id"] = eid
        curve = ds.build_curve(plays_by_event.get(eid, []),
                               wp_by_event.get(eid, []), fixture)
        if not len(curve):
            stats["empty_curve"] += 1
            continue
        curve_spans.append(curve.span)
        rows = ds.candle_rows(market, curve, fixture, "KXMLBGAME")
        all_rows.extend(rows)
        stats["markets_used"] += 1

print(f"\nmarkets seen {stats['markets_seen']:,}, used "
      f"{stats['markets_used']:,}, unparseable {stats['unparseable']:,}, "
      f"no event {stats['no_event']:,}, empty curve {stats['empty_curve']:,}")
print(f"rows built: {len(all_rows):,}")

if not all_rows:
    raise SystemExit("no rows -- nothing to validate")

# --- does the curve actually span a game? --------------------------------
durations = [(b - a) / 3600 for a, b in curve_spans if a and b]
durations.sort()
print(f"\ncurve span (hours): median {durations[len(durations)//2]:.2f}, "
      f"min {durations[0]:.2f}, max {durations[-1]:.2f}")
print("  a real baseball game is ~3h; near-zero would mean the plays all"
      " carry one timestamp")

# --- point-in-time discipline --------------------------------------------
in_game = [r for r in all_rows if r["in_game"]]
pregame = [r for r in all_rows if not r["in_game"]]
print(f"\nrows in game {len(in_game):,}  pregame {len(pregame):,}")
print(f"  pregame rows carrying an estimate: "
      f"{sum(1 for r in pregame if r['p_independent'] is not None)}"
      f"   (must be 0)")

# --- does the estimate sharpen as the game runs? -------------------------
# Bucket in-game rows by how far from close they are, and measure how
# often the independent estimate is on the right side of 0.5.
buckets = collections.defaultdict(lambda: [0, 0])
for r in in_game:
    if r["label"] is None or r["p_independent"] is None:
        continue
    secs = r["seconds_to_close"]
    if secs is None:
        continue
    key = ("> 2h" if secs > 7200 else "1-2h" if secs > 3600
           else "30-60m" if secs > 1800 else "< 30m")
    correct = (r["p_independent"] > 0.5) == (r["label"] == 1)
    buckets[key][0] += int(correct)
    buckets[key][1] += 1

print("\nindependent estimate on the correct side of 0.5:")
for key in ("> 2h", "1-2h", "30-60m", "< 30m"):
    hit, n = buckets[key]
    if n:
        print(f"  {key:<8} {100.0*hit/n:>5.1f}%   ({n:,} rows)")
print("  this must RISE toward the end; flat means it is not tracking"
      " the game")

# --- price vs estimate at the very end -----------------------------------
last_by_ticker = {}
for r in all_rows:
    if r["p_independent"] is None or r["price"] is None:
        continue
    cur = last_by_ticker.get(r["ticker"])
    if cur is None or r["ts"] > cur["ts"]:
        last_by_ticker[r["ticker"]] = r
gaps = sorted(abs(r["price"] - r["p_independent"])
              for r in last_by_ticker.values())
if gaps:
    print(f"\n|price - estimate| on the last row of each market:")
    print(f"  median {gaps[len(gaps)//2]:.3f}   "
          f"p90 {gaps[int(len(gaps)*0.9)]:.3f}   max {gaps[-1]:.3f}")

# --- what an edge would look like ----------------------------------------
edges = [e for e in (ds.edge(r) for r in in_game) if e is not None]
edges.sort()
if edges:
    print(f"\nedge (estimate - price), {len(edges):,} in-game rows:")
    print(f"  p10 {edges[int(len(edges)*0.1)]:+.3f}   "
          f"median {edges[len(edges)//2]:+.3f}   "
          f"p90 {edges[int(len(edges)*0.9)]:+.3f}")
    print("  DIAGNOSTIC ONLY -- ignores fees, spread and whether anything"
          " was on the book, all of which killed candidates before.")
