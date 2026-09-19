"""Tests for export_archive.py.

These delete data, so nearly every test is about what must NOT happen:
no delete without a verified export, and no delete when the export is
short of what the database holds.
"""
import datetime as dt
import gzip
import os
import shutil
import sqlite3
import tempfile
import unittest

import export_archive

DAY = 86400
# 2026-09-10T00:00:00Z and the day after.
D1 = int(dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc).timestamp())
D2 = D1 + DAY


class ExportTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        self.dir = tempfile.mkdtemp()
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
                ticker TEXT, yes_bid INTEGER, yes_ask INTEGER);
        """)
        conn.executemany(
            "INSERT INTO market_snapshots (ts, ticker, yes_bid, yes_ask) "
            "VALUES (?,?,?,?)",
            [(D1 + i * 60, f"T{i}", i, i + 2) for i in range(50)]
            + [(D2 + i * 60, f"U{i}", i, i + 2) for i in range(30)])
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)
        shutil.rmtree(self.dir, ignore_errors=True)

    def count(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        finally:
            conn.close()


class TestExport(ExportTestCase):
    def test_finds_the_days_present(self):
        days = export_archive.days_with_rows(
            "market_snapshots", D2 + 10 * DAY, self.db)
        self.assertEqual(days, ["2026-09-10", "2026-09-11"])

    def test_writes_one_file_per_day(self):
        export_archive.export_day("market_snapshots", "2026-09-10",
                                   self.dir, self.db)
        p = export_archive.path_for("market_snapshots", "2026-09-10", self.dir)
        self.assertTrue(os.path.exists(p))

    def test_every_row_survives_the_round_trip(self):
        out = export_archive.export_day("market_snapshots", "2026-09-10",
                                         self.dir, self.db)
        self.assertEqual(out["rows"], 50)
        self.assertEqual(out["verified"], 50)
        import json
        p = export_archive.path_for("market_snapshots", "2026-09-10", self.dir)
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["ticker"], "T0")
        self.assertEqual(rows[-1]["ticker"], "T49")

    def test_days_do_not_bleed_into_each_other(self):
        export_archive.export_day("market_snapshots", "2026-09-10",
                                   self.dir, self.db)
        import json
        p = export_archive.path_for("market_snapshots", "2026-09-10", self.dir)
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            tickers = {json.loads(l)["ticker"] for l in fh if l.strip()}
        self.assertTrue(all(t.startswith("T") for t in tickers))

    def test_an_empty_day_writes_nothing(self):
        out = export_archive.export_day("market_snapshots", "2020-01-01",
                                         self.dir, self.db)
        self.assertEqual(out["rows"], 0)
        self.assertFalse(os.path.exists(
            export_archive.path_for("market_snapshots", "2020-01-01", self.dir)))


class TestPruneRefusesWithoutAVerifiedExport(ExportTestCase):
    def test_no_export_means_no_delete(self):
        out = export_archive.prune_day("market_snapshots", "2026-09-10",
                                        self.dir, self.db)
        self.assertEqual(out["deleted"], 0)
        self.assertIn("no export", out["error"])
        self.assertEqual(self.count(), 80)

    def test_a_short_export_means_no_delete(self):
        # The failure that matters: an export that only half-wrote, then
        # a prune that trusts it.
        export_archive.export_day("market_snapshots", "2026-09-10",
                                   self.dir, self.db)
        p = export_archive.path_for("market_snapshots", "2026-09-10", self.dir)
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            lines = fh.readlines()
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            fh.writelines(lines[:20])          # truncate the export
        out = export_archive.prune_day("market_snapshots", "2026-09-10",
                                        self.dir, self.db)
        self.assertEqual(out["deleted"], 0)
        self.assertIn("refusing to delete", out["error"])
        self.assertEqual(self.count(), 80)

    def test_a_verified_export_allows_the_delete(self):
        export_archive.export_day("market_snapshots", "2026-09-10",
                                   self.dir, self.db)
        out = export_archive.prune_day("market_snapshots", "2026-09-10",
                                        self.dir, self.db)
        self.assertEqual(out["deleted"], 50)
        self.assertEqual(self.count(), 30)

    def test_the_other_day_is_untouched(self):
        export_archive.export_day("market_snapshots", "2026-09-10",
                                   self.dir, self.db)
        export_archive.prune_day("market_snapshots", "2026-09-10",
                                  self.dir, self.db)
        conn = sqlite3.connect(self.db)
        remaining = conn.execute(
            "SELECT DISTINCT date(ts,'unixepoch') FROM market_snapshots"
        ).fetchall()
        conn.close()
        self.assertEqual([r[0] for r in remaining], ["2026-09-11"])


class TestRun(ExportTestCase):
    def test_respects_the_keep_window(self):
        # Both fixture days are well in the past, so a huge keep window
        # should protect them.
        results = export_archive.run(keep_days=10_000, directory=self.dir,
                                      tables=["market_snapshots"],
                                      do_prune=True, db_path=self.db)
        self.assertEqual(results, [])
        self.assertEqual(self.count(), 80)

    def test_exports_without_pruning_by_default(self):
        export_archive.run(keep_days=1, directory=self.dir,
                           tables=["market_snapshots"], db_path=self.db)
        self.assertEqual(self.count(), 80, "run() must not delete unless asked")
        self.assertTrue(os.path.exists(export_archive.path_for(
            "market_snapshots", "2026-09-10", self.dir)))

    def test_prune_removes_only_what_it_exported(self):
        export_archive.run(keep_days=1, directory=self.dir,
                           tables=["market_snapshots"], do_prune=True,
                           db_path=self.db)
        self.assertEqual(self.count(), 0)

    def test_a_missing_table_is_skipped_not_fatal(self):
        results = export_archive.run(keep_days=1, directory=self.dir,
                                      tables=["market_snapshots", "no_such"],
                                      db_path=self.db)
        self.assertTrue(any(r["table"] == "market_snapshots" for r in results))

    def test_rerunning_is_safe(self):
        export_archive.run(keep_days=1, directory=self.dir,
                           tables=["market_snapshots"], do_prune=True,
                           db_path=self.db)
        again = export_archive.run(keep_days=1, directory=self.dir,
                                    tables=["market_snapshots"],
                                    do_prune=True, db_path=self.db)
        self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main()
