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

# The general form, surveyed across every collected series rather than
# inferred from one example:
#
#   KXNFLGAME-25JUL31LACDET-LAC              winner, team
#   KXEPLGAME-25MAY11NEWCHE-TIE              winner, DRAW -- soccer is
#                                            three-way, not binary
#   KXNFLSPREAD-25AUG21NENYG-NE3             spread, "wins by over 3.5"
#   KXNFLTOTAL-25AUG21NENYG-37               total,  "over 37.5 points"
#   KXMLBHIT-26JUN271507TEXTOR-TORYPINANGO24-2    prop, with a KICKOFF
#                                            TIME (1507) in the fixture
#   KXMLBGAME-25APR16DETMIL2-DET             game number, doubleheader
#
# Two segments are optional and both are load-bearing. The 4-digit time
# and the trailing game number each separate the halves of a
# doubleheader, which date-only matching cannot -- and which mis-joined
# 10.9% of MLB markets before match_event_at existed.
MARKET_RE = re.compile(
    r"^(?P<series>[A-Z0-9]+)-"
    r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
    r"(?P<hhmm>\d{4})?"
    # Lazy, so the optional game-number suffix below gets first refusal
    # on the trailing characters rather than being swallowed.
    r"(?P<teams>[A-Z]+?)"
    # Doubleheaders. Real data writes COLKCG1; the G is optional because
    # a bare trailing digit appears too, and consuming it into the team
    # blob turns COL+KC into COLKCG and resolves to nothing.
    r"(?:G?(?P<game_no>\d))?"
    r"-(?P<rest>.+)$"
)

# "NE3" -> team NE, line 3. "37" -> line 37 and no team.
SIDE_RE = re.compile(r"^(?P<team>[A-Z]*)(?P<line>\d+)$")

# The ticker's number is an IDENTIFIER; the subtitle states the actual
# line. "NE3" is "wins by over 3.5 points", so hardcoding +0.5 against
# the ticker would be a guess. Read the real number off the text.
LINE_RE = re.compile(r"(?:over|above|under)\s+(\d+(?:\.\d+)?)", re.I)

DRAW = "TIE"

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
# SCOPED BY LEAGUE, because team codes are only unique within one.
# Kalshi's MUN is Manchester United and maps to ESPN's MAN; ESPN's MUN
# is Bayern Munich, which Kalshi calls BMU. A single global table cannot
# hold both, and merging them silently joins English and German football.
#
# Proposals came from analysis/derive_all_aliases.py and were filtered to
# codes that genuinely had no ESPN counterpart. That filter matters: the
# raw output also suggested things like "BOS": "CLE" at 15 occurrences,
# which is noise from the date window catching other games in a series --
# both are already-valid codes and neither needs an alias.
LEAGUE_ALIASES = {
    "nfl": {
        "LA": "LAR",      # Kalshi's Rams; ESPN keeps LAC for the Chargers
        "WAS": "WSH",
        "JAC": "JAX",
    },
    "mlb": {
        "CWS": "CHW",     # seen 533x
        "AZ": "ARI",      # seen 444x
    },
    "epl": {
        "MUN": "MAN", "LFC": "LIV", "MCI": "MNC",
        "BRI": "BHA", "CFC": "CHE",
    },
    "laliga": {
        "RVC": "RAY", "VCF": "VAL", "MAL": "MLL",
        "RCC": "CEL", "RBB": "BET",
    },
    "seriea": {
        "ACM": "MIL", "COM": "COMO", "ROM": "ROMA", "BFC": "BOL",
    },
    "bundesliga": {
        "BVB": "DOR", "FCH": "HDH", "UNI": "FCU",
        "LEV": "B04", "BMU": "MUN",
        # PAD had no unambiguous alignment and is deliberately absent
        # rather than guessed.
    },
    "ligue1": {
        "OM": "OLM", "ASM": "MON", "OL": "LYON", "FCN": "NAN",
        "FCL": "LOR", "NIC": "NICE", "STB": "BRE", "TFC": "TOU",
        "FCM": "METZ", "RCS": "STR", "LIL": "LILL",
        # STE unresolved, left out on purpose.
    },
}

# Which league a series belongs to, so the right table is consulted.
SERIES_LEAGUE = {
    "KXNFLGAME": "nfl", "KXNFLSPREAD": "nfl", "KXNFLTOTAL": "nfl",
    "KXMLBGAME": "mlb", "KXMLBHIT": "mlb", "KXMLBTB": "mlb",
    "KXMLBHRR": "mlb", "KXMLBRBI": "mlb", "KXMLBSB": "mlb",
    "KXMLBKS": "mlb", "KXMLBHR": "mlb",
    "KXEPLGAME": "epl", "KXEPLSPREAD": "epl", "KXEPLTOTAL": "epl",
    "KXEPLGOAL": "epl",
    "KXLALIGAGAME": "laliga", "KXLALIGATOTAL": "laliga",
    "KXSERIEAGAME": "seriea", "KXSERIEATOTAL": "seriea",
    "KXBUNDESLIGAGAME": "bundesliga", "KXLIGUE1GAME": "ligue1",
}

# Kept for callers that predate league scoping. NFL only, because that
# is the one league whose codes this was ever correct for.
CODE_ALIASES = LEAGUE_ALIASES["nfl"]


def league_for(series: str | None) -> str | None:
    return SERIES_LEAGUE.get((series or "").upper())


def espn_code(kalshi_code: str, league: str | None = None) -> str:
    """Kalshi's spelling of a team to ESPN's, within a league.

    With no league, every table is consulted and a code is translated
    only if exactly one league claims it. That keeps old callers working
    without letting MUN mean two teams at once -- a code claimed by more
    than one league is returned unchanged rather than guessed.
    """
    if league:
        return LEAGUE_ALIASES.get(league, {}).get(kalshi_code, kalshi_code)
    hits = {table[kalshi_code] for table in LEAGUE_ALIASES.values()
            if kalshi_code in table}
    return hits.pop() if len(hits) == 1 else kalshi_code


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


def fixture_for(ticker: str, known: set[str] | None = None,
                yes_sub_title: str | None = None) -> dict | None:
    """Everything needed to find this market's game.

    Handles all the fixture-based families, not just the winner
    markets: spreads and totals name a line, props name a player, and
    soccer winner markets have a three-way TIE. What they share is a
    date and a pair of teams, which is what the join needs.

    Returns None for markets that are not about a fixture at all
    (KXRT, KXTRUMPMENTION), and for a fixture whose team blob cannot be
    split unambiguously.
    """
    parsed = parse_market(ticker, yes_sub_title)
    if not parsed:
        return None

    blob, side, kind = parsed["teams"], parsed["side"], parsed["kind"]
    league = league_for(parsed["series"])

    # A total names no team, so the blob cannot be anchored on the
    # outcome; try each cut and accept only an unambiguous one.
    if kind == "total" or parsed["is_draw"] or not side:
        split = _split_unanchored(blob, known, league)
    else:
        split = split_teams(blob, side, known)
        if not split:
            # A prop's side is a player code prefixed by a team
            # (TORYPINANGO24 -> TOR). Anchor on the prefix instead.
            split = _split_by_prefix(blob, side, known)
    if not split:
        return None

    away, home = split
    # The draw belongs to neither team, and a total belongs to the game
    # rather than a side, so yes_is_home is meaningless for both. None
    # says so instead of defaulting to False and silently inverting
    # every probability.
    if parsed["is_draw"] or kind == "total":
        yes_is_home = None
    elif side:
        yes_is_home = espn_code(side, league) == espn_code(home, league)
    else:
        yes_is_home = None

    return {
        "date": parsed["date"],
        "away": away,
        "home": home,
        "yes_team": side,
        "league": league,
        "espn_away": espn_code(away, league),
        "espn_home": espn_code(home, league),
        "yes_is_home": yes_is_home,
        "kind": kind,
        "line": parsed["line"],
        "is_draw": parsed["is_draw"],
        "game_no": parsed["game_no"],
        "start_hhmm": parsed["start_hhmm"],
        "series": parsed["series"],
    }


def _split_unanchored(blob: str, known: set[str] | None,
                      league: str | None = None) -> tuple[str, str] | None:
    """Split with no outcome to anchor on, using the known code list.

    Only for totals and draws, where the ticker names no team. Accepts
    a cut only when it is the single one whose halves are both known
    codes -- an ambiguous blob returns None rather than a coin flip.

    Club codes run to four characters in some leagues (COMO, LYON,
    LILL), so the cut range has to reach further than the three that
    covers American sports.
    """
    if not known:
        return None
    options = []
    for cut in range(2, min(len(blob) - 2, 5) + 1):
        away, home = blob[:cut], blob[cut:]
        if (espn_code(away, league) in known
                and espn_code(home, league) in known):
            options.append((away, home))
    return options[0] if len(options) == 1 else None


def _split_by_prefix(blob: str, side: str,
                     known: set[str] | None) -> tuple[str, str] | None:
    """Split when the outcome segment merely STARTS with a team code.

    Props read TORYPINANGO24 -- team TOR, then a player. Try the
    longest team prefix that also splits the fixture blob cleanly.
    """
    for length in (3, 2):
        team = side[:length]
        hit = split_teams(blob, team, known)
        if hit:
            return hit
    return None


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


def market_kind(series: str, rest: str) -> str:
    """winner | spread | total | prop | other.

    Decided from the SERIES NAME first, because it is explicit, with the
    outcome segment only as a fallback for series whose name does not
    say. Reading the shape first would call KXNFLTOTAL-...-37 a "total"
    and KXRT-MER-27 a total as well, and they are nothing alike.
    """
    s = series.upper()
    if s.endswith("SPREAD"):
        return "spread"
    if s.endswith("TOTAL"):
        return "total"
    if s.endswith("GAME") or s.endswith("FIGHT"):
        return "winner"
    if "-" in rest:
        return "prop"
    return "other"


def parse_market(ticker: str, yes_sub_title: str | None = None) -> dict | None:
    """Everything a ticker encodes, for every fixture-based series.

    Returns None for markets that are not about a two-team fixture at
    all -- KXRT-MER-27, KXTRUMPMENTION-25APR02-GA -- rather than
    forcing them into a shape they do not have.

    `yes_sub_title` is optional but worth passing: the ticker carries
    an identifier ("NE3") and the subtitle carries the real line
    ("wins by over 3.5 points").
    """
    m = MARKET_RE.match(ticker or "")
    if not m:
        return None
    if m.group("mon") not in MONTHS:
        return None
    try:
        date = dt.date(2000 + int(m.group("yy")), MONTHS[m.group("mon")],
                       int(m.group("dd")))
    except ValueError:
        return None

    rest = m.group("rest")
    kind = market_kind(m.group("series"), rest)
    head = rest.split("-")[0]

    side, line = None, None
    if kind in ("spread", "total"):
        hit = SIDE_RE.match(head)
        if hit:
            side = hit.group("team") or None
            line = float(hit.group("line"))
    elif kind == "winner":
        side = head

    # Prefer the stated line over the ticker's identifier.
    if yes_sub_title:
        stated = LINE_RE.search(yes_sub_title)
        if stated:
            line = float(stated.group(1))

    hhmm = m.group("hhmm")
    return {
        "series": m.group("series"),
        "kind": kind,
        "date": date.isoformat(),
        # Kalshi's own start time where it publishes one. Local, not
        # UTC, so it is a tiebreaker between same-day games rather than
        # something to compare against ESPN timestamps directly.
        "start_hhmm": hhmm,
        "teams": m.group("teams"),
        "game_no": int(m.group("game_no")) if m.group("game_no") else None,
        "side": side,
        "line": line,
        "is_draw": kind == "winner" and head == DRAW,
        "rest": rest,
    }


def candidate_events(fixture: dict, events_by_date: dict,
                     window_days: int = 1) -> list[dict]:
    """Every event matching this fixture within a day either side.

    THE TWO SOURCES DISAGREE ABOUT WHAT DAY A GAME IS ON. Kalshi dates
    its tickers in US local time; ESPN's event `date` is UTC. An 8pm ET
    start is next-day UTC, which is most evening games, and ignoring
    that cost 112 NFL fixtures.
    """
    out, seen = [], set()
    base = dt.date.fromisoformat(fixture["date"])
    for delta in range(-window_days, window_days + 1):
        day = (base + dt.timedelta(days=delta)).isoformat()
        for ev in events_by_date.get(day, []):
            sides = {c.get("home_away"): c.get("abbrev")
                     for c in ev.get("competitors") or []}
            if (sides.get("away") == fixture["espn_away"]
                    and sides.get("home") == fixture["espn_home"]):
                key = ev.get("event_id") or id(ev)
                if key not in seen:
                    seen.add(key)
                    out.append(ev)
    return out


def match_event_near(fixture: dict, events_by_date: dict,
                     window_days: int = 1) -> dict | None:
    """The event for this fixture by date alone, exact day preferred.

    ONLY SAFE WHERE A FIXTURE HAPPENS AT MOST ONCE IN THREE DAYS. That
    holds in football and is flatly false in baseball, where teams play
    three- and four-game series on consecutive days and doubleheaders
    put two games on ONE day. Using this for MLB matched 10.9% of
    markets to the wrong game -- half of them a neighbouring day in the
    same series, half a doubleheader.

    Prefer match_event_at(), which disambiguates on kickoff time. This
    remains for callers that genuinely have no market close time.
    """
    for ev in candidate_events(fixture, events_by_date, window_days):
        if (ev.get("date") or "")[:10] == fixture["date"]:
            return ev
    candidates = candidate_events(fixture, events_by_date, window_days)
    return candidates[0] if candidates else None


def match_event_at(fixture: dict, events_by_date: dict,
                   close_ts: int | None,
                   window_days: int = 1,
                   max_lag_hours: float = 14.0) -> dict | None:
    """The event for this fixture, disambiguated by START TIME.

    A Kalshi market closes when its game ends, and ESPN's event `date`
    is when that game started. So the right event is the one that began
    shortly BEFORE the market closed -- which separates the two halves
    of a doubleheader and the consecutive days of a series, neither of
    which a date-only match can tell apart.

    An event starting AFTER the market closed is rejected outright: a
    game cannot settle a contract that closed before it began. Among
    the rest the latest qualifying start wins, and anything more than
    max_lag_hours earlier is refused rather than guessed at (baseball
    runs ~3h, a suspended game can run far longer, and 14h comfortably
    covers both while still excluding yesterday's game).

    Falls back to the date-only match when the market has no close
    time, so callers do not silently lose rows.
    """
    candidates = candidate_events(fixture, events_by_date, window_days)
    if not candidates:
        return None
    if close_ts is None:
        return match_event_near(fixture, events_by_date, window_days)

    best, best_lag = None, None
    for ev in candidates:
        start = _epoch(ev.get("date"))
        if start is None or start > close_ts:
            continue
        lag = close_ts - start
        if lag > max_lag_hours * 3600:
            continue
        if best_lag is None or lag < best_lag:
            best, best_lag = ev, lag
    return best


def _epoch(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
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
