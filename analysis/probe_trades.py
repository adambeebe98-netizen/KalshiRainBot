"""Does Kalshi serve individual executed TRADES, and for how far back?

The audit flagged /trades as having no client wrapper at all. If it
returns the real transaction record -- price, size, side, timestamp per
fill -- then every candle-based analysis done so far has been working
from a summary of data we could have had exactly.

That matters specifically for the question at hand. A candle says the
hour closed at 20c; a trade log says somebody bought 300 contracts at
19c at 14:32:07 and nothing traded for the next forty minutes. The
second one answers "could I have actually got filled", which is what
killed every candidate so far.

Checks: does the endpoint exist, what fields come back, does it work
for a SETTLED market from a year ago, and how deep does pagination go.
"""
from __future__ import annotations

import datetime as dt
import json

from kalshi_client import KalshiClient

kalshi = KalshiClient()


def try_call(label, path, **params):
    try:
        resp = kalshi._request("GET", path, params=params or None)
    except Exception as exc:
        print(f"  {label:<34} FAILED: {str(exc)[:70]}")
        return None
    keys = list(resp) if isinstance(resp, dict) else type(resp).__name__
    print(f"  {label:<34} OK   keys={keys}")
    return resp


print("=== does /markets/trades exist? ===")
resp = try_call("all recent trades", "/markets/trades", limit=5)
if resp:
    trades = resp.get("trades") or []
    print(f"    {len(trades)} trades returned")
    if trades:
        print("\n    one trade, verbatim:")
        print("   ", json.dumps(trades[0], indent=4)[:600])

print("\n=== trades for a SETTLED market from the archive ===")
hist = kalshi.get_historical_markets(series_ticker="KXRAINNYC", limit=5)
settled = [m for m in hist.get("markets", [])
           if m.get("result") in ("yes", "no")]
if settled:
    m = settled[0]
    print(f"  {m['ticker']}  closed {m.get('close_time', '')[:19]}  "
          f"volume {float(m.get('volume_fp') or 0):,.0f}")
    r = try_call("trades for that ticker", "/markets/trades",
                 ticker=m["ticker"], limit=100)
    if r:
        tr = r.get("trades") or []
        print(f"    {len(tr)} trades, cursor="
              f"{str(r.get('cursor'))[:20]!r}")
        if tr:
            times = sorted(t.get("created_time", "") for t in tr)
            print(f"    time span {times[0][:19]} .. {times[-1][:19]}")
            print(f"    fields: {sorted(tr[0].keys())}")
            total = sum(int(t.get("count") or 0) for t in tr)
            print(f"    contracts in this page: {total:,}")

print("\n=== /events, for bracket grouping ===")
try_call("events", "/events", limit=2)

print("\n=== orderbook depth on a live market ===")
live = kalshi.get_markets(series_ticker="KXNFLGAME", status="open", limit=1)
mk = (live.get("markets") or [None])[0]
if mk:
    print(f"  {mk['ticker']}")
    ob = try_call("orderbook depth=10", f"/markets/{mk['ticker']}/orderbook",
                  depth=10)
    if ob:
        book = ob.get("orderbook") or ob.get("orderbook_fp") or {}
        for side in ("yes", "no", "yes_dollars", "no_dollars"):
            if side in book:
                levels = book[side] or []
                print(f"    {side}: {len(levels)} levels  "
                      f"{levels[:3]}")
