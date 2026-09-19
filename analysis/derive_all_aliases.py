"""Derive the Kalshi->ESPN code map for every league, from the data.

Hand-writing this has been wrong every single time it was tried today:
WAS/WSH and JAC/JAX were both inverted, and the football assumption
that a fixture happens once per three days broke baseball. So none of
it is guessed here.

THE METHOD. Kalshi's winner markets come in pairs -- one contract per
team on the same fixture -- so a ticker pair names both codes for a
game. ESPN lists the same game with its own two codes. Align them on
the date and on whichever code the two sources already agree about,
and the remaining pair must be the same team spelled differently.

Ambiguous cases are printed and NOT resolved. A wrong entry joins a
market to a different game and produces confident nonsense, so the
output is a proposal a human reads, not a patch.
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


def _shift(iso_date: str, days: int) -> str:
    return (dt.date.fromisoformat(iso_date)
            + dt.timedelta(days=days)).isoformat()

ROOT = sys.argv[1] if len(sys.argv) > 1 else r"D:\kalshi-data"

LEAGUE_SERIES = {
    "nfl": ["KXNFLGAME"],
    "mlb": ["KXMLBGAME"],
    "epl": ["KXEPLGAME"],
    "laliga": ["KXLALIGAGAME"],
    "seriea": ["KXSERIEAGAME"],
    "bundesliga": ["KXBUNDESLIGAGAME"],
    "ligue1": ["KXLIGUE1GAME"],
}


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


for league, series_list in LEAGUE_SERIES.items():
    # ESPN fixtures for this league, by date.
    espn = collections.defaultdict(list)
    espn_codes = set()
    for path in glob.glob(os.path.join(ROOT, "truth_backfill",
                                       f"{league}_boxscore", "*.jsonl.gz")):
        for row in read_jsonl(path):
            sides = {c.get("home_away"): c.get("abbrev")
                     for c in (row.get("competitors") or [])}
            if sides.get("away") and sides.get("home"):
                espn[(row.get("date") or "")[:10]].append(
                    (sides["away"], sides["home"]))
                espn_codes |= {sides["away"], sides["home"]}

    # Kalshi fixtures, taken from the ticker pairs on each event.
    kalshi_by_event = collections.defaultdict(set)
    for series in series_list:
        for path in glob.glob(os.path.join(ROOT, "backfill", series,
                                           "*.jsonl.gz")):
            for row in read_jsonl(path):
                pm = mj.parse_market(row["ticker"], row.get("yes_sub_title"))
                if not pm or pm["kind"] != "winner" or pm["is_draw"]:
                    continue
                kalshi_by_event[(pm["date"], pm["teams"],
                                 pm["game_no"])].add(pm["side"])

    kalshi_codes = set()
    for sides in kalshi_by_event.values():
        kalshi_codes |= sides

    unmapped = {c for c in kalshi_codes if mj.espn_code(c) not in espn_codes}
    print(f"\n{'=' * 64}\n{league.upper()}   "
          f"kalshi {len(kalshi_codes)} codes, espn {len(espn_codes)}")
    if not unmapped:
        print("  every Kalshi code already maps to an ESPN code")
        continue
    print(f"  unmapped: {sorted(unmapped)}")

    votes = collections.Counter()
    for (date, blob, _), sides in kalshi_by_event.items():
        if len(sides) != 2:
            continue
        a, b = sorted(sides)
        # The two sources date evening games a day apart (Kalshi local,
        # ESPN UTC), so look either side of it.
        nearby = []
        for delta in (0, 1, -1):
            nearby += espn.get(_shift(date, delta), [])
        for away, home in nearby:
            pair = {away, home}
            mapped = {mj.espn_code(a), mj.espn_code(b)}
            common = pair & mapped
            if len(common) != 1:
                continue
            # One side agrees; the leftovers name the same team.
            k_left = a if mj.espn_code(a) not in common else b
            e_left = (pair - common).pop()
            if mj.espn_code(k_left) != e_left:
                votes[(k_left, e_left)] += 1

    if not votes:
        print("  no unambiguous alignment found")
        continue
    for (k, e), n in votes.most_common(12):
        print(f'    "{k}": "{e}",   # seen {n}x')
