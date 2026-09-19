"""Why do ~10% of MLB markets end at the opposite extreme from ESPN?

The last-row gap is median 0.010 and p90 0.990 -- most agree almost
exactly and a large minority are maximally opposed. That is not noise,
it is a population of rows joined to the WRONG GAME.

Prime suspect: doubleheaders. Two games between the same teams on the
same date share a fixture key, and match_event_near returns the first
one it finds, so roughly half of those pairings are wrong. The Kalshi
ticker distinguishes them and the current parser throws that away --
KXMLBHIT-...LADNYYG2-... carries a G2.

Second suspect: the 43.59h maximum curve span. No baseball game runs 43
hours; that is a suspended or resumed game, and its plays would
straddle two days.

The test that settles it: Kalshi and ESPN each record a winner
independently. If they disagree, the games are different -- no
interpretation needed.
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


def jpath(*p):
    return os.path.join(ROOT, *p)


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


events_by_date = collections.defaultdict(list)
known = set()
for path in sorted(glob.glob(jpath("truth_backfill", "mlb_winprob",
                                   "*.jsonl.gz"))):
    for row in read_jsonl(path):
        events_by_date[(row.get("date") or "")[:10]].append(row)
        known |= {c.get("abbrev") for c in (row.get("competitors") or [])
                  if c.get("abbrev")}

# --- how common are same-fixture-same-day repeats? -----------------------
dupes = 0
total_fixtures = 0
for day, evs in events_by_date.items():
    seen = collections.Counter()
    for ev in evs:
        sides = {c.get("home_away"): c.get("abbrev")
                 for c in ev.get("competitors") or []}
        seen[(sides.get("away"), sides.get("home"))] += 1
    for key, n in seen.items():
        total_fixtures += 1
        if n > 1:
            dupes += 1

print(f"ESPN fixtures: {total_fixtures:,}, of which "
      f"{dupes:,} have MORE THAN ONE game that day "
      f"({100.0*dupes/max(total_fixtures,1):.1f}%)")
print("  every one of these is a coin flip for a matcher that takes the"
      " first\n")

# --- does Kalshi's result agree with ESPN's winner? ----------------------
agree = disagree = unknown = 0
disagreeing_examples = []
dupe_disagree = 0

count = 0
for path in sorted(glob.glob(jpath("backfill", "KXMLBGAME", "*.jsonl.gz"))):
    if count >= LIMIT:
        break
    for market in read_jsonl(path):
        if count >= LIMIT:
            break
        fixture = mj.fixture_for(market.get("ticker", ""), known)
        if not fixture:
            continue
        event = mj.match_event_near(fixture, events_by_date)
        if not event:
            continue
        count += 1

        winner = None
        for c in event.get("competitors") or []:
            if c.get("winner"):
                winner = c.get("abbrev")
        if winner is None:
            unknown += 1
            continue
        yes_espn = fixture["espn_home"] if fixture["yes_is_home"] \
            else fixture["espn_away"]
        kalshi_yes = market.get("result") == "yes"
        if (winner == yes_espn) == kalshi_yes:
            agree += 1
        else:
            disagree += 1
            # Was there more than one game for this fixture that day?
            same_day = [
                e for e in events_by_date.get(fixture["date"], [])
                if {c.get("home_away"): c.get("abbrev")
                    for c in e.get("competitors") or []}
                == {"away": fixture["espn_away"],
                    "home": fixture["espn_home"]}
            ]
            if len(same_day) > 1:
                dupe_disagree += 1
            if len(disagreeing_examples) < 8:
                disagreeing_examples.append(
                    (market["ticker"], market.get("result"), winner,
                     yes_espn, len(same_day)))

print(f"of {count:,} joined markets:")
print(f"  agree    {agree:>6,}")
print(f"  DISAGREE {disagree:>6,}  ({100.0*disagree/max(count,1):.1f}%)")
print(f"  no winner recorded {unknown:>6,}")
print(f"\n  of the disagreements, {dupe_disagree:,} are on a fixture with"
      f" MORE THAN ONE game that day")

if disagreeing_examples:
    print(f"\n  {'ticker':<36} {'kalshi':<7} {'espn won':<9} "
          f"{'yes side':<9} games")
    for t, res, won, yes, n in disagreeing_examples:
        print(f"  {t[:36]:<36} {str(res):<7} {won:<9} {yes:<9} {n}")

# --- the 43-hour curve ---------------------------------------------------
print("\n=== longest play spans ===")
longest = []
for path in sorted(glob.glob(jpath("truth_backfill", "mlb_plays",
                                   "*.jsonl.gz"))):
    for row in read_jsonl(path):
        stamps = [ds.to_epoch(p.get("wallclock"))
                  for p in (row.get("plays") or []) if p.get("wallclock")]
        stamps = [s for s in stamps if s]
        if len(stamps) > 1:
            longest.append(((max(stamps) - min(stamps)) / 3600,
                            row.get("name"), row.get("date")))
longest.sort(reverse=True)
for hours, name, date in longest[:5]:
    print(f"  {hours:>6.1f}h  {name}  {date}")
