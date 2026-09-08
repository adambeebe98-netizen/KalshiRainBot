"""
Runs each cycle before scanning for new trades. For every trade still marked
'open' in the DB, checks whether Kalshi has settled that market yet. If so:
  1. Computes real PnL and marks the trade won/lost.
  2. Updates the risk manager's bankroll (this is what makes bankroll
     tracking real instead of just "contracts bought so far").
  3. Feeds the outcome back into calibration.py — this is the actual
     learning step. Without this function running regularly, the bot never
     learns anything; the calibration table just stays empty.
"""
from __future__ import annotations

import logging

from kalshi_client import KalshiClient
from risk_manager import RiskManager
import calibration
import shadow
import storage

log = logging.getLogger("kalshi_weather_bot.settlement")


def settle_resolved_trades(kalshi: KalshiClient, risk: RiskManager) -> int:
    open_trades = storage.get_open_trades()
    open_shadow = storage.get_open_shadow_trades()
    settled_count = 0

    # One settlement check per unique ticker, shared between real trades and
    # every shadow strategy — so having 6+ shadow strategies doesn't mean 6x
    # the API calls. bracket_arbitrage trades store a SYNTHETIC event ticker
    # in `ticker` (not a real market — see shadow.evaluate_bracket_set), so
    # they contribute their `sample_member_ticker` here instead, which IS a
    # real, pollable market ticker.
    real_shadow_tickers = {
        (t["sample_member_ticker"] if t["strategy"] == "bracket_arbitrage" else t["ticker"])
        for t in open_shadow
    }
    real_shadow_tickers.discard(None)
    tickers_to_check = {t["ticker"] for t in open_trades} | real_shadow_tickers
    checked: dict[str, tuple[bool, str | None]] = {}
    for ticker in tickers_to_check:
        try:
            checked[ticker] = kalshi.get_market_settlement(ticker)
        except Exception as e:
            log.warning(f"Could not check settlement for {ticker}: {e}")

    for trade in open_trades:
        ticker = trade["ticker"]
        if ticker not in checked:
            continue

        is_settled, result = checked[ticker]
        if not is_settled:
            continue

        won = (trade["side"] == result)
        count = trade["count"]
        price = trade["price_cents"]
        # Binary contract payout: winner gets 100c/contract, loser gets 0.
        # You already paid `price` cents/contract when you bought it.
        pnl_cents = (100 - price) * count if won else -price * count

        storage.settle_trade(trade["id"], won, pnl_cents)
        risk.record_settlement(pnl_cents)

        # Calibration tracks the YES-event probability consistently, regardless
        # of which side was actually traded — trades table stores
        # model_probability as the calibrated P(yes) at decision time (see bot.py).
        calibration.record_outcome(
            station_code=trade.get("station_code"),
            measure=trade.get("measure"),
            predicted_probability=trade.get("model_probability"),
            actual_outcome=(result == "yes"),
        )

        log.info(
            f"Settled {ticker}: {'WON' if won else 'LOST'}, pnl={pnl_cents}c, "
            f"new bankroll={risk.state.bankroll_cents}c"
        )
        settled_count += 1

    swing_closed = shadow.check_swing_exits()
    if swing_closed:
        log.info(f"Closed {swing_closed} swing position(s) early on price target this cycle.")

    bracket_settled = shadow.settle_bracket_arbitrage(checked)
    if bracket_settled:
        log.info(f"Settled {bracket_settled} bracket-arbitrage set(s) this cycle.")

    shadow_settled = shadow.settle(checked)
    shadow.snapshot_all()
    if shadow_settled:
        log.info(f"Settled {shadow_settled} shadow-strategy trade(s) this cycle.")

    return settled_count
