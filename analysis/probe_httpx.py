"""Confirm both feeds work through httpx, the client already installed.

curl gets 200 where urllib gets 406, so MLB is rejecting a header rather
than the droplet's address. httpx sends a curl-like Accept and accepts
compression, so it should behave like curl -- but "should" is how the
last three wrong answers started, so check it.

Then the real question: does a play carry the OFFICIAL SCORER'S RULING,
and can a revision to that ruling be detected?
"""
import httpx

MLB = "https://statsapi.mlb.com/api/v1"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"

client = httpx.Client(timeout=30.0, follow_redirects=True)

print("=== MLB via httpx ===")
r = client.get(f"{MLB}/schedule", params={"sportId": 1, "date": "2026-07-19"})
print(f"  status {r.status_code}")
print(f"  sent: UA={r.request.headers.get('user-agent')} "
      f"AE={r.request.headers.get('accept-encoding')}")
games = [g for d in r.json().get("dates", []) for g in d.get("games", [])]
print(f"  {len(games)} games")

if games:
    pk = games[0]["gamePk"]
    feed = client.get(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live").json()
    plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    meta = feed.get("metaData", {})
    print(f"\n  gamePk {pk}: {len(plays)} plays")
    print(f"  metaData.timeStamp = {meta.get('timeStamp')}")
    print(f"  feed size ~{len(str(feed))//1024} KB")

    counts = {}
    for p in plays:
        e = p.get("result", {}).get("event") or "?"
        counts[e] = counts.get(e, 0) + 1
    print("\n  event types (the scorer's vocabulary):")
    for e, n in sorted(counts.items(), key=lambda kv: -kv[1])[:14]:
        print(f"    {e:<26} {n}")

    print("\n  a play, fields that matter:")
    for p in plays[:3]:
        res, about = p.get("result", {}), p.get("about", {})
        print(f"    idx={p.get('atBatIndex')} inn{about.get('inning')} "
              f"{about.get('halfInning'):<6} event={res.get('event'):<16} "
              f"type={res.get('eventType'):<16} "
              f"rbi={res.get('rbi')} complete={about.get('isComplete')}")
        print(f"      desc: {(res.get('description') or '')[:90]}")

print("\n=== ESPN via httpx ===")
for label, path in (("NFL", "football/nfl"), ("MLB", "baseball/mlb"),
                    ("EPL", "soccer/eng.1"), ("La Liga", "soccer/esp.1"),
                    ("Serie A", "soccer/ita.1"),
                    ("Bundesliga", "soccer/ger.1"),
                    ("Ligue 1", "soccer/fra.1")):
    try:
        r = client.get(f"{ESPN}/{path}/scoreboard")
        ev = r.json().get("events", [])
        print(f"  {label:<11} {r.status_code}  {len(ev)} events")
    except Exception as exc:
        print(f"  {label:<11} {type(exc).__name__}: {str(exc)[:50]}")

client.close()
