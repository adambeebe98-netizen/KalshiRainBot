"""
Does the rain edge appear in the final hour, where the spread collapses?

Every negative result so far was measured on HOURLY candles whose last
reading sits a median of 60 minutes before close. At that distance the
median weather spread is 10c, which is wider than any mispricing found,
so both sides of every trade lost -- necessarily, because buying both
sides costs exactly the spread plus fees.

The minute candles change the question. They cover the final six hours
at one-minute resolution, and the hourly data already hinted that
spreads fall from 10c to about 1c approaching close. A 1c spread leaves
room that a 10c spread does not.

So this asks, at minute resolution and bucketed by time-to-close:

  1. what the spread actually is, minute by minute
  2. whether the price is calibrated there
  3. whether buying at the real ask, net of fees, makes money

Note what is NOT claimed: this is the same 1,571 markets seen at finer
resolution, not new independent evidence. If an edge appears only in
the last few minutes of a market that has already resolved in
substance, it is likely to be unfillable size rather than free money --
which the volume column is there to expose.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
from collections import defaultdict

import fees

BANDS = [(0, 15), (15, 60), (60, 180), (180, 360)]


def fp(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def epoch(value):
    import datetime as dt
    if value is None:
        return None
    try:
        return int(dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def read(pattern):
    for path in sorted(glob.glob(pattern)):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="data/backfill_wx")
    ap.add_argument("--series", default="KXRAINNYC")
    args = ap.parse_args()

    pattern = os.path.join(args.dir, args.series, "*.jsonl.gz")

    spreads = defaultdict(list)
    obs = defaultdict(list)          # band -> (mid, ask, bid, vol, label)
    markets = 0

    for m in read(pattern):
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        close = epoch(m.get("close_time"))
        if close is None:
            continue
        markets += 1
        label = 1 if result == "yes" else 0
        for c in (m.get("candles_minute") or []):
            ts = c.get("end_period_ts")
            if ts is None:
                continue
            mins = (close - int(ts)) / 60.0
            if mins < 0:
                continue
            bid = fp((c.get("yes_bid") or {}).get("close"), -1) * 100
            ask = fp((c.get("yes_ask") or {}).get("close"), -1) * 100
            if not (0 < bid <= 100 and 0 < ask <= 100 and ask >= bid):
                continue
            vol = fp(c.get("volume"))
            for lo, hi in BANDS:
                if lo <= mins < hi:
                    spreads[(lo, hi)].append(ask - bid)
                    obs[(lo, hi)].append(
                        ((bid + ask) / 2.0, ask, bid, vol, label))
                    break

    print(f"{args.series}: {markets:,} settled markets with minute candles\n")
    print(f"{'minutes to close':<18} {'candles':>9} {'med spread':>11} "
          f"{'p90 spread':>11} {'candles w/ volume':>18}")
    print("-" * 72)
    for band in BANDS:
        s = sorted(spreads.get(band, []))
        o = obs.get(band, [])
        if not s:
            continue
        traded = sum(1 for x in o if x[3] > 0)
        print(f"{band[0]:>4}-{band[1]:<13} {len(s):>9,} "
              f"{s[len(s)//2]:>10.1f}c {s[int(0.9*len(s))]:>10.1f}c "
              f"{100.0*traded/len(o):>17.1f}%")

    print("\n  Calibration and executable edge, by time-to-close.")
    print("  E = expected cents per contract paying the REAL ask, net of")
    print("  the entry fee, using each bucket's own realised rate.\n")

    for band in BANDS:
        o = obs.get(band, [])
        if len(o) < 200:
            continue
        print(f"  --- {band[0]}-{band[1]} minutes to close "
              f"({len(o):,} candles) ---")
        print(f"    {'mid':<9} {'n':>7} {'actual':>7} {'95% CI':>14} "
              f"{'ask':>6} {'E[buy YES]':>11}")
        by_b = defaultdict(list)
        for mid, ask, bid, vol, label in o:
            b = int(mid // 10) * 10
            by_b[b].append((ask, label))
        for b in sorted(by_b):
            rows = by_b[b]
            n = len(rows)
            if n < 100:
                continue
            k = sum(r[1] for r in rows)
            q = k / n
            lo, hi = wilson(k, n)
            avg_ask = sum(r[0] for r in rows) / n
            e = 100 * q - avg_ask - fees.taker_fee_cents(1, round(avg_ask))
            mark = "  <-- positive" if e > 0 else ""
            print(f"    {b:>3}-{b+10:<5} {n:>7,} {100*q:>6.1f}% "
                  f"{100*lo:>6.1f}-{100*hi:<6.1f} {avg_ask:>5.1f}c "
                  f"{e:>+10.2f}c{mark}")
        print()

    print("  These candles are NOT independent -- the same market appears")
    print("  once a minute -- so the intervals are far too narrow and no")
    print("  bucket here is a finding. This measures whether the SPREAD")
    print("  leaves room, which is the thing the hourly data could not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
