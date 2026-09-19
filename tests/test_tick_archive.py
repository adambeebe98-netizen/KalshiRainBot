"""Tests for tick_archive.py."""
import gzip
import json
import os
import shutil
import tempfile
import unittest

import tick_archive

# 2026-09-19T03:00:00Z and 2026-09-20T01:00:00Z
DAY_A = 1789786800
DAY_B = 1789866000


class TickArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def archive(self, **kw):
        kw.setdefault("directory", self.dir)
        kw.setdefault("flush_every", 1000)
        kw.setdefault("flush_seconds", 1e9)
        return tick_archive.TickArchive(**kw)


class TestRoundTrip(TickArchiveTestCase):
    def test_what_goes_in_comes_out(self):
        messages = [json.dumps({"type": "ticker", "i": i, "price": i * 3})
                    for i in range(50)]
        with self.archive() as a:
            for i, m in enumerate(messages):
                a.append(DAY_A + i, m)
        got = list(tick_archive.read_day("2026-09-19", self.dir))
        self.assertEqual(len(got), 50)
        self.assertEqual([g["r"] for g in got], [DAY_A + i for i in range(50)])
        self.assertEqual([g["m"]["i"] for g in got], list(range(50)))

    def test_message_is_stored_verbatim_not_re_serialised(self):
        # Key detail: the bytes on disk should be the bytes the exchange
        # sent, with no escaping round trip.
        raw = '{"type":"ticker","ticker":"KXRAINNYC-26SEP19-T0","yes_bid":42}'
        with self.archive() as a:
            a.append(DAY_A, raw)
        with gzip.open(os.path.join(self.dir, "2026-09-19.jsonl.gz"),
                       "rt", encoding="utf-8") as fh:
            line = fh.read().strip()
        self.assertIn(raw, line)
        self.assertNotIn('\\"', line, "message was escaped as a string")

    def test_a_multiline_message_does_not_break_the_file(self):
        # The bug this guards: embedding a message verbatim is only safe
        # while it is single-line. A pretty-printed payload split across
        # two lines and made every record after it unreadable.
        pretty = '{\n  "type": "ticker",\n  "n": 1\n}'
        with self.archive() as a:
            a.append(DAY_A, pretty)
            a.append(DAY_A + 1, '{"type":"ticker","n":2}')
            a.append(DAY_A + 2, '{"type":"ticker","n":3}')
        got = list(tick_archive.read_day("2026-09-19", self.dir))
        self.assertEqual(len(got), 3, "a multiline message ate the records "
                                      "that followed it")
        self.assertIn("ticker", got[0]["m"])
        self.assertEqual(got[2]["m"]["n"], 3)

    def test_a_damaged_line_does_not_abort_the_read(self):
        with self.archive() as a:
            a.append(DAY_A, '{"n":1}')
        path = os.path.join(self.dir, "2026-09-19.jsonl.gz")
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write('{"r":1,"m":{"broken"\n')
            fh.write('{"r":2,"m":{"n":3}}\n')
        seen = []
        got = list(tick_archive.read_day(
            "2026-09-19", self.dir, on_bad_line=lambda *a: seen.append(a)))
        self.assertEqual(len(got), 2)
        self.assertEqual(len(seen), 1)

    def test_verify_day_counts_damage(self):
        with self.archive() as a:
            a.append(DAY_A, '{"n":1}')
        path = os.path.join(self.dir, "2026-09-19.jsonl.gz")
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write("not json\n")
        out = tick_archive.verify_day("2026-09-19", self.dir)
        self.assertEqual(out["records"], 1)
        self.assertEqual(out["bad_lines"], 1)

    def test_non_json_message_is_stored_safely(self):
        with self.archive() as a:
            a.append(DAY_A, "not json at all")
            a.append(DAY_A + 1, '{"ok":true}')
        got = list(tick_archive.read_day("2026-09-19", self.dir))
        self.assertEqual(got[0]["m"], "not json at all")
        self.assertEqual(got[1]["m"], {"ok": True})

    def test_appends_across_separate_sessions(self):
        with self.archive() as a:
            a.append(DAY_A, '{"n":1}')
        with self.archive() as a:
            a.append(DAY_A, '{"n":2}')
        got = list(tick_archive.read_day("2026-09-19", self.dir))
        self.assertEqual([g["m"]["n"] for g in got], [1, 2])


class TestDayPartitioning(TickArchiveTestCase):
    def test_records_land_in_their_own_day(self):
        with self.archive() as a:
            a.append(DAY_A, '{"n":1}')
            a.append(DAY_B, '{"n":2}')
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["2026-09-19.jsonl.gz", "2026-09-20.jsonl.gz"])

    def test_a_file_never_mixes_two_days(self):
        with self.archive() as a:
            for i in range(10):
                a.append(DAY_A + i, '{"d":"a"}')
            for i in range(10):
                a.append(DAY_B + i, '{"d":"b"}')
        for day, tag in (("2026-09-19", "a"), ("2026-09-20", "b")):
            tags = {r["m"]["d"] for r in tick_archive.read_day(day, self.dir)}
            self.assertEqual(tags, {tag}, f"{day} mixed days")

    def test_archived_days_lists_them(self):
        with self.archive() as a:
            a.append(DAY_A, '{"n":1}')
            a.append(DAY_B, '{"n":2}')
        self.assertEqual(tick_archive.archived_days(self.dir),
                         ["2026-09-19", "2026-09-20"])

    def test_path_for_uses_utc(self):
        self.assertTrue(tick_archive.path_for(DAY_A, self.dir)
                        .endswith("2026-09-19.jsonl.gz"))


class TestBuffering(TickArchiveTestCase):
    def test_nothing_is_written_before_a_flush(self):
        a = self.archive(flush_every=1000)
        a.append(DAY_A, '{"n":1}')
        self.assertEqual(os.listdir(self.dir), [])

    def test_count_threshold_triggers_a_flush(self):
        a = self.archive(flush_every=5)
        for i in range(5):
            a.append(DAY_A + i, '{"n":%d}' % i)
        self.assertEqual(len(list(tick_archive.read_day("2026-09-19", self.dir))), 5)

    def test_time_threshold_triggers_a_flush(self):
        a = self.archive(flush_every=10_000, flush_seconds=0.0)
        a.append(DAY_A, '{"n":1}')
        self.assertEqual(len(list(tick_archive.read_day("2026-09-19", self.dir))), 1)

    def test_close_flushes(self):
        a = self.archive(flush_every=1000)
        a.append(DAY_A, '{"n":1}')
        a.close()
        self.assertEqual(len(list(tick_archive.read_day("2026-09-19", self.dir))), 1)

    def test_flush_on_an_empty_buffer_is_a_noop(self):
        a = self.archive()
        self.assertEqual(a.flush(), 0)
        self.assertEqual(os.listdir(self.dir), [])

    def test_records_written_is_tracked(self):
        with self.archive(flush_every=2) as a:
            for i in range(7):
                a.append(DAY_A + i, '{"n":%d}' % i)
        self.assertEqual(a.records_written, 7)


class TestCompressionActuallyHelps(TickArchiveTestCase):
    def test_real_shaped_messages_compress_several_fold(self):
        # The justification for the whole module. Messages of this shape
        # averaged 544 bytes in SQLite; if compression did not pay, moving
        # them out of the database would buy much less than claimed.
        msg = json.dumps({
            "type": "ticker", "sid": 42,
            "msg": {"market_ticker": "KXRAINNYC-26SEP19-T0",
                    "yes_bid": 42, "yes_ask": 45, "last_price": 43,
                    "volume": 1200, "open_interest": 8400,
                    "ts": 1789786800, "dollar_volume": 51600}})
        n = 2000
        with self.archive(flush_every=500) as a:
            for i in range(n):
                a.append(DAY_A + i, msg)
        size = os.path.getsize(os.path.join(self.dir, "2026-09-19.jsonl.gz"))
        raw_size = n * (len(msg) + 20)
        ratio = raw_size / size
        self.assertGreater(ratio, 5.0,
                           f"compression ratio only {ratio:.1f}x -- the "
                           f"storage argument for this module is weaker "
                           f"than claimed")


class TestStats(TickArchiveTestCase):
    def test_reports_days_and_bytes(self):
        with self.archive(flush_every=1) as a:
            a.append(DAY_A, '{"n":1}')
            a.append(DAY_B, '{"n":2}')
        s = tick_archive.stats(self.dir)
        self.assertEqual(s["days"], 2)
        self.assertGreater(s["bytes"], 0)
        self.assertEqual(s["first"], "2026-09-19")
        self.assertEqual(s["last"], "2026-09-20")

    def test_empty_directory_is_handled(self):
        s = tick_archive.stats(os.path.join(self.dir, "nothing-here"))
        self.assertEqual(s["days"], 0)
        self.assertIsNone(s["first"])

    def test_reading_a_missing_day_yields_nothing(self):
        self.assertEqual(list(tick_archive.read_day("1999-01-01", self.dir)), [])


if __name__ == "__main__":
    unittest.main()
