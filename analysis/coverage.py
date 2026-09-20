"""How much of Kalshi are we actually recording?

"Everything" is an easy thing to believe about a collector and an
expensive thing to be wrong about, so count it rather than assume it.
"""
import sqlite3
import time
from contextlib import closing

from config import SETTINGS
from kalshi_client import KalshiClient

kalshi = KalshiClient()

# Every open market on the exchange, by series.
by_series = {}
cursor = None
pages = 0
while pages < 400:
    params = {"status": "open", "limit": 1000}
    if cursor:
        params["cursor"] = cursor
    resp = kalshi._request("GET", "/markets", params=params)
    batch = resp.get("markets", [])
    for m in batch:
        s = m.get("event_ticker", "").split("-")[0] or "?"
        by_series[s] = by_series.get(s, 0) + 1
    cursor = resp.get("cursor")
    pages += 1
    if not cursor or not batch:
        break

total_markets = sum(by_series.values())
print(f"open markets on Kalshi right now : {total_markets:,}")
print(f"distinct series among them       : {len(by_series):,}")

# What we touched in the last hour.
since = int(time.time()) - 3600
with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT COUNT(DISTINCT ticker) n FROM price_history WHERE ts > ?",
        (since,)).fetchone()
    polled = rows["n"]
    tick_rows = conn.execute(
        "SELECT COUNT(DISTINCT ticker) n FROM realtime_ticks "
        "WHERE received_ts > ?", (since,)).fetchone()["n"]

print(f"\ndistinct tickers we POLLED in the last hour    : {polled:,}")
print(f"distinct tickers we saw TICKS from, last hour  : {tick_rows:,}")
print(f"\ncoverage of open markets: {100.0 * polled / max(total_markets,1):.1f}%")

print("\nlargest series we are NOT collecting:")
COLLECTED = {
    "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXEPLGAME", "KXEPLSPREAD",
    "KXEPLTOTAL", "KXLALIGAGAME", "KXLALIGATOTAL", "KXSERIEAGAME",
    "KXSERIEATOTAL", "KXBUNDESLIGAGAME", "KXLIGUE1GAME", "KXMLBGAME",
    "KXUFCFIGHT", "KXRT", "KXTRUMPMENTION", "KXHORMUZWEEKLY",
}
missing = [(n, s) for s, n in by_series.items()
           if s not in COLLECTED and not s.startswith(("KXHIGH", "KXRAIN",
                                                       "KXSNOW", "KXTEMP"))]
for n, s in sorted(missing, reverse=True)[:20]:
    print(f"  {s:<24} {n:>6,} open markets")
