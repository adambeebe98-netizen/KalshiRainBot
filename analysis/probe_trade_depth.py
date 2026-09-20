"""How far back does the trade log go, and how big is it?

/markets/trades returns the real transaction record -- price, size,
microsecond timestamp, and which side took -- and it answered for a
market that settled in July. Two things decide whether the whole
archive is recoverable:

  1. does it serve markets from a YEAR ago, or only recent ones
  2. how many requests and how much disk a full backfill would cost

Trades are strictly richer than candles. A candle says the hour closed
at 20c. The trade log says 300 contracts went through at 19c at
14:32:07 and then nothing for forty minutes -- which is the difference
between "the price was 20c" and "I could have been filled at 20c", and
that distinction has killed every candidate so far.
"""
from __future__ import annotations

import time

from kalshi_client import KalshiClient

kalshi = KalshiClient()


def fp(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def count_trades(ticker: str, max_pages: int = 40):
    """Page the trade log; return (n_trades, contracts, first, last)."""
    cursor, n, contracts, times, pages = None, 0, 0.0, [], 0
    while pages < max_pages:
        params = {"ticker": ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        try:
            r = kalshi._request("GET", "/markets/trades", params=params)
        except Exception as exc:
            return n, contracts, times, f"ERR {str(exc)[:40]}"
        batch = r.get("trades") or []
        n += len(batch)
        # The size field is count_fp, a fixed-point STRING. Reading a
        # plain "count" gives zero for every trade, which is exactly
        # what the first probe reported.
        contracts += sum(fp(t.get("count_fp")) for t in batch)
        times += [t.get("created_time", "") for t in batch]
        cursor = r.get("cursor")
        pages += 1
        if not cursor or not batch:
            break
    return n, contracts, times, ("capped" if cursor else "complete")


print("Does the trade log reach back through the archive?\n")
print(f"{'series':<16} {'ticker':<26} {'closed':<12} {'trades':>8} "
      f"{'contracts':>12} {'status':<9}")
print("-" * 92)

SERIES = ["KXRAINNYC", "KXHIGHNY", "KXNFLGAME", "KXMLBGAME"]
for series in SERIES:
    try:
        hist = kalshi.get_historical_markets(series_ticker=series, limit=200)
    except Exception as exc:
        print(f"{series:<16} listing failed: {exc}")
        continue
    markets = [m for m in hist.get("markets", [])
               if m.get("result") in ("yes", "no")
               and fp(m.get("volume_fp")) > 0]
    if not markets:
        print(f"{series:<16} no settled traded markets in the first page")
        continue
    markets.sort(key=lambda m: m.get("close_time") or "")
    for m in (markets[0], markets[len(markets) // 2], markets[-1]):
        t0 = time.time()
        n, contracts, times, status = count_trades(m["ticker"], max_pages=6)
        span = ""
        if times:
            ts = sorted(t for t in times if t)
            span = f"{ts[0][:10]}"
        print(f"{series:<16} {m['ticker'][:26]:<26} "
              f"{(m.get('close_time') or '')[:10]:<12} {n:>8,} "
              f"{contracts:>12,.0f} {status:<9} {time.time()-t0:.1f}s")

print("\n  'capped' means the 6-page probe limit was hit, not the end of")
print("  the data -- those markets have more trades than shown.")
