"""
Adam's insight, confirmed correct: within one Kalshi series, every
market's rules text is the same template with only the date and the
threshold number substituted -- "...for July 16, 2026, is greater than
91..." vs "...for July 15, 2026, is greater than 89...". The station,
measure, and settlement source never change within a series. Calling
rules_extractor (a real Anthropic API call) separately for every single
market was correct on the FIRST market of a series but wasteful for
every one after it -- confirmed live tonight: Austin alone produced
3,700+ distinct tickers, meaning that many separate LLM calls where at
most a small handful of genuinely distinct templates existed.

This learns a template from the FIRST market's real (LLM) extraction in
a series, then reuses it for subsequent markets via deterministic regex
+ arithmetic instead of a fresh LLM call each time. Deliberately does
NOT try to reimplement rules_extractor's own threshold-adjustment logic
(e.g. how a strict "less than 99" becomes threshold_high_f=98.9999) --
that offset is learned empirically from the first real extraction and
reapplied, never guessed at. A successful regex fullmatch against the
market's own real rules text is what makes reuse trustworthy: it proves
the text conforms to the template at every character, not just near the
number -- there is nothing left to separately "double check" once that
succeeds. The one thing that must be tracked explicitly (and was
initially missed) is WHICH threshold field each captured number belongs
to (low, high, or both for a bracket) -- this is stored on the template
itself rather than re-inferred later, since re-inferring it after the
fact from cleared fields is not recoverable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from rules_extractor import MarketRules

_DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
                 "%m/%d/%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y"]

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class SeriesTemplate:
    regex: re.Pattern                 # matches full rules text; numbered groups are the threshold(s)
    offsets: list[float]              # per capture group, in order: threshold_value - raw_captured_number
    slots: list[str]                  # per capture group, in order: "low" or "high"
    base_rules: MarketRules           # the first market's extraction, with both threshold fields cleared


def _find_date_match(rules_text: str, occurrence_datetime_iso: str | None) -> str | None:
    """Tries each known date format against the market's own real EVENT
    date and returns whichever exact substring actually appears in the
    text. Deliberately uses occurrence_datetime, not open_time or
    close_time -- confirmed live tonight that a market's open_time is
    the day BEFORE the actual weather event and close_time is the day
    AFTER (time is left for the official report to publish), so neither
    matches the date the rules text actually states. occurrence_datetime
    is the field that does -- USUALLY: CONFIRMED LIVE for KXHIGHTNOLA
    that Kalshi's own occurrence_datetime can be off by a full day from
    the date the market's own ticker and rules text actually state (a
    ticker for Jul 17 reporting occurrence_datetime of Jul 18). Since
    the exact date never appeared in the text, every single template
    built came out with that day's specific date baked in as fixed,
    unmasked text -- unable to ever match a different day's market
    again, forcing nearly every market in the series (552 of 615, 90%,
    confirmed directly) to fall back to its own fresh LLM call. The
    ticker's own embedded date has been reliable in every case seen
    tonight, but rather than trust either source blindly, this tries a
    small window of nearby dates when the exact one doesn't literally
    appear in the text -- each candidate is still required to be an
    exact, literal substring match, so this can never mask the wrong
    date: a market's rules text only ever states one specific date, and
    at most one candidate in the window will ever actually be present.
    Returns None if no format matches -- callers must not build a
    template in that case, since the date can't be safely generalized
    to other markets."""
    if not occurrence_datetime_iso:
        return None
    try:
        base_date = datetime.fromisoformat(occurrence_datetime_iso.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None
    for offset in (0, -1, 1, -2, 2):
        d = base_date + timedelta(days=offset)
        for fmt in _DATE_FORMATS:
            variant = d.strftime(fmt)
            # strftime can zero-pad the day (e.g. "July 06, 2026"); also try
            # the more natural non-padded form ("July 6, 2026").
            no_pad = variant.replace(f" 0{d.day}", f" {d.day}") if d.day < 10 else variant
            for candidate in {variant, no_pad}:
                if candidate in rules_text:
                    return candidate
    return None


def build_template(rules_text: str, rules: MarketRules, occurrence_datetime_iso: str | None) -> SeriesTemplate | None:
    """
    Derives a reusable template from one market's real LLM extraction.
    Returns None whenever the text can't be safely generalized -- no
    date match found, or a known threshold value can't be located in
    the text -- so callers keep using full LLM extraction for this
    series rather than ever build an untrustworthy template.
    """
    date_match = _find_date_match(rules_text, occurrence_datetime_iso)
    masked = rules_text.replace(date_match, "\x00DATE\x00", 1) if date_match else rules_text

    slots: list[str] = []
    if rules.threshold_low_f is not None:
        slots.append("low")
    if rules.threshold_high_f is not None:
        slots.append("high")
    if not slots:
        return None

    offsets = []
    for slot in slots:
        target = rules.threshold_low_f if slot == "low" else rules.threshold_high_f
        found = None
        for m in _NUMBER_RE.finditer(masked):
            if abs(float(m.group()) - target) < 1.0:
                found = m
                break
        if found is None:
            return None
        offsets.append(target - float(found.group()))
        masked = masked[:found.start()] + "\x00NUM\x00" + masked[found.end():]

    pattern = re.escape(masked)
    pattern = pattern.replace(re.escape("\x00DATE\x00"), r".+?")
    pattern = pattern.replace(re.escape("\x00NUM\x00"), r"(\d+(?:\.\d+)?)")

    try:
        compiled = re.compile(pattern, re.DOTALL)
    except re.error:
        return None

    if not compiled.fullmatch(rules_text):
        return None  # must match the very text it was built from

    base_rules = replace(rules, threshold_low_f=None, threshold_high_f=None)
    return SeriesTemplate(regex=compiled, offsets=offsets, slots=slots, base_rules=base_rules)


def try_apply_template(template: SeriesTemplate, rules_text: str, ticker: str) -> MarketRules | None:
    """
    Extracts this market's rules using an already-learned series
    template, with no LLM call. A successful fullmatch here proves the
    text conforms to the template at every character (not just near the
    number), which is what makes trusting the captured values safe.
    Returns None -- never a guess -- if the template doesn't cleanly
    apply, so the caller falls back to a full LLM extraction.
    """
    match = template.regex.fullmatch(rules_text)
    if not match or len(match.groups()) != len(template.offsets):
        return None

    values = {}
    for i, slot in enumerate(template.slots):
        values[slot] = float(match.group(i + 1)) + template.offsets[i]

    return replace(
        template.base_rules,
        ticker=ticker,
        threshold_low_f=values.get("low"),
        threshold_high_f=values.get("high"),
    )
