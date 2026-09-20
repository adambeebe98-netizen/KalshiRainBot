"""Are there TWO archives, with trades in only one of them?

The historical endpoint's newest settled market closed 2026-07-21, and
today is 2026-09-20. That two-month gap is not missing data -- it is
Kalshi's live/historical cutoff, which the client's own docstring
describes as a rolling window. Markets settled since then are still on
the LIVE endpoint.

Which matters enormously for trades: the probe found trades present for
2026-07-21 and absent for everything older. If that boundary is the
same cutoff, then trades exist for the LIVE side and not the historical
side -- and the live side is two months of markets whose trade logs are
retrievable right now and will roll out of reach.

Check /markets?status=settled directly.
"""
from __future__ import annotations

import collections

from kalshi_client import KalshiClient

kalshi = KalshiClient()


def fp(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


print("=== /markets?status=settled  (the LIVE side) ===")
by_day = collections.Counter()
sample: dict[str, str] = {}
cursor, pages, total = None, 0, 0
while pages < 15:
    params = {"status": "settled", "limit": 1000}
    if cursor:
        params["cursor"] = cursor
    resp = kalshi._request("GET", "/markets", params=params)
    batch = resp.get("markets", [])
    if not batch:
        break
    for m in batch:
        day = (m.get("close_time") or "")[:10]
        if not day:
            continue
        by_day[day] += 1
        if fp(m.get("volume_fp")) > 1000 and day not in sample:
            sample[day] = m["ticker"]
    total += len(batch)
    cursor = resp.get("cursor")
    pages += 1
    if not cursor:
        break

print(f"  {total:,} settled markets listed across {len(by_day)} close dates")
if by_day:
    days = sorted(by_day)
    print(f"  range: {days[0]} .. {days[-1]}")

print("\n=== do these carry trades? ===")
print(f"{'close date':<12} {'markets':>8} {'trades':>8} {'contracts':>12}"
      f"  ticker")
print("-" * 76)
checked = 0
for day in sorted(sample, reverse=True):
    if checked >= 12:
        break
    ticker = sample[day]
    try:
        r = kalshi._request("GET", "/markets/trades",
                            params={"ticker": ticker, "limit": 500})
        batch = r.get("trades") or []
        n = len(batch)
        contracts = sum(fp(t.get("count_fp")) for t in batch)
    except Exception as exc:
        n, contracts = -1, 0.0
    mark = "" if n > 0 else "   <-- EMPTY"
    print(f"{day:<12} {by_day[day]:>8,} {n:>8,} {contracts:>12,.0f}"
          f"  {ticker[:30]}{mark}")
    checked += 1

print("\n  If the recent dates carry trades, the live side is a second")
print("  archive we have never scraped -- and it rolls.")
