"""
The baselines a candidate has to beat.

The structure here matters more than any individual baseline. A forecaster
produces a probability; a trader turns a probability into a decision. The
SAME trader wraps every forecaster, so a candidate and a baseline face
identical execution, identical fees, identical sizing and identical edge
thresholds. The only thing that differs is the number being forecast.

Without that, "my candidate beat the baseline" can quietly mean "my
candidate traded more aggressively than the baseline", which is not a
finding about forecasting at all.

Three baselines are specified (DESIGN.md section 8). Two are here:

1. **Market price.** The market-implied probability is the forecast. This
   is a strong opponent, not a formality -- measured Brier 0.1391 at 24h
   on NYC daily rain against 0.2469 for a constant. Beating it is the bar.
2. **Constant base rate, fit on TRAIN only.** Fitted once per fold from
   training markets and held fixed across every test window in that fold.
   The test window's own base rate is never computed, because it is only
   knowable in hindsight and grading against it flatters the baseline.

The third -- the best existing heuristic -- is NOT implemented here, and
deliberately not faked. See HeuristicBaseline below.
"""
from __future__ import annotations

from dataclasses import dataclass

from evaluation import pit
from evaluation.execution import Decision


class Forecaster:
    """Produces a probability that a market settles YES, or None to
    decline. Declining is a real answer: 'I have no view here' is very
    different from 'I think it is a coin flip', and collapsing the two
    would have a forecaster trading every market on the board."""

    name: str = "unnamed"

    # A stable identifier, unlike `name`, which may embed a fitted value.
    # The harness groups fold results by `kind`: the constant baseline is
    # refit per fold, so its name differs per fold, and keying on the name
    # split one baseline into several partial ones -- each with a fraction
    # of the trades, and each an easier target than the real thing.
    kind: str = "unnamed"

    def probability(self, view, terms, as_of: int) -> float | None:
        raise NotImplementedError


# --------------------------------------------------------------------------
# Baseline 1: the market itself
# --------------------------------------------------------------------------

class MarketForecaster(Forecaster):
    """The market-implied probability, taken as the mid of the touch.

    Used for Brier comparison. As a TRADING baseline it necessarily
    produces no trades -- a forecaster that agrees with the price by
    construction never finds edge -- which makes the trading hurdle simply
    'net P&L above zero after fees'. That is the correct hurdle: paying
    the ask and beating nothing is not edge.
    """

    name = "market_price"
    kind = "market_price"

    def probability(self, view, terms, as_of: int) -> float | None:
        points = view.price_points(terms.ticker)
        if not points:
            return None
        last = points[-1]
        bid, ask = last.get("yes_bid_cents"), last.get("yes_ask_cents")
        if bid is None or ask is None:
            close = last.get("yes_price_cents")
            return close / 100.0 if close is not None else None
        return (bid + ask) / 200.0


# --------------------------------------------------------------------------
# Baseline 2: a constant, fit out of sample
# --------------------------------------------------------------------------

def fit_base_rate(train_tickers, db_path: str | None = None) -> float:
    """P(yes) over settled training markets.

    Takes an explicit ticker list rather than a date range so that the
    caller cannot accidentally hand it the test window. Markets resolving
    'scalar' are excluded by label_for and simply do not count.
    """
    yes = total = 0
    for ticker in train_tickers:
        label = pit.label_for(ticker, db_path=db_path)
        if label is None:
            continue
        total += 1
        yes += 1 if label.result == "yes" else 0
    if total == 0:
        raise ValueError(
            "no settled markets in the training set -- cannot fit a base "
            "rate, and defaulting to 0.5 would invent a forecast")
    return yes / total


class ConstantBaseRateForecaster(Forecaster):
    """A fixed probability for every market, fitted on training data only.

    The fitted value is frozen at construction. It is not recomputed
    per-market, per-test-window or per-anything, and a test asserts it is
    identical across every test window of a fold -- the failure mode being
    a 'constant' baseline that quietly peeks at the period it is grading.
    """

    def __init__(self, rate: float, fitted_on: int = 0):
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"base rate must be a probability, got {rate}")
        self.rate = rate
        self.fitted_on = fitted_on
        self.name = f"constant_base_rate({rate:.4f})"
        self.kind = "constant_base_rate"

    @classmethod
    def fit(cls, train_tickers, db_path: str | None = None):
        tickers = list(train_tickers)
        return cls(fit_base_rate(tickers, db_path=db_path),
                   fitted_on=len(tickers))

    def probability(self, view, terms, as_of: int) -> float | None:
        return self.rate


# --------------------------------------------------------------------------
# Baseline 3: not implemented, and not faked
# --------------------------------------------------------------------------

class HeuristicBaseline(Forecaster):
    """The best existing live strategy, re-run on the same folds.

    NOT IMPLEMENTED, on purpose. shadow.py's strategies are 1,471 lines of
    config-driven evaluation that write to the database as they go, so they
    are not pure functions and cannot be called from a harness that must be
    deterministic and side-effect free. Turning one into a pure adapter is
    real work with a real risk of silent drift: an adapter that has quietly
    diverged from the live strategy would attribute a difference to the
    candidate that actually comes from the reimplementation.

    Raising here is the honest state. A stub returning 0.5, or a
    plausible-looking approximation, would let a candidate 'beat the best
    heuristic' by beating something that is not it -- which is worse than
    having no third baseline, because it looks like having one.

    To implement: reimplement the leading strategy as a pure function, and
    add a fidelity test asserting the adapter reproduces shadow.py's own
    decision on recorded fixture inputs. Then 'the baseline drifted' is a
    test failure rather than a silent misattribution.
    """

    name = "best_heuristic"
    kind = "best_heuristic"

    def probability(self, view, terms, as_of: int) -> float | None:
        raise NotImplementedError(
            "HeuristicBaseline is not implemented. shadow.py's strategies "
            "are not pure functions; a faithful adapter plus a fidelity "
            "test against recorded fixtures is required first. Refusing to "
            "return a plausible number that is not the live strategy.")


# --------------------------------------------------------------------------
# Turning a probability into a decision -- shared by every forecaster
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TraderConfig:
    edge_threshold: float = 0.05   # required gap between forecast and price
    contracts: int = 10
    min_price_cents: int = 2       # avoid the degenerate ends of the book
    max_price_cents: int = 98


class ProbabilityTrader:
    """Converts a forecast into a Decision, identically for every
    forecaster.

    Buy YES when the forecast exceeds the ask by more than the threshold;
    buy NO when it falls below the bid by more than the threshold. Compare
    against the price actually payable -- the ask for a yes, the bid for a
    no -- not the mid. A rule written against the mid books edge it could
    not have captured, on every single trade, which is among the easiest
    ways to manufacture a profitable backtest.
    """

    def __init__(self, forecaster: Forecaster, config: TraderConfig | None = None):
        self.forecaster = forecaster
        self.config = config or TraderConfig()

    @property
    def name(self) -> str:
        return self.forecaster.name

    @property
    def kind(self) -> str:
        return self.forecaster.kind

    def decide(self, view, terms, as_of: int) -> Decision:
        abstain = Decision(terms.ticker, "yes", 0, as_of)
        p = self.forecaster.probability(view, terms, as_of)
        if p is None:
            return abstain

        points = view.price_points(terms.ticker)
        if not points:
            return abstain
        last = points[-1]
        bid, ask = last.get("yes_bid_cents"), last.get("yes_ask_cents")

        cfg = self.config
        if ask is not None and cfg.min_price_cents <= ask <= cfg.max_price_cents:
            if p - (ask / 100.0) > cfg.edge_threshold:
                return Decision(terms.ticker, "yes", cfg.contracts, as_of)
        if bid is not None and cfg.min_price_cents <= bid <= cfg.max_price_cents:
            if (bid / 100.0) - p > cfg.edge_threshold:
                return Decision(terms.ticker, "no", cfg.contracts, as_of)
        return abstain


def standard_baselines(train_tickers, db_path: str | None = None,
                       config: TraderConfig | None = None,
                       include_heuristic: bool = False) -> list[ProbabilityTrader]:
    """The baseline set for a fold.

    `include_heuristic` defaults to False and raises when True, so that a
    run which believes it compared against all three cannot quietly have
    compared against two.
    """
    out = [ProbabilityTrader(MarketForecaster(), config),
           ProbabilityTrader(
               ConstantBaseRateForecaster.fit(train_tickers, db_path=db_path),
               config)]
    if include_heuristic:
        out.append(ProbabilityTrader(HeuristicBaseline(), config))
    return out
