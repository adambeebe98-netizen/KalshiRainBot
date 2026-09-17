"""
Orchestrates the full historical backfill: for a given Kalshi series,
pages through every settled market, extracts its station/measure/
threshold via the same RulesExtractor the live bot uses, pulls its real
price history, and reconstructs the weather picture for its station and
date range — joining Kalshi's own historical archive with Open-Meteo's
(see historical_weather.py). Explicitly requested: "if we go back,
extract all of that available data that is applicable to the trades."

Deliberately a standalone script, not part of bot.py's main loop — this
is a one-time or occasional bulk job over years of history, not
something to run every 5-minute cycle. Idempotent throughout: every
storage write here (save_historical_market, and weather backfill via the
has_historical_weather_for_station check) is safe to re-run over a
series that's already partly processed, so an interrupted run can just
be restarted rather than needing its own separate resume-tracking logic.

IMPORTANT, VERIFIED LIMITATION: built against Kalshi's and Open-Meteo's
documented response shapes (both fetched directly from their own docs
pages while writing this), not against a live response actually seen —
every external domain this depends on returned HTTP 403 from the
environment that wrote it (confirmed directly). Every step here is
wrapped so one market's failure (a malformed response, a station this
codebase doesn't have coordinates for, a transient API error) logs and
moves on to the next market rather than aborting the whole backfill.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import historical_weather
import storage
from kalshi_client import KalshiClient
from rules_extractor import RulesExtractor
from series_template import SeriesTemplate, build_template, try_apply_template
from weather_data import STATION_REFERENCE, kalshi_station_to_nws_id

log = logging.getLogger("historical_backfill")

# CONFIRMED LIVE, repeatedly, across two different cities: rules_extractor's
# LLM-based station_code inference hallucinates plausible-looking but WRONG
# 3-letter codes when a market's rules text states only a bare city name
# with no airport/station reference — Phoenix's rules text ("at Phoenix
# for...") produced "CLIPHO" (should be CLIPHX), and in the same live
# backfill run Austin's rules text produced TWO DIFFERENT wrong codes
# across different Austin markets in the same series (CLIAIS, then
# CLIAYC — neither is CLIAUS). This isn't a one-off fluke; it's a real
# reliability gap in free-text inference for this specific field. The
# series ticker itself is unambiguous where the free text apparently
# isn't to the LLM, so for series confirmed here, this ticker-based
# mapping overrides whatever the LLM extracted, rather than trusting a
# per-market guess that's been shown to vary market-to-market within the
# very same series. Every entry here was verified either against real
# rules text fetched live tonight, or via the standard CLI+3-letter-city
# pattern already confirmed correct for dozens of other cities. Series
# that genuinely rotate across multiple cities within one series (the
# base KXRAIN series, KXRAINWKND) are deliberately NOT included here —
# a series-level override would be actively wrong for those, since the
# station differs market to market within the same series ticker.
CONFIRMED_SERIES_STATION_OVERRIDES = {
    "KXHIGHAUS": "CLIAUS", "KXLOWTAUS": "CLIAUS", "KXRAINAUSM": "CLIAUS",
    "KXHIGHCHI": "CLIMDW", "KXLOWTCHI": "CLIMDW", "KXRAINCHIM": "CLIMDW",  # confirmed Midway, not O'Hare
    "KXHIGHDEN": "CLIDEN", "KXLOWTDEN": "CLIDEN", "KXRAINDENM": "CLIDEN",
    "KXHIGHLAX": "CLILAX", "KXLOWTLAX": "CLILAX", "KXRAINLAXM": "CLILAX",
    "KXHIGHMIA": "CLIMIA", "KXLOWTMIA": "CLIMIA", "KXRAINMIAM": "CLIMIA",
    "KXHIGHNY": "CLINYC", "KXLOWTNYC": "CLINYC", "KXRAINNYCM": "CLINYC", "KXRAINDNYC": "CLINYC",
    "KXHIGHPHIL": "CLIPHL", "KXLOWTPHIL": "CLIPHL",
    "KXHIGHTATL": "CLIATL", "KXLOWTATL": "CLIATL",
    "KXHIGHTBOS": "CLIBOS", "KXLOWTBOS": "CLIBOS",
    "KXHIGHTDAL": "CLIDFW", "KXLOWTDAL": "CLIDFW", "KXRAINDALM": "CLIDFW",
    "KXHIGHTDC": "CLIDCA", "KXLOWTDC": "CLIDCA",
    "KXHIGHTEWR": "CLIEWR", "KXLOWTEWR": "CLIEWR",
    "KXHIGHTHOU": "CLIHOU", "KXLOWTHOU": "CLIHOU", "KXRAINHOUM": "CLIHOU",
    "KXHIGHTLV": "CLILAS", "KXLOWTLV": "CLILAS",
    "KXHIGHTMIN": "CLIMSP", "KXLOWTMIN": "CLIMSP",
    "KXHIGHTNOLA": "CLIMSY", "KXLOWTNOLA": "CLIMSY",
    "KXHIGHTOKC": "CLIOKC", "KXLOWTOKC": "CLIOKC",
    "KXHIGHTPHX": "CLIPHX", "KXLOWTPHX": "CLIPHX",  # confirmed CLIPHO was a wrong LLM guess
    "KXHIGHTSAN": "CLISAN", "KXLOWTSAN": "CLISAN",
    "KXHIGHTSATX": "CLISAT", "KXLOWTSATX": "CLISAT",
    "KXHIGHTSDF": "CLISDF", "KXLOWTSDF": "CLISDF",
    "KXHIGHTSEA": "CLISEA", "KXLOWTSEA": "CLISEA", "KXRAINSEAM": "CLISEA",
    "KXHIGHTSFO": "CLISFO", "KXLOWTSFO": "CLISFO", "KXRAINSFOM": "CLISFO",
    "KXHIGHTTTN": "CLITTN", "KXLOWTTTN": "CLITTN",
    "KXRAINPVDM": "KPVD",  # confirmed via real rules text: KPVD directly, not CLI-prefixed
}


def apply_station_override(series_ticker: str, extracted_station_code: str | None) -> str | None:
    """Returns the confirmed override for this series if one exists,
    logging when it actually changes something so it's visible how often
    the LLM's guess would otherwise have been wrong. Returns the
    extractor's own value unchanged for any series not in the confirmed
    table -- this is a targeted correction for known-unreliable cases,
    not a replacement for extraction in general."""
    override = CONFIRMED_SERIES_STATION_OVERRIDES.get(series_ticker)
    if override is None:
        return extracted_station_code
    if extracted_station_code != override:
        log.info(f"{series_ticker}: overriding extracted station_code "
                 f"{extracted_station_code!r} with confirmed {override!r}")
    return override

# Courtesy delays between requests — these are free, no-API-key services
# (Open-Meteo) and Kalshi's own historical archive; a bulk backfill over
# years of markets should not hammer either one as fast as possible.
_PAGE_DELAY_SECONDS = 0.5
_PER_MARKET_DELAY_SECONDS = 0.2


def _parse_iso_to_unix(iso_str: str | None) -> int | None:
    """Same parsing pattern as bot.hours_until_close — returns None on a
    missing/malformed timestamp rather than raising, so one market with
    a bad timestamp doesn't abort the whole backfill."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _extract_candlestick_price_cents(candle: dict) -> int | None:
    """
    Candlestick prices arrive as dollar strings (e.g. "0.5600"), the same
    "_dollars" pattern this codebase already had one confirmed bug from
    for live markets (see market_price_cents) — never assume a plain
    numeric field. Prefers the actual traded price (price.close); if no
    trade occurred that period, falls back to the ask, then the bid,
    mirroring how the live bot already falls back when a quote is thin.
    """
    price = candle.get("price") or {}
    yes_ask = candle.get("yes_ask") or {}
    yes_bid = candle.get("yes_bid") or {}
    for source in (price.get("close"), yes_ask.get("close"), yes_bid.get("close")):
        if source is not None:
            try:
                return round(float(source) * 100)
            except (ValueError, TypeError):
                continue
    return None


def _extract_volume(candle: dict) -> int | None:
    volume_str = candle.get("volume")
    if volume_str is None:
        return None
    try:
        return round(float(volume_str))
    except (ValueError, TypeError):
        return None


def backfill_weather_for_station(station_code: str, start_ts: int, end_ts: int) -> bool:
    """
    Reconstructs the weather picture for one station over one date range,
    merging Open-Meteo's forecast and observation series by matching
    timestamp rather than assuming positional alignment between the two
    separate API responses (they could, in principle, have gaps in
    different places). Returns True if it actually fetched anything new,
    False if this station+range was already covered (see
    has_historical_weather_for_station) or coordinates aren't known for
    this station.

    station_code is Kalshi's own "CLI"-prefixed settlement-source format
    (e.g. "CLIAUS"), the SAME format historical_markets.station_code
    stores it in (matching what the live bot itself stores in
    market_snapshots/trades — see bot.py's own storage calls, which also
    store the raw code, translating only internally for the actual
    weather.gov lookup). CONFIRMED REAL BUG, caught on a live backfill
    run: every single station failed with "no coordinates known" because
    this function was looking up STATION_REFERENCE directly by the raw
    "CLI" code, which is never a key in that table — STATION_REFERENCE is
    keyed by the real ICAO code ("KAUS"), exactly the translation
    kalshi_station_to_nws_id exists for. The raw code is still what gets
    used as this function's OWN storage key (has_historical_weather_for_station /
    save_historical_weather_points), specifically so it stays joinable
    against historical_markets.station_code, which stores the same raw
    form — only the STATION_REFERENCE lookup and the actual Open-Meteo
    coordinates need the translated ICAO code.
    """
    if storage.has_historical_weather_for_station(station_code, start_ts, end_ts):
        return False

    nws_station = kalshi_station_to_nws_id(station_code)
    ref = STATION_REFERENCE.get(nws_station)
    if not ref:
        log.warning(f"No coordinates known for station {station_code} "
                    f"(translated: {nws_station}) — skipping weather backfill")
        return False

    start_date = datetime.fromtimestamp(start_ts, tz=timezone.utc).date()
    end_date = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()

    forecast_points = historical_weather.get_historical_forecast_hourly(
        ref["lat"], ref["lon"], start_date, end_date
    )
    observation_points = historical_weather.get_historical_observation_hourly(
        ref["lat"], ref["lon"], start_date, end_date
    )

    # Merge by ISO timestamp string, not by list position — the two API
    # calls are independent and could in principle return different
    # numbers of points if one has a data gap the other doesn't.
    merged: dict[str, dict] = {}
    for p in forecast_points:
        merged.setdefault(p.timestamp, {})["forecast_temp_f"] = p.temperature_f
        merged[p.timestamp]["forecast_precip_pop_pct"] = p.precipitation_probability_pct
    for p in observation_points:
        merged.setdefault(p.timestamp, {})["observed_temp_f"] = p.temperature_f
        merged[p.timestamp]["observed_precip_mm"] = p.precipitation_mm

    rows = []
    for ts_str, values in merged.items():
        ts_unix = _parse_iso_to_unix(ts_str)
        if ts_unix is None:
            continue
        rows.append((
            ts_unix,
            values.get("forecast_temp_f"),
            values.get("forecast_precip_pop_pct"),
            values.get("observed_temp_f"),
            values.get("observed_precip_mm"),
        ))

    storage.save_historical_weather_points(station_code, rows)
    return True


_MAX_REMEMBERED_TEMPLATES = 8  # small cap -- a series realistically has a
# handful of genuinely distinct wordings at most (e.g. separate phrasing
# for single-threshold "T" tickers vs bracket "B" tickers, or an older
# wording era), never dozens.


def backfill_one_market(kalshi: KalshiClient, extractor: RulesExtractor, market_obj: dict,
                          series_ticker: str, candlestick_interval: int = 60,
                          templates: list[SeriesTemplate] | None = None) -> list[SeriesTemplate]:
    """
    Backfills everything for one already-settled market: rules
    extraction, price history, and (if not already covered) its
    station's weather. Any single failure here should be caught by the
    caller and logged, not allowed to abort the whole series.

    templates holds every distinct wording template already learned for
    this series (not just the most recent one) -- CONFIRMED LIVE: a
    single-slot version of this initially caused Chicago's throughput to
    drop to ~35 markets/minute versus Austin's ~200/minute, and directly
    checking the LLM cache's growth rate confirmed why: roughly a third
    of Chicago's markets were still triggering a fresh LLM call, most
    likely because two genuinely different wordings (single-threshold
    "T" tickers vs bracket "B" tickers) are interleaved throughout its
    history, causing a single remembered template to be repeatedly
    forgotten and relearned every time the wording switched. Each
    template already-learned for this series is tried in turn; a full
    LLM extraction (and a new template added to the list) only happens
    when NONE of them cleanly apply to this specific market's text (see
    series_template.py for why a match is provably safe, not a guess).
    Returns the list, so the caller can pass it into the next market.
    """
    if templates is None:
        templates = []

    ticker = market_obj["ticker"]

    rules_text = kalshi.get_historical_market_rules_text(ticker)
    rules = None
    for tmpl in templates:
        rules = try_apply_template(tmpl, rules_text, ticker)
        if rules is not None:
            break

    if rules is None:
        rules = extractor.extract(ticker, rules_text)
        rules.station_code = apply_station_override(series_ticker, rules.station_code)
        new_template = build_template(rules_text, rules, market_obj.get("occurrence_datetime"))
        if new_template is not None:
            templates.append(new_template)
            if len(templates) > _MAX_REMEMBERED_TEMPLATES:
                templates.pop(0)  # evict the oldest, keep the cap small

    open_time = market_obj.get("open_time")
    close_time = market_obj.get("close_time")
    result = market_obj.get("result") or None

    storage.save_historical_market(
        ticker, series_ticker=series_ticker, event_ticker=market_obj.get("event_ticker"),
        station_code=rules.station_code, measure=rules.measure,
        threshold_low_f=rules.threshold_low_f, threshold_high_f=rules.threshold_high_f,
        open_time=open_time, close_time=close_time, result=result,
    )

    start_ts = _parse_iso_to_unix(open_time)
    end_ts = _parse_iso_to_unix(close_time)
    if start_ts is None or end_ts is None:
        log.warning(f"{ticker}: missing/malformed open_time or close_time — skipping price/weather backfill")
        return templates

    candlestick_data = kalshi.get_historical_candlesticks(
        series_ticker, ticker, start_ts, end_ts, period_interval=candlestick_interval
    )
    candles = candlestick_data.get("candlesticks", [])
    price_points = [
        (c["end_period_ts"], _extract_candlestick_price_cents(c), _extract_volume(c))
        for c in candles if "end_period_ts" in c
    ]
    storage.save_historical_price_points(ticker, price_points)

    if rules.station_code:
        backfill_weather_for_station(rules.station_code, start_ts, end_ts)

    return templates


def backfill_series(kalshi: KalshiClient, extractor: RulesExtractor, series_ticker: str,
                      max_markets: int | None = None, candlestick_interval: int = 60,
                      min_open_time: str | None = None) -> dict:
    """
    Pages through every settled market in one series and backfills each.
    Returns a summary dict — {"processed": N, "failed": N} — rather than
    raising, since a partial backfill (most markets succeeded, a few
    failed) is still a genuinely useful result, not something to discard.

    min_open_time (ISO 8601, e.g. "2025-03-17T00:00:00Z") bounds how far
    back this goes. CONFIRMED LIVE: Kalshi's own weather-market history
    for at least one series (KXHIGHAUS) extends nearly 2 years back —
    far older than the market's current liquidity/structure likely
    resembles, and far more than needed for either calibration (which
    doesn't need years of samples per city) or swing-trading pattern
    work (where an old, thinner market regime may not transfer to
    today's). Markets older than this are skipped individually rather
    than aborting the whole series — safer than trusting strict date
    ordering within a single page, since observed real pagination
    shows LOCAL date jumbling (e.g. an Apr-08 market appearing between
    Apr-27 and Apr-26 entries). Pagination itself only stops once an
    ENTIRE page (not a single market) comes back past the cutoff --
    confirmed live that pagination moves in a consistent overall
    direction (newest to oldest) even with that local jumbling, so a
    whole page past cutoff is a reliable stop signal without assuming
    perfect per-market ordering.
    """
    processed = 0
    failed = 0
    skipped_too_old = 0
    cursor = None
    templates: list[SeriesTemplate] = []  # every distinct wording template learned so far this series

    while True:
        page = kalshi.get_historical_markets(series_ticker=series_ticker, cursor=cursor)
        markets = page.get("markets", [])
        if not markets:
            break

        page_has_any_in_range = False
        for market_obj in markets:
            ticker = market_obj.get("ticker", "<unknown>")

            if min_open_time and (market_obj.get("open_time") or "") < min_open_time:
                skipped_too_old += 1
                continue
            page_has_any_in_range = True

            try:
                templates = backfill_one_market(kalshi, extractor, market_obj, series_ticker,
                                                   candlestick_interval, templates)
                processed += 1
            except Exception as e:
                log.warning(f"Failed to backfill {ticker}: {e}")
                failed += 1
            if max_markets and (processed + failed) >= max_markets:
                return {"processed": processed, "failed": failed, "skipped_too_old": skipped_too_old}
            time.sleep(_PER_MARKET_DELAY_SECONDS)

        if min_open_time and not page_has_any_in_range:
            log.info(f"{series_ticker}: entire page past min_open_time cutoff, stopping pagination early")
            break

        cursor = page.get("cursor")
        if not cursor:
            break
        time.sleep(_PAGE_DELAY_SECONDS)

    return {"processed": processed, "failed": failed, "skipped_too_old": skipped_too_old}
