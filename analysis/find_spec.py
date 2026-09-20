"""Find Kalshi's actual API specification.

The first audit listed endpoints from memory, which is exactly the
weakness being discussed: it can only find gaps someone already
suspects. A machine-readable spec turns "what did we miss" from a
recall problem into a diff.

Tries the usual locations for an OpenAPI document. If none serves one,
falls back to probing candidate paths directly against the live API --
a 200 or a 401 means the endpoint exists, a 404 means it does not.
"""
from __future__ import annotations

import json

import httpx

client = httpx.Client(timeout=30.0, follow_redirects=True)

CANDIDATES = [
    "https://api.elections.kalshi.com/trade-api/v2/openapi.json",
    "https://api.elections.kalshi.com/trade-api/openapi.json",
    "https://api.elections.kalshi.com/openapi.json",
    "https://docs.kalshi.com/openapi.json",
    "https://trading-api.readme.io/openapi.json",
    "https://api.elections.kalshi.com/trade-api/v2/swagger.json",
]

print("=== looking for a machine-readable spec ===")
spec = None
for url in CANDIDATES:
    try:
        r = client.get(url)
    except Exception as exc:
        print(f"  {r'%-62s' % url} {type(exc).__name__}")
        continue
    ok = r.status_code == 200 and r.headers.get(
        "content-type", "").startswith("application/json")
    print(f"  {url:<62} {r.status_code} {len(r.content):>8,}b")
    if ok and spec is None:
        try:
            spec = r.json()
        except Exception:
            spec = None

if spec and "paths" in spec:
    paths = sorted(spec["paths"])
    print(f"\n  SPEC FOUND: {len(paths)} paths")
    for p in paths:
        methods = ",".join(sorted(m.upper()
                                  for m in spec["paths"][p]
                                  if m in ("get", "post", "delete", "put")))
        print(f"    {methods:<12} {p}")
else:
    print("\n  no spec served; probing candidate paths directly")
    print("  (200 or 401 => the endpoint exists; 404 => it does not)\n")
    BASE = "https://api.elections.kalshi.com/trade-api/v2"
    PROBES = [
        "/markets", "/markets/trades", "/events", "/series",
        "/exchange/status", "/exchange/schedule", "/exchange/announcements",
        "/milestones", "/structured_targets",
        "/historical/markets", "/historical/trades",
        "/portfolio/balance", "/portfolio/positions", "/portfolio/orders",
        "/portfolio/fills", "/portfolio/settlements",
        "/communications/quotes", "/multivariate_event_collections",
        "/search", "/lookup/tickers",
    ]
    for path in PROBES:
        try:
            r = client.get(BASE + path, params={"limit": 1})
            exists = r.status_code in (200, 400, 401, 403)
            mark = "EXISTS" if exists else ("404" if r.status_code == 404
                                            else str(r.status_code))
            note = ""
            if r.status_code == 200:
                try:
                    keys = list(r.json())[:5]
                    note = f"  keys={keys}"
                except Exception:
                    pass
            elif r.status_code in (401, 403):
                note = "  (needs auth)"
            print(f"    {path:<40} {mark:<8}{note}")
        except Exception as exc:
            print(f"    {path:<40} {type(exc).__name__}")

client.close()
