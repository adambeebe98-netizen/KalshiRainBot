"""Do settled sports markets carry a usable price trajectory?

The market listing already answered the first question: thousands of
settled markets per family, back to May 2025. That is a label set.

A label with no features trains nothing, so this asks the second
question -- whether the price path over each market's life comes back,
at what resolution, and whether it actually moves. A market that sits at
50c until it settles is a row in a table, not a training example.
"""
import datetime as dt

from kalshi_client import KalshiClient

kalshi = KalshiClient()

FAMILIES = ["KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXEPLGAME",
            "KXLALIGAGAME", "KXSERIEAGAME", "KXMLBGAME", "KXUFCFIGHT",
            "KXRT", "KXTRUMPMENTION"]


def epoch(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


print(f"{'series':<16} {'interval':>9} {'candles':>8} {'traded':>7} "
      f"{'price range':>13} {'moves>10c':>10}")
print("-" * 70)

for series in FAMILIES:
    resp = kalshi.get_historical_markets(series_ticker=series, limit=50)
    markets = [m for m in resp.get("markets", [])
               if m.get("result") in ("yes", "no")]
    if not markets:
        print(f"{series:<16} no settled markets")
        continue

    for interval, label in ((60, "hourly"), (1, "1-min")):
        got = []
        for m in markets[:5]:
            start = epoch(m.get("open_time"))
            end = epoch(m.get("close_time"))
            if not start or not end:
                continue
            # 1-minute candles over a long window get refused, so ask for
            # the last stretch before close rather than the whole life.
            if interval == 1:
                start = max(start, end - 6 * 3600)
            try:
                r = kalshi.get_historical_candlesticks(
                    series_ticker=series, ticker=m["ticker"],
                    start_ts=start, end_ts=end, period_interval=interval)
                got.append(r.get("candlesticks") or [])
            except Exception as exc:
                got.append(f"ERR {exc}"[:60])

        usable = [c for c in got if isinstance(c, list) and c]
        if not usable:
            errs = [g for g in got if isinstance(g, str)]
            print(f"{series:<16} {label:>9} {'-':>8}  "
                  f"{errs[0] if errs else 'empty'}")
            continue

        counts, traded, spans, movers = [], 0, [], 0
        for candles in usable:
            counts.append(len(candles))
            prices = []
            vol = 0
            for c in candles:
                # Volume comes back as a fixed-point STRING ("1028.00"),
                # the same quirk as volume_fp on the market object.
                vol += int(float(c.get("volume") or 0))
                p = c.get("price") or {}
                close = p.get("close") if isinstance(p, dict) else None
                if close is None and isinstance(p, dict):
                    close = p.get("mean")
                if close is not None:
                    prices.append(int(float(close)))
            if vol:
                traded += 1
            if prices:
                spans.append(max(prices) - min(prices))
                if max(prices) - min(prices) > 10:
                    movers += 1

        avg = sum(counts) // len(counts)
        span = f"{min(spans)}-{max(spans)}c" if spans else "-"
        print(f"{series:<16} {label:>9} {avg:>8,} {traded:>5}/{len(usable)} "
              f"{span:>13} {movers:>8}/{len(usable)}")
