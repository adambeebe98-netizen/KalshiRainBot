"""
Pulls weather data from api.weather.gov (free, no key required).

The whole point of this module is to answer the SAME question the Kalshi
contract will be graded on — precipitation at a specific station — not
"is it raining somewhere in the metro area." Station precision is the edge.

api.weather.gov gives NWS data. Some Kalshi rain contracts settle on
The Weather Company instead of raw NWS — rules_extractor.py records which
source a given market actually uses so you know when NWS data is only a
proxy/estimate rather than the literal settlement feed.
"""
from __future__ import annotations

import httpx
from dataclasses import dataclass
from typing import Optional

NWS_BASE = "https://api.weather.gov"
HEADERS = {"User-Agent": "kalshi-weather-bot (contact: set-your-email-here)"}


@dataclass
class StationObservation:
    station_id: str
    precipitation_last_hour_mm: Optional[float]
    precipitation_last_3hr_mm: Optional[float]
    temperature_f: Optional[float]  # converted from NWS's Celsius observation
    description: str
    timestamp: str


@dataclass
class PrecipForecast:
    """
    Name kept as-is for backward compatibility with existing callers, but
    this now carries the full forecast period — including temperature and
    is_daytime — since it's the SAME NWS periods payload either way and a
    second fetch would just be wasteful. Used by both the precipitation
    model (strategy.estimate_precip_probability) and the temperature model
    (strategy.estimate_temperature_probability).
    """
    period_name: str
    probability_of_precipitation_pct: Optional[int]
    short_forecast: str
    temperature_f: Optional[float]
    is_daytime: Optional[bool]


def get_station_latest_observation(station_id: str) -> Optional[StationObservation]:
    """station_id is an NWS station code, e.g. 'KAUS' for Austin-Bergstrom."""
    url = f"{NWS_BASE}/stations/{station_id}/observations/latest"
    with httpx.Client(headers=HEADERS, timeout=10.0) as client:
        resp = client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json().get("properties", {})
        precip_1h = (data.get("precipitationLastHour") or {}).get("value")
        precip_3h = (data.get("precipitationLast3Hours") or {}).get("value")
        temp_c = (data.get("temperature") or {}).get("value")
        temp_f = (temp_c * 9 / 5 + 32) if temp_c is not None else None
        return StationObservation(
            station_id=station_id,
            precipitation_last_hour_mm=precip_1h,
            precipitation_last_3hr_mm=precip_3h,
            temperature_f=temp_f,
            description=data.get("textDescription", ""),
            timestamp=data.get("timestamp", ""),
        )


def get_forecast_pop(lat: float, lon: float) -> list[PrecipForecast]:
    """
    Probability-of-precipitation forecast for a lat/lon. This is a genuine
    forecast (not an observation) — use it for markets that haven't resolved
    yet, and treat it as one input to your model, not the model itself.
    """
    with httpx.Client(headers=HEADERS, timeout=10.0) as client:
        points = client.get(f"{NWS_BASE}/points/{lat},{lon}")
        points.raise_for_status()
        forecast_url = points.json()["properties"]["forecast"]
        forecast = client.get(forecast_url)
        forecast.raise_for_status()
        periods = forecast.json()["properties"]["periods"]
        out = []
        for p in periods:
            pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
            out.append(PrecipForecast(
                period_name=p.get("name", ""),
                probability_of_precipitation_pct=pop,
                short_forecast=p.get("shortForecast", ""),
                temperature_f=p.get("temperature"),  # NWS returns this in temperatureUnit, "F" by default for this endpoint
                is_daytime=p.get("isDaytime"),
            ))
        return out


# Common settlement-station reference points for major Kalshi weather cities.
# Extend this as you add series. Coordinates are for the named station itself,
# not just "the city," since the station is what actually gets graded.
STATION_REFERENCE = {
    "KAUS": {"name": "Austin-Bergstrom Intl", "lat": 30.1975, "lon": -97.6664},
    "KLAS": {"name": "Las Vegas Harry Reid Intl", "lat": 36.0840, "lon": -115.1537},
    "KMDW": {"name": "Chicago Midway", "lat": 41.7868, "lon": -87.7522},
    "KHOU": {"name": "Houston Hobby", "lat": 29.6454, "lon": -95.2789},
    "KDEN": {"name": "Denver Intl", "lat": 39.8561, "lon": -104.6737},
    # Added after confirming Kalshi's CLI-prefixed settlement-source codes
    # were being passed straight through to weather.gov's observation API,
    # which needs the real ICAO/airport code instead (see
    # kalshi_station_to_nws_id below) — an entire night's worth of 404s
    # on every one of these before the translation existed.
    "KORD": {"name": "Chicago O'Hare Intl", "lat": 41.9742, "lon": -87.9073},
    "KPHX": {"name": "Phoenix Sky Harbor Intl", "lat": 33.4352, "lon": -112.0101},
    "KSEA": {"name": "Seattle-Tacoma Intl", "lat": 47.4502, "lon": -122.3088},
    "KSAN": {"name": "San Diego Intl", "lat": 32.7338, "lon": -117.1933},
    "KSAT": {"name": "San Antonio Intl", "lat": 29.5312, "lon": -98.4685},
    "KDCA": {"name": "Washington Reagan National", "lat": 38.8512, "lon": -77.0402},
    "KDFW": {"name": "Dallas/Fort Worth Intl", "lat": 32.8998, "lon": -97.0403},
    "KBOS": {"name": "Boston Logan Intl", "lat": 42.3656, "lon": -71.0096},
    "KATL": {"name": "Atlanta Hartsfield-Jackson Intl", "lat": 33.6407, "lon": -84.4277},
    "KSFO": {"name": "San Francisco Intl", "lat": 37.6213, "lon": -122.3790},
    "KMSY": {"name": "New Orleans Louis Armstrong Intl", "lat": 29.9934, "lon": -90.2580},
    "KOKC": {"name": "Oklahoma City Will Rogers World", "lat": 35.3931, "lon": -97.6007},
}


def kalshi_station_to_nws_id(kalshi_station_code: str | None) -> str | None:
    """
    rules_extractor's station_code comes back in Kalshi's OWN
    settlement-source format ("CLI" + a 3-letter city code, e.g. "CLIHOU"
    for Houston) — confirmed live via a full night of weather.gov 404s on
    every single station lookup, since that API needs the real
    ICAO/airport identifier instead (e.g. "KHOU"). Every case checked
    follows the same rule: strip the "CLI" prefix, add "K"
    (CLIHOU->KHOU, CLIAUS->KAUS, CLIORD->KORD, CLIPHX->KPHX, CLISEA->KSEA,
    CLISAN->KSAN, CLISAT->KSAT, CLIDCA->KDCA, CLIDFW->KDFW, CLIBOS->KBOS,
    CLIATL->KATL, CLISFO->KSFO, CLIMSY->KMSY, CLIOKC->KOKC).

    Only covers US stations — weather.gov is a US-only NWS system, so an
    international city (Tokyo, London, Singapore, etc., all of which show
    up in Kalshi's discovered temperature series) will never get real
    observation/forecast data through this path no matter how its station
    code is translated. Returns the input unchanged if it doesn't match
    the CLI+3-letter pattern, so this is safe to call on anything without
    needing to know in advance what format a given value is already in.
    """
    if kalshi_station_code and kalshi_station_code.startswith("CLI") and len(kalshi_station_code) == 6:
        return "K" + kalshi_station_code[3:]
    return kalshi_station_code


def looks_like_us_station(station: str | None) -> bool:
    """
    A plausible US ICAO/airport code — 4 letters, K-prefixed (the standard
    for the continental US). Every international city seen in Kalshi's
    discovered temperature series (RJTT/Tokyo, RKSI/Seoul, LFPG/Paris,
    LSGG/Geneva, EDDF/Frankfurt, EGLL/London, EHAM/Amsterdam,
    EBBR/Brussels, MMMX/Mexico City, CYYZ/Toronto, VABB/Mumbai,
    VHHH/Hong Kong, WSSS/Singapore, YSSY/Sydney, ZBAA/Beijing, ZSPD/Shanghai)
    uses a non-K prefix. weather.gov is a US-only NWS system, so a
    non-US-shaped code would 404 every single cycle with zero chance of
    ever succeeding — this lets the caller skip that call entirely rather
    than pay the cost (and add log noise) for something that structurally
    can't be fixed from this side. Deliberately permissive rather than an
    exhaustive allowlist: any 4-letter K-code is treated as "worth trying,"
    even ones not yet in STATION_REFERENCE, since a plausible US station
    still might resolve.
    """
    return bool(station) and station.startswith("K") and len(station) == 4
