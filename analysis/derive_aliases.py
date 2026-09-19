"""Derive the Kalshi-to-ESPN team code map from the data.

I have now guessed this map twice and got it wrong twice -- the first
version had the WAS/WSH direction inverted and invented a JAX->JAC
mapping that does not exist. Guessing a third time is not a plan.

The fixtures themselves contain the answer. On any given date both
sources list the same games, so a Kalshi fixture that fails to match can
be aligned against the ESPN games on that date: if exactly one ESPN game
shares a team code with it, the remaining codes must correspond.

Proposals are printed for review, not written to the module
automatically. A wrong entry here joins a market to a different game and
produces confident nonsense, so a human reads this list.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json

import market_join as mj

KALSHI_GLOB = "data/backfill/KXNFLGAME/*.jsonl.gz"
TRUTH_GLOB = "data/truth_backfill/nfl_winprob/*.jsonl.gz"


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


espn_by_date = collections.defaultdict(list)
espn_codes = set()
for path in sorted(glob.glob(TRUTH_GLOB)):
    for row in read_jsonl(path):
        day = (row.get("date") or "")[:10]
        sides = {c.get("home_away"): c.get("abbrev")
                 for c in (row.get("competitors") or [])}
        if sides.get("away") and sides.get("home"):
            espn_by_date[day].append((sides["away"], sides["home"]))
            espn_codes |= {sides["away"], sides["home"]}

kalshi_codes = set()
unmatched = []
for path in sorted(glob.glob(KALSHI_GLOB)):
    for m in read_jsonl(path):
        f = mj.fixture_for(m.get("ticker", ""), espn_codes)
        if not f:
            continue
        kalshi_codes |= {f["away"], f["home"]}
        fixtures = espn_by_date.get(f["date"], [])
        if not fixtures:
            continue
        if (f["espn_away"], f["espn_home"]) in fixtures:
            continue
        unmatched.append((f, fixtures))

print(f"ESPN codes  ({len(espn_codes)}): {' '.join(sorted(espn_codes))}")
print(f"Kalshi codes({len(kalshi_codes)}): {' '.join(sorted(kalshi_codes))}")

only_kalshi = {c for c in kalshi_codes if mj.espn_code(c) not in espn_codes}
only_espn = espn_codes - {mj.espn_code(c) for c in kalshi_codes}
print(f"\nKalshi codes with no ESPN counterpart: {sorted(only_kalshi)}")
print(f"ESPN codes never produced by Kalshi   : {sorted(only_espn)}")

print(f"\n{len(unmatched)} unmatched fixtures. Deriving candidates...\n")

votes = collections.Counter()
for f, fixtures in unmatched:
    for away, home in fixtures:
        # Align on the side that already agrees; the other side names
        # the pair that must be the same team spelled differently.
        if f["espn_home"] == home and f["espn_away"] != away:
            votes[(f["away"], away)] += 1
        elif f["espn_away"] == away and f["espn_home"] != home:
            votes[(f["home"], home)] += 1

if not votes:
    print("  no unambiguous alignments found")
for (kalshi, espn), n in votes.most_common(20):
    flag = "" if kalshi != espn else "   (identical -- not an alias)"
    print(f'  "{kalshi}": "{espn}",   # seen {n}x{flag}')

print("\nPaste the reviewed lines into market_join.CODE_ALIASES.")
