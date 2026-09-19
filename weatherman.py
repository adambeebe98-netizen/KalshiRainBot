"""
Layer 1: predict what the gauge will report, not what a market will pay.

The design decision that matters is the training set. There are 612
historical daily-rain markets and 554 of them are New York, which is
nowhere near enough to learn rain from. But the question "will this gauge
record any precipitation over this window" does not need a market to have
existed -- it needs weather. The archive holds 410,975 observations and
forecasts at known lead times across 23 stations, which is roughly 17,000
labelled station-days.

So the label comes from `wx_observations`, not from Kalshi. That is
legitimate precisely because the observation archive was validated
against 462 real settlements at 98.7% (analysis/validate_wx.py) -- it
reproduces the thing the contracts pay on, including trace.

The target is `sum(precip_in) > 0` over the window, with trace counting,
because that is what "strictly greater than 0 inches" means. A model
trained on measurable rain (>= 0.01in) would be predicting an event 7
points rarer than the one being paid on.

Pure Python: no numpy, no scipy, no sklearn on the droplet, and this does
not add them. Logistic regression fitted by gradient descent, which for a
handful of features and ~17k rows is a second of compute and has the
property that matters most here -- it produces calibrated probabilities
rather than opinions, so Layer 2 can size on them.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from dataclasses import dataclass, field

from config import SETTINGS

HOUR = 3600
DAY = 86400

FEATURE_NAMES = (
    "fc_precip_sum_mm",      # forecast total over the window
    "fc_precip_max_mm",      # wettest forecast hour
    "fc_precip_hours",       # how many hours are forecast wet
    "fc_pop_max",            # peak precipitation probability
    "fc_pop_mean",           # mean probability
    "obs_precip_prior_24h",  # what actually fell in the preceding day
    "obs_wet_hours_prior_72h",
    "climo_wet_rate",        # station-month base rate, fit on training data
    "season_sin",
    "season_cos",
)


def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

def utc_offset_hours(lon: float) -> int:
    """Approximate standard-time offset from longitude.

    Used only to place a station's daily window at roughly local midnight,
    which is what the CLI day runs on. Deliberately ignores daylight
    saving: the window then sits an hour off for part of the year, which
    blurs a few boundary hours rather than misattributing a day. Markets
    we actually have are scored against their own close times, where this
    approximation is not used at all.

    West longitudes are negative and so are their offsets: New York at
    -73.97 is UTC-5. The first version negated this, which put every
    station's "local midnight" 14 hours out and silently trained the model
    on windows that straddled two calendar days.
    """
    return round(lon / 15.0)


def station_windows(station: str, start_ts: int, end_ts: int,
                    db_path: str | None = None) -> list[tuple[int, int]]:
    """Daily [start, end) windows at local midnight for one station."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT lon FROM wx_stations WHERE station = ?",
                           (station,)).fetchone()
    if row is None:
        return []
    offset = utc_offset_hours(row["lon"])
    midnight_utc = (-offset) % 24
    out = []
    day = dt.datetime.fromtimestamp(start_ts, dt.timezone.utc).date()
    last = dt.datetime.fromtimestamp(end_ts, dt.timezone.utc).date()
    while day <= last:
        lo = int(dt.datetime(day.year, day.month, day.day, midnight_utc % 24,
                             tzinfo=dt.timezone.utc).timestamp())
        out.append((lo, lo + DAY))
        day += dt.timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------

@dataclass
class ArchiveCache:
    """Everything one station needs, loaded once and indexed for slicing.

    The dicts are kept because callers build them directly in tests and
    the adapter, but every window query goes through a sorted index and
    bisect. Scanning the whole dict per window was O(n) per window --
    about 18,000 entries times 730 windows times 23 stations, which is
    300 million operations and turned training into something that timed
    out rather than finished.
    """
    obs: dict[int, float] = field(default_factory=dict)
    fc_precip: dict[int, float] = field(default_factory=dict)
    fc_pop: dict[int, float] = field(default_factory=dict)
    _index: dict = field(default_factory=dict, repr=False)

    def _sorted(self, name: str):
        source = getattr(self, name)
        cached = self._index.get(name)
        if cached is None or cached[2] != len(source):
            keys = sorted(source)
            cached = (keys, [source[k] for k in keys], len(source))
            self._index[name] = cached
        return cached[0], cached[1]

    def between(self, name: str, lo: int, hi: int) -> list[float]:
        """Values whose timestamp falls in [lo, hi)."""
        import bisect
        keys, values = self._sorted(name)
        return values[bisect.bisect_left(keys, lo):bisect.bisect_left(keys, hi)]

    def pairs_between(self, name: str, lo: int, hi: int):
        import bisect
        keys, values = self._sorted(name)
        i, j = bisect.bisect_left(keys, lo), bisect.bisect_left(keys, hi)
        return zip(keys[i:j], values[i:j])

    @classmethod
    def load(cls, station: str, lead_hours: int, db_path: str | None = None):
        c = cls()
        with _connect(db_path) as conn:
            for ts, v in conn.execute(
                    "SELECT valid_at, precip_in FROM wx_observations "
                    "WHERE station = ? AND precip_in IS NOT NULL", (station,)):
                c.obs[ts] = v
            for ts, p, pop in conn.execute(
                    "SELECT valid_at, precip_mm, precip_prob_pct "
                    "FROM wx_forecasts WHERE station = ? AND lead_hours = ?",
                    (station, lead_hours)):
                if p is not None:
                    c.fc_precip[ts] = p
                if pop is not None:
                    c.fc_pop[ts] = pop
        return c


def features_for(cache: ArchiveCache, lo: int, hi: int, climo: float,
                 lead_hours: int) -> list[float] | None:
    """Features for the window [lo, hi), using only what a decision at
    `lo` could have seen.

    Returns None when the forecast is absent, rather than substituting
    zero -- "no forecast" and "a forecast of no rain" are different
    states, and conflating them teaches the model that missing data means
    dry.
    """
    cutoff = lo   # the decision moment
    fc_hours = [v for t, v in cache.pairs_between("fc_precip", lo, hi)
                if (t - lead_hours * HOUR) <= cutoff]
    if not fc_hours:
        return None
    pops = [v for t, v in cache.pairs_between("fc_pop", lo, hi)
            if (t - lead_hours * HOUR) <= cutoff]

    prior_24 = sum(cache.between("obs", lo - DAY, lo))
    prior_wet_72 = sum(1 for v in cache.between("obs", lo - 3 * DAY, lo) if v > 0)

    when = dt.datetime.fromtimestamp(lo, dt.timezone.utc)
    doy = when.timetuple().tm_yday
    angle = 2 * math.pi * doy / 365.25

    return [
        sum(fc_hours),
        max(fc_hours),
        float(sum(1 for v in fc_hours if v > 0)),
        (max(pops) / 100.0) if pops else 0.0,
        (sum(pops) / len(pops) / 100.0) if pops else 0.0,
        prior_24,
        float(prior_wet_72),
        climo,
        math.sin(angle),
        math.cos(angle),
    ]


def label_for_window(cache: ArchiveCache, lo: int, hi: int) -> float | None:
    """1.0 if any precipitation was recorded in the window, trace included.

    None when the window has no observations at all -- an unobserved day
    is not a dry day.
    """
    hours = cache.between("obs", lo, hi)
    if not hours:
        return None
    return 1.0 if sum(hours) > 0 else 0.0


# --------------------------------------------------------------------------
# Logistic regression
# --------------------------------------------------------------------------

def fit_climatology(stations, train_end_ts: int,
                    db_path: str | None = None) -> dict[tuple[str, int], float]:
    """P(any precipitation) per station and calendar month.

    Fitted only on observations before `train_end_ts`. A climatology that
    included the evaluation period would be a fitted value quietly
    describing the thing it is being used to predict -- the same mistake
    as grading a "constant" baseline against the test window's own base
    rate.
    """
    counts: dict[tuple[str, int], list[int]] = {}
    for station in stations:
        cache = ArchiveCache.load(station, lead_hours=24, db_path=db_path)
        for lo, hi in station_windows(
                station, min(cache.obs) if cache.obs else 0,
                train_end_ts, db_path=db_path):
            if hi > train_end_ts:
                break
            y = label_for_window(cache, lo, hi)
            if y is None:
                continue
            month = dt.datetime.fromtimestamp(lo, dt.timezone.utc).month
            wet, total = counts.setdefault((station, month), [0, 0])
            counts[(station, month)] = [wet + int(y), total + 1]
    return {k: (v[0] / v[1]) for k, v in counts.items() if v[1] >= 5}


def build_dataset(stations, lead_hours: int, start_ts: int, end_ts: int,
                  climo: dict, db_path: str | None = None):
    """Rows, labels and provenance for every usable station-day."""
    rows, labels, meta = [], [], []
    for station in stations:
        cache = ArchiveCache.load(station, lead_hours, db_path=db_path)
        if not cache.obs or not cache.fc_precip:
            continue
        default_climo = (sum(1 for v in cache.obs.values() if v > 0)
                         / max(1, len(cache.obs)))
        for lo, hi in station_windows(station, start_ts, end_ts, db_path=db_path):
            if lo < start_ts or hi > end_ts:
                continue
            y = label_for_window(cache, lo, hi)
            if y is None:
                continue
            month = dt.datetime.fromtimestamp(lo, dt.timezone.utc).month
            x = features_for(cache, lo, hi,
                             climo.get((station, month), default_climo),
                             lead_hours)
            if x is None:
                continue
            rows.append(x)
            labels.append(y)
            meta.append({"station": station, "window_start": lo})
    return rows, labels, meta


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


@dataclass
class LogisticModel:
    weights: list[float]
    bias: float
    mean: list[float]
    std: list[float]
    feature_names: tuple = FEATURE_NAMES

    def predict(self, x: list[float]) -> float:
        z = self.bias
        for w, v, m, s in zip(self.weights, x, self.mean, self.std):
            z += w * ((v - m) / s)
        return _sigmoid(z)

    def to_json(self) -> str:
        return json.dumps({"weights": self.weights, "bias": self.bias,
                           "mean": self.mean, "std": self.std,
                           "feature_names": list(self.feature_names)},
                          sort_keys=True)

    @classmethod
    def from_json(cls, text: str):
        d = json.loads(text)
        return cls(weights=d["weights"], bias=d["bias"], mean=d["mean"],
                   std=d["std"], feature_names=tuple(d["feature_names"]))

    def describe(self) -> str:
        pairs = sorted(zip(self.feature_names, self.weights),
                       key=lambda kv: -abs(kv[1]))
        lines = [f"  bias {self.bias:+.4f}"]
        lines += [f"  {n:<24} {w:+.4f}" for n, w in pairs]
        return "\n".join(lines)


def fit(rows: list[list[float]], labels: list[float], *,
        iterations: int = 600, learning_rate: float = 1.0,
        l2: float = 1e-3) -> LogisticModel:
    """Standardise, then batch gradient descent on the log loss.

    Standardising matters more than it looks: the features here span
    millimetres of rain, counts of hours and unit-scale probabilities, and
    an unscaled fit lets whichever feature happens to have the largest
    units dominate the first few hundred steps.
    """
    if not rows:
        raise ValueError("cannot fit on an empty dataset")
    n, d = len(rows), len(rows[0])
    mean = [sum(r[j] for r in rows) / n for j in range(d)]
    std = []
    for j in range(d):
        var = sum((r[j] - mean[j]) ** 2 for r in rows) / n
        std.append(math.sqrt(var) if var > 1e-12 else 1.0)
    scaled = [[(r[j] - mean[j]) / std[j] for j in range(d)] for r in rows]

    w = [0.0] * d
    b = 0.0
    for _ in range(iterations):
        gw = [0.0] * d
        gb = 0.0
        for x, y in zip(scaled, labels):
            z = b + sum(w[j] * x[j] for j in range(d))
            err = _sigmoid(z) - y
            gb += err
            for j in range(d):
                gw[j] += err * x[j]
        b -= learning_rate * gb / n
        for j in range(d):
            w[j] -= learning_rate * (gw[j] / n + l2 * w[j])
    return LogisticModel(weights=w, bias=b, mean=mean, std=std)
