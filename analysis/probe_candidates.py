"""Probe candidate series for settled history worth backfilling.

The unfiltered historical endpoint pages backwards from the newest
settled date, so 30,000 markets came back from a single day and told us
nothing about depth. Per-series queries do work -- that is how the first
sports probe found thousands per family.

Candidates come from what that one-day sample revealed rather than from
taste. Per-market volume was the striking number: KXITFMATCH traded
203,751 per market against 867 for the parlay series that dominated the
raw count. Volume per market is the figure that decides whether a family
is worth collecting; total count is how weather looked good and was not.

Each line reports what a backfill would actually yield, so the expansion
list is chosen on evidence.
"""
from __future__ import annotations

import sys
import time

from kalshi_client import KalshiClient

kalshi = KalshiClient()
MAX_PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 30

CANDIDATES = {
    "tennis": ["KXITFMATCH", "KXITFWMATCH", "KXATPMATCH", "KXWTAMATCH",
               "KXATPCHALLENGERMATCH", "KXATPGSPREAD"],
    "esports": ["KXVALORANTGAME", "KXLOLGAME", "KXCS2GAME", "KXR6GAME",
                "KXVALORANTMAP", "KXLOLMAP", "KXCS2MAP"],
    "us sports": ["KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNHLGAME",
                  "KXNCAAFGAME", "KXNCAABGAME", "KXWNBAGAME"],
    "commodities": ["KXWTIH", "KXGOLDH", "KXSILVERH", "KXNATGASD"],
    "weather (hourly temp)": ["KXTEMPLAXH", "KXTEMPCHIH", "KXTEMPDCH",
                              "KXTEMPAUSH", "KXTEMPNYCH"],
    "ambiguous settlement": ["KXMAMDANIMENTION", "KXRT", "KXTRUMPMENTION"],
    "finance": ["KXINX", "KXNASDAQ100", "KXBTC", "KXETH"],
}


def fp(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def probe(series: str) -> dict | None:
    markets, cursor, pages = [], None, 0
    while pages < MAX_PAGES:
        try:
            resp = kalshi.get_historical_markets(series_ticker=series,
                                                 cursor=cursor, limit=200)
        except Exception:
            return None
        batch = resp.get("markets", [])
        markets.extend(batch)
        cursor = resp.get("cursor")
        pages += 1
        if not cursor or not batch:
            break
    if not markets:
        return None
    settled = [m for m in markets if m.get("result") in ("yes", "no")]
    traded = [m for m in settled if fp(m.get("volume_fp")) > 0]
    volume = sum(fp(m.get("volume_fp")) for m in settled)
    closes = sorted((m.get("close_time") or "")[:10] for m in markets
                    if m.get("close_time"))
    return {
        "settled": len(settled),
        "traded": len(traded),
        "volume": volume,
        "per_market": volume / max(len(traded), 1),
        "oldest": closes[0] if closes else "-",
        "newest": closes[-1] if closes else "-",
        "capped": bool(cursor),
    }


print(f"{'series':<24} {'settled':>8} {'traded':>7} {'vol/market':>11} "
      f"{'oldest':>11} {'newest':>11}")
t0 = time.time()
for group, series_list in CANDIDATES.items():
    print(f"\n--- {group} ---")
    for series in series_list:
        r = probe(series)
        if not r:
            print(f"{series:<24} {'(no settled history)':>40}")
            continue
        cap = " CAPPED" if r["capped"] else ""
        print(f"{series:<24} {r['settled']:>8,} {r['traded']:>7,} "
              f"{r['per_market']:>11,.0f} {r['oldest']:>11} "
              f"{r['newest']:>11}{cap}")
print(f"\n{time.time()-t0:.0f}s")
