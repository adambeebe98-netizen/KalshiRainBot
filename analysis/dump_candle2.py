"""Find a settled market that DOES return candles, and print them raw.

The first market sampled returned zero candles over its full life, so
before concluding anything about coverage: walk several markets, and for
each try both the full window and a short one ending at close. Print the
first real candle verbatim rather than inferring its shape.
"""
import datetime as dt
import json

from kalshi_client import KalshiClient

kalshi = KalshiClient()


def epoch(value) -> int:
    return int(dt.datetime.fromisoformat(
        str(value).replace("Z", "+00:00")).timestamp())


resp = kalshi.get_historical_markets(series_ticker="KXNFLGAME", limit=60)
settled = [m for m in resp["markets"] if m.get("result") in ("yes", "no")]
print(f"{len(settled)} settled markets to walk\n")

found = None
for m in settled[:15]:
    start, end = epoch(m["open_time"]), epoch(m["close_time"])
    life_hours = (end - start) / 3600
    attempts = {
        "full life": (start, end, 60),
        "last 24h": (max(start, end - 86400), end, 60),
        "last 4h @1m": (max(start, end - 4 * 3600), end, 1),
    }
    line = [f"{m['ticker'][:34]:<34} life={life_hours:6.1f}h "
            f"vol={float(m.get('volume_fp') or 0):>9,.0f}"]
    for label, (s, e, iv) in attempts.items():
        try:
            c = kalshi.get_historical_candlesticks(
                series_ticker="KXNFLGAME", ticker=m["ticker"],
                start_ts=s, end_ts=e, period_interval=iv
            ).get("candlesticks") or []
            line.append(f"{label}={len(c):>4}")
            if c and found is None:
                found = (m, label, c)
        except Exception as exc:
            line.append(f"{label}=ERR({str(exc)[:28]})")
    print("  ".join(line))

if not found:
    print("\nNo candles returned for any sampled market.")
    raise SystemExit(0)

market, label, candles = found
print(f"\n=== {market['ticker']} via '{label}': {len(candles)} candles ===")
print(f"result={market['result']}  "
      f"settlement={market.get('settlement_value_dollars')}\n")
print("FIRST:")
print(json.dumps(candles[0], indent=2))
print("\nMIDDLE:")
print(json.dumps(candles[len(candles) // 2], indent=2))
print("\nLAST:")
print(json.dumps(candles[-1], indent=2))
