"""
A population of candidate forecasters, for searching at scale.

Organised into FAMILIES rather than as one flat pile of parameters. A
thousand variants of one idea tells you almost nothing -- they share their
mistakes, and the best of them is the luckiest draw from a single
distribution. Six families of a hundred and fifty tells you which IDEAS
survive contact with fees and out-of-sample data, which is the question
worth a thousand trials.

Every family reads only through the PointInTimeView and returns a
probability, so the shared ProbabilityTrader turns all of them into
decisions the same way. The comparison between families is then about
forecasting rather than about who was allowed to trade more aggressively.

Note on what this costs. Every candidate generated here becomes a
permanent row in eval_trials, and the luck threshold every FUTURE
candidate must clear rises with the count. That is the intended
mechanism, not a side effect: a search this size will throw up
spectacular-looking results by chance, and the whole point is that the
bar moves to match.
"""
from __future__ import annotations

from evaluation import baselines

HOUR = 3600


def _last_price(view, ticker):
    points = view.price_points(ticker)
    if not points:
        return None, None
    last = points[-1]
    bid, ask = last.get("yes_bid_cents"), last.get("yes_ask_cents")
    if bid is None or ask is None:
        close = last.get("yes_price_cents")
        return (close, points) if close is not None else (None, points)
    return (bid + ask) / 2.0, points


class PriceLevelForecaster(baselines.Forecaster):
    """The market is systematically biased by a fixed amount.

    The generalisation of the finding in analysis/horizon_calibration.py,
    which measured NYC rain pricing YES 4.6 to 10.1 points above its
    realized rate. A negative shift fades that; a positive one follows it.
    """

    kind = "price_level"

    def __init__(self, shift: float):
        self.shift = shift
        self.name = f"price_level(shift={shift:+.2f})"

    def probability(self, view, terms, as_of):
        mid, _ = _last_price(view, terms.ticker)
        if mid is None:
            return None
        return min(max(mid / 100.0 + self.shift, 0.001), 0.999)


class MomentumForecaster(baselines.Forecaster):
    """Price moved over a lookback -- follow it, or fade it.

    `strength` positive follows the move, negative fades it. Both
    directions are in the population deliberately: a search that only
    tries one has assumed its answer.
    """

    kind = "momentum"

    def __init__(self, lookback_h: int, strength: float):
        self.lookback_h = lookback_h
        self.strength = strength
        self.name = f"momentum(lookback={lookback_h}h,k={strength:+.2f})"

    def probability(self, view, terms, as_of):
        mid, points = _last_price(view, terms.ticker)
        if mid is None or not points:
            return None
        cutoff = as_of - self.lookback_h * HOUR
        earlier = [p for p in points if p["ts"] <= cutoff]
        if not earlier:
            return None
        past = earlier[-1].get("yes_price_cents")
        if past is None:
            return None
        move = (mid - past) / 100.0
        return min(max(mid / 100.0 + self.strength * move, 0.001), 0.999)


class MeanReversionForecaster(baselines.Forecaster):
    """Price far from its own recent average -- pull the forecast back."""

    kind = "mean_reversion"

    def __init__(self, window_h: int, pull: float):
        self.window_h = window_h
        self.pull = pull
        self.name = f"mean_reversion(window={window_h}h,pull={pull:.2f})"

    def probability(self, view, terms, as_of):
        mid, points = _last_price(view, terms.ticker)
        if mid is None or not points:
            return None
        cutoff = as_of - self.window_h * HOUR
        window = [p["yes_price_cents"] for p in points
                  if p["ts"] >= cutoff and p.get("yes_price_cents") is not None]
        if len(window) < 3:
            return None
        avg = sum(window) / len(window)
        target = mid + self.pull * (avg - mid)
        return min(max(target / 100.0, 0.001), 0.999)


class SpreadFilterForecaster(baselines.Forecaster):
    """Only express a view when the book is tight enough to trade.

    Wraps another forecaster. A wide spread is where a backtest's
    optimism and reality diverge most, so refusing to trade there is a
    hypothesis worth testing rather than a foregone conclusion.
    """

    kind = "spread_filter"

    def __init__(self, inner: baselines.Forecaster, max_spread_cents: int):
        self.inner = inner
        self.max_spread_cents = max_spread_cents
        self.name = f"spread<={max_spread_cents}c+{inner.name}"

    def probability(self, view, terms, as_of):
        points = view.price_points(terms.ticker)
        if not points:
            return None
        last = points[-1]
        bid, ask = last.get("yes_bid_cents"), last.get("yes_ask_cents")
        if bid is None or ask is None or (ask - bid) > self.max_spread_cents:
            return None
        return self.inner.probability(view, terms, as_of)


class ExtremeFadeForecaster(baselines.Forecaster):
    """Fade only the extremes of the book.

    The favourite-longshot pattern: if very cheap contracts are
    systematically overpriced, a fade confined to them should show it
    while leaving the middle of the book alone.
    """

    kind = "extreme_fade"

    def __init__(self, threshold_cents: int, shift: float):
        self.threshold_cents = threshold_cents
        self.shift = shift
        self.name = f"extreme_fade(beyond={threshold_cents}c,shift={shift:+.2f})"

    def probability(self, view, terms, as_of):
        mid, _ = _last_price(view, terms.ticker)
        if mid is None:
            return None
        if self.threshold_cents < mid < (100 - self.threshold_cents):
            return None                      # middle of the book: no view
        direction = -1.0 if mid > 50 else 1.0
        return min(max(mid / 100.0 + direction * self.shift, 0.001), 0.999)


class TimeWindowForecaster(baselines.Forecaster):
    """A fixed bias, but only inside a window before close.

    Separates "the market is wrong" from "the market is wrong at a
    particular moment", which the horizon analysis suggested might differ.
    """

    kind = "time_window"

    def __init__(self, inner: baselines.Forecaster, min_h: float, max_h: float):
        self.inner = inner
        self.min_h = min_h
        self.max_h = max_h
        self.name = f"window({min_h:g}-{max_h:g}h)+{inner.name}"

    def probability(self, view, terms, as_of):
        from evaluation import pit
        close = pit.iso_to_epoch(terms.close_time)
        if close is None:
            return None
        hours = (close - as_of) / HOUR
        if not (self.min_h <= hours <= self.max_h):
            return None
        return self.inner.probability(view, terms, as_of)


class VolumeFilterForecaster(baselines.Forecaster):
    """Only act where something actually traded this hour.

    38.6% of archive hours have zero volume, and a fill assumed in a
    hour where nothing traded is the most optimistic thing a backtest can
    invent. This asks whether confining a view to liquid hours is worth
    the trades it gives up.
    """

    kind = "volume_filter"

    def __init__(self, inner: baselines.Forecaster, min_volume: int):
        self.inner = inner
        self.min_volume = min_volume
        self.name = f"vol>={min_volume}+{inner.name}"

    def probability(self, view, terms, as_of):
        points = view.price_points(terms.ticker)
        if not points or (points[-1].get("volume") or 0) < self.min_volume:
            return None
        return self.inner.probability(view, terms, as_of)


class VolatilityForecaster(baselines.Forecaster):
    """Trade on how much the price has been moving, not where it is.

    A market that has been thrashing is one where the crowd disagrees,
    which is a different situation from a quiet market at the same price
    -- and the two are indistinguishable to every other family here.
    """

    kind = "volatility"

    def __init__(self, window_h: int, threshold_c: float, shift: float):
        self.window_h = window_h
        self.threshold_c = threshold_c
        self.shift = shift
        self.name = (f"volatility(window={window_h}h,"
                     f"move>={threshold_c:g}c,shift={shift:+.2f})")

    def probability(self, view, terms, as_of):
        mid, points = _last_price(view, terms.ticker)
        if mid is None or not points:
            return None
        cutoff = as_of - self.window_h * HOUR
        window = [p["yes_price_cents"] for p in points
                  if p["ts"] >= cutoff and p.get("yes_price_cents") is not None]
        if len(window) < 3:
            return None
        if (max(window) - min(window)) < self.threshold_c:
            return None                     # too quiet to have a view
        return min(max(mid / 100.0 + self.shift, 0.001), 0.999)


class BracketRelativeForecaster(baselines.Forecaster):
    """Price this leg against the rest of its own event.

    In a one-of-n set the legs must sum to certainty. When they do not,
    every leg is mispriced by its share of the gap -- which is a view no
    single-market family here can express, because it needs the siblings.
    """

    kind = "bracket_relative"

    def __init__(self, strength: float, min_gap_c: float):
        self.strength = strength
        self.min_gap_c = min_gap_c
        self.name = (f"bracket_relative(k={strength:+.2f},"
                     f"gap>={min_gap_c:g}c)")

    def probability(self, view, terms, as_of):
        mid, _ = _last_price(view, terms.ticker)
        if mid is None or not terms.event_ticker:
            return None
        siblings = getattr(view, "_sibling_cache", None)
        if siblings is None:
            return None                     # caller did not supply the set
        legs = siblings.get(terms.event_ticker)
        if not legs or len(legs) < 2:
            return None
        total = 0.0
        for ticker in legs:
            m, _ = _last_price(view, ticker)
            if m is None:
                return None
            total += m
        gap = total - 100.0
        if abs(gap) < self.min_gap_c:
            return None
        share = gap / len(legs)
        return min(max((mid - self.strength * share) / 100.0, 0.001), 0.999)


def build_population(config=None) -> list:
    """The full search population, wrapped in the shared trader."""
    forecasters: list[baselines.Forecaster] = []

    # Family 1: a flat bias, swept both ways.
    for shift in [round(-0.30 + 0.02 * i, 2) for i in range(31)]:
        if abs(shift) > 1e-9:
            forecasters.append(PriceLevelForecaster(shift))

    # Family 2: momentum and its fade.
    for lookback in (2, 4, 6, 12, 24, 36):
        for k in (-1.5, -1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 1.5):
            forecasters.append(MomentumForecaster(lookback, k))

    # Family 3: mean reversion.
    for window in (3, 6, 12, 24, 48):
        for pull in (0.1, 0.25, 0.5, 0.75, 1.0):
            forecasters.append(MeanReversionForecaster(window, pull))

    # Family 4: the extremes only.
    for threshold in (5, 10, 15, 20, 25, 30):
        for shift in (0.02, 0.05, 0.10, 0.15, 0.20):
            forecasters.append(ExtremeFadeForecaster(threshold, shift))

    # Family 5: spread-filtered versions of the strongest flat biases.
    for max_spread in (1, 2, 3, 5, 8):
        for shift in (-0.15, -0.10, -0.05, 0.05, 0.10, 0.15):
            forecasters.append(
                SpreadFilterForecaster(PriceLevelForecaster(shift), max_spread))

    # Family 6: the same biases confined to a window before close.
    for lo, hi in ((0, 6), (6, 12), (12, 24), (24, 36), (0, 12), (12, 36)):
        for shift in (-0.15, -0.10, -0.05, 0.05, 0.10, 0.15):
            forecasters.append(
                TimeWindowForecaster(PriceLevelForecaster(shift), lo, hi))

    # Family 7: only where something traded.
    for min_volume in (1, 5, 20, 50, 200):
        for shift in (-0.15, -0.10, -0.05, 0.05, 0.10, 0.15):
            forecasters.append(
                VolumeFilterForecaster(PriceLevelForecaster(shift), min_volume))
        for lookback, k in ((6, 0.5), (12, 0.5), (12, 0.25), (24, -0.5)):
            forecasters.append(VolumeFilterForecaster(
                MomentumForecaster(lookback, k), min_volume))

    # Family 8: realised movement as the signal.
    for window in (3, 6, 12, 24):
        for threshold in (3, 6, 10, 20):
            for shift in (-0.15, -0.08, 0.08, 0.15):
                forecasters.append(
                    VolatilityForecaster(window, threshold, shift))

    # Family 9: the leg priced against its own event.
    for strength in (0.25, 0.5, 0.75, 1.0, 1.5):
        for gap in (2, 5, 10, 20):
            forecasters.append(BracketRelativeForecaster(strength, gap))

    # Family 10: momentum inside a window, and momentum on a tight book --
    # the two filters that matter most, applied to the family that showed
    # the strongest raw result rather than to a flat bias.
    for lo, hi in ((0, 6), (6, 12), (12, 24), (24, 36)):
        for lookback in (2, 6, 12, 24):
            for k in (-0.5, 0.25, 0.5, 1.0):
                forecasters.append(TimeWindowForecaster(
                    MomentumForecaster(lookback, k), lo, hi))
    for max_spread in (1, 2, 3, 5):
        for lookback in (2, 6, 12, 24):
            for k in (-0.5, 0.25, 0.5, 1.0):
                forecasters.append(SpreadFilterForecaster(
                    MomentumForecaster(lookback, k), max_spread))

    # Family 11: mean reversion under the same two filters.
    for max_spread in (2, 5):
        for window in (3, 6, 12, 24):
            for pull in (0.25, 0.5, 0.75, 1.0):
                forecasters.append(SpreadFilterForecaster(
                    MeanReversionForecaster(window, pull), max_spread))
    for lo, hi in ((0, 12), (12, 36)):
        for window in (3, 6, 12, 24):
            for pull in (0.25, 0.5, 0.75, 1.0):
                forecasters.append(TimeWindowForecaster(
                    MeanReversionForecaster(window, pull), lo, hi))

    return [baselines.ProbabilityTrader(f, config) for f in forecasters]
