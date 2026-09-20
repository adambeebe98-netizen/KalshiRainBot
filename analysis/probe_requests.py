"""curl gets 200 where urllib gets 406, so the block is a header, not an IP.

urllib defaults to `User-Agent: Python-urllib/3.x` and
`Accept-Encoding: identity`; curl sends `*/*` and accepts compression.
Find the combination that works, using the library the project already
depends on rather than adding one.
"""
import requests

MLB = "https://statsapi.mlb.com/api/v1/schedule"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"

print("=== MLB via requests (default headers) ===")
for label, params in (("date=", {"sportId": 1, "date": "2026-07-19"}),
                      ("startDate/endDate", {"sportId": 1,
                                             "startDate": "2026-07-19",
                                             "endDate": "2026-07-19"})):
    try:
        r = requests.get(MLB, params=params, timeout=20)
        n = sum(len(d.get("games", [])) for d in r.json().get("dates", []))
        print(f"  {label:<20} {r.status_code}  {n} games")
    except Exception as exc:
        print(f"  {label:<20} {type(exc).__name__}: {str(exc)[:60]}")

print("\n=== what requests actually sends ===")
r = requests.get(MLB, params={"sportId": 1, "date": "2026-07-19"}, timeout=20)
for k, v in r.request.headers.items():
    print(f"  {k}: {v}")

print("\n=== MLB live feed: does it carry the scorer's ruling? ===")
sched = requests.get(MLB, params={"sportId": 1, "date": "2026-07-19"},
                     timeout=20).json()
games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
print(f"games: {len(games)}")
if games:
    pk = games[0]["gamePk"]
    feed = requests.get(
        f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live",
        timeout=30).json()
    plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    print(f"gamePk {pk}: {len(plays)} plays, "
          f"feed timestamp {feed.get('metaData', {}).get('timeStamp')}")
    counts = {}
    for p in plays:
        e = p.get("result", {}).get("event") or "?"
        counts[e] = counts.get(e, 0) + 1
    print("  events:", dict(sorted(counts.items(), key=lambda kv: -kv[1])[:12]))
    for p in plays[:3]:
        res, about = p.get("result", {}), p.get("about", {})
        print(f"    inn{about.get('inning')} {about.get('halfInning'):<6} "
              f"{res.get('event'):<18} rbi={res.get('rbi')} "
              f"score={res.get('awayScore')}-{res.get('homeScore')} "
              f"end={about.get('endTime')}")

print("\n=== ESPN via requests ===")
try:
    r = requests.get(ESPN, timeout=20)
    print(f"  {r.status_code}  {len(r.json().get('events', []))} events")
    print(f"  sent UA: {r.request.headers.get('User-Agent')}")
except Exception as exc:
    print(f"  {type(exc).__name__}: {exc}")
