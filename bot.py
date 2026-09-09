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
from kalshi_client import KalshiClient, market_price_cents
from rules_extractor import RulesExtractor
from risk_manager import RiskManager, RiskState
from strategy import evaluate_market, evaluate_temperature_market, pick_relevant_forecast_temp_f
from weather_data import (get_station_latest_observation, get_forecast_pop, STATION_REFERENCE,
                           kalshi_station_to_nws_id, looks_like_us_station)
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


def is_far_future_rain(close_time_str: str | None) -> bool:
    """
    Called only for markets ALREADY classified as measure=='precipitation_daily'
    (see bot.py) — daily rain resolves same-day, so a market whose close
    time is still far out is pure forecast noise with no real observation
    grounding yet. Deliberately NOT applied to precipitation_monthly (a
    genuinely long-horizon question by design, not a same-day one) or
    temperature bracket arbitrage (doesn't depend on forecast accuracy at
    all — see shadow.evaluate_bracket_set).

    Fails OPEN (returns False) on a missing/malformed close_time — a
    parsing hiccup should never make a real, tradeable same-day market
    silently disappear.

    (Earlier version of this also did its own "RAIN in ticker" name
    matching so it could run cheaply BEFORE rules extraction, avoiding an
    API call for a market about to be skipped anyway. That heuristic
    couldn't tell precipitation_daily apart from precipitation_monthly,
    though, and ended up silently blocking every monthly-cumulative rain
    market too — e.g. KXRAINSEAM, KXRAINSFOM — which should get the same
    long horizon as temperature bracket arbitrage, not the daily-only
    cutoff. Moved to run AFTER the real measure classification instead:
    rules_extractor caches per-ticker, so this only costs one real
    classification call per ticker ever, not per cycle — a mixup between
    daily and monthly is the more expensive mistake to guard against now.)
    """
    if not close_time_str:
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
        # DISABLED as of 2026-09-09 (see shadow.py's STRATEGIES dict) — the
        # per-leg orderbook fetch below is a real, non-trivial API cost, no
        # reason to keep paying it while the strategy itself is a no-op.
        if "bracket_arbitrage" in shadow.ACTIVE_STRATEGIES:
            by_event: dict[str, list[dict]] = {}
            for market in found:
                event_ticker = market.get("event_ticker")
                if event_ticker:
                    by_event.setdefault(event_ticker, []).append(market)
            for event_ticker, event_markets in by_event.items():
                if len(event_markets) >= 2:  # a "bracket set" needs at least 2 mutually-exclusive options
                    try:
                        # One orderbook fetch per leg — a real, additional API
                        # cost proportional to the set's size, deliberately
                        # accepted so bracket sizing can walk each leg's real
                        # depth (see shadow.evaluate_bracket_set) instead of
                        # assuming the flat top-of-book price holds at any
                        # size. Fails open per-leg: a single leg's fetch
                        # failing just makes that leg's entry (None, None),
                        # which evaluate_bracket_set treats as "no depth data
                        # for this set" and falls back to its original flat
                        # top-of-book behavior for the WHOLE set, rather than
                        # guessing at partial depth data.
                        leg_orderbooks: dict[str, tuple] = {}
                        for m in event_markets:
                            try:
                                leg_orderbooks[m["ticker"]] = kalshi.get_orderbook_levels(m["ticker"])
                            except Exception as e:
                                log.warning(f"Orderbook fetch failed for bracket leg {m['ticker']}: {e}")
                                leg_orderbooks[m["ticker"]] = (None, None)
                        shadow.evaluate_bracket_set(event_ticker, event_markets, leg_orderbooks)
                    except Exception as e:
                        log.warning(f"Bracket arbitrage evaluation failed for {event_ticker}: {e}")

        for market in found:
            ticker = market["ticker"]

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
            # market_price_cents() reads the real "<field>_dollars" keys
            # Kalshi's list endpoint actually returns (see its docstring in
            # kalshi_client.py) — a raw market.get("yes_ask") always
            # silently returned None here, since that key never existed.
            m_yes_ask = market_price_cents(market, "yes_ask")
            m_yes_bid = market_price_cents(market, "yes_bid")
            m_no_ask = market_price_cents(market, "no_ask")
            m_no_bid = market_price_cents(market, "no_bid")
            m_last_price = market_price_cents(market, "last_price")

            storage.log_price_snapshot(ticker, m_yes_ask, m_yes_bid)

            yes_price = m_yes_ask or m_last_price
            if yes_price is None and m_no_bid is not None:
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
                yes_price = 100 - m_no_bid
            if not yes_price:
                storage.log_decision(ticker, "n/a", 0, 0.5, 0, "skipped",
                                      "no live ask quote or trade history yet — nothing to price a signal against",
                                      mode)
                continue

            no_ask = m_no_ask
            if no_ask is None and m_yes_bid is not None:
                no_ask = 100 - m_yes_bid

            # 1. Get and cache structured settlement rules for this market.
            rules_text = kalshi.get_market_rules_text(ticker)
            rules = extractor.extract(ticker, rules_text)

            if rules.confidence == "low":
                storage.log_decision(ticker, "n/a", yes_price, 0.5, 0, "skipped",
                                      "low-confidence rules extraction — needs manual review", mode)
                continue

            # 2. Pull weather data for the named station, if we know it.
            # kalshi_station_to_nws_id() translates rules.station_code (Kalshi's
            # own "CLI"-prefixed settlement-source format) into the real
            # ICAO/airport code weather.gov and STATION_REFERENCE both use —
            # without this, every single one of these lookups 404s, silently,
            # regardless of which city it is.
            station = kalshi_station_to_nws_id(rules.station_code)
            observation, forecast = None, []
            if station and station in STATION_REFERENCE:
                observation = get_station_latest_observation(station)
                ref = STATION_REFERENCE[station]
                forecast = get_forecast_pop(ref["lat"], ref["lon"])
            elif looks_like_us_station(station):
                # Plausible US ICAO code not yet in STATION_REFERENCE —
                # still worth trying weather.gov for the observation even
                # without forecast coordinates on file, since it might
                # genuinely resolve.
                observation = get_station_latest_observation(station)
                # No lat/lon on file for forecast lookup — add it to STATION_REFERENCE
                # in weather_data.py to enable forecast-based signals for this station.
            # else: station doesn't look like a US station at all (e.g.
            # RJTT/Tokyo, EGLL/London, WSSS/Singapore — every international
            # city seen in Kalshi's discovered temperature series uses a
            # non-K ICAO prefix). weather.gov is a US-only NWS system, so
            # this call would 404 every single cycle with zero chance of
            # ever succeeding — skipped entirely rather than paying that
            # cost and adding log noise for something that can't be fixed
            # from this side.

            # 3. Evaluate — routed by measure type, since precipitation and
            # temperature markets need different models (they used to both
            # go through the rain model, which silently produced meaningless
            # signals for temperature markets).
            if rules.measure in ("precipitation_daily", "precipitation_monthly"):
                # Only precipitation_daily gets the same-day horizon check —
                # precipitation_monthly is a genuinely long-horizon question
                # by design (see is_far_future_rain's docstring for why the
                # ticker-name-based version of this check used to wrongly
                # catch monthly-cumulative markets too).
                if rules.measure == "precipitation_daily" and is_far_future_rain(market.get("close_time")):
                    storage.log_decision(ticker, "n/a", yes_price, 0.5, 0, "skipped",
                                          f"rain market more than {RAIN_MAX_HORIZON_HOURS}h out — "
                                          "rain signals are same-day/real-time only, unlike temperature "
                                          "bracket arbitrage or precipitation_monthly, neither of which "
                                          "depend on near-term forecast accuracy", mode)
                    continue
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

            # Fetched ONCE here and shared by both the shadow strategies
            # below (all 16+ of them) and the main bot's own real-order
            # sizing further down — same order book, no reason to re-fetch
            # it per-consumer. Fails open (None, None) rather than skipping
            # the market entirely: every shadow strategy already falls back
            # to the flat top-of-book price/count when book data isn't
            # available (see shadow.evaluate_and_log's docstring), so a
            # transient orderbook-fetch failure degrades sizing accuracy
            # for this one cycle rather than losing the market completely.
            try:
                yes_bids, no_bids = kalshi.get_orderbook_levels(ticker)
            except Exception as e:
                log.warning(f"Orderbook fetch failed for {ticker}, shadow/sizing falls back to flat pricing: {e}")
                yes_bids, no_bids = None, None

            # Every shadow strategy (see shadow.py / strategies_lib.py) gets a
            # look at this same market, independent of what the ACTIVE bot
            # decides below — always paper, never a real order.
            shadow.evaluate_and_log(ticker, signal, yes_price, no_ask, rules.station_code, rules.measure,
                                     confidence=rules.confidence,
                                     current_forecast_temp_f=current_forecast_temp_f,
                                     previous_forecast_temp_f=previous_forecast_temp_f,
                                     yes_bids=yes_bids, no_bids=no_bids)

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
            # good the strategy actually is). yes_bids/no_bids were already
            # fetched once, earlier, and shared with every shadow strategy
            # too (see above) — reused here rather than fetched again.
            if yes_bids is None or no_bids is None:
                storage.log_decision(ticker, signal.side, yes_price, signal.model_probability,
                                      signal.edge_cents, "skipped", "orderbook fetch failed earlier this cycle", mode)
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
                max_slippage_cents=risk.preset.max_slippage_cents,
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
            # Core hardcoded series (SETTINGS.series_tickers, e.g. KXRAIN)
            # always go FIRST, every cycle — sorting the combined set
            # purely alphabetically meant a series like KXRAIN could land
            # 130+ deep in a 162-series discovered list (dominated by
            # KXHIGH*/KXLOW* temperature series alone), starving it for
            # 20-40+ minutes per cycle at real per-market scan cost
            # (orderbook fetch + LLM rules extraction each). Confirmed
            # live: rain_always_trade sat with zero trades for over 20
            # minutes because KXRAIN simply hadn't come up in rotation yet,
            # not because of any pricing or logic bug. Discovered extras
            # still all get scanned every cycle — just never ahead of the
            # series already known to matter.
            extras = sorted(discovered - set(SETTINGS.series_tickers))
            result = tuple(SETTINGS.series_tickers) + tuple(extras)
            log.info(f"Discovered {len(extras)} additional series matching {SETTINGS.discovery_keywords} "
                     f"(scanned after the {len(SETTINGS.series_tickers)} configured core series "
                     f"{SETTINGS.series_tickers}): {result}")
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
    # Same reasoning for today's realized P&L — see
    # storage.get_todays_realized_pnl_cents()'s docstring: without this,
    # every restart silently resets the daily kill switch back to
    # "fresh," regardless of what already happened earlier today.
    resumed_pnl_today = storage.get_todays_realized_pnl_cents_main()
    risk = RiskManager(RiskState(bankroll_cents=resumed_bankroll, day=date.today(),
                                  realized_pnl_today_cents=resumed_pnl_today))

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
