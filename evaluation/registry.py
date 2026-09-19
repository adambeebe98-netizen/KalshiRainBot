"""
The trial counter and run records.

Everything in stats.py that deflates a score needs one number: how many
candidates have been looked at. That number is not a property of the
current run -- it is cumulative over the life of the project, because the
selection effect is cumulative. Evaluate a thousand candidates across
fifty sessions and the best of them is the best of a thousand, not the
best of the twenty you tried this afternoon.

So `eval_trials` is append-only and enforced as such by a SQLite trigger:
DELETE and UPDATE both abort. This is not bookkeeping fussiness. Deleting
rows from it does not tidy the history, it silently lowers the bar that
every future result has to clear, and it does so in the direction that
makes results look better. The one operation that would let you fool
yourself is the one the database refuses.

`eval_runs` records enough to reproduce a run: the config hash, the seed,
the code commit, the fold parameters, the execution assumptions, and the
data snapshot boundaries -- the maximum row id in each source table at run
start. The live tables keep growing underneath the archive, so without
pinned boundaries "re-run that config" quietly means "run it on more
data". If a pinned boundary no longer exists, the harness raises rather
than running on a different dataset while claiming to reproduce.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from config import SETTINGS
from evaluation import stats

# Tables whose growth would change a result if it were not pinned.
DEFAULT_SNAPSHOT_TABLES = (
    "historical_markets",
    "historical_price_points",
    "historical_weather_points",
    "market_snapshots",
    "realtime_ticks",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    seed INTEGER NOT NULL,
    code_commit TEXT,
    splits_used TEXT NOT NULL,
    purge_seconds INTEGER NOT NULL,
    embargo_seconds INTEGER NOT NULL,
    execution_model TEXT NOT NULL,
    execution_assumptions_json TEXT NOT NULL,
    data_boundaries_json TEXT NOT NULL
);

-- APPEND ONLY. The count of rows here is the N that every deflated score
-- is measured against. Deleting from it does not clean up history, it
-- lowers the bar for every future result, in the flattering direction.
-- The triggers below make that impossible rather than merely discouraged.
CREATE TABLE IF NOT EXISTS eval_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    code_commit TEXT,
    net_sharpe REAL,
    verdict TEXT
);

-- SQLite has no implicit string concatenation, so these messages are
-- single literals however long they run.
CREATE TRIGGER IF NOT EXISTS eval_trials_no_delete
BEFORE DELETE ON eval_trials
BEGIN
    SELECT RAISE(ABORT, 'eval_trials is append-only: deleting trials lowers the multiple-testing bar for every future result');
END;

CREATE TRIGGER IF NOT EXISTS eval_trials_no_update
BEFORE UPDATE ON eval_trials
BEGIN
    SELECT RAISE(ABORT, 'eval_trials is append-only: rewriting a trial is the same as deleting it');
END;

CREATE INDEX IF NOT EXISTS idx_eval_trials_run ON eval_trials(run_id);
"""


class BoundaryError(RuntimeError):
    """A pinned data boundary no longer exists, so the run cannot be
    reproduced on the data it originally saw."""


def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def init(db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


# --------------------------------------------------------------------------
# Config hashing
# --------------------------------------------------------------------------

def canonical_json(config: dict) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace.

    Sorting matters. Without it, two identical configs built in different
    orders hash differently, and the registry would report a config as
    new every time it was reconstructed -- inflating the trial count with
    phantom candidates while failing to recognise a genuine repeat.
    """
    return json.dumps(config, sort_keys=True, separators=(",", ":"),
                      default=str)


def config_hash(config: dict) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def code_commit() -> str | None:
    """The commit the code was at. Best-effort: a run from a dirty tree or
    outside a checkout still records, it just cannot say which commit."""
    try:
        import bot
        return bot.get_git_commit()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Data snapshot boundaries
# --------------------------------------------------------------------------

def snapshot_boundaries(tables=DEFAULT_SNAPSHOT_TABLES,
                        db_path: str | None = None) -> dict[str, int]:
    """Highest row id per source table, or row count for tables without an
    integer id (historical_markets is keyed by ticker).

    Missing tables are recorded as -1 rather than skipped, so a later run
    can tell "this table did not exist" apart from "nobody looked".
    """
    out: dict[str, int] = {}
    with _connect(db_path) as conn:
        for table in tables:
            try:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            except sqlite3.DatabaseError:
                cols = []
            if not cols:
                out[table] = -1
                continue
            if "id" in cols:
                row = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
                out[table] = int(row[0]) if row[0] is not None else 0
            else:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                out[table] = int(row[0])
    return out


def check_boundaries(boundaries: dict[str, int],
                     db_path: str | None = None) -> None:
    """Verify a pinned snapshot is still reachable.

    Raises rather than warning. A reproduction run that silently sees
    different data is worse than one that fails, because it produces a
    number that looks comparable to the original and is not.
    """
    current = snapshot_boundaries(tuple(boundaries.keys()), db_path)
    problems = []
    for table, pinned in boundaries.items():
        now = current.get(table, -1)
        if pinned == -1:
            continue
        if now == -1:
            problems.append(f"{table}: table no longer exists")
        elif now < pinned:
            problems.append(
                f"{table}: pinned at {pinned}, now at {now} -- rows were "
                f"deleted, so the original data is gone")
    if problems:
        raise BoundaryError(
            "cannot reproduce this run on the data it originally saw:\n  "
            + "\n  ".join(problems))


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRecord:
    run_id: str
    ts: int
    config_hash: str
    seed: int
    code_commit: str | None
    splits_used: str
    purge_seconds: int
    embargo_seconds: int
    execution_model: str
    execution_assumptions: dict
    data_boundaries: dict = field(default_factory=dict)

    def describe(self) -> str:
        stamp = dt.datetime.fromtimestamp(self.ts, dt.timezone.utc).isoformat()
        return (f"run {self.run_id} at {stamp}\n"
                f"  config {self.config_hash[:12]}  seed {self.seed}  "
                f"commit {self.code_commit or 'unknown'}\n"
                f"  splits {self.splits_used}  purge {self.purge_seconds}s  "
                f"embargo {self.embargo_seconds}s\n"
                f"  execution {self.execution_model}")


def open_run(config: dict, seed: int, splits_used: str,
             purge_seconds: int, embargo_seconds: int,
             execution_assumptions, db_path: str | None = None,
             snapshot_tables=DEFAULT_SNAPSHOT_TABLES) -> RunRecord:
    """Start a run, pinning everything needed to reproduce it."""
    init(db_path)
    from dataclasses import asdict
    assumptions = (asdict(execution_assumptions)
                   if hasattr(execution_assumptions, "__dataclass_fields__")
                   else dict(execution_assumptions))
    record = RunRecord(
        run_id=uuid.uuid4().hex,
        ts=int(time.time()),
        config_hash=config_hash(config),
        seed=seed,
        code_commit=code_commit(),
        splits_used=splits_used,
        purge_seconds=purge_seconds,
        embargo_seconds=embargo_seconds,
        execution_model=assumptions.get("model", "unknown"),
        execution_assumptions=assumptions,
        data_boundaries=snapshot_boundaries(snapshot_tables, db_path),
    )
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO eval_runs (run_id, ts, config_hash, seed, code_commit, "
            "splits_used, purge_seconds, embargo_seconds, execution_model, "
            "execution_assumptions_json, data_boundaries_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (record.run_id, record.ts, record.config_hash, record.seed,
             record.code_commit, record.splits_used, record.purge_seconds,
             record.embargo_seconds, record.execution_model,
             canonical_json(record.execution_assumptions),
             canonical_json(record.data_boundaries)))
        conn.commit()
    return record


def load_run(run_id: str, db_path: str | None = None) -> RunRecord | None:
    init(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM eval_runs WHERE run_id = ?",
                           (run_id,)).fetchone()
    if row is None:
        return None
    return RunRecord(
        run_id=row["run_id"], ts=row["ts"], config_hash=row["config_hash"],
        seed=row["seed"], code_commit=row["code_commit"],
        splits_used=row["splits_used"], purge_seconds=row["purge_seconds"],
        embargo_seconds=row["embargo_seconds"],
        execution_model=row["execution_model"],
        execution_assumptions=json.loads(row["execution_assumptions_json"]),
        data_boundaries=json.loads(row["data_boundaries_json"]),
    )


def reopen_run(run_id: str, db_path: str | None = None) -> RunRecord:
    """Load a run and verify its data is still there. Raises otherwise."""
    record = load_run(run_id, db_path)
    if record is None:
        raise KeyError(f"no such run: {run_id}")
    check_boundaries(record.data_boundaries, db_path)
    return record


# --------------------------------------------------------------------------
# Trials
# --------------------------------------------------------------------------

def record_trial(run_id: str, candidate_name: str, config_hash_: str,
                 net_sharpe: float | None = None, verdict: str | None = None,
                 db_path: str | None = None) -> int:
    """Append one evaluation and return the new cumulative trial count.

    Called for EVERY candidate evaluated, including ones abandoned
    immediately. A candidate you looked at and discarded still consumed a
    look, and the selection effect does not care that you were unimpressed.
    """
    init(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO eval_trials (ts, run_id, candidate_name, config_hash, "
            "code_commit, net_sharpe, verdict) VALUES (?,?,?,?,?,?,?)",
            (int(time.time()), run_id, candidate_name, config_hash_,
             code_commit(), net_sharpe, verdict))
        conn.commit()
        (n,) = conn.execute("SELECT COUNT(*) FROM eval_trials").fetchone()
    return int(n)


def trials_to_date(db_path: str | None = None) -> int:
    init(db_path)
    with _connect(db_path) as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM eval_trials").fetchone()
    return int(n)


def observed_sharpe_variance(db_path: str | None = None,
                             minimum: float = 0.01) -> float:
    """Variance of net Sharpe across all trials recorded so far.

    This is the sigma in Bailey's expected-maximum formula, and it is the
    term people get wrong: it is the dispersion of the SEARCH's results,
    not the variance of any one strategy's returns. A wide-ranging search
    over very different candidates has a high value here and therefore a
    high luck threshold, which is correct -- it had more chances to throw
    up something extreme.

    Floored, because a handful of early trials can produce a near-zero
    variance that would make the luck threshold vanish exactly when the
    sample is too small to trust.
    """
    with _connect(db_path) as conn:
        values = [r[0] for r in conn.execute(
            "SELECT net_sharpe FROM eval_trials WHERE net_sharpe IS NOT NULL")]
    if len(values) < 2:
        return minimum
    return max(minimum, stats.stdev(values) ** 2)


def luck_threshold(n_trials: int | None = None,
                   sharpe_variance: float | None = None,
                   db_path: str | None = None) -> float:
    """The Sharpe a no-skill best-of-N would reach by luck alone."""
    n = trials_to_date(db_path) if n_trials is None else n_trials
    var = (observed_sharpe_variance(db_path)
           if sharpe_variance is None else sharpe_variance)
    return stats.expected_max_sharpe(max(n, 1), var)


def trial_banner(n_trials: int | None = None,
                 sharpe_variance: float | None = None,
                 db_path: str | None = None) -> str:
    """Printed on every report, so the selection effect is never out of
    sight while a number is being read."""
    n = trials_to_date(db_path) if n_trials is None else n_trials
    var = (observed_sharpe_variance(db_path)
           if sharpe_variance is None else sharpe_variance)
    threshold = stats.expected_max_sharpe(max(n, 1), var)
    unit = stats.expected_max_of_n_standard_normals(max(n, 1))
    return (f"trials to date: {n} -- a no-skill best-of-{n} reaches "
            f"SR {threshold:.3f} by luck alone "
            f"({unit:.2f} sigma at unit dispersion)")


def history(limit: int = 50, db_path: str | None = None) -> list[dict]:
    init(db_path)
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM eval_trials ORDER BY id DESC LIMIT ?", (limit,))]
