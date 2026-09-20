"""
Is a weather contract trading at 20c actually a 20% event?

The question matters because of how Kalshi charges. The taker fee is

    0.07 * contracts * price * (1 - price)

which PEAKS at 50c and shrinks toward both extremes. A coin flip costs
1.75c a contract to trade; a 10c contract costs 0.63c. So if the market
is mispriced by the same amount everywhere, the mispricing is worth
nearly three times as much at the edges -- and if the edges are where
the market is ALSO worst calibrated, that is the place to look.

METHOD, and its limits:

  * ONE OBSERVATION PER MARKET, taken at a fixed horizon before close.
    Pooling every candle would weight long-lived markets more heavily
    and treat 200 readings of the same market as 200 independent facts.
    They are not; a market that drifts at 30c all day contributes one
    data point here, not a day's worth.

  * Wilson intervals, not normal approximation. At 612 rain markets
    split across buckets, several buckets hold a few dozen markets, and
    the normal interval is badly wrong near 0 and 1 -- exactly the
    region this study is about.

  * The YES side and the NO side are the same market seen twice. A
    contract at 20c that resolves NO is equally a statement that the
    80c NO side resolved YES. Reported once, from the YES side.

Perfect calibration is the diagonal: bucket 20-30c should resolve YES
about 25% of the time. A bucket that resolves YES MORE often than its
price says is underpriced -- buying it has positive expected value
before fees. Whether it survives fees is the second column.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from contextlib import closing

import fees
from config import SETTINGS

# Bucket edges in cents. Narrow at the extremes, where the fee argument
# says the money is and where calibration is hardest to get right.
EDGES = [0, 5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 85, 90, 95, 100]


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Binomial confidence interval that behaves near 0 and 1.

    The textbook normal interval puts the lower bound below zero for a
    bucket that went 2-for-40, which is where this study spends most of
    its time.
    """
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bucket_of(price_cents: int) -> int | None:
    for i in range(len(EDGES) - 1):
        if EDGES[i] <= price_cents < EDGES[i + 1]:
            return i
    return None


def load(measures: list[str] | None, horizon_hours: float,
         db_path: str | None = None, max_spread: int = 10,
         use_last_trade: bool = False) -> list[tuple[int, int]]:
    """(price_cents, outcome) at `horizon_hours` before each close.

    THE PRICE IS THE BOOK MIDPOINT, not the last trade, and that
    distinction changes the answer completely.

    yes_price_cents is the candlestick close -- the last print. For a
    thin bracket leg that can be weeks stale against an empty book:
    real rows in this table show a last trade of 96c sitting on a 1c
    bid with zero volume, and another showing 99c when the book was
    46/51. Calibrating against those measures an old print, not a
    price anyone could get. Across all observations, 11.1% have NO bid
    at all and 5.7% sit more than 10c from the mid.

    So a market only counts here if it had a genuine two-sided book:
    a bid above zero, an ask below 100, and a spread no wider than
    max_spread. That discards markets nobody was quoting, which is the
    honest thing to do -- an edge you cannot transact on is not an edge.
    """
    where = ""
    params: list = []
    if measures:
        where = " AND m.measure IN (%s)" % ",".join("?" * len(measures))
        params = list(measures)

    sql = f"""
        SELECT m.result, p.yes_price_cents, p.yes_bid_cents,
               p.yes_ask_cents
          FROM historical_markets m
          JOIN historical_price_points p ON p.ticker = m.ticker
         WHERE m.result IN ('yes','no')
           AND m.close_time IS NOT NULL{where}
           AND p.ts = (SELECT q.ts FROM historical_price_points q
                        WHERE q.ticker = m.ticker
                          AND q.ts <= strftime('%s', m.close_time) - ?
                        ORDER BY q.ts DESC LIMIT 1)
    """
    # PARAMETER ORDER FOLLOWS THE SQL TEXT, not the logical order they
    # were written in. The measure filter is spliced into the WHERE
    # clause ABOVE the horizon placeholder in the subquery, so the
    # measure strings bind first. Passing [horizon] + measures silently
    # swapped them -- the horizon integer went into `measure IN (...)`
    # and a measure name went into the timestamp arithmetic. No error,
    # just wrong rows, and only the unfiltered case looked sane.
    out = []
    with closing(sqlite3.connect(db_path or SETTINGS.db_path)) as conn:
        for result, last, bid, ask in conn.execute(
                sql, params + [int(horizon_hours * 3600)]):
            y = 1 if result == "yes" else 0
            if use_last_trade:
                if last is None or not (0 <= last <= 100):
                    continue
                out.append((int(last), y))
                continue
            if bid is None or ask is None:
                continue
            if not (0 < bid <= 100 and 0 < ask <= 100 and ask >= bid):
                continue
            if ask - bid > max_spread:
                continue
            out.append((int(round((bid + ask) / 2.0)), y))
    return out


def report(rows: list[tuple[int, int]], label: str) -> None:
    buckets: dict[int, list[int]] = {}
    for px, y in rows:
        b = bucket_of(px)
        if b is None:
            continue
        buckets.setdefault(b, []).append(y)

    print(f"\n{'=' * 78}")
    print(f"{label}   {len(rows):,} markets")
    print("=" * 78)
    print(f"{'price':<10} {'n':>6} {'mid':>5} {'actual':>7} "
          f"{'95% CI':>15} {'gap':>7} {'fee':>6} {'net':>7}")
    print("-" * 78)

    for b in sorted(buckets):
        ys = buckets[b]
        n = len(ys)
        if n < 15:
            continue
        hits = sum(ys)
        actual = hits / n
        lo, hi = wilson(hits, n)
        mid = (EDGES[b] + EDGES[b + 1]) / 2.0
        implied = mid / 100.0
        gap = actual - implied            # + means YES is underpriced
        # Round-trip cost of taking this bet one contract at a time:
        # entry now, and exit at settlement is free (0 or 100 pays no
        # fee), so it is the entry fee alone.
        fee = fees.taker_fee_cents(1, int(round(mid))) / 100.0
        net = abs(gap) - fee
        flag = ""
        if lo > implied:
            flag = "  UNDERPRICED"
        elif hi < implied:
            flag = "  OVERPRICED"
        print(f"{EDGES[b]:>3}-{EDGES[b+1]:<6} {n:>6,} {mid:>5.1f} "
              f"{100*actual:>6.1f}% {100*lo:>6.1f}-{100*hi:<6.1f} "
              f"{100*gap:>+6.1f} {100*fee:>5.1f}c {100*net:>+6.1f}{flag}")

    print("\n  gap = actual YES rate minus the price. Positive means the")
    print("  market UNDERPRICES yes. A bucket is only called mispriced")
    print("  when its whole 95% interval sits off the diagonal.")
    print("  net = |gap| minus the one-way taker fee at that price.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--horizon-hours", type=float, default=24.0)
    p.add_argument("--max-spread", type=int, default=10,
                   help="widest bid-ask, in cents, still counted")
    p.add_argument("--last-trade", action="store_true",
                   help="use the stale last print instead of the book "
                        "(for comparison only -- see load())")
    p.add_argument("--db")
    args = p.parse_args()

    print(f"price = book midpoint {args.horizon_hours:g}h before close, "
          f"one observation per market")
    print(f"markets counted only with a two-sided book, spread <= "
          f"{args.max_spread}c")
    if args.last_trade:
        print("!! using LAST TRADE -- includes stale prints on empty books")

    for label, measures in (
            ("RAIN -- daily precipitation", ["precipitation_daily"]),
            ("RAIN -- monthly precipitation", ["precipitation_monthly"]),
            ("TEMPERATURE brackets (high and low)",
             ["temperature_high", "temperature_low"]),
            ("EVERYTHING", None)):
        rows = load(measures, args.horizon_hours, args.db,
                    args.max_spread, args.last_trade)
        if rows:
            report(rows, label)
        else:
            print(f"\n{label}: no markets with a quoted book at this horizon")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
