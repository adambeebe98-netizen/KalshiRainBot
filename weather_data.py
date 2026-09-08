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
    description: str
    timestamp: str


@dataclass
class PrecipForecast:
    period_name: str
    probability_of_precipitation_pct: Optional[int]
    short_forecast: str


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
        return StationObservation(
            station_id=station_id,
            precipitation_last_hour_mm=precip_1h,
            precipitation_last_3hr_mm=precip_3h,
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
}
