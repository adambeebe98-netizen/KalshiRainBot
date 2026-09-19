"""Is there a real clock on NFL plays, or only a game clock?

The core API gives 198 plays and 198 probability points for a finished
NFL game, with `clock`, `period`, `secondsLeft`, `sequenceNumber`, and
`modified` / `lastModified`.

Game clock is NOT wall clock. Fifteen minutes of fourth quarter can take
forty minutes of real time, and the Kalshi candles are indexed by real
time. Pairing a price at 23:15 UTC with a probability at "6:42 left in
Q4" needs a mapping between the two.

So: are `modified` and `lastModified` genuine timestamps, do they move
monotonically through the game, and do they span the market's actual
open window? If yes, NFL aligns as well as MLB does. If they are all
identical -- a bulk write when the game was archived -- they are
useless for this and NFL win probability stays unusable for
time-alignment.
"""
import datetime as dt
import json

import httpx

CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
EVENT_ID = "401772983"      # HOU @ NE, 2026-01-18

client = httpx.Client(timeout=30.0, follow_redirects=True)

probs = client.get(f"{CORE}/events/{EVENT_ID}/competitions/{EVENT_ID}"
                   f"/probabilities?limit=1000").json().get("items", [])
print(f"{len(probs)} probability entries")

# Items may be $refs; resolve a handful either way.
def resolve(item):
    if set(item.keys()) == {"$ref"}:
        return client.get(item["$ref"]).json()
    return item


sample = [resolve(p) for p in probs[:3] + probs[len(probs)//2:len(probs)//2+1]
          + probs[-2:]]

print("\nfull first entry:")
print(json.dumps(sample[0], indent=2)[:900])

print(f"\n{'seq':>6} {'secsLeft':>9} {'homeWin':>8} {'spreadHome':>11} "
      f"{'overProb':>9}  lastModified")
for p in sample:
    print(f"{str(p.get('sequenceNumber')):>6} {str(p.get('secondsLeft')):>9} "
          f"{str(p.get('homeWinPercentage')):>8} "
          f"{str(p.get('spreadCoverProbHome')):>11} "
          f"{str(p.get('totalOverProb')):>9}  {p.get('lastModified')}")

stamps = [p.get("lastModified") for p in sample if p.get("lastModified")]
print(f"\ndistinct lastModified in sample: {len(set(stamps))} of {len(stamps)}")
if len(set(stamps)) <= 1:
    print("  ALL IDENTICAL -> a bulk archive write, not a live timestamp.")
    print("  NFL win probability cannot be placed on a real clock this way.")

print("\n=== plays: do they carry a real timestamp? ===")
plays = client.get(f"{CORE}/events/{EVENT_ID}/competitions/{EVENT_ID}"
                   f"/plays?limit=1000").json().get("items", [])
resolved = [resolve(p) for p in plays[:2] + plays[-2:]]
print(json.dumps(resolved[0], indent=2)[:700])
for p in resolved:
    clock = (p.get("clock") or {}).get("displayValue")
    period = (p.get("period") or {}).get("number")
    print(f"  seq={p.get('sequenceNumber'):>6} Q{period} {clock:>6} "
          f"modified={p.get('modified')}  wallclock={p.get('wallclock')}")

print("\n=== the market's own window, for comparison ===")
ev = client.get(f"{CORE}/events/{EVENT_ID}").json()
print(f"  event date: {ev.get('date')}")
print("  Kalshi market for this game closed 2026-01-18 (see backfill).")
print("  A usable timestamp must fall inside the game, not months later.")

client.close()
