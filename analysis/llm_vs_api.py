"""Did the API already contain what the LLM was extracting from prose?

The rules extractor reads free-form contract text and returns station,
measure, threshold and settlement source. The question is whether those
were ever only available as prose, or whether Kalshi has been serving
them as structured fields all along -- in which case an LLM was hired
to re-derive facts that were one field lookup away.

Compare, for the same markets:
  * what rules_cache.json holds (the LLM's answer)
  * the STRUCTURED fields on the market object
  * /series/{series}, which the audit found is never called and which
    advertises settlement sources

This is not a rhetorical exercise. If the structured fields are absent
or wrong the LLM was the right call, and saying so matters as much as
the reverse.
"""
from __future__ import annotations

import json
import os

from kalshi_client import KalshiClient

kalshi = KalshiClient()

CACHE = "rules_cache.json"
cache = {}
if os.path.exists(CACHE):
    with open(CACHE, encoding="utf-8") as fh:
        cache = json.load(fh)
print(f"cache holds {len(cache):,} extractions\n")

STRUCTURED = ["strike_type", "custom_strike", "floor_strike", "cap_strike",
              "cap_strike_dollars", "floor_strike_dollars",
              "settlement_value_dollars", "expiration_value",
              "market_type", "yes_sub_title", "title",
              "settlement_timer_seconds", "can_close_early",
              "early_close_condition", "rules_primary"]

for series in ("KXRAINNYC", "KXHIGHNY"):
    print("=" * 76)
    print(series)
    print("=" * 76)

    resp = kalshi.get_historical_markets(series_ticker=series, limit=6)
    markets = [m for m in resp.get("markets", [])
               if m.get("result") in ("yes", "no")]
    if not markets:
        print("  no settled markets")
        continue
    m = markets[0]
    ticker = m["ticker"]
    print(f"  ticker: {ticker}\n")

    print("  --- STRUCTURED FIELDS on the market object ---")
    for k in STRUCTURED:
        if k in m and m[k] not in (None, ""):
            val = str(m[k])
            if k == "rules_primary":
                val = val[:90] + ("..." if len(val) > 90 else "")
            print(f"    {k:<28} {val}")

    print("\n  --- what the LLM extracted for this ticker ---")
    hit = cache.get(ticker)
    if hit:
        for k, v in hit.items():
            if v not in (None, ""):
                print(f"    {k:<28} {str(v)[:70]}")
    else:
        near = [k for k in cache if k.startswith(series)]
        print(f"    not cached (cache has {len(near)} {series} entries)")
        if near:
            print(f"    nearest: {near[0]}")
            for k, v in cache[near[0]].items():
                if v not in (None, ""):
                    print(f"      {k:<26} {str(v)[:66]}")

    print("\n  --- /series/{series}, never called by the bot ---")
    try:
        s = kalshi.get_series(series)
        block = s.get("series") or s
        for k in ("ticker", "title", "category", "frequency",
                  "settlement_sources", "contract_url", "tags"):
            if k in block and block[k] not in (None, "", []):
                print(f"    {k:<28} {str(block[k])[:110]}")
    except Exception as exc:
        print(f"    failed: {type(exc).__name__}: {str(exc)[:70]}")
    print()
