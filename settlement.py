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
    # the API calls. LEGACY bracket_arbitrage rows (from before the
    # per-leg refactor — see shadow.evaluate_bracket_set) store a SYNTHETIC
    # event ticker in `ticker` (not a real market) and need their
    # `sample_member_ticker` instead, which IS a real, pollable ticker.
    # Current-style bracket_arbitrage rows already have a real ticker
    # directly in `ticker` (each row is one real bracket leg now), same as
    # every other strategy — sample_member_ticker is None for those, so the
    # `and t["sample_member_ticker"]` guard below falls through to the
    # normal `t["ticker"]` case correctly.
    real_shadow_tickers = {
        (t["sample_member_ticker"] if t["strategy"] == "bracket_arbitrage" and t["sample_member_ticker"]
         else t["ticker"])
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
        log.info(f"Settled {bracket_settled} legacy bracket-arbitrage set(s) this cycle.")

    bracket_offloaded = shadow.check_bracket_arbitrage_offload()
    if bracket_offloaded:
        log.info(f"Offloaded {bracket_offloaded} bracket-arbitrage leg(s) early this cycle "
                 f"(sold below entry price, above zero, rather than holding to settlement).")

    shadow_settled = shadow.settle(checked)
    shadow.snapshot_all()
    if shadow_settled:
        log.info(f"Settled {shadow_settled} shadow-strategy trade(s) this cycle.")

    return settled_count


def backfill_market_outcomes(kalshi: KalshiClient, limit: int = 50) -> int:
    """
    Records the real settlement result for every scanned market whose
    close time has passed, regardless of whether any strategy ever
    traded it — foundation for retrospective backtesting, explicitly
    requested: collect data across every market, then later analyze it
    to find where a profitable trade existed that current strategies
    missed. That question can only be answered once the actual outcome
    is known for markets nobody acted on, which is exactly what this
    fills in.

    Deliberately NOT called every cycle like settle_resolved_trades above
    — over weeks of scanning, the number of distinct tickers ever
    snapshotted could be large, and checking settlement for all of them
    every single cycle would mean a large, mostly-redundant burst of API
    calls (a market that hasn't settled yet won't suddenly settle between
    one cycle and the next a few minutes later). Called on its own,
    longer interval instead (see bot.py), with `limit` bounding how many
    get checked per run so a real backlog doesn't turn into one huge
    burst either.
    """
    pending = storage.get_tickers_needing_outcome_backfill(limit=limit)
    recorded = 0
    for row in pending:
        ticker = row["ticker"]
        try:
            is_settled, result = kalshi.get_market_settlement(ticker)
        except Exception as e:
            log.warning(f"Could not check settlement for backfill on {ticker}: {e}")
            continue
        if not is_settled or result not in ("yes", "no"):
            continue
        storage.record_market_outcome(ticker, result)
        recorded += 1
    return recorded
