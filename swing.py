"""
Swing trading: enter, and then get out before settlement.

Everything else in this harness buys and holds. That is one decision per
market, and it cannot express the idea that a contract is worth more now
than it will be in six hours regardless of how it settles.

The measured case for trying (analysis/swing_feasibility.py): on
temperature markets the best capturable move is a median 49c against a
9c round trip, and 46% of hours are tradeable. A rule has to capture
better than a fifth of the perfect move to clear its costs, which is
demanding but not arithmetically impossible -- unlike every other avenue
tested so far.

The design keeps forecasters stateless. A Forecaster still answers "what
is this worth" and nothing else, so the same objects can be compared
across hold and swing modes. The POSITION lives in the trader, which is
where the asymmetry actually is: entering is optional, exiting is not.
"""
from __future__ import annotations

from dataclasses import dataclass

from evaluation import baselines
from evaluation.execution import Decision, Position

HOLD, ENTER, EXIT = "hold", "enter", "exit"


@dataclass(frozen=True)
class SwingConfig:
    edge_threshold: float = 0.08     # to open
    exit_edge: float = 0.02          # close once the edge has decayed to this
    take_profit_cents: int = 0       # 0 disables
    stop_loss_cents: int = 0         # 0 disables
    contracts: int = 10
    min_price_cents: int = 3
    max_price_cents: int = 97
    max_hold_hours: float = 0.0      # 0 disables


class SwingTrader:
    """Wraps a forecaster and carries a position.

    Exits are checked before entries, and in a fixed order: stop, target,
    time, then edge decay. Order matters -- a bar that would trigger both
    a stop and a target is ambiguous in hourly data, and resolving it in
    favour of the stop is the pessimistic reading. The alternative
    silently books the good side of every ambiguous bar, which is one of
    the classic ways a swing backtest invents profit.
    """

    def __init__(self, forecaster: baselines.Forecaster,
                 config: SwingConfig | None = None):
        self.forecaster = forecaster
        self.config = config or SwingConfig()
        self.name = f"swing[{forecaster.name}]"

    @property
    def kind(self) -> str:
        return f"swing_{self.forecaster.kind}"

    def _mid(self, view, ticker):
        points = view.price_points(ticker)
        if not points:
            return None, None, None
        last = points[-1]
        bid, ask = last.get("yes_bid_cents"), last.get("yes_ask_cents")
        if bid is None or ask is None:
            close = last.get("yes_price_cents")
            return (close, None, None) if close is not None else (None, None, None)
        return (bid + ask) / 2.0, bid, ask

    def decide(self, view, terms, as_of: int, position: Position | None):
        """Returns (action, Decision | None)."""
        cfg = self.config
        mid, bid, ask = self._mid(view, terms.ticker)
        if mid is None:
            return HOLD, None

        if position is not None:
            # Mark the position where it could actually be closed.
            mark = bid if position.side == "yes" else (
                (100 - ask) if ask is not None else None)
            if mark is not None:
                move = (mark - position.entry_price_cents)
                if cfg.stop_loss_cents and move <= -cfg.stop_loss_cents:
                    return EXIT, None
                if cfg.take_profit_cents and move >= cfg.take_profit_cents:
                    return EXIT, None
            if cfg.max_hold_hours:
                held_h = (as_of - position.entered_at) / 3600.0
                if held_h >= cfg.max_hold_hours:
                    return EXIT, None
            p = self.forecaster.probability(view, terms, as_of)
            if p is None:
                return EXIT, None            # no view left: do not hold on
            edge = (p - mid / 100.0) if position.side == "yes" else (mid / 100.0 - p)
            if edge <= cfg.exit_edge:
                return EXIT, None
            return HOLD, None

        p = self.forecaster.probability(view, terms, as_of)
        if p is None:
            return HOLD, None
        if ask is not None and cfg.min_price_cents <= ask <= cfg.max_price_cents:
            if p - ask / 100.0 > cfg.edge_threshold:
                return ENTER, Decision(terms.ticker, "yes", cfg.contracts, as_of)
        if bid is not None and cfg.min_price_cents <= bid <= cfg.max_price_cents:
            if bid / 100.0 - p > cfg.edge_threshold:
                return ENTER, Decision(terms.ticker, "no", cfg.contracts, as_of)
        return HOLD, None


def build_population(forecasters, configs=None) -> list:
    """Cross a set of forecasters with a set of exit rules."""
    configs = configs or default_configs()
    return [SwingTrader(f, c) for f in forecasters for c in configs]


def default_configs() -> list:
    out = []
    for take, stop in ((0, 0), (10, 10), (15, 10), (20, 15), (10, 20)):
        for hold_h in (0.0, 6.0, 12.0):
            out.append(SwingConfig(take_profit_cents=take, stop_loss_cents=stop,
                                    max_hold_hours=hold_h))
    return out
