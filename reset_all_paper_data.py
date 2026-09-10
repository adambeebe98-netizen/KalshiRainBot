"""
Also importable — web_ui/app.py's "Wipe all paper data" Settings action
calls wipe_all_data() directly, so there's exactly one implementation of
this destructive operation, not two that could quietly drift apart.

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


def row_counts() -> dict[str, int]:
    storage.init_db()  # make sure every table actually exists before touching it
    counts = {}
    with storage.get_conn() as conn:
        for t in TABLES:
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception as e:
                counts[t] = f"error: {e}"
    return counts


def wipe_all_data() -> dict:
    """
    Backs up the database file first (non-negotiable before anything
    destructive — no caller-provided way to skip this), clears every
    table, returns {"backup_path": str, "before": {...}, "after": {...}}
    so callers (this script's CLI, or the dashboard's web route) can
    report exactly what happened without re-deriving it themselves.
    """
    storage.init_db()
    db_path = Path(SETTINGS.db_path)
    before = row_counts()

    backup_path = db_path.with_name(f"{db_path.stem}.backup-{int(time.time())}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)

    with storage.get_conn() as conn:
        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.commit()

    after = row_counts()
    return {"backup_path": str(backup_path), "before": before, "after": after}


def main():
    before = row_counts()
    db_path = Path(SETTINGS.db_path)
    print(f"Database: {db_path.resolve()}")
    print("\nCurrent row counts:")
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

    result = wipe_all_data()
    print(f"\nBacked up database to: {result['backup_path']}")
    print("\nAll tables cleared. Row counts after:")
    for t, n in result["after"].items():
        print(f"  {t:<24} {n}")

    print(f"\nDone. Every strategy's bankroll will resume from the configured "
          f"starting value (${SETTINGS.starting_bankroll_cents/100:.2f}) on its next trade. "
          f"Backup saved if you ever want to look back at the old data.")


if __name__ == "__main__":
    main()

