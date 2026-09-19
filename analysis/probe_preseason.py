"""Why does ESPN return the date but not the game for August fixtures?

178 Kalshi markets joined to a date ESPN had, with no matching fixture
on it -- all of them in August, all preseason. The scoreboard endpoint
defaults to a season type, so the likely answer is that preseason games
are simply not in the default view rather than absent from ESPN.

Worth fixing rather than writing off: preseason is 178 of 622 markets on
one series, and dropping a whole class of game is precisely the kind of
non-random hole that biases whatever gets trained on what is left.
"""
import httpx

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
client = httpx.Client(timeout=30.0, follow_redirects=True)

# Dates from the unmatched examples.
DATES = ["20250808", "20250809", "20250807"]

# ESPN's seasontype: 1 = preseason, 2 = regular, 3 = postseason.
for date in DATES:
    print(f"\n=== {date} ===")
    for label, params in (
            ("default", {"dates": date}),
            ("seasontype=1", {"dates": date, "seasontype": 1}),
            ("seasontype=2", {"dates": date, "seasontype": 2}),
            ("seasontype=3", {"dates": date, "seasontype": 3})):
        try:
            r = client.get(f"{BASE}/scoreboard", params=params)
            events = r.json().get("events", []) if r.status_code == 200 else []
            names = [e.get("shortName") for e in events][:6]
            print(f"  {label:<14} {r.status_code} {len(events):>3} events "
                  f"{names}")
        except Exception as exc:
            print(f"  {label:<14} {type(exc).__name__}: {str(exc)[:40]}")

print("\n=== does a preseason game carry win probability? ===")
r = client.get(f"{BASE}/scoreboard",
               params={"dates": "20250808", "seasontype": 1})
events = r.json().get("events", [])
if events:
    ev = events[0]
    print(f"  {ev.get('shortName')}  id={ev.get('id')}")
    s = client.get(f"{BASE}/summary", params={"event": ev["id"]})
    d = s.json() if s.status_code == 200 else {}
    print(f"  plays={len(d.get('plays') or [])} "
          f"winprob={len(d.get('winprobability') or [])} "
          f"odds={len(d.get('pickcenter') or [])} "
          f"box={'yes' if d.get('boxscore') else 'no'}")
else:
    print("  no preseason events returned")

client.close()
