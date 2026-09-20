"""Will the full weather trade backfill fit, and what breaks if it does not?

The droplet is a 24 GB box that also runs five live services. A backfill
that fills the disk does not merely fail -- it takes down collection,
and live data is the only kind that cannot be re-fetched. So the size
question is a safety question, not a capacity one.

Estimates come from the rain backfill's measured bytes-per-trade rather
than a guess, and the growth rate of the live services is measured too,
because the backfill is not the only thing consuming the disk while it
runs.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from contextlib import closing

from config import SETTINGS


def du(path: str) -> int:
    try:
        out = subprocess.run(["du", "-sb", path], capture_output=True,
                             text=True, timeout=120).stdout
        return int(out.split()[0])
    except Exception:
        return 0


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


total, used, free = shutil.disk_usage("/")
print("=" * 66)
print("DROPLET DISK")
print("=" * 66)
print(f"  total {human(total)}   used {human(used)}   FREE {human(free)}")

print("\n  biggest consumers:")
for path in ("data/backfill", "data/truth_backfill", "data/trades",
             "data/backfill_wx", "data/ticks", "data/export",
             "data/espn_core", "data/market_rules", "data/truth",
             "bot_state.db"):
    if os.path.exists(path):
        print(f"    {path:<26} {human(du(path)):>12}")

# --- measured cost per trade --------------------------------------------
trades_bytes = du("data/trades")
n_trades = 0
try:
    import glob
    import gzip
    import json
    for p in glob.glob("data/trades/*/*.jsonl.gz"):
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    n_trades += len(json.loads(line).get("trades") or [])
except Exception as exc:
    print(f"  (could not count trades: {exc})")

print("\n" + "=" * 66)
print("PROJECTED COST OF THE FULL WEATHER TRADE BACKFILL")
print("=" * 66)
if n_trades:
    per = trades_bytes / n_trades
    print(f"  measured: {n_trades:,} trades in {human(trades_bytes)} "
          f"= {per:.1f} bytes/trade")
    with closing(sqlite3.connect(SETTINGS.db_path)) as conn:
        markets = conn.execute(
            "SELECT COUNT(*) FROM historical_markets "
            "WHERE result IN ('yes','no')").fetchone()[0]
    # Rain trades far more per market than a temperature bracket leg,
    # so this is an upper bound by design.
    per_market = n_trades / max(len(
        [p for p in __import__("glob").glob("data/trades/*/*.jsonl.gz")]), 1)
    est_trades = markets * 199          # measured trades/market on rain
    est_bytes = est_trades * per
    print(f"  {markets:,} settled weather markets")
    print(f"  estimated {est_trades:,.0f} trades -> {human(est_bytes)}")
    print(f"  free after:               {human(free - est_bytes)}")
    verdict = "FITS" if est_bytes < free * 0.5 else "TIGHT -- do not run"
    print(f"  verdict: {verdict}")

# --- how fast is the live layer growing? ---------------------------------
print("\n" + "=" * 66)
print("LIVE GROWTH, which continues during any backfill")
print("=" * 66)
db_bytes = os.path.getsize(SETTINGS.db_path) if os.path.exists(
    SETTINGS.db_path) else 0
print(f"  bot_state.db                {human(db_bytes)}")
for extra in ("-wal", "-shm"):
    p = SETTINGS.db_path + extra
    if os.path.exists(p):
        print(f"  {os.path.basename(p):<27} {human(os.path.getsize(p))}")
print("\n  export_archive.py prunes SQLite to a rolling 14 days nightly,")
print("  so the database is flat. The data/ folders are NOT pruned and")
print("  grow until synced away.")
