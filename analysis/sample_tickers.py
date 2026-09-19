"""What shape are the tickers, per series?

market_join's regex was written against NFL, where the fixture segment
is letters only: KXNFLGAME-26JAN18HOUNE-NE. A prop ticker seen earlier
was KXMLBHIT-26JUL191920LADNYYG2-NYYTGRISHAM12-3, which has a time, a
game number and a player in it. So the NFL regex almost certainly does
not parse baseball, and assuming it does would just produce zero rows
and look like missing data.

Print real examples per series before writing any more parsing.
"""
from __future__ import annotations

import glob
import gzip
import json
import os

import market_join as mj

for series_dir in sorted(glob.glob("data/backfill/*")):
    series = os.path.basename(series_dir)
    if series.startswith("_"):
        continue
    files = sorted(glob.glob(os.path.join(series_dir, "*.jsonl.gz")))
    if not files:
        continue
    samples = []
    for path in files[:1]:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    samples.append(json.loads(line))
                if len(samples) >= 3:
                    break
    if not samples:
        continue
    parsed = sum(1 for s in samples if mj.parse_ticker(s["ticker"]))
    print(f"\n{series}   (regex parses {parsed}/{len(samples)} sampled)")
    for s in samples[:2]:
        print(f"  {s['ticker']}")
        print(f"      title: {(s.get('title') or '')[:70]}")
