"""
Tests for the order-book depth poller.

Depth is the one thing here that cannot be re-fetched, so the failure
that matters is silent under-capture: a parser that drops levels, or a
priority order that starves the markets near close. Both would leave an
archive that looks populated and answers the execution question wrongly.

The ask-derivation is tested explicitly because Kalshi returns BIDS
ONLY. An ask on one side is a dollar minus the other side's bid, and
getting that backwards produces a book that is plausible and inverted.
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import depth_poller as dp


class ParseBook(unittest.TestCase):

    RAW = {"orderbook_fp": {
        # Kalshi sends ascending; best price is the HIGHEST bid.
        "yes_dollars": [["0.3800", "158.00"], ["0.3900", "7.00"],
                        ["0.4000", "607.00"]],
        "no_dollars": [["0.4300", "10.00"], ["0.4500", "505.00"]],
    }}

    def test_yes_bids_are_best_price_first(self):
        book = dp.parse_book(self.RAW)
        self.assertEqual([p for p, _s in book["yes_bids"]], [40, 39, 38])

    def test_asks_are_derived_from_the_OTHER_side(self):
        """100 - no_bid, because there is no ask array at all."""
        book = dp.parse_book(self.RAW)
        # no bids at 43c and 45c -> yes asks at 57c and 55c
        self.assertEqual(sorted(p for p, _s in book["yes_asks"]), [55, 57])

    def test_ask_sizes_come_from_the_no_side(self):
        book = dp.parse_book(self.RAW)
        by_price = dict(book["yes_asks"])
        self.assertEqual(by_price[55], 505.0)
        self.assertEqual(by_price[57], 10.0)

    def test_depth_totals(self):
        book = dp.parse_book(self.RAW)
        self.assertEqual(book["yes_bid_depth"], 772.0)
        self.assertEqual(book["no_bid_depth"], 515.0)

    def test_an_empty_book_does_not_crash(self):
        book = dp.parse_book({"orderbook_fp": {}})
        self.assertEqual(book["yes_bids"], [])
        self.assertEqual(book["yes_bid_depth"], 0)

    def test_malformed_levels_are_skipped_not_fatal(self):
        raw = {"orderbook_fp": {"yes_dollars": [
            ["0.4000", "607.00"], ["oops"], ["bad", "worse"]]}}
        book = dp.parse_book(raw)
        self.assertEqual(book["yes_bids"], [[40, 607.0]])


class Candidates(unittest.TestCase):

    def _rows(self, rows):
        """Patch the DB read with canned market_snapshots rows."""
        class FakeConn:
            def __init__(self, data):
                self.data = data
                self.row_factory = None

            def execute(self, *_a, **_k):
                return self.data

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return FakeConn(rows)

    def test_orders_by_time_to_close(self):
        now = int(time.time())
        rows = [
            {"ticker": "FAR", "last_seen": now, "bid_size": 5,
             "ask_size": 5,
             "close_time": now + 40000},
            {"ticker": "SOON", "last_seen": now, "bid_size": 1,
             "ask_size": 1,
             "close_time": now + 600},
        ]
        with mock.patch.object(dp.sqlite3, "connect",
                               return_value=self._rows(rows)):
            out = dp.candidates()
        self.assertEqual([r["ticker"] for r in out], ["SOON", "FAR"],
                         "a market closing in 10 minutes must outrank one"
                         " closing in 11 hours")

    def test_closed_markets_are_excluded(self):
        now = int(time.time())
        rows = [{"ticker": "GONE", "last_seen": now, "bid_size": 9,
                 "ask_size": 9, "close_time": now - 60}]
        with mock.patch.object(dp.sqlite3, "connect",
                               return_value=self._rows(rows)):
            out = dp.candidates()
        self.assertEqual(out, [], "its book no longer exists")

    def test_displayed_size_breaks_ties(self):
        now = int(time.time())
        rows = [
            {"ticker": "THIN", "last_seen": now, "bid_size": 0,
             "ask_size": 0, "close_time": now + 600},
            {"ticker": "DEEP", "last_seen": now, "bid_size": 500,
             "ask_size": 500, "close_time": now + 600},
        ]
        with mock.patch.object(dp.sqlite3, "connect",
                               return_value=self._rows(rows)):
            out = dp.candidates()
        self.assertEqual(out[0]["ticker"], "DEEP")


class Writing(unittest.TestCase):

    def test_round_trips_as_gzipped_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            n = dp.write([{"ticker": "A", "yes_bids": [[40, 1.0]]},
                          {"ticker": "B", "yes_bids": []}], d)
            self.assertEqual(n, 2)
            path = os.path.join(d, f"{dp._day()}.jsonl.gz")
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
            self.assertEqual([r["ticker"] for r in rows], ["A", "B"])

    def test_appends_rather_than_truncating(self):
        with tempfile.TemporaryDirectory() as d:
            dp.write([{"ticker": "A"}], d)
            dp.write([{"ticker": "B"}], d)
            path = os.path.join(d, f"{dp._day()}.jsonl.gz")
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
            self.assertEqual(len(rows), 2)

    def test_empty_write_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(dp.write([], d), 0)
            self.assertFalse(os.path.exists(
                os.path.join(d, f"{dp._day()}.jsonl.gz")))


if __name__ == "__main__":
    unittest.main()
