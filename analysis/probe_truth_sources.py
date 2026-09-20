"""Do the free ground-truth feeds actually carry what the thesis needs?

Contract prices alone can only say the market was inconsistent with
itself, which is the thing 960 trials already found nothing in. Ground
truth is what lets you say the market was WRONG.

Two candidates, both public and keyless:

  statsapi.mlb.com   -- MLB's own feed. The question is whether it
                        exposes the OFFICIAL SCORER'S RULING per play
                        (hit vs error), because that ruling is what the
                        prop contracts settle on and it can be revised
                        after the fact.
  site.api.espn.com  -- scoreboard and play-by-play for NFL and the
                        European leagues.

A score is retrievable later. WHEN THE MARKET KNEW IT is not, which is
why this has to run live rather than be backfilled.
"""
import json
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (research; market-data collection)"}


def get(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


print("=== MLB StatsAPI ===")
try:
    sched = get("https://statsapi.mlb.com/api/v1/schedule"
                "?sportId=1&startDate=2026-07-19&endDate=2026-07-19")
    games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    print(f"games on 2026-07-19: {len(games)}")
    if games:
        pk = games[0]["gamePk"]
        print(f"sample gamePk {pk}: {games[0]['teams']['away']['team']['name']}"
              f" @ {games[0]['teams']['home']['team']['name']}")
        feed = get(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live")
        plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
        print(f"plays in feed: {len(plays)}")
        print(f"feed timestamp: {feed.get('metaData', {}).get('timeStamp')}")
        # The field that matters: does a play name the scorer's call?
        for p in plays[:4]:
            res = p.get("result", {})
            about = p.get("about", {})
            print(f"  inn {about.get('inning')} {about.get('halfInning'):<6} "
                  f"event={res.get('event'):<22} "
                  f"type={res.get('eventType'):<20} "
                  f"end={about.get('endTime')}")
        # Errors and hits are the ambiguous pair.
        events = {}
        for p in plays:
            e = p.get("result", {}).get("event") or "?"
            events[e] = events.get(e, 0) + 1
        print("  event types seen:", dict(sorted(events.items(),
                                                 key=lambda kv: -kv[1])[:10]))
except Exception as exc:
    print(f"  FAILED: {type(exc).__name__}: {exc}")

print("\n=== ESPN scoreboard ===")
for label, path in (("NFL", "football/nfl"),
                    ("EPL", "soccer/eng.1"),
                    ("La Liga", "soccer/esp.1"),
                    ("Serie A", "soccer/ita.1")):
    try:
        sb = get(f"https://site.api.espn.com/apis/site/v2/sports/{path}"
                 f"/scoreboard")
        events = sb.get("events", [])
        print(f"{label:<9} {len(events)} events listed")
        if events:
            e = events[0]
            comp = (e.get("competitions") or [{}])[0]
            status = (comp.get("status") or {}).get("type", {}).get("detail")
            teams = " vs ".join(
                c.get("team", {}).get("abbreviation", "?")
                for c in comp.get("competitors", []))
            print(f"          sample: {teams}  [{status}]  id={e.get('id')}")
    except Exception as exc:
        print(f"{label:<9} FAILED: {type(exc).__name__}: {exc}")
