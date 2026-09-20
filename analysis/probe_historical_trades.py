"""Does /historical/trades serve the OLD markets /markets/trades cannot?

I concluded earlier that the trade log was a rolling window and that
everything before roughly 20 July 2026 was gone for good -- and put it
at the top of the audit as the one gap with a deadline.

That conclusion came from probing /markets/trades only, because that is
the endpoint I knew about. A direct probe of the API surface then turned
up /historical/trades, which is exactly the pattern the markets and
candlesticks endpoints already follow: a live one and a historical one,
split at the same ~3-month cutoff.

If it works, the finding inverts. The trade log would not be expiring;
it would be a large recoverable archive we simply never fetched --
which is a far better problem, and a direct answer to whether the
original scrape left value on the table.

Tested against markets that returned ZERO trades from the live
endpoint.
"""
from __future__ import annotations

import json

from kalshi_client import KalshiClient

kalshi = KalshiClient()


def fp(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def ask(path: str, **params):
    try:
        return kalshi._request("GET", path, params=params or None)
    except Exception as exc:
        return {"__error__": str(exc)[:90]}


# Markets confirmed EMPTY on /markets/trades in the earlier probe.
OLD = [
    ("KXRAINNYC-26APR07-T0", "2026-04-08"),
    ("KXRAINNYC-25DEC28-T0", "2025-12-29"),
    ("KXNFLGAME-26JAN18LACHI-CHI", "2026-01-19"),
    ("KXHIGHNY-26JUN17-T81", "2026-06-18"),
]

print(f"{'ticker':<30} {'closed':<12} {'live':>7} {'historical':>11}  span")
print("-" * 86)

for ticker, closed in OLD:
    live = ask("/markets/trades", ticker=ticker, limit=200)
    hist = ask("/historical/trades", ticker=ticker, limit=200)

    n_live = len(live.get("trades") or []) if "__error__" not in live else -1
    trades = hist.get("trades") or [] if "__error__" not in hist else []
    n_hist = len(trades) if "__error__" not in hist else -1

    span = ""
    if trades:
        times = sorted(t.get("created_time", "") for t in trades)
        contracts = sum(fp(t.get("count_fp")) for t in trades)
        span = (f"{times[0][:16]} .. {times[-1][:16]}  "
                f"{contracts:,.0f} contracts")
    elif "__error__" in hist:
        span = hist["__error__"]

    print(f"{ticker[:30]:<30} {closed:<12} {n_live:>7} {n_hist:>11}  {span}")

print("\n=== fields, and how deep it pages ===")
h = ask("/historical/trades", ticker=OLD[0][0], limit=5)
trades = h.get("trades") or []
if trades:
    print(json.dumps(trades[0], indent=2))
    cursor, total, pages = h.get("cursor"), len(trades), 1
    while cursor and pages < 30:
        h = ask("/historical/trades", ticker=OLD[0][0], limit=1000,
                cursor=cursor)
        batch = h.get("trades") or []
        total += len(batch)
        cursor = h.get("cursor")
        pages += 1
        if not batch:
            break
    print(f"\n  {OLD[0][0]}: {total:,} trades over {pages} pages"
          f"{' (capped)' if cursor else ' (complete)'}")
else:
    print("  no trades returned; endpoint may need different parameters")
    print(f"  raw response keys: {list(h)}")
