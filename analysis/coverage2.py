"""A clean denominator for "how much of Kalshi are we recording".

The first pass hit its page cap at 400,000 markets and reported 0.1%
coverage, which is true and useless: 97% of that count is one
combinatorial parlay series (KXMVECROSSCATEGORY), where the exchange
lists every cross-product of legs as its own market. Those are generated,
not traded, and a denominator dominated by them measures nothing.

So page to exhaustion and split the count: parlay versus everything else.
"""
import sqlite3
import time
from contextlib import closing

from config import SETTINGS
from kalshi_client import KalshiClient

kalshi = KalshiClient()

PARLAY_PREFIX = "KXMVECROSSCATEGORY"

by_series = {}
cursor, pages, seen = None, 0, 0
t0 = time.time()
while pages < 3000:
    params = {"status": "open", "limit": 1000}
    if cursor:
        params["cursor"] = cursor
    resp = kalshi._request("GET", "/markets", params=params)
    batch = resp.get("markets", [])
    for m in batch:
        s = m.get("event_ticker", "").split("-")[0] or "?"
        by_series[s] = by_series.get(s, 0) + 1
    seen += len(batch)
    cursor = resp.get("cursor")
    pages += 1
    if not cursor or not batch:
        break

exhausted = not cursor
parlay = sum(n for s, n in by_series.items() if s.startswith(PARLAY_PREFIX))
real = seen - parlay

print(f"pages fetched            : {pages} ({time.time()-t0:.0f}s), "
      f"cursor exhausted: {exhausted}")
print(f"open markets listed      : {seen:,}")
print(f"  parlay cross-products  : {parlay:,}   ({PARLAY_PREFIX}*)")
print(f"  everything else        : {real:,}")
print(f"distinct series          : {len(by_series):,}")

since = int(time.time()) - 3600
with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    polled = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM price_history WHERE ts > ?",
        (since,)).fetchone()[0]
    ticked = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM realtime_ticks "
        "WHERE received_ts > ?", (since,)).fetchone()[0]

union = max(polled, ticked)
print(f"\nrecorded in the last hour: {polled:,} polled, {ticked:,} via ticks")
print(f"coverage of non-parlay markets: {100.0*union/max(real,1):.1f}%")

print("\nWEATHER vs SPORTS, as listed right now:")
for label, pref in (("weather", ("KXHIGH", "KXRAIN", "KXSNOW", "KXTEMP",
                                 "KXMINTEMP", "KXCLI")),
                    ("collected sports/other", ("KXNFL", "KXEPL", "KXLALIGA",
                                                "KXSERIEA", "KXBUNDESLIGA",
                                                "KXLIGUE1", "KXMLBGAME",
                                                "KXUFCFIGHT", "KXRT",
                                                "KXTRUMPMENTION",
                                                "KXHORMUZ"))):
    n = sum(v for k, v in by_series.items() if k.startswith(pref))
    print(f"  {label:<24} {n:>7,} open markets")
