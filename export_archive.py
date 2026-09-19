"""
Export old rows to date-partitioned files, then let the database forget.

The droplet is a 1 vCPU box with 14 GB free collecting ~311 MB a day. It
does not need to hold history; it needs to hold enough to operate. A
machine with a real disk and a GPU is where history belongs.

So this gives the SQLite tables the shape tick_archive already has:
one gzipped JSONL file per UTC day, append-only, rsync-friendly. Those
files sync anywhere. The droplet then keeps a rolling window and stops
growing, permanently, rather than buying another month with each prune.

The order is export, verify, THEN delete. A crash between the first two
leaves duplicate files, which is harmless. A crash the other way round
loses data that is not recoverable at any price -- a quote that was on
the book last Tuesday is gone.

Deletion is refused outright unless the export for that day verifies
row-for-row. There is no flag to skip the check.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sqlite3
from contextlib import closing

from config import SETTINGS

DEFAULT_DIR = "data/export"
DEFAULT_KEEP_DAYS = 14

# Tables worth exporting, with the column holding their unix timestamp.
EXPORTABLE = {
    "market_snapshots": "ts",
    "price_history": "ts",
    "forecast_history": "ts",
    "realtime_ticks": "received_ts",
    "wx_observations": "valid_at",
    "wx_forecasts": "valid_at",
}


def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def _day_bounds(day: str) -> tuple[int, int]:
    d = dt.date.fromisoformat(day)
    start = int(dt.datetime(d.year, d.month, d.day,
                            tzinfo=dt.timezone.utc).timestamp())
    return start, start + 86400


def path_for(table: str, day: str, directory: str = DEFAULT_DIR) -> str:
    return os.path.join(directory, table, f"{day}.jsonl.gz")


def days_with_rows(table: str, before_ts: int,
                   db_path: str | None = None) -> list[str]:
    col = EXPORTABLE[table]
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT date({col}, 'unixepoch') d FROM {table} "
            f"WHERE {col} < ? ORDER BY d", (before_ts,)).fetchall()
    return [r["d"] for r in rows if r["d"]]


def export_day(table: str, day: str, directory: str = DEFAULT_DIR,
               db_path: str | None = None) -> dict:
    """Write one table-day to a gzipped JSONL file and verify it reads
    back with the same row count."""
    col = EXPORTABLE[table]
    lo, hi = _day_bounds(day)
    with closing(_connect(db_path)) as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM {table} WHERE {col} >= ? AND {col} < ? "
            f"ORDER BY rowid", (lo, hi))]
    if not rows:
        return {"table": table, "day": day, "rows": 0, "verified": 0}

    out = path_for(table, day, directory)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".writing"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")

    verified = 0
    with gzip.open(tmp, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                json.loads(line)
                verified += 1
    if verified != len(rows):
        os.unlink(tmp)
        return {"table": table, "day": day, "rows": len(rows),
                "verified": verified, "error": "verification failed"}
    os.replace(tmp, out)
    return {"table": table, "day": day, "rows": len(rows),
            "verified": verified, "bytes": os.path.getsize(out)}


def prune_day(table: str, day: str, directory: str = DEFAULT_DIR,
              db_path: str | None = None) -> dict:
    """Delete a table-day from the database, but only after re-reading
    its export and confirming the row count matches.

    Re-verified here rather than trusting the export's own result: the
    two can be separated by a crash, a disk filling, or a partial rsync,
    and the whole point is that the delete is the last thing that happens.
    """
    col = EXPORTABLE[table]
    lo, hi = _day_bounds(day)
    path = path_for(table, day, directory)
    if not os.path.exists(path):
        return {"table": table, "day": day, "deleted": 0,
                "error": "no export on disk"}
    exported = 0
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                exported += 1
    with closing(_connect(db_path)) as conn:
        live = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} >= ? AND {col} < ?",
            (lo, hi)).fetchone()[0]
        if exported < live:
            return {"table": table, "day": day, "deleted": 0,
                    "error": f"export has {exported} rows, database has "
                             f"{live} -- refusing to delete"}
        conn.execute(f"DELETE FROM {table} WHERE {col} >= ? AND {col} < ?",
                     (lo, hi))
        conn.commit()
    return {"table": table, "day": day, "deleted": live, "exported": exported}


def run(keep_days: int = DEFAULT_KEEP_DAYS, directory: str = DEFAULT_DIR,
        tables=None, do_prune: bool = False, db_path: str | None = None):
    cutoff = int((dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=keep_days)).timestamp())
    results = []
    for table in (tables or EXPORTABLE):
        if table not in EXPORTABLE:
            # A table with no declared timestamp column cannot be
            # partitioned by day, and guessing one would be a good way to
            # export the wrong rows and then delete the right ones.
            results.append({"table": table, "day": "-", "rows": 0,
                            "error": "no timestamp column declared in "
                                     "EXPORTABLE"})
            continue
        try:
            days = days_with_rows(table, cutoff, db_path)
        except sqlite3.DatabaseError:
            continue
        for day in days:
            r = export_day(table, day, directory, db_path)
            if do_prune and not r.get("error") and r["rows"]:
                r.update(prune_day(table, day, directory, db_path))
            results.append(r)
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--table", action="append")
    p.add_argument("--prune", action="store_true",
                   help="delete exported days from the database")
    args = p.parse_args()

    print(f"exporting rows older than {args.keep_days} days to {args.dir}/")
    if args.prune:
        print("pruning after each verified export\n")
    results = run(args.keep_days, args.dir, args.table, args.prune)
    if not results:
        print("nothing old enough to export")
        return 0
    total_rows = total_bytes = total_deleted = 0
    for r in results:
        if r.get("error"):
            print(f"  {r['table']} {r['day']}: ERROR {r['error']}")
            continue
        if not r["rows"]:
            continue
        total_rows += r["rows"]
        total_bytes += r.get("bytes", 0)
        total_deleted += r.get("deleted", 0)
        print(f"  {r['table']:<20} {r['day']}  {r['rows']:>9,} rows  "
              f"{r.get('bytes', 0)/1048576:>7.1f} MB"
              + (f"  deleted {r['deleted']:,}" if r.get("deleted") else ""))
    print(f"\n{total_rows:,} rows exported, {total_bytes/1048576:.1f} MB")
    if total_deleted:
        print(f"{total_deleted:,} rows removed from the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
