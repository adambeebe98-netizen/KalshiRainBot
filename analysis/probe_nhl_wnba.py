"""NHL and WNBA returned zero events. Bad dates, or a different path?

Both should have been playing on the dates I picked -- NHL in April,
WNBA in July -- so zero events is more likely my probe than ESPN. The
same mistake earlier asked for NFL games on Labor Day and concluded
ESPN had no history.

Try several real in-season dates, and both the plain and hyphenated
league spellings ESPN uses in different places.
"""
from __future__ import annotations

import httpx

SITE = "https://site.api.espn.com/apis/site/v2/sports"
client = httpx.Client(timeout=30.0, follow_redirects=True)

ATTEMPTS = {
    "nhl": (["hockey/nhl"],
            ["20260115", "20260220", "20251115", "20250315", "20260401"]),
    "wnba": (["basketball/wnba"],
             ["20250615", "20250710", "20250805", "20260610", "20260715"]),
}

for league, (paths, dates) in ATTEMPTS.items():
    print(f"\n=== {league} ===")
    best = None
    for path in paths:
        for date in dates:
            try:
                r = client.get(f"{SITE}/{path}/scoreboard",
                               params={"dates": date})
                n = len(r.json().get("events", [])) if r.status_code == 200 else -1
            except Exception as exc:
                print(f"  {path} {date}: {type(exc).__name__}")
                continue
            print(f"  {path:<14} {date}  HTTP {r.status_code}  {n} events")
            if n > 0 and best is None:
                best = (path, r.json()["events"][0])

    if not best:
        print("  no events on any date tried")
        continue

    path, ev = best
    comp = (ev.get("competitions") or [{}])[0]
    print(f"\n  sample {ev.get('id')}  {ev.get('shortName')}  "
          f"completed="
          f"{(comp.get('status') or {}).get('type', {}).get('completed')}")
    s = client.get(f"{SITE}/{path}/summary", params={"event": ev["id"]})
    d = s.json() if s.status_code == 200 else {}
    plays = d.get("plays") or []
    print(f"  SITE plays={len(plays)} "
          f"(timed {sum(1 for p in plays if p.get('wallclock'))})  "
          f"winprob={len(d.get('winprobability') or [])}  "
          f"odds={len(d.get('pickcenter') or [])}  "
          f"box={'y' if d.get('boxscore') else 'n'}")

client.close()
