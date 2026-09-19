"""Trace test.

554 of the 612 historical daily-rain markets are New York. Within those,
34 settled YES while Kalshi's own expiration_value read 0.0, against a
contract reading "strictly greater than 0 inches of precipitation".

Hypothesis: those are TRACE days -- rain that fell but never reached the
0.01" the gauge can record. If the CLI report says "T" on those dates and
a number on the positive-value YES dates and 0.00 on the NO dates, then
the boundary between "it rained" and "the contract paid" is a knowable
quantity, not noise.

Read-only. Hits IEM's public CLI archive, writes nothing.
"""
import json
import sqlite3
from collections import Counter
from datetime import date, timedelta

import httpx

DB = "/root/kalshi_weather_bot/bot_state.db"
IEM = "https://mesonet.agron.iastate.edu/json/cli.py"
UA = {"User-Agent": "kalshi-weather-bot research (adambeebe98@gmail.com)"}
STATION = "KNYC"  # Central Park, the NYC climate site

conn = sqlite3.connect(DB, timeout=15)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute(
    "SELECT ticker, date(close_time) d, result, expiration_value v "
    "FROM historical_markets "
    "WHERE measure='precipitation_daily' AND station_code='CLINYC' "
    "AND expiration_value IS NOT NULL ORDER BY d"
)]
conn.close()

years = sorted({r["d"][:4] for r in rows})
print(f"{len(rows)} NYC rain markets with a settlement value, years {years}")

cli = {}
for y in years:
    r = httpx.get(IEM, params={"station": STATION, "year": y}, headers=UA, timeout=60.0)
    print(f"  CLI {y}: HTTP {r.status_code}", end="")
    if r.status_code != 200:
        print()
        continue
    data = r.json().get("results", [])
    print(f", {len(data)} daily reports")
    if data and not cli:
        print("  fields:", sorted(data[0].keys()))
        print("  sample:", json.dumps(data[0])[:400])
    for rec in data:
        if rec.get("valid"):
            cli[rec["valid"][:10]] = rec

print(f"\nCLI days loaded: {len(cli)}")

PRECIP_KEYS = [k for k in (next(iter(cli.values())).keys() if cli else [])
               if "precip" in k.lower()]
print("precip-ish fields:", PRECIP_KEYS)


def bucket(r):
    if r["result"] == "yes" and r["v"] == 0.0:
        return "YES @ value 0.0  <-- the 34"
    if r["result"] == "yes":
        return "YES @ value > 0"
    return "NO  @ value 0.0"


print("\n=== CLI PRECIPITATION BY MARKET OUTCOME ===")
summary = {}
misses = Counter()
for r in rows:
    # The market closes the morning after the observation day, so the CLI
    # report we want is normally dated the day before the close date.
    y, m, dd = (int(x) for x in r["d"].split("-"))
    prev = (date(y, m, dd) - timedelta(days=1)).isoformat()
    rec = cli.get(prev) or cli.get(r["d"])
    b = bucket(r)
    summary.setdefault(b, [])
    if rec is None:
        misses[b] += 1
        continue
    vals = {k: rec.get(k) for k in PRECIP_KEYS}
    summary[b].append(vals)

for b in sorted(summary):
    entries = summary[b]
    print(f"\n{b}   (n={len(entries)}, unmatched={misses[b]})")
    for k in PRECIP_KEYS:
        seen = Counter(repr(e.get(k)) for e in entries)
        top = ", ".join(f"{v}x {kk}" for kk, v in seen.most_common(6))
        print(f"   {k}: {top}")
