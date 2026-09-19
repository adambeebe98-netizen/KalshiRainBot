"""
Database maintenance: keep the live file small enough to keep working.

Measured 2026-09-19, once real-time collection was actually running:
~770,000 ticks/day at ~544 bytes of raw_json each, plus ~122,000
decisions/day, into a 594 MB SQLite file with 14 GB of disk free. About
460 MB/day, so roughly four weeks before the box filled. The earlier
"50 MB/day, months not weeks" estimate was measured while the collector
was broken and only capturing a 23-minute window.

Three jobs here, in the order they should run:

1. `archive_legacy_raw_json` -- a one-time migration. Rows written before
   tick_archive existed still carry the full message. It copies them into
   the archive first, THEN replaces the column with a pointer. Lossless,
   and in that order: a crash between the two leaves duplicate archive
   records, which is harmless, rather than deleted messages, which is not.
2. `prune_decisions` -- deletes old *skipped* decisions. Every 'traded'
   row is kept forever regardless of age: there are only 120 of them and
   they are the record of what the bot actually did. The sole reader is
   the dashboard's 24-hour summary, so a 7-day window is already generous.
3. `checkpoint_wal` -- folds the write-ahead log back into the database.

Deliberately NOT here: VACUUM. It needs a full second copy of the file
and an exclusive lock, which on a database three live services are
writing to is a way to cause an outage while tidying up. Freed pages are
reused by SQLite anyway, so the file stops growing rather than shrinking,
which is the part that matters.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time

import tick_archive
from config import SETTINGS

DEFAULT_DECISION_KEEP_DAYS = 7
ARCHIVE_POINTER_PREFIX = "@"


def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def archive_legacy_raw_json(db_path: str | None = None, batch_size: int = 2000,
                            archive_dir: str = tick_archive.DEFAULT_DIR,
                            max_batches: int | None = None) -> dict:
    """Move pre-archive raw messages out of the database, losslessly.

    Works in batches and commits each one, so it can be interrupted and
    resumed: rows already converted carry a pointer and are not selected
    again.
    """
    moved = 0
    batches = 0
    archive = tick_archive.TickArchive(directory=archive_dir,
                                       flush_every=batch_size)
    conn = _connect(db_path)
    try:
        while max_batches is None or batches < max_batches:
            rows = conn.execute(
                "SELECT id, received_ts, raw_json FROM realtime_ticks "
                "WHERE raw_json NOT LIKE ? ORDER BY id LIMIT ?",
                (f"{ARCHIVE_POINTER_PREFIX}%", batch_size)).fetchall()
            if not rows:
                break
            for row in rows:
                archive.append(row["received_ts"], row["raw_json"])
            # Archive first, then point the rows at it. The reverse order
            # would lose messages if this died in between.
            archive.flush()
            conn.executemany(
                "UPDATE realtime_ticks SET raw_json = ? WHERE id = ?",
                [(f"{ARCHIVE_POINTER_PREFIX}{tick_archive.day_key(r['received_ts'])}",
                  r["id"]) for r in rows])
            conn.commit()
            moved += len(rows)
            batches += 1
    finally:
        archive.close()
        conn.close()
    return {"rows_moved": moved, "batches": batches}


def prune_decisions(keep_days: int = DEFAULT_DECISION_KEEP_DAYS,
                    db_path: str | None = None) -> dict:
    """Delete skipped decisions older than `keep_days`. Never deletes a
    traded decision, at any age."""
    if keep_days < 1:
        raise ValueError("keep_days must be at least 1 -- refusing to delete "
                         "today's decisions")
    cutoff = int(time.time()) - keep_days * 86400
    conn = _connect(db_path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        traded_before = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE action = 'traded'").fetchone()[0]
        conn.execute("DELETE FROM decisions WHERE action != 'traded' AND ts < ?",
                     (cutoff,))
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        traded_after = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE action = 'traded'").fetchone()[0]
    finally:
        conn.close()
    if traded_after != traded_before:
        raise RuntimeError(
            f"prune deleted traded decisions ({traded_before} -> "
            f"{traded_after}); this should be impossible")
    return {"deleted": before - after, "remaining": after,
            "traded_kept": traded_after, "cutoff": cutoff}


def refresh_observations(days_back: int = 3, db_path: str | None = None) -> dict:
    """Top up observed weather from IEM for every station in use.

    This is what keeps the precipitation gap closed going forward. The
    live NWS poller cannot do it -- `precipitationLastHour` is absent from
    that API's payload for these stations, which is why `precip_last_hour_mm`
    is NULL on every row it has ever written. Backfilling history without
    this would have fixed the past and left the present broken.

    A rolling few days rather than only yesterday, because ASOS
    observations are occasionally corrected after the fact and INSERT OR
    IGNORE makes re-fetching free.
    """
    import datetime as dt
    import weather_archive

    # Tomorrow, not today. IEM's day range excludes its end date, so
    # asking for [today-3, today] silently stops at yesterday and the
    # archive sits permanently a day behind -- which is invisible unless
    # someone checks how stale the newest row is.
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=days_back + 1)
    inserted = 0
    failures = []
    for station in weather_archive.stations_in_use(db_path):
        try:
            out = weather_archive.backfill_station(
                station, start, end, db_path=db_path, sleep_between=1.0)
            inserted += out["inserted"]
        except Exception as exc:
            failures.append(f"{station}: {type(exc).__name__}")
    return {"inserted": inserted, "days": days_back, "failures": failures}


def checkpoint_wal(db_path: str | None = None) -> dict:
    conn = _connect(db_path)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    return {"busy": row[0], "log_pages": row[1], "checkpointed_pages": row[2]}


def report(db_path: str | None = None,
           archive_dir: str = tick_archive.DEFAULT_DIR) -> str:
    path = db_path or SETTINGS.db_path
    lines = []
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            lines.append(f"  {os.path.basename(p)}: "
                         f"{os.path.getsize(p) / 1048576:.1f} MB")
    conn = _connect(db_path)
    try:
        for table in ("realtime_ticks", "decisions", "price_history",
                      "market_snapshots", "forecast_history"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                lines.append(f"  {table}: {n:,} rows")
            except sqlite3.DatabaseError:
                pass
        legacy = conn.execute(
            "SELECT COUNT(*) FROM realtime_ticks WHERE raw_json NOT LIKE ?",
            (f"{ARCHIVE_POINTER_PREFIX}%",)).fetchone()[0]
        lines.append(f"  realtime_ticks still holding full messages: {legacy:,}")
    finally:
        conn.close()
    s = tick_archive.stats(archive_dir)
    lines.append(f"  tick archive: {s['days']} days, "
                 f"{s['bytes'] / 1048576:.1f} MB, {s['first']} .. {s['last']}")
    return "database:\n" + "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-legacy", action="store_true",
                        help="move pre-archive raw messages out of the DB")
    parser.add_argument("--prune-decisions", action="store_true")
    parser.add_argument("--keep-days", type=int, default=DEFAULT_DECISION_KEEP_DAYS)
    parser.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--refresh-observations", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    print(report())
    if args.refresh_observations or args.all:
        print("\ntopping up observed weather from IEM...")
        print(" ", refresh_observations())
    if args.archive_legacy or args.all:
        print("\narchiving legacy raw messages...")
        print(" ", archive_legacy_raw_json())
    if args.prune_decisions or args.all:
        print(f"\npruning skipped decisions older than {args.keep_days} days...")
        print(" ", prune_decisions(args.keep_days))
    if args.checkpoint or args.all:
        print("\ncheckpointing WAL...")
        print(" ", checkpoint_wal())
    if any((args.archive_legacy, args.prune_decisions, args.checkpoint, args.all)):
        print()
        print(report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
