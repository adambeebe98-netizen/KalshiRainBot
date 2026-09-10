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
        self.assertIn("Open positions", body)
        self.assertIn("No open positions right now", body)
        self.assertIn("Strategy performance", body)
        self.assertIn("No pending suggestions", body)
        self.assertIn("No retrospective yet", body)


class TestOpenPositionsView(DashboardTestCase):
    def test_shows_rationale_and_exit_target_for_an_open_position(self):
        storage.log_shadow_trade("swing", "KXRAIN-TEST", "yes", 10, 40, exit_target_cents=65,
                                   confidence="medium",
                                   rationale="forecast POP 65%, calibration n=25, bias +0.03")
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("KXRAIN-TEST", body)
        self.assertIn("forecast POP 65%", body)
        self.assertIn("sell at 65c", body)
        self.assertIn("1 open", body)

    def test_shows_real_mark_to_market_unrealized_value(self):
        storage.log_price_snapshot("T1", yes_ask=None, yes_bid=55)
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("+1.50", body)  # (55-40)*10 = 150c = $1.50 unrealized

    def test_settled_trades_never_appear_as_open(self):
        tid = storage.log_shadow_trade("swing", "SETTLED-ONE", "yes", 10, 40)
        storage.settle_shadow_trade(tid, won=True, pnl_cents=600)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("0 open", body)

    def test_badge_shows_the_real_total_even_beyond_the_display_limit(self):
        """THE regression: the badge used to show len(get_open_positions_detail()),
        which silently truncates at 300 -- a real total of 350 would have
        displayed as exactly 300, hiding the true number."""
        for i in range(305):
            storage.log_shadow_trade("calibrated_balanced", f"T{i}", "yes", 5, 40)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("305 open", body)
        self.assertIn("Showing the most recent", body)


    def test_positions_grouped_by_strategy_with_busiest_first(self):
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.log_shadow_trade("swing", "T2", "yes", 5, 30)
        storage.log_shadow_trade("calibrated_conservative", "T3", "yes", 15, 20)
        body = _client().get("/").get_data(as_text=True)
        swing_pos = body.find("swing")
        calib_pos = body.find("calibrated_conservative")
        self.assertNotEqual(swing_pos, -1)
        self.assertNotEqual(calib_pos, -1)
        self.assertLess(swing_pos, calib_pos, "the strategy with more open positions should list first")

    def test_all_positions_and_reasoning_present_even_when_grouped(self):
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40, exit_target_cents=65,
                                   rationale="forecast POP 65%")
        storage.log_shadow_trade("calibrated_conservative", "T2", "yes", 15, 20,
                                   rationale="station already recorded rain")
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("T1", body)
        self.assertIn("T2", body)
        self.assertIn("forecast POP 65%", body)
        self.assertIn("sell at 65c", body)
        self.assertIn("station already recorded rain", body)


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
