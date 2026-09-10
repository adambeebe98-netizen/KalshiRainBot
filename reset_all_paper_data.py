"""
ONE-OFF, run-once-by-hand script. Not part of the bot's normal operation,
not imported by anything else — never wired into bot.py or any scheduled
path. Run manually on the droplet when you actually want a clean slate.

Backs up the database file first (non-negotiable before anything
destructive), prints exactly what exists before and after, then clears
EVERY table: decisions, trades, bankroll_snapshots, calibration_stats,
shadow_trades, shadow_bankroll_snapshots, price_history, forecast_history,
strategy_overrides, suggestions, retrospectives, meta.

Context for why this exists: a confirmed bug (open_positions_count never
being seeded from real data across restarts) let strategies accumulate
far more simultaneous open positions than intended, and some settled
history predates other correctness fixes from the same session (pricing,
station codes). Rather than try to untangle which specific trades were
affected, the call was made to wipe everything and start clean — only ~1.5
days of data existed, not enough calibration progress to be worth trying
to salvage a subset of.

Usage (from the bot's directory, with its real venv):
    python3 reset_all_paper_data.py             # asks for confirmation first
    python3 reset_all_paper_data.py --yes       # skips the confirmation prompt
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from config import SETTINGS
import storage

TABLES = [
    "decisions", "trades", "bankroll_snapshots", "calibration_stats",
    "shadow_trades", "shadow_bankroll_snapshots", "price_history",
    "forecast_history", "strategy_overrides", "suggestions",
    "retrospectives", "meta",
]


def _row_counts() -> dict[str, int]:
    counts = {}
    with storage.get_conn() as conn:
        for t in TABLES:
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception as e:
                counts[t] = f"error: {e}"
    return counts


def main():
    storage.init_db()  # make sure every table actually exists before touching it

    db_path = Path(SETTINGS.db_path)
    print(f"Database: {db_path.resolve()}")
    print("\nCurrent row counts:")
    before = _row_counts()
    for t, n in before.items():
        print(f"  {t:<24} {n}")
    total_before = sum(v for v in before.values() if isinstance(v, int))
    print(f"\nTotal rows across all tables: {total_before}")

    if "--yes" not in sys.argv:
        print("\nThis will PERMANENTLY clear every table above (a backup is made first).")
        answer = input("Type 'wipe' to continue: ").strip().lower()
        if answer != "wipe":
            print("Aborted — nothing was touched.")
            return

    backup_path = db_path.with_name(f"{db_path.stem}.backup-{int(time.time())}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    print(f"\nBacked up database to: {backup_path.resolve()}")

    with storage.get_conn() as conn:
        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.commit()

    print("\nAll tables cleared. Row counts after:")
    after = _row_counts()
    for t, n in after.items():
        print(f"  {t:<24} {n}")

    print(f"\nDone. Every strategy's bankroll will resume from the configured "
          f"starting value (${SETTINGS.starting_bankroll_cents/100:.2f}) on its next trade. "
          f"Backup saved at {backup_path.name} if you ever want to look back at the old data.")


if __name__ == "__main__":
    main()
