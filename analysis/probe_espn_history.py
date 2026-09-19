"""Can the ground truth be backfilled too?

The poller was written on the assumption that ground truth is a
going-forward-only proposition. That is true of ONE thing -- our own
observation latency, when we knew a fact relative to when the market
did. It may not be true of the facts themselves.

If ESPN serves scoreboards and summaries for PAST dates, then the whole
three-legged dataset can be assembled retroactively against the Kalshi
backfill, and the first trainable data exists today rather than after
a season of collection.

Checks, in order of what would kill the idea fastest:
  1. does a dated scoreboard return finished games
  2. does a summary for an old event still carry plays and win prob
  3. do the plays carry ESPN's own wallclock, so event time survives
     even though our observation time does not
"""
import httpx

BASE = "https://site.api.espn.com/apis/site/v2/sports"
client = httpx.Client(timeout=30.0, follow_redirects=True)

# Dates chosen to line up with markets already in the Kalshi backfill.
PROBES = [
    ("nfl", "football/nfl", "20260118"),
    ("mlb", "baseball/mlb", "20260719"),
    ("epl", "soccer/eng.1", "20260524"),
]

for label, path, date in PROBES:
    print(f"\n=== {label} on {date} ===")
    r = client.get(f"{BASE}/{path}/scoreboard", params={"dates": date})
    if r.status_code != 200:
        print(f"  scoreboard HTTP {r.status_code}")
        continue
    events = r.json().get("events", [])
    print(f"  {len(events)} events")
    if not events:
        continue
    ev = events[0]
    comp = (ev.get("competitions") or [{}])[0]
    status = (comp.get("status") or {}).get("type", {})
    print(f"  sample: {ev.get('shortName')}  [{status.get('detail')}] "
          f"completed={status.get('completed')}  id={ev.get('id')}")

    s = client.get(f"{BASE}/{path}/summary", params={"event": ev["id"]})
    if s.status_code != 200:
        print(f"  summary HTTP {s.status_code}")
        continue
    data = s.json()
    plays = data.get("plays") or []
    wp = data.get("winprobability") or []
    odds = data.get("pickcenter") or []
    print(f"  plays={len(plays)}  winprob points={len(wp)}  "
          f"odds providers={len(odds)}")
    print(f"  boxscore={'yes' if data.get('boxscore') else 'no'}  "
          f"rosters={'yes' if data.get('rosters') else 'no'}")
    if plays:
        with_wall = sum(1 for p in plays if p.get("wallclock"))
        print(f"  plays carrying wallclock: {with_wall}/{len(plays)}")
        p = plays[len(plays) // 2]
        print(f"    mid play: {p.get('wallclock')} | "
              f"{(p.get('text') or '')[:60]}")
    if wp:
        print(f"    winprob first={wp[0].get('homeWinPercentage')} "
              f"last={wp[-1].get('homeWinPercentage')}")
    if odds:
        o = odds[0]
        print(f"    odds: {(o.get('provider') or {}).get('name')} "
              f"{o.get('details')} ml={(o.get('homeTeamOdds') or {}).get('moneyLine')}")

print("\n=== how far back does it go? ===")
for date in ("20250901", "20240901", "20230901", "20200901"):
    try:
        r = client.get(f"{BASE}/football/nfl/scoreboard", params={"dates": date})
        n = len(r.json().get("events", [])) if r.status_code == 200 else -1
        print(f"  NFL {date}: {r.status_code}  {n} events")
    except Exception as exc:
        print(f"  NFL {date}: {type(exc).__name__}")

client.close()
