"""The complete Kalshi surface, diffed against what this bot calls.

Both audits so far were bounded by what I already knew to ask for, and
that cost a real conclusion: I reported the trade log as an expiring
rolling window because I probed /markets/trades and not
/historical/trades, which turns out to serve the entire archive.

A published spec removes recall from the loop. Every path it declares
gets classified:

  CALLED      a client method hits it
  AVAILABLE   declared, never called -- the interesting column
  AUTH-ONLY   account endpoints; listed but not a data gap

The point is not to call everything. It is that nothing should be
missing because nobody thought of it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

SPEC_URL = "https://trading-api.readme.io/openapi.json"
CLIENT = Path("/root/kalshi_weather_bot/kalshi_client.py")

client = httpx.Client(timeout=60.0, follow_redirects=True)
r = client.get(SPEC_URL)
print(f"spec: {r.status_code}  {len(r.content):,} bytes")
spec = r.json()
client.close()

paths = spec.get("paths") or {}
print(f"{len(paths)} paths declared\n")

src = CLIENT.read_text(encoding="utf-8")
# Every literal request path the client builds.
called = set(re.findall(r'_request\(\s*"[A-Z]+",\s*f?"([^"]+)"', src))
called_norm = {re.sub(r"\{[^}]+\}", "{}", c).rstrip("/") for c in called}

print("paths this client actually requests:")
for c in sorted(called_norm):
    print(f"   {c}")

groups: dict[str, list] = {}
for p, ops in sorted(paths.items()):
    methods = sorted(m.upper() for m in ops
                     if m in ("get", "post", "delete", "put", "patch"))
    norm = re.sub(r"\{[^}]+\}", "{}", p).rstrip("/")
    tail = norm.replace("/trade-api/v2", "")
    hit = any(tail == c or tail.endswith(c) or c.endswith(tail)
              for c in called_norm)
    top = tail.strip("/").split("/")[0] or "root"
    groups.setdefault(top, []).append((tail, methods, hit))

print("\n" + "=" * 78)
print("FULL SURFACE, by group   (* = this bot calls it)")
print("=" * 78)

gaps = []
for top in sorted(groups):
    rows = groups[top]
    n_hit = sum(1 for _t, _m, h in rows if h)
    print(f"\n  /{top}   ({n_hit}/{len(rows)} called)")
    for tail, methods, hit in sorted(rows):
        mark = "*" if hit else " "
        print(f"   {mark} {','.join(methods):<12} {tail}")
        if not hit and "GET" in methods and not tail.startswith(
                "/portfolio"):
            gaps.append(tail)

print("\n" + "=" * 78)
print("READ-ONLY DATA ENDPOINTS NEVER CALLED")
print("=" * 78)
for g in sorted(set(gaps)):
    print(f"   {g}")
print(f"\n  {len(set(gaps))} uncalled read endpoints outside /portfolio")
