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
import datetime as dt
import gzip
import json
import logging
import os
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
EXTRA_SERIES_SECONDS = 360

# Non-weather series, collected generically: prices, displayed size,
# outcomes and settlement text, with no weather lookup and no LLM rules
# extraction.
#
# Chosen from a survey of all 14,163 series (analysis/survey_markets.py)
# on the two criteria that actually decided every question tonight --
# displayed depth and instance count -- with a third group added for the
# thesis rather than the liquidity.
#
# The scale difference is not marginal. Weather's median thinnest bracket
# leg showed ZERO contracts; NFL games show 71,825 at a 1.5c spread with
# 100% of markets quoted. Weather does not appear in the exchange's top
# 25 series by volume at all.
DEEP_AND_LIQUID = (
    # Deep books, tight spreads, and a fixture list that repeats forever,
    # which is what a model needs and what rain markets never had.
    "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL",
    "KXEPLGAME", "KXEPLSPREAD", "KXEPLTOTAL",
    "KXLALIGAGAME", "KXLALIGATOTAL", "KXSERIEAGAME", "KXSERIEATOTAL",
    "KXBUNDESLIGAGAME", "KXLIGUE1GAME",
    "KXMLBGAME", "KXUFCFIGHT",
)
AMBIGUOUS_SETTLEMENT = (
    # Where the project's actual thesis lives. Sports settle on "did the
    # team win", which nobody misreads -- there is no CLI product, no
    # trace, no gap between the event and the record of it. These are the
    # opposite: the settlement SOURCE is the interesting part.
    "KXRT",              # a published score, read at a particular moment
    "KXTRUMPMENTION",    # whether specific words were said
    "KXHORMUZWEEKLY",    # a geopolitical condition someone has to adjudicate
)

# Player props, added after reading their actual contract text rather
# than assuming. Two clauses put them in this group and not the one
# above:
#
#   "If [player] records 3+ hits ..."  -- a HIT is an official scorer's
#   ruling, not an observable fact. Hit or error is a judgment call, and
#   it can be revised after the game has ended.
#
#   "If [player] is scratched or not included in the starting lineup,
#   the market will resolve to the FAIR MARKET PRICE."  -- an outcome
#   that is neither yes nor no, hanging on a lineup card posted an hour
#   before first pitch.
#
# That is the Austin loss exactly: right about the world, wrong about
# what the contract reads.
#
# Depth was measured before committing cycles to them, and it is better
# than the settled sample implied -- 100% of these are quoted, at 2-7c
# spreads with 150-230 contracts on the bid:
#
#   KXMLBHIT   200 open  100% quoted   5c   230 bid
#   KXMLBHRR   200 open  100% quoted   4c   150 bid
#   KXMLBKS    195 open  100% quoted   3c    25 bid
#
# Deliberately EXCLUDED after that measurement: KXEPLFIRSTGOAL (74c
# spread), KXLALIGAFIRSTGOAL (88c), KXLALIGAGOAL (89c). All quoted with
# ZERO on the bid, which is a displayed price with nothing behind it --
# no signal to learn from, and a request per cycle forever to collect
# it. Settlement-wise they are interesting; as data they are empty.
AMBIGUOUS_PROPS = (
    "KXMLBHIT", "KXMLBTB", "KXMLBHRR", "KXMLBRBI", "KXMLBSB",
    "KXMLBKS", "KXMLBHR",
    "KXWNBAPTS", "KXWNBAREB",
    "KXEPLGOAL",
)

# The highest-volume families on the exchange, ranked on SETTLED history
# by volume per market rather than by count -- count is how weather
# looked promising and was not. For scale, the 22,000-market parlay
# series that tops the raw count trades 867 per market:
#
#   KXNBAGAME     4,035,993 per market      KXWNBAGAME    679,929
#   KXNCAAFGAME   2,147,871                 KXNHLGAME     509,911
#   KXATPMATCH      797,362                 KXWTAMATCH    459,802
#
# These are already being backfilled. Collecting them LIVE is a separate
# and more urgent thing: a settled market's price history can be fetched
# whenever, but the book as it stood this minute cannot. NCAAF is in
# season now and basketball and hockey start next month.
HIGH_VOLUME = (
    "KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL",
    "KXNHLGAME",
    "KXNCAAFGAME",
    "KXWNBAGAME",
    # No ESPN ground truth -- its tennis scoreboard lists tournaments
    # rather than matches, and it does not cover esports. Collected for
    # price and outcome, which is still worth having at this volume.
    "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH",
    "KXITFMATCH", "KXITFWMATCH",
    "KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME",
)

EXTRA_SERIES = (DEEP_AND_LIQUID + AMBIGUOUS_SETTLEMENT + AMBIGUOUS_PROPS
                + HIGH_VOLUME + ("KXMAMDANIMENTION",))

# Settlement rules, written once per ticker to a gzipped file rather
# than to a snapshot column. The text is static for a market's whole
# life, so putting it in market_snapshots would repeat the same
# paragraph every cycle for every market -- the duplication that already
# cost 230 MB a day once. Files also keep the droplet flat and ride the
# existing sync home.
RULES_DIR = "data/market_rules"


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


def _fp(market, key) -> float | None:
    """Kalshi's fixed-point string fields. market.get('volume') is always
    None -- the key is volume_fp and the value is a string."""
    raw = market.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


_rules_seen: set[str] = set()


def record_rules(market: dict, series_ticker: str,
                 directory: str = RULES_DIR) -> bool:
    """Write a market's settlement text once, the first time it is seen.

    This used to be claimed in a docstring and not actually done -- the
    list endpoint returns rules_primary and rules_secondary on every
    market and the collector dropped both. That is the single field that
    separates "I was wrong about the game" from "I was wrong about what
    the contract reads", and it disappears when the market does.

    Returns True if this call wrote the market for the first time.
    """
    ticker = market.get("ticker")
    if not ticker or ticker in _rules_seen:
        return False
    _rules_seen.add(ticker)

    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(directory, series_ticker, f"{day}.jsonl.gz")
    record = {
        "ticker": ticker,
        "series_ticker": series_ticker,
        "event_ticker": market.get("event_ticker"),
        "title": market.get("title"),
        "yes_sub_title": market.get("yes_sub_title"),
        "no_sub_title": market.get("no_sub_title"),
        "rules_primary": market.get("rules_primary"),
        "rules_secondary": market.get("rules_secondary"),
        "strike_type": market.get("strike_type"),
        "custom_strike": market.get("custom_strike"),
        "market_type": market.get("market_type"),
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time"),
        "expiration_time": market.get("expiration_time"),
        "settlement_timer_seconds": market.get("settlement_timer_seconds"),
        "can_close_early": market.get("can_close_early"),
        "early_close_condition": market.get("early_close_condition"),
        "observed_ts": int(time.time()),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":"),
                                default=str) + "\n")
        return True
    except OSError as exc:
        # Losing a rules write must not take the price collection down
        # with it -- prices are perishable, and this file can be rebuilt
        # from the next cycle.
        log.warning("Rules write failed for %s (non-fatal): %s", ticker, exc)
        _rules_seen.discard(ticker)
        return False


def collect_generic_series(kalshi: KalshiClient, series_ticker: str) -> int:
    """Record a non-weather series: prices, displayed size, and terms.

    No weather lookup and no LLM rules extraction -- but the settlement
    text the list endpoint already returns IS now stored, once per
    ticker, via record_rules().
    """
    found = []
    cursor = None
    try:
        for _ in range(PAGINATION_CAP):
            resp = kalshi.get_markets(series_ticker=series_ticker,
                                      status="open", cursor=cursor, limit=200)
            found.extend(resp.get("markets", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
    except Exception as exc:
        log.warning("Fetch failed for %s: %s", series_ticker, exc)
        return 0

    recorded = 0
    new_rules = 0
    for market in found:
        ticker = market.get("ticker")
        if not ticker:
            continue
        if record_rules(market, series_ticker):
            new_rules += 1
        try:
            yes_ask = market_price_cents(market, "yes_ask")
            yes_bid = market_price_cents(market, "yes_bid")
            # Deliberately NOT log_price_snapshot as well. market_snapshots
            # already carries yes_bid and yes_ask, so writing both doubles
            # the row count for the same numbers -- and at 1,459 markets a
            # cycle that duplication alone was 230 MB a day.
            storage.log_market_snapshot(
                ticker,
                event_ticker=market.get("event_ticker"),
                station_code=None,
                measure=series_ticker,
                yes_ask=yes_ask, yes_bid=yes_bid,
                no_ask=market_price_cents(market, "no_ask"),
                no_bid=market_price_cents(market, "no_bid"),
                # Displayed size rides in the threshold columns rather
                # than adding a table. Not elegant, but the alternative is
                # a migration on a live database to bank data that is
                # perishable -- a quote that was there this minute is not
                # recoverable next week.
                threshold_low_f=_fp(market, "yes_bid_size_fp"),
                threshold_high_f=_fp(market, "yes_ask_size_fp"),
                hours_until_close=None,
                model_probability_yes=None,
                close_time=market.get("close_time"),
            )
            recorded += 1
        except Exception as exc:
            log.warning("Recording failed for %s (non-fatal): %s", ticker, exc)
    if recorded:
        log.info("Series %s: %d markets recorded%s", series_ticker, recorded,
                 f", {new_rules} new rules captured" if new_rules else "")
    return recorded


def run_cycle(kalshi: KalshiClient, extractor: RulesExtractor,
              series_tickers, extra_series=EXTRA_SERIES,
              include_extra: bool = True) -> int:
    weather_cache: dict = {}
    total = 0
    for series_ticker in series_tickers:
        total += collect_series(kalshi, extractor, series_ticker, weather_cache)
    weather_total = total
    for series_ticker in (extra_series if include_extra else ()):
        total += collect_generic_series(kalshi, series_ticker)
    log.info("Cycle complete: %d weather markets across %d series "
             "(%d station fetches), %d other markets across %d series",
             weather_total, len(series_tickers), len(weather_cache),
             total - weather_total, len(extra_series))
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
    last_extra = 0.0
    while True:
        started = time.time()
        # Weather every cycle; the other families every EXTRA_INTERVAL.
        # Those 1,459 markets are being banked for a model to train on
        # later, not traded on now, so six-minute resolution loses
        # nothing a tick feed cannot supply -- and at two minutes they
        # alone were 230 MB a day.
        do_extra = (time.time() - last_extra) >= EXTRA_SERIES_SECONDS
        try:
            run_cycle(kalshi, extractor, series, include_extra=do_extra)
            if do_extra:
                last_extra = time.time()
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

