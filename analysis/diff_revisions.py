"""WHICH field is actually changing on a 'revision'?

778 plays were written twice, and the twelve sampled showed almost no
difference in the fields worth caring about. So the digest is firing on
something else -- and if that something is cosmetic, then "revision" as
a signal is noise, and I was one report away from presenting it as the
thesis working.

The digest covers seq, type, text, score_value, away_score, home_score,
period, clock, scoring_play, wallclock and athletes. Count which of
those actually differ between the first and last version of every
revised play, rather than eyeballing a sample.

A changed TYPE or SCORE is the official record being corrected -- the
event this service exists for. A changed WALLCLOCK is ESPN adjusting a
timestamp and means nothing.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "data/truth"

FIELDS = ("seq", "type", "text", "score_value", "away_score", "home_score",
          "period", "clock", "scoring_play", "wallclock", "athletes")


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


versions = collections.defaultdict(list)
for path in sorted(glob.glob(os.path.join(ROOT, "*_play", "*.jsonl.gz"))):
    for row in read_jsonl(path):
        versions[(row.get("league"), row.get("event_id"),
                  row.get("play_id"))].append(row)

revised = {k: v for k, v in versions.items() if len(v) > 1}
print(f"{len(revised):,} plays written more than once\n")

changed = collections.Counter()
only_field = collections.Counter()
examples = collections.defaultdict(list)

for key, rows in revised.items():
    first, last = rows[0], rows[-1]
    diffs = [f for f in FIELDS if first.get(f) != last.get(f)]
    for f in diffs:
        changed[f] += 1
        if len(examples[f]) < 3:
            examples[f].append((key, first.get(f), last.get(f)))
    if not diffs:
        only_field["(nothing in the digest fields)"] += 1
    elif len(diffs) == 1:
        only_field[f"only {diffs[0]}"] += 1
    else:
        only_field[f"{len(diffs)} fields: {','.join(sorted(diffs))}"] += 1

print("fields that differ, across ALL revised plays:")
for field, n in changed.most_common():
    print(f"  {field:<14} {n:>6,}")

print("\nwhat the revision consisted of:")
for combo, n in only_field.most_common(10):
    print(f"  {combo:<46} {n:>6,}")

print("\nexamples:")
for field in ("type", "text", "away_score", "home_score", "wallclock"):
    for key, a, b in examples.get(field, [])[:2]:
        print(f"  [{field}] {key[0]} {key[2]}")
        print(f"      {str(a)[:60]!r}  ->  {str(b)[:60]!r}")

meaningful = sum(changed[f] for f in ("type", "away_score", "home_score",
                                      "score_value", "scoring_play"))
print(f"\nrevisions touching the RECORD (type/score/scoring_play): "
      f"{meaningful:,}")
print(f"revisions touching only wallclock: "
      f"{only_field.get('only wallclock', 0):,}")
