"""Backfill observed weather for every station our markets reference.

Skips stations already well covered, so a re-run after a rate-limit
failure fetches only what is missing rather than hammering IEM for data
we already hold.
"""
import datetime as dt
import sys

import weather_archive

START = dt.date(2024, 9, 1)
END = dt.date.today()
WELL_COVERED = 15000

have = {r["station"]: r["n"] for r in weather_archive.coverage()}
stations = weather_archive.stations_in_use()
todo = [s for s in stations if have.get(s, 0) < WELL_COVERED]
print(f"{len(stations)} stations, {len(todo)} need backfill: {', '.join(todo)}\n")

total = 0
for i, station in enumerate(todo, 1):
    try:
        out = weather_archive.backfill_station(
            station, START, END, chunk_days=400, sleep_between=5.0)
        total += out["inserted"]
        print(f"[{i}/{len(todo)}] {station}: +{out['inserted']:,}")
    except Exception as exc:
        print(f"[{i}/{len(todo)}] {station}: FAILED {type(exc).__name__}: {exc}")
    sys.stdout.flush()

print(f"\ninserted {total:,}")
print(f"{'station':<8} {'obs':>8} {'wet':>7} {'trace':>7}  range")
for row in weather_archive.coverage():
    first = dt.datetime.fromtimestamp(row["first_at"], dt.timezone.utc).date()
    last = dt.datetime.fromtimestamp(row["last_at"], dt.timezone.utc).date()
    print(f"{row['station']:<8} {row['n']:>8,} {row['wet_hours']:>7,} "
          f"{row['traces']:>7,}  {first} .. {last}")
