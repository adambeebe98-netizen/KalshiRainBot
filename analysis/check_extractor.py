"""Is the LLM rules extractor still running, and what is it costing?

It parses free-form contract terms into structured fields -- station,
measure, threshold, settlement source -- using the Anthropic API, with
a local cache so the same market is never paid for twice.

Three things worth knowing before deciding whether to change it:

  1. is it still being called at all, or has it gone quiet
  2. how big is the cache, and what is the hit rate -- a cache that
     works means the cost is one-off per market, not per cycle
  3. WHICH markets go through it. collector.py deliberately skips it
     for the 42 non-weather series, so if it only runs on weather, its
     footprint is small and shrinking
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing

from config import SETTINGS

print("=== the cache ===")
for name in ("rules_cache.json", "data/rules_cache.json"):
    if os.path.exists(name):
        size = os.path.getsize(name)
        try:
            with open(name, encoding="utf-8") as fh:
                cache = json.load(fh)
            print(f"  {name}: {size/1024:,.0f} KB, {len(cache):,} markets "
                  f"cached")
            sample = list(cache.items())[:1]
            if sample:
                k, v = sample[0]
                print(f"    example key: {k}")
                print(f"    example value: {json.dumps(v)[:200]}")
        except Exception as exc:
            print(f"  {name}: {size:,}b, unreadable ({exc})")
    else:
        print(f"  {name}: absent")

print("\n=== is it called from the running services? ===")
for f in ("collector.py", "bot.py"):
    if not os.path.exists(f):
        continue
    src = open(f, encoding="utf-8").read()
    calls = len(re.findall(r"extractor\.extract\(", src))
    print(f"  {f}: {calls} call site(s)")
    for m in re.finditer(r"^(.*extractor\.extract\(.*)$", src, re.M):
        print(f"      {m.group(1).strip()[:78]}")

print("\n=== recent Anthropic calls in the collector log ===")
log = "collector.log"
if os.path.exists(log):
    out = subprocess.run(
        ["grep", "-c", "api.anthropic.com", log],
        capture_output=True, text=True).stdout.strip()
    print(f"  total anthropic requests logged: {out}")
    tail = subprocess.run(
        ["tail", "-4000", log], capture_output=True, text=True).stdout
    recent = len(re.findall(r"api\.anthropic\.com", tail))
    print(f"  in the last 4,000 log lines: {recent}")
    for m in list(re.finditer(r"^(.*anthropic.*)$", tail, re.M))[-3:]:
        print(f"      {m.group(1)[:100]}")
else:
    print("  no collector.log")

print("\n=== how many markets would ever need extraction? ===")
with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    for label, sql in (
            ("distinct weather tickers seen, last 7d",
             "SELECT COUNT(DISTINCT ticker) FROM market_snapshots "
             "WHERE ts > ? AND station_code IS NOT NULL"),
            ("distinct NON-weather tickers, last 7d",
             "SELECT COUNT(DISTINCT ticker) FROM market_snapshots "
             "WHERE ts > ? AND station_code IS NULL")):
        try:
            n = conn.execute(sql, (int(time.time()) - 7 * 86400,)).fetchone()[0]
            print(f"  {label:<40} {n:>8,}")
        except sqlite3.Error as exc:
            print(f"  {label}: {exc}")
