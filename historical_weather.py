"""
Foundation for retrospective backtesting: reconstructs what the weather
picture looked like at points in the past, for markets whose settlement
outcome and price history are separately retrievable from Kalshi's own
historical archive (see kalshi_client.get_historical_markets and
get_historical_candlesticks). Explicitly requested: "if we go back,
extract all of that available data that is applicable to the trades."

Uses Open-Meteo (https://open-meteo.com), a free, no-API-key weather
archive — NWS's own api.weather.gov (what weather_data.py uses for LIVE
data) only ever serves the CURRENT forecast; it has no way to ask "what
was the forecast on this past date." Two distinct Open-Meteo products
answer two genuinely distinct questions:

- HISTORICAL FORECAST (historical-forecast-api.open-meteo.com/v1/forecast):
  per Open-Meteo's own docs, this "closely tracks actual conditions
  because each run is initialised from real measurements" — the closest
  available proxy for "what would a forecast have shown at this moment,"
  matching the same principle the live bot already relies on (always use
  the most recent available forecast). This is NOT an exact reproduction
  of NWS's own human-curated forecast — it's a different, related model
  blend — but the best available approximation of it. Coverage starts
  2021 for GFS (the model closest to what NWS itself blends from),
  varies for other models.
- HISTORICAL OBSERVATION / ERA5 reanalysis (archive-api.open-meteo.com/v1/archive):
  the actual ground-truth record of what happened, back to 1940,
  independent of any forecast model. Used for "what really happened,"
  the counterpart to the forecast above.

IMPORTANT, VERIFIED LIMITATION: this module's network calls could not be
tested against the real Open-Meteo API from the environment that wrote
it — that environment's own network allowlist blocks
historical-forecast-api.open-meteo.com and archive-api.open-meteo.com
(confirmed directly: both return HTTP 403 there). This was built
strictly against Open-Meteo's documented request/response shapes,
verified via their own docs pages, not against a live response this
module's author actually saw. The first real validation happens
wherever this actually runs with real network access — likely the
droplet, which already successfully reaches api.weather.gov and Kalshi's
live API for real trading. Every function here fails loudly (raises) on
an unexpected response shape rather than silently returning a plausible-
looking but wrong number, specifically so a bad assumption about the
API surfaces immediately instead of quietly polluting the backfilled
dataset.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import httpx

log = logging.getLogger("historical_weather")

HISTORICAL_FORECAST_BASE = "https://historical-forecast-api.open-meteo.com/v1/forecast"
HISTORICAL_OBSERVATION_BASE = "https://archive-api.open-meteo.com/v1/archive"

_TIMEOUT_SECONDS = 30.0


@dataclass
class HistoricalHourlyPoint:
    """One hour of reconstructed historical weather. precipitation_probability_pct
    is only ever populated for forecast points — an observation is a
    record of what happened, not a probability, so it stays None there
    rather than being filled with a meaningless placeholder."""
    timestamp: str  # ISO 8601, exactly as Open-Meteo returns it
    temperature_f: Optional[float]
    precipitation_mm: Optional[float]
    precipitation_probability_pct: Optional[float] = None


def _celsius_to_f(c: Optional[float]) -> Optional[float]:
    if c is None:
        return None
    return c * 9 / 5 + 32


def _fetch_hourly(base_url: str, lat: float, lon: float, start_date: date, end_date: date,
                   hourly_vars: str, model: Optional[str] = None) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": hourly_vars,
        "temperature_unit": "celsius",  # explicit, not relying on Open-Meteo's default —
                                          # converted to F ourselves right after the call so
                                          # a silent default change on their side can't
                                          # quietly corrupt every temperature in the dataset
    }
    if model:
        params["models"] = model
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
        response = client.get(base_url, params=params)
        response.raise_for_status()
        return response.json()


def get_historical_forecast_hourly(lat: float, lon: float, start_date: date, end_date: date,
                                     model: str = "best_match") -> list[HistoricalHourlyPoint]:
    """
    What the forecast would have shown, hour by hour, over this date
    range — the closest available reconstruction of what the live bot's
    forecast-driven model would have seen at each point in a market's
    life. See this module's docstring for why "best_match" (Open-Meteo's
    own blended best-available model per location) is used rather than
    a single fixed model, and for the real caveat that this is a
    different, related model blend from NWS's own official forecast,
    not an exact reproduction of it.
    """
    data = _fetch_hourly(HISTORICAL_FORECAST_BASE, lat, lon, start_date, end_date,
                          hourly_vars="temperature_2m,precipitation,precipitation_probability",
                          model=model)
    hourly = data.get("hourly")
    if not hourly or "time" not in hourly:
        raise ValueError(f"Unexpected historical forecast response shape (no 'hourly.time'): {data}")
    times = hourly["time"]
    temps_c = hourly.get("temperature_2m", [None] * len(times))
    precip = hourly.get("precipitation", [None] * len(times))
    pop = hourly.get("precipitation_probability", [None] * len(times))
    return [
        HistoricalHourlyPoint(
            timestamp=t,
            temperature_f=_celsius_to_f(temps_c[i] if i < len(temps_c) else None),
            precipitation_mm=precip[i] if i < len(precip) else None,
            precipitation_probability_pct=pop[i] if i < len(pop) else None,
        )
        for i, t in enumerate(times)
    ]


def get_historical_observation_hourly(lat: float, lon: float, start_date: date,
                                        end_date: date) -> list[HistoricalHourlyPoint]:
    """
    What actually happened, hour by hour, over this date range — ERA5
    reanalysis, independent of any forecast model. No model= parameter:
    the observation archive isn't per-model, it's the reconstructed
    ground truth. precipitation_probability_pct always stays None here
    (see HistoricalHourlyPoint's docstring for why).
    """
    data = _fetch_hourly(HISTORICAL_OBSERVATION_BASE, lat, lon, start_date, end_date,
                          hourly_vars="temperature_2m,precipitation")
    hourly = data.get("hourly")
    if not hourly or "time" not in hourly:
        raise ValueError(f"Unexpected historical observation response shape (no 'hourly.time'): {data}")
    times = hourly["time"]
    temps_c = hourly.get("temperature_2m", [None] * len(times))
    precip = hourly.get("precipitation", [None] * len(times))
    return [
        HistoricalHourlyPoint(
            timestamp=t,
            temperature_f=_celsius_to_f(temps_c[i] if i < len(temps_c) else None),
            precipitation_mm=precip[i] if i < len(precip) else None,
        )
        for i, t in enumerate(times)
    ]
