"""Can historical NFL plays be recovered from ESPN's other API?

The site API returns plays=0 for finished NFL games, so the 159-point
win-probability curve has an order and no clock -- unusable for pairing
against a price at time t. MLB is fine (76/76 playIds resolve to plays
carrying wallclock); NFL is the deepest, most liquid market we collect,
so it is worth one look at the core API before writing it off.

sports.core.api.espn.com served requests earlier where the site API
refused them, and it exposes plays as their own resource.
"""
import httpx

CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

client = httpx.Client(timeout=30.0, follow_redirects=True)

# A real finished game already in the Kalshi backfill.
EVENT_ID = "401772983"      # HOU @ NE, 2026-01-18

print("=== site API (known to return no plays) ===")
r = client.get(f"{SITE}/summary", params={"event": EVENT_ID})
d = r.json() if r.status_code == 200 else {}
print(f"  status {r.status_code}  plays={len(d.get('plays') or [])} "
      f"winprob={len(d.get('winprobability') or [])}")

print("\n=== core API: the event's competitions ===")
r = client.get(f"{CORE}/events/{EVENT_ID}")
print(f"  events/{EVENT_ID} -> {r.status_code}")
if r.status_code == 200:
    ev = r.json()
    comps = ev.get("competitions") or []
    print(f"  {len(comps)} competitions; keys: {sorted(ev.keys())[:12]}")
    if comps:
        ref = comps[0].get("$ref", "")
        comp_id = ref.rstrip("/").split("/")[-1].split("?")[0] or EVENT_ID
        print(f"  competition id: {comp_id}")

        for label, path in (
                ("plays", f"{CORE}/events/{EVENT_ID}/competitions/"
                          f"{comp_id}/plays?limit=1000"),
                ("probabilities", f"{CORE}/events/{EVENT_ID}/competitions/"
                                  f"{comp_id}/probabilities?limit=1000")):
            pr = client.get(path)
            print(f"\n  {label}: HTTP {pr.status_code}")
            if pr.status_code != 200:
                continue
            body = pr.json()
            items = body.get("items") or []
            print(f"    count={body.get('count')} returned={len(items)}")
            if items:
                first = items[0]
                if "$ref" in first and len(first) == 1:
                    print("    items are $refs; fetching one...")
                    one = client.get(first["$ref"]).json()
                    print(f"    keys: {sorted(one.keys())[:16]}")
                    for k in ("wallclock", "clock", "period", "text",
                              "homeWinPercentage", "sequenceNumber"):
                        if k in one:
                            print(f"      {k}: {str(one[k])[:70]}")
                else:
                    print(f"    keys: {sorted(first.keys())[:16]}")
                    for k in ("wallclock", "clock", "period", "text",
                              "homeWinPercentage", "sequenceNumber", "id"):
                        if k in first:
                            print(f"      {k}: {str(first[k])[:70]}")

client.close()
