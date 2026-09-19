"""
Plays and probabilities from ESPN's core API, on a real clock.

backfill_truth.py uses the site API, which for finished NFL games
returns a 198-point win-probability curve and ZERO plays. A curve with
no plays has an order but no clock, and pairing it with price candles
would mean assuming plays are evenly spaced -- a two-minute drill and a
first-quarter possession are the same play count and wildly different
durations. That assumption misaligns every row and does it silently.

The core API does not have that gap:

  /plays          198 items with clock, period, scores, and WALLCLOCK --
                  the real timestamp the site API omits. Verified on a
                  finished game: Q1 15:00 at 20:03:01Z through Q4 0:34
                  at 23:18:13Z, spanning the game rather than a bulk
                  archive write. (`modified` IS such a write -- every
                  play on that game reads 00:54Z. Do not use it.)

  /probabilities  198 items, each referencing its play, so the curve
                  inherits that clock. And it carries more than the
                  moneyline:

                    homeWinPercentage    -> KXNFLGAME
                    spreadCoverProbHome  -> KXNFLSPREAD
                    totalOverProb        -> KXNFLTOTAL

                  All three are series this project already collects, so
                  the spread and total markets get an independent
                  estimate too, not just the winner market.

Written as its own job rather than folded into backfill_truth.py: that
one is mid-flight, and its per-event done-markers would skip every game
it has already finished. Separate directory, separate markers.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import time

import httpx

CORE = "https://sports.core.api.espn.com/v2/sports"
SITE = "https://site.api.espn.com/apis/site/v2/sports"
DEFAULT_DIR = "data/espn_core"

# Leagues whose site-API summaries come back without plays. MLB is
# deliberately absent: its site summary already carries 548 plays with
# wallclock, and 76/76 win-probability points resolve against them, so
# re-fetching it here would be two extra requests per game for data we
# already hold.
# THE TWO APIS USE DIFFERENT PATH SHAPES, which is easy to miss because
# both start with .../sports/football/. The site API wants
# `football/nfl`; the core API inserts `leagues`, as `football/leagues/
# nfl`. Reusing one for the other returns an empty items list rather
# than a 404, so it looks like a game with no plays instead of a bad URL
# -- which is exactly how the first run reported zero events on dates
# that plainly had games.
LEAGUES = {
    "nfl": "football/nfl",
}
CORE_LEAGUES = {
    "nfl": "football/leagues/nfl",
}

REQUEST_INTERVAL = 0.2
PAGE_LIMIT = 1000

_client = httpx.Client(timeout=30.0, follow_redirects=True)


def _get(url: str, **params) -> dict | None:
    delay = 1.0
    for attempt in range(4):
        try:
            r = _client.get(url, params=params or None)
            time.sleep(REQUEST_INTERVAL)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception:
            pass
        time.sleep(delay)
        delay *= 2
    return None


def load_done(league: str, directory: str) -> set[str]:
    path = os.path.join(directory, "_done", f"{league}.txt")
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def mark_done(league: str, event_id: str, directory: str) -> None:
    path = os.path.join(directory, "_done", f"{league}.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(event_id + "\n")


def write(league: str, kind: str, day: str, records: list[dict],
          directory: str) -> int:
    if not records:
        return 0
    path = os.path.join(directory, f"{league}_{kind}", f"{day}.jsonl.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
    return len(records)


def _ref_id(block) -> str | None:
    """The trailing id out of a $ref URL."""
    if not isinstance(block, dict):
        return None
    ref = block.get("$ref") or ""
    tail = ref.split("?")[0].rstrip("/").split("/")[-1]
    return tail or None


def fetch_game(league: str, event_id: str) -> dict:
    """Plays and probabilities for one finished game, trimmed.

    The raw objects are mostly $ref links to teams and competitions that
    are identical across every play; keeping them would multiply the
    archive for no information.
    """
    base = (f"{CORE}/{CORE_LEAGUES[league]}/events/{event_id}"
            f"/competitions/{event_id}")

    raw_plays = (_get(f"{base}/plays", limit=PAGE_LIMIT) or {}).get("items", [])
    plays = []
    for p in raw_plays:
        clock = p.get("clock") or {}
        plays.append({
            "id": str(p.get("id")),
            "seq": p.get("sequenceNumber"),
            # The real timestamp. Absent on the coin toss and a few
            # administrative rows, which is why callers interpolate
            # rather than require it on every play.
            "wallclock": p.get("wallclock"),
            "period": (p.get("period") or {}).get("number"),
            "clock_seconds": clock.get("value"),
            "clock": clock.get("displayValue"),
            "type": (p.get("type") or {}).get("text"),
            "text": p.get("text"),
            "away_score": p.get("awayScore"),
            "home_score": p.get("homeScore"),
            "scoring_play": p.get("scoringPlay"),
            "is_turnover": p.get("isTurnover"),
            "is_penalty": p.get("isPenalty"),
            "score_value": p.get("scoreValue"),
        })

    raw_probs = (_get(f"{base}/probabilities",
                      limit=PAGE_LIMIT) or {}).get("items", [])
    probs = []
    for p in raw_probs:
        probs.append({
            "seq": p.get("sequenceNumber"),
            # Links this point to a play, and therefore to a wallclock.
            "play_id": _ref_id(p.get("play")),
            "home_win_pct": p.get("homeWinPercentage"),
            "away_win_pct": p.get("awayWinPercentage"),
            "tie_pct": p.get("tiePercentage"),
            # The spread and total markets are collected too, and these
            # are their independent estimates.
            "spread_cover_home": p.get("spreadCoverProbHome"),
            "spread_push": p.get("spreadPushProb"),
            "total_over": p.get("totalOverProb"),
            "total_push": p.get("totalPushProb"),
            "seconds_left": p.get("secondsLeft"),
            "source": (p.get("source") or {}).get("id")
                      if isinstance(p.get("source"), dict) else p.get("source"),
        })

    return {"plays": plays, "probabilities": probs}


def backfill_day(league: str, day: str, directory: str,
                 done: set[str]) -> dict:
    stats = {"events": 0, "skipped": 0, "plays": 0, "probabilities": 0,
             "no_clock": 0}
    board = _get(f"{SITE}/{LEAGUES[league]}/scoreboard",
                 dates=day.replace("-", ""))
    if not board:
        return stats

    for event in board.get("events", []):
        event_id = str(event.get("id"))
        if event_id in done:
            stats["skipped"] += 1
            continue
        comp = (event.get("competitions") or [{}])[0]
        if not (comp.get("status") or {}).get("type", {}).get("completed"):
            continue

        got = fetch_game(league, event_id)
        if not got["plays"] and not got["probabilities"]:
            done.add(event_id)
            mark_done(league, event_id, directory)
            continue

        header = {
            "league": league,
            "event_id": event_id,
            "name": event.get("shortName"),
            "date": event.get("date"),
            "competitors": [
                {"abbrev": c.get("team", {}).get("abbreviation"),
                 "home_away": c.get("homeAway"),
                 "score": c.get("score"),
                 "winner": c.get("winner")}
                for c in comp.get("competitors", [])
            ],
            "backfilled_ts": int(time.time()),
        }

        timed = sum(1 for p in got["plays"] if p.get("wallclock"))
        if not timed:
            # A game whose plays carry no wallclock cannot be aligned,
            # and counting them is how we find out whether that is rare
            # or systematic.
            stats["no_clock"] += 1

        stats["plays"] += write(league, "plays", day, [{
            **header, "kind": "plays", "count": len(got["plays"]),
            "timed": timed, "plays": got["plays"]}], directory)
        stats["probabilities"] += write(league, "probabilities", day, [{
            **header, "kind": "probabilities",
            "count": len(got["probabilities"]),
            "probabilities": got["probabilities"]}], directory)
        stats["events"] += 1
        done.add(event_id)
        mark_done(league, event_id, directory)
    return stats


def daterange(start: str, end: str):
    d = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    while d <= last:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--league", action="append", choices=sorted(LEAGUES))
    p.add_argument("--start", default="2025-05-01")
    p.add_argument("--end", default=dt.date.today().isoformat())
    args = p.parse_args()

    leagues = args.league or list(LEAGUES)
    print(f"core-API backfill: {leagues}, {args.start} to {args.end}")

    t0 = time.time()
    for league in leagues:
        done = load_done(league, args.dir)
        totals = {"events": 0, "skipped": 0, "plays": 0,
                  "probabilities": 0, "no_clock": 0}
        for day in daterange(args.start, args.end):
            s = backfill_day(league, day, args.dir, done)
            for k in totals:
                totals[k] += s.get(k, 0)
        print(f"  {league:<8} events={totals['events']:>5,} "
              f"skipped={totals['skipped']:>5,} "
              f"plays={totals['plays']:>5,} "
              f"probs={totals['probabilities']:>5,} "
              f"games with no wallclock={totals['no_clock']:>4,}")

    print(f"\ndone in {(time.time()-t0)/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
