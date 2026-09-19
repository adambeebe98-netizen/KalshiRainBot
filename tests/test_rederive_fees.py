"""
Covers rederive_fees.py — the one-time backfill that charges real Kalshi
fees against trades that settled before settlement.py/shadow.py started
deducting them, and rebuilds the bankroll snapshots the leaderboard and
every restart read from.

The two things that actually matter here: it must not double-charge on a
second run (the whole reason fees_applied_to_pnl exists), and it must
leave still-open positions completely alone — their fee gets charged when
they settle, by the live settlement path.
"""
from __future__ import annotations

import unittest

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import fees  # noqa: E402
import storage  # noqa: E402
import rederive_fees  # noqa: E402
from config import SETTINGS  # noqa: E402


class TestRederiveFees(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "trades", "shadow_bankroll_snapshots",
                         "bankroll_snapshots")

    def _settle_gross(self, trade_id, pnl_cents, status="won"):
        """Writes a settled row the OLD way — gross P&L, no fee applied —
        which is exactly the state of every row this script exists for."""
        with storage.get_conn() as conn:
            conn.execute("UPDATE shadow_trades SET status=?, settled_ts=1, pnl_cents=? WHERE id=?",
                          (status, pnl_cents, trade_id))

    def test_settled_rows_lose_the_fee_and_open_rows_are_untouched(self):
        settled = storage.log_shadow_trade("calibrated_balanced", "T1", "yes", 10, 40,
                                            fee_cents_paid=17)
        self._settle_gross(settled, (100 - 40) * 10)
        still_open = storage.log_shadow_trade("calibrated_balanced", "T2", "yes", 10, 40,
                                               fee_cents_paid=17)

        rederive_fees.rederive(apply=True)

        with storage.get_conn() as conn:
            done = conn.execute("SELECT pnl_cents, fees_applied_to_pnl FROM shadow_trades "
                                 "WHERE id=?", (settled,)).fetchone()
            untouched = conn.execute("SELECT status, pnl_cents, fees_applied_to_pnl FROM "
                                      "shadow_trades WHERE id=?", (still_open,)).fetchone()
        self.assertEqual(done[0], 600 - 17)
        self.assertEqual(done[1], 1)
        self.assertEqual(untouched[0], "open")
        self.assertIsNone(untouched[1])
        self.assertIn(untouched[2], (None, 0))

    def test_running_it_twice_does_not_charge_twice(self):
        settled = storage.log_shadow_trade("calibrated_balanced", "T3", "yes", 10, 40,
                                            fee_cents_paid=17)
        self._settle_gross(settled, 600)

        rederive_fees.rederive(apply=True)
        rederive_fees.rederive(apply=True)

        with storage.get_conn() as conn:
            pnl = conn.execute("SELECT pnl_cents FROM shadow_trades WHERE id=?",
                                (settled,)).fetchone()[0]
        self.assertEqual(pnl, 600 - 17, "second run must be a no-op, not a second fee")

    def test_a_row_with_no_recorded_fee_gets_the_estimate_written_back(self):
        settled = storage.log_shadow_trade("calibrated_balanced", "T4", "yes", 10, 40)
        self._settle_gross(settled, 600)

        rederive_fees.rederive(apply=True)

        expected = fees.taker_fee_cents(10, 40)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT pnl_cents, fee_cents_paid FROM shadow_trades WHERE id=?",
                                (settled,)).fetchone()
        self.assertEqual(row[1], expected)
        self.assertEqual(row[0], 600 - expected)

    def test_a_sold_row_is_charged_for_both_of_its_orders(self):
        """A 'sold' row was closed by a second real order. The exit price
        isn't stored, but it's exactly recoverable from the gross P&L that
        is (entry + pnl/count), so both fees can be charged correctly."""
        sold = storage.log_shadow_trade("swing", "T5", "yes", 10, 40, fee_cents_paid=17)
        self._settle_gross(sold, (55 - 40) * 10, status="sold")  # sold at 55c

        rederive_fees.rederive(apply=True)

        expected_fee = 17 + fees.taker_fee_cents(10, 55)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT pnl_cents, fee_cents_paid FROM shadow_trades WHERE id=?",
                                (sold,)).fetchone()
        self.assertEqual(row[1], expected_fee)
        self.assertEqual(row[0], 150 - expected_fee)

    def test_the_bankroll_snapshot_is_rebuilt_from_the_corrected_pnl(self):
        settled = storage.log_shadow_trade("calibrated_balanced", "T6", "yes", 10, 40,
                                            fee_cents_paid=17)
        self._settle_gross(settled, 600)
        storage.snapshot_shadow_bankroll("calibrated_balanced",
                                          SETTINGS.starting_bankroll_cents + 600)  # the stale, gross figure

        rederive_fees.rederive(apply=True)

        latest = storage.load_last_shadow_bankroll("calibrated_balanced", 0)
        self.assertEqual(latest, SETTINGS.starting_bankroll_cents + 600 - 17)

    def test_a_dry_run_writes_nothing(self):
        settled = storage.log_shadow_trade("calibrated_balanced", "T7", "yes", 10, 40,
                                            fee_cents_paid=17)
        self._settle_gross(settled, 600)

        rederive_fees.rederive(apply=False)

        with storage.get_conn() as conn:
            pnl = conn.execute("SELECT pnl_cents FROM shadow_trades WHERE id=?",
                                (settled,)).fetchone()[0]
        self.assertEqual(pnl, 600)


if __name__ == "__main__":
    unittest.main()
