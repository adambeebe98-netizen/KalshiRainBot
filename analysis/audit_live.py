"""Exactly what is being captured live, and what is not.

"Are we recording everything?" deserves a measured answer rather than a
confident one. Four services run; they cover different things, and the
gaps between them are the interesting part.

Checks, per source: what it is configured to watch, what it actually
wrote in the last hour, and where that lands on disk.
"""
from __future__ import annotations

import glob
import gzip
import os
import sqlite3
import time
from contextlib import closing

import collector
import sports_truth
from config import SETTINGS

DB = SETTINGS.db_path
since = int(time.time()) - 3600

print("=" * 68)
print("CONFIGURED")
print("=" * 68)
print(f"  weather series (collector)   : {len(SETTINGS.series_tickers)}")
print(f"  non-weather series (collector): {len(collector.EXTRA_SERIES)}")
print(f"  ESPN leagues (sports_truth)  : {len(sports_truth.LEAGUES)}"
      f"  {sorted(sports_truth.LEAGUES)}")

print()
print("=" * 68)
print("WRITTEN IN THE LAST HOUR")
print("=" * 68)
with closing(sqlite3.connect(DB)) as conn:
    for label, table, col in (
            ("market_snapshots (prices+size)", "market_snapshots", "ts"),
            ("price_history", "price_history", "ts"),
            ("realtime_ticks (websocket)", "realtime_ticks", "received_ts"),
            ("wx_observations", "wx_observations", "valid_at"),
            ("wx_forecasts", "wx_forecasts", "valid_at")):
        try:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} > ?",
                (since,)).fetchone()[0]
            t = conn.execute(
                f"SELECT COUNT(DISTINCT ticker) FROM {table} WHERE {col} > ?",
                (since,)).fetchone()[0] if table != "wx_observations" else None
        except sqlite3.Error as exc:
            print(f"  {label:<32} ERROR {exc}")
            continue
        extra = f", {t:,} tickers" if t is not None else ""
        print(f"  {label:<32} {n:>9,} rows{extra}")

print()
print("  live ground-truth files written in the last hour:")
now_day = time.strftime("%Y-%m-%d", time.gmtime())
for d in sorted(glob.glob(f"data/truth/*")):
    path = os.path.join(d, f"{now_day}.jsonl.gz")
    if not os.path.exists(path):
        continue
    if time.time() - os.path.getmtime(path) > 3600:
        continue
    n = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            n = sum(1 for line in fh if line.strip())
    except OSError:
        pass
    print(f"    {os.path.basename(d):<24} {n:>7,} rows today")

print()
print("=" * 68)
print("THE GAPS")
print("=" * 68)

kalshi_leagues = {
    "KXNBAGAME": "nba", "KXNBASPREAD": "nba", "KXNBATOTAL": "nba",
    "KXWNBAGAME": "wnba", "KXWNBAPTS": "wnba", "KXWNBAREB": "wnba",
    "KXNHLGAME": "nhl", "KXNCAAFGAME": "ncaaf",
    "KXNFLGAME": "nfl", "KXNFLSPREAD": "nfl", "KXNFLTOTAL": "nfl",
    "KXMLBGAME": "mlb", "KXMLBHIT": "mlb", "KXMLBTB": "mlb",
    "KXEPLGAME": "epl", "KXLALIGAGAME": "laliga",
    "KXSERIEAGAME": "seriea", "KXBUNDESLIGAGAME": "bundesliga",
    "KXLIGUE1GAME": "ligue1",
}
watched = set(sports_truth.LEAGUES)
need = {kalshi_leagues[s] for s in collector.EXTRA_SERIES
        if s in kalshi_leagues}
missing = sorted(need - watched)
print(f"  Kalshi series collected whose league has NO live ground truth:")
if missing:
    for league in missing:
        series = sorted(s for s in collector.EXTRA_SERIES
                        if kalshi_leagues.get(s) == league)
        print(f"    {league:<12} {series}")
else:
    print("    none")

no_espn = [s for s in collector.EXTRA_SERIES
           if s.startswith(("KXATP", "KXWTA", "KXITF", "KXCS2", "KXLOL",
                            "KXVALORANT"))]
print(f"\n  collected but ESPN cannot cover at all (no play data exists):")
print(f"    {no_espn}")
