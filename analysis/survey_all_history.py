"""Where is the untapped settled history, and how far back does it go?

Two questions, both previously answered from a capped probe and worth
redoing properly.

1. HOW FAR BACK. The earlier probe stopped at 12 pages per family and
   reported an oldest close of May 2025. That may have been the cap
   talking rather than the archive. If Kalshi serves years rather than
   months, every "wait for data" conclusion changes again.

2. WHAT ELSE IS THERE. 30 series are being backfilled out of an
   exchange with far more. Rank every series by how much SETTLED,
   TRADED history it actually holds -- not by how interesting it
   sounds -- so the next expansion is chosen on evidence.

Volume matters as much as count: a family with 5,000 settled markets
that never traded is 5,000 rows of nothing, which is what weather
turned out to be.
"""
from __future__ import annotations

import collections
import sys
import time

from kalshi_client import KalshiClient

kalshi = KalshiClient()
MAX_PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 200


def fp(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


print("paging ALL settled markets (no series filter)...")
by_series = collections.defaultdict(
    lambda: {"n": 0, "traded": 0, "volume": 0.0,
             "oldest": "9999", "newest": "0000"})

cursor, pages, total = None, 0, 0
t0 = time.time()
while pages < MAX_PAGES:
    try:
        resp = kalshi.get_historical_markets(cursor=cursor, limit=200)
    except Exception as exc:
        print(f"  stopped at page {pages}: {type(exc).__name__}: {exc}")
        break
    batch = resp.get("markets", [])
    if not batch:
        break
    for m in batch:
        series = (m.get("event_ticker") or "").split("-")[0] or "?"
        s = by_series[series]
        s["n"] += 1
        vol = fp(m.get("volume_fp"))
        s["volume"] += vol
        if vol > 0:
            s["traded"] += 1
        close = (m.get("close_time") or "")[:10]
        if close:
            s["oldest"] = min(s["oldest"], close)
            s["newest"] = max(s["newest"], close)
    total += len(batch)
    cursor = resp.get("cursor")
    pages += 1
    if not cursor:
        break

print(f"  {total:,} settled markets across {len(by_series):,} series "
      f"in {pages} pages ({time.time()-t0:.0f}s)")

oldest = min((s["oldest"] for s in by_series.values()
              if s["oldest"] != "9999"), default="-")
print(f"  oldest close seen: {oldest}")

ALREADY = {
    "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXEPLGAME", "KXEPLSPREAD",
    "KXEPLTOTAL", "KXLALIGAGAME", "KXLALIGATOTAL", "KXSERIEAGAME",
    "KXSERIEATOTAL", "KXBUNDESLIGAGAME", "KXLIGUE1GAME", "KXMLBGAME",
    "KXUFCFIGHT", "KXRT", "KXTRUMPMENTION", "KXHORMUZWEEKLY",
    "KXMLBHIT", "KXMLBTB", "KXMLBHRR", "KXMLBRBI", "KXMLBSB",
    "KXMLBKS", "KXMLBHR", "KXWNBAPTS", "KXWNBAREB", "KXEPLGOAL",
}

rows = sorted(by_series.items(), key=lambda kv: -kv[1]["volume"])
print(f"\n{'series':<26} {'settled':>8} {'traded':>7} {'volume':>14} "
      f"{'oldest':>11}  new?")
print("-" * 76)
for series, s in rows[:40]:
    mark = "" if series in ALREADY else "  <-- NOT COLLECTED"
    print(f"{series:<26} {s['n']:>8,} {s['traded']:>7,} "
          f"{s['volume']:>14,.0f} {s['oldest']:>11}{mark}")

missing_vol = sum(s["volume"] for k, s in by_series.items()
                  if k not in ALREADY)
have_vol = sum(s["volume"] for k, s in by_series.items() if k in ALREADY)
print(f"\nvolume in series we collect    : {have_vol:>16,.0f}")
print(f"volume in series we do NOT     : {missing_vol:>16,.0f}")
