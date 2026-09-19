"""
Layer 1 as a harness candidate.

Wraps a trained weatherman model in the Forecaster interface, so it faces
exactly the same trader, execution model, fees and gates as the market
price and the constant baseline. That symmetry is the point: any
difference in the result then comes from the forecast rather than from
the candidate being allowed to trade differently.

Everything it reads goes through the PointInTimeView, so it is subject to
the same filter as any other candidate. It cannot see an observation that
had not been reported or a forecast that had not been issued, and it has
no route to the label at all.
"""
from __future__ import annotations

import datetime as dt
import math

import weatherman
from evaluation import baselines

DAY = 86400
HOUR = 3600


class WeathermanForecaster(baselines.Forecaster):
    """A trained Layer 1 model, reading only what the view permits."""

    kind = "weatherman"

    def __init__(self, model: weatherman.LogisticModel, lead_hours: int = 24,
                 climatology: dict | None = None,
                 default_climo: float = 0.35,
                 station_of=None):
        self.model = model
        self.lead_hours = lead_hours
        self.climatology = climatology or {}
        self.default_climo = default_climo
        self.name = f"weatherman(lead={lead_hours}h)"
        self._station_of = station_of or _default_station_of

    def probability(self, view, terms, as_of: int) -> float | None:
        station = self._station_of(terms.station_code)
        if station is None:
            return None
        close = _iso(terms.close_time)
        if close is None:
            return None

        # The window the contract settles on: validated at 98.7% against
        # 462 real settlements in analysis/validate_wx.py, and 8.7 points
        # better than using the UTC calendar day.
        lo, hi = close - DAY, close
        if as_of > lo:
            # Deciding inside the window would mean the model can see part
            # of the day it is predicting. Decline rather than quietly
            # forecasting an event already half-resolved.
            return None

        cache = weatherman.ArchiveCache()
        for row in view.observations(station):
            if row.get("precip_in") is not None:
                cache.obs[row["valid_at"]] = row["precip_in"]
        for row in view.forecasts_at_lead(station, self.lead_hours):
            if row.get("precip_mm") is not None:
                cache.fc_precip[row["valid_at"]] = row["precip_mm"]
            if row.get("precip_prob_pct") is not None:
                cache.fc_pop[row["valid_at"]] = row["precip_prob_pct"]

        month = dt.datetime.fromtimestamp(lo, dt.timezone.utc).month
        climo = self.climatology.get((station, month), self.default_climo)
        x = weatherman.features_for(cache, lo, hi, climo, self.lead_hours)
        if x is None:
            return None
        p = self.model.predict(x)
        return min(max(p, 0.001), 0.999) if math.isfinite(p) else None


def _default_station_of(station_code):
    import weather_archive
    try:
        return weather_archive.asos_id(station_code)
    except Exception:
        return None


def _iso(value):
    if not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(
            value.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def load(path: str, lead_hours: int = 24, climatology: dict | None = None
         ) -> WeathermanForecaster:
    with open(path) as fh:
        model = weatherman.LogisticModel.from_json(fh.read())
    return WeathermanForecaster(model, lead_hours=lead_hours,
                                 climatology=climatology)
