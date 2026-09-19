"""
The train / dev / vault split, and the only sanctioned way to read
historical data for modelling work.

WHY THIS IS A MODULE AND NOT A CONVENTION
-----------------------------------------
The single most expensive mistake available to this project is to tune a
model against the same data used to judge whether it works, discover an
edge that exists only in that data, and trade real money on it. That
mistake doesn't announce itself — a backtest that has seen its own test
set looks exactly like a good strategy. The only defence that survives
months of iteration is mechanical: the held-out period is unreachable
through the normal data path, and every deliberate exception is written
down at the moment it happens.

So the boundaries below are hard-coded CONSTANTS, not config and not env
vars. A split that can be edited by changing a setting is a split that
will quietly move the first time a result is disappointing.

THE SPLITS (on historical_markets.close_time, the market's own resolution
time — the moment its outcome became knowable)

    TRAIN   close_time <  2026-02-19          25,782 markets
    DEV     2026-02-19 <= close_time < 2026-04-19   10,921 markets
    VAULT   2026-04-19 <= close_time < 2026-07-20   22,430 markets

The backfilled archive runs 2024-09-19 to 2026-07-19, so VAULT is the most
recent contiguous ~3 months of it and DEV the 2 months immediately before.
Chronological, not random: a random split across a time series leaks the
future into training through same-day sibling markets (the same city,
the same day, adjacent temperature brackets all resolve together), and
would flatter every model trained on it.

Markets closing on or after 2026-07-20 — i.e. everything the live bot is
capturing right now — belong to no split at all. They are deliberately
NOT silently folded into TRAIN: live-captured data has a different shape
and different provenance from the backfilled archive (see the data audit),
and quietly mixing the two is its own class of bug. Ask for them
explicitly with split=Split.FUTURE when that's really what you want.

THE VAULT RULE
--------------
No result computed on vault data is valid unless it was the FIRST time
that candidate touched the vault. Look twice and the second number is
not an estimate of out-of-sample performance any more — it's a number
you selected for, and the vault is spent for that idea.

Every allow_vault=True read appends to `vault_access_log` with the
caller, the reason, and how many rows came back. That log is the audit
trail: if it has ten entries for one model family, that model family no
longer has a clean holdout, whatever anyone remembers.
"""
from __future__ import annotations

import inspect
import sqlite3
import time
from enum import Enum

import storage

# --- Hard-coded boundaries. Do not make these configurable. ---
# ISO date strings, compared directly against close_time's ISO-8601 text
# ("2026-07-19T08:00:00Z") — lexicographic order on that format is
# chronological order, so no parsing is needed for the comparison to be
# correct, and half-open [start, end) ranges make the splits exhaustive
# and non-overlapping by construction.
TRAIN_START = "2024-01-01"   # before the earliest backfilled close_time (2024-09-19)
TRAIN_END = "2026-02-19"
DEV_START = "2026-02-19"
DEV_END = "2026-04-19"
VAULT_START = "2026-04-19"
VAULT_END = "2026-07-20"     # exclusive; archive's last close_time is 2026-07-19T08:00Z


class Split(str, Enum):
    TRAIN = "train"
    DEV = "dev"
    TRAIN_DEV = "train+dev"   # the normal "everything I'm allowed to fit on"
    VAULT = "vault"
    FUTURE = "future"         # live-captured markets, after the archive ends


_RANGES = {
    Split.TRAIN: (TRAIN_START, TRAIN_END),
    Split.DEV: (DEV_START, DEV_END),
    Split.TRAIN_DEV: (TRAIN_START, DEV_END),
    Split.VAULT: (VAULT_START, VAULT_END),
    Split.FUTURE: (VAULT_END, "9999-12-31"),
}

# Datasets reachable through the gate. Each maps to (table, how it's
# scoped to a split). Market-keyed tables join through historical_markets
# so a split always means the same thing — the market's close_time —
# rather than each table inventing its own notion of "when".
_MARKET_KEYED = {
    "markets": "historical_markets",
    "price_points": "historical_price_points",
}
# Weather isn't market-keyed (one station's hour serves every market at
# that city that day), so it's scoped by its own observation timestamp
# against the same window.
_TIME_KEYED = {
    "weather_points": ("historical_weather_points", "ts"),
}


class VaultAccessError(RuntimeError):
    """Raised when vault rows are requested without explicit, reasoned
    opt-in. Deliberately not a subclass of ValueError — this should never
    be swallowed by a generic `except ValueError` somewhere upstream."""


def _caller() -> str:
    """file:line of the first frame outside this module — who actually
    asked for the data, not this function."""
    for frame in inspect.stack()[1:]:
        if frame.filename != __file__:
            return f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}"
    return "unknown"


def _iso_to_epoch(iso_date: str) -> int:
    return int(time.mktime(time.strptime(iso_date, "%Y-%m-%d")) - time.timezone)


def load(dataset: str = "markets", split: Split | str = Split.TRAIN,
         allow_vault: bool = False, reason: str | None = None,
         limit: int | None = None) -> list[dict]:
    """
    The single sanctioned read path for historical data.

    dataset: "markets" | "price_points" | "weather_points"
    split:   Split.TRAIN (default) | DEV | TRAIN_DEV | VAULT | FUTURE

    Returns TRAIN by default. Asking for VAULT raises VaultAccessError
    unless allow_vault=True AND a real reason string is supplied; that
    access is then logged to vault_access_log before the rows are handed
    back. allow_vault has no effect on any other split — it can't be left
    switched on in a helper and silently change what a normal call returns.

    >>> load("markets")                       # train rows
    >>> load("markets", split=Split.DEV)      # dev rows
    >>> load("markets", split=Split.VAULT)    # VaultAccessError
    >>> load("markets", split=Split.VAULT, allow_vault=True,
    ...      reason="final eval of candidate temp-momentum-v3, first look")
    """
    split = Split(split)
    if split is Split.VAULT:
        if not allow_vault:
            raise VaultAccessError(
                "Refusing to return VAULT rows. This is the held-out validation "
                f"period ({VAULT_START} to {VAULT_END}) and a result computed on it "
                "is only valid the FIRST time a candidate sees it. If this really is "
                "that first, final evaluation, pass allow_vault=True with a reason "
                "describing exactly which candidate is being evaluated — it will be "
                "logged to vault_access_log."
            )
        if not reason or not reason.strip():
            raise ValueError(
                "allow_vault=True requires a non-empty reason. The reason is the "
                "audit trail — 'which candidate, evaluated for what' — not a formality."
            )
    elif allow_vault:
        raise ValueError(
            f"allow_vault=True was passed with split={split.value}, which doesn't "
            "touch the vault. That combination is almost always a sign the split "
            "argument is wrong; refusing rather than silently ignoring it."
        )

    start, end = _RANGES[split]
    storage.init_db()
    with storage.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        tail = f" LIMIT {int(limit)}" if limit else ""

        if dataset in _MARKET_KEYED:
            table = _MARKET_KEYED[dataset]
            if dataset == "markets":
                sql = (f"SELECT * FROM {table} WHERE close_time >= ? AND close_time < ?"
                       f" ORDER BY close_time{tail}")
            else:
                sql = (f"SELECT p.* FROM {table} p JOIN historical_markets m ON m.ticker = p.ticker "
                       f"WHERE m.close_time >= ? AND m.close_time < ? ORDER BY p.ticker, p.ts{tail}")
            rows = [dict(r) for r in conn.execute(sql, (start, end))]
        elif dataset in _TIME_KEYED:
            table, col = _TIME_KEYED[dataset]
            sql = (f"SELECT * FROM {table} WHERE {col} >= ? AND {col} < ? "
                   f"ORDER BY station_code, {col}{tail}")
            rows = [dict(r) for r in conn.execute(sql, (_iso_to_epoch(start), _iso_to_epoch(end)))]
        else:
            raise ValueError(
                f"Unknown dataset {dataset!r}. Known: "
                f"{sorted(list(_MARKET_KEYED) + list(_TIME_KEYED))}. Add it here rather "
                "than querying the table directly — a query that bypasses this function "
                "also bypasses the vault."
            )

        if split is Split.VAULT:
            conn.execute(
                "INSERT INTO vault_access_log (ts, caller, reason, dataset, rows_returned) "
                "VALUES (?,?,?,?,?)",
                (int(time.time()), _caller(), reason.strip(), dataset, len(rows)),
            )
    return rows


def vault_access_history() -> list[dict]:
    """Every vault read ever made, oldest first. Read this before trusting
    any out-of-sample number: if the candidate being evaluated already
    appears here, that number is not out-of-sample any more."""
    storage.init_db()
    with storage.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM vault_access_log ORDER BY ts, id")]


def describe() -> str:
    """Human-readable summary of the boundaries — for printing at the top
    of any analysis so a saved notebook/log says which split it used."""
    return (f"TRAIN [{TRAIN_START}, {TRAIN_END})  "
            f"DEV [{DEV_START}, {DEV_END})  "
            f"VAULT [{VAULT_START}, {VAULT_END})  (on historical_markets.close_time)")


if __name__ == "__main__":
    print(describe())
    for s in (Split.TRAIN, Split.DEV, Split.TRAIN_DEV):
        print(f"  {s.value:<10} {len(load('markets', split=s)):>7,} markets")
    print(f"  {'vault':<10} (blocked by default — allow_vault=True + reason required)")
    history = vault_access_history()
    print(f"\nvault accesses logged so far: {len(history)}")
    for h in history:
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(h['ts']))}  {h['caller']:<24} "
              f"{h['dataset']:<14} {h['rows_returned']:>8,}  {h['reason']}")
