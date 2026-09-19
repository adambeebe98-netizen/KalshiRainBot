"""How far back does ESPN history go, and what survives at each age?

The previous attempt asked for NFL games on September 1st and got zero
events for every year, which says nothing about retention -- the NFL
does not play on Labor Day. Dates here are real game days.

What matters is which of the three artefacts survives, because they
degrade separately: a 2026 MLB game returned 502 plays, while a 2026
NFL game returned 0 plays but a full 198-point win-probability curve.
Win probability and odds paired with the Kalshi price are already
enough to train against; plays are a bonus where they exist.
"""
import httpx

BASE = "https://site.api.espn.com/apis/site/v2/sports"
client = httpx.Client(timeout=30.0, follow_redirects=True)

PROBES = [
    ("NFL", "football/nfl", ["20250907", "20240908", "20230910",
                             "20210912", "20180909"]),
    ("MLB", "baseball/mlb", ["20250615", "20240615", "20230615",
                             "20210615", "20180615"]),
    ("EPL", "soccer/eng.1", ["20250816", "20240817", "20230812"]),
]

print(f"{'league':<6} {'date':<10} {'events':>6} {'plays':>7} {'winprob':>8} "
      f"{'odds':>5} {'box':>4}")
print("-" * 52)

for label, path, dates in PROBES:
    for date in dates:
        try:
            r = client.get(f"{BASE}/{path}/scoreboard", params={"dates": date})
            if r.status_code != 200:
                print(f"{label:<6} {date:<10} HTTP {r.status_code}")
                continue
            events = r.json().get("events", [])
            if not events:
                print(f"{label:<6} {date:<10} {0:>6}")
                continue
            s = client.get(f"{BASE}/{path}/summary",
                           params={"event": events[0]["id"]})
            d = s.json() if s.status_code == 200 else {}
            print(f"{label:<6} {date:<10} {len(events):>6} "
                  f"{len(d.get('plays') or []):>7} "
                  f"{len(d.get('winprobability') or []):>8} "
                  f"{len(d.get('pickcenter') or []):>5} "
                  f"{'yes' if d.get('boxscore') else 'no':>4}")
        except Exception as exc:
            print(f"{label:<6} {date:<10} {type(exc).__name__}: {str(exc)[:30]}")

client.close()
