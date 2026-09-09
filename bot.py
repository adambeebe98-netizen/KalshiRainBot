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
from datetime import date, datetime, timezone

from config import SETTINGS
from kalshi_client import KalshiClient
from rules_extractor import RulesExtractor
from risk_manager import RiskManager, RiskState
from strategy import evaluate_market, evaluate_temperature_market, pick_relevant_forecast_temp_f
from weather_data import get_station_latest_observation, get_forecast_pop, STATION_REFERENCE
import depth_sizing
import fees
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

# Rain resolves same-day — "will it rain" more than a day or so out is pure
# forecast uncertainty with no real observation grounding yet, unlike
# temperature bracket-arbitrage (which cares about mispricing across a set,
# not forecast accuracy, so a longer horizon is fine there — see
# shadow.evaluate_bracket_set). 30h rather than a strict 24h gives a buffer
# for markets that close late in the evening rather than at midnight.
RAIN_MAX_HORIZON_HOURS = 30


def is_far_future_rain(ticker: str, close_time_str: str | None) -> bool:
    """
    Cheap, ticker-name-based heuristic ("RAIN" in the ticker) rather than
    the authoritative rules_extractor measure classification — deliberately
    so this check can run BEFORE any per-market API calls or LLM-based
    classification, on every scanned market, every cycle. In practice every
    real precipitation series observed so far (KXRAIN, KXRAINNYCM,
    KXRAINSEAM, KXRAINHOU, KXRAINMIA, KXRAINSFOM, ...) is consistently
    prefixed this way. Fails OPEN (returns False) on anything that can't be
    parsed — a missing/malformed close_time should never cause a real,
    tradeable rain market to silently disappear; the cost of occasionally
    showing one extra far-future market is much lower than hiding a real one.
    """
    if "RAIN" not in ticker.upper() or not close_time_str:
        return False
    try:
        close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        hours_until_close = (close_time - datetime.now(timezone.utc)).total_seconds() / 3600
        return hours_until_close > RAIN_MAX_HORIZON_HOURS
    except (ValueError, TypeError):
        return False


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
                    risk: RiskManager, live: bool, series_tickers: tuple[str, ...]) -> None:
    mode = "live" if live else "paper"

    for series_ticker in series_tickers:
        found = []
        cursor = None
        try:
            for _ in range(10):  # safety cap on pagination
                markets_resp = kalshi.get_markets(series_ticker=series_ticker, status="open", cursor=cursor)
                found.extend(markets_resp.get("markets", []))
                cursor = markets_resp.get("cursor")
                if not cursor:
                    break
        except Exception as e:
            log.error(f"Failed to fetch markets for {series_ticker}: {e}")
            continue

        # This is the actual proof of how many individual markets (cities,
        # for KXRAIN) came back for this one series — check this line in
        # the log/dashboard to confirm multiple cities are really coming
        # through, rather than assuming it from the API structure. Only
        # logs when something was actually found — with discovery pulling
        # in every series that keyword-matches (many of which are empty or
        # not the real home of a given city's market), logging every empty
        # check drowned out the real hits.
        if found:
            log.info(f"Series {series_ticker}: {len(found)} open market(s) — "
                     f"{[m['ticker'] for m in found][:15]}{'...' if len(found) > 15 else ''}")

        # Group by event so bracket_arbitrage can evaluate each full set of
        # mutually-exclusive brackets together — it needs ALL of a day's
        # brackets for one city at once, not one market at a time like every
        # other strategy. This runs once per event per cycle, independent of
        # the per-market loop below.
        by_event: dict[str, list[dict]] = {}
        for market in found:
            event_ticker = market.get("event_ticker")
            if event_ticker:
                by_event.setdefault(event_ticker, []).append(market)
        for event_ticker, event_markets in by_event.items():
            if len(event_markets) >= 2:  # a "bracket set" needs at least 2 mutually-exclusive options
                try:
                    shadow.evaluate_bracket_set(event_ticker, event_markets)
                except Exception as e:
                    log.warning(f"Bracket arbitrage evaluation failed for {event_ticker}: {e}")

        for market in found:
            ticker = market["ticker"]

            if is_far_future_rain(ticker, market.get("close_time")):
                storage.log_decision(ticker, "n/a", 0, 0.5, 0, "skipped",
                                      f"rain market more than {RAIN_MAX_HORIZON_HOURS}h out — "
                                      "rain signals are same-day/real-time only, unlike temperature "
                                      "bracket arbitrage which doesn't depend on forecast accuracy", mode)
                continue

            # Log a price point for EVERY scanned market, regardless of
            # whether it currently has a tradeable price — this is what
            # feeds the dashboard's "Open markets" view and the raw data
            # needed to eventually design a real swing-trading strategy
            # from observed movement instead of a guess (see shadow.py's
            # "swing" strategy notes). Deliberately BEFORE the tradeability
            # check below: a thin market with no live ask and no trade
            # history yet is still genuinely open, and should still show up
            # as open, even though there's nothing to compute a signal
            # against yet.
            storage.log_price_snapshot(ticker, market.get("yes_ask"), market.get("yes_bid"))

            yes_price = market.get("yes_ask") or market.get("last_price")
            if yes_price is None and market.get("no_bid") is not None:
                # Symmetric case to the no_ask-from-yes_bid inversion a few
                # lines below: a resting NO bid at price P is the exact
                # same underlying liquidity as an implied YES ask at
                # (100-P) — buying YES at (100-P) and someone else buying
                # NO at P settle the same trade. Kalshi's market-list
                # endpoint can return yes_ask as null even when this
                # synthetic equivalent exists; without this, a market with
                # only resting NO-side liquidity looked exactly like a
                # market with no liquidity at all, and got silently skipped
                # as "no tradeable quote yet."
                yes_price = 100 - market["no_bid"]
            if not yes_price:
                storage.log_decision(ticker, "n/a", 0, 0.5, 0, "skipped",
                                      "no live ask quote or trade history yet — nothing to price a signal against",
                                      mode)
                continue

            no_ask = market.get("no_ask")
            if no_ask is None and market.get("yes_bid") is not None:
                no_ask = 100 - market["yes_bid"]

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

            # 3. Evaluate — routed by measure type, since precipitation and
            # temperature markets need different models (they used to both
            # go through the rain model, which silently produced meaningless
            # signals for temperature markets).
            if rules.measure in ("precipitation_daily", "precipitation_monthly"):
                signal = evaluate_market(ticker, yes_price, rules, observation, forecast)
                current_forecast_temp_f = previous_forecast_temp_f = None
            elif rules.measure in ("temperature_high", "temperature_low"):
                signal = evaluate_temperature_market(ticker, yes_price, rules, observation, forecast)
                # For temp_forecast_momentum (see shadow.py): grab the
                # previous cycle's logged forecast BEFORE overwriting it with
                # this cycle's, so the comparison is "did it move since last
                # time," not "compared to itself."
                current_forecast_temp_f = pick_relevant_forecast_temp_f(rules.measure, forecast)
                previous_forecast_temp_f = storage.get_previous_forecast_temp_f(ticker)
                storage.log_forecast_snapshot(ticker, current_forecast_temp_f)
            else:
                storage.log_decision(ticker, "n/a", yes_price, 0.5, 0, "skipped",
                                      f"no model for measure={rules.measure!r}", mode)
                continue

            # Every shadow strategy (see shadow.py / strategies_lib.py) gets a
            # look at this same market, independent of what the ACTIVE bot
            # decides below — always paper, never a real order.
            shadow.evaluate_and_log(ticker, signal, yes_price, no_ask, rules.station_code, rules.measure,
                                     confidence=rules.confidence,
                                     current_forecast_temp_f=current_forecast_temp_f,
                                     previous_forecast_temp_f=previous_forecast_temp_f)

            approved, reason = risk.approve_trade(
                price_cents=(yes_price if signal.side == "yes" else 100 - yes_price),
                edge_cents=signal.edge_cents,
            )

            if not approved:
                storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                      signal.edge_cents, "skipped", reason, mode)
                continue

            quoted_price = yes_price if signal.side == "yes" else 100 - yes_price
            flat_cap_contracts = risk.max_contracts_for_trade(quoted_price)

            # Real depth-aware sizing (see depth_sizing.py): the quoted
            # top-of-book price only holds for the first few contracts —
            # walking further into the book to fill a bigger order raises
            # the real average price you'd pay. This finds the largest size
            # (up to the flat bankroll-pct cap above) whose NET, post-fee,
            # real-fill-price expected edge still clears the minimum —
            # rather than assuming the quoted price holds at any size, which
            # would overstate edge (and, during paper trading, overstate how
            # good the strategy actually is). Scoped to the main bot's trade
            # path only for now, not the 7 shadow strategies — see shadow.py
            # if extending this there later; that's a real extra API call
            # per candidate trade, multiplied by every shadow strategy, so
            # it wasn't added there without deciding that tradeoff on purpose.
            try:
                yes_bids, no_bids = kalshi.get_orderbook_levels(ticker)
            except Exception as e:
                log.warning(f"Orderbook fetch failed for {ticker}, skipping: {e}")
                storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                      signal.edge_cents, "skipped", f"orderbook fetch failed: {e}", mode)
                continue

            # Buying YES is matched against resting NO bids (inverted to
            # implied YES asks); buying NO is matched against resting YES
            # bids (inverted to implied NO asks) — see depth_sizing.py's
            # module docstring for why this inversion is correct.
            opposite_bids = no_bids if signal.side == "yes" else yes_bids
            ask_levels = depth_sizing.implied_ask_levels(opposite_bids)

            fill = depth_sizing.find_max_profitable_size(
                ask_levels, fees.taker_fee_cents, signal.model_probability,
                max_contracts_cap=flat_cap_contracts, min_net_edge_cents=risk.preset.min_edge_cents,
            )
            if fill is None:
                storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                      signal.edge_cents, "skipped",
                                      "no size clears net-of-fee edge at real order book depth", mode)
                continue

            contracts = fill.contracts_fillable
            price = round(fill.avg_price_cents)  # real volume-weighted fill price, not just the quote

            storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                  signal.edge_cents, "traded", signal.rationale, mode)

            if live:
                try:
                    order = kalshi.create_order(
                        ticker=ticker, side=signal.side, action="buy",
                        count=contracts, price_cents=price,
                    )
                    # V2's create-order response is flat (order_id at the top
                    # level), unlike the legacy endpoint's {"order": {...}}
                    # wrapper — see kalshi_client.create_order()'s docstring.
                    order_id = order.get("order_id")
                    log.info(f"LIVE order placed: {ticker} {signal.side} x{contracts} @ {price}c "
                             f"(edge {signal.edge_cents}c, depth-walked avg fill ~{fill.avg_price_cents:.1f}c"
                             f"{', book exhausted' if fill.exhausted_book else ''}) — {order_id}")
                except Exception as e:
                    log.error(f"Order failed for {ticker}: {e}")
                    continue
            else:
                order_id = None
                log.info(f"PAPER trade: {ticker} {signal.side} x{contracts} @ {price}c "
                         f"(edge {signal.edge_cents}c, depth-walked avg fill ~{fill.avg_price_cents:.1f}c"
                         f"{', book exhausted' if fill.exhausted_book else ''}) — {signal.rationale}")

            storage.log_trade(ticker, signal.side, contracts, price, mode, order_id,
                               model_probability=signal.model_probability_yes,
                               station_code=rules.station_code, measure=rules.measure)
            risk.record_fill(cost_cents=contracts * price)


def get_series_tickers(kalshi: KalshiClient, cache: dict) -> tuple[str, ...]:
    """
    Returns the current list of series to scan, refreshing via live
    discovery at most once per DISCOVERY_REFRESH_SECONDS (a series list
    changes rarely — no need to hit /series every 5-minute cycle). Falls
    back to the configured SERIES_TICKERS if discovery is off or fails,
    so a Kalshi API hiccup never leaves the bot scanning nothing.
    `cache` is a small dict the caller keeps across calls: {'tickers':..., 'ts':...}
    """
    if not SETTINGS.auto_discover_series:
        return SETTINGS.series_tickers

    now = time.time()
    if cache.get("tickers") and (now - cache.get("ts", 0)) < SETTINGS.discovery_refresh_seconds:
        return cache["tickers"]

    try:
        discovered: set[str] = set()
        for keyword in SETTINGS.discovery_keywords:
            discovered.update(kalshi.discover_series_tickers(keyword))
        if discovered:
            result = tuple(sorted(discovered))
            log.info(f"Discovered {len(result)} series matching {SETTINGS.discovery_keywords}: {result}")
            cache["tickers"] = result
            cache["ts"] = now
            return cache["tickers"]
        else:
            log.warning(f"Series discovery found nothing matching {SETTINGS.discovery_keywords} — "
                        f"falling back to configured SERIES_TICKERS: {SETTINGS.series_tickers}")
    except Exception as e:
        log.warning(f"Series discovery failed ({e}) — falling back to configured SERIES_TICKERS: {SETTINGS.series_tickers}")

    return cache.get("tickers") or SETTINGS.series_tickers


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
        if not SETTINGS.order_schema_verified:
            log.error(
                "create_order() now targets Kalshi's V2 order schema (bid/ask, fixed-point "
                "dollar strings — see kalshi_client.py's create_order docstring), matching "
                "docs.kalshi.com as of 2026-09-08. But published docs and live behavior "
                "aren't guaranteed identical, and this has NOT been confirmed against a real "
                "order on the live API. Real orders may still fail or behave unexpectedly. "
                "Set ORDER_SCHEMA_VERIFIED=true in .env only after confirming a real test "
                "order works."
            )
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
             f"auto-discover series: {SETTINGS.auto_discover_series} (keywords={SETTINGS.discovery_keywords})")

    advisor_interval = getattr(SETTINGS, "advisor_interval_seconds", 7 * 24 * 3600)
    series_cache: dict = {}
    consecutive_failures = 0
    while True:
        try:
            # Settle anything that's resolved since the last cycle FIRST —
            # this is what feeds the calibration loop and keeps bankroll
            # accurate before deciding on any new trades this cycle.
            settled = settlement.settle_resolved_trades(kalshi, risk)
            if settled:
                log.info(f"Settled {settled} resolved trade(s) this cycle.")

            current_series = get_series_tickers(kalshi, series_cache)
            scan_and_trade(kalshi, extractor, risk, live, current_series)
            storage.snapshot_bankroll(risk.state.bankroll_cents, note="cycle complete")

            # Weekly (by default) Claude-based review — writes suggestions
            # only, never applies anything. See advisor.py's module docstring
            # for the boundary this respects.
            last_run = float(storage.get_meta("last_advisor_run_ts", "0"))
            if time.time() - last_run > advisor_interval:
                try:
                    import advisor
                    advisor.generate_suggestions()
                except Exception as e:
                    log.warning(f"Advisor run failed (non-fatal, trading continues): {e}")

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
