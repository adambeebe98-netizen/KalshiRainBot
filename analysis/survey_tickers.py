"""Survey every ticker shape before writing a line of parser.

Guessing at Kalshi's encoding has cost two inverted alias maps and a
football assumption baked into baseball code today. So: print real
tickers per series with their titles and subtitles, enough of them to
see the structure rather than infer it from one example.

The title and yes_sub_title matter as much as the ticker. A ticker
segment like "NE3" is meaningless until the title says "New England
wins by over 3.5 points", which fixes both what NE is and what 3 means.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else r"D:\kalshi-data"
PER_SERIES = int(sys.argv[2]) if len(sys.argv) > 2 else 4


def read_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def shape(ticker: str) -> str:
    """Collapse a ticker to its structure: A=letters, 9=digits."""
    out = []
    for seg in ticker.split("-"):
        s = re.sub(r"[A-Z]+", "A", re.sub(r"\d+", "9", seg))
        out.append(s)
    return "-".join(out)


for series_dir in sorted(glob.glob(os.path.join(ROOT, "backfill", "*"))):
    series = os.path.basename(series_dir)
    if series.startswith("_"):
        continue
    files = sorted(glob.glob(os.path.join(series_dir, "*.jsonl.gz")))
    if not files:
        continue

    samples, shapes = [], collections.Counter()
    for path in files[: max(3, len(files) // 40)]:
        for row in read_jsonl(path):
            shapes[shape(row["ticker"])] += 1
            if len(samples) < PER_SERIES:
                samples.append(row)
        if len(samples) >= PER_SERIES and len(shapes) > 1:
            break

    print(f"\n{'=' * 74}")
    print(f"{series}")
    print(f"  shapes: {dict(shapes.most_common(4))}")
    for s in samples:
        print(f"  {s['ticker']}")
        print(f"      title : {(s.get('title') or '')[:64]}")
        print(f"      yes   : {(s.get('yes_sub_title') or '')[:64]}")
