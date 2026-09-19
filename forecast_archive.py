"""
Forecasts as they were issued, with the lead time preserved.

This is the piece DESIGN.md section 3.3 said the project did not have, and
without which the harness cannot honestly evaluate a weather feature on
the archive: the existing forecast series has no issue time, drops
revisions, and is plausibly a nowcast -- a "forecast" that already knows
the answer produces a backtest that looks extraordinary and means nothing.

Source is Open-Meteo's **Previous Runs** API, which archives each model at
a fixed lead-time offset: `precipitation_previous_day1` is what was
predicted 24 hours before the valid hour, `_day2` 48 hours before, and so
on to seven days.

NOT the Historical Forecast API. That one stitches each run's first few
hours into a continuous series, which is an analysis in forecast clothing
and is exactly the leak being guarded against. It is also the endpoint
anyone would reach for first, and plausibly what produced the existing
untimestamped archive. Verified 2026-03 at Central Park: the 24h-lead
series differs from the analysis on genuinely wet hours -- actual 0.40mm
against a forecast of 0.00, actual 1.50 against 0.70 -- which a nowcast
would not.

`available_at` is therefore not a declared guess. A `previous_day1` value
was, by the API's own definition, available 24 hours before its valid
hour, so availability is arithmetic rather than assumption.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import sqlite3
import time

import httpx

import weather_archive
from config import SETTINGS

PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
IEM_ASOS = weather_archive.IEM_ASOS
USER_AGENT = weather_archive.USER_AGENT

# Lead times worth storing. These markets carry no price beyond ~36h
# before close, so a 5-day forecast has nothing to trade against; 24h and
# 48h bracket the whole tradeable window, and 72h is kept as a cheap
# measure of how fast skill decays.
DEFAULT_LEADS_H = (24, 48, 72)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,
    valid_at INTEGER NOT NULL,       -- the hour being forecast
    lead_hours INTEGER NOT NULL,     -- how far ahead it was issued
    available_at INTEGER NOT NULL,   -- valid_at - lead, by definition
    source TEXT NOT NULL,
    precip_mm REAL,
    precip_prob_pct REAL,
    temp_c REAL,
    UNIQUE(station, valid_at, lead_hours, source)
);
CREATE INDEX IF NOT EXISTS idx_wx_fc_station_valid
    ON wx_forecasts(station, valid_at, lead_hours);
CREATE INDEX IF NOT EXISTS idx_wx_fc_available ON wx_forecasts(available_at);

CREATE TABLE IF NOT EXISTS wx_stations (
    station TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    fetched_ts INTEGER NOT NULL
);
"""


def init(db_path: str | None = None) -> None:
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _get(url, params, timeout=180.0):
    """Shared fetch with backoff, for 429s and for read timeouts.

    Timeouts are retried rather than fatal because a wide date range
    across three lead times is a genuinely large query, and a backfill
    that dies two thirds of the way through leaves a partial archive
    that looks complete.
    """
    delay = 5.0
    last_exc = None
    for _ in range(5):
        try:
            resp = httpx.get(url, params=params,
                             headers={"User-Agent": USER_AGENT}, timeout=timeout)
        except httpx.TimeoutException as exc:
            last_exc = exc
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code != 429:
            return resp
        time.sleep(delay)
        delay *= 2
    if last_exc is not None:
        raise last_exc
    return resp


def fetch_station_coordinates(station: str) -> tuple[float, float] | None:
    """Coordinates for an ASOS station, from IEM itself.

    Taken from the same service that supplies the observations rather than
    from a hand-typed table, so the forecast is pulled for the point the
    gauge actually sits at. Given this project exists because of a few
    miles between a house and an airport, that is not a detail.
    """
    sid = weather_archive.asos_id(station)
    today = dt.date.today()
    resp = _get(IEM_ASOS, {
        "station": sid, "data": "tmpf", "latlon": "yes",
        "year1": today.year, "month1": today.month, "day1": max(1, today.day - 2),
        "year2": today.year, "month2": today.month, "day2": today.day,
        "tz": "Etc/UTC", "format": "onlycomma", "missing": "empty",
        "report_type": "3"})
    if resp.status_code != 200:
        return None
    for row in csv.DictReader(io.StringIO(resp.text)):
        try:
            return float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def station_coordinates(station: str, db_path: str | None = None
                        ) -> tuple[float, float] | None:
    """Cached coordinates; fetches once per station."""
    init(db_path)
    sid = weather_archive.asos_id(station)
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        row = conn.execute("SELECT lat, lon FROM wx_stations WHERE station = ?",
                           (sid,)).fetchone()
        if row:
            return float(row[0]), float(row[1])
    finally:
        conn.close()
    coords = fetch_station_coordinates(sid)
    if coords is None:
        return None
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("INSERT OR REPLACE INTO wx_stations "
                     "(station, lat, lon, fetched_ts) VALUES (?,?,?,?)",
                     (sid, coords[0], coords[1], int(time.time())))
        conn.commit()
    finally:
        conn.close()
    return coords


def _parse_hour(value: str) -> int | None:
    try:
        return int(dt.datetime.fromisoformat(value)
                   .replace(tzinfo=dt.timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


def fetch_forecasts(station: str, lat: float, lon: float,
                    start: dt.date, end: dt.date,
                    leads_h=DEFAULT_LEADS_H) -> list[dict]:
    """Hourly forecasts at each lead time for one station over a range."""
    variables = []
    for lead in leads_h:
        day = lead // 24
        variables += [f"precipitation_previous_day{day}",
                      f"precipitation_probability_previous_day{day}",
                      f"temperature_2m_previous_day{day}"]
    resp = _get(PREVIOUS_RUNS, {
        "latitude": lat, "longitude": lon, "hourly": ",".join(variables),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "timezone": "UTC"})
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError as exc:
        # Open-Meteo answers overload with an HTML error page and a 200,
        # so raise_for_status does not catch it. Raising with a snippet
        # beats a bare JSONDecodeError three stations into a backfill.
        raise RuntimeError(
            f"non-JSON response from {PREVIOUS_RUNS} "
            f"(status {resp.status_code}): {resp.text[:160]!r}") from exc
    if "error" in body:
        raise RuntimeError(f"API error: {body.get('reason', body['error'])}")
    hourly = body.get("hourly", {})
    times = hourly.get("time", [])
    sid = weather_archive.asos_id(station)
    out = []
    for lead in leads_h:
        day = lead // 24
        precip = hourly.get(f"precipitation_previous_day{day}") or []
        prob = hourly.get(f"precipitation_probability_previous_day{day}") or []
        temp = hourly.get(f"temperature_2m_previous_day{day}") or []
        for i, stamp in enumerate(times):
            valid_at = _parse_hour(stamp)
            if valid_at is None:
                continue
            p = precip[i] if i < len(precip) else None
            pp = prob[i] if i < len(prob) else None
            t = temp[i] if i < len(temp) else None
            if p is None and pp is None and t is None:
                continue
            out.append({
                "station": sid, "valid_at": valid_at, "lead_hours": lead,
                "available_at": valid_at - lead * 3600,
                "precip_mm": p, "precip_prob_pct": pp, "temp_c": t,
            })
    return out


def store_forecasts(rows: list[dict], source: str = "openmeteo_previous_runs",
                    db_path: str | None = None) -> int:
    if not rows:
        return 0
    init(db_path)
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        before = conn.execute("SELECT COUNT(*) FROM wx_forecasts").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO wx_forecasts "
            "(station, valid_at, lead_hours, available_at, source, "
            "precip_mm, precip_prob_pct, temp_c) VALUES (?,?,?,?,?,?,?,?)",
            [(r["station"], r["valid_at"], r["lead_hours"], r["available_at"],
              source, r["precip_mm"], r["precip_prob_pct"], r["temp_c"])
             for r in rows])
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM wx_forecasts").fetchone()[0]
    finally:
        conn.close()
    return after - before


def backfill_station(station: str, start: dt.date, end: dt.date,
                     chunk_days: int = 60, leads_h=DEFAULT_LEADS_H,
                     db_path: str | None = None,
                     sleep_between: float = 2.0) -> dict:
    coords = station_coordinates(station, db_path=db_path)
    if coords is None:
        return {"station": weather_archive.asos_id(station),
                "error": "no coordinates", "inserted": 0}
    lat, lon = coords
    inserted = fetched = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=chunk_days), end)
        rows = fetch_forecasts(station, lat, lon, cursor, chunk_end, leads_h)
        fetched += len(rows)
        inserted += store_forecasts(rows, db_path=db_path)
        cursor = chunk_end + dt.timedelta(days=1)
        if cursor <= end and sleep_between:
            time.sleep(sleep_between)
    return {"station": weather_archive.asos_id(station), "lat": lat, "lon": lon,
            "fetched": fetched, "inserted": inserted}


def coverage(db_path: str | None = None) -> list[dict]:
    init(db_path)
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT station, lead_hours, COUNT(*) n, "
            "MIN(valid_at) first_at, MAX(valid_at) last_at, "
            "SUM(precip_mm IS NOT NULL) with_precip, "
            "SUM(precip_mm > 0) wet_hours "
            "FROM wx_forecasts GROUP BY station, lead_hours "
            "ORDER BY station, lead_hours")]
    finally:
        conn.close()
