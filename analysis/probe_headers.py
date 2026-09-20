"""Work out what these feeds want before building a poller on them.

MLB returned 406 from the droplet and worked from a home machine, which
points at a missing Accept header rather than a blocked address. ESPN
returned 403 from both, so that one is not about the droplet's IP
either. Find out which headers each actually requires.
"""
import json
import urllib.error
import urllib.request

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

HEADER_SETS = {
    "bare": {},
    "accept-json": {"Accept": "application/json"},
    "browser-ua": {"User-Agent": BROWSER_UA},
    "ua+accept": {"User-Agent": BROWSER_UA, "Accept": "application/json"},
    "full": {"User-Agent": BROWSER_UA,
             "Accept": "application/json, text/plain, */*",
             "Accept-Language": "en-US,en;q=0.9",
             "Referer": "https://www.espn.com/"},
}

TARGETS = {
    "MLB schedule": ("https://statsapi.mlb.com/api/v1/schedule"
                     "?sportId=1&startDate=2026-07-19&endDate=2026-07-19"),
    "ESPN NFL": ("https://site.api.espn.com/apis/site/v2/sports/"
                 "football/nfl/scoreboard"),
    "ESPN core NFL": ("https://sports.core.api.espn.com/v2/sports/football/"
                      "leagues/nfl/events?limit=5"),
}

for name, url in TARGETS.items():
    print(f"\n=== {name} ===")
    for label, headers in HEADER_SETS.items():
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read(400_000).decode("utf-8", "replace")
            try:
                data = json.loads(body)
                if "dates" in data:
                    n = sum(len(d.get("games", [])) for d in data["dates"])
                    detail = f"{n} games"
                elif "events" in data:
                    detail = f"{len(data['events'])} events"
                elif "items" in data:
                    detail = f"{len(data['items'])} items"
                else:
                    detail = f"keys={list(data)[:5]}"
            except json.JSONDecodeError:
                detail = f"non-JSON, {len(body)} bytes"
            print(f"  {label:<12} OK   {detail}")
        except urllib.error.HTTPError as e:
            print(f"  {label:<12} HTTP {e.code}")
        except Exception as e:
            print(f"  {label:<12} {type(e).__name__}: {str(e)[:50]}")
