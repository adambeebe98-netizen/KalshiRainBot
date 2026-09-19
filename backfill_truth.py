"""
Backfill the ground-truth leg, so the trainable dataset exists now.

sports_truth.py was written on the premise that ground truth is a
going-forward-only proposition. That premise is only half true, and the
half that is false is the expensive one.

What genuinely cannot be recovered is OUR OBSERVATION LATENCY -- the gap
between when a fact became true and when this system saw it. What can be
recovered, it turns out, is the facts themselves. ESPN serves dated
scoreboards and summaries back to at least 2018:

  NFL 2018-2026   full win-probability curve (177-228 points/game)
  MLB 2018-2026   full play-by-play (516-612 plays/game), with wallclock
  EPL             boxscore and final result only

Kalshi's own settled history starts around May 2025, so ESPN is not the
binding constraint -- every market in backfill_sports.py's output can be
paired with an independent probability estimate. That is the third leg,
and it is what turns "the market disagreed with itself" into "the market
was wrong", which is the distinction 960 trials died on.

DIFFERENT FROM THE LIVE POLLER ON PURPOSE. The live one stores the
LATEST win-probability point each cycle and dedups the rest, because it
is watching for change. This one stores the WHOLE curve once, because
the curve is the point: 198 paired (time, probability) readings to hold
against 198 hours of Kalshi price.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import time

import httpx

BASE = "https://site.api.espn.com/apis/site/v2/sports"
DEFAULT_DIR = "data/truth_backfill"

# Same seven leagues the collector records Kalshi markets for.
LEAGUES = {
    "nfl": "football/nfl",
    "mlb": "baseball/mlb",
    "epl": "soccer/eng.1",
    "laliga": "soccer/esp.1",
    "seriea": "soccer/ita.1",
    "bundesliga": "soccer/ger.1",
    "ligue1": "soccer/fra.1",
}

REQUEST_INTERVAL = 0.2

_client = httpx.Client(timeout=30.0, follow_redirects=True)


def _get(path: str, **params) -> dict | None:
    delay = 1.0
    for attempt in range(4):
        try:
            r = _client.get(f"{BASE}/{path}", params=params or None)
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


def done_path(league: str, directory: str) -> str:
    return os.path.join(directory, "_done", f"{league}.txt")


def load_done(league: str, directory: str) -> set[str]:
    path = done_path(league, directory)
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def mark_done(league: str, event_id: str, directory: str) -> None:
    path = done_path(league, directory)
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


def backfill_event(league: str, event: dict, day: str, directory: str) -> dict:
    """Everything ESPN still holds for one finished game."""
    event_id = str(event.get("id"))
    path = LEAGUES[league]
    data = _get(f"{path}/summary", event=event_id)
    if not data:
        return {"plays": 0, "winprob": 0, "odds": 0, "boxscore": 0}

    comp = (event.get("competitions") or [{}])[0]
    status = (comp.get("status") or {}).get("type", {})
    header = {
        "league": league,
        "event_id": event_id,
        "name": event.get("shortName"),
        "date": event.get("date"),
        "completed": bool(status.get("completed")),
        "detail": status.get("detail"),
        "competitors": [
            {"abbrev": c.get("team", {}).get("abbreviation"),
             "home_away": c.get("homeAway"),
             "score": c.get("score"),
             "winner": c.get("winner")}
            for c in comp.get("competitors", [])
        ],
        "backfilled_ts": int(time.time()),
    }

    counts = {}

    plays = data.get("plays") or []
    counts["plays"] = write(league, "plays", day, [{
        **header, "kind": "plays", "play_count": len(plays), "plays": plays,
    }] if plays else [], directory)

    # The WHOLE curve, not the last point. Paired against the Kalshi
    # candles for the same market, this is the comparison the project
    # has never had: an independent probability beside the price.
    wp = data.get("winprobability") or []
    counts["winprob"] = write(league, "winprob", day, [{
        **header, "kind": "winprob", "points": len(wp), "winprobability": wp,
    }] if wp else [], directory)

    odds = data.get("pickcenter") or []
    counts["odds"] = write(league, "odds", day, [{
        **header, "kind": "odds", "pickcenter": odds,
    }] if odds else [], directory)

    box = data.get("boxscore")
    counts["boxscore"] = write(league, "boxscore", day, [{
        **header, "kind": "boxscore", "boxscore": box,
        "rosters": data.get("rosters"),
    }] if box else [], directory)

    return counts


def backfill_day(league: str, day: str, directory: str,
                 done: set[str]) -> dict:
    """One league-day. `day` is YYYY-MM-DD; ESPN wants YYYYMMDD."""
    stats = {"events": 0, "skipped": 0, "plays": 0, "winprob": 0,
             "odds": 0, "boxscore": 0}
    board = _get(f"{LEAGUES[league]}/scoreboard", dates=day.replace("-", ""))
    if not board:
        return stats
    for event in board.get("events", []):
        event_id = str(event.get("id"))
        if event_id in done:
            stats["skipped"] += 1
            continue
        comp = (event.get("competitions") or [{}])[0]
        if not (comp.get("status") or {}).get("type", {}).get("completed"):
            # An unfinished game has nothing final to record, and it will
            # come back around on a later run.
            continue
        counts = backfill_event(league, event, day, directory)
        for k, v in counts.items():
            stats[k] += v
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
    # Kalshi's own settled history starts around here, so earlier dates
    # would collect truth with nothing to pair it against.
    p.add_argument("--start", default="2025-05-01")
    p.add_argument("--end",
                   default=dt.date.today().isoformat())
    args = p.parse_args()

    leagues = args.league or list(LEAGUES)
    print(f"backfilling truth for {len(leagues)} league(s), "
          f"{args.start} to {args.end}")

    grand = {"events": 0, "plays": 0, "winprob": 0, "odds": 0,
             "boxscore": 0, "skipped": 0}
    t0 = time.time()
    for league in leagues:
        done = load_done(league, args.dir)
        totals = {k: 0 for k in grand}
        for day in daterange(args.start, args.end):
            s = backfill_day(league, day, args.dir, done)
            for k in totals:
                totals[k] += s.get(k, 0)
        for k in grand:
            grand[k] += totals[k]
        print(f"  {league:<11} events={totals['events']:>5,} "
              f"skipped={totals['skipped']:>5,} "
              f"plays={totals['plays']:>5,} winprob={totals['winprob']:>5,} "
              f"odds={totals['odds']:>5,} box={totals['boxscore']:>5,}")

    print(f"\n{grand['events']:,} games in {(time.time()-t0)/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
