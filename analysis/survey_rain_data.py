"""What rain-market data do we hold with BOTH a price path and an outcome?

The calibration question -- does a contract trading at 20c resolve YES
20% of the time -- needs three things per market: a settled result, a
price at a stated moment, and enough markets per price bucket for the
answer to mean anything.

So establish what exists before computing anything on it. A calibration
curve built on 30 markets per bucket is noise with a confident shape.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from config import SETTINGS

with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
    conn.row_factory = sqlite3.Row

    print("=== historical_markets ===")
    r = conn.execute(
        "SELECT COUNT(*) n, SUM(result IS NOT NULL) settled, "
        "SUM(result='yes') yes FROM historical_markets").fetchone()
    print(f"  {r['n']:,} markets, {r['settled'] or 0:,} settled, "
          f"{r['yes'] or 0:,} YES")

    print("\n  by measure:")
    for row in conn.execute(
            "SELECT COALESCE(measure,'(null)') m, COUNT(*) n, "
            "SUM(result='yes') y, SUM(result IS NOT NULL) s "
            "FROM historical_markets GROUP BY m ORDER BY n DESC LIMIT 12"):
        rate = (100.0 * row["y"] / row["s"]) if row["s"] else 0
        print(f"    {row['m']:<22} {row['n']:>7,} markets  "
              f"{row['s'] or 0:>7,} settled  {rate:5.1f}% YES")

    print("\n  by ticker family:")
    for row in conn.execute(
            "SELECT substr(ticker, 1, instr(ticker||'-','-')-1) fam, "
            "COUNT(*) n, SUM(result IS NOT NULL) s, SUM(result='yes') y "
            "FROM historical_markets GROUP BY fam ORDER BY n DESC LIMIT 14"):
        rate = (100.0 * row["y"] / row["s"]) if row["s"] else 0
        print(f"    {row['fam']:<22} {row['n']:>7,} markets  "
              f"{row['s'] or 0:>7,} settled  {rate:5.1f}% YES")

    print("\n=== historical_price_points ===")
    r = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT ticker) t, "
        "MIN(ts) lo, MAX(ts) hi FROM historical_price_points").fetchone()
    print(f"  {r['n']:,} candles across {r['t']:,} tickers")

    print("\n  markets having BOTH a settled result and price points:")
    r = conn.execute(
        "SELECT COUNT(DISTINCT m.ticker) n FROM historical_markets m "
        "JOIN historical_price_points p ON p.ticker = m.ticker "
        "WHERE m.result IN ('yes','no')").fetchone()
    print(f"    {r['n']:,}")

    print("\n  of those, by family:")
    for row in conn.execute(
            "SELECT substr(m.ticker,1,instr(m.ticker||'-','-')-1) fam, "
            "COUNT(DISTINCT m.ticker) n FROM historical_markets m "
            "JOIN historical_price_points p ON p.ticker = m.ticker "
            "WHERE m.result IN ('yes','no') "
            "GROUP BY fam ORDER BY n DESC LIMIT 12"):
        print(f"    {row['fam']:<22} {row['n']:>7,}")
