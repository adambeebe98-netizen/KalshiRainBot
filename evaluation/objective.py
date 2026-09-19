"""
Net-of-fees P&L accounting.

This project has already been bitten once by gross figures: settlement
computed P&L without the taker fee, which turned a real -$15.33 into a
reported +$8.52 and reordered the strategy leaderboard. Three strategies
that looked profitable were not.

So the rule here is structural rather than advisory. `net_pnl_cents` is
the headline and the only figure any reporting surface emits. The gross
number exists -- it is genuinely useful for diagnosing whether a loss came
from bad selection or from overtrading -- but it is called
`_diagnostic_gross_pnl_cents`, it never appears in `summary()` or
`__str__`, and a test asserts that.

Fee model, matching the live code rather than the original brief:

  entry order          -> always a taker fee
  closed early by sale -> plus an exit fee
  held to settlement   -> no second fee

Kalshi charges on execution and nothing at settlement, and most positions
here are held to settlement. Charging twice would overstate cost by a full
fee on the majority of trades and bias the harness toward rejecting real
edge. `conservative_fees=True` charges the exit fee unconditionally for
anyone who wants the pessimistic bound.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import fees
from evaluation import stats
from evaluation.execution import ExecutionAssumptions, Fill


@dataclass(frozen=True)
class SettledTrade:
    """One filled position, carried to its conclusion."""
    fill: Fill
    outcome: str                      # 'yes' | 'no' -- the settled result
    exit_price_cents: int | None      # None means held to settlement
    entry_fee_cents: int
    exit_fee_cents: int
    gross_pnl_cents: int
    net_pnl_cents: int

    @property
    def cost_cents(self) -> int:
        return self.fill.contracts * self.fill.price_cents

    @property
    def net_return(self) -> float:
        """Return on capital actually put at risk. The denominator is the
        entry cost, not a notional -- a binary contract bought at 5c risks
        5c, not 100c, and dividing by notional would understate both the
        wins and the losses by twentyfold."""
        return self.net_pnl_cents / self.cost_cents if self.cost_cents else 0.0


def settle(fill: Fill, outcome: str, exit_price_cents: int | None = None,
           conservative_fees: bool = False) -> SettledTrade:
    """Account for one filled position.

    A YES contract pays 100c if the event happened and 0 otherwise. Buying
    NO is buying the complement, so it pays 100c when the event did NOT
    happen. The price paid is what was risked either way.
    """
    if outcome not in ("yes", "no"):
        raise ValueError(f"outcome must be 'yes' or 'no', got {outcome!r}")
    if fill.contracts <= 0:
        raise ValueError("cannot settle a zero-size fill")

    n = fill.contracts
    paid = fill.price_cents
    won = (fill.side == outcome)

    if exit_price_cents is None:
        gross = (100 - paid) * n if won else -paid * n
    else:
        # Sold before settlement: the outcome is irrelevant to the P&L.
        gross = (exit_price_cents - paid) * n

    entry_fee = fees.taker_fee_cents(n, paid)
    if exit_price_cents is not None:
        exit_fee = fees.taker_fee_cents(n, exit_price_cents)
    elif conservative_fees:
        # Pessimistic bound: charge as if the position had to be unwound at
        # the price it was opened at, rather than held.
        #
        # Deliberately NOT the settlement price. Kalshi's fee is
        # 0.07*n*p*(1-p), which is exactly zero at 100c and near zero at
        # 1c, so "charge a fee at the settlement value" charges nothing at
        # all and the conservative mode silently does nothing -- which is
        # what the first version of this did.
        exit_fee = fees.taker_fee_cents(n, paid)
    else:
        exit_fee = 0

    return SettledTrade(
        fill=fill, outcome=outcome, exit_price_cents=exit_price_cents,
        entry_fee_cents=entry_fee, exit_fee_cents=exit_fee,
        gross_pnl_cents=gross, net_pnl_cents=gross - entry_fee - exit_fee,
    )


@dataclass(frozen=True)
class Result:
    """The outcome of evaluating one candidate over one fold.

    Construction requires `assumptions`. There is no default, because a
    P&L number whose execution assumptions are unknown is not a result --
    it is a number.
    """
    trades: tuple[SettledTrade, ...]
    assumptions: ExecutionAssumptions
    n_decisions: int = 0          # including abstentions and no-fills
    label: str = ""

    _diagnostic_gross_pnl_cents: int = field(init=False, default=0, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "_diagnostic_gross_pnl_cents",
                           sum(t.gross_pnl_cents for t in self.trades))

    # -- headline figures, all net --------------------------------------

    @property
    def net_pnl_cents(self) -> int:
        return sum(t.net_pnl_cents for t in self.trades)

    @property
    def fees_cents(self) -> int:
        return sum(t.entry_fee_cents + t.exit_fee_cents for t in self.trades)

    @property
    def capital_deployed_cents(self) -> int:
        return sum(t.cost_cents for t in self.trades)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def fill_rate(self) -> float:
        return self.n_trades / self.n_decisions if self.n_decisions else 0.0

    @property
    def net_roi(self) -> float:
        cap = self.capital_deployed_cents
        return self.net_pnl_cents / cap if cap else 0.0

    @property
    def net_returns(self) -> list[float]:
        return [t.net_return for t in self.trades]

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.net_pnl_cents > 0) / len(self.trades)

    def net_sharpe(self) -> float | None:
        """Per-trade Sharpe on net returns, or None when undefined.

        None rather than an exception: a candidate that made two trades or
        whose returns are constant is a legitimate thing for a search to
        produce, and it should be reported as unscoreable rather than
        crashing a thousand-candidate run.
        """
        try:
            return stats.sharpe_ratio(self.net_returns)
        except ValueError:
            return None

    def fee_drag(self) -> float:
        """Fees as a share of capital deployed. The number that decides
        whether an edge is real: an edge smaller than this is not one."""
        cap = self.capital_deployed_cents
        return self.fees_cents / cap if cap else 0.0

    # -- reporting -- net only ------------------------------------------

    def summary(self) -> str:
        sharpe = self.net_sharpe()
        sharpe_text = f"{sharpe:.3f}" if sharpe is not None else "n/a"
        return (
            f"{self.label or 'result'}: "
            f"net {self.net_pnl_cents / 100:+.2f} USD over {self.n_trades} trades "
            f"({self.fill_rate:.1%} of {self.n_decisions} decisions filled), "
            f"net ROI {self.net_roi:+.2%}, net Sharpe {sharpe_text}, "
            f"win rate {self.win_rate:.1%}, "
            f"fees {self.fees_cents / 100:.2f} USD ({self.fee_drag():.2%} drag)"
        )

    def __str__(self) -> str:
        return self.summary()

    def report(self) -> str:
        return f"{self.summary()}\n{self.assumptions.describe()}"


def combine(results: list[Result], label: str = "combined") -> Result:
    """Pool fold results. Assumptions must match: combining runs made under
    different execution models would produce a number describing neither."""
    if not results:
        raise ValueError("cannot combine zero results")
    first = results[0].assumptions
    for r in results[1:]:
        if r.assumptions != first:
            raise ValueError(
                "refusing to combine results with different execution "
                "assumptions -- the pooled figure would describe no actual "
                "run.\n"
                f"  {first.model} vs {r.assumptions.model}")
    trades = tuple(t for r in results for t in r.trades)
    return Result(trades=trades, assumptions=first,
                  n_decisions=sum(r.n_decisions for r in results), label=label)
