"""
Pull settled non-weather market history straight to syncable files.

"Wait for data to accumulate" was the plan until it turned out the data
already exists: thousands of settled markets per family going back to
May 2025, each with a full price trajectory. That is months of training
data available today instead of in three months.

WHAT A CANDLE ACTUALLY CARRIES, verbatim from the API:

    "price":   {"open": "0.8600", "close": "0.9800",
                "high": "0.9800", "low": "0.6900", "mean": "0.8472"}
    "yes_bid": {"open": "0.8500", "close": "0.9700", ...}
    "yes_ask": {"open": "0.8600", "close": "0.9800", ...}
    "volume": "2245449.00"   "open_interest": "5100536.00"

Separate OHLC for price, bid AND ask. historical_price_points keeps only
the close, and its own schema comment records that throwing away bid/ask
"taught the hard way" -- so this stores the candle whole rather than
learning it a third time. Prices are DOLLAR strings, not cents; reading
"0.6000" as an int is how an earlier probe reported a 0c price range on
every market on the exchange.

WHY FILES AND NOT SQLITE. The droplet is a 1 vCPU box that just stopped
growing, and putting a few hundred MB of history back into the hot
database would undo that. This writes the same gzipped-JSONL shape
export_archive.py produces, under data/, so sync-kalshi-data.ps1 carries
it to the machine with the disk and the GPU without any change.

Resumable by design: completed tickers are recorded per series, and a
re-run skips them. A long job on a small box WILL be interrupted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import time

from kalshi_client import KalshiClient

DEFAULT_DIR = "data/backfill"
DONE_DIR = os.path.join(DEFAULT_DIR, "_done")

# Deep books and a fixture list that repeats forever. Settlement here is
# unambiguous -- nobody misreads "did Seattle win" -- so these are for
# liquidity and volume of examples, not for the thesis.
DEEP_AND_LIQUID = (
    "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL",
    "KXEPLGAME", "KXEPLSPREAD", "KXEPLTOTAL",
    "KXLALIGAGAME", "KXLALIGATOTAL", "KXSERIEAGAME", "KXSERIEATOTAL",
    "KXBUNDESLIGAGAME", "KXLIGUE1GAME",
    "KXMLBGAME", "KXUFCFIGHT",
)

# Where the project's actual thesis lives. A game line settles on who
# won. These settle on an OFFICIAL SCORER'S RULING -- hit or error is a
# human judgment call, and it can be revised after the game is over.
# That is the Austin failure mode exactly: the bet was right about the
# world and wrong about what the contract measures.
AMBIGUOUS_SETTLEMENT = (
    "KXRT", "KXTRUMPMENTION", "KXHORMUZWEEKLY",
    "KXMLBHIT", "KXMLBTB", "KXMLBHRR", "KXMLBRBI", "KXMLBSB",
    "KXMLBKS", "KXMLBHR",
    "KXWNBAPTS", "KXWNBAREB",
    "KXEPLFIRSTGOAL", "KXLALIGAFIRSTGOAL", "KXEPLGOAL", "KXLALIGAGOAL",
)

# Added after surveying settled history across the exchange by VOLUME
# PER MARKET, which is the figure that decides whether a family is worth
# collecting. Total count is how weather looked good and was not: the
# raw settled-market ranking is topped by a 22,000-market parlay series
# trading 867 per market, while these trade hundreds of thousands.
#
#   KXNBAGAME     2,892 settled   4,035,993 per market
#   KXNCAAFGAME   1,872 settled   2,147,871
#   KXATPMATCH    4,812+ CAPPED     797,362
#   KXWNBAGAME    1,004 settled     679,929
#   KXNHLGAME     3,074 settled     509,911
#
# Several probes hit their page cap, so these counts are floors.
#
# ESPN ground truth was checked before committing to any of them:
# NBA, WNBA and NCAAB return fully timed play-by-play with win
# probability; NCAAF needs the core API exactly as NFL does; NHL gives
# plays but no win probability, so it is score-and-clock only. Tennis
# returns TOURNAMENTS rather than matches and has no play data at all --
# it is included for price and outcome, with no third leg, and that
# limit is recorded rather than discovered later.
HIGH_VOLUME_EXPANSION = (
    "KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL",
    "KXNHLGAME",
    "KXNCAAFGAME",
    "KXWNBAGAME",
    # No ESPN ground truth; collected for price and outcome only.
    "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH",
    "KXITFMATCH", "KXITFWMATCH",
    # Esports. ESPN does not cover these either.
    "KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME",
)

# One more "mention" family for the settlement thesis: whether specific
# words were said, adjudicated by a human reading a transcript.
AMBIGUOUS_SETTLEMENT_EXTRA = ("KXMAMDANIMENTION",)

ALL_SERIES = (DEEP_AND_LIQUID + AMBIGUOUS_SETTLEMENT
              + HIGH_VOLUME_EXPANSION + AMBIGUOUS_SETTLEMENT_EXTRA)

# What a second, concurrent run should take. The per-series done-markers
# make separate series independent, so this can run alongside a backfill
# already working through ALL_SERIES without either treading on the
# other.
EXPANSION = HIGH_VOLUME_EXPANSION + AMBIGUOUS_SETTLEMENT_EXTRA

# Kalshi publishes no read limit this code can rely on, so pace rather
# than discover one. ~4 requests/second with a backoff on failure.
REQUEST_INTERVAL = 0.25
FINAL_STRETCH_HOURS = 6      # window covered at 1-minute resolution


def _epoch(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _fp(value, default=0.0) -> float:
    """Kalshi sends numbers as fixed-point strings ("2245449.00")."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _throttled(fn, *args, **kwargs):
    """Call with pacing, and back off rather than hammer on failure."""
    delay = 1.0
    for attempt in range(5):
        try:
            result = fn(*args, **kwargs)
            time.sleep(REQUEST_INTERVAL)
            return result
        except Exception as exc:
            if attempt == 4:
                raise
            # A 404 means this market simply has no candles; retrying
            # that is pure waste, and it is a normal state for a market
            # that never traded.
            if "404" in str(exc):
                return None
            time.sleep(delay)
            delay *= 2
    return None


def done_path(series: str, directory: str = DEFAULT_DIR) -> str:
    return os.path.join(directory, "_done", f"{series}.txt")


def load_done(series: str, directory: str = DEFAULT_DIR) -> set[str]:
    path = done_path(series, directory)
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def mark_done(series: str, ticker: str, directory: str = DEFAULT_DIR) -> None:
    path = done_path(series, directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(ticker + "\n")


def settled_markets(kalshi: KalshiClient, series: str,
                    max_markets: int | None = None) -> list[dict]:
    """Every settled market in a series, paged to exhaustion."""
    out: list[dict] = []
    cursor = None
    while True:
        resp = _throttled(kalshi.get_historical_markets,
                          series_ticker=series, cursor=cursor, limit=200)
        if not resp:
            break
        batch = resp.get("markets", [])
        out.extend(m for m in batch if m.get("result") in ("yes", "no"))
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
        if max_markets and len(out) >= max_markets:
            break
    return out[:max_markets] if max_markets else out


def fetch_candles(kalshi: KalshiClient, series: str, market: dict) -> dict:
    """Hourly candles across the market's life, plus 1-minute candles
    over the final stretch where the price actually resolves."""
    ticker = market["ticker"]
    start, end = _epoch(market.get("open_time")), _epoch(market.get("close_time"))
    if not start or not end or end <= start:
        return {"hourly": [], "minute": []}

    hourly = _throttled(kalshi.get_historical_candlesticks,
                        series_ticker=series, ticker=ticker,
                        start_ts=start, end_ts=end, period_interval=60)
    minute = _throttled(kalshi.get_historical_candlesticks,
                        series_ticker=series, ticker=ticker,
                        start_ts=max(start, end - FINAL_STRETCH_HOURS * 3600),
                        end_ts=end, period_interval=1)
    return {
        "hourly": (hourly or {}).get("candlesticks") or [],
        "minute": (minute or {}).get("candlesticks") or [],
    }


def _out_path(series: str, market: dict, directory: str) -> str:
    """Partition by settlement date, matching the export layout so the
    existing sync picks these up with no change."""
    ts = _epoch(market.get("close_time")) or _epoch(market.get("settlement_ts")) or 0
    day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(directory, series, f"{day}.jsonl.gz")


def backfill_series(kalshi: KalshiClient, series: str,
                    directory: str = DEFAULT_DIR,
                    max_markets: int | None = None,
                    skip_untraded: bool = True) -> dict:
    done = load_done(series, directory)
    markets = settled_markets(kalshi, series, max_markets)

    stats = {"series": series, "settled": len(markets), "already_done": 0,
             "untraded": 0, "written": 0, "candles": 0, "bytes": 0,
             "errors": 0}

    for market in markets:
        ticker = market["ticker"]
        if ticker in done:
            stats["already_done"] += 1
            continue
        # A market that never traded returns no candles. That is not an
        # error and not worth a request -- volume is on the listing.
        if skip_untraded and _fp(market.get("volume_fp")) <= 0:
            stats["untraded"] += 1
            mark_done(series, ticker, directory)
            continue
        try:
            candles = fetch_candles(kalshi, series, market)
        except Exception:
            stats["errors"] += 1
            continue
        if not candles["hourly"] and not candles["minute"]:
            stats["untraded"] += 1
            mark_done(series, ticker, directory)
            continue

        record = {
            "ticker": ticker,
            "series_ticker": series,
            "event_ticker": market.get("event_ticker"),
            "title": market.get("title"),
            "yes_sub_title": market.get("yes_sub_title"),
            "result": market.get("result"),
            "open_time": market.get("open_time"),
            "close_time": market.get("close_time"),
            "settlement_ts": market.get("settlement_ts"),
            "settlement_value_dollars": market.get("settlement_value_dollars"),
            "expiration_value": market.get("expiration_value"),
            "volume": _fp(market.get("volume_fp")),
            "open_interest": _fp(market.get("open_interest_fp")),
            # The settlement rules, kept verbatim. This is the single
            # field that separates "I was wrong about the game" from "I
            # was wrong about what the contract reads", and it is not
            # reconstructable after the market is gone.
            "rules_primary": market.get("rules_primary"),
            "rules_secondary": market.get("rules_secondary"),
            "strike_type": market.get("strike_type"),
            "custom_strike": market.get("custom_strike"),
            "market_type": market.get("market_type"),
            "candles_hourly": candles["hourly"],
            "candles_minute": candles["minute"],
            "backfilled_ts": int(time.time()),
        }

        path = _out_path(series, market, directory)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")

        mark_done(series, ticker, directory)
        stats["written"] += 1
        stats["candles"] += len(candles["hourly"]) + len(candles["minute"])

    total = 0
    series_dir = os.path.join(directory, series)
    if os.path.isdir(series_dir):
        total = sum(os.path.getsize(os.path.join(series_dir, f))
                    for f in os.listdir(series_dir))
    stats["bytes"] = total
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--series", action="append",
                   help="limit to these series (repeatable)")
    p.add_argument("--max-markets", type=int,
                   help="cap per series -- use for a costed trial run")
    p.add_argument("--group",
                   choices=["all", "liquid", "ambiguous", "expansion"],
                   default="all")
    args = p.parse_args()

    if args.series:
        series_list = args.series
    elif args.group == "liquid":
        series_list = list(DEEP_AND_LIQUID)
    elif args.group == "ambiguous":
        series_list = list(AMBIGUOUS_SETTLEMENT)
    elif args.group == "expansion":
        series_list = list(EXPANSION)
    else:
        series_list = list(ALL_SERIES)

    kalshi = KalshiClient()
    print(f"backfilling {len(series_list)} series to {args.dir}/")
    if args.max_markets:
        print(f"capped at {args.max_markets} markets per series\n")

    grand = {"written": 0, "candles": 0, "untraded": 0, "errors": 0}
    t0 = time.time()
    for series in series_list:
        try:
            s = backfill_series(kalshi, series, args.dir, args.max_markets)
        except Exception as exc:
            print(f"  {series:<22} FAILED: {type(exc).__name__}: {exc}")
            continue
        for k in grand:
            grand[k] += s.get(k, 0)
        print(f"  {series:<22} settled={s['settled']:>6,} "
              f"written={s['written']:>6,} skipped={s['already_done']:>6,} "
              f"untraded={s['untraded']:>5,} candles={s['candles']:>8,} "
              f"{s['bytes']/1048576:>6.1f} MB")

    mins = (time.time() - t0) / 60
    print(f"\n{grand['written']:,} markets, {grand['candles']:,} candles, "
          f"{grand['untraded']:,} untraded, {grand['errors']:,} errors "
          f"in {mins:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
