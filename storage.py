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
from categories import category_for, MIN_SAMPLE_SIZE

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
    exit_target_cents INTEGER,   -- swing strategy only: sell early once this price is reached
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
                      measure: str | None = None, exit_target_cents: int | None = None,
                      precomputed_payout_cents: int | None = None,
                      sample_member_ticker: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO shadow_trades (ts, strategy, ticker, side, count, price_cents, "
            "model_probability, station_code, measure, exit_target_cents, "
            "precomputed_payout_cents, sample_member_ticker) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), strategy, ticker, side, count, price_cents,
             model_probability, station_code, measure, exit_target_cents,
             precomputed_payout_cents, sample_member_ticker),
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
    """{strategy: {param: value}} for every human-approved override currently active."""
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
