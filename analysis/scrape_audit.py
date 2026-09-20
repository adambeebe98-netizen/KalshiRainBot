"""
A hard audit of what should have been scraped and was not.

Four distinct kinds of gap, because they need different fixes and have
very different costs:

  RESOLUTION  the right thing captured too coarsely. Already found once
              today: the whole weather archive is HOURLY and its last
              candle sits a median of 60 minutes before close, so the
              final hour -- where spreads collapse from 10c to 1c -- is
              simply absent. Kalshi serves 1-minute candles for it.

  FIELDS      the right call made, the answer partly thrown away. This
              has bitten twice: historical_price_points' own schema
              comment records that bid/ask/open_interest were fetched
              and discarded, and the collector claimed in a docstring to
              store settlement rules while dropping them.

  COVERAGE    endpoints never called at all. The expensive ones, because
              nothing in the data hints at what is missing.

  RETENTION   captured, then aged out. Recoverable only if the source
              still serves it.

What this CANNOT tell us is how much any gap is worth. It reports what
exists and what we hold, and leaves the judgement separate.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path("/root/kalshi_weather_bot")


def used_count(name: str) -> int:
    """How many times a client method is called outside its definition."""
    try:
        out = subprocess.run(
            ["grep", "-rho", "--include=*.py", rf"\.{name}(", str(ROOT)],
            capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return -1
    return len([ln for ln in out.splitlines() if ln.strip()])


src = (ROOT / "kalshi_client.py").read_text(encoding="utf-8")
methods = re.findall(r"^    def ([a-z_][a-z0-9_]*)\(", src, re.M)

print("=" * 72)
print("KALSHI CLIENT: methods defined vs actually called")
print("=" * 72)
unused = []
for m in sorted(methods):
    if m.startswith("_"):
        continue
    n = used_count(m)
    flag = ""
    if n == 0:
        flag = "   <-- NEVER CALLED"
        unused.append(m)
    print(f"  {m:<36} {n:>4} call sites{flag}")

print(f"\n  never called: {unused}")

# Endpoints the REST API exposes that this client has no method for at
# all. Taken from Kalshi's published API surface; the point is the ones
# with no wrapper, because nothing in the codebase would reveal them.
print("\n" + "=" * 72)
print("KALSHI ENDPOINTS WITH NO CLIENT METHOD AT ALL")
print("=" * 72)
KNOWN = {
    "/markets/trades": "every executed TRADE with price, size, side and "
                       "timestamp -- the actual transaction record, "
                       "strictly richer than any candle",
    "/markets/{t}/orderbook": "FULL DEPTH, not just top of book. There IS "
                              "a wrapper (get_orderbook) but nothing "
                              "stores the result historically",
    "/events": "event-level grouping and metadata; bracket legs that "
               "belong to one event are only inferable from tickers now",
    "/series": "series metadata including settlement sources and "
               "frequency",
    "/portfolio/settlements": "own settlement records",
    "/milestones": "scheduled events some series settle against",
}
for path, why in KNOWN.items():
    stem = path.strip("/").split("/")[-1].replace("{t}", "")
    has = any(stem in m for m in methods)
    mark = "wrapper exists" if has else "NO WRAPPER"
    print(f"  {path:<26} {mark}")
    print(f"      {why}")

print("\n" + "=" * 72)
print("STORAGE: what the schema keeps per source")
print("=" * 72)
storage = (ROOT / "storage.py").read_text(encoding="utf-8")
for table in ("historical_price_points", "market_snapshots",
              "realtime_ticks"):
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
                  storage, re.S)
    if not m:
        continue
    cols = [ln.strip().split()[0] for ln in m.group(1).splitlines()
            if ln.strip() and not ln.strip().startswith("--")]
    print(f"  {table}: {', '.join(cols)}")
