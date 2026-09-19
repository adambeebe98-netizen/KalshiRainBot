"""
Point-in-time data access for the evaluation harness.

The safety property this module exists to provide: **a candidate cannot
see data that did not exist when it decided.** Not by convention, not by
reviewer diligence -- there is no accessor that returns a future row and
no accessor that returns the label at all.

Two things follow from that, and both are deliberate:

1. Every source declares how its availability is known (§3.2 of DESIGN.md).
   The archive has no receive time and never will, so "filter on
   received_at" is impossible there. Instead availability is a typed
   property: MEASURED where a row carries a real receipt timestamp,
   DECLARED where it is derived from event time plus a stated lag, and
   UNKNOWN where it cannot be established at all. Filtering is identical
   in every case; what differs is how loudly the result says what it rests
   on.

2. UNKNOWN sources are refused. `historical_weather_points` is the one
   that matters: its forecast series has no issue time and silently drops
   revisions, so a "forecast" read from it may be a nowcast -- which would
   leak the answer into a temperature market and produce a backtest that
   looks extraordinary and means nothing. Using it anyway requires an
   explicit flag plus a written reason, logged to `eval_unverified_access`
   with the same audit trail as a vault read.

This module never reaches the VAULT split. It does not import a path that
can set allow_vault=True, and a test asserts it.
"""
from __future__ import annotations

import datetime as dt
import inspect
import os
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum

from config import SETTINGS


class Availability(Enum):
    MEASURED = "measured"   # the row carries a real received_ts
    DECLARED = "declared"   # available_at = event_ts + a stated lag
    UNKNOWN = "unknown"     # availability cannot be established


class UnavailableSourceError(RuntimeError):
    """Raised when a candidate reaches for a source whose availability
    cannot be established, without explicitly accepting that."""


class SourceNotDeclaredError(RuntimeError):
    """Raised when a view is asked for a source it was not constructed
    with. Sources are declared up front so that what a candidate touched
    is a property of its configuration, not of which branch it took."""


@dataclass(frozen=True)
class Source:
    """How one table's availability is known."""
    name: str
    availability: Availability
    time_column: str           # the column availability is derived from
    lag_seconds: int = 0       # added to time_column for DECLARED sources
    rationale: str = ""

    def available_at_sql(self) -> str:
        if self.lag_seconds:
            return f"({self.time_column} + {self.lag_seconds})"
        return self.time_column


# Default candle publish lag is zero: `ts` is the candle's END timestamp,
# so the bar is complete at that instant. Parameterised because if Kalshi
# ever publishes on a delay, the correct response is to raise this number,
# not to discover it in a live drawdown.
DEFAULT_CANDLE_PUBLISH_LAG_S = 0

# Observation lag was measured at median 21 / p90 25 / max 66 minutes.
# Declared availability must be at least p90, so a DECLARED observation
# source is pessimistic rather than optimistic about when we knew a thing.
DEFAULT_OBSERVATION_LAG_S = 25 * 60


def default_sources(candle_publish_lag_s: int = DEFAULT_CANDLE_PUBLISH_LAG_S
                     ) -> dict[str, Source]:
    return {
        "realtime_ticks": Source(
            "realtime_ticks", Availability.MEASURED, "received_ts",
            rationale="the listener stamps receipt as the message arrives"),
        "realtime_weather_obs": Source(
            "realtime_weather_obs", Availability.MEASURED, "received_ts",
            rationale="the poller stamps receipt at fetch time"),
        "historical_price_points": Source(
            "historical_price_points", Availability.DECLARED, "ts",
            lag_seconds=candle_publish_lag_s,
            rationale="ts is the candle END, so the bar is complete then"),
        "market_snapshots": Source(
            "market_snapshots", Availability.DECLARED, "ts",
            rationale="our own write time is an upper bound on receipt"),
        "forecast_history": Source(
            "forecast_history", Availability.DECLARED, "ts",
            rationale="our own write time is an upper bound on receipt"),
        "historical_weather_points": Source(
            "historical_weather_points", Availability.UNKNOWN, "ts",
            rationale="no forecast issue time; revisions dropped by INSERT "
                      "OR IGNORE; provider differs from production. A row "
                      "here may be a nowcast wearing a forecast's clothes."),
    }


@dataclass(frozen=True)
class MarketTerms:
    """A market's *terms* -- everything public from the moment it opens.

    There is deliberately no `result` and no `expiration_value` field. The
    outcome is not something a candidate can forget to avoid looking at,
    because there is nothing to look at. See `label_for` for the harness's
    own scoring path, which is not reachable from a view.
    """
    ticker: str
    series_ticker: str | None
    event_ticker: str | None
    station_code: str | None
    measure: str | None
    threshold_low_f: float | None
    threshold_high_f: float | None
    threshold_description: str | None
    open_time: str | None
    close_time: str | None


def iso_to_epoch(iso: str | None) -> int | None:
    """Kalshi timestamps are ISO-8601, sometimes with fractional seconds
    and a trailing Z. Returns None rather than raising for unparseable
    input, because a market with no open_time should be excluded by the
    availability filter, not crash the run."""
    if not iso:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def _caller() -> str:
    """file:line of the first frame outside this module -- who asked."""
    for frame in inspect.stack()[1:]:
        if os.path.basename(frame.filename) != os.path.basename(__file__):
            return f"{os.path.basename(frame.filename)}:{frame.lineno}"
    return "unknown"


UNVERIFIED_ACCESS_DDL = """
CREATE TABLE IF NOT EXISTS eval_unverified_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    caller TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    as_of INTEGER NOT NULL,
    rows_returned INTEGER NOT NULL
)
"""


class PointInTimeView:
    """A read-only window onto the database as it stood at `as_of`.

    Every query is filtered so that only rows whose declared or measured
    availability is at or before `as_of` can come back. Sources must be
    declared when the view is built: what a candidate was allowed to touch
    is then a property of its configuration rather than of which code path
    happened to run.
    """

    def __init__(self, as_of: int, sources: list[str] | None = None,
                 db_path: str | None = None,
                 candle_publish_lag_s: int = DEFAULT_CANDLE_PUBLISH_LAG_S,
                 allow_unverified: bool = False, reason: str | None = None):
        if not isinstance(as_of, int):
            raise TypeError(f"as_of must be a unix int, got {type(as_of).__name__}")
        if allow_unverified and not (reason and reason.strip()):
            raise ValueError(
                "allow_unverified=True requires a written reason -- this is "
                "logged, and an unexplained one is not auditable")
        self.as_of = as_of
        self.allow_unverified = allow_unverified
        self.reason = reason
        self._db_path = db_path or SETTINGS.db_path
        self._catalog = default_sources(candle_publish_lag_s)
        declared = sources if sources is not None else [
            "historical_price_points", "market_snapshots", "realtime_ticks",
            "realtime_weather_obs", "forecast_history",
        ]
        unknown_names = [s for s in declared if s not in self._catalog]
        if unknown_names:
            raise SourceNotDeclaredError(
                f"no availability policy for: {unknown_names}. Add a Source "
                f"with an explicit policy rather than querying it untyped.")
        self.sources = list(declared)

    # -- plumbing ---------------------------------------------------------

    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _check(self, name: str) -> Source:
        if name not in self.sources:
            raise SourceNotDeclaredError(
                f"{name!r} was not declared when this view was built "
                f"(declared: {self.sources})")
        src = self._catalog[name]
        if src.availability is Availability.UNKNOWN and not self.allow_unverified:
            raise UnavailableSourceError(
                f"{name!r} has UNKNOWN availability and is refused by default.\n"
                f"  why: {src.rationale}\n"
                f"  to use it anyway, build the view with allow_unverified=True "
                f"and a written reason. That access is logged.")
        return src

    def _log_unverified(self, source: str, rows: int) -> None:
        with self._connect() as conn:
            conn.execute(UNVERIFIED_ACCESS_DDL)
            conn.execute(
                "INSERT INTO eval_unverified_access "
                "(ts, caller, reason, source, as_of, rows_returned) "
                "VALUES (?,?,?,?,?,?)",
                (int(time.time()), _caller(), self.reason, source,
                 self.as_of, rows))
            conn.commit()

    def _rows(self, name: str, where: str, params: tuple) -> list[dict]:
        src = self._check(name)
        sql = (f"SELECT * FROM {src.name} "
               f"WHERE {where} AND {src.available_at_sql()} <= ? "
               f"ORDER BY {src.time_column}")
        with self._connect() as conn:
            out = [dict(r) for r in conn.execute(sql, params + (self.as_of,))]
        if src.availability is Availability.UNKNOWN:
            self._log_unverified(name, len(out))
        return out

    # -- accessors --------------------------------------------------------

    def price_points(self, ticker: str) -> list[dict]:
        return self._rows("historical_price_points", "ticker = ?", (ticker,))

    def snapshots(self, ticker: str) -> list[dict]:
        return self._rows("market_snapshots", "ticker = ?", (ticker,))

    def forecasts(self, ticker: str) -> list[dict]:
        return self._rows("forecast_history", "ticker = ?", (ticker,))

    def ticks(self, ticker: str) -> list[dict]:
        return self._rows("realtime_ticks", "ticker = ?", (ticker,))

    def weather_obs(self, station_code: str) -> list[dict]:
        return self._rows("realtime_weather_obs", "station_code = ?", (station_code,))

    def weather_points(self, station_code: str) -> list[dict]:
        """UNKNOWN availability -- refused unless the view was built with
        allow_unverified and a reason. See §3.3 of DESIGN.md."""
        return self._rows("historical_weather_points", "station_code = ?",
                           (station_code,))

    def market(self, ticker: str) -> MarketTerms | None:
        """A market's terms, if they were public by `as_of`.

        Availability is `open_time`: the terms are knowable from the moment
        the market opens. Returns None for a market that had not opened yet,
        which is the correct answer rather than an error -- a candidate
        scanning for tradeable markets should simply not see it.
        """
        self._check_market_source()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ticker, series_ticker, event_ticker, station_code, "
                "measure, threshold_low_f, threshold_high_f, "
                "threshold_description, open_time, close_time "
                "FROM historical_markets WHERE ticker = ?", (ticker,)).fetchone()
        if row is None:
            return None
        opened = iso_to_epoch(row["open_time"])
        if opened is None or opened > self.as_of:
            return None
        return MarketTerms(**dict(row))

    def open_markets(self, measure: str | None = None) -> list[MarketTerms]:
        """Every market open at `as_of` -- opened at or before, not yet
        closed. This is what a candidate scans."""
        self._check_market_source()
        sql = ("SELECT ticker, series_ticker, event_ticker, station_code, "
               "measure, threshold_low_f, threshold_high_f, "
               "threshold_description, open_time, close_time "
               "FROM historical_markets")
        params: tuple = ()
        if measure is not None:
            sql += " WHERE measure = ?"
            params = (measure,)
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
        out = []
        for r in rows:
            opened = iso_to_epoch(r["open_time"])
            closed = iso_to_epoch(r["close_time"])
            if opened is None or opened > self.as_of:
                continue
            if closed is not None and closed <= self.as_of:
                continue
            out.append(MarketTerms(**r))
        return out

    def _check_market_source(self) -> None:
        # historical_markets metadata is DECLARED-available at open_time and
        # is always permitted: a market's terms are public by definition.
        # Its result and expiration_value columns are simply never selected.
        return None

    def __repr__(self) -> str:
        stamp = dt.datetime.fromtimestamp(self.as_of, dt.timezone.utc).isoformat()
        return (f"PointInTimeView(as_of={self.as_of} [{stamp}], "
                f"sources={self.sources}, unverified={self.allow_unverified})")


# --------------------------------------------------------------------------
# Labels -- NOT reachable from a view
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Label:
    ticker: str
    result: str                 # 'yes' or 'no'
    expiration_value: float | None
    available_at: int           # close_time; when the outcome became known


def label_for(ticker: str, db_path: str | None = None) -> Label | None:
    """The settled outcome of a market. **For the harness's scoring path
    only.** It is a module-level function rather than a view method
    precisely so that a candidate holding a PointInTimeView has no route
    to it -- `view.label(...)` raises AttributeError because there is no
    such attribute, not because something checks for it.

    `available_at` is close_time: the outcome is knowable then and not
    before. folds.py uses that to purge training rows whose label period
    overlaps a test window.

    Note for whoever writes the label function for a strategy: the label
    is `result`, which is defined by the settlement product. It is NOT a
    physical threshold that looks equivalent. Trace precipitation settles
    YES on the rain markets, so P(contract YES) is 44.0% where
    P(measurable rain) is 36.9% -- see analysis/trace_test.py.
    """
    path = db_path or SETTINGS.db_path
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT ticker, result, expiration_value, close_time "
            "FROM historical_markets WHERE ticker = ?", (ticker,)).fetchone()
    finally:
        conn.close()
    if row is None or row["result"] not in ("yes", "no"):
        # 40 archive markets resolve 'scalar' rather than yes/no. Excluded
        # explicitly here rather than silently coerced somewhere downstream.
        return None
    closed = iso_to_epoch(row["close_time"])
    if closed is None:
        return None
    return Label(ticker=row["ticker"], result=row["result"],
                 expiration_value=row["expiration_value"], available_at=closed)
