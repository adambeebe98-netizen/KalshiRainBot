"""
Covers splits.py — the validation vault.

These tests are the enforcement, not a description of it: if any of them
start failing, the holdout period has become reachable through the normal
data path and every out-of-sample number computed after that point is
suspect. The important cases are the refusals (default access can't see
the vault, an unreasoned override is rejected) and the audit trail (a
real access is recorded with who and why).
"""
from __future__ import annotations

import unittest

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402
import splits  # noqa: E402
from splits import Split, VaultAccessError  # noqa: E402


class TestSplits(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "historical_markets", "historical_price_points",
                         "historical_weather_points", "vault_access_log")
        # One market per split, each with a price point, so "did the gate
        # filter correctly" is unambiguous rather than a count comparison.
        for ticker, close_time in (("TRAIN1", "2025-06-01T12:00:00Z"),
                                    ("DEV1", "2026-03-01T12:00:00Z"),
                                    ("VAULT1", "2026-05-01T12:00:00Z"),
                                    ("FUTURE1", "2026-09-01T12:00:00Z")):
            storage.save_historical_market(ticker, close_time=close_time, result="yes")
            storage.save_historical_price_points(ticker, [(1, 50, 10, 49, 51, 100)])

    def test_default_access_returns_train_and_never_vault(self):
        tickers = {r["ticker"] for r in splits.load("markets")}
        self.assertEqual(tickers, {"TRAIN1"})

    def test_dev_is_reachable_without_ceremony(self):
        tickers = {r["ticker"] for r in splits.load("markets", split=Split.DEV)}
        self.assertEqual(tickers, {"DEV1"})

    def test_train_dev_is_the_normal_fitting_set_and_still_excludes_vault(self):
        tickers = {r["ticker"] for r in splits.load("markets", split=Split.TRAIN_DEV)}
        self.assertEqual(tickers, {"TRAIN1", "DEV1"})

    def test_asking_for_the_vault_is_refused_by_default(self):
        with self.assertRaises(VaultAccessError):
            splits.load("markets", split=Split.VAULT)

    def test_allow_vault_without_a_reason_raises(self):
        with self.assertRaises(ValueError):
            splits.load("markets", split=Split.VAULT, allow_vault=True)
        with self.assertRaises(ValueError):
            splits.load("markets", split=Split.VAULT, allow_vault=True, reason="   ")

    def test_a_refused_access_is_not_logged(self):
        """Only real reads are auditable events — a blocked attempt
        shouldn't pad the log and make the vault look more spent than it is."""
        with self.assertRaises(VaultAccessError):
            splits.load("markets", split=Split.VAULT)
        self.assertEqual(splits.vault_access_history(), [])

    def test_a_deliberate_vault_access_returns_rows_and_is_logged(self):
        rows = splits.load("markets", split=Split.VAULT, allow_vault=True,
                            reason="final eval of candidate X, first look")
        self.assertEqual({r["ticker"] for r in rows}, {"VAULT1"})

        history = splits.vault_access_history()
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertEqual(entry["reason"], "final eval of candidate X, first look")
        self.assertEqual(entry["dataset"], "markets")
        self.assertEqual(entry["rows_returned"], 1)
        self.assertTrue(entry["caller"].startswith("test_splits.py:"),
                        f"caller should point at the asking code, got {entry['caller']!r}")
        self.assertGreater(entry["ts"], 0)

    def test_every_vault_access_is_logged_separately(self):
        """Two looks is exactly the situation the log exists to make
        visible — it must not dedupe or overwrite."""
        for i in range(2):
            splits.load("markets", split=Split.VAULT, allow_vault=True,
                         reason=f"look {i}")
        self.assertEqual([h["reason"] for h in splits.vault_access_history()],
                         ["look 0", "look 1"])

    def test_allow_vault_on_a_non_vault_split_is_an_error_not_a_no_op(self):
        """Guards against allow_vault=True being left switched on in a
        wrapper: it must never sit there harmlessly, waiting to matter."""
        with self.assertRaises(ValueError):
            splits.load("markets", split=Split.TRAIN, allow_vault=True, reason="x")

    def test_price_points_are_scoped_by_their_market_close_time(self):
        """Market-keyed tables must be split by the MARKET's close_time,
        not by the price point's own ts — otherwise a vault market's price
        history leaks in through a table that has no close_time of its own."""
        rows = splits.load("price_points")
        self.assertEqual({r["ticker"] for r in rows}, {"TRAIN1"})
        with self.assertRaises(VaultAccessError):
            splits.load("price_points", split=Split.VAULT)

    def test_weather_points_are_scoped_by_their_own_timestamp(self):
        import calendar
        may = calendar.timegm((2026, 5, 1, 0, 0, 0, 0, 0, 0))     # inside vault
        june_2025 = calendar.timegm((2025, 6, 1, 0, 0, 0, 0, 0, 0))  # inside train
        storage.save_historical_weather_points("KAUS", [(may, 80.0, 10.0, 79.0, 0.0)])
        storage.save_historical_weather_points("KAUS", [(june_2025, 70.0, 5.0, 71.0, 0.0)])

        train_ts = {r["ts"] for r in splits.load("weather_points")}
        self.assertEqual(train_ts, {june_2025})
        with self.assertRaises(VaultAccessError):
            splits.load("weather_points", split=Split.VAULT)

    def test_future_rows_are_in_no_split_and_must_be_asked_for_explicitly(self):
        """Live-captured markets aren't silently folded into TRAIN."""
        for split in (Split.TRAIN, Split.DEV, Split.TRAIN_DEV):
            self.assertNotIn("FUTURE1", {r["ticker"] for r in splits.load("markets", split=split)})
        self.assertEqual({r["ticker"] for r in splits.load("markets", split=Split.FUTURE)},
                          {"FUTURE1"})

    def test_an_unknown_dataset_is_refused_rather_than_silently_empty(self):
        with self.assertRaises(ValueError):
            splits.load("shadow_trades")

    def test_the_boundaries_are_contiguous_and_non_overlapping(self):
        """The three ranges must tile the timeline with no gap and no
        overlap — a gap silently drops markets from every split, an
        overlap puts vault rows into training."""
        self.assertEqual(splits.TRAIN_END, splits.DEV_START)
        self.assertEqual(splits.DEV_END, splits.VAULT_START)
        self.assertLess(splits.VAULT_START, splits.VAULT_END)


if __name__ == "__main__":
    unittest.main()
