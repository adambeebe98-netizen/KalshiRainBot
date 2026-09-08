"""
Maps each market's `measure` field (set by rules_extractor.py — see its
prompt there for the full enum: 'precipitation_daily', 'precipitation_monthly',
'temperature_high', 'temperature_low', 'other') to a human-facing trading
category, so performance can be sliced by "what kind of bet is this" instead
of one aggregate number.

Why this exists as its own tiny module: as more categories get added (snow
was discussed — see README/handoff notes), there's exactly one place to
extend, instead of scattered if/elif checks in storage.py, web_ui/app.py,
and reporting.py drifting out of sync with each other.
"""
from __future__ import annotations

CATEGORY_BY_MEASURE: dict[str, str] = {
    "precipitation_daily": "Rain",
    "precipitation_monthly": "Rain",
    # Snow isn't built yet (discussed as a candidate, using the existing
    # precipitation_monthly measure type) — when it lands, give it its own
    # 'precipitation_monthly_snow' measure or similar and add a line here
    # rather than lumping it into Rain by default.
    "temperature_high": "Temperature",
    "temperature_low": "Temperature",
    "other": "Other",
}

# Category display order on the dashboard — anything not listed here
# (a new measure added without a matching category update) falls back to
# appearing after these, alphabetically, so it's visible rather than silently
# dropped.
CATEGORY_ORDER = ["Rain", "Temperature", "Other"]

# Minimum settled trades before a win-rate/ROI number is trusted enough to
# act on rather than reflecting a hot or cold streak. Matches the threshold
# already used for calibration_stats — kept as one shared constant instead
# of two numbers that could quietly drift apart.
MIN_SAMPLE_SIZE = 20


def category_for(measure: str | None) -> str:
    return CATEGORY_BY_MEASURE.get(measure or "other", "Other")
