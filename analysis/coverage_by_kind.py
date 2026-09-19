"""Which ESPN artefact should index the fixtures?

join_check indexes on the win-probability file, so any game ESPN has no
curve for is invisible to the join even when its boxscore exists. That
would be a silent, systematic hole -- and preseason is exactly where
curves are likeliest to be missing, which matches the 112 unmatched
fixtures all falling in August.

Boxscore is written for every completed game, so if it covers strictly
more fixtures than winprob, it is the right index and winprob becomes an
optional attachment.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json

KINDS = ("boxscore", "winprob", "odds", "plays")


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


fixtures_by_kind = {}
for kind in KINDS:
    seen = set()
    rows = 0
    for path in sorted(glob.glob(f"data/truth_backfill/nfl_{kind}/*.jsonl.gz")):
        for row in read_jsonl(path):
            rows += 1
            seen.add((row.get("date", "")[:10], row.get("event_id")))
    fixtures_by_kind[kind] = seen
    print(f"nfl_{kind:<9} {rows:>6,} rows   {len(seen):>5,} distinct games")

box, wp = fixtures_by_kind["boxscore"], fixtures_by_kind["winprob"]
print(f"\ngames with a boxscore but NO win probability: {len(box - wp):,}")
print(f"games with win probability but no boxscore  : {len(wp - box):,}")

missing = sorted(box - wp)
if missing:
    by_month = collections.Counter(d[:7] for d, _ in missing)
    print("\nwhen are the curve-less games?")
    for month, n in sorted(by_month.items()):
        print(f"  {month}  {n:>4}")
    print("\nIf these cluster in August, the hole is preseason -- and")
    print("indexing on winprob would drop every preseason fixture.")
