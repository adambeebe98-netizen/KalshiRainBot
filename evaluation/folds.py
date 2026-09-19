"""
Purged, embargoed walk-forward folds.

Ordinary cross-validation shuffles rows. On this data that is not merely
imprecise, it is a leak: a market that opens on Monday and settles on
Wednesday carries information about Wednesday. Put it in the training set
while testing on Wednesday and the model is being handed the answer.

Two mechanisms, both explicit parameters recorded in every fold:

**PURGE.** A market's label period is `[open_time, close_time]` -- the
outcome is determined across that whole span, not at a point. Any training
market whose label period overlaps the test window is dropped:

    open_time < test_end AND close_time > test_start

With a median market lifetime of 39 hours this removes roughly the last
two days of each training window. Small, and exactly the rows that leak.

**EMBARGO.** After a test window, training does not resume for a further
stretch of wall-clock time. This is not about direct label overlap -- purge
handles that -- but about serial correlation: the same weather system and
the same bracket set persist across the boundary, so the first days after a
test window are close to being the test window again.

Folds are generated over TRAIN and DEV only. A fold window intersecting
the vault is a bug, and `generate` raises rather than returning one.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import splits

SECONDS_PER_HOUR = 3600
DEFAULT_PURGE_S = 48 * SECONDS_PER_HOUR    # > p90 market lifetime (41h)
DEFAULT_EMBARGO_S = 24 * SECONDS_PER_HOUR


class VaultOverlapError(RuntimeError):
    """A generated fold reached into the held-out period. Always a bug."""


def to_epoch(value: str | int | None) -> int | None:
    """Accepts a unix int, an ISO datetime, or a bare YYYY-MM-DD date.

    Date-only strings are read as UTC midnight explicitly. Letting them
    fall through to local time would shift every split boundary by the
    host's timezone offset, which is the kind of error that produces a
    backtest off by a few hours at every fold edge and never announces
    itself.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        text = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: int
    train_end: int          # exclusive; the purge boundary, not the test start
    test_start: int
    test_end: int           # exclusive
    train_tickers: tuple[str, ...]
    test_tickers: tuple[str, ...]
    purge_seconds: int
    embargo_seconds: int
    n_purged: int           # training markets dropped by the purge
    n_embargoed: int        # training markets dropped by an earlier embargo
    purged_tickers: tuple[str, ...] = field(default=(), repr=False)

    def summary(self) -> str:
        def stamp(t):
            return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
        return (f"fold {self.index}: train {stamp(self.train_start)}"
                f"->{stamp(self.train_end)} ({len(self.train_tickers)} markets, "
                f"{self.n_purged} purged, {self.n_embargoed} embargoed) | "
                f"test {stamp(self.test_start)}->{stamp(self.test_end)} "
                f"({len(self.test_tickers)} markets)")


def _label_period(market: dict) -> tuple[int | None, int | None]:
    return to_epoch(market.get("open_time")), to_epoch(market.get("close_time"))


def _vault_bounds() -> tuple[int, int]:
    return to_epoch(splits.VAULT_START), to_epoch(splits.VAULT_END)


def generate(markets: list[dict], n_folds: int = 5,
             purge_seconds: int = DEFAULT_PURGE_S,
             embargo_seconds: int = DEFAULT_EMBARGO_S,
             min_train_markets: int = 50,
             min_test_markets: int = 10) -> list[Fold]:
    """Expanding-window walk-forward folds over `markets`.

    The timeline is cut into `n_folds + 1` equal spans of wall-clock time.
    Fold k tests on span k+1 and trains on everything before it, after
    purging and embargoing. Expanding rather than sliding because there is
    not much data and discarding early history to keep the window a fixed
    size would cost more than the staleness it avoids.

    Folds with too little train or test data are dropped rather than
    returned undersized -- a fold with six test markets produces a number
    that looks like a result and is not one.
    """
    if n_folds < 1:
        raise ValueError("n_folds must be >= 1")
    if purge_seconds < 0 or embargo_seconds < 0:
        raise ValueError("purge and embargo must be non-negative")

    dated = []
    for m in markets:
        opened, closed = _label_period(m)
        if opened is None or closed is None or not m.get("ticker"):
            continue
        dated.append((m["ticker"], opened, closed))
    if not dated:
        raise ValueError("no markets with usable open_time and close_time")

    first = min(o for _, o, _ in dated)
    last = max(c for _, _, c in dated)
    if last <= first:
        raise ValueError("markets span no time")

    vault_start, vault_end = _vault_bounds()
    span = (last - first) / (n_folds + 1)

    out: list[Fold] = []
    for k in range(n_folds):
        test_start = int(first + span * (k + 1))
        test_end = int(first + span * (k + 2)) if k < n_folds - 1 else last + 1

        if vault_start is not None and test_start < vault_end and test_end > vault_start:
            raise VaultOverlapError(
                f"fold {k} test window [{test_start}, {test_end}) intersects the "
                f"vault [{vault_start}, {vault_end}). Folds must be generated "
                f"from TRAIN/DEV markets only.")

        # Training universe: everything whose label period ended before the
        # test window opens. The purge then removes anything still running
        # into it, and the embargo removes the run-up to it.
        train_cut = test_start - purge_seconds
        embargo_from = test_start - purge_seconds - embargo_seconds

        train, purged, embargoed = [], [], 0
        for ticker, opened, closed in dated:
            if opened >= test_start:
                continue                      # not yet open when testing starts
            if opened < test_end and closed > test_start:
                purged.append(ticker)         # label period straddles the window
                continue
            if closed > train_cut:
                purged.append(ticker)         # settles inside the purge gap
                continue
            if embargo_seconds and closed > embargo_from:
                embargoed += 1                # inside the embargo run-up
                continue
            train.append(ticker)

        test = [t for t, opened, closed in dated
                if test_start <= closed < test_end]

        if len(train) < min_train_markets or len(test) < min_test_markets:
            continue

        out.append(Fold(
            index=len(out),
            train_start=first,
            train_end=min(embargo_from, train_cut),
            test_start=test_start, test_end=test_end,
            train_tickers=tuple(train), test_tickers=tuple(test),
            purge_seconds=purge_seconds, embargo_seconds=embargo_seconds,
            n_purged=len(purged), n_embargoed=embargoed,
            purged_tickers=tuple(purged),
        ))

    if not out:
        raise ValueError(
            f"no fold met the minimums (train >= {min_train_markets}, "
            f"test >= {min_test_markets}) across {len(dated)} markets. "
            f"Reduce n_folds or the minimums rather than accepting thin folds.")
    return out


def generate_from_splits(split: str = "train+dev", **kwargs) -> list[Fold]:
    """Convenience loader. Deliberately cannot reach the vault: the split
    is passed through to splits.load without allow_vault, so asking for
    the vault here raises VaultAccessError from splits itself."""
    markets = splits.load("markets", split=split)
    return generate(markets, **kwargs)


def describe(folds: list[Fold]) -> str:
    lines = [f"{len(folds)} folds, "
             f"purge={folds[0].purge_seconds // SECONDS_PER_HOUR}h "
             f"embargo={folds[0].embargo_seconds // SECONDS_PER_HOUR}h"]
    lines.extend(f.summary() for f in folds)
    total_purged = sum(f.n_purged for f in folds)
    total_embargoed = sum(f.n_embargoed for f in folds)
    lines.append(f"total dropped: {total_purged} purged, {total_embargoed} embargoed")
    return "\n".join(lines)
