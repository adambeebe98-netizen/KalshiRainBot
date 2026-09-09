"""
Periodically (see RETROSPECTIVE_INTERVAL_SECONDS in bot.py's main loop)
asks Claude to look at a real, recent sample of settled paper trades — both
wins AND losses, with full context including the actual reasoning logged
at decision time (see shadow.py's rationale field) — and write a plain,
evidence-based diagnosis of what patterns are actually showing up: what
kinds of setups tend to lose, what kinds tend to win, and any concrete
hypothesis for why, grounded in the specific trades reviewed.

CRITICAL BOUNDARY, stronger than advisor.py's: this module produces PROSE
ONLY. There is no structured {strategy, param, value} suggestion the way
advisor.py's suggestions table has — nothing here is even shaped like
something a button could apply. This is diagnostic reading material for a
human, full stop. Open-ended pattern-finding across many trades ("X seems
to correlate with losing") is a much easier place for a model to be
confidently wrong than "is this one number too high" — advisor.py's
narrow, numeric, whitelisted scope already earns it a real (if
human-gated) write path; this doesn't get one at all, on purpose.

If a pattern surfaced here looks real and actionable (e.g. "trades whose
rationale mentions only forecast data, no observation, lose far more
often"), that's a cue for a HUMAN to decide whether it's worth a real code
change — a new heuristic, a stricter confidence gate, etc. This module
itself never turns insight into action.
"""
from __future__ import annotations

import logging
import time

import anthropic

from config import SETTINGS
import storage

log = logging.getLogger("kalshi_weather_bot.retrospective")

MIN_TRADES_FOR_RETROSPECTIVE = 20

PROMPT_TEMPLATE = """You are reviewing real paper-trading results for a Kalshi weather trading \
bot — a sample of {n} recently settled trades ({wins} won, {losses} lost), across multiple \
paper-trading strategies. Each trade includes: which strategy made it, the market and side \
taken, the price paid, the real profit/loss, the model's stated probability estimate at \
decision time, and — where available — the actual reasoning logged then.

Your job: find CONCRETE, EVIDENCE-BASED patterns in what's actually showing up in this data. \
Not vague generalities like "the model could be improved" — specific, checkable observations \
like "6 of the 8 losing trades on precipitation_daily had a rationale mentioning only forecast \
data with no observation confirmation yet" or "temp_calibrated_aggressive's 3 losses all came \
from markets where the forecast was within 2 degrees of the threshold boundary."

Rules:
- Only claim a pattern if the data actually shows it — cite roughly how many trades support \
each observation. If you don't see a clear pattern, say so plainly rather than inventing one.
- Cover both what's LOSING and what's WORKING — a one-sided report is a distorted picture.
- Do NOT suggest specific numeric parameter changes (a separate system already handles that). \
Focus on qualitative diagnosis: what KINDS of trades lose, and any grounded hypothesis for why.
- Write in plain, direct language — 3 to 6 short paragraphs. This is for a human to actually \
read, not a formal report.

Trade data:
{trade_data}
"""


def _format_trade(t: dict) -> str:
    outcome = "WON" if t["status"] == "won" else ("LOST" if t["status"] == "lost" else "SOLD EARLY")
    pnl = t.get("pnl_cents") or 0
    prob = t.get("model_probability")
    prob_str = f"{prob:.2f}" if prob is not None else "n/a"
    rationale = (t.get("rationale") or "(no rationale recorded)")[:200]
    return (f"- [{t['strategy']}] {t['ticker']} {t['side']} x{t['count']} @ {t['price_cents']}c "
            f"-> {outcome} ({pnl:+d}c) | model_p={prob_str} | measure={t.get('measure')} "
            f"station={t.get('station_code')} | \"{rationale}\"")


def generate_retrospective() -> int | None:
    """Returns the new retrospective's row id, or None if there wasn't
    enough data yet / the run failed. Never raises past the caller — a
    failure here should never take down the main trading loop."""
    trades = storage.get_trades_for_retrospective()
    if len(trades) < MIN_TRADES_FOR_RETROSPECTIVE:
        log.info(f"Only {len(trades)} settled trades in the review window — "
                 f"need at least {MIN_TRADES_FOR_RETROSPECTIVE}, skipping this cycle.")
        return None

    wins = sum(1 for t in trades if t["status"] in ("won", "sold") and (t.get("pnl_cents") or 0) > 0)
    losses = len(trades) - wins

    trade_data = "\n".join(_format_trade(t) for t in trades)
    prompt = PROMPT_TEMPLATE.format(n=len(trades), wins=wins, losses=losses, trade_data=trade_data[:12000])

    try:
        client = anthropic.Anthropic(api_key=SETTINGS.anthropic_api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis_text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    except Exception as e:
        log.warning(f"Retrospective generation failed (non-fatal): {e}")
        return None

    if not analysis_text:
        log.warning("Retrospective response was empty, discarding.")
        return None

    row_id = storage.log_retrospective(analysis_text, len(trades), wins, losses)
    storage.set_meta("last_retrospective_run_ts", str(int(time.time())))
    log.info(f"Retrospective complete — reviewed {len(trades)} trades ({wins} won, {losses} lost).")
    return row_id
