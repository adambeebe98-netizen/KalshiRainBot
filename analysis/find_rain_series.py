"""Which series tickers carry the rain markets?

Needed so backfill_sports.py can be pointed at them: it already pulls
1-minute candles for the final six hours, which is exactly the window
the weather archive is missing.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from config import SETTINGS

with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    conn.row_factory = sqlite3.Row
    for measure in ("precipitation_daily", "precipitation_monthly"):
        print(f"\n{measure}")
        rows = conn.execute(
            "SELECT ticker, close_time FROM historical_markets "
            "WHERE measure = ? ORDER BY close_time", (measure,)).fetchall()
        fams: dict[str, list] = {}
        for r in rows:
            fam = r["ticker"].split("-")[0]
            fams.setdefault(fam, []).append(r["close_time"][:10])
        for fam, days in sorted(fams.items(), key=lambda kv: -len(kv[1])):
            print(f"   {fam:<22} {len(days):>5} markets   "
                  f"{min(days)} .. {max(days)}")
        print(f"   sample tickers: "
              f"{[r['ticker'] for r in rows[:2]]}")
