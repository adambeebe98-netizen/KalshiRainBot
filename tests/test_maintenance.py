"""Tests for maintenance.py.

These delete data, so the assertions are mostly about what must NOT be
deleted.
"""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

import maintenance
import tick_archive

DAY = 86400


class MaintenanceTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        self.archive_dir = tempfile.mkdtemp()
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE realtime_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
                channel TEXT NOT NULL, ts INTEGER NOT NULL,
                received_ts INTEGER NOT NULL, yes_price_cents INTEGER,
                yes_bid_cents INTEGER, yes_ask_cents INTEGER, volume INTEGER,
                open_interest INTEGER, raw_json TEXT NOT NULL);
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
                ticker TEXT, action TEXT NOT NULL, reason TEXT);
        """)
        now = int(time.time())
        conn.executemany(
            "INSERT INTO realtime_ticks (ticker, channel, ts, received_ts, raw_json) "
            "VALUES (?,?,?,?,?)",
            [("MKT", "ticker", now - i, now - i,
              '{"type":"ticker","n":%d}' % i) for i in range(25)])
        # Some rows already migrated.
        conn.executemany(
            "INSERT INTO realtime_ticks (ticker, channel, ts, received_ts, raw_json) "
            "VALUES (?,?,?,?,?)",
            [("MKT", "ticker", now, now, "@2026-09-19") for _ in range(5)])
        rows = []
        for age_days, action, n in ((30, "skipped", 100), (30, "traded", 3),
                                    (1, "skipped", 50), (1, "traded", 2)):
            for i in range(n):
                rows.append((now - age_days * DAY - i, "MKT", action, "r"))
        conn.executemany(
            "INSERT INTO decisions (ts, ticker, action, reason) VALUES (?,?,?,?)",
            rows)
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)
        shutil.rmtree(self.archive_dir, ignore_errors=True)

    def _count(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()


class TestLegacyArchiveMigration(MaintenanceTestCase):
    def test_moves_only_unmigrated_rows(self):
        out = maintenance.archive_legacy_raw_json(
            db_path=self.db, archive_dir=self.archive_dir, batch_size=10)
        self.assertEqual(out["rows_moved"], 25)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM realtime_ticks "
                        "WHERE raw_json NOT LIKE '@%'"), 0)

    def test_the_messages_survive_in_the_archive(self):
        maintenance.archive_legacy_raw_json(
            db_path=self.db, archive_dir=self.archive_dir, batch_size=10)
        found = []
        for day in tick_archive.archived_days(self.archive_dir):
            found.extend(tick_archive.read_day(day, self.archive_dir))
        self.assertEqual(len(found), 25)
        self.assertEqual(sorted(r["m"]["n"] for r in found), list(range(25)))

    def test_rows_point_at_the_day_holding_them(self):
        maintenance.archive_legacy_raw_json(
            db_path=self.db, archive_dir=self.archive_dir)
        conn = sqlite3.connect(self.db)
        pointers = {r[0] for r in conn.execute(
            "SELECT DISTINCT raw_json FROM realtime_ticks")}
        conn.close()
        days = set(tick_archive.archived_days(self.archive_dir))
        for p in pointers:
            self.assertTrue(p.startswith("@"))
            if p != "@2026-09-19":          # the pre-seeded already-migrated rows
                self.assertIn(p[1:], days)

    def test_is_resumable(self):
        maintenance.archive_legacy_raw_json(
            db_path=self.db, archive_dir=self.archive_dir,
            batch_size=10, max_batches=1)
        remaining = self._count("SELECT COUNT(*) FROM realtime_ticks "
                                "WHERE raw_json NOT LIKE '@%'")
        self.assertEqual(remaining, 15)
        maintenance.archive_legacy_raw_json(
            db_path=self.db, archive_dir=self.archive_dir, batch_size=10)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM realtime_ticks "
                        "WHERE raw_json NOT LIKE '@%'"), 0)

    def test_running_twice_moves_nothing_the_second_time(self):
        maintenance.archive_legacy_raw_json(
            db_path=self.db, archive_dir=self.archive_dir)
        again = maintenance.archive_legacy_raw_json(
            db_path=self.db, archive_dir=self.archive_dir)
        self.assertEqual(again["rows_moved"], 0)

    def test_row_count_is_unchanged(self):
        before = self._count("SELECT COUNT(*) FROM realtime_ticks")
        maintenance.archive_legacy_raw_json(
            db_path=self.db, archive_dir=self.archive_dir)
        self.assertEqual(self._count("SELECT COUNT(*) FROM realtime_ticks"), before)


class TestDecisionPruning(MaintenanceTestCase):
    def test_traded_decisions_are_never_deleted(self):
        before = self._count(
            "SELECT COUNT(*) FROM decisions WHERE action = 'traded'")
        maintenance.prune_decisions(keep_days=7, db_path=self.db)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM decisions WHERE action = 'traded'"),
            before)

    def test_old_skipped_decisions_go(self):
        out = maintenance.prune_decisions(keep_days=7, db_path=self.db)
        self.assertEqual(out["deleted"], 100)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM decisions WHERE action = 'skipped'"),
            50)

    def test_recent_skipped_decisions_stay(self):
        maintenance.prune_decisions(keep_days=7, db_path=self.db)
        recent = self._count(
            "SELECT COUNT(*) FROM decisions WHERE ts > ?",
            (int(time.time()) - 7 * DAY,))
        self.assertGreater(recent, 0)

    def test_a_longer_window_deletes_nothing(self):
        out = maintenance.prune_decisions(keep_days=365, db_path=self.db)
        self.assertEqual(out["deleted"], 0)

    def test_refuses_a_zero_day_window(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                maintenance.prune_decisions(keep_days=bad, db_path=self.db)

    def test_reports_what_it_kept(self):
        out = maintenance.prune_decisions(keep_days=7, db_path=self.db)
        self.assertEqual(out["traded_kept"], 5)
        self.assertEqual(out["remaining"], 55)


class TestReport(MaintenanceTestCase):
    def test_renders_sizes_and_counts(self):
        text = maintenance.report(db_path=self.db, archive_dir=self.archive_dir)
        self.assertIn("realtime_ticks", text)
        self.assertIn("still holding full messages", text)
        self.assertIn("tick archive", text)

    def test_counts_unmigrated_rows(self):
        text = maintenance.report(db_path=self.db, archive_dir=self.archive_dir)
        self.assertIn("full messages: 25", text)


if __name__ == "__main__":
    unittest.main()
