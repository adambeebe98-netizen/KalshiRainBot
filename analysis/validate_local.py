"""End-to-end check of the training-row pipeline, run where the RAM is.

The droplet attempt died exactly as it should have: the kernel OOM-killed
it at 643 MB RSS on a box with 186 MB free. Loading 4,778 games of
play-by-play was never going to fit, and the module docstring already
said this belongs on the machine with the disk and the GPU. It does.

MLB is the right place to prove the pipeline: 4,778 games carrying
play-by-play, win probability AND boxscore, with a KXMLBGAME ticker the
join already parses. If the rows are coherent here, what remains is
ticker parsing for the other families, not a design problem.

Coherent has to mean something falsifiable, because a misaligned
dataset looks fine:

  * the curve spans a real game, not one instant
  * pregame candles carry NO estimate
  * the estimate sharpens as the game runs -- one that does not is
    not tracking anything
  * price and estimate agree at settlement; disagreement there means
    the rows are fiction

Usage:  python validate_local.py [DATA_ROOT] [MARKET_LIMIT]
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
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 3000


def jpath(*parts) -> str:
    return os.path.join(ROOT, *parts)


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


print(f"data root: {ROOT}")

# --- ESPN side -----------------------------------------------------------
# Plays are the bulk of this. Keep only id -> wallclock rather than the
# whole play: 4,778 games times ~550 plays is a lot of dictionaries to
# hold for two fields.
clock_by_event: dict[str, dict[str, int]] = {}
for path in sorted(glob.glob(jpath("truth_backfill", "mlb_plays",
                                   "*.jsonl.gz"))):
    for row in read_jsonl(path):
        clock_by_event[row["event_id"]] = {
            str(p["id"]): ds.to_epoch(p.get("wallclock"))
            for p in (row.get("plays") or [])
            if p.get("id") is not None and p.get("wallclock")
        }

wp_by_event, events_by_date, known = {}, collections.defaultdict(list), set()
for path in sorted(glob.glob(jpath("truth_backfill", "mlb_winprob",
                                   "*.jsonl.gz"))):
    for row in read_jsonl(path):
        wp_by_event[row["event_id"]] = row.get("winprobability") or []
        events_by_date[(row.get("date") or "")[:10]].append(row)
        known |= {c.get("abbrev") for c in (row.get("competitors") or [])
                  if c.get("abbrev")}

total_plays = sum(len(v) for v in clock_by_event.values())
print(f"ESPN: {len(clock_by_event):,} games, {total_plays:,} timed plays, "
      f"{len(wp_by_event):,} curves")


def curve_for(event_id: str, fixture: dict) -> ds.ProbabilityCurve:
    clock = clock_by_event.get(event_id, {})
    placed = []
    for point in wp_by_event.get(event_id, []):
        pid = point.get("playId") or point.get("play_id")
        home = point.get("homeWinPercentage")
        if pid is None or home is None:
            continue
        stamp = clock.get(str(pid))
        if stamp is None:
            continue
        placed.append((stamp, {"p_yes": mj.probability_for_yes(home, fixture),
                               "p_home": float(home)}))
    return ds.ProbabilityCurve(placed)


# --- Kalshi side ---------------------------------------------------------
stats = collections.Counter()
rows, spans = [], []

for path in sorted(glob.glob(jpath("backfill", "KXMLBGAME", "*.jsonl.gz"))):
    if stats["used"] >= LIMIT:
        break
    for market in read_jsonl(path):
        if stats["used"] >= LIMIT:
            break
        stats["seen"] += 1
        fixture = mj.fixture_for(market.get("ticker", ""), known)
        if not fixture:
            stats["unparseable"] += 1
            continue
        # Match on START TIME, not date. Baseball teams play multi-day
        # series and doubleheaders, so a date-only match put 10.9% of
        # these markets on the wrong game.
        event = mj.match_event_at(fixture, events_by_date,
                                  ds.to_epoch(market.get("close_time")))
        if not event:
            stats["no_event"] += 1
            continue
        fixture["event_id"] = event["event_id"]
        curve = curve_for(event["event_id"], fixture)
        if not len(curve):
            stats["empty_curve"] += 1
            continue
        spans.append(curve.span)
        rows.extend(ds.candle_rows(market, curve, fixture, "KXMLBGAME"))
        stats["used"] += 1

        winner = next((c.get("abbrev") for c in event.get("competitors") or []
                       if c.get("winner")), None)
        if winner is not None:
            yes_espn = (fixture["espn_home"] if fixture["yes_is_home"]
                        else fixture["espn_away"])
            if (winner == yes_espn) == (market.get("result") == "yes"):
                stats["agree"] += 1
            else:
                stats["settle_disagree"] += 1

print(f"\nmarkets: seen {stats['seen']:,}  used {stats['used']:,}  "
      f"unparseable {stats['unparseable']:,}  no-event {stats['no_event']:,}"
      f"  empty-curve {stats['empty_curve']:,}")
print(f"rows: {len(rows):,}")
if not rows:
    raise SystemExit("no rows")

# The one check that needs no interpretation: both sources record a
# winner independently, so a disagreement means different games.
print(f"\nKalshi settlement vs ESPN's recorded winner:")
print(f"  agree    {stats['agree']:>6,}")
print(f"  DISAGREE {stats['settle_disagree']:>6,}  "
      f"({100.0*stats['settle_disagree']/max(stats['used'],1):.1f}%)")

hours = sorted((b - a) / 3600 for a, b in spans if a and b)
print(f"\ncurve span (h): median {hours[len(hours)//2]:.2f}  "
      f"min {hours[0]:.2f}  max {hours[-1]:.2f}   (a game is ~3h)")

in_game = [r for r in rows if r["in_game"]]
pregame = [r for r in rows if not r["in_game"]]
leaked = sum(1 for r in pregame if r["p_independent"] is not None)
print(f"\nin-game {len(in_game):,}  pregame {len(pregame):,}  "
      f"pregame rows carrying an estimate: {leaked}  (must be 0)")

buckets = collections.defaultdict(lambda: [0, 0])
for r in in_game:
    if r["label"] is None or r["p_independent"] is None:
        continue
    s = r["seconds_to_close"]
    if s is None:
        continue
    key = ("> 2h" if s > 7200 else "1-2h" if s > 3600
           else "30-60m" if s > 1800 else "< 30m")
    buckets[key][0] += int((r["p_independent"] > 0.5) == (r["label"] == 1))
    buckets[key][1] += 1

print("\nindependent estimate on the correct side of 0.5:")
for key in ("> 2h", "1-2h", "30-60m", "< 30m"):
    hit, n = buckets[key]
    if n:
        print(f"  {key:<8} {100.0*hit/n:>5.1f}%   ({n:,} rows)")

last = {}
for r in rows:
    if r["p_independent"] is None or r["price"] is None:
        continue
    if r["ticker"] not in last or r["ts"] > last[r["ticker"]]["ts"]:
        last[r["ticker"]] = r
gaps = sorted(abs(r["price"] - r["p_independent"]) for r in last.values())
if gaps:
    print(f"\n|price - estimate| on each market's last row: "
          f"median {gaps[len(gaps)//2]:.3f}  "
          f"p90 {gaps[int(len(gaps)*0.9)]:.3f}  max {gaps[-1]:.3f}")

edges = sorted(e for e in (ds.edge(r) for r in in_game) if e is not None)
if edges:
    print(f"\nedge (estimate - price) over {len(edges):,} in-game rows: "
          f"p10 {edges[int(len(edges)*0.1)]:+.3f}  "
          f"median {edges[len(edges)//2]:+.3f}  "
          f"p90 {edges[int(len(edges)*0.9)]:+.3f}")
    print("  DIAGNOSTIC ONLY -- no fees, no spread, no check that anything"
          " was on the book. All three killed candidates before.")
