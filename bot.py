"""
Main entry point.

Default mode is PAPER TRADING: it does everything a live bot would do —
scan markets, extract rules, pull weather data, compute edges, size
positions — except it never calls kalshi_client.create_order(). Trades are
recorded to the local database as if they happened, at the real quoted
price, so you can evaluate the strategy against real market prices without
risking money.

To go live, ALL of the following must be true (defense in depth,
deliberately redundant):
    1. LIVE_TRADING=true in your .env
    2. You pass --live on the command line
    3. You type the confirmation phrase when prompted

Run:
    python bot.py                 # paper trading (default, safe)
    python bot.py --live          # live trading (requires .env flag + confirmation)
    python bot.py --once          # single scan cycle instead of continuous loop

Set RISK_MODE=conservative|balanced|aggressive in .env to control edge
threshold and position sizing (see config.py for exact presets).

Calibration: every settled trade feeds calibration.py, which tracks a
per-station bias between predicted and actual outcomes and applies it to
future estimates for that station. This is the "learning" in this bot —
an auditable running average, not a black-box model. See calibration.py
for how it works and settlement.py for where it gets fed.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date

from config import SETTINGS
from kalshi_client import KalshiClient
from rules_extractor import RulesExtractor
from risk_manager import RiskManager, RiskState
from strategy import evaluate_market
from weather_data import get_station_latest_observation, get_forecast_pop, STATION_REFERENCE
import settlement
import shadow
import storage

from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3),
    ],
)
log = logging.getLogger("kalshi_weather_bot")


def confirm_live_trading() -> bool:
    phrase = "I ACCEPT THE RISK"
    print("\n" + "=" * 60)
    print("LIVE TRADING REQUESTED — REAL MONEY WILL BE AT RISK")
    print(f"Bankroll on file: ${SETTINGS.starting_bankroll_cents / 100:.2f}")
    print(f"Max daily loss before kill switch: {SETTINGS.max_daily_loss_pct * 100:.1f}%")
    print(f"Max position size: {SETTINGS.max_position_pct * 100:.1f}% of bankroll")
    print("=" * 60)

    if not sys.stdin.isatty():
        # Running headless (systemd, cron, a detached process) — there's no
        # one to type a confirmation. Rather than crash on input() or,
        # worse, silently skip the confirmation, this requires a separate
        # explicit env var so headless live-trading is a deliberate choice
        # made once in your .env, not an accident of how the process starts.
        if SETTINGS.live_confirmed_headless:
            log.warning("No interactive terminal detected — proceeding on LIVE_TRADING_CONFIRMED=true.")
            return True
        log.error(
            "No interactive terminal detected, so the live-trading confirmation prompt "
            "can't be answered. Set LIVE_TRADING_CONFIRMED=true in .env if you deliberately "
            "want headless live trading (e.g. under systemd), or run 'python bot.py --live' "
            "manually in a terminal once first."
        )
        return False

    typed = input(f"Type '{phrase}' to proceed, or anything else to abort: ")
    return typed.strip() == phrase


def scan_and_trade(kalshi: KalshiClient, extractor: RulesExtractor,
                    risk: RiskManager, live: bool) -> None:
    mode = "live" if live else "paper"

    for series_ticker in SETTINGS.series_tickers:
        try:
            markets_resp = kalshi.get_markets(series_ticker=series_ticker, status="open")
        except Exception as e:
            log.error(f"Failed to fetch markets for {series_ticker}: {e}")
            continue

        for market in markets_resp.get("markets", []):
            ticker = market["ticker"]
            yes_price = market.get("yes_ask") or market.get("last_price")
            if not yes_price:
                continue

            # Log a price point for EVERY scanned market, regardless of
            # whether any strategy trades it — this is the raw data needed
            # to eventually design a real swing-trading strategy from
            # observed movement instead of a guess (see shadow.py's "swing"
            # strategy notes).
            no_ask = market.get("no_ask")
            if no_ask is None and market.get("yes_bid") is not None:
                no_ask = 100 - market["yes_bid"]
            storage.log_price_snapshot(ticker, yes_price, market.get("yes_bid"))

            # 1. Get and cache structured settlement rules for this market.
            rules_text = kalshi.get_market_rules_text(ticker)
            rules = extractor.extract(ticker, rules_text)

            if rules.confidence == "low":
                storage.log_decision(ticker, "n/a", yes_price, 0.5, 0, "skipped",
                                      "low-confidence rules extraction — needs manual review", mode)
                continue

            # 2. Pull weather data for the named station, if we know it.
            station = rules.station_code
            observation, forecast = None, []
            if station and station in STATION_REFERENCE:
                observation = get_station_latest_observation(station)
                ref = STATION_REFERENCE[station]
                forecast = get_forecast_pop(ref["lat"], ref["lon"])
            elif station:
                observation = get_station_latest_observation(station)
                # No lat/lon on file for forecast lookup — add it to STATION_REFERENCE
                # in weather_data.py to enable forecast-based signals for this station.

            # 3. Evaluate.
            signal = evaluate_market(ticker, yes_price, rules, observation, forecast)

            # Every shadow strategy (see shadow.py / strategies_lib.py) gets a
            # look at this same market, independent of what the ACTIVE bot
            # decides below — always paper, never a real order.
            shadow.evaluate_and_log(ticker, signal, yes_price, no_ask, rules.station_code, rules.measure)

            approved, reason = risk.approve_trade(
                price_cents=(yes_price if signal.side == "yes" else 100 - yes_price),
                edge_cents=signal.edge_cents,
            )

            if not approved:
                storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                      signal.edge_cents, "skipped", reason, mode)
                continue

            price = yes_price if signal.side == "yes" else 100 - yes_price
            contracts = risk.max_contracts_for_trade(price)

            storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                  signal.edge_cents, "traded", signal.rationale, mode)

            if live:
                try:
                    order = kalshi.create_order(
                        ticker=ticker, side=signal.side, action="buy",
                        count=contracts, price_cents=price,
                    )
                    order_id = order.get("order", {}).get("order_id")
                    log.info(f"LIVE order placed: {ticker} {signal.side} x{contracts} @ {price}c "
                             f"(edge {signal.edge_cents}c) — {order_id}")
                except Exception as e:
                    log.error(f"Order failed for {ticker}: {e}")
                    continue
            else:
                order_id = None
                log.info(f"PAPER trade: {ticker} {signal.side} x{contracts} @ {price}c "
                         f"(edge {signal.edge_cents}c) — {signal.rationale}")

            storage.log_trade(ticker, signal.side, contracts, price, mode, order_id,
                               model_probability=signal.model_probability_yes,
                               station_code=rules.station_code, measure=rules.measure)
            risk.record_fill(cost_cents=contracts * price)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Enable live order placement")
    parser.add_argument("--once", action="store_true", help="Run a single scan cycle and exit")
    args = parser.parse_args()

    live = args.live
    if live:
        if not SETTINGS.live_trading_enabled:
            log.error("LIVE_TRADING is not set to true in your .env — refusing to trade live.")
            sys.exit(1)
        if not confirm_live_trading():
            log.info("Live trading not confirmed. Exiting.")
            sys.exit(0)

    storage.init_db()
    extractor = RulesExtractor()
    # Resume from last known bankroll rather than resetting on every restart —
    # a crash or reboot shouldn't erase the bot's memory of real P&L.
    resumed_bankroll = storage.load_last_bankroll(SETTINGS.starting_bankroll_cents)
    risk = RiskManager(RiskState(bankroll_cents=resumed_bankroll, day=date.today()))

    kalshi = None
    if live:
        kalshi = KalshiClient()
    else:
        # Paper mode still needs real market prices — Kalshi's market data
        # endpoints are public/read-only, so this uses the same client but
        # create_order is simply never called in the paper branch above.
        try:
            kalshi = KalshiClient()
        except Exception as e:
            log.error(f"Even paper mode needs API credentials to read market data: {e}")
            sys.exit(1)

    log.info(f"Starting bot in {'LIVE' if live else 'PAPER'} mode, risk profile '{SETTINGS.risk_mode}'. "
             f"Bankroll: ${risk.state.bankroll_cents/100:.2f}, "
             f"scanning series: {SETTINGS.series_tickers}")

    consecutive_failures = 0
    while True:
        try:
            # Settle anything that's resolved since the last cycle FIRST —
            # this is what feeds the calibration loop and keeps bankroll
            # accurate before deciding on any new trades this cycle.
            settled = settlement.settle_resolved_trades(kalshi, risk)
            if settled:
                log.info(f"Settled {settled} resolved trade(s) this cycle.")

            scan_and_trade(kalshi, extractor, risk, live)
            storage.snapshot_bankroll(risk.state.bankroll_cents, note="cycle complete")
            consecutive_failures = 0
        except Exception as e:
            # One bad cycle (API hiccup, network blip, a market with malformed
            # data) should never take the whole bot down. Log it, back off,
            # and keep running — that's the difference between "unattended"
            # and "silently dead since Tuesday."
            consecutive_failures += 1
            log.error(f"Scan cycle failed ({consecutive_failures} in a row): {e}", exc_info=True)
            if consecutive_failures >= 5:
                log.error(
                    "5 consecutive cycle failures — something is likely structurally wrong "
                    "(bad credentials, API change, etc). Halting rather than looping forever. "
                    "Check the log above before restarting."
                )
                sys.exit(1)
            time.sleep(min(SETTINGS.poll_interval_seconds, 60) * consecutive_failures)
            continue

        if risk.state.is_kill_switch_tripped(risk.preset.max_daily_loss_pct):
            log.warning("Daily loss kill switch tripped. Halting new trades until tomorrow.")

        if args.once:
            break
        time.sleep(SETTINGS.poll_interval_seconds)


if __name__ == "__main__":
    main()
