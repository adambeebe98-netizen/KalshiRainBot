"""Where exactly does the trade log stop, and what is still inside it?

Trades exist for markets that closed around 20 July 2026 and are gone
for anything earlier. That looks like a ROLLING WINDOW, which makes
this the one gap in the audit with a deadline: whatever is still
retrievable is leaving on a schedule, and nothing about the stored data
will ever reveal that it was once available.

Find the boundary by walking recent closes and asking each for trades,
then state what is still inside it.
"""
from __future__ import annotations

import collections
import datetime as dt

from kalshi_client import KalshiClient

kalshi = KalshiClient()


def fp(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def has_trades(ticker: str) -> tuple[int, float]:
    try:
        r = kalshi._request("GET", "/markets/trades",
                            params={"ticker": ticker, "limit": 100})
    except Exception:
        return -1, 0.0
    batch = r.get("trades") or []
    return len(batch), sum(fp(t.get("count_fp")) for t in batch)


# Walk settled markets newest-first and probe one per close date.
print("probing one traded market per close date, newest first\n")
seen_dates: dict[str, tuple] = {}
cursor = None
pages = 0
while pages < 25 and len(seen_dates) < 45:
    resp = kalshi.get_historical_markets(cursor=cursor, limit=200)
    batch = resp.get("markets", [])
    if not batch:
        break
    for m in batch:
        if m.get("result") not in ("yes", "no"):
            continue
        if fp(m.get("volume_fp")) < 500:
            continue
        day = (m.get("close_time") or "")[:10]
        if not day or day in seen_dates:
            continue
        n, contracts = has_trades(m["ticker"])
        seen_dates[day] = (m["ticker"], n, contracts)
    cursor = resp.get("cursor")
    pages += 1
    if not cursor:
        break

print(f"{'close date':<12} {'trades':>8} {'contracts':>12}  ticker")
print("-" * 72)
with_trades = []
for day in sorted(seen_dates, reverse=True):
    ticker, n, contracts = seen_dates[day]
    mark = "" if n > 0 else "   <-- EMPTY"
    if n > 0:
        with_trades.append(day)
    print(f"{day:<12} {n:>8,} {contracts:>12,.0f}  {ticker[:34]}{mark}")

if with_trades:
    oldest = min(with_trades)
    today = dt.date.today()
    age = (today - dt.date.fromisoformat(oldest)).days
    print(f"\n  oldest close date still carrying trades: {oldest} "
          f"({age} days ago)")
    print(f"  today: {today}")
    print("\n  Everything older than that is unrecoverable. Everything")
    print("  inside the window is leaving on the same schedule, so it is")
    print("  worth capturing now rather than after the next question")
    print("  makes it interesting.")
