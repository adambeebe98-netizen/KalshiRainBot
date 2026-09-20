"""Two questions left before the poller can be written.

1. MLB rejects `python-httpx/0.28.1` and accepts curl. Find a
   user-agent it serves. Prefer one that says honestly what this is
   over one that impersonates a browser -- the data is public and there
   is no reason to pretend to be something else.

2. If MLB stays blocked, does ESPN's own play-by-play carry the
   scorer's ruling? ESPN already serves all seven leagues we collect,
   so if its MLB detail is good enough, MLB's API is a nice-to-have
   rather than a dependency.
"""
import httpx

MLB_SCHED = "https://statsapi.mlb.com/api/v1/schedule"

CANDIDATES = [
    ("curl-like", "curl/8.5.0"),
    ("descriptive", "kalshi-research/1.0"),
    ("descriptive+contact", "kalshi-research/1.0 (market data collection)"),
    ("empty", ""),
    ("none", None),
]

print("=== which user-agent does MLB serve? ===")
for label, ua in CANDIDATES:
    headers = {} if ua is None else {"User-Agent": ua}
    try:
        r = httpx.get(MLB_SCHED, params={"sportId": 1, "date": "2026-07-19"},
                      headers=headers, timeout=20.0)
        n = 0
        if r.status_code == 200:
            n = sum(len(d.get("games", [])) for d in r.json().get("dates", []))
        print(f"  {label:<20} {r.status_code}  {n} games   ua={ua!r}")
    except Exception as exc:
        print(f"  {label:<20} {type(exc).__name__}: {str(exc)[:40]}")

print("\n=== ESPN MLB play-by-play depth ===")
client = httpx.Client(timeout=30.0, follow_redirects=True)
sb = client.get("https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"
                "/scoreboard").json()
events = sb.get("events", [])
print(f"  {len(events)} MLB events on the board")
if events:
    eid = events[0]["id"]
    comp = (events[0].get("competitions") or [{}])[0]
    print(f"  sample event {eid}: "
          f"{(comp.get('status') or {}).get('type', {}).get('detail')}")
    summ = client.get("https://site.api.espn.com/apis/site/v2/sports/"
                      "baseball/mlb/summary", params={"event": eid}).json()
    print(f"  summary keys: {sorted(summ.keys())}")
    plays = summ.get("plays") or []
    print(f"  plays: {len(plays)}")
    for p in plays[:5]:
        t = (p.get("type") or {}).get("text")
        print(f"    {str(t):<24} scoreValue={p.get('scoreValue')} "
              f"| {(p.get('text') or '')[:70]}")
    # Box score is where a hit/error distinction would live.
    box = summ.get("boxscore") or {}
    print(f"  boxscore keys: {sorted(box.keys())}")
client.close()
