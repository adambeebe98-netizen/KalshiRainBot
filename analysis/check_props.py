"""Confirm the prop series exist and are quoted before collecting them.

Adding a series that returns nothing costs a request per cycle forever
and produces no data, so check rather than assume. Depth matters too:
these were chosen for their settlement structure, not their liquidity,
and it is worth knowing how thin "thin" actually is.
"""
from collector import AMBIGUOUS_PROPS, record_rules
from kalshi_client import KalshiClient, market_price_cents

kalshi = KalshiClient()

print(f"{'series':<20} {'open':>6} {'quoted':>7} {'med spread':>11} "
      f"{'med bid size':>13}")
print("-" * 62)

for series in AMBIGUOUS_PROPS:
    try:
        resp = kalshi.get_markets(series_ticker=series, status="open",
                                  limit=200)
    except Exception as exc:
        print(f"{series:<20} ERROR {type(exc).__name__}: {str(exc)[:30]}")
        continue
    markets = resp.get("markets", [])
    spreads, sizes = [], []
    quoted = 0
    for m in markets:
        bid = market_price_cents(m, "yes_bid")
        ask = market_price_cents(m, "yes_ask")
        if bid is not None and ask is not None and ask > 0:
            quoted += 1
            spreads.append(ask - bid)
        try:
            sizes.append(float(m.get("yes_bid_size_fp") or 0))
        except (TypeError, ValueError):
            pass

    def med(xs):
        return sorted(xs)[len(xs) // 2] if xs else 0

    print(f"{series:<20} {len(markets):>6} {quoted:>7} "
          f"{med(spreads):>10}c {med(sizes):>13,.0f}")

# The rules writer is new; prove it produces a file before the service
# restarts on top of it.
print("\nrules capture smoke test:")
resp = kalshi.get_markets(series_ticker="KXMLBHIT", status="open", limit=3)
for m in resp.get("markets", [])[:2]:
    wrote = record_rules(m, "KXMLBHIT", directory="data/rules_smoke")
    print(f"  {m.get('ticker')}: wrote={wrote}")
    again = record_rules(m, "KXMLBHIT", directory="data/rules_smoke")
    print(f"    second call (should be False): {again}")
