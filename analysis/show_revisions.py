"""Show the revisions the live poller has caught.

This is the event the whole sports_truth service was built around. A
game line settles on who won and nobody misreads that. A player prop
settles on an OFFICIAL SCORER'S RULING -- hit or error is a judgment
call, and it can be changed after the fact. That gap, between what
happened and what the record SAYS happened, is the same gap that made a
correct read on rain in Austin lose $200.

A revision row means we stored a play, then saw the same play id come
back with different content. Print the pairs so they can be judged
rather than counted: some will be ESPN tidying up wording, and some
will be the scorer changing a call.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "data/truth"


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


# Every version of every play, keyed by id, in write order.
versions = collections.defaultdict(list)
revised_ids = set()
total = 0

for path in sorted(glob.glob(os.path.join(ROOT, "*_play", "*.jsonl.gz"))):
    for row in read_jsonl(path):
        total += 1
        key = (row.get("league"), row.get("event_id"), row.get("play_id"))
        versions[key].append(row)
        if row.get("revision"):
            revised_ids.add(key)

print(f"{total:,} play rows stored, {len(versions):,} distinct plays")
print(f"{len(revised_ids):,} plays were written more than once "
      f"(a REVISION -- the record changed after we first saw it)\n")

if not revised_ids:
    print("none yet")
    raise SystemExit(0)

by_league = collections.Counter(k[0] for k in revised_ids)
print("by league:", dict(by_league))

changed_text = 0
changed_score = 0
changed_type = 0

print(f"\n{'=' * 72}")
for key in list(revised_ids)[:12]:
    rows = versions[key]
    if len(rows) < 2:
        continue
    first, last = rows[0], rows[-1]
    league, event_id, play_id = key
    print(f"\n{league} event {event_id} play {play_id}  "
          f"({len(rows)} versions)")
    for field in ("type", "text", "away_score", "home_score",
                  "scoring_play", "period", "clock"):
        a, b = first.get(field), last.get(field)
        if a != b:
            if field == "text":
                changed_text += 1
            if field in ("away_score", "home_score"):
                changed_score += 1
            if field == "type":
                changed_type += 1
            print(f"    {field:<13} {str(a)[:44]!r}")
            print(f"    {'':<13} -> {str(b)[:44]!r}")

print(f"\n{'=' * 72}")
print(f"fields that changed across all revisions shown: "
      f"type={changed_type} text={changed_text} score={changed_score}")
print("\nA changed TYPE or SCORE is the official record being corrected.")
print("A changed TEXT alone is usually ESPN rewording the same play.")
