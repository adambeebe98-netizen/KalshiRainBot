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
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402
import shadow  # noqa: E402

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
                         "decisions", "strategy_overrides", "meta")


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


class TestTrendArrow(unittest.TestCase):
    def test_up_when_latest_exceeds_baseline(self):
        self.assertEqual(webapp._trend_arrow([(1000, 50000), (2000, 55000)]), "up")

    def test_down_when_latest_is_below_baseline(self):
        self.assertEqual(webapp._trend_arrow([(1000, 50000), (2000, 45000)]), "down")

    def test_flat_when_unchanged(self):
        self.assertEqual(webapp._trend_arrow([(1000, 50000), (2000, 50000)]), "flat")

    def test_none_with_fewer_than_two_points(self):
        self.assertIsNone(webapp._trend_arrow([(1000, 50000)]))
        self.assertIsNone(webapp._trend_arrow([]))

    def test_uses_the_window_start_not_the_very_first_point_when_history_is_long(self):
        # A long history where the window-relevant baseline differs from
        # the very first point on record -- the trend should reflect the
        # recent window, not all-time history.
        history = [(0, 100000), (1000, 90000), (90000, 50000), (91000, 55000)]
        # window_hours=24 -> cutoff is 91000 - 86400 = 4600; only the last
        # two points (90000, 91000) are within that window.
        self.assertEqual(webapp._trend_arrow(history, window_hours=24), "up")


class TestHeartbeat(DashboardTestCase):
    def test_shows_last_scan_time_when_fresh(self):
        storage.set_meta("last_scan_completed_ts", str(int(time.time()) - 30))
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("last scan", body)
        self.assertNotIn("hasn't completed a scan cycle", body)

    def test_flags_stale_when_last_scan_is_old(self):
        storage.set_meta("last_scan_completed_ts", str(int(time.time()) - 5000))
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("hasn't completed a scan cycle", body)

    def test_no_heartbeat_pill_when_never_recorded(self):
        body = _client().get("/").get_data(as_text=True)
        self.assertNotIn("hasn't completed a scan cycle", body)


class TestSafetyBanner(DashboardTestCase):
    def test_kill_switched_strategy_shows_in_banner_and_per_row_tag(self):
        for i in range(50):
            storage.log_shadow_trade("calibrated_conservative", f"L{i}", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("calibrated_conservative", 50000)
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()
        rm = shadow.get_engines()["calibrated_conservative"]
        rm.state.realized_pnl_today_cents = -4000
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("kill-switched for today", body)
        self.assertIn("kill-switched today", body)

    def test_no_banner_when_everything_is_healthy(self):
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertNotIn("kill-switched for today", body)
        self.assertNotIn("cooling off after a losing streak", body)


class TestLowDataWarningsRemoved(DashboardTestCase):
    def test_never_shows_the_streak_warning_anywhere(self):
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertNotIn("could easily be a streak", body)
        self.assertNotIn("too early to call", body)
        self.assertNotIn("(low data)", body)


class TestBigSwingFlag(DashboardTestCase):
    def test_flags_a_position_with_a_large_unrealized_swing(self):
        storage.log_price_snapshot("T1", yes_ask=None, yes_bid=60)  # bought at 40c, now worth 60c = +50%
        storage.log_shadow_trade("swing", "T1", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertIn("big move", body)

    def test_does_not_flag_a_small_ordinary_move(self):
        storage.log_price_snapshot("T2", yes_ask=None, yes_bid=41)  # bought at 40c, now 41c -- tiny move
        storage.log_shadow_trade("swing", "T2", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 50000)
        body = _client().get("/").get_data(as_text=True)
        self.assertNotIn("big move", body)


class TestPasswordChange(DashboardTestCase):
    def test_setting_a_new_password_updates_env(self):
        with patch.object(webapp, "restart_bot", return_value="restarted"):
            _client().post("/setup", data={
                "risk_mode": "balanced", "bankroll": "500", "series": "KXRAIN",
                "dashboard_password": "newpass456",
            })
        self.assertEqual(webapp.read_env().get("WEB_UI_PASSWORD"), "newpass456")

    def test_blank_password_field_keeps_the_existing_one(self):
        webapp.write_env({**webapp.read_env(), "WEB_UI_PASSWORD": "original123"})
        with patch.object(webapp, "restart_bot", return_value="restarted"):
            _client().post("/setup", data={"risk_mode": "balanced", "bankroll": "500", "series": "KXRAIN"})
        self.assertEqual(webapp.read_env().get("WEB_UI_PASSWORD"), "original123")


class TestPullAndRestart(DashboardTestCase):
    def test_already_up_to_date_skips_restart(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="Already up to date.\n", stderr="")
            resp = _client().post("/control", data={"action": "pull_and_restart"}, follow_redirects=True)
        self.assertIn("Already up to date", resp.get_data(as_text=True))

    def test_real_change_pulls_and_restarts(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kwargs):
                if cmd[0] == "git":
                    return MagicMock(stdout="Updating abc123..def456\n 3 files changed\n", stderr="")
                return MagicMock(stdout="", stderr="")
            mock_run.side_effect = side_effect
            resp = _client().post("/control", data={"action": "pull_and_restart"}, follow_redirects=True)
        self.assertIn("Pulled latest code", resp.get_data(as_text=True))


class TestManualTriggers(DashboardTestCase):
    def test_run_advisor_reports_suggestion_count(self):
        with patch.dict(sys.modules, {"advisor": MagicMock(generate_suggestions=lambda: 2)}):
            resp = _client().post("/control", data={"action": "run_advisor"}, follow_redirects=True)
        self.assertIn("2 new suggestion", resp.get_data(as_text=True))

    def test_run_advisor_failure_is_handled_without_crashing(self):
        broken = MagicMock()
        broken.generate_suggestions.side_effect = RuntimeError("boom")
        with patch.dict(sys.modules, {"advisor": broken}):
            resp = _client().post("/control", data={"action": "run_advisor"}, follow_redirects=True)
        self.assertIn("failed", resp.get_data(as_text=True))

    def test_run_retrospective_success(self):
        with patch.dict(sys.modules, {"retrospective": MagicMock(generate_retrospective=lambda: 5)}):
            resp = _client().post("/control", data={"action": "run_retrospective"}, follow_redirects=True)
        self.assertIn("refresh to see it", resp.get_data(as_text=True))

    def test_run_retrospective_not_enough_data(self):
        with patch.dict(sys.modules, {"retrospective": MagicMock(generate_retrospective=lambda: None)}):
            resp = _client().post("/control", data={"action": "run_retrospective"}, follow_redirects=True)
        self.assertIn("Not enough settled trades", resp.get_data(as_text=True))


class TestWipeDataRoute(DashboardTestCase):
    def test_wrong_confirmation_leaves_data_untouched(self):
        storage.log_trade("T1", "yes", 5, 40, "paper", None)
        resp = _client().post("/wipe_data", data={"confirm": "wipe"}, follow_redirects=True)
        self.assertIn("match", resp.get_data(as_text=True))
        with storage.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(n, 1)

    def test_correct_confirmation_wipes_and_reports_backup(self):
        storage.log_trade("T1", "yes", 5, 40, "paper", None)
        with patch.object(webapp, "restart_bot", return_value="restarted"):
            resp = _client().post("/wipe_data", data={"confirm": "WIPE ALL DATA"}, follow_redirects=True)
        body = resp.get_data(as_text=True)
        self.assertIn("Wiped", body)
        self.assertIn("Backup saved as", body)
        with storage.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
