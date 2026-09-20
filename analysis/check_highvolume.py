"""Depth check on the new live-collection series, before deploying them.

The same check dropped KXEPLFIRSTGOAL, KXLALIGAFIRSTGOAL and
KXLALIGAGOAL earlier: all quoted, all with ZERO on the bid. A displayed
price with nothing behind it is not data, and it costs a request every
cycle forever.

SEASONALITY IS EXPECTED HERE and is not a reason to drop anything. It
is late September: college football is in season, basketball and hockey
start next month, and a series with no open markets today will have
hundreds in a few weeks. The settled history already proves these trade
-- KXNBAGAME at 4.0M per market. So zero open markets is recorded as
"out of season", not "worthless"; zero DEPTH on markets that ARE open is
the disqualifying result.
"""
from __future__ import annotations

from collector import HIGH_VOLUME
from kalshi_client import KalshiClient, market_price_cents

kalshi = KalshiClient()


def fp(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def med(xs):
    return sorted(xs)[len(xs) // 2] if xs else 0


print(f"{'series':<24} {'open':>6} {'quoted':>7} {'med spread':>11} "
      f"{'med bid sz':>11} {'tot bid sz':>12}")
print("-" * 76)

keep, out_of_season, thin = [], [], []
for series in HIGH_VOLUME:
    try:
        resp = kalshi.get_markets(series_ticker=series, status="open",
                                  limit=200)
    except Exception as exc:
        print(f"{series:<24} ERROR {type(exc).__name__}")
        continue
    markets = resp.get("markets", [])
    if not markets:
        print(f"{series:<24} {0:>6}   (no open markets -- out of season)")
        out_of_season.append(series)
        continue

    spreads, sizes, quoted = [], [], 0
    for m in markets:
        bid = market_price_cents(m, "yes_bid")
        ask = market_price_cents(m, "yes_ask")
        if bid is not None and ask is not None and ask > 0:
            quoted += 1
            spreads.append(ask - bid)
        sizes.append(fp(m.get("yes_bid_size_fp")))

    print(f"{series:<24} {len(markets):>6} {quoted:>7} {med(spreads):>10}c "
          f"{med(sizes):>11,.0f} {sum(sizes):>12,.0f}")
    if med(sizes) == 0 and med(spreads) > 20:
        thin.append(series)
    else:
        keep.append(series)

print(f"\nkeep            : {len(keep)}  {keep}")
print(f"out of season   : {len(out_of_season)}  {out_of_season}")
print(f"thin, drop      : {len(thin)}  {thin}")
