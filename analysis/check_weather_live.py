"""Has weather ground truth stopped being written?

The live audit reported ZERO wx_observations rows in the last hour while
kalshi-realtime-weather shows active. A service that is up and not
writing is the worst failure mode there is: nothing alerts, and the gap
only turns up when someone tries to train on the period.

Weather observations are the ORIGINAL third leg of this project -- the
IEM measurement that says what actually happened at the gauge the
contract settles on, which is the whole reason the Austin loss is
understood at all. If that has been silent, it matters more than any of
the sports work.

Establish: when did it last write, how much, and is the gap a break or
just a slow cadence?
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import time
from contextlib import closing

from config import SETTINGS


def as_epoch(value):
    """These tables do not agree on a timestamp type.

    wx_observations stores unix seconds; realtime_weather_obs stores an
    ISO string. Assuming one shape crashed the first version of this
    script -- and would have quietly mis-sorted rather than crashed if
    the types had been merely different integers.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def ago(ts):
    ts = as_epoch(ts)
    if not ts:
        return "never"
    mins = (time.time() - ts) / 60
    if mins < 90:
        return f"{mins:.0f} min ago"
    return f"{mins/60:.1f} h ago"


def stamp(ts):
    ts = as_epoch(ts)
    return (dt.datetime.fromtimestamp(ts, dt.timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC") if ts else "-")


with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    conn.row_factory = sqlite3.Row
    # Parenthesised: without them the OR escapes the type filter and
    # the query returns indexes, which then fail to COUNT(*).
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND (name LIKE 'wx%' OR name LIKE '%weather%')")]
    print(f"weather-related tables: {tables}\n")

    for table in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        tcol = next((c for c in ("valid_at", "ts", "observed_ts",
                                 "received_ts", "fetched_at")
                     if c in cols), None)
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if not tcol:
            print(f"{table:<26} {total:>9,} rows   (no timestamp column)")
            continue
        newest = conn.execute(
            f"SELECT MAX({tcol}) FROM {table}").fetchone()[0]
        # Compare in whichever type the column actually holds, rather
        # than forcing epochs onto an ISO string column.
        if isinstance(newest, str):
            cutoff_1h = dt.datetime.fromtimestamp(
                time.time() - 3600, dt.timezone.utc).isoformat()
            cutoff_1d = dt.datetime.fromtimestamp(
                time.time() - 86400, dt.timezone.utc).isoformat()
        else:
            cutoff_1h = int(time.time()) - 3600
            cutoff_1d = int(time.time()) - 86400
        last_hour = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {tcol} > ?",
            (cutoff_1h,)).fetchone()[0]
        last_day = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {tcol} > ?",
            (cutoff_1d,)).fetchone()[0]
        print(f"{table:<26} {total:>9,} rows | newest {stamp(newest)} "
              f"({ago(newest)}) | 1h {last_hour:>6,} | 24h {last_day:>7,}")

    print("\nrows per day, wx_observations, last 10 days:")
    try:
        for r in conn.execute(
                "SELECT date(valid_at,'unixepoch') d, COUNT(*) n, "
                "COUNT(DISTINCT station_code) s FROM wx_observations "
                "GROUP BY d ORDER BY d DESC LIMIT 10"):
            print(f"  {r['d']}  {r['n']:>7,} rows  {r['s']:>4} stations")
    except sqlite3.Error as exc:
        print(f"  {exc}")

    print("\nrows per day, wx_forecasts, last 5 days:")
    try:
        for r in conn.execute(
                "SELECT date(valid_at,'unixepoch') d, COUNT(*) n "
                "FROM wx_forecasts GROUP BY d ORDER BY d DESC LIMIT 5"):
            print(f"  {r['d']}  {r['n']:>7,} rows")
    except sqlite3.Error as exc:
        print(f"  {exc}")
