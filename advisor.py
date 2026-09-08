"""
Periodically (see ADVISOR_INTERVAL_SECONDS in bot.py's main loop) asks
Claude to look at real performance data and suggest specific numeric
tweaks to the strategies that were explicitly built as starting guesses —
swing's entry/exit prices, favorites' price threshold, longshot's price
band. The calibrated model and arbitrage are never touched here; their
numbers come from validated risk presets, not a guess waiting to be
replaced.

CRITICAL BOUNDARY: this module only ever calls storage.log_suggestion().
It never calls storage.set_override() — that function is only ever called
from the dashboard's /apply-suggestion route (web_ui/app.py), which only
runs when a human clicks the button. A model reading its own recent
results and silently raising its own risk parameters is exactly the
failure mode this boundary exists to prevent — keeping the write path
physically separate (different module, different trigger) makes that
mistake harder to introduce later by accident, not just a rule to remember.
"""
from __future__ import annotations

import json
import logging
import time

import anthropic

from config import SETTINGS
import shadow
import storage
import reporting

log = logging.getLogger("kalshi_weather_bot.advisor")

MIN_SETTLED_TRADES_FOR_SUGGESTION = 15

PROMPT_TEMPLATE = """You are reviewing real paper-trading performance data for a set of \
strategies on Kalshi weather markets. Some of these strategies use manually-guessed \
numeric thresholds (marked below) that were always meant to be replaced with real \
data once enough existed. Your ONLY job is to suggest specific numeric changes to \
those thresholds, based on the actual trade data provided — nothing else.

Adjustable strategies and their CURRENT values:
{current_config}

Recent performance data (trades, settlements, calibration):
{export_data}

Rules:
- Only suggest changes to the strategies and params listed above. Nothing else.
- If a strategy doesn't have at least {min_trades} settled trades yet, do not \
suggest anything for it — say nothing rather than guess from too little data.
- Base suggestions on patterns actually visible in the data (e.g. "swing positions \
that hit their target took a median of only 8c of movement, not the current 20c \
target" — only if the data actually shows that).

Respond with ONLY a JSON array (no prose, no markdown fences). Each item exactly:
{{"strategy": "<name>", "param": "<param>", "current_value": <number>, \
"suggested_value": <number>, "rationale": "<one plain-English sentence>"}}

Return an empty array [] if nothing has enough data to justify a real suggestion yet."""


def generate_suggestions() -> int:
    """Returns the number of new suggestions written. Never raises past the
    caller — a failure here should never take down the main trading loop."""
    current_config = {
        name: {p: shadow.ACTIVE_STRATEGIES.get(name, {}).get(p) for p in params}
        for name, params in shadow.TUNABLE_PARAMS.items()
    }

    export_text = reporting.build_export_text()
    prompt = PROMPT_TEMPLATE.format(
        current_config=json.dumps(current_config, indent=2),
        export_data=export_text[:8000],
        min_trades=MIN_SETTLED_TRADES_FOR_SUGGESTION,
    )

    client = anthropic.Anthropic(api_key=SETTINGS.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in response.content if hasattr(b, "text"))

    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        items = json.loads(cleaned.strip())
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"Advisor response wasn't valid JSON, discarding: {e}")
        return 0

    if not isinstance(items, list):
        log.warning("Advisor response wasn't a JSON array, discarding.")
        return 0

    logged = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        strategy = item.get("strategy")
        param = item.get("param")
        # Hard whitelist check — an out-of-scope suggestion (wrong strategy,
        # wrong param, or anything touching the calibrated model/arbitrage)
        # is discarded here rather than trusted from the model's output.
        if strategy not in shadow.TUNABLE_PARAMS or param not in shadow.TUNABLE_PARAMS[strategy]:
            log.warning(f"Advisor suggested an out-of-whitelist change, discarding: {item}")
            continue
        try:
            current_value = float(item["current_value"])
            suggested_value = float(item["suggested_value"])
        except (KeyError, TypeError, ValueError):
            continue
        rationale = str(item.get("rationale", ""))[:500]
        storage.log_suggestion(strategy, param, current_value, suggested_value, rationale)
        logged += 1

    storage.set_meta("last_advisor_run_ts", str(int(time.time())))
    log.info(f"Advisor run complete — {logged} new suggestion(s) written for review.")
    return logged
