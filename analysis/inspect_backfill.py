"""Show what a backfilled record actually contains.

Specifically the settlement rules on a player prop. The premise for
collecting these is that they settle on an official scorer's ruling
rather than on an observable fact, and that premise is worth reading
off the contract rather than assuming.
"""
import glob
import gzip
import json
import sys

pattern = sys.argv[1] if len(sys.argv) > 1 else "data/backfill_trial/*/*.jsonl.gz"

for path in sorted(glob.glob(pattern)):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        line = fh.readline()
    if not line.strip():
        continue
    d = json.loads(line)
    print("=" * 72)
    print(f"{d['ticker']}   result={d['result']}   "
          f"volume={d['volume']:,.0f}")
    print(f"TITLE : {d.get('title')}")
    print(f"YES   : {d.get('yes_sub_title')}")
    print(f"candles: {len(d['candles_hourly'])} hourly, "
          f"{len(d['candles_minute'])} minute")
    rules = (d.get("rules_primary") or "").strip()
    print(f"RULES : {rules[:600] or '(none)'}")
    sec = (d.get("rules_secondary") or "").strip()
    if sec:
        print(f"ALSO  : {sec[:300]}")
