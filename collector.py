"""
Scan markets, record everything, trade nothing.

This replaces bot.py's scan_and_trade for the collection half of the job.
Everything predictive that existed before the evaluation harness did is
deliberately absent: no strategy.py signals, no shadow.py strategies, no
RiskManager, no max_open_positions, no kill switch, no bankroll, no
order path. None of it was ever validated, and all of it shaped the data
-- the position cap alone was the binding constraint on 85-90% of
decisions, so the record reflects the cap more than any view of the
world.

What stays is what the data needs:

  * market discovery and pagination
  * rules extraction -- station, measure, thresholds. Required, because
    "what does this contract settle on" is the one thing this project has
    established actually matters.
  * weather observation and forecast for the station
  * price_history, market_snapshots, forecast_history

`model_probability_yes` is written as NULL and that is correct rather
than lazy. The column exists so a later backtest can tell "a model looked
at this and passed" apart from "nothing was looking". Nothing is looking:
the one candidate that has been through the harness failed it. Writing a
retired strategy's opinion there would make the record say something
untrue about what this system believed.

Limits come back when something earns them. The evaluation harness
decides that, not a number typed into a config file in advance.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import storage
from config import SETTINGS
from kalshi_client import KalshiClient, market_price_cents
from rules_extractor import RulesExtractor
from weather_data import (STATION_REFERENCE, get_forecast_pop,
                          get_station_latest_observation)

log = logging.getLogger("collector")

DEFAULT_CYCLE_SECONDS = 120
PAGINATION_CAP = 10
OUTCOME_BACKFILL_SECONDS = 3600


def kalshi_station_to_nws_id(station_code: str | None) -> str | None:
    """Kalshi's CLI-prefixed settlement code to an ICAO id.

    Same translation bot.py used. Kept here rather than imported so the
    collector does not depend on the module being retired.
    """
    if not station_code:
        return None
    code = station_code.strip().upper()
    if code.startswith("CLI"):
        return "K" + code[3:]
    return code


def looks_like_us_station(station: str | None) -> bool:
    """weather.gov is US-only. An international ICAO (RJTT, EGLL, WSSS)
    would 404 every cycle forever, so it is not worth the call."""
    return bool(station) and len(station) == 4 and station.startswith("K")


def fetch_weather(station_code: str | None):
    station = kalshi_station_to_nws_id(station_code)
    observation, forecast = None, []
    if station and station in STATION_REFERENCE:
        observation = get_station_latest_observation(station)
        ref = STATION_REFERENCE[station]
        forecast = get_forecast_pop(ref["lat"], ref["lon"])
    elif looks_like_us_station(station):
        observation = get_station_latest_observation(station)
    return observation, forecast


def pick_forecast_temp_f(measure: str | None, forecast) -> float | None:
    """The forecast temperature relevant to this market's measure."""
    if not forecast:
        return None
    daytime = measure == "temperature_high"
    for period in forecast:
        if getattr(period, "is_daytime", None) is daytime:
            return getattr(period, "temperature_f", None)
    return getattr(forecast[0], "temperature_f", None)


def collect_series(kalshi: KalshiClient, extractor: RulesExtractor,
                   series_ticker: str, weather_cache: dict,
                   rules_cache: dict | None = None) -> int:
    """Record every open market in one series. Returns markets recorded."""
    found = []
    cursor = None
    try:
        for _ in range(PAGINATION_CAP):
            resp = kalshi.get_markets(series_ticker=series_ticker,
                                      status="open", cursor=cursor)
            found.extend(resp.get("markets", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
    except Exception as exc:
        log.error("Failed to fetch markets for %s: %s", series_ticker, exc)
        return 0

    if found:
        log.info("Series %s: %d open market(s)", series_ticker, len(found))

    recorded = 0
    for market in found:
        ticker = market.get("ticker")
        if not ticker:
            continue
        try:
            recorded += _record_market(kalshi, extractor, market, ticker,
                                       weather_cache)
        except Exception as exc:
            # One bad market must never stop the scan. Collection outranks
            # completeness of any single row -- bot.py learned this the hard
            # way, where an uncaught rules-fetch failure killed the rest of
            # the series for that cycle.
            log.warning("Recording failed for %s (non-fatal): %s", ticker, exc)
    return recorded


def _record_market(kalshi, extractor, market, ticker, weather_cache) -> int:
    # market_price_cents reads Kalshi's "<field>_dollars" string keys; a
    # plain market.get("yes_ask") silently returns None because that key
    # does not exist on this endpoint.
    yes_ask = market_price_cents(market, "yes_ask")
    yes_bid = market_price_cents(market, "yes_bid")
    no_ask = market_price_cents(market, "no_ask")
    no_bid = market_price_cents(market, "no_bid")

    storage.log_price_snapshot(ticker, yes_ask, yes_bid)

    rules_text = kalshi.get_market_rules_text(ticker)
    rules = extractor.extract(ticker, rules_text)
    station_code = getattr(rules, "station_code", None)
    measure = getattr(rules, "measure", None)

    # One weather fetch per station per cycle, not per market. The old
    # loop refetched for all 12 markets of a city inside the same second.
    if station_code not in weather_cache:
        weather_cache[station_code] = fetch_weather(station_code)
    observation, forecast = weather_cache[station_code]

    forecast_temp_f = pick_forecast_temp_f(measure, forecast)
    if measure in ("temperature_high", "temperature_low"):
        storage.log_forecast_snapshot(ticker, forecast_temp_f)

    pop = None
    if forecast:
        pops = [p.probability_of_precipitation_pct for p in forecast
                if getattr(p, "probability_of_precipitation_pct", None) is not None]
        pop = max(pops) if pops else None

    storage.log_market_snapshot(
        ticker,
        event_ticker=market.get("event_ticker"),
        station_code=station_code,
        measure=measure,
        yes_ask=yes_ask, yes_bid=yes_bid, no_ask=no_ask, no_bid=no_bid,
        observed_temp_f=getattr(observation, "temperature_f", None),
        forecast_temp_f=forecast_temp_f,
        precip_pop_pct=pop,
        observed_precip_mm=getattr(observation, "precipitation_last_hour_mm", None),
        threshold_low_f=getattr(rules, "threshold_low_f", None),
        threshold_high_f=getattr(rules, "threshold_high_f", None),
        hours_until_close=None,
        # NULL on purpose -- see the module docstring. Nothing is
        # forecasting here, and the column should say so.
        model_probability_yes=None,
        close_time=market.get("close_time"),
    )
    return 1


def run_cycle(kalshi: KalshiClient, extractor: RulesExtractor,
              series_tickers) -> int:
    weather_cache: dict = {}
    total = 0
    for series_ticker in series_tickers:
        total += collect_series(kalshi, extractor, series_ticker, weather_cache)
    log.info("Cycle complete: %d markets recorded across %d series, "
             "%d station weather fetches", total, len(series_tickers),
             len(weather_cache))
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_CYCLE_SECONDS)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s")

    storage.init_db()
    kalshi = KalshiClient()
    extractor = RulesExtractor()

    series = tuple(SETTINGS.series_tickers)
    if getattr(SETTINGS, "auto_discover_series", False):
        discovered = set()
        for keyword in SETTINGS.discovery_keywords:
            try:
                discovered.update(kalshi.discover_series_tickers(keyword))
            except Exception as exc:
                log.warning("Series discovery failed for %r: %s", keyword, exc)
        series = series + tuple(sorted(discovered - set(series)))
    log.info("Collector starting. %d series. NOTHING TRADES -- this process "
             "has no order path, no strategy and no position limits.",
             len(series))

    last_outcome_backfill = 0.0
    while True:
        started = time.time()
        try:
            run_cycle(kalshi, extractor, series)
        except Exception:
            log.exception("Cycle failed; continuing")

        # Labels. Without this the snapshots accumulate with no outcome
        # attached, and the archive becomes a pile of features nothing can
        # be trained against. It was the only part of bot.py's main loop
        # that had nothing to do with trading, and it is the part that
        # matters most. Hourly, as before -- checking every scanned ticker
        # every cycle would be a large, mostly redundant burst of API
        # calls.
        if time.time() - last_outcome_backfill > OUTCOME_BACKFILL_SECONDS:
            try:
                import settlement
                n = settlement.backfill_market_outcomes(kalshi)
                if n:
                    log.info("Recorded outcomes for %d settled market(s)", n)
            except Exception:
                log.exception("Outcome backfill failed; continuing")
            last_outcome_backfill = time.time()

        if args.once:
            return 0
        time.sleep(max(1.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    sys.exit(main())
