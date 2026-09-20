"""
Capture full order-book depth, which exists only in the present tense.

Everything else this project collects can be re-fetched. Settled
markets keep their candles and their trade logs indefinitely, as the
trade backfill just demonstrated by recovering 2021. The BOOK cannot:
Kalshi publishes ten price levels a side right now and keeps no history
of them, so a level that is not written down this minute is gone.

WHY DEPTH AND NOT JUST TOP OF BOOK. market_snapshots already stores the
best bid and ask plus displayed size at those two prices. That answers
"what was the spread" and not "could I have bought 500 contracts". The
whole reason 1,014 trials died was execution realism -- an edge that
needs size, against a book only quoted one deep, is not an edge. Ten
levels turns that from an assumption into arithmetic.

BUDGETED, because the naive version does not fit. A full sweep of every
tracked ticker is ~6,000 requests; at the measured-safe pacing that is
five minutes of solid traffic per cycle, forever, for markets that
mostly are not moving. So each cycle spends a fixed request budget on
the markets where depth actually matters:

  1. closing soonest -- the book tightens and thickens near settlement,
     which is exactly the window every execution question lives in
  2. genuinely traded -- a market with no volume has no book worth
     recording
  3. round-robin for the rest, so nothing is starved entirely

Written as gzipped JSONL beside the other archives, so the existing
sync carries it home unchanged.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import logging
import os
import sqlite3
import time
from contextlib import closing

import throttle as th
from config import SETTINGS
from kalshi_client import KalshiClient

log = logging.getLogger("depth")

DEFAULT_DIR = "data/depth"
CYCLE_SECONDS = 300
REQUESTS_PER_CYCLE = 400
DEPTH_LEVELS = 10
MIN_FREE_GB = 2.0


def _day() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


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


def candidates(db_path: str | None = None, limit: int = 2000) -> list[dict]:
    """Tickers worth sampling, most urgent first.

    Ordered by time to close, because that is where the book changes
    fastest and where every execution question is asked. Markets that
    have already closed are excluded -- their book is gone and the
    request would be wasted.
    """
    now = int(time.time())
    sql = """
        SELECT ticker, MAX(ts) last_seen, close_time,
               MAX(COALESCE(threshold_low_f, 0)) bid_size,
               MAX(COALESCE(threshold_high_f, 0)) ask_size
          FROM market_snapshots
         WHERE ts > ?
         GROUP BY ticker
    """
    out = []
    with closing(sqlite3.connect(db_path or SETTINGS.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(sql, (now - 3600,)):
            close = _epoch(r["close_time"])
            if close is not None and close <= now:
                continue
            out.append({
                "ticker": r["ticker"],
                "close_ts": close,
                "seconds_to_close": (close - now) if close else None,
                "displayed_size": (r["bid_size"] or 0) + (r["ask_size"] or 0),
            })

    def key(row):
        # Soonest close first; unknown close times last. Displayed size
        # breaks ties so a quoted market beats a dead one.
        secs = row["seconds_to_close"]
        return (secs if secs is not None else 10 ** 9,
                -row["displayed_size"])

    out.sort(key=key)
    return out[:limit]


def parse_book(raw: dict) -> dict:
    """Kalshi returns BIDS ONLY, per side, as dollar strings.

    There is no ask array: an ask on one side is the other side's bid
    subtracted from a dollar. Stored as given plus a derived view, so a
    later reader never has to rediscover the convention -- which the
    client's own get_orderbook_levels docstring had to work out once
    already.
    """
    book = raw.get("orderbook_fp") or raw.get("orderbook") or {}

    def levels(key):
        out = []
        for entry in book.get(key) or []:
            try:
                price = round(float(entry[0]) * 100)
                size = float(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            out.append([price, size])
        out.sort(key=lambda x: -x[0])
        return out

    yes_bids = levels("yes_dollars") or levels("yes")
    no_bids = levels("no_dollars") or levels("no")
    return {
        "yes_bids": yes_bids,
        "no_bids": no_bids,
        # What it costs to BUY yes, level by level.
        "yes_asks": [[100 - p, s] for p, s in no_bids][::-1],
        "yes_bid_depth": sum(s for _p, s in yes_bids),
        "no_bid_depth": sum(s for _p, s in no_bids),
    }


def write(rows: list[dict], directory: str) -> int:
    if not rows:
        return 0
    path = os.path.join(directory, f"{_day()}.jsonl.gz")
    os.makedirs(directory, exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":"),
                                default=str) + "\n")
    return len(rows)


def run_once(kalshi: KalshiClient, directory: str, budget: int,
             pacer: th.Throttle, guard: th.DiskGuard,
             cursor: dict) -> dict:
    """One cycle: spend `budget` requests on the most urgent tickers."""
    pool = candidates()
    if not pool:
        return {"sampled": 0, "written": 0, "pool": 0}

    start = cursor.get("offset", 0) % max(len(pool), 1)
    picked = pool[:budget]
    # Anything left over goes round-robin, so distant markets are
    # sampled occasionally rather than never.
    if len(pool) > budget:
        spare = max(0, budget // 4)
        tail = pool[budget:]
        picked = pool[:budget - spare] + [
            tail[(start + i) % len(tail)] for i in range(spare)]
        cursor["offset"] = start + spare

    observed = int(time.time())
    rows = []
    for row in picked:
        raw = th.call(kalshi.get_orderbook, throttle=pacer, guard=guard,
                      ticker=row["ticker"], depth=DEPTH_LEVELS)
        if not raw:
            continue
        book = parse_book(raw)
        if not book["yes_bids"] and not book["no_bids"]:
            continue
        rows.append({
            "ticker": row["ticker"],
            "observed_ts": observed,
            "seconds_to_close": row["seconds_to_close"],
            **book,
        })
    written = write(rows, directory)
    return {"sampled": len(picked), "written": written, "pool": len(pool)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--interval", type=int, default=CYCLE_SECONDS)
    p.add_argument("--budget", type=int, default=REQUESTS_PER_CYCLE)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    kalshi = KalshiClient()
    pacer = th.Throttle(interval=0.05)
    guard = th.DiskGuard(min_free_gb=MIN_FREE_GB)
    cursor: dict = {}

    while True:
        t0 = time.time()
        try:
            c = run_once(kalshi, args.dir, args.budget, pacer, guard, cursor)
            log.info("pool=%d sampled=%d written=%d (%.1fs) "
                     "pacing=%.3fs throttles=%d disk=%.1fGB",
                     c["pool"], c["sampled"], c["written"],
                     time.time() - t0, pacer.interval, pacer.throttles,
                     guard.free_gb)
        except RuntimeError as exc:
            log.error("stopping: %s", exc)
            return 1
        except Exception:
            log.exception("cycle failed")
        if args.once:
            return 0
        time.sleep(max(5, args.interval - (time.time() - t0)))


if __name__ == "__main__":
    raise SystemExit(main())
