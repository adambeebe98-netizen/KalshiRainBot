"""
Observed weather from the instrument the contracts actually settle on.

The gap this closes: NWS's own API returns null for precipitation at
these stations -- `precipitationLastHour` is absent from the payload
entirely, and the raw METAR fallback carries a P-group in 0 of 72
observations at KMDW. So the live poller has recorded 0 precipitation
readings out of 302 observations. For rain markets, the largest family by
tick volume, we have the forecast and the price and no actual.

IEM's ASOS archive has it: 432 of 432 rows carry `p01i` for NYC over the
same period. Same instrument, same airport, real hourly values.

**Trace is encoded as 0.0001 inches.** That is not a rounding artifact,
it is IEM's deliberate representation of a METAR "T", and it is the
single most important property of this source for this project. The
contracts settle on "strictly greater than 0 inches of precipitation" and
trace settles YES -- verified across 478 markets in
analysis/trace_test.py. So `p01i > 0` in this archive maps exactly onto
the contract's YES condition, which no other source we have reproduces.

Deliberately NOT Open-Meteo for observations. Its precipitation is a
model grid cell a few kilometres across; the contract settles on one
bucket at one airport. Substituting the grid for the gauge is the
house-versus-airport mistake that started this project, rebuilt in
software. Open-Meteo has a role for *forecasts* (see DESIGN.md 14.3,
Single Runs only), not for what actually fell.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import sqlite3
import time

import httpx

from config import SETTINGS

IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
USER_AGENT = "kalshi-weather-bot (research; contact via GitHub adambeebe98-netizen)"

# IEM's encoding of a METAR trace report. Exact equality is correct here:
# it is a sentinel IEM writes, not a measurement that might round to it.
TRACE_INCHES = 0.0001

# ASOS transmits on the hour (:51 past, typically) and IEM ingests within
# minutes, but the honest number for a backtest is the pessimistic one.
# Matches the p90 observation lag measured on the live NWS poller.
DEFAULT_AVAILABILITY_LAG_S = 25 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT NOT NULL,          -- ASOS identifier, e.g. 'NYC'
    valid_at INTEGER NOT NULL,      -- the moment observed, unix seconds UTC
    available_at INTEGER NOT NULL,  -- when we could first have known it
    source TEXT NOT NULL,
    temp_f REAL,
    precip_in REAL,                 -- 0.0001 is TRACE, see is_trace
    is_trace INTEGER NOT NULL DEFAULT 0,
    UNIQUE(station, valid_at, source)
);
CREATE INDEX IF NOT EXISTS idx_wx_obs_station_valid
    ON wx_observations(station, valid_at);
CREATE INDEX IF NOT EXISTS idx_wx_obs_available
    ON wx_observations(available_at);
"""


def init(db_path: str | None = None) -> None:
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


class UnknownStationError(KeyError):
    """A station code with no reviewed mapping. Raised rather than guessed."""


# Explicit, reviewed, one line per code. NOT derived by stripping "CLI",
# which is what the first version did and which silently fetched 16,787
# rows of Hobbs, New Mexico weather for Houston markets: CLIHOB's markets
# are KXHIGHHOU, but HOB is Lea County Regional. Wrong-station data is
# worse than missing data, because nothing downstream can tell.
#
# Codes absent here raise. A guess that happens to hit a real ASOS id
# somewhere else in the country is precisely the failure being guarded
# against, so silence is the wrong default.
STATION_MAP = {
    "CLIATL": "ATL",   # Atlanta Hartsfield
    "CLIAUS": "AUS",   # Austin-Bergstrom -- the airport behind this project
    "CLIBOS": "BOS",   # Boston Logan
    "CLIDAL": "DAL",   # Dallas Love Field
    "CLIDCA": "DCA",   # Washington National
    "CLIDEN": "DEN",   # Denver International
    "CLIDFW": "DFW",   # Dallas/Fort Worth
    "CLIHOB": "HOU",   # Houston Hobby -- NOT Hobbs NM
    "CLIHOU": "HOU",   # Houston Hobby
    "CLIIAH": "IAH",   # Houston Intercontinental
    "CLILAS": "LAS",   # Las Vegas Harry Reid
    "CLILAX": "LAX",   # Los Angeles International
    "CLIMDW": "MDW",   # Chicago Midway
    "CLIMIA": "MIA",   # Miami International
    "CLIMSP": "MSP",   # Minneapolis-St Paul
    "CLIMSY": "MSY",   # New Orleans Louis Armstrong
    "CLINOL": "MSY",   # New Orleans, same station
    "CLINYC": "NYC",   # Central Park
    "CLIOKC": "OKC",   # Oklahoma City Will Rogers
    "CLIORD": "ORD",   # Chicago O'Hare
    "CLIPHL": "PHL",   # Philadelphia International
    "CLIPHO": "PHX",   # Phoenix, same station
    "CLIPHX": "PHX",   # Phoenix Sky Harbor
    "CLISAT": "SAT",   # San Antonio International
    "CLISEA": "SEA",   # Seattle-Tacoma
    "CLISFO": "SFO",   # San Francisco International
    # CLINEW is deliberately absent: one market, and it is not clear
    # whether it means Newark or New Orleans Lakefront. Guessing between
    # two real airports is exactly the CLIHOB mistake.
}


def asos_id(station_code: str, strict: bool = True) -> str:
    """Map a market's station code to an ASOS identifier.

    Rain markets carry CLI product codes because they settle on the daily
    climate report; temperature markets sometimes carry a raw ICAO. Only
    the ICAO form is derived, because dropping a leading K is unambiguous.
    Everything else must be in STATION_MAP.
    """
    code = (station_code or "").strip().upper()
    if not code:
        if strict:
            raise UnknownStationError("empty station code")
        return ""
    if code in STATION_MAP:
        return STATION_MAP[code]
    if len(code) == 4 and code.startswith("K"):
        return code[1:]
    if len(code) == 3 and code not in STATION_MAP.values():
        # A bare three-letter id that is already an ASOS id.
        return code
    if len(code) == 3:
        return code
    if strict:
        raise UnknownStationError(
            f"no reviewed mapping for station code {code!r}. Add it to "
            f"STATION_MAP after checking which airport it means -- a guess "
            f"that lands on a real station elsewhere is undetectable "
            f"downstream.")
    return ""


def _parse_ts(value: str) -> int | None:
    try:
        return int(dt.datetime.strptime(value, "%Y-%m-%d %H:%M")
                   .replace(tzinfo=dt.timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


def _parse_float(value: str) -> float | None:
    v = (value or "").strip()
    if v in ("", "M", "T"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def fetch_observations(station: str, start: dt.date, end: dt.date,
                       timeout: float = 120.0) -> list[dict]:
    """Hourly observations for one station over [start, end].

    `trace=0.0001` is passed explicitly rather than relying on the
    default, because the whole value of this source here is that a trace
    report survives as something distinguishable from zero.
    """
    params = {
        "station": asos_id(station), "data": ["tmpf", "p01i"],
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
        "missing": "empty", "trace": str(TRACE_INCHES), "direct": "no",
        "report_type": "3",
    }
    # IEM rate-limits, and returns 429 rather than queueing. Backing off
    # is not politeness theatre: a naive loop over 27 stations tripped it
    # on 9 of them, and a half-filled archive that looks complete is a
    # data-quality problem rather than a transient one.
    delay = 5.0
    for attempt in range(5):
        resp = httpx.get(IEM_ASOS, params=params,
                         headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if resp.status_code != 429:
            break
        time.sleep(delay)
        delay *= 2
    resp.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        valid_at = _parse_ts(row.get("valid", ""))
        if valid_at is None:
            continue
        precip = _parse_float(row.get("p01i", ""))
        out.append({
            "station": asos_id(station),
            "valid_at": valid_at,
            "temp_f": _parse_float(row.get("tmpf", "")),
            "precip_in": precip,
            "is_trace": 1 if precip is not None and precip == TRACE_INCHES else 0,
        })
    return out


def store_observations(rows: list[dict], source: str = "iem_asos",
                       availability_lag_s: int = DEFAULT_AVAILABILITY_LAG_S,
                       db_path: str | None = None) -> int:
    """Insert observations, ignoring ones already held.

    INSERT OR IGNORE against UNIQUE(station, valid_at, source): an
    observation of a given hour is a fact that does not get revised, so a
    repeat really is a repeat. Forecasts are a different matter entirely
    and do not belong in this table.
    """
    if not rows:
        return 0
    init(db_path)
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        before = conn.execute("SELECT COUNT(*) FROM wx_observations").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO wx_observations "
            "(station, valid_at, available_at, source, temp_f, precip_in, is_trace) "
            "VALUES (?,?,?,?,?,?,?)",
            [(r["station"], r["valid_at"], r["valid_at"] + availability_lag_s,
              source, r["temp_f"], r["precip_in"], r["is_trace"]) for r in rows])
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM wx_observations").fetchone()[0]
    finally:
        conn.close()
    return after - before


def backfill_station(station: str, start: dt.date, end: dt.date,
                     chunk_days: int = 120,
                     db_path: str | None = None,
                     sleep_between: float = 1.0) -> dict:
    """Backfill one station in chunks, politely.

    IEM is a free public service run by a university. Chunking and
    sleeping is not defensive coding, it is not being a bad citizen with
    someone else's infrastructure.
    """
    inserted = fetched = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=chunk_days), end)
        rows = fetch_observations(station, cursor, chunk_end)
        fetched += len(rows)
        inserted += store_observations(rows, db_path=db_path)
        cursor = chunk_end + dt.timedelta(days=1)
        if cursor <= end and sleep_between:
            time.sleep(sleep_between)
    return {"station": asos_id(station), "fetched": fetched,
            "inserted": inserted}


def stations_in_use(db_path: str | None = None) -> list[str]:
    """Every distinct ASOS id referenced by a market we have.

    Derived from the markets rather than hard-coded, so a new city's
    series is picked up by re-running the backfill instead of by
    remembering to edit a list.
    """
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT station_code FROM historical_markets "
            "WHERE station_code IS NOT NULL")]
    finally:
        conn.close()
    seen, out, unmapped = set(), [], []
    for code in sorted(codes):
        try:
            sid = asos_id(code)
        except UnknownStationError:
            unmapped.append(code)
            continue
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    if unmapped:
        # Surfaced rather than swallowed: an unmapped code means some
        # markets have no observed weather, and that should be a visible
        # gap rather than a silent one.
        print(f"weather_archive: {len(unmapped)} unmapped station code(s), "
              f"skipped: {', '.join(unmapped)}")
    return out


def coverage(db_path: str | None = None) -> list[dict]:
    init(db_path)
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT station, COUNT(*) n, "
            "MIN(valid_at) first_at, MAX(valid_at) last_at, "
            "SUM(precip_in IS NOT NULL) with_precip, "
            "SUM(is_trace) traces, "
            "SUM(precip_in > 0) wet_hours "
            "FROM wx_observations GROUP BY station ORDER BY station")]
    finally:
        conn.close()
