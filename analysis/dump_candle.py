"""Print one settled market's raw candles verbatim.

The last probe reported a 0c price range on every family including
traded markets, which is not a thing a real order book does. Guessing
at the field shape a second time would be a good way to get a second
plausible-looking wrong answer, so print what the API actually sends.
"""
import datetime as dt
import json

from kalshi_client import KalshiClient

kalshi = KalshiClient()


def epoch(value) -> int:
    return int(dt.datetime.fromisoformat(
        str(value).replace("Z", "+00:00")).timestamp())


resp = kalshi.get_historical_markets(series_ticker="KXNFLGAME", limit=20)
market = [m for m in resp["markets"] if m.get("result") in ("yes", "no")][0]
print("TICKER:", market["ticker"], " RESULT:", market["result"])
print("MARKET KEYS:", sorted(market.keys()))

start, end = epoch(market["open_time"]), epoch(market["close_time"])
candles = kalshi.get_historical_candlesticks(
    series_ticker="KXNFLGAME", ticker=market["ticker"],
    start_ts=start, end_ts=end, period_interval=60).get("candlesticks") or []

print(f"\n{len(candles)} candles. First two, verbatim:\n")
for c in candles[:2]:
    print(json.dumps(c, indent=2))

print("\nA candle from the middle of the market's life:\n")
if len(candles) > 4:
    print(json.dumps(candles[len(candles) // 2], indent=2))
