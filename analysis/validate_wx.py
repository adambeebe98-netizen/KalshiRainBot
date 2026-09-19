"""Backfill NYC observations, then check the archive against reality.

The acid test for this whole approach: if summing p01i over a market's
day reproduces how that market actually settled, then the archive
captures the quantity the contract pays on, and a model trained against
it is predicting the right thing. If it does not, the archive is a nice
weather dataset and not a trading one.

It also settles a question raised but not answered earlier: the CLI
"day" is local midnight to midnight, not UTC, and the contracts close at
a fixed UTC time. Three candidate windows are scored so the right one is
chosen by evidence rather than assumption.
"""
import datetime as dt
import sqlite3
import sys

import splits
import weather_archive
from config import SETTINGS

STATION = "NYC"


def backfill():
    print("backfilling NYC observations from IEM...")
    out = weather_archive.backfill_station(
        STATION, dt.date(2024, 9, 1), dt.date(2026, 9, 19), chunk_days=150)
    print(" ", out)
    for row in weather_archive.coverage():
        print(f"  {row['station']}: {row['n']:,} obs, "
              f"{row['wet_hours']:,} wet hours, {row['traces']:,} traces, "
              f"{dt.datetime.fromtimestamp(row['first_at'], dt.timezone.utc).date()}"
              f" .. "
              f"{dt.datetime.fromtimestamp(row['last_at'], dt.timezone.utc).date()}")


def _obs_by_hour():
    conn = sqlite3.connect(SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        return {ts: p for ts, p in conn.execute(
            "SELECT valid_at, precip_in FROM wx_observations "
            "WHERE station = ? AND precip_in IS NOT NULL", (STATION,))}
    finally:
        conn.close()


def _sum_between(obs, lo, hi):
    return sum(p for ts, p in obs.items() if lo <= ts < hi)


def validate():
    obs = _obs_by_hour()
    print(f"\n{len(obs):,} NYC hourly observations with a precipitation value")

    markets = [m for m in splits.load("markets", split="train+dev")
               if m.get("measure") == "precipitation_daily"
               and m.get("station_code") == "CLINYC"
               and m.get("result") in ("yes", "no")]
    print(f"{len(markets)} settled NYC daily-rain markets to check against\n")

    def close_epoch(m):
        return int(dt.datetime.fromisoformat(
            m["close_time"].replace("Z", "+00:00")).timestamp())

    windows = {
        "24h back from close": lambda c: (c - 86400, c),
        "UTC day before close": lambda c: (
            int(dt.datetime.fromtimestamp(c - 86400, dt.timezone.utc)
                .replace(hour=0, minute=0, second=0).timestamp()),
            int(dt.datetime.fromtimestamp(c - 86400, dt.timezone.utc)
                .replace(hour=0, minute=0, second=0).timestamp()) + 86400),
        "28h back from close": lambda c: (c - 28 * 3600, c),
    }

    print(f"{'window':<22} {'agree':>7} {'of':>5} {'accuracy':>9} "
          f"{'false yes':>10} {'false no':>9}")
    print("-" * 68)
    best = None
    for name, bounds in windows.items():
        agree = false_yes = false_no = 0
        for m in markets:
            lo, hi = bounds(close_epoch(m))
            total = _sum_between(obs, lo, hi)
            predicted = "yes" if total > 0 else "no"
            if predicted == m["result"]:
                agree += 1
            elif predicted == "yes":
                false_yes += 1
            else:
                false_no += 1
        acc = agree / len(markets) if markets else 0.0
        print(f"{name:<22} {agree:>7} {len(markets):>5} {acc:>8.1%} "
              f"{false_yes:>10} {false_no:>9}")
        if best is None or acc > best[1]:
            best = (name, acc, bounds)

    print(f"\nbest window: {best[0]} at {best[1]:.1%}")

    # Does trace do the work the trace test said it does?
    lo_hi = best[2]
    trace_only = wet_measurable = 0
    trace_yes = 0
    for m in markets:
        lo, hi = lo_hi(close_epoch(m))
        total = _sum_between(obs, lo, hi)
        if 0 < total < 0.01:
            trace_only += 1
            if m["result"] == "yes":
                trace_yes += 1
        elif total >= 0.01:
            wet_measurable += 1
    print(f"\ndays whose total is above zero but under 0.01in: {trace_only}")
    print(f"  of those, settled YES: {trace_yes}")
    print(f"days with 0.01in or more: {wet_measurable}")
    print("\nIf the first number is large and almost all of them settled YES,")
    print("the archive reproduces the rule that a model keyed to measurable")
    print("rain would get wrong on every one of those days.")


if __name__ == "__main__":
    if "--skip-backfill" not in sys.argv:
        backfill()
    validate()
