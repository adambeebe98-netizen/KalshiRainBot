"""
Everything that stands between "a strategy found an edge" and "money leaves
the account." This is the part that matters more than any model.

Rules encoded here:
  1. Never risk more than max_position_pct of bankroll on one contract.
  2. Hard daily loss limit — once tripped, the bot stops opening new
     positions until the next calendar day (existing positions are left
     to resolve, not panic-closed).
  3. Cap on total concurrent open positions, so a burst of correlated
     weather-system trades (same storm, five cities) can't blow past your
     intended risk in one cycle.
  4. Price sanity band — refuses to trade contracts priced too close to
     0 or 100 cents, where a single tick is a huge % move and slippage/fees
     eat any theoretical edge.

RiskPreset makes all of this pluggable per-strategy: the main bot uses one
preset (from RISK_MODE), and each shadow strategy (see strategies.py and
shadow.py) gets its own preset and its own independent bankroll, so they
never share risk with each other even though they run in the same process.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from config import SETTINGS
import fees


@dataclass
class RiskPreset:
    min_edge_cents: int
    max_position_pct: float
    max_daily_loss_pct: float
    min_contract_price_cents: int = 2
    max_contract_price_cents: int = 90
    max_open_positions: int = 15


def default_preset() -> RiskPreset:
    """The preset implied by RISK_MODE / explicit env vars in .env — used by
    the main bot unless a specific preset is passed in."""
    return RiskPreset(
        min_edge_cents=SETTINGS.min_edge_cents,
        max_position_pct=SETTINGS.max_position_pct,
        max_daily_loss_pct=SETTINGS.max_daily_loss_pct,
        min_contract_price_cents=SETTINGS.min_contract_price_cents,
        max_contract_price_cents=SETTINGS.max_contract_price_cents,
        max_open_positions=SETTINGS.max_open_positions,
    )


@dataclass
class RiskState:
    bankroll_cents: int
    day: date
    realized_pnl_today_cents: int = 0
    open_positions_count: int = 0

    def is_kill_switch_tripped(self, max_daily_loss_pct: float) -> bool:
        loss_limit = -abs(int(self.bankroll_cents * max_daily_loss_pct))
        return self.realized_pnl_today_cents <= loss_limit

    def roll_day_if_needed(self, today: date) -> None:
        if today != self.day:
            self.day = today
            self.realized_pnl_today_cents = 0


class RiskManager:
    def __init__(self, state: RiskState, preset: RiskPreset | None = None):
        self.state = state
        self.preset = preset or default_preset()

    def max_contracts_for_trade(self, price_cents: int) -> int:
        if price_cents <= 0:
            return 0
        max_risk_cents = int(self.state.bankroll_cents * self.preset.max_position_pct)
        return max(0, max_risk_cents // price_cents)

    def approve_trade(self, price_cents: int, edge_cents: int, edge_already_net_of_fees: bool = False) -> tuple[bool, str]:
        today = date.today()
        self.state.roll_day_if_needed(today)

        if self.state.is_kill_switch_tripped(self.preset.max_daily_loss_pct):
            return False, "daily loss kill switch tripped — no new trades today"

        if self.state.open_positions_count >= self.preset.max_open_positions:
            return False, f"at max open positions ({self.preset.max_open_positions})"

        if not (self.preset.min_contract_price_cents <= price_cents <= self.preset.max_contract_price_cents):
            return False, (f"price {price_cents}c outside allowed band "
                            f"[{self.preset.min_contract_price_cents}, {self.preset.max_contract_price_cents}]")

        if edge_cents < self.preset.min_edge_cents:
            return False, f"edge {edge_cents}c below minimum {self.preset.min_edge_cents}c"

        contracts = self.max_contracts_for_trade(price_cents)
        if contracts < 1:
            return False, "position size rounds to 0 contracts under max_position_pct"

        if edge_already_net_of_fees:
            # Caller (e.g. a multi-order strategy like bracket arbitrage,
            # where the true fee is a sum across several separately-priced
            # orders, not one) has already computed real fees correctly and
            # baked them into edge_cents — applying the generic single-order
            # fee formula on top here would double-count or misstate it.
            return True, "approved"

        # Fee-awareness — a trade whose modeled edge doesn't survive real
        # Kalshi fees isn't actually a trade worth making, no matter how
        # good the raw edge_cents number looks. This check applies to
        # EVERY strategy through this one shared function, not just the
        # ones that were built with fees in mind from the start.
        fee_cents = fees.taker_fee_cents(contracts, price_cents)
        gross_expected_cents = edge_cents * contracts
        if gross_expected_cents - fee_cents <= 0:
            return False, (f"edge doesn't survive real fees: gross {gross_expected_cents}c "
                            f"- fee {fee_cents}c = {gross_expected_cents - fee_cents}c")

        return True, "approved"

    def record_fill(self, cost_cents: int) -> None:
        self.state.open_positions_count += 1

    def record_settlement(self, pnl_cents: int) -> None:
        self.state.bankroll_cents += pnl_cents
        self.state.realized_pnl_today_cents += pnl_cents
        self.state.open_positions_count = max(0, self.state.open_positions_count - 1)
