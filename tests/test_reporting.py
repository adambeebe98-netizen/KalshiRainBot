"""
Covers reporting.py's build_export_text() — the single function that feeds
both the dashboard's /export page and advisor.py's weekly review, so
whatever's tested here is exactly what advisor.py bases its suggestions on.
"""
from __future__ import annotations

import unittest

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402
import reporting  # noqa: E402


class TestBuildExportText(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "trades", "shadow_bankroll_snapshots", "calibration_stats")

    def test_runs_cleanly_with_no_data_at_all(self):
        """Every section wraps its own query in a try/except — a brand
        new install with empty tables should never crash the export."""
        text = reporting.build_export_text({})
        self.assertIn("EXPORT generated at", text)
        self.assertIn("STRATEGY COMPARISON", text)
        self.assertIn("DOES CONFIDENCE/EDGE PREDICT OUTCOMES", text)

    def test_includes_real_confidence_and_edge_data(self):
        tid1 = storage.log_shadow_trade("calibrated_balanced", "T1", "yes", 10, 40,
                                          model_probability=0.7, confidence="high")
        storage.settle_shadow_trade(tid1, won=True, pnl_cents=600)
        tid2 = storage.log_shadow_trade("calibrated_balanced", "T2", "yes", 10, 40,
                                          model_probability=0.45, confidence="medium")
        storage.settle_shadow_trade(tid2, won=False, pnl_cents=-400)

        text = reporting.build_export_text({})
        self.assertIn("confidence=high", text)
        self.assertIn("confidence=medium", text)
        self.assertIn("win_rate=100%", text)
        self.assertIn("win_rate=0%", text)

    def test_includes_real_strategy_summary_data(self):
        storage.log_shadow_trade("swing", "T3", "yes", 10, 40)
        storage.snapshot_shadow_bankroll("swing", 51000)
        text = reporting.build_export_text({})
        self.assertIn("swing", text)

    def test_includes_calibration_data(self):
        import calibration
        for _ in range(25):
            calibration.record_outcome("KAUS", "precipitation_daily", 0.7, True)
        text = reporting.build_export_text({})
        self.assertIn("KAUS", text)
        self.assertIn("precipitation_daily", text)

    def test_env_dict_values_are_reflected(self):
        text = reporting.build_export_text({"LIVE_TRADING": "false", "RISK_MODE": "conservative"})
        self.assertIn("Live trading: false", text)
        self.assertIn("Risk mode: conservative", text)

    def test_a_broken_section_does_not_take_down_the_whole_export(self):
        """Each section is independently wrapped -- confirm the overall
        function still returns a complete, readable document even if one
        section's underlying data is malformed enough to raise."""
        with storage.get_conn() as conn:
            conn.execute("DROP TABLE calibration_stats")
            conn.commit()
        text = reporting.build_export_text({})
        self.assertIn("calibration unavailable", text)
        self.assertIn("STRATEGY COMPARISON", text)  # other sections still ran fine


if __name__ == "__main__":
    unittest.main()
