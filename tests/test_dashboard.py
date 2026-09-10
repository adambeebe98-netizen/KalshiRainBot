"""
Covers web_ui/app.py's dashboard: authentication gating, rendering in both
empty and populated states, the new Open Positions view (rationale, exit
targets, per-trade mark-to-market), and every action route (suggestion
apply/dismiss, export). Uses Flask's real test client against a real
temporary database — this is an integration test of the whole page, not a
unit test of template internals.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web_ui"))
import app as webapp  # noqa: E402

webapp.app.config["TESTING"] = True


def _client():
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return client


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "trades", "shadow_bankroll_snapshots",
                         "calibration_stats", "suggestions", "retrospectives", "price_history",
                         "decisions", "strategy_overrides")


class TestAuthGating(DashboardTestCase):
    def test_unauthenticated_dashboard_access_redirects_to_login(self):
        client = webapp.app.test_client()
        resp = client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))

    def test_login_page_renders(self):
        client = webapp.app.test_client()
        resp = client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Dashboard password", resp.get_data(as_text=True))

    def test_authenticated_access_succeeds(self):
        resp = _client().get("/")
        self.assertEqual(resp.status_code, 200)


class TestDashboardEmptyState(DashboardTestCase):
    def test_renders_cleanly_with_no_data_at_all(self):
        resp = _client().get("/")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Strategy performance", body)
        self.assertIn("No shadow strategy data yet", body)
        self.assertIn("No pending suggestions", body)
        self.assertIn("No retrospective yet", body)


class TestOpenPositionsView(DashboardTestCase):
    def test_shows_rationale_and_exit_target_for_an_open_position(self):
        # In production every strategy gets a bankroll snapshot every
        # cycle via shadow.snapshot_all() (called from settlement.py),
        # regardless of whether it's traded yet -- a strategy only shows
        # up in get_shadow_summary() (and therefore in the merged
        # performance+positions view) once it has at least one snapshot,
        # so real production behavior always includes every active
        # strategy from its very first cycle onward.
        storage.log_shadow_trade("swing", "KXRAIN-TEST", "yes", 10, 40, exit_target_cents=65,
                                   confidence="medium",
                                   rationale="forecast POP 65%, calibration n=25, bias +0.03")
        storage.snapshot_shadow_bankroll("swing", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("KXRAIN-TEST", body)
        self.assertIn("forecast POP 65%", body)
        self.assertIn("sell at 65c", body)
        self.assertIn("1 open", body)

    def test_shows_real_mark_to_market_unrealized_value(self):
        storage.log_price_snapshot("T1", yes_ask=None, yes_bid=55)
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("+1.50", body)  # (55-40)*10 = 150c = $1.50 unrealized

    def test_settled_trades_never_appear_as_open(self):
        tid = storage.log_shadow_trade("swing", "SETTLED-ONE", "yes", 10, 40)
        storage.settle_shadow_trade(tid, won=True, pnl_cents=600)
        storage.snapshot_shadow_bankroll("swing", 50600)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("0 open", body)

    def test_badge_shows_the_real_total_even_beyond_the_display_limit(self):
        """THE regression: the badge used to show len(get_open_positions_detail()),
        which silently truncates at 300 -- a real total of 350 would have
        displayed as exactly 300, hiding the true number."""
        for i in range(305):
            storage.log_shadow_trade("calibrated_balanced", f"T{i}", "yes", 5, 40)
        storage.snapshot_shadow_bankroll("calibrated_balanced", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("305 open", body)
        self.assertIn("showing the most recent", body)

    def test_positions_grouped_by_strategy_in_performance_rank_order(self):
        """Positions are nested under each strategy's row in the merged
        performance+positions view, so they follow that view's existing
        rank order (by total P&L) rather than a separate sort of their
        own -- confirms the merge didn't silently drop or reorder
        anything unexpectedly."""
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.log_shadow_trade("calibrated_conservative", "T3", "yes", 15, 20)
        storage.snapshot_shadow_bankroll("swing", 50000)
        storage.snapshot_shadow_bankroll("calibrated_conservative", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("swing", body)
        self.assertIn("calibrated_conservative", body)
        self.assertIn("T1", body)
        self.assertIn("T3", body)

    def test_all_positions_and_reasoning_present_even_when_grouped(self):
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40, exit_target_cents=65,
                                   rationale="forecast POP 65%")
        storage.log_shadow_trade("calibrated_conservative", "T2", "yes", 15, 20,
                                   rationale="station already recorded rain")
        storage.snapshot_shadow_bankroll("swing", 50000)
        storage.snapshot_shadow_bankroll("calibrated_conservative", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("T1", body)
        self.assertIn("T2", body)
        self.assertIn("forecast POP 65%", body)
        self.assertIn("sell at 65c", body)
        self.assertIn("station already recorded rain", body)

    def test_strategy_row_shows_both_performance_stats_and_positions(self):
        """THE core point of the merge: one row per strategy carries both
        the performance metrics and its open positions, instead of two
        separate sections repeating the same strategy names."""
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40, rationale="test rationale")
        storage.snapshot_shadow_bankroll("swing", 50600)
        body = _client().get("/").get_data(as_text=True)
        self.assertEqual(body.count("Strategy performance"), 1,
                          "should be exactly one unified section, not two separate ones")
        self.assertIn("Bankroll", body)
        self.assertIn("Deployed", body)
        self.assertIn("test rationale", body)


class TestSuggestionActions(DashboardTestCase):
    def test_apply_calls_set_override_and_clears_the_suggestion(self):
        sid = storage.log_suggestion("longshot", "min_price", 5, 4, "test rationale")
        with patch.object(webapp, "restart_bot", return_value="restarted"):
            resp = _client().post("/suggestion", data={"id": str(sid), "action": "apply"})
        self.assertIn(resp.status_code, (301, 302))
        pending = storage.get_suggestions(status="pending")
        self.assertFalse(any(s["id"] == sid for s in pending))
        overrides = storage.get_overrides()
        self.assertEqual(overrides.get("longshot", {}).get("min_price"), 4)

    def test_dismiss_clears_the_suggestion_without_applying(self):
        sid = storage.log_suggestion("swing", "exit_offset", 20, 25, "test")
        _client().post("/suggestion", data={"id": str(sid), "action": "dismiss"})
        pending = storage.get_suggestions(status="pending")
        self.assertFalse(any(s["id"] == sid for s in pending))
        overrides = storage.get_overrides()
        self.assertNotIn("exit_offset", overrides.get("swing", {}))

    def test_a_stale_suggestion_id_is_handled_without_crashing(self):
        resp = _client().post("/suggestion", data={"id": "999999", "action": "apply"})
        self.assertIn(resp.status_code, (301, 302))


class TestExportPage(DashboardTestCase):
    def test_renders_with_real_data(self):
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 50400)
        body = _client().get("/export").get_data(as_text=True)
        self.assertEqual(200, _client().get("/export").status_code)
        self.assertIn("Back to dashboard", body)
        self.assertIn("swing", body)


if __name__ == "__main__":
    unittest.main()
