"""
Ground truth for the sports markets, captured live because it cannot be
captured later.

Contract prices on their own can only show that a market was
inconsistent with itself, and 960 trials have already found nothing
there. Ground truth is what makes "the market was WRONG" a statement you
can test. The weather stack has had three legs for months -- Kalshi
prices, IEM observations, Open-Meteo forecasts. The sports side has had
one.

WHY THIS CANNOT BE BACKFILLED. A final score is retrievable forever.
WHEN THE MARKET KNEW IT is gone the moment it passes. Every hour this
does not run is an hour of data that no amount of money buys back later.

WHAT IT RECORDS, per poll:

  scoreboard  cheap, every cycle: status, clock, period, score
  play        new plays only, keyed by id -- and a play whose content
              CHANGES is written again as a revision rather than
              overwriting. That is the whole point (see below).
  winprob     ESPN's own win probability, an independent estimate to
              hold against the Kalshi price
  odds        sportsbook lines, a second independent estimate
  boxscore    once, at completion

REVISIONS ARE THE THESIS. A game line settles on who won and nobody
misreads that. A player prop settles on an OFFICIAL SCORER'S RULING --
hit or error is a judgment call, and it can be changed after the game.
Kalshi's own prop rules go further: a scratched player resolves the
market "to the fair market price", an outcome that is neither yes nor
no and that hinges on a lineup card posted an hour before first pitch.
That gap -- between what happened and what the record SAYS happened --
is the same gap that made a correct call about rain in Austin lose $200.
Storing a play's later revisions is how that gap becomes measurable.

WHY ESPN AND NOT MLB'S OWN API. statsapi.mlb.com returns 406 to every
Python client regardless of user-agent while serving curl normally,
which is TLS fingerprinting. Building an evasion for a block somebody
put up on purpose is not worth it, and ESPN serves all seven leagues
this project collects without objection -- plus win probability and
sportsbook odds, which MLB's feed does not carry at all.

Files, not SQLite: the droplet is a 1 vCPU box that just stopped
growing. Same gzipped-JSONL shape as export_archive.py, under data/, so
sync-kalshi-data.ps1 carries it home unchanged.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import time

import httpx

log = logging.getLogger("sports_truth")

BASE = "https://site.api.espn.com/apis/site/v2/sports"
DEFAULT_DIR = "data/truth"

# Exactly the leagues the collector records Kalshi markets for. Pairing
# only matters where both halves exist.
LEAGUES = {
    "nfl": "football/nfl",
    "mlb": "baseball/mlb",
    "epl": "soccer/eng.1",
    "laliga": "soccer/esp.1",
    "seriea": "soccer/ita.1",
    "bundesliga": "soccer/ger.1",
    "ligue1": "soccer/fra.1",
    # Added to match the collector, which was expanded to NBA, WNBA,
    # NHL and NCAAF markets while this list was left at seven. That gap
    # is the expensive kind: their PRICE history can be backfilled at
    # any time, and the ground truth alongside it -- what was known, and
    # when -- cannot. College football is in season now; basketball and
    # hockey start next month.
    #
    # Capability differs and is recorded rather than assumed:
    #   nba/wnba/ncaab  timed plays and win probability
    #   nhl             timed plays, NO win probability
    #   ncaaf           the site API returns ZERO plays, exactly like
    #                   the NFL, so its live plays are thin here too;
    #                   backfill_espn_core.py covers it after the fact
    "nba": "basketball/nba",
    "wnba": "basketball/wnba",
    "nhl": "hockey/nhl",
    "ncaaf": "football/college-football",
    "ncaab": "basketball/mens-college-basketball",
}

SCOREBOARD_SECONDS = 60      # cheap call, run every cycle
SUMMARY_SECONDS = 90         # per live game
FINISHED_GRACE_SECONDS = 7200  # keep polling after "Final" -- see below

# ESPN blocks browser-like user-agents from this endpoint and serves
# httpx's default happily, which is the opposite of the usual advice.
# Sending nothing special is both what works and what is honest.
_client = httpx.Client(timeout=30.0, follow_redirects=True)


STATE_FILE = "sports_truth_state.json"


def load_state(directory: str = DEFAULT_DIR) -> dict:
    """Digests seen so far, carried across restarts.

    WITHOUT THIS, every restart rewrites every play of every game still
    in scope, because the in-memory digest map starts empty. Measured
    over one day with several restarts: 16,420 plays had more than one
    stored version and 15,640 of those differed in NOTHING -- pure
    duplication from restarts, inflating the archive and polluting any
    later count of how often the record actually changes.

    A missing or corrupt state file is not fatal: the service starts
    with an empty map and re-records, which is the old behaviour rather
    than a new failure.
    """
    path = os.path.join(directory, STATE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {
        "plays_seen": saved.get("plays_seen", {}),
        "game_state": saved.get("game_state", {}),
        "board_seen": saved.get("board_seen", {}),
        "finished_at": {k: int(v) for k, v
                        in (saved.get("finished_at") or {}).items()},
        "last_summary": {},   # timing only; safe to forget
    }


def save_state(state: dict, directory: str = DEFAULT_DIR) -> None:
    """Write the digest map, atomically.

    A half-written state file read on the next boot would look like a
    game nobody had seen, so write beside the target and rename.
    """
    path = os.path.join(directory, STATE_FILE)
    tmp = path + ".writing"
    try:
        os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "plays_seen": state.get("plays_seen", {}),
                "game_state": state.get("game_state", {}),
                "board_seen": state.get("board_seen", {}),
                "finished_at": state.get("finished_at", {}),
            }, fh)
        os.replace(tmp, path)
    except OSError as exc:
        # Losing the state costs duplicate rows on the next restart.
        # Losing the CYCLE would cost live data, which is worse.
        log.warning("could not save state (non-fatal): %s", exc)


def prune_state(state: dict, keep_events: int = 400) -> None:
    """Forget games that aged out, so the file does not grow forever.

    Keyed by the same "league:event" the rest of the state uses, and
    trimmed oldest-first by when the game was seen finishing.
    """
    plays_seen = state.get("plays_seen", {})
    if len(plays_seen) <= keep_events:
        return
    finished = state.get("finished_at", {})

    def age(key: str) -> int:
        return finished.get(key.split(":", 1)[-1], 0)

    for key in sorted(plays_seen, key=age)[:len(plays_seen) - keep_events]:
        plays_seen.pop(key, None)
        state.get("game_state", {}).pop(key, None)


def _now() -> int:
    return int(time.time())


def _day(ts: int | None = None) -> str:
    return dt.datetime.fromtimestamp(ts or _now(),
                                     dt.timezone.utc).strftime("%Y-%m-%d")


def _path(league: str, kind: str, directory: str) -> str:
    return os.path.join(directory, f"{league}_{kind}", f"{_day()}.jsonl.gz")


def write(league: str, kind: str, records: list[dict],
          directory: str = DEFAULT_DIR) -> int:
    if not records:
        return 0
    path = _path(league, kind, directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
    return len(records)


def _get(path: str, **params) -> dict | None:
    try:
        r = _client.get(f"{BASE}/{path}", params=params or None)
        if r.status_code != 200:
            log.warning("%s -> HTTP %s", path, r.status_code)
            return None
        return r.json()
    except Exception as exc:
        log.warning("%s -> %s: %s", path, type(exc).__name__, exc)
        return None


def _digest(obj) -> str:
    """Stable fingerprint of a play's content, so a later change to it
    is detectable rather than silent."""
    return hashlib.sha1(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def scoreboard(league: str) -> list[dict]:
    data = _get(f"{LEAGUES[league]}/scoreboard")
    if not data:
        return []
    observed = _now()
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        status = comp.get("status") or {}
        stype = status.get("type") or {}
        out.append({
            "kind": "scoreboard",
            "observed_ts": observed,
            "league": league,
            "event_id": ev.get("id"),
            "name": ev.get("shortName"),
            "date": ev.get("date"),
            "state": stype.get("state"),
            "completed": bool(stype.get("completed")),
            "detail": stype.get("detail"),
            "period": status.get("period"),
            "clock": status.get("displayClock"),
            "competitors": [
                {"abbrev": c.get("team", {}).get("abbreviation"),
                 "home_away": c.get("homeAway"),
                 "score": c.get("score"),
                 "winner": c.get("winner")}
                for c in comp.get("competitors", [])
            ],
        })
    return out


def summary(league: str, event_id: str, seen: dict,
            state_digests: dict | None = None) -> dict[str, list[dict]]:
    """Plays, win probability and odds for one game.

    `seen` maps play id -> content digest, carried across polls so only
    new plays are written and a CHANGED play is written again, flagged.

    `state_digests` does the same job for the whole-game artefacts --
    boxscore, win probability, odds. Without it a finished game rewrites
    its entire boxscore every cycle for the whole grace window, which is
    ~80 identical copies per game and hundreds of MB a day of nothing.
    Skipping unchanged content is not just a size win: a boxscore whose
    digest MOVES is a stat being revised after the fact, which is the
    event this file exists to catch.
    """
    state_digests = state_digests if state_digests is not None else {}
    data = _get(f"{LEAGUES[league]}/summary", event=event_id)
    if not data:
        return {}
    observed = _now()
    batch: dict[str, list[dict]] = {"play": [], "winprob": [], "odds": [],
                                    "boxscore": []}

    for idx, p in enumerate(data.get("plays") or []):
        pid = str(p.get("id") or f"{event_id}:{idx}")
        core = {
            "seq": p.get("sequenceNumber"),
            "type": (p.get("type") or {}).get("text"),
            "text": p.get("text"),
            "score_value": p.get("scoreValue"),
            "away_score": p.get("awayScore"),
            "home_score": p.get("homeScore"),
            "period": (p.get("period") or {}).get("number"),
            "clock": (p.get("clock") or {}).get("displayValue"),
            "scoring_play": p.get("scoringPlay"),
            "wallclock": p.get("wallclock"),
            "athletes": [a.get("athlete", {}).get("id")
                         for a in (p.get("participants") or [])],
        }
        # WALLCLOCK IS EXCLUDED FROM THE DIGEST, deliberately. ESPN
        # revises it constantly -- filling in a null, nudging a
        # timestamp by a minute -- and it is not the official record
        # changing. Measured over one day of live collection: 778 plays
        # were flagged as revised, of which 612 differed ONLY in
        # wallclock and 72 touched the record. Including it makes the
        # revision flag 8-to-1 noise and buries the signal the whole
        # service exists to catch.
        #
        # The value is still STORED; it is simply not what makes a play
        # count as revised.
        digest = _digest({k: v for k, v in core.items()
                          if k != "wallclock"})
        previous = seen.get(pid)
        if previous == digest:
            continue
        batch["play"].append({
            "kind": "play",
            "observed_ts": observed,
            "league": league,
            "event_id": event_id,
            "play_id": pid,
            # A play we already stored whose content no longer matches is
            # a REVISION -- the official record changing after the fact.
            # That is the event this whole file exists to catch.
            "revision": previous is not None,
            "previous_digest": previous,
            "digest": digest,
            **core,
        })
        seen[pid] = digest

    def changed(kind: str, payload) -> tuple[bool, bool]:
        """(write_it, is_revision) for a whole-game artefact.

        is_revision means we had stored a DIFFERENT version of this
        before -- a genuine after-the-fact change, as distinct from
        seeing it for the first time.
        """
        digest = _digest(payload)
        previous = state_digests.get(kind)
        if previous == digest:
            return False, False
        state_digests[kind] = digest
        return True, previous is not None

    wp = data.get("winprobability") or []
    if wp:
        last = wp[-1]
        core = {"points": len(wp),
                "home": last.get("homeWinPercentage"),
                "tie": last.get("tiePercentage"),
                "play": last.get("playId")}
        write_it, _ = changed("winprob", core)
        if write_it:
            batch["winprob"].append({
                "kind": "winprob", "observed_ts": observed, "league": league,
                "event_id": event_id, "points": len(wp),
                "home_win_pct": last.get("homeWinPercentage"),
                "tie_pct": last.get("tiePercentage"),
                "play_id": last.get("playId"),
            })

    for book in (data.get("pickcenter") or []):
        provider = (book.get("provider") or {}).get("name")
        row = {
            "kind": "odds", "observed_ts": observed, "league": league,
            "event_id": event_id,
            "provider": provider,
            "details": book.get("details"),
            "over_under": book.get("overUnder"),
            "spread": book.get("spread"),
            "home_ml": (book.get("homeTeamOdds") or {}).get("moneyLine"),
            "away_ml": (book.get("awayTeamOdds") or {}).get("moneyLine"),
            "home_win_pct": ((book.get("homeTeamOdds") or {})
                             .get("winPercentage")),
        }
        # Keyed per provider: a line that MOVES is the signal, a line
        # repeated unchanged every 90 seconds is noise.
        write_it, _ = changed(
            f"odds:{provider}",
            {k: v for k, v in row.items() if k != "observed_ts"})
        if write_it:
            batch["odds"].append(row)

    header = data.get("header") or {}
    comps = (header.get("competitions") or [{}])[0]
    if comps.get("status", {}).get("type", {}).get("completed"):
        box = data.get("boxscore")
        write_it, is_revision = changed("boxscore", box)
        if write_it:
            batch["boxscore"].append({
                "kind": "boxscore", "observed_ts": observed,
                "league": league, "event_id": event_id,
                # A second write for the same game means a stat was
                # revised after the final whistle -- which is the whole
                # reason the grace window exists.
                "revision": is_revision,
                "boxscore": box,
                "winprobability": data.get("winprobability"),
            })
    return batch


def _is_interesting(row: dict, finished_at: dict) -> bool:
    """Poll live games, and keep polling finished ones for a while.

    The grace window is not padding. An official scorer can change a
    ruling after the final whistle, and a prop settles on the ruling --
    so the period right after "Final" is exactly when the gap between
    what happened and what the record says opens up.
    """
    eid, state = row.get("event_id"), row.get("state")
    if state == "in":
        return True
    if row.get("completed"):
        first = finished_at.setdefault(eid, _now())
        return (_now() - first) < FINISHED_GRACE_SECONDS
    # Pregame: worth one look for the opening line and the lineup.
    return state == "pre" and eid not in finished_at


def run_once(directory: str = DEFAULT_DIR, state: dict | None = None) -> dict:
    state = state if state is not None else {}
    plays_seen = state.setdefault("plays_seen", {})
    game_state = state.setdefault("game_state", {})
    finished_at = state.setdefault("finished_at", {})
    last_summary = state.setdefault("last_summary", {})
    counts = {"scoreboard": 0, "play": 0, "revision": 0, "winprob": 0,
              "odds": 0, "boxscore": 0, "games": 0}

    board_seen = state.setdefault("board_seen", {})

    for league in LEAGUES:
        rows = scoreboard(league)
        # A finished game's scoreboard row is identical every minute
        # until it ages out of the grace window. A live game's clock
        # moves, so it still writes every cycle -- which is the point.
        fresh = []
        for row in rows:
            key = f"{league}:{row.get('event_id')}"
            digest = _digest({k: v for k, v in row.items()
                              if k != "observed_ts"})
            if board_seen.get(key) != digest:
                board_seen[key] = digest
                fresh.append(row)
        counts["scoreboard"] += write(league, "scoreboard", fresh, directory)
        for row in rows:
            if not _is_interesting(row, finished_at):
                continue
            eid = row["event_id"]
            key = f"{league}:{eid}"
            if _now() - last_summary.get(key, 0) < SUMMARY_SECONDS:
                continue
            last_summary[key] = _now()
            counts["games"] += 1
            batch = summary(league, eid,
                            plays_seen.setdefault(key, {}),
                            game_state.setdefault(key, {}))
            for kind, records in batch.items():
                counts[kind] = counts.get(kind, 0) + write(
                    league, kind, records, directory)
                counts["revision"] += sum(1 for r in records
                                          if r.get("revision"))
    return counts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=SCOREBOARD_SECONDS)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # One INFO line per request is ~60 lines a cycle, every minute,
    # forever. The cycle summary says everything the log needs to.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    state = load_state(args.dir)
    if state.get("plays_seen"):
        log.info("resumed: %d games already recorded",
                 len(state["plays_seen"]))

    while True:
        t0 = time.time()
        try:
            c = run_once(args.dir, state)
            log.info("scoreboard=%d games=%d plays=%d revisions=%d "
                     "winprob=%d odds=%d box=%d (%.1fs)",
                     c["scoreboard"], c["games"], c["play"], c["revision"],
                     c["winprob"], c["odds"], c["boxscore"], time.time() - t0)
            prune_state(state)
            save_state(state, args.dir)
        except Exception:
            log.exception("cycle failed")
        if args.once:
            save_state(state, args.dir)
            return 0
        time.sleep(max(5, args.interval - (time.time() - t0)))


if __name__ == "__main__":
    raise SystemExit(main())
