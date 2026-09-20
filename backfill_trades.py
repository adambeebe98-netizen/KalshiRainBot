"""
Pull the executed-trade log for settled markets.

This is the single largest thing the archive was missing, and it was
missing for the most ordinary reason: the client had no method for it,
so nobody could call it, so nobody noticed it existed.

WHY IT MATTERS MORE THAN CANDLES. Every rejected candidate so far died
on the same question -- could this trade actually have been filled? A
candle answers "the hour closed at 20c", which is compatible with both
"there was a deep book at 20c all hour" and "one contract printed at
20c and nothing else happened". The trade log distinguishes them:

    created_time        2026-04-08T03:58:51.909084Z
    count_fp            "467.00"
    yes_price_dollars   "0.9900"
    taker_side          "no"          <- who crossed the spread
    is_block_trade      false

That last field pair is not available anywhere else. Knowing which side
initiated turns a price series into an order-flow series.

TWO ENDPOINTS, ONE ARCHIVE. /markets/trades answers for markets inside
Kalshi's live window and returns an EMPTY LIST -- not an error -- for
anything older. /historical/trades answers for the rest. Probing only
the first is how this was briefly, and wrongly, reported as an expiring
rolling window with a deadline. This job tries live first and falls
back, so a market is fetched correctly wherever it sits relative to the
cutoff.

Written to gzipped JSONL partitioned by settlement day, the same shape
export_archive.py and backfill_sports.py already produce, so the
existing sync carries it home with no change. Resumable per ticker,
because a job this size will be interrupted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import time

import throttle as th
from kalshi_client import KalshiClient

DEFAULT_DIR = "data/trades"
PAGE_LIMIT = 1000
MAX_PAGES_PER_MARKET = 200          # 200k trades; nothing is that big

# Measured, not guessed: Kalshi answers these reads in ~10ms and served
# 50 requests a second with no 429 at all. The old hard-coded 0.2s
# sleep meant spending 95% of the job asleep -- 4.3 hours for the full
# weather archive instead of under one.
#
# 0.05s is deliberately four times slower than the rate that showed no
# throttling, because 25 clean requests is not proof of a ceiling. The
# Throttle widens itself on any 429 and eases back only after a long
# clean run, so the conservative guess costs little if it is wrong in
# either direction.
DEFAULT_INTERVAL = 0.05

# The droplet runs five live collectors. A backfill that fills the disk
# takes them down, and their data -- unlike this job's -- cannot be
# re-fetched at any price.
MIN_FREE_GB = 2.0


def fp(value, default=0.0) -> float:
    """Sizes and prices arrive as fixed-point STRINGS."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def epoch(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def done_path(series: str, directory: str) -> str:
    return os.path.join(directory, "_done", f"{series}.txt")


def load_done(series: str, directory: str) -> set[str]:
    path = done_path(series, directory)
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def mark_done(series: str, ticker: str, directory: str) -> None:
    path = done_path(series, directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(ticker + "\n")


def fetch_trades(kalshi: KalshiClient, ticker: str, pacer: th.Throttle,
                 guard: th.DiskGuard) -> tuple[list, str]:
    """Every trade for one market. Returns (trades, which_endpoint).

    Live first, then historical. An empty live result is NOT proof the
    market never traded -- it is what the live endpoint returns for
    anything past the cutoff -- so the fallback is unconditional rather
    than triggered by an error.
    """
    for label, fn in (("live", kalshi.get_trades),
                      ("historical", kalshi.get_historical_trades)):
        out: list = []
        cursor = None
        for _ in range(MAX_PAGES_PER_MARKET):
            resp = th.call(fn, throttle=pacer, guard=guard, ticker=ticker,
                           cursor=cursor, limit=PAGE_LIMIT)
            if not resp:
                break
            batch = resp.get("trades") or []
            out.extend(batch)
            cursor = resp.get("cursor")
            if not cursor or not batch:
                break
        if out:
            return out, label
    return [], "none"


def backfill_series(kalshi: KalshiClient, series: str, directory: str,
                    max_markets: int | None = None,
                    pacer: th.Throttle | None = None,
                    guard: th.DiskGuard | None = None) -> dict:
    pacer = pacer or th.Throttle(interval=DEFAULT_INTERVAL)
    guard = guard or th.DiskGuard(min_free_gb=MIN_FREE_GB)
    done = load_done(series, directory)
    stats = {"series": series, "markets": 0, "skipped": 0, "trades": 0,
             "contracts": 0.0, "no_trades": 0, "live": 0, "historical": 0}

    markets, cursor = [], None
    while True:
        resp = th.call(kalshi.get_historical_markets, throttle=pacer,
                       guard=guard, series_ticker=series, cursor=cursor,
                       limit=200)
        if not resp:
            break
        batch = resp.get("markets", [])
        markets.extend(m for m in batch if m.get("result") in ("yes", "no"))
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
        if max_markets and len(markets) >= max_markets:
            break
    if max_markets:
        markets = markets[:max_markets]

    for market in markets:
        ticker = market.get("ticker")
        if not ticker or ticker in done:
            stats["skipped"] += 1
            continue
        # A market that never traded has no trade log, and the listing
        # already says so -- no need to spend two requests finding out.
        if fp(market.get("volume_fp")) <= 0:
            stats["no_trades"] += 1
            mark_done(series, ticker, directory)
            continue

        trades, source = fetch_trades(kalshi, ticker, pacer, guard)
        if not trades:
            stats["no_trades"] += 1
            mark_done(series, ticker, directory)
            continue
        stats[source] = stats.get(source, 0) + 1

        close = epoch(market.get("close_time")) or 0
        day = dt.datetime.fromtimestamp(
            close, dt.timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(directory, series, f"{day}.jsonl.gz")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        contracts = sum(fp(t.get("count_fp")) for t in trades)
        record = {
            "ticker": ticker,
            "series_ticker": series,
            "event_ticker": market.get("event_ticker"),
            "result": market.get("result"),
            "close_time": market.get("close_time"),
            "volume": fp(market.get("volume_fp")),
            "source": source,
            "trade_count": len(trades),
            "contracts": contracts,
            "trades": trades,
            "backfilled_ts": int(time.time()),
        }
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":"),
                                default=str) + "\n")

        mark_done(series, ticker, directory)
        stats["markets"] += 1
        stats["trades"] += len(trades)
        stats["contracts"] += contracts

    size = 0
    d = os.path.join(directory, series)
    if os.path.isdir(d):
        size = sum(os.path.getsize(os.path.join(d, f))
                   for f in os.listdir(d))
    stats["bytes"] = size
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--series", action="append", required=True)
    p.add_argument("--max-markets", type=int)
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help="seconds between requests; adapts upward on 429")
    p.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB,
                   help="abort if the disk falls below this")
    args = p.parse_args()

    kalshi = KalshiClient()
    pacer = th.Throttle(interval=args.interval)
    guard = th.DiskGuard(min_free_gb=args.min_free_gb)
    guard.check(force=True)

    print(f"trade backfill: {len(args.series)} series -> {args.dir}/")
    print(f"  pacing {args.interval:.3f}s ({1/args.interval:.0f} req/s), "
          f"backs off on 429")
    print(f"  disk {guard.free_gb:.1f} GB free, aborting below "
          f"{args.min_free_gb:.1f} GB")
    t0 = time.time()
    grand = {"markets": 0, "trades": 0, "contracts": 0.0, "no_trades": 0}

    for series in args.series:
        try:
            s = backfill_series(kalshi, series, args.dir, args.max_markets,
                                pacer, guard)
        except RuntimeError as exc:
            # The disk guard. Stop the whole job, not just this series.
            print(f"\n  ABORTED: {exc}")
            break
        except Exception as exc:
            print(f"  {series:<22} FAILED {type(exc).__name__}: {exc}")
            continue
        for k in grand:
            grand[k] += s.get(k, 0)
        print(f"  {series:<22} markets={s['markets']:>6,} "
              f"skipped={s['skipped']:>6,} untraded={s['no_trades']:>5,} "
              f"trades={s['trades']:>9,} "
              f"contracts={s['contracts']:>13,.0f} "
              f"[live {s['live']}, hist {s['historical']}] "
              f"{s['bytes']/1048576:>6.1f} MB")

    mins = (time.time() - t0) / 60
    print(f"\n{grand['markets']:,} markets, {grand['trades']:,} trades, "
          f"{grand['contracts']:,.0f} contracts in {mins:.1f} min")
    print(f"  {pacer.requests:,} requests, {pacer.throttles} throttled, "
          f"final pacing {pacer.interval:.3f}s")
    print(f"  disk {guard.free_gb:.1f} GB free")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
