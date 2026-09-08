"""
Runs several named strategies in parallel, ALL in paper simulation, all the
time — regardless of what the main bot's active RISK_MODE or LIVE_TRADING
setting is. Each strategy gets its own simulated bankroll and its own
running P&L, so over time the dashboard can show a real, apples-to-apples
comparison: which of these approaches would actually have made money.

This module never places a real order and never touches the real bankroll
in risk_manager — it's entirely observational. The active bot (bot.py's
main scan_and_trade + its one RiskManager) is the only thing that can ever
place a live order, and only when LIVE_TRADING=true. Promoting a shadow
strategy to "the real one" is a manual step: change RISK_MODE (or the
underlying strategy choice) in .env/the dashboard yourself, once you're
convinced by the data here — this module does not do that automatically.

Strategies defined below:
  calibrated_conservative / balanced / aggressive — the main model
    (strategy.py) at three different risk-threshold presets.
  arbitrage — buys both sides when yes_ask + no_ask < 100c (rare on a
    healthy Kalshi market; see strategies_lib.py for why).
  favorites_baseline — naive: buys YES whenever price >= 90c, no model.
    A deliberately dumb comparison point.
  longshot — the calibrated model, restricted to cheap (2-15c) contracts.
  swing — buy cheap, sell higher BEFORE resolution, on the price movement
    itself rather than the weather outcome. See the big caveat on this one
    right above its entry in STRATEGIES below — the thresholds are a
    starting guess, not something backed by real movement data yet.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from config import RISK_PRESETS, SETTINGS
from risk_manager import RiskManager, RiskState, RiskPreset
from strategy import TradeSignal
import strategies_lib as lib
import storage

STRATEGIES = {
    "calibrated_conservative": {"kind": "calibrated", "risk": "conservative"},
    "calibrated_balanced":     {"kind": "calibrated", "risk": "balanced"},
    "calibrated_aggressive":   {"kind": "calibrated", "risk": "aggressive"},
    # Arbitrage's "edge" is a guaranteed profit in cents, not a probability
    # edge — a much lower bar clears it (even 1-2c guaranteed is worth
    # taking in theory; real fees would eat small amounts, which is exactly
    # the kind of thing this comparison is meant to surface).
    # Arbitrage's "price" is the COMBINED yes_ask+no_ask cost, which sits
    # near 100c by construction — the normal single-side price band (capped
    # at 90c) would reject every real arbitrage candidate, so it gets its
    # own band up to 99c.
    "arbitrage":               {"kind": "arbitrage",  "risk": "conservative", "min_edge_cents_override": 1,
                                 "max_price_override": 99},
    # Favorites baseline has NO edge concept at all by design — it trades
    # purely on price, specifically ABOVE the normal 90c ceiling (that's the
    # whole point of the strategy), so both the edge filter and the price
    # ceiling get overridden here.
    "favorites_baseline":      {"kind": "favorites",  "risk": "balanced", "threshold": 90,
                                 "min_edge_cents_override": 0, "max_price_override": 99},
    "longshot":                {"kind": "longshot",   "risk": "aggressive", "min_price": 2, "max_price": 15},
    # SWING — buys when the calibrated model still likes the price, then
    # sells as soon as the price rises to entry+exit_offset, regardless of
    # whether the market has resolved yet. If the exit target is never hit,
    # it falls through to normal settlement (held to resolution) like any
    # other strategy — see shadow.check_swing_exits().
    #
    # HONEST CAVEAT: entry_max=40 and exit_offset=20 (buy under 40c, sell
    # ~20c higher) are a starting guess, not a validated rule. price_history
    # is now logging every scanned market every cycle specifically so these
    # numbers can be replaced with real observed movement once there's
    # enough data (a few weeks minimum) — check storage.get_price_history()
    # per ticker before trusting this strategy's results over the others.
    "swing":                   {"kind": "swing", "risk": "balanced", "entry_max": 40, "exit_offset": 20},
}

_engines: dict[str, RiskManager] | None = None


def _build_preset(cfg: dict) -> RiskPreset:
    p = RISK_PRESETS[cfg["risk"]]
    return RiskPreset(
        min_edge_cents=cfg.get("min_edge_cents_override", p["min_edge_cents"]),
        max_position_pct=p["max_position_pct"],
        max_daily_loss_pct=p["max_daily_loss_pct"],
        min_contract_price_cents=SETTINGS.min_contract_price_cents,
        max_contract_price_cents=cfg.get("max_price_override", SETTINGS.max_contract_price_cents),
        max_open_positions=SETTINGS.max_open_positions,
    )


def get_engines() -> dict[str, RiskManager]:
    global _engines
    if _engines is None:
        _engines = {}
        for name, cfg in STRATEGIES.items():
            bankroll = storage.load_last_shadow_bankroll(name, SETTINGS.starting_bankroll_cents)
            state = RiskState(bankroll_cents=bankroll, day=date.today())
            _engines[name] = RiskManager(state, _build_preset(cfg))
    return _engines


def evaluate_and_log(ticker: str, signal: Optional[TradeSignal], yes_ask: Optional[int],
                      no_ask: Optional[int], station_code: Optional[str], measure: Optional[str]) -> None:
    """Called once per scanned market per cycle. Every strategy independently
    decides whether IT would trade this market — never a real order."""
    engines = get_engines()

    for name, cfg in STRATEGIES.items():
        rm = engines[name]
        kind = cfg["kind"]

        if kind == "calibrated":
            if not signal:
                continue
            price = yes_ask if signal.side == "yes" else (100 - yes_ask if yes_ask else None)
            if price is None:
                continue
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents, signal.rationale)
            model_prob = signal.model_probability_yes

        elif kind == "arbitrage":
            candidate = lib.arbitrage_candidate(yes_ask, no_ask)
            if candidate:
                # Arbitrage buys ONE unit of each side — always exactly 1 contract
                # pair, since the "edge" doesn't scale the same way with size as
                # a probability-based bet does. Log directly, skip normal sizing.
                approved, reason = rm.approve_trade(candidate.price_cents, candidate.edge_cents)
                if approved:
                    storage.log_shadow_trade(name, ticker, "both", 1, candidate.price_cents,
                                              model_probability=None, station_code=station_code, measure=measure)
                    rm.record_fill(cost_cents=candidate.price_cents)
            continue

        elif kind == "favorites":
            if yes_ask is None:
                continue
            candidate = lib.favorites_candidate(yes_ask, cfg.get("threshold", 90))
            model_prob = None

        elif kind == "longshot":
            if not signal:
                continue
            price = yes_ask if signal.side == "yes" else (100 - yes_ask if yes_ask else None)
            if price is None:
                continue
            candidate = lib.longshot_candidate(signal, price, cfg.get("min_price", 2), cfg.get("max_price", 15))
            model_prob = signal.model_probability_yes

        elif kind == "swing":
            if not signal:
                continue
            price = yes_ask if signal.side == "yes" else (100 - yes_ask if yes_ask else None)
            if price is None or price > cfg.get("entry_max", 40):
                continue
            # Still requires the calibrated model to see SOME edge — this
            # isn't pure price-momentum yet (that needs the movement data
            # to design responsibly), it's "cheap AND the model likes it,"
            # with the early-sell mechanism layered on top.
            candidate = lib.StrategyCandidate(signal.side, price, signal.edge_cents, signal.rationale)
            model_prob = signal.model_probability_yes

        else:
            continue

        if not candidate:
            continue

        approved, reason = rm.approve_trade(candidate.price_cents, candidate.edge_cents)
        if not approved:
            continue

        contracts = rm.max_contracts_for_trade(candidate.price_cents)
        if contracts < 1:
            continue

        exit_target = (candidate.price_cents + cfg["exit_offset"]) if kind == "swing" else None
        storage.log_shadow_trade(name, ticker, candidate.side, contracts, candidate.price_cents,
                                  model_probability=model_prob, station_code=station_code, measure=measure,
                                  exit_target_cents=exit_target)
        rm.record_fill(cost_cents=contracts * candidate.price_cents)


def check_swing_exits() -> int:
    """Runs every cycle, independent of settlement. For every open 'swing'
    position, checks the latest logged price (see storage.log_price_snapshot,
    called each cycle in bot.py for every scanned market) against the exit
    target, and closes the position (sells) the moment it's reached —
    profit is (exit_price - entry_price), never mind how the market
    eventually resolves. Positions that never reach their target fall
    through to normal settlement in settle() below, same as any other
    strategy, once the market actually resolves."""
    engines = get_engines()
    open_swing = storage.get_open_shadow_trades(strategy="swing")
    closed_count = 0

    for trade in open_swing:
        if trade["exit_target_cents"] is None:
            continue
        latest = storage.get_latest_price(trade["ticker"])
        if not latest:
            continue

        side = trade["side"]
        # Selling YES realizes the yes_bid; selling NO realizes (100 - yes_ask).
        exit_price = latest["yes_bid"] if side == "yes" else (
            100 - latest["yes_ask"] if latest["yes_ask"] is not None else None
        )
        if exit_price is None or exit_price < trade["exit_target_cents"]:
            continue

        pnl_cents = (exit_price - trade["price_cents"]) * trade["count"]
        storage.close_shadow_trade_sold(trade["id"], pnl_cents)
        rm = engines.get("swing")
        if rm:
            rm.record_settlement(pnl_cents)
        closed_count += 1

    return closed_count


def settle(ticker_results: dict[str, tuple[bool, Optional[str]]]) -> int:
    """ticker_results: {ticker: (is_settled, 'yes'|'no'|None)} — reuses the
    same settlement lookups the real settlement.py already fetched this
    cycle, so this never makes its own extra API calls."""
    engines = get_engines()
    open_trades = storage.get_open_shadow_trades()
    settled_count = 0

    for trade in open_trades:
        ticker = trade["ticker"]
        if ticker not in ticker_results:
            continue
        is_settled, result = ticker_results[ticker]
        if not is_settled:
            continue

        count = trade["count"]
        price = trade["price_cents"]
        side = trade["side"]

        if side == "both":
            # Arbitrage: guaranteed payout of 100c regardless of result,
            # cost was `price` (the combined yes_ask+no_ask paid).
            pnl_cents = 100 - price
            won = True
        else:
            won = (side == result)
            pnl_cents = (100 - price) * count if won else -price * count

        storage.settle_shadow_trade(trade["id"], won, pnl_cents)
        rm = engines.get(trade["strategy"])
        if rm:
            rm.record_settlement(pnl_cents)
        settled_count += 1

    return settled_count


def snapshot_all() -> None:
    for name, rm in get_engines().items():
        storage.snapshot_shadow_bankroll(name, rm.state.bankroll_cents)
