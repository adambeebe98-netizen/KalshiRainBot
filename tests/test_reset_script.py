"""
Covers reset_all_paper_data.py's reusable functions — wipe_all_data() is
shared between the CLI script's own confirmation flow and the dashboard's
"Wipe all paper data" web route, so there's exactly one implementation of
this destructive operation.
"""
from __future__ import annotations

import sqlite3
import unittest

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402
import reset_all_paper_data as reset_script  # noqa: E402


class TestWipeAllData(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, *reset_script.TABLES)

    def test_wipes_every_table(self):
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.log_trade("T2", "yes", 5, 40, "paper", None)
        storage.log_suggestion("swing", "entry_max", 40, 35, "test")
        storage.log_retrospective("test analysis", 10, 7, 3)

        result = reset_script.wipe_all_data()

        self.assertEqual(result["before"]["shadow_trades"], 1)
        self.assertEqual(result["before"]["trades"], 1)
        self.assertEqual(result["before"]["suggestions"], 1)
        self.assertEqual(result["before"]["retrospectives"], 1)
        for table, count in result["after"].items():
            with self.subTest(table=table):
                self.assertEqual(count, 0)

    def test_creates_a_real_backup_with_the_pre_wipe_data(self):
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        result = reset_script.wipe_all_data()

        conn = sqlite3.connect(result["backup_path"])
        try:
            n = conn.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0]
            self.assertEqual(n, 1, "backup must contain the data as it was BEFORE the wipe")
        finally:
            conn.close()

    def test_row_counts_reflects_real_data(self):
        storage.log_trade("T1", "yes", 5, 40, "paper", None)
        counts = reset_script.row_counts()
        self.assertEqual(counts["trades"], 1)
        self.assertEqual(counts["shadow_trades"], 0)


if __name__ == "__main__":
    unittest.main()
