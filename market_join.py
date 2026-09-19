"""
Join a Kalshi market to the game it is actually about.

Every downstream use of the three-legged dataset runs through here, and
the failure mode is not a crash. A bad split joins a market to the WRONG
GAME and produces a row that looks perfectly reasonable -- a price path,
a win-probability path, an outcome -- and is fiction. So this is a module
with tests rather than a helper inside an analysis script.

THE PROBLEM. Kalshi encodes the fixture in the ticker:

    KXNFLGAME-26JAN18HOUNE-NE
              ^^^^^^^          date, 2026-01-18
                     ^^^^^     away+home, concatenated, NO separator
                           ^^  the team this contract pays YES on

Team codes are 2 or 3 characters, so the blob is genuinely ambiguous:
'LACHI' is LA+CHI or LAC+HI, and both are real NFL teams.

THE FIX. The ticker already contains the answer. Its last segment names
one of the two teams, so anchor the split on it: if the blob starts with
yes_team the remainder is the home side, if it ends with yes_team the
front is the away side. Guessing is only needed when both hold.

Measured on real files: the naive both-halves-are-known-codes approach
matched 75% and its failures were all Los Angeles fixtures -- exactly the
kind of non-random hole that would quietly bias a model. Anchoring took
it to 100%.
"""
from __future__ import annotations

import datetime as dt
import re

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

TICKER_RE = re.compile(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)-([A-Z]+)$")

# Kalshi's code on the left, ESPN's abbreviation on the right.
#
# DERIVED FROM THE DATA, not guessed -- analysis/derive_aliases.py aligns
# unmatched fixtures against the ESPN games on the same date, so when one
# side already agrees the other side names the pair. Two hand-written
# guesses in the first version were both INVERTED (WSH->WAS and JAX->JAC,
# when reality is the reverse), which cost 178 NFL fixtures.
#
# Direction matters and is not recoverable by inspection: a wrong entry
# joins a market to a different game and produces a row that looks
# entirely reasonable. Re-run derive_aliases.py for a new sport rather
# than extending this by hand.
#
# All 32 NFL teams account for with these three.
CODE_ALIASES = {
    "LA": "LAR",     # Kalshi's Rams; ESPN keeps LAC for the Chargers
    "WAS": "WSH",    # seen 28x
    "JAC": "JAX",    # seen 38x
}


def espn_code(kalshi_code: str) -> str:
    """Kalshi's spelling of a team to ESPN's."""
    return CODE_ALIASES.get(kalshi_code, kalshi_code)


def parse_ticker(ticker: str) -> tuple[str, str, str] | None:
    """(iso_date, away+home blob, yes_team), or None if not a fixture.

    Returns None rather than raising: plenty of Kalshi tickers are not
    two-team fixtures at all, and callers page through mixed series.
    """
    m = TICKER_RE.match(ticker or "")
    if not m:
        return None
    yy, mon, dd, teams, yes_team = m.groups()
    if mon not in MONTHS:
        return None
    try:
        date = dt.date(2000 + int(yy), MONTHS[mon], int(dd))
    except ValueError:
        return None
    return date.isoformat(), teams, yes_team


def split_teams(blob: str, yes_team: str,
                known: set[str] | None = None) -> tuple[str, str] | None:
    """Split 'AWAYHOME' into (away, home), anchored on yes_team.

    `known` is an optional set of ESPN codes seen in the data, used only
    to break a genuine two-way tie. Returns None rather than guessing --
    a dropped row is recoverable, a silently mis-joined one is not.
    """
    if not blob or not yes_team:
        return None

    candidates = []
    if blob.startswith(yes_team):
        candidates.append((yes_team, blob[len(yes_team):]))
    if blob.endswith(yes_team):
        candidates.append((blob[:-len(yes_team)], yes_team))

    valid = [(a, h) for a, h in candidates if a and h]
    if len(valid) == 1:
        return valid[0]
    if len(valid) == 2 and known:
        for away, home in valid:
            other = home if away == yes_team else away
            if espn_code(other) in known:
                return away, home
    return None


def fixture_for(ticker: str,
                known: set[str] | None = None) -> dict | None:
    """Everything needed to find this market's game: date, away, home,
    and which side the contract pays YES on."""
    parsed = parse_ticker(ticker)
    if not parsed:
        return None
    date, blob, yes_team = parsed
    split = split_teams(blob, yes_team, known)
    if not split:
        return None
    away, home = split
    return {"date": date, "away": away, "home": home, "yes_team": yes_team,
            "espn_away": espn_code(away), "espn_home": espn_code(home),
            "yes_is_home": espn_code(yes_team) == espn_code(home)}


def match_event(fixture: dict, events: list[dict]) -> dict | None:
    """The ESPN event for this fixture, or None.

    `events` are rows carrying a `competitors` list of
    {abbrev, home_away}, as written by backfill_truth.py.

    Home and away are NOT interchangeable -- a reversed fixture is a
    different game, and the two teams usually play each other twice.
    """
    for ev in events:
        sides = {c.get("home_away"): c.get("abbrev")
                 for c in ev.get("competitors") or []}
        if (sides.get("away") == fixture["espn_away"]
                and sides.get("home") == fixture["espn_home"]):
            return ev
    return None


def match_event_near(fixture: dict, events_by_date: dict,
                     window_days: int = 1) -> dict | None:
    """Find the event, allowing the date to be off by a day.

    THE TWO SOURCES DISAGREE ABOUT WHAT DAY A GAME IS ON. Kalshi dates
    its tickers in US local time; ESPN's event `date` is UTC. A Saturday
    8pm ET kickoff is Sunday 00:00 UTC, so the two differ by one day for
    every evening game -- which is most of them in preseason, and is why
    112 August fixtures went unmatched while ESPN plainly had them.

    Widening the date window is safe here because the match still
    requires both team codes AND the home/away orientation. Two teams
    playing each other on consecutive days does not happen.

    The exact date is tried first, so a same-day fixture never loses to
    a neighbouring one.
    """
    exact = match_event(fixture, events_by_date.get(fixture["date"], []))
    if exact:
        return exact

    base = dt.date.fromisoformat(fixture["date"])
    for offset in range(1, window_days + 1):
        for delta in (offset, -offset):
            day = (base + dt.timedelta(days=delta)).isoformat()
            hit = match_event(fixture, events_by_date.get(day, []))
            if hit:
                return hit
    return None


def probability_for_yes(home_win_pct: float, fixture: dict) -> float:
    """ESPN reports HOME win probability; a contract may pay on either
    side. Flipping this silently inverts every away-team row, so it is
    one function used everywhere rather than an inline 1-x."""
    p = float(home_win_pct)
    return p if fixture["yes_is_home"] else 1.0 - p


def candle_price(candle: dict, field: str = "price",
                 point: str = "close") -> float | None:
    """One price out of a candle, as a float.

    Kalshi sends prices as DOLLAR strings ("0.6000"). Reading one as an
    int floors every price on the exchange to zero, which is exactly
    what an early probe did before reporting a 0c range everywhere.
    """
    block = candle.get(field)
    if not isinstance(block, dict):
        return None
    raw = block.get(point)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def final_price(market: dict) -> float | None:
    """The market's price AT SETTLEMENT -- minute candles first.

    The hourly series structurally cannot reach settlement: its last
    bucket ends on an hour boundary, and a market closes whenever the
    game does. For one real example the hourly tail closed at 0.02 on a
    market that settled YES, because the final bucket ended at 23:00 and
    the game finished at 23:40. The minute series covered those forty
    minutes and closed at 0.99.

    Reading the hourly tail as a settlement price therefore does not
    just add noise, it inverts the outcome on exactly the dramatic games
    that carry the most information. Always prefer the minute series.
    """
    for key in ("candles_minute", "candles_hourly"):
        candles = market.get(key) or []
        for candle in reversed(candles):
            price = candle_price(candle)
            if price is not None:
                return price
    return None
