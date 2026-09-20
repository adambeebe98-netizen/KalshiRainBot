"""Is there settled history for the new series, and does it carry prices?

"Wait for data to accumulate" is only the right answer if the data does
not already exist. The 59,133-market weather backfill proves Kalshi
serves settled history with candlesticks; the question is whether the
same is true for the families now being collected.

Two things have to hold. Settled markets have to be listable, and they
have to carry price history -- a settled market with an outcome and no
candles gives a label with no features, which trains nothing.
"""
import collections

from kalshi_client import KalshiClient

kalshi = KalshiClient()

FAMILIES = [
    "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL",
    "KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME", "KXMLBGAME",
    "KXUFCFIGHT", "KXRT", "KXTRUMPMENTION",
]

print(f"{'series':<18} {'settled':>9} {'with result':>12} {'oldest':>12} "
      f"{'newest':>12}")
print("-" * 68)

summary = {}
for series in FAMILIES:
    markets, cursor = [], None
    try:
        for _ in range(12):     # up to ~2,400 per family for the probe
            resp = kalshi.get_historical_markets(series_ticker=series,
                                                 cursor=cursor, limit=200)
            batch = resp.get("markets", [])
            markets.extend(batch)
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
    except Exception as exc:
        print(f"{series:<18} ERROR {type(exc).__name__}: {exc}")
        continue
    settled = [m for m in markets if m.get("result") in ("yes", "no")]
    closes = sorted(m.get("close_time") or "" for m in markets if m.get("close_time"))
    summary[series] = markets
    print(f"{series:<18} {len(markets):>9,} {len(settled):>12,} "
          f"{(closes[0][:10] if closes else '-'):>12} "
          f"{(closes[-1][:10] if closes else '-'):>12}")

print("\n=== DO SETTLED MARKETS CARRY CANDLESTICKS? ===")
print("A label with no features trains nothing, so this is the question")
print("that decides whether a backfill is worth running.\n")
for series, markets in summary.items():
    settled = [m for m in markets if m.get("result") in ("yes", "no")]
    if not settled:
        print(f"{series:<18} no settled markets to sample")
        continue
    got = []
    for m in settled[:3]:
        ticker = m.get("ticker")
        try:
            resp = kalshi.get_historical_candlesticks(
                series_ticker=series, ticker=ticker)
            candles = resp.get("candlesticks", []) if isinstance(resp, dict) else resp
            got.append(len(candles or []))
        except Exception as exc:
            got.append(f"ERR {type(exc).__name__}")
    print(f"{series:<18} candles on 3 sampled markets: {got}")
