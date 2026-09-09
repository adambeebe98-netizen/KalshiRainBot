"""
Covers retrospective.py's safety boundary (prose-only output, no
structured suggestion that anything could apply automatically) and its
data-gathering/formatting logic. The Anthropic API call is mocked
throughout.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402
import retrospective  # noqa: E402


def fake_response(text: str):
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestRetrospectiveSafetyBoundary(unittest.TestCase):
    def test_never_calls_set_override_or_apply_directly(self):
        """THE core safety property, stronger even than advisor.py's: this
        module has no structured suggestion path at all, just prose."""
        import inspect
        source = inspect.getsource(retrospective)
        self.assertNotIn("set_override", source)
        self.assertNotIn("apply_suggestion", source)

    def test_only_ever_writes_via_log_retrospective(self):
        import inspect
        source = inspect.getsource(retrospective.generate_retrospective)
        # The only storage write in the whole function should be
        # log_retrospective (plus set_meta for the run timestamp, which
        # doesn't touch trading behavior either).
        self.assertIn("storage.log_retrospective(", source)
        self.assertNotIn("log_shadow_trade", source)
        self.assertNotIn("set_override", source)


class TestGenerateRetrospective(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "shadow_trades", "retrospectives", "meta")

    def _seed_trades(self, n_won: int, n_lost: int):
        for i in range(n_won):
            tid = storage.log_shadow_trade("calibrated_balanced", f"WIN{i}", "yes", 10, 40,
                                             model_probability=0.7, station_code="KAUS",
                                             measure="precipitation_daily",
                                             rationale=f"station already recorded rain, win {i}")
            storage.settle_shadow_trade(tid, won=True, pnl_cents=600)
        for i in range(n_lost):
            tid = storage.log_shadow_trade("calibrated_balanced", f"LOSS{i}", "yes", 10, 40,
                                             model_probability=0.7, station_code="KAUS",
                                             measure="precipitation_daily",
                                             rationale=f"forecast only, no observation, loss {i}")
            storage.settle_shadow_trade(tid, won=False, pnl_cents=-400)

    def test_skips_with_too_few_settled_trades(self):
        self._seed_trades(n_won=3, n_lost=2)  # below MIN_TRADES_FOR_RETROSPECTIVE
        with patch("anthropic.Anthropic") as mock_cls:
            result = retrospective.generate_retrospective()
        self.assertIsNone(result)
        mock_cls.assert_not_called()

    def test_generates_and_logs_a_retrospective_with_enough_data(self):
        self._seed_trades(n_won=15, n_lost=10)
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response(
                "Losses cluster around forecast-only signals with no observation confirmation."
            )
            mock_cls.return_value = mock_client
            result = retrospective.generate_retrospective()
        self.assertIsNotNone(result)
        recent = storage.get_recent_retrospectives()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["trades_analyzed"], 25)
        self.assertEqual(recent[0]["wins_analyzed"], 15)
        self.assertEqual(recent[0]["losses_analyzed"], 10)
        self.assertIn("forecast-only", recent[0]["analysis_text"])

    def test_records_the_last_run_timestamp(self):
        self._seed_trades(n_won=15, n_lost=10)
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response("some analysis")
            mock_cls.return_value = mock_client
            retrospective.generate_retrospective()
        with storage.get_conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='last_retrospective_run_ts'").fetchone()
        self.assertIsNotNone(row)

    def test_empty_response_is_discarded_without_crashing(self):
        self._seed_trades(n_won=15, n_lost=10)
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response("")
            mock_cls.return_value = mock_client
            result = retrospective.generate_retrospective()
        self.assertIsNone(result)
        self.assertEqual(len(storage.get_recent_retrospectives()), 0)

    def test_api_failure_is_handled_gracefully(self):
        self._seed_trades(n_won=15, n_lost=10)
        with patch("anthropic.Anthropic") as mock_cls:
            mock_cls.side_effect = RuntimeError("simulated API outage")
            result = retrospective.generate_retrospective()
        self.assertIsNone(result)

    def test_open_unsettled_trades_are_never_included_in_the_review(self):
        self._seed_trades(n_won=15, n_lost=10)
        storage.log_shadow_trade("calibrated_balanced", "STILL-OPEN", "yes", 10, 40)
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()

            def capture_prompt(*args, **kwargs):
                capture_prompt.prompt = kwargs["messages"][0]["content"]
                return fake_response("analysis")
            mock_client.messages.create.side_effect = capture_prompt
            mock_cls.return_value = mock_client
            retrospective.generate_retrospective()
        self.assertNotIn("STILL-OPEN", capture_prompt.prompt)


class TestFormatTrade(unittest.TestCase):
    def test_includes_the_key_fields_a_reviewer_would_need(self):
        trade = {
            "strategy": "calibrated_balanced", "ticker": "T1", "side": "yes", "count": 10,
            "price_cents": 40, "status": "lost", "pnl_cents": -400, "model_probability": 0.7,
            "measure": "precipitation_daily", "station_code": "KAUS",
            "rationale": "forecast only, no observation data",
        }
        formatted = retrospective._format_trade(trade)
        self.assertIn("calibrated_balanced", formatted)
        self.assertIn("T1", formatted)
        self.assertIn("LOST", formatted)
        self.assertIn("-400c", formatted)
        self.assertIn("forecast only", formatted)

    def test_handles_missing_rationale_gracefully(self):
        trade = {
            "strategy": "arbitrage", "ticker": "T2", "side": "both", "count": 5,
            "price_cents": 90, "status": "won", "pnl_cents": 50, "model_probability": None,
            "measure": None, "station_code": None, "rationale": None,
        }
        formatted = retrospective._format_trade(trade)
        self.assertIn("no rationale recorded", formatted)
        self.assertIn("n/a", formatted)  # model_probability


if __name__ == "__main__":
    unittest.main()
