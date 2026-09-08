"""
Everything the bot does gets logged here. If you can't reconstruct exactly
why a trade happened six weeks later, you can't tell whether the strategy
is working or you're fooling yourself with a few lucky weeks.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from config import SETTINGS

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
    measure TEXT                 -- 'precipitation_daily' etc, for calibration lookups
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
    exit_target_cents INTEGER    -- swing strategy only: sell early once this price is reached
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


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_add_columns(conn)


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
              station_code: str | None = None, measure: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trades (ts, ticker, side, count, price_cents, mode, order_id, "
            "model_probability, station_code, measure) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), ticker, side, count, price_cents, mode, order_id,
             model_probability, station_code, measure),
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
                      measure: str | None = None, exit_target_cents: int | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO shadow_trades (ts, strategy, ticker, side, count, price_cents, "
            "model_probability, station_code, measure, exit_target_cents) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), strategy, ticker, side, count, price_cents,
             model_probability, station_code, measure, exit_target_cents),
        )
        return cur.lastrowid


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


def get_shadow_bankroll_history(strategy: str, limit: int = 300) -> list[tuple[int, int]]:
    """Returns [(ts, bankroll_cents), ...] oldest-first, for charting."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, bankroll_cents FROM shadow_bankroll_snapshots WHERE strategy=? "
            "ORDER BY ts DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        return list(reversed(rows))


def get_shadow_summary() -> list[dict]:
    """One row per strategy: settled count, win rate, total pnl, current bankroll."""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        strategies = [r["strategy"] for r in conn.execute("SELECT DISTINCT strategy FROM shadow_bankroll_snapshots")]
        summary = []
        for s in strategies:
            settled = conn.execute(
                "SELECT COUNT(*) as n, SUM(CASE WHEN COALESCE(pnl_cents,0) > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(COALESCE(pnl_cents,0)) as total_pnl FROM shadow_trades "
                "WHERE strategy=? AND status IN ('won','lost','sold')", (s,)
            ).fetchone()
            bankroll_row = conn.execute(
                "SELECT bankroll_cents FROM shadow_bankroll_snapshots WHERE strategy=? ORDER BY ts DESC LIMIT 1", (s,)
            ).fetchone()
            n = settled["n"] or 0
            summary.append({
                "strategy": s,
                "settled": n,
                "wins": settled["wins"] or 0,
                "win_rate": (settled["wins"] or 0) / n if n else None,
                "total_pnl_cents": settled["total_pnl"] or 0,
                "bankroll_cents": bankroll_row["bankroll_cents"] if bankroll_row else None,
            })
        return summary


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
