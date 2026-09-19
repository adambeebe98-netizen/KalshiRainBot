"""Backfill forecasts for every station, skipping those already covered."""
import datetime as dt
import sys

import forecast_archive
import weather_archive

START = dt.date(2024, 9, 1)
END = dt.date(2026, 9, 18)
WELL_COVERED = 40000

have = {}
for row in forecast_archive.coverage():
    have[row["station"]] = have.get(row["station"], 0) + row["n"]

stations = weather_archive.stations_in_use()
todo = [s for s in stations if have.get(s, 0) < WELL_COVERED]
print(f"{len(stations)} stations, {len(todo)} need forecasts", flush=True)

for i, station in enumerate(todo, 1):
    try:
        out = forecast_archive.backfill_station(
            station, START, END, chunk_days=30, sleep_between=2.0)
        print(f"[{i}/{len(todo)}] {station}: +{out.get('inserted', 0):,}", flush=True)
    except Exception as exc:
        print(f"[{i}/{len(todo)}] {station}: FAILED {type(exc).__name__}: {exc}",
              flush=True)
print("done", flush=True)
