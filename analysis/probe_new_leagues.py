"""Does ESPN serve ground truth for the leagues worth adding?

The Kalshi side says NBA, NCAAF, NHL and WNBA carry far more volume per
market than anything currently collected -- NBA at 4.0M against a
22,000-market parlay series at 867. Adding them is only worth it if the
third leg exists, because price-only data is the dead end 960 trials
already measured.

For each league, three things decide how useful it will be:

  plays with wallclock   -> a score/clock series, which settles spread
                            and total markets exactly (verified on NFL:
                            8,839 markets, 0 disagreements)
  win probability        -> an independent estimate for winner markets
  odds                   -> a second independent estimate

Tennis and esports are checked too, with low expectations: Kalshi trades
them heavily, and if ESPN has nothing they are price-and-outcome only.
"""
from __future__ import annotations

import httpx

SITE = "https://site.api.espn.com/apis/site/v2/sports"
CORE = "https://sports.core.api.espn.com/v2/sports"

client = httpx.Client(timeout=30.0, follow_redirects=True)

LEAGUES = {
    "nba": ("basketball/nba", "basketball/leagues/nba", "20260410"),
    "wnba": ("basketball/wnba", "basketball/leagues/wnba", "20250720"),
    "nhl": ("hockey/nhl", "hockey/leagues/nhl", "20260410"),
    "ncaaf": ("football/college-football", "football/leagues/college-football",
              "20251108"),
    "ncaab": ("basketball/mens-college-basketball",
              "basketball/leagues/mens-college-basketball", "20260214"),
    "tennis-atp": ("tennis/atp", None, "20260612"),
    "tennis-wta": ("tennis/wta", None, "20260612"),
}

for name, (site_path, core_path, date) in LEAGUES.items():
    print(f"\n=== {name}  ({date}) ===")
    try:
        r = client.get(f"{SITE}/{site_path}/scoreboard", params={"dates": date})
    except Exception as exc:
        print(f"  scoreboard failed: {type(exc).__name__}")
        continue
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
    teams = " vs ".join(c.get("team", {}).get("abbreviation", "?")
                        for c in comp.get("competitors", []))
    print(f"  sample {ev.get('id')}  {teams or ev.get('shortName')}  "
          f"completed={status.get('completed')}")

    s = client.get(f"{SITE}/{site_path}/summary", params={"event": ev["id"]})
    d = s.json() if s.status_code == 200 else {}
    plays = d.get("plays") or []
    timed = sum(1 for p in plays if p.get("wallclock"))
    print(f"  SITE  plays={len(plays)} (timed {timed})  "
          f"winprob={len(d.get('winprobability') or [])}  "
          f"odds={len(d.get('pickcenter') or [])}  "
          f"box={'y' if d.get('boxscore') else 'n'}")

    if not core_path:
        continue
    eid = ev["id"]
    base = f"{CORE}/{core_path}/events/{eid}/competitions/{eid}"
    for kind in ("plays", "probabilities"):
        try:
            cr = client.get(f"{base}/{kind}", params={"limit": 1000})
            if cr.status_code != 200:
                print(f"  CORE  {kind}: HTTP {cr.status_code}")
                continue
            items = cr.json().get("items") or []
            extra = ""
            if items and kind == "plays":
                extra = (f", timed "
                         f"{sum(1 for i in items if i.get('wallclock'))}")
            if items and kind == "probabilities":
                keys = [k for k in ("homeWinPercentage", "spreadCoverProbHome",
                                    "totalOverProb") if k in items[0]]
                extra = f", fields {keys}"
            print(f"  CORE  {kind}: {len(items)}{extra}")
        except Exception as exc:
            print(f"  CORE  {kind}: {type(exc).__name__}")

client.close()
