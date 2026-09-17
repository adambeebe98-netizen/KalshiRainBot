"""
Everything the bot does gets logged here. If you can't reconstruct exactly
why a trade happened six weeks later, you can't tell whether the strategy
is working or you're fooling yourself with a few lucky weeks.
"""
from __future__ import annotations

import sqlite3
import time
import logging
from datetime import datetime, timezone
from contextlib import contextmanager

from config import SETTINGS
from categories import category_for, MIN_SAMPLE_SIZE

log = logging.getLogger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    market_price_cents INTEGER,
    model_probability REAL,
    edge_cents INTEGER,
    action TEXT NOT NULL,       -- 'traded' | 'skipped'
    reason TEXT,
    mode TEXT NOT NULL          -- 'paper' | 'live'
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    mode TEXT NOT NULL,
    order_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'won' | 'lost'
    settled_ts INTEGER,
    pnl_cents INTEGER,
    model_probability REAL,      -- the (pre-calibration-adjusted) probability behind this trade
    station_code TEXT,           -- settlement station, for calibration lookups
    measure TEXT,                -- 'precipitation_daily' etc, for calibration lookups
    bot_version TEXT             -- the short git commit hash the bot was running at decision
                                   -- time (see bot.get_git_commit and shadow_trades' matching
                                   -- column for the full reasoning)
);

CREATE TABLE IF NOT EXISTS bankroll_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    bankroll_cents INTEGER NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS calibration_stats (
    station_code TEXT NOT NULL,
    measure TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    sum_predicted REAL NOT NULL DEFAULT 0,
    sum_actual REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (station_code, measure)
);

-- Every named strategy (see strategies.py / shadow.py) runs entirely in
-- paper simulation, each with its own bankroll, logged here separately
-- from the real 'trades' table so they never mix with real money.
CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,          -- 'yes' | 'no' | 'both' (arbitrage buys both)
    count INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'won' | 'lost' | 'sold'
    settled_ts INTEGER,
    pnl_cents INTEGER,
    model_probability REAL,
    station_code TEXT,
    measure TEXT,
    exit_target_cents INTEGER,   -- swing strategy only: sell early once this price is reached
    rationale TEXT,              -- the human-readable reasoning generated at decision time
                                  -- (forecast/observation data used, calibration note, etc.) —
                                  -- without this, a post-mortem analysis of WHY a trade lost only
                                  -- has raw numbers to work with, losing the actual reasoning
                                  -- context that makes root-cause analysis useful. Only populated
                                  -- for trades logged after this field was added; older rows are
                                  -- NULL, not an error.
    confidence TEXT,              -- rules_extractor.MarketRules.confidence ('high'|'medium'|'low')
                                   -- at decision time — lets analytics actually check whether
                                   -- confidence level predicts real outcomes, instead of just
                                   -- assuming the sizing multiplier it already drives is correct.
    event_ticker TEXT,            -- Kalshi's own event grouping (e.g. every bucket/threshold
                                   -- market for one city+day shares one event_ticker) — lets a
                                   -- strategy check "do I already hold a position on this same
                                   -- underlying event" before adding another. CONFIRMED BUG this
                                   -- exists to fix: favorites_baseline bought YES on four
                                   -- mutually-conflicting bucket markets for the same underlying
                                   -- temperature event simultaneously (its rule — buy anything
                                   -- priced >=95c — had no concept of "already exposed to this
                                   -- event"), losing all four (~4410c combined, confirmed live).
    market_implied_probability REAL,  -- the market's own implied probability of the traded
                                        -- side at decision time (yes_price/100, side-adjusted
                                        -- same as model_probability) — was already computed for
                                        -- every trade, just never stored. Directly comparable
                                        -- against model_probability to see the real, realized gap
                                        -- our edge claims were based on, not just the edge_cents
                                        -- number derived from it.
    raw_model_probability REAL,   -- model_probability_yes BEFORE calibration's bias correction —
                                    -- NULL for has_real_signal=False cases (no real estimate to
                                    -- calibrate) and non-model strategies (favorites,
                                    -- depth_imbalance, always_trade). Stored alongside the
                                    -- calibrated value so calibration's real, measured effect is
                                    -- directly queryable instead of reconstructed from rationale
                                    -- text.
    hours_until_close_at_decision REAL,  -- how long until this market's close, at the moment
                                           -- this specific trade was decided — lets analysis
                                           -- check whether trades made close to settlement really
                                           -- do perform differently, not just the
                                           -- settlement_window strategies that deliberately gate
                                           -- on it.
    performance_dampening_multiplier REAL,   -- the actual multiplier applied by each dampening
    calibration_dampening_multiplier REAL,    -- layer at decision time (1.0 = no dampening).
    concentration_dampening_multiplier REAL,  -- Structured columns, not just the rationale text
    self_concentration_dampening_multiplier REAL,  -- notes these already produce — makes "does
                                                     -- ROI differ when calibration dampening
                                                     -- kicked in" a plain GROUP BY instead of a
                                                     -- fragile LIKE query against free text.
    bot_version TEXT,              -- the short git commit hash the bot was running at decision
                                     -- time (see bot.get_git_commit) — lets a later review answer
                                     -- "was fix X actually live when this trade happened" from the
                                     -- trade data itself, instead of having to separately ask
                                     -- whether a pull+restart happened at the right time. Real
                                     -- motivation: a favorites_baseline loss that looked identical
                                     -- to an already-fixed bug, with no way to tell from the trade
                                     -- alone whether the fix was live yet.
    observed_temp_f REAL,          -- the RAW ground-truth weather inputs the model actually
    forecast_temp_f REAL,           -- consumed to produce its probability, captured directly
    precip_pop_pct REAL,            -- instead of only surviving as a human-readable trace in
    observed_precip_mm REAL,        -- rationale text. threshold_low_f/high_f is the actual
    threshold_low_f REAL,           -- market threshold this specific trade was evaluated against
    threshold_high_f REAL,          -- (e.g. "84F", "[85,95]") — each field stays NULL where it
                                     -- genuinely doesn't apply to the market type (rain fields for
                                     -- a temperature trade and vice versa) rather than a
                                     -- placeholder value; NULL here means "not applicable," not
                                     -- "data was lost." Explicitly requested: "data is the
                                     -- absolute most important thing to log and store."
    fee_cents_paid INTEGER,         -- the actual per-order fee (see fees.taker_fee_cents) —
                                     -- already computed internally for every profitability check,
                                     -- but never previously stored per-trade, so decomposing "how
                                     -- much of my apparent edge did fees actually eat" required
                                     -- recomputing it by hand.
    yes_bid_depth_total INTEGER,    -- total resting order-book depth on each side at decision
    no_bid_depth_total INTEGER,     -- time, when real book data was available that cycle — the
                                     -- exact numbers behind depth_imbalance's decisions (and any
                                     -- other strategy that had book data available), previously
                                     -- only visible embedded in that one strategy's rationale text
                                     -- ("yes_bid_depth=X no_bid_depth=Y"), not as its own queryable
                                     -- column, and not captured at all for any other strategy.
    -- bracket_arbitrage only: the payout is mathematically fixed the moment
    -- the trade is placed (see shadow.py) — settlement just needs to know
    -- WHEN the event resolved, not WHICH bracket won, so this stores the
    -- known payout up front and `sample_member_ticker` gives settlement.py
    -- one real market ticker from the group to poll (the synthetic event
    -- ticker in the `ticker` column above isn't itself a tradeable market).
    precomputed_payout_cents INTEGER,
    sample_member_ticker TEXT
);

CREATE TABLE IF NOT EXISTS shadow_bankroll_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    bankroll_cents INTEGER NOT NULL
);

-- Logged every scan cycle for EVERY open market being watched, regardless
-- of whether any strategy trades it. This is the raw material for
-- eventually characterizing real intraday price movement per city/market
-- type — you can't design a swing-trading entry/exit rule responsibly
-- without first knowing how these contracts actually move during a day.
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    yes_ask INTEGER,
    yes_bid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_price_history_ticker_ts ON price_history(ticker, ts);

-- Same idea as price_history, but for the NWS forecast temperature used by
-- the temperature model (see strategy.pick_relevant_forecast_temp_f) rather
-- than the market's own quoted price. Lets temp_forecast_momentum detect
-- "the forecast just moved" as a distinct signal from "the market price
-- moved" — logged once per scanned temperature market per cycle, same
-- cadence as price_history.
CREATE TABLE IF NOT EXISTS forecast_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    forecast_temp_f REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_history_ticker_ts ON forecast_history(ticker, ts);

-- Foundation for retrospective backtesting/pattern-mining, explicitly
-- requested: collect data across every scanned market — regardless of
-- whether any strategy chose to trade it — then later analyze it to find
-- where a profitable trade existed that current strategies missed. This
-- is fundamentally different from shadow_trades: that table only has a
-- row when SOME strategy decided to act; this one has a row every cycle
-- for EVERY market scanned, win or lose, traded or not, which is what
-- "did we miss an opportunity" actually requires being able to ask.
-- One row per (ticker, cycle) — NOT deduplicated or aggregated, so the
-- full price/weather trajectory over a market's life is reconstructable,
-- not just its final state.
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT,
    station_code TEXT,
    measure TEXT,
    yes_ask INTEGER,
    yes_bid INTEGER,
    no_ask INTEGER,
    no_bid INTEGER,
    observed_temp_f REAL,
    forecast_temp_f REAL,
    precip_pop_pct REAL,
    observed_precip_mm REAL,
    threshold_low_f REAL,
    threshold_high_f REAL,
    hours_until_close REAL,
    model_probability_yes REAL,  -- what the CURRENT calibrated model says — captured so a later
                                   -- backtest can distinguish "the model already saw this and
                                   -- correctly abstained" from "no strategy was even looking here"
    close_time TEXT              -- Kalshi's own ISO close timestamp, needed by the outcome
                                   -- backfill below to know when a market is even eligible to
                                   -- have settled yet, without re-fetching market metadata
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_ticker_ts ON market_snapshots(ticker, ts);

-- The actual settlement result for every ticker ever snapshotted above,
-- once known — deliberately separate from market_snapshots (many
-- snapshot rows per ticker over its life, but settlement happens exactly
-- once), and deliberately separate from shadow_trades/trades (this
-- covers markets NO strategy ever touched, which is the entire point).
-- Backfilled by settlement.backfill_market_outcomes — see its docstring
-- for why this runs on its own schedule rather than every cycle.
CREATE TABLE IF NOT EXISTS market_outcomes (
    ticker TEXT PRIMARY KEY,
    result TEXT NOT NULL,        -- 'yes' or 'no'
    settled_ts INTEGER NOT NULL
);

-- Historical backfill (see historical_backfill.py) — reconstructs the
-- same price+weather+outcome picture as market_snapshots/market_outcomes
-- above, but for YEARS of past markets already settled, instead of
-- waiting for it to accumulate going forward. Explicitly requested: "if
-- we go back, extract all of that available data that is applicable to
-- the trades." Kept structurally SEPARATE from market_snapshots (not
-- merged into the same table) because the data provenance genuinely
-- differs: live snapshots use api.weather.gov and Kalshi's live API;
-- this uses Kalshi's /historical/ endpoints and Open-Meteo (see
-- historical_weather.py's docstring for why NWS itself has no way to
-- answer "what was forecast on a past date," and for the real caveat
-- that Open-Meteo's reconstruction is a different, related model blend
-- from NWS's own official forecast, not an exact reproduction of it).
-- Blurring that distinction later would make it impossible to tell which
-- rows are the ground truth the live bot actually saw versus a
-- best-available historical approximation.
CREATE TABLE IF NOT EXISTS historical_markets (
    ticker TEXT PRIMARY KEY,
    series_ticker TEXT,
    event_ticker TEXT,
    station_code TEXT,
    measure TEXT,
    threshold_low_f REAL,
    threshold_high_f REAL,
    open_time TEXT,
    close_time TEXT,
    result TEXT,              -- 'yes' or 'no', once known
    settlement_source TEXT,   -- e.g. 'NWS', 'The Weather Company' — extracted all along but discarded before
    threshold_description TEXT,  -- plain-language threshold, e.g. 'strictly greater than 96F'
    confidence TEXT,          -- rules_extractor's own confidence in this extraction: 'high'|'medium'|'low'
    backfilled_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_historical_markets_station ON historical_markets(station_code, measure);

-- Real price history per historical market, from Kalshi's own
-- candlesticks — one row per candlestick interval, not deduplicated, so
-- the full price trajectory over a settled market's life is
-- reconstructable, mirroring market_snapshots' same design choice for
-- live data.
CREATE TABLE IF NOT EXISTS historical_price_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts INTEGER NOT NULL,       -- candlestick end timestamp, Unix seconds
    yes_price_cents INTEGER,   -- candlestick close price
    volume INTEGER
);
CREATE INDEX IF NOT EXISTS idx_historical_price_points_ticker_ts ON historical_price_points(ticker, ts);

-- Reconstructed historical weather, keyed by STATION and hour rather
-- than by individual market — many bracket/threshold markets for the
-- same city and day share one station, and fetching per-market would
-- mean fetching (and storing) the identical weather data many times
-- over. Joined against historical_markets by station_code at analysis
-- time instead.
CREATE TABLE IF NOT EXISTS historical_weather_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_code TEXT NOT NULL,
    ts INTEGER NOT NULL,               -- Unix seconds, the hour this point represents
    forecast_temp_f REAL,
    forecast_precip_pop_pct REAL,
    observed_temp_f REAL,
    observed_precip_mm REAL
);
CREATE INDEX IF NOT EXISTS idx_historical_weather_points_station_ts ON historical_weather_points(station_code, ts);

-- Human-approved overrides to the heuristic strategies' guessed thresholds
-- (swing/favorites/longshot — never the calibrated model or arbitrage).
-- Only ever written by the dashboard's "Apply" button (see web_ui/app.py) —
-- advisor.py can only ever WRITE a row to `suggestions`, never here directly.
CREATE TABLE IF NOT EXISTS strategy_overrides (
    strategy TEXT NOT NULL,
    param TEXT NOT NULL,
    value REAL NOT NULL,
    applied_ts INTEGER NOT NULL,
    PRIMARY KEY (strategy, param)
);

-- Suggestions generated by advisor.py's periodic Claude review. Purely
-- informational until a human clicks Apply on the dashboard, which is the
-- only thing that ever copies a row from here into strategy_overrides.
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    param TEXT NOT NULL,
    current_value REAL NOT NULL,
    suggested_value REAL NOT NULL,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'pending'  -- 'pending' | 'applied' | 'dismissed'
);

-- Claude's periodic qualitative review of WHY trades are winning/losing —
-- see retrospective.py. Deliberately PROSE ONLY, no structured
-- strategy/param/value fields the way `suggestions` has: this is
-- diagnostic reading material for a human, not something any code path
-- could ever apply automatically even by mistake. That's a stronger
-- safety property than advisor.py's suggestions table has, on purpose —
-- open-ended pattern-finding across many trades is a much easier place
-- for a model to be confidently wrong than "is this one number too high."
CREATE TABLE IF NOT EXISTS retrospectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    analysis_text TEXT NOT NULL,
    trades_analyzed INTEGER NOT NULL,
    wins_analyzed INTEGER NOT NULL,
    losses_analyzed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _migrate_add_columns(conn) -> None:
    """
    Best-effort migration for anyone who ran an earlier version of this bot
    before the calibration columns existed. Adding a column that already
    exists raises 'duplicate column' — that's fine, ignore it.
    """
    for stmt in (
        "ALTER TABLE trades ADD COLUMN model_probability REAL",
        "ALTER TABLE trades ADD COLUMN station_code TEXT",
        "ALTER TABLE trades ADD COLUMN measure TEXT",
        "ALTER TABLE shadow_trades ADD COLUMN exit_target_cents INTEGER",
        "ALTER TABLE shadow_trades ADD COLUMN precomputed_payout_cents INTEGER",
        "ALTER TABLE shadow_trades ADD COLUMN sample_member_ticker TEXT",
        "ALTER TABLE shadow_trades ADD COLUMN rationale TEXT",
        "ALTER TABLE shadow_trades ADD COLUMN confidence TEXT",
        "ALTER TABLE shadow_trades ADD COLUMN event_ticker TEXT",
        "ALTER TABLE shadow_trades ADD COLUMN market_implied_probability REAL",
        "ALTER TABLE shadow_trades ADD COLUMN raw_model_probability REAL",
        "ALTER TABLE shadow_trades ADD COLUMN hours_until_close_at_decision REAL",
        "ALTER TABLE shadow_trades ADD COLUMN performance_dampening_multiplier REAL",
        "ALTER TABLE shadow_trades ADD COLUMN calibration_dampening_multiplier REAL",
        "ALTER TABLE shadow_trades ADD COLUMN concentration_dampening_multiplier REAL",
        "ALTER TABLE shadow_trades ADD COLUMN self_concentration_dampening_multiplier REAL",
        "ALTER TABLE shadow_trades ADD COLUMN bot_version TEXT",
        "ALTER TABLE trades ADD COLUMN bot_version TEXT",
        "ALTER TABLE shadow_trades ADD COLUMN observed_temp_f REAL",
        "ALTER TABLE shadow_trades ADD COLUMN forecast_temp_f REAL",
        "ALTER TABLE shadow_trades ADD COLUMN precip_pop_pct REAL",
        "ALTER TABLE shadow_trades ADD COLUMN observed_precip_mm REAL",
        "ALTER TABLE shadow_trades ADD COLUMN threshold_low_f REAL",
        "ALTER TABLE shadow_trades ADD COLUMN threshold_high_f REAL",
        "ALTER TABLE shadow_trades ADD COLUMN fee_cents_paid INTEGER",
        "ALTER TABLE shadow_trades ADD COLUMN yes_bid_depth_total INTEGER",
        "ALTER TABLE shadow_trades ADD COLUMN no_bid_depth_total INTEGER",
        "ALTER TABLE trades ADD COLUMN observed_temp_f REAL",
        "ALTER TABLE trades ADD COLUMN forecast_temp_f REAL",
        "ALTER TABLE trades ADD COLUMN precip_pop_pct REAL",
        "ALTER TABLE trades ADD COLUMN observed_precip_mm REAL",
        "ALTER TABLE trades ADD COLUMN threshold_low_f REAL",
        "ALTER TABLE trades ADD COLUMN threshold_high_f REAL",
        "ALTER TABLE trades ADD COLUMN fee_cents_paid INTEGER",
        # historical_markets: settlement_source, threshold_description,
        # and confidence were extracted by rules_extractor all along but
        # discarded rather than stored -- added per explicit request,
        # going forward only (not backfilled onto already-processed
        # markets, which the person explicitly said not to bother with).
        "ALTER TABLE historical_markets ADD COLUMN settlement_source TEXT",
        "ALTER TABLE historical_markets ADD COLUMN threshold_description TEXT",
        "ALTER TABLE historical_markets ADD COLUMN confidence TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass



@contextmanager
def get_conn():
    conn = sqlite3.connect(SETTINGS.db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_add_unique_constraints(conn) -> None:
    """
    Upgrades historical_price_points and historical_weather_points from
    a plain (non-unique) index to a real UNIQUE constraint on their
    natural key -- CONFIRMED-NEEDED: unlike historical_markets (which
    has always had a true PRIMARY KEY on ticker), these two only ever
    had id INTEGER PRIMARY KEY AUTOINCREMENT as their key, with the
    (ticker, ts) / (station_code, ts) index existing for query speed
    only, not uniqueness -- meaning nothing in the database itself ever
    prevented a market or station+hour from being inserted twice.
    Reprocessing the same ticker before the application-level "already
    stored, skip" guard existed (which happened tonight, across several
    restarts) could have inserted duplicate rows with no error at all.

    Making this a real UNIQUE index, and switching both save_* functions
    to INSERT OR IGNORE, moves this guarantee to the database itself:
    correct even if a future code change ever reintroduces a
    reprocessing bug, not dependent on that one Python-level check
    always being present and correct.

    Deliberately best-effort and non-fatal: if duplicate rows already
    exist on some pre-existing database when this runs, building a
    UNIQUE index over them fails with an IntegrityError -- caught and
    logged here rather than raised, since this must never be able to
    break init_db() (which the live trading bot calls on every startup).
    A database in that state keeps working on the old, non-unique index;
    the caller sees a clear log line naming which table still has
    duplicates left to clean up before this protection can take effect.
    """
    for old_index_name, table, key_cols in (
        ("idx_historical_price_points_ticker_ts", "historical_price_points", "ticker, ts"),
        ("idx_historical_weather_points_station_ts", "historical_weather_points", "station_code, ts"),
    ):
        try:
            conn.execute(f"DROP INDEX IF EXISTS {old_index_name}")
            conn.execute(f"CREATE UNIQUE INDEX {old_index_name} ON {table}({key_cols})")
        except sqlite3.IntegrityError:
            log.warning(f"{table} already has duplicate ({key_cols}) rows — "
                        f"could not add a UNIQUE constraint. Falling back to a "
                        f"plain (non-unique) index so queries stay fast; "
                        f"existing duplicates should be cleaned up manually.")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {old_index_name} ON {table}({key_cols})")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_add_columns(conn)
        _migrate_add_unique_constraints(conn)


def log_decision(ticker: str, side: str, market_price_cents: int, model_probability: float,
                  edge_cents: int, action: str, reason: str, mode: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO decisions (ts, ticker, side, market_price_cents, model_probability, "
            "edge_cents, action, reason, mode) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(time.time()), ticker, side, market_price_cents, model_probability,
             edge_cents, action, reason, mode),
        )


def log_trade(ticker: str, side: str, count: int, price_cents: int, mode: str,
              order_id: str | None, model_probability: float | None = None,
              station_code: str | None = None, measure: str | None = None,
              bot_version: str | None = None,
              observed_temp_f: float | None = None,
              forecast_temp_f: float | None = None,
              precip_pop_pct: float | None = None,
              observed_precip_mm: float | None = None,
              threshold_low_f: float | None = None,
              threshold_high_f: float | None = None,
              fee_cents_paid: int | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trades (ts, ticker, side, count, price_cents, mode, order_id, "
            "model_probability, station_code, measure, bot_version, observed_temp_f, "
            "forecast_temp_f, precip_pop_pct, observed_precip_mm, threshold_low_f, "
            "threshold_high_f, fee_cents_paid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), ticker, side, count, price_cents, mode, order_id,
             model_probability, station_code, measure, bot_version, observed_temp_f,
             forecast_temp_f, precip_pop_pct, observed_precip_mm, threshold_low_f,
             threshold_high_f, fee_cents_paid),
        )
        return cur.lastrowid


def get_open_trades(mode: str | None = None) -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if mode:
            rows = cur.execute("SELECT * FROM trades WHERE status='open' AND mode=?", (mode,)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM trades WHERE status='open'").fetchall()
        return [dict(r) for r in rows]


def settle_trade(trade_id: int, won: bool, pnl_cents: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE trades SET status=?, settled_ts=?, pnl_cents=? WHERE id=?",
            ("won" if won else "lost", int(time.time()), pnl_cents, trade_id),
        )


def snapshot_bankroll(bankroll_cents: int, note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bankroll_snapshots (ts, bankroll_cents, note) VALUES (?,?,?)",
            (int(time.time()), bankroll_cents, note),
        )


def load_last_bankroll(default_cents: int) -> int:
    """
    So a restart (crash, reboot, deploy) doesn't silently reset your
    bankroll back to STARTING_BANKROLL_CENTS and lose track of real P&L.
    Falls back to the configured starting value only if this is a fresh DB.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT bankroll_cents FROM bankroll_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else default_cents


def record_calibration_outcome(station_code: str, measure: str,
                                predicted_probability: float, actual_outcome: bool) -> None:
    """
    Rolling record of (what we predicted) vs (what actually happened),
    bucketed by station+measure. This is the entire "learning" mechanism —
    no black box, just running sums you can inspect directly in the DB.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO calibration_stats (station_code, measure, n, sum_predicted, sum_actual)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(station_code, measure) DO UPDATE SET
                n = n + 1,
                sum_predicted = sum_predicted + excluded.sum_predicted,
                sum_actual = sum_actual + excluded.sum_actual
            """,
            (station_code, measure, predicted_probability, 1.0 if actual_outcome else 0.0),
        )


def get_calibration_stats(station_code: str, measure: str) -> tuple[int, float, float]:
    """Returns (n, avg_predicted, avg_actual) for this station+measure, or (0, 0, 0)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT n, sum_predicted, sum_actual FROM calibration_stats "
            "WHERE station_code=? AND measure=?",
            (station_code, measure),
        ).fetchone()
        if not row or row[0] == 0:
            return 0, 0.0, 0.0
        n, sum_pred, sum_actual = row
        return n, sum_pred / n, sum_actual / n


# ---------- shadow strategies (all paper, never real money) ----------

def log_shadow_trade(strategy: str, ticker: str, side: str, count: int, price_cents: int,
                      model_probability: float | None = None, station_code: str | None = None,
                      measure: str | None = None, exit_target_cents: int | None = None,
                      precomputed_payout_cents: int | None = None,
                      sample_member_ticker: str | None = None,
                      rationale: str | None = None,
                      confidence: str | None = None,
                      event_ticker: str | None = None,
                      market_implied_probability: float | None = None,
                      raw_model_probability: float | None = None,
                      hours_until_close_at_decision: float | None = None,
                      performance_dampening_multiplier: float | None = None,
                      calibration_dampening_multiplier: float | None = None,
                      concentration_dampening_multiplier: float | None = None,
                      self_concentration_dampening_multiplier: float | None = None,
                      bot_version: str | None = None,
                      observed_temp_f: float | None = None,
                      forecast_temp_f: float | None = None,
                      precip_pop_pct: float | None = None,
                      observed_precip_mm: float | None = None,
                      threshold_low_f: float | None = None,
                      threshold_high_f: float | None = None,
                      fee_cents_paid: int | None = None,
                      yes_bid_depth_total: int | None = None,
                      no_bid_depth_total: int | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO shadow_trades (ts, strategy, ticker, side, count, price_cents, "
            "model_probability, station_code, measure, exit_target_cents, "
            "precomputed_payout_cents, sample_member_ticker, rationale, confidence, event_ticker, "
            "market_implied_probability, raw_model_probability, hours_until_close_at_decision, "
            "performance_dampening_multiplier, calibration_dampening_multiplier, "
            "concentration_dampening_multiplier, self_concentration_dampening_multiplier, "
            "bot_version, observed_temp_f, forecast_temp_f, "
            "precip_pop_pct, observed_precip_mm, threshold_low_f, threshold_high_f, "
            "fee_cents_paid, yes_bid_depth_total, no_bid_depth_total) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), strategy, ticker, side, count, price_cents,
             model_probability, station_code, measure, exit_target_cents,
             precomputed_payout_cents, sample_member_ticker, rationale, confidence, event_ticker,
             market_implied_probability, raw_model_probability, hours_until_close_at_decision,
             performance_dampening_multiplier, calibration_dampening_multiplier,
             concentration_dampening_multiplier, self_concentration_dampening_multiplier,
             bot_version, observed_temp_f, forecast_temp_f,
             precip_pop_pct, observed_precip_mm, threshold_low_f, threshold_high_f,
             fee_cents_paid, yes_bid_depth_total, no_bid_depth_total),
        )
        return cur.lastrowid


def has_open_position_for_event(strategy: str, event_ticker: str) -> bool:
    """
    Does this strategy already hold an open position somewhere in this
    same Kalshi event (e.g. any bucket/threshold market for one city+day
    shares one event_ticker)? Used by favorites_baseline specifically —
    see log_shadow_trade's event_ticker column docstring for the
    confirmed bug this exists to prevent: buying multiple
    mutually-conflicting positions on the same underlying outcome isn't
    diversification, it's the same bet placed several times with extra
    steps. Returns False (fails open) if event_ticker is empty — no
    event to check against.
    """
    if not event_ticker:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM shadow_trades WHERE strategy=? AND event_ticker=? AND status='open' LIMIT 1",
            (strategy, event_ticker),
        ).fetchone()
        return row is not None


def get_open_shadow_trades(strategy: str | None = None) -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if strategy:
            rows = cur.execute("SELECT * FROM shadow_trades WHERE status='open' AND strategy=?", (strategy,)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM shadow_trades WHERE status='open'").fetchall()
        return [dict(r) for r in rows]


def settle_shadow_trade(trade_id: int, won: bool, pnl_cents: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE shadow_trades SET status=?, settled_ts=?, pnl_cents=? WHERE id=?",
            ("won" if won else "lost", int(time.time()), pnl_cents, trade_id),
        )


def close_shadow_trade_sold(trade_id: int, pnl_cents: int) -> None:
    """Swing strategy only: position closed by selling before resolution,
    not by the market settling. Counted as a 'win' in summaries when profitable."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE shadow_trades SET status='sold', settled_ts=?, pnl_cents=? WHERE id=?",
            (int(time.time()), pnl_cents, trade_id),
        )


def get_todays_realized_pnl_cents(strategy: str) -> int:
    """
    Reconstructs "how much has this shadow strategy already won/lost
    today" from shadow_trades' own settled_ts/pnl_cents columns — no new
    schema needed, since that data was already being recorded.

    This matters because RiskState.realized_pnl_today_cents (the number
    the daily kill switch actually checks) otherwise starts at 0 on every
    process restart, REGARDLESS of what already happened earlier that same
    calendar day. Confirmed: the bot restarted many times today alone —
    every one of those restarts would have silently reset any tripped
    kill switch back to "fresh," completely defeating the daily loss
    limit's actual purpose. Called once at engine-creation time
    (get_engines()) to seed the real value instead of assuming zero.

    Assumes the server's local timezone matches SQLite's UTC-based date()
    — true on a standard UTC-configured droplet (confirmed via tonight's
    own systemctl output), but would silently drift out of sync with
    Python's date.today() (used for the day-rollover check) if that ever
    changed. Worth revisiting if the server's timezone is ever reconfigured.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT SUM(pnl_cents) FROM shadow_trades WHERE strategy=? AND status IN ('won','lost','sold') "
            "AND settled_ts IS NOT NULL AND date(settled_ts, 'unixepoch') = date('now')",
            (strategy,),
        ).fetchone()
        return row[0] or 0


def get_todays_realized_pnl_cents_main() -> int:
    """Same idea as get_todays_realized_pnl_cents(), for the main bot's own
    real (or paper-mode) trades table instead of a specific shadow strategy."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT SUM(pnl_cents) FROM trades WHERE status IN ('won','lost') "
            "AND settled_ts IS NOT NULL AND date(settled_ts, 'unixepoch') = date('now')"
        ).fetchone()
        return row[0] or 0


def get_open_shadow_position_count(strategy: str) -> int:
    """
    Real count of this strategy's currently-open positions, straight from
    the database — the source of truth get_engines() should seed
    RiskState.open_positions_count from, instead of the dataclass default
    of 0.

    CONFIRMED, SEVERE bug this fixes: open_positions_count is an in-memory
    counter that only stayed correct as long as one continuous process
    ran — every restart reset it to 0 regardless of how many real open
    positions already existed, which means max_open_positions (the cap
    approve_trade actually checks) never meaningfully bound in practice
    on a bot restarted as often as this one has been during active
    development. Confirmed directly: strategies showing 350-440+ "Active"
    positions on the dashboard, far beyond any reasonable per-strategy cap,
    with total deployed capital several times the paper bankroll — exactly
    what you'd expect if the cap reset to "room for N more" on every
    restart instead of reflecting what was actually already open.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM shadow_trades WHERE strategy=? AND status='open'",
            (strategy,),
        ).fetchone()
        return row[0] or 0


def get_open_position_count_main() -> int:
    """Same idea as get_open_shadow_position_count(), for the main bot's
    own real trades table."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()
        return row[0] or 0


def get_trades_for_retrospective(hours: int = 168, limit: int = 250) -> list[dict]:
    """
    Recent SETTLED shadow trades (both wins AND losses — a balanced sample,
    not just failures, so the model has contrast to reason from rather than
    only ever seeing one side of the picture) with their full context:
    strategy, market, side/price/count, real pnl, the model's stated
    probability at decision time, and — critically — the rationale text
    logged then (see shadow.py's log_shadow_trade calls). Trades from
    before rationale existed have NULL there; still included, just with
    less context for the model to work with on those specific rows.

    bracket_arbitrage is deliberately included despite being disabled for
    live trading — its own broken pattern (0% win rate) is exactly the
    kind of thing this kind of review should be able to surface and name,
    not hide from the model doing the reviewing.
    """
    cutoff = int(time.time()) - hours * 3600
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT strategy, ticker, side, count, price_cents, status, pnl_cents, "
            "model_probability, station_code, measure, rationale, confidence, settled_ts "
            "FROM shadow_trades WHERE status IN ('won','lost','sold') AND settled_ts >= ? "
            "ORDER BY settled_ts DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def log_retrospective(analysis_text: str, trades_analyzed: int, wins_analyzed: int,
                       losses_analyzed: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO retrospectives (ts, analysis_text, trades_analyzed, wins_analyzed, losses_analyzed) "
            "VALUES (?,?,?,?,?)",
            (int(time.time()), analysis_text, trades_analyzed, wins_analyzed, losses_analyzed),
        )
        return cur.lastrowid


def get_recent_retrospectives(limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, analysis_text, trades_analyzed, wins_analyzed, losses_analyzed "
            "FROM retrospectives ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def snapshot_shadow_bankroll(strategy: str, bankroll_cents: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO shadow_bankroll_snapshots (ts, strategy, bankroll_cents) VALUES (?,?,?)",
            (int(time.time()), strategy, bankroll_cents),
        )


def load_last_shadow_bankroll(strategy: str, default_cents: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT bankroll_cents FROM shadow_bankroll_snapshots WHERE strategy=? ORDER BY ts DESC LIMIT 1",
            (strategy,),
        ).fetchone()
        return row[0] if row else default_cents


def count_distinct_strategies_exposed_to_event(event_ticker: str | None,
                                                 exclude_strategy: str | None = None) -> int:
    """
    How many DIFFERENT strategies currently hold an open position
    somewhere in this same Kalshi event, other than exclude_strategy
    itself. Foundation for shadow.py's concentration dampening — see its
    docstring for the confirmed real-world motivation (20+ trades across
    nearly every strategy all buying the same losing side of the same
    underlying market, because they all share the same weather model).
    Counts DISTINCT strategies, not trades — three trades from the same
    strategy on the same event count as 1, not 3, since the concentration
    risk this measures is "how many independent judgments have piled onto
    this outcome," not "how many individual orders."
    """
    if not event_ticker:
        return 0
    with get_conn() as conn:
        query = "SELECT COUNT(DISTINCT strategy) FROM shadow_trades WHERE event_ticker=? AND status='open'"
        params: list = [event_ticker]
        if exclude_strategy:
            query += " AND strategy != ?"
            params.append(exclude_strategy)
        return conn.execute(query, params).fetchone()[0]


def count_open_positions_for_strategy_and_event(strategy: str, event_ticker: str | None) -> int:
    """
    How many open positions does THIS SAME strategy already hold
    somewhere in this event — the complement to
    count_distinct_strategies_exposed_to_event, which deliberately
    excludes a strategy's own prior positions since it's counting
    cross-strategy concentration. This counts the opposite risk: a
    single strategy repeatedly betting on the same underlying outcome
    through different thresholds/brackets within one event.

    CONFIRMED REAL-WORLD MOTIVATION: a loss-analysis review found
    temp_forecast_momentum taking three separate positions on different
    thresholds within one event (KXLOWTLV), on zero calibration samples,
    all lost together — concentration_dampening's own count excludes a
    strategy's prior positions on the same event by design, so this
    specific pattern (one strategy, multiple thresholds, same underlying
    outcome) had no dampening layer covering it at all.
    """
    if not event_ticker:
        return 0
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM shadow_trades WHERE strategy=? AND event_ticker=? AND status='open'",
            (strategy, event_ticker),
        ).fetchone()[0]


def get_recent_strategy_performance(strategy: str, lookback: int = 20) -> dict:
    """
    Rolling-window performance for a strategy's last `lookback` SETTLED
    trades (most recent first) — the data source for the automatic
    performance-dampening mechanism in shadow.py. Purely a summary of
    what already happened; computing what to DO about it is a separate,
    deliberately pure function so the decision logic stays testable
    without a database.

    Returns {"trades": int, "total_pnl_cents": int, "total_cost_cents": int,
    "roi_pct": float | None} — roi_pct is None when total_cost_cents is 0
    (shouldn't happen for a real trade, but avoids a division error if it
    somehow did).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pnl_cents, price_cents, count FROM shadow_trades "
            "WHERE strategy=? AND status IN ('won','lost','sold') "
            "ORDER BY settled_ts DESC LIMIT ?",
            (strategy, lookback),
        ).fetchall()
    trades = len(rows)
    total_pnl = sum((r[0] or 0) for r in rows)
    total_cost = sum(r[1] * r[2] for r in rows)
    roi_pct = (total_pnl / total_cost * 100) if total_cost else None
    return {"trades": trades, "total_pnl_cents": total_pnl,
            "total_cost_cents": total_cost, "roi_pct": roi_pct}


def get_shadow_bankroll_history(strategy: str, limit: int = 300) -> list[tuple[int, int]]:
    """Returns [(ts, bankroll_cents), ...] oldest-first, for charting."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, bankroll_cents FROM shadow_bankroll_snapshots WHERE strategy=? "
            "ORDER BY ts DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        return list(reversed(rows))


def _current_mark_for_position(conn, ticker: str, side: str, price_cents: int) -> int:
    """
    Per-CONTRACT mark-to-market price for one open position, using the
    same convention check_swing_exits()/check_bracket_arbitrage_offload()
    already use to value a held position: a YES holding is worth its
    current yes_bid (what you could sell it for right now), a NO holding
    is worth (100 - current yes_ask). A 'both' (2-leg arbitrage) position
    is a locked-in 100c regardless of price movement — dutch-book
    arbitrage doesn't fluctuate the way a directional position does, it's
    already guaranteed at entry.

    Falls back to the entry price_cents (assumes no unrealized gain/loss)
    when there's no recent price_history snapshot, rather than guessing at
    a number with no real data behind it. Shared by both the per-strategy
    aggregate (_mark_open_positions_to_market) and the per-trade detail
    view (get_open_positions_detail) so the two can never drift apart on
    what "current value" means.
    """
    if side == "both":
        return 100
    latest = conn.execute(
        "SELECT yes_ask, yes_bid FROM price_history WHERE ticker=? ORDER BY ts DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if latest is not None:
        yes_ask, yes_bid = latest
        mark = yes_bid if side == "yes" else (100 - yes_ask if yes_ask is not None else None)
        if mark is not None:
            return mark
    return price_cents


def _mark_open_positions_to_market(conn, strategy: str) -> tuple[int, int]:
    """
    Current mark-to-market value of every OPEN position for this strategy
    — see _current_mark_for_position()'s docstring for the per-contract
    valuation rules this aggregates.

    Returns (current_value_cents, cost_basis_cents) — the CALLER already
    has cost basis available separately in most cases, but returning both
    here keeps this function usable on its own too.
    """
    open_trades = conn.execute(
        "SELECT ticker, side, count, price_cents FROM shadow_trades WHERE strategy=? AND status='open'",
        (strategy,),
    ).fetchall()

    cost_basis = 0
    current_value = 0
    for ticker, side, count, price_cents in open_trades:
        cost_basis += price_cents * count
        mark = _current_mark_for_position(conn, ticker, side, price_cents)
        current_value += mark * count

    return current_value, cost_basis


def get_open_shadow_position_total_count() -> int:
    """
    The REAL total count of open positions across every strategy combined
    — unlimited, unlike get_open_positions_detail()'s necessarily-capped
    list (that one exists to render a bounded number of position cards on
    the dashboard, not to answer "how many are there really"). The
    dashboard's "N open" badge must use THIS, not the length of the
    (possibly truncated) detail list, or a real count above the detail
    limit would silently display as if it were the true total.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM shadow_trades WHERE status='open'").fetchone()
        return row[0] or 0


def get_open_positions_detail(limit: int = 300) -> list[dict]:
    """
    Every currently open position, individually — not aggregated by
    strategy the way get_shadow_summary() is. This is the data source for
    the dashboard's "Open Positions" view: what's actually held right now,
    what it's worth, and why the strategy took it (rationale) plus what
    it's aiming for (exit_target_cents, when the strategy sets one — only
    swing does today).

    Ordered most-recently-opened first, since that's usually what someone
    checking in on the bot cares about seeing near the top.
    """
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, ts, strategy, ticker, side, count, price_cents, model_probability, "
            "station_code, measure, exit_target_cents, rationale, confidence, bot_version "
            "FROM shadow_trades WHERE status='open' ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()

        positions = []
        for r in rows:
            row = dict(r)
            mark = _current_mark_for_position(conn, row["ticker"], row["side"], row["price_cents"])
            cost_basis = row["price_cents"] * row["count"]
            current_value = mark * row["count"]
            row["current_price_cents"] = mark
            row["cost_basis_cents"] = cost_basis
            row["current_value_cents"] = current_value
            row["unrealized_pnl_cents"] = current_value - cost_basis
            positions.append(row)
        return positions


def get_shadow_summary() -> list[dict]:
    """
    One row per strategy: settled count, win rate, total pnl, current
    bankroll, ROI% (comparable across strategies since they all start from
    the same SETTINGS.starting_bankroll_cents), and days_tracked (time since
    that strategy's first bankroll snapshot). Sorted best-to-worst by total
    P&L (equivalent to sorting by ROI% here, since the starting bankroll is
    shared) so the dashboard/export show a ranked leaderboard, not an
    arbitrary DISTINCT-query order.
    """
    # Strategies excluded despite having historical bankroll snapshots —
    # bracket_arbitrage disabled 2026-09-09 (18 settled trades, 0% win
    # rate, -5656% ROI — a real, not-yet-root-caused bug: a correctly
    # hedged bracket set should structurally win most of its individual
    # leg-bets, so a 0% win rate means something is inverted, not bad
    # luck). Not deleted from shadow.py's STRATEGIES, just commented out
    # there and filtered from the leaderboard here — historical rows stay
    # queryable directly for whoever eventually debugs it.
    excluded = {"bracket_arbitrage"}
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        strategies = [r["strategy"] for r in conn.execute("SELECT DISTINCT strategy FROM shadow_bankroll_snapshots")
                      if r["strategy"] not in excluded]
        summary = []
        for s in strategies:
            settled = conn.execute(
                "SELECT COUNT(*) as n, SUM(CASE WHEN COALESCE(pnl_cents,0) > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(COALESCE(pnl_cents,0)) as total_pnl FROM shadow_trades "
                "WHERE strategy=? AND status IN ('won','lost','sold')", (s,)
            ).fetchone()
            open_row = conn.execute(
                "SELECT COUNT(*) as n, SUM(price_cents * count) as capital FROM shadow_trades "
                "WHERE strategy=? AND status='open'", (s,)
            ).fetchone()
            current_value_cents, _ = _mark_open_positions_to_market(conn, s)
            bankroll_row = conn.execute(
                "SELECT bankroll_cents FROM shadow_bankroll_snapshots WHERE strategy=? ORDER BY ts DESC LIMIT 1", (s,)
            ).fetchone()
            first_ts_row = conn.execute(
                "SELECT MIN(ts) as first_ts FROM shadow_bankroll_snapshots WHERE strategy=?", (s,)
            ).fetchone()
            n = settled["n"] or 0
            total_pnl = settled["total_pnl"] or 0
            first_ts = first_ts_row["first_ts"] if first_ts_row else None
            summary.append({
                "strategy": s,
                "settled": n,
                "open": open_row["n"] or 0,
                "open_capital_cents": open_row["capital"] or 0,
                "current_value_cents": current_value_cents,
                "unrealized_pnl_cents": current_value_cents - (open_row["capital"] or 0),
                "wins": settled["wins"] or 0,
                "win_rate": (settled["wins"] or 0) / n if n else None,
                "total_pnl_cents": total_pnl,
                "bankroll_cents": bankroll_row["bankroll_cents"] if bankroll_row else None,
                "roi_pct": (total_pnl / SETTINGS.starting_bankroll_cents * 100) if SETTINGS.starting_bankroll_cents else None,
                "days_tracked": max(0, (time.time() - first_ts) / 86400) if first_ts else None,
                "enough_data": n >= MIN_SAMPLE_SIZE,
            })
        summary.sort(key=lambda row: row["total_pnl_cents"], reverse=True)
        for i, row in enumerate(summary, start=1):
            row["rank"] = i
        return summary


def get_win_rate_by_edge_bucket() -> list[dict]:
    """
    Does a bigger claimed edge actually predict a better outcome, or is
    the model's edge_cents just noise dressed up as confidence? Buckets
    every settled trade with a real model_probability by its ESTIMATED
    edge size at decision time, computed from stored columns rather than
    a new field: for a 'yes' trade, edge = model_probability*100 -
    price_cents (model_probability is always stored as the YES-side
    probability, per the convention calibration.py depends on — see
    strategy.TradeSignal's docstring); for 'no', edge = (1-model_probability)*100
    - price_cents. 'both' (arbitrage) trades are excluded — they don't
    carry a real probability estimate to compute an edge from at all.

    Bucket boundaries are in cents: [0,5), [5,10), [10,20), [20,+inf).
    If bigger buckets don't show meaningfully better win rates than
    smaller ones, that's a real, checkable signal the edge estimate isn't
    doing what it's supposed to — not a hunch, a number.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT side, price_cents, model_probability, pnl_cents FROM shadow_trades "
            "WHERE status IN ('won','lost','sold') AND model_probability IS NOT NULL AND side != 'both'"
        ).fetchall()

    buckets = [(0, 5), (5, 10), (10, 20), (20, float("inf"))]
    bucket_stats = {f"{lo}-{hi if hi != float('inf') else '+'}c": {"trades": 0, "wins": 0, "total_pnl": 0}
                     for lo, hi in buckets}

    for side, price_cents, model_prob, pnl_cents in rows:
        edge = (model_prob * 100 - price_cents) if side == "yes" else ((1 - model_prob) * 100 - price_cents)
        edge = abs(edge)
        for lo, hi in buckets:
            if lo <= edge < hi:
                key = f"{lo}-{hi if hi != float('inf') else '+'}c"
                bucket_stats[key]["trades"] += 1
                if (pnl_cents or 0) > 0:
                    bucket_stats[key]["wins"] += 1
                bucket_stats[key]["total_pnl"] += (pnl_cents or 0)
                break

    result = []
    for lo, hi in buckets:
        key = f"{lo}-{hi if hi != float('inf') else '+'}c"
        s = bucket_stats[key]
        result.append({
            "edge_bucket": key,
            "trades": s["trades"],
            "win_rate": (s["wins"] / s["trades"]) if s["trades"] else None,
            "total_pnl_cents": s["total_pnl"],
        })
    return result


def get_win_rate_by_confidence() -> list[dict]:
    """
    Same idea as get_win_rate_by_edge_bucket, for rules-extraction
    confidence instead of edge size — does 'high' confidence actually win
    more than 'medium'? Only trades logged after the confidence column
    was added have a real value here; older rows show up under
    '(unknown)' rather than being silently dropped, so the total count
    stays honest even while the field is still filling in.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(confidence, '(unknown)') as conf, "
            "COUNT(*) as trades, "
            "SUM(CASE WHEN COALESCE(pnl_cents,0) > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(COALESCE(pnl_cents,0)) as total_pnl "
            "FROM shadow_trades WHERE status IN ('won','lost','sold') "
            "GROUP BY conf"
        ).fetchall()

    result = []
    for conf, trades, wins, total_pnl in rows:
        result.append({
            "confidence": conf,
            "trades": trades,
            "win_rate": (wins / trades) if trades else None,
            "total_pnl_cents": total_pnl or 0,
        })
    # 'high' -> 'medium' -> 'low' -> '(unknown)' is a more useful reading
    # order than whatever GROUP BY happened to return.
    order = {"high": 0, "medium": 1, "low": 2, "(unknown)": 3}
    result.sort(key=lambda r: order.get(r["confidence"], 4))
    return result


def get_shadow_summary_by_category() -> dict[str, dict]:
    """
    Same idea as get_shadow_summary(), but split by trading category (Rain /
    Temperature / Other — see categories.py) instead of lumping every
    market type into one number. Each category gets its own 'overall' line
    (every strategy combined, for "is this category worth trading at all")
    plus a per-strategy leaderboard within that category, sorted best-to-
    worst by total P&L. Categories themselves are sorted the same way.

    Only counts SETTLED trades (won/lost/sold) — open positions have no P&L
    yet and would just add noise to a "which category actually wins" view.

    ROI% uses the same SETTINGS.starting_bankroll_cents denominator as
    get_shadow_summary(), so category ROI numbers are directly comparable to
    each other and to the overall per-strategy leaderboard — there's no
    separate bankroll per category, since all shadow strategies trade every
    category from one shared paper bankroll.
    """
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT strategy, measure, pnl_cents FROM shadow_trades WHERE status IN ('won','lost','sold') "
            "AND strategy NOT IN ('bracket_arbitrage')"  # see get_shadow_summary()'s exclusion note
        ).fetchall()

    from collections import defaultdict
    per_strategy: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"settled": 0, "wins": 0, "total_pnl": 0}))
    per_category: dict[str, dict] = defaultdict(lambda: {"settled": 0, "wins": 0, "total_pnl": 0})

    for r in rows:
        cat = category_for(r["measure"])
        pnl = r["pnl_cents"] or 0
        strat_bucket = per_strategy[cat][r["strategy"]]
        strat_bucket["settled"] += 1
        strat_bucket["total_pnl"] += pnl
        if pnl > 0:
            strat_bucket["wins"] += 1
        cat_bucket = per_category[cat]
        cat_bucket["settled"] += 1
        cat_bucket["total_pnl"] += pnl
        if pnl > 0:
            cat_bucket["wins"] += 1

    def roi(total_pnl: int) -> float | None:
        return (total_pnl / SETTINGS.starting_bankroll_cents * 100) if SETTINGS.starting_bankroll_cents else None

    result = {}
    for cat, strat_map in per_strategy.items():
        strategies = []
        for strat, b in strat_map.items():
            n = b["settled"]
            strategies.append({
                "strategy": strat,
                "settled": n,
                "win_rate": b["wins"] / n if n else None,
                "total_pnl_cents": b["total_pnl"],
                "roi_pct": roi(b["total_pnl"]),
                "enough_data": n >= MIN_SAMPLE_SIZE,
            })
        strategies.sort(key=lambda row: row["total_pnl_cents"], reverse=True)
        for i, row in enumerate(strategies, start=1):
            row["rank"] = i

        cat_bucket = per_category[cat]
        n = cat_bucket["settled"]
        result[cat] = {
            "overall": {
                "settled": n,
                "win_rate": cat_bucket["wins"] / n if n else None,
                "total_pnl_cents": cat_bucket["total_pnl"],
                "roi_pct": roi(cat_bucket["total_pnl"]),
                "enough_data": n >= MIN_SAMPLE_SIZE,
            },
            "strategies": strategies,
        }

    return dict(sorted(result.items(), key=lambda kv: kv[1]["overall"]["total_pnl_cents"], reverse=True))


def get_category_pnl_over_time() -> dict[str, list[tuple[int, int]]]:
    """
    Cumulative P&L over time per category (Rain/Temperature/Other), for
    charting. Unlike get_shadow_bankroll_history (per-strategy, from
    periodic snapshots), this is built directly from settled_ts + pnl_cents
    on shadow_trades — every strategy's settled trades in a category are
    summed together, running-total style, ordered by settlement time. No
    new table needed since shadow_trades already has everything required.

    Returns {category: [(ts, cumulative_pnl_cents), ...]}, oldest first.
    Categories with zero settled trades are simply absent from the dict —
    same "shows up once it has data" behavior as get_shadow_summary_by_category.
    """
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT measure, settled_ts, pnl_cents FROM shadow_trades "
            "WHERE status IN ('won','lost','sold') AND settled_ts IS NOT NULL "
            "ORDER BY settled_ts ASC"
        ).fetchall()

    from collections import defaultdict
    running: dict[str, int] = defaultdict(int)
    series: dict[str, list[tuple[int, int]]] = defaultdict(list)

    for r in rows:
        cat = category_for(r["measure"])
        running[cat] += r["pnl_cents"] or 0
        series[cat].append((r["settled_ts"], running[cat]))

    return dict(series)


# ---------- price history (raw material for swing-strategy design) ----------

def log_price_snapshot(ticker: str, yes_ask: int | None, yes_bid: int | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO price_history (ts, ticker, yes_ask, yes_bid) VALUES (?,?,?,?)",
            (int(time.time()), ticker, yes_ask, yes_bid),
        )


def get_latest_price(ticker: str) -> dict | None:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ts, yes_ask, yes_bid FROM price_history WHERE ticker=? ORDER BY ts DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        return dict(row) if row else None


def get_current_open_markets(window_seconds: int = 900) -> list[dict]:
    """
    "Currently open" isn't tracked as its own state anywhere — this infers
    it from price_history instead: every market that's actually open gets a
    fresh snapshot logged every scan cycle (see bot.py), so a ticker with a
    RECENT snapshot is open, and one that's gone quiet (closed/settled, so
    nothing is logging it anymore) ages out of this list on its own after
    `window_seconds` with no new activity. Default window is 3x the normal
    5-minute poll interval — wide enough to tolerate one missed cycle
    without a genuinely-open market dropping off.

    Returns the latest snapshot per ticker, most recently updated first.
    """
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cutoff = int(time.time()) - window_seconds
        rows = conn.execute(
            # MAX(id), not MAX(ts): two snapshots logged within the same
            # second share an identical Unix timestamp (real, observed —
            # not just a theoretical edge case), which made ts-based
            # tie-breaking non-deterministic and could return more than
            # one row for the same ticker. id is always unique and
            # strictly increases with insertion order, so this guarantees
            # exactly one row per ticker — the most recently INSERTED one,
            # which matches "most recent" even when ts ties.
            "SELECT p1.ticker, p1.yes_ask, p1.yes_bid, p1.ts FROM price_history p1 "
            "WHERE p1.id = (SELECT MAX(p2.id) FROM price_history p2 WHERE p2.ticker = p1.ticker) "
            "AND p1.ts >= ? ORDER BY p1.ts DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_decision_summary(hours: int = 24) -> dict:
    """
    Diagnostic summary of the main bot's per-market decision log (see
    log_decision, called in bot.py once per scanned market — either
    'traded' or 'skipped' with a reason). This ISN'T shadow-strategy
    activity — it's the ACTIVE bot's own gate — but it matters for shadow
    strategies too: a market skipped here for "low-confidence rules
    extraction" or "no model for measure" never even reaches
    shadow.evaluate_and_log (see the `continue` statements in bot.py right
    after those specific log_decision calls). So a big bucket of either of
    those two reasons explains an otherwise-mysterious quiet day across
    every shadow strategy, not just the main bot — that's the single most
    useful thing this view can surface.

    Skip reasons carry dynamic numbers (an exact price or edge value) that
    would otherwise make almost every skip its own unique group — bucketed
    here into stable categories by known prefix/substring instead.
    """
    cutoff = int(time.time()) - hours * 3600
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT action, reason FROM decisions WHERE ts >= ?", (cutoff,)
        ).fetchall()

    def bucket(reason: str | None) -> str:
        if not reason:
            return "(no reason logged)"
        r = reason.lower()
        if r.startswith("low-confidence"):
            return "low-confidence rules extraction (never reaches shadow strategies)"
        if r.startswith("no model for measure"):
            return "unrecognized measure — not weather, or a new measure type (never reaches shadow strategies)"
        if r.startswith("no live ask quote"):
            return "no tradeable quote yet (thin/new market — never reaches shadow strategies)"
        if "rain signals are same-day" in r:
            return "rain market too far out (>30h) — filtered before reaching shadow strategies"
        if "kill switch" in r:
            return "daily loss kill switch tripped"
        if r.startswith("at max open positions"):
            return "at max open positions"
        if "outside allowed band" in r:
            return "price outside allowed band"
        if "below minimum" in r:
            return "edge below minimum"
        if "rounds to 0 contracts" in r:
            return "position size too small for bankroll"
        if r.startswith("orderbook fetch failed"):
            return "orderbook fetch failed"
        if r.startswith("rules extraction failed"):
            return "rules extraction failed (network or API issue, not a bad-data problem)"
        if "no size clears net-of-fee edge" in r:
            return "no profitable size at real order-book depth"
        return reason[:70]

    counts = {"traded": 0}
    skip_reasons: dict[str, int] = {}
    for row in rows:
        if row["action"] == "traded":
            counts["traded"] += 1
        else:
            b = bucket(row["reason"])
            skip_reasons[b] = skip_reasons.get(b, 0) + 1
    counts["skipped"] = sum(skip_reasons.values())
    counts["total"] = counts["traded"] + counts["skipped"]
    return {
        "counts": counts,
        "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: -kv[1])),
    }


def get_previous_forecast_temp_f(ticker: str) -> float | None:
    """The most recently logged forecast temp for this ticker — call this
    BEFORE log_forecast_snapshot() for the current cycle, so "most recent"
    means "as of last cycle," not "as of right now." Returns None on the
    first-ever scan of a ticker (nothing to compare against yet)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT forecast_temp_f FROM forecast_history WHERE ticker=? ORDER BY ts DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        return row[0] if row else None


def log_forecast_snapshot(ticker: str, forecast_temp_f: float | None) -> None:
    if forecast_temp_f is None:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO forecast_history (ts, ticker, forecast_temp_f) VALUES (?,?,?)",
            (int(time.time()), ticker, forecast_temp_f),
        )


def log_market_snapshot(ticker: str, event_ticker: str | None = None,
                          station_code: str | None = None, measure: str | None = None,
                          yes_ask: int | None = None, yes_bid: int | None = None,
                          no_ask: int | None = None, no_bid: int | None = None,
                          observed_temp_f: float | None = None,
                          forecast_temp_f: float | None = None,
                          precip_pop_pct: float | None = None,
                          observed_precip_mm: float | None = None,
                          threshold_low_f: float | None = None,
                          threshold_high_f: float | None = None,
                          hours_until_close: float | None = None,
                          model_probability_yes: float | None = None,
                          close_time: str | None = None) -> None:
    """
    Foundation for retrospective backtesting — see market_snapshots'
    schema comment for the full reasoning. Called once per scanned
    market per cycle, regardless of whether any strategy trades it.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO market_snapshots (ts, ticker, event_ticker, station_code, measure, "
            "yes_ask, yes_bid, no_ask, no_bid, observed_temp_f, forecast_temp_f, precip_pop_pct, "
            "observed_precip_mm, threshold_low_f, threshold_high_f, hours_until_close, "
            "model_probability_yes, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), ticker, event_ticker, station_code, measure,
             yes_ask, yes_bid, no_ask, no_bid, observed_temp_f, forecast_temp_f, precip_pop_pct,
             observed_precip_mm, threshold_low_f, threshold_high_f, hours_until_close,
             model_probability_yes, close_time),
        )


def get_tickers_needing_outcome_backfill(limit: int = 50) -> list[dict]:
    """
    Every ticker that's been snapshotted (so we have something to
    backtest) but has no recorded outcome yet, and whose close_time has
    genuinely passed — no point checking settlement on a market that
    hasn't even closed. Returns the MOST RECENT snapshot's close_time
    per ticker (a market's close_time doesn't change between snapshots,
    but taking the latest is more robust to a rules/data hiccup on an
    earlier cycle). limit bounds how many get checked per backfill run,
    since a real backlog could otherwise mean a huge burst of settlement
    API calls in one pass — see settlement.backfill_market_outcomes.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, MAX(ts) as latest_ts, close_time FROM market_snapshots "
            "WHERE ticker NOT IN (SELECT ticker FROM market_outcomes) "
            "AND close_time IS NOT NULL AND close_time < ? "
            "GROUP BY ticker LIMIT ?",
            (now_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def record_market_outcome(ticker: str, result: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO market_outcomes (ticker, result, settled_ts) VALUES (?,?,?) "
            "ON CONFLICT(ticker) DO NOTHING",
            (ticker, result, int(time.time())),
        )


def save_historical_market(ticker: str, series_ticker: str | None = None,
                             event_ticker: str | None = None, station_code: str | None = None,
                             measure: str | None = None, threshold_low_f: float | None = None,
                             threshold_high_f: float | None = None, open_time: str | None = None,
                             close_time: str | None = None, result: str | None = None,
                             settlement_source: str | None = None, threshold_description: str | None = None,
                             confidence: str | None = None) -> None:
    """Idempotent by design (INSERT OR REPLACE on the ticker primary key)
    — the backfill script can safely be re-run over a series it's already
    partly processed without creating duplicates or needing its own
    resume-tracking logic."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO historical_markets "
            "(ticker, series_ticker, event_ticker, station_code, measure, threshold_low_f, "
            "threshold_high_f, open_time, close_time, result, settlement_source, "
            "threshold_description, confidence, backfilled_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, series_ticker, event_ticker, station_code, measure, threshold_low_f,
             threshold_high_f, open_time, close_time, result, settlement_source,
             threshold_description, confidence, int(time.time())),
        )


def get_historical_market(ticker: str) -> dict | None:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM historical_markets WHERE ticker=?", (ticker,)).fetchone()
        return dict(row) if row else None


def save_historical_price_points(ticker: str, points: list[tuple[int, int | None, int | None]]) -> None:
    """points: list of (ts, yes_price_cents, volume). Bulk insert — a
    single historical market's candlesticks can be hundreds of points,
    and this is called once per market during backfill, not once per
    point."""
    if not points:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO historical_price_points (ticker, ts, yes_price_cents, volume) VALUES (?,?,?,?)",
            [(ticker, ts, price, vol) for ts, price, vol in points],
        )


def save_historical_weather_points(station_code: str,
                                     points: list[tuple[int, float | None, float | None, float | None, float | None]]) -> None:
    """points: list of (ts, forecast_temp_f, forecast_precip_pop_pct,
    observed_temp_f, observed_precip_mm)."""
    if not points:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO historical_weather_points "
            "(station_code, ts, forecast_temp_f, forecast_precip_pop_pct, observed_temp_f, observed_precip_mm) "
            "VALUES (?,?,?,?,?,?)",
            [(station_code, ts, ftemp, fpop, otemp, oprecip) for ts, ftemp, fpop, otemp, oprecip in points],
        )


def has_historical_weather_for_station(station_code: str, start_ts: int, end_ts: int) -> bool:
    """Whether this station's weather has already been backfilled for
    this time range — lets the backfill script skip re-fetching weather
    for a station+date range it's already covered via an earlier market
    at the same station, since many bracket markets for one city/day
    share a station."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM historical_weather_points WHERE station_code=? AND ts BETWEEN ? AND ?",
            (station_code, start_ts, end_ts),
        ).fetchone()
        return row[0] > 0


def get_price_history(ticker: str, limit: int = 500) -> list[dict]:
    """Oldest-first price series for one ticker — use this once you have
    weeks of data to actually measure typical intraday movement per city."""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, yes_ask, yes_bid FROM price_history WHERE ticker=? ORDER BY ts DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ---------- strategy overrides & advisor suggestions ----------

def get_overrides() -> dict[str, dict[str, float]]:
    """{strategy: {param: value}} for every override currently active —
    either auto-applied by advisor.auto_apply_pending_suggestions or set
    directly. "Human-approved" no longer describes every row here since
    auto-apply was introduced for TUNABLE_PARAMS (paper-only shadow
    strategies, never real-money settings — see that constant's own
    docstring for why that boundary is structural, not just a rule)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT strategy, param, value FROM strategy_overrides").fetchall()
    result: dict[str, dict[str, float]] = {}
    for strategy, param, value in rows:
        result.setdefault(strategy, {})[param] = value
    return result


def set_override(strategy: str, param: str, value: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO strategy_overrides (strategy, param, value, applied_ts) VALUES (?,?,?,?) "
            "ON CONFLICT(strategy, param) DO UPDATE SET value=excluded.value, applied_ts=excluded.applied_ts",
            (strategy, param, value, int(time.time())),
        )


def clear_override(strategy: str, param: str) -> None:
    """
    Reverts a single tunable parameter back to its hardcoded default by
    removing its row from strategy_overrides — the safety valve paired
    with auto-apply (see advisor.auto_apply_pending_suggestions): nothing
    requires a human to approve a suggestion before it takes effect
    anymore, but anything auto-applied can be undone with one call.
    Silently a no-op if no override exists for this (strategy, param) —
    reverting something already at its default isn't an error.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM strategy_overrides WHERE strategy=? AND param=?", (strategy, param))


def log_suggestion(strategy: str, param: str, current_value: float, suggested_value: float,
                    rationale: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO suggestions (ts, strategy, param, current_value, suggested_value, rationale) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), strategy, param, current_value, suggested_value, rationale),
        )
        return cur.lastrowid


def get_suggestions(status: str = "pending") -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE status=? ORDER BY ts DESC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_suggestion_status(suggestion_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE suggestions SET status=? WHERE id=?", (status, suggestion_id))


def get_meta(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_meta(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
