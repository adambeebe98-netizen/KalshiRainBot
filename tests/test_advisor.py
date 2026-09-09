"""
Covers advisor.py's safety boundaries — this module reads real performance
data and asks Claude for numeric threshold suggestions, but critically
NEVER applies them directly (only storage.log_suggestion(), never
storage.set_override() — that's only reachable from a human clicking
Apply in the dashboard). Tests here focus on what happens to a
suggestion BEFORE a human ever sees it: the hard whitelist, the
hallucination guard (does the model's stated current_value match
reality), the numeric range check, and defensive parsing of a model
response that isn't clean JSON.

The Anthropic API call itself is mocked throughout — these tests are
about the validation logic around the response, not the LLM call.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch, MagicMock

from tests.helpers import use_temp_db, clear_tables

use_temp_db()

import storage  # noqa: E402
import shadow  # noqa: E402
import advisor  # noqa: E402


def fake_anthropic_response(text: str):
    """Mimics the shape advisor.py reads: response.content[i].text"""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestAdvisorSafetyBoundaries(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.get_conn() as conn:
            clear_tables(conn, "suggestions", "meta", "strategy_overrides")
        shadow._engines = None
        shadow.ACTIVE_STRATEGIES = shadow._load_active_strategies()

    def _run_with_response(self, text: str) -> int:
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_anthropic_response(text)
            mock_cls.return_value = mock_client
            return advisor.generate_suggestions()

    def test_never_calls_set_override_directly(self):
        """THE core safety property: advisor.py must physically be
        incapable of applying its own suggestions. The string
        "set_override" legitimately appears in the module's own docstring
        (explaining why it's avoided) — checking the whole module source
        would false-positive on that explanation, so this checks only the
        generate_suggestions() function body itself."""
        import inspect
        function_source = inspect.getsource(advisor.generate_suggestions)
        self.assertNotIn("set_override", function_source)

    def test_a_whitelisted_suggestion_is_logged(self):
        real_current = shadow.ACTIVE_STRATEGIES["swing"]["entry_max"]
        response = json.dumps([{
            "strategy": "swing", "param": "entry_max",
            "current_value": real_current, "suggested_value": 35,
            "rationale": "positions hit target with less movement than expected",
        }])
        logged = self._run_with_response(response)
        self.assertEqual(logged, 1)
        with storage.get_conn() as conn:
            row = conn.execute("SELECT strategy, param, suggested_value FROM suggestions").fetchone()
        self.assertEqual(row, ("swing", "entry_max", 35.0))

    def test_a_non_whitelisted_strategy_is_discarded(self):
        """calibrated_balanced is never in TUNABLE_PARAMS — a suggestion
        touching it must never be logged, regardless of what the model says."""
        response = json.dumps([{
            "strategy": "calibrated_balanced", "param": "min_edge_cents",
            "current_value": 6, "suggested_value": 2, "rationale": "seems fine",
        }])
        logged = self._run_with_response(response)
        self.assertEqual(logged, 0)

    def test_a_non_whitelisted_param_on_a_real_strategy_is_discarded(self):
        response = json.dumps([{
            "strategy": "swing", "param": "risk",  # real strategy, but "risk" isn't tunable
            "current_value": "balanced", "suggested_value": "aggressive", "rationale": "x",
        }])
        logged = self._run_with_response(response)
        self.assertEqual(logged, 0)

    def test_bracket_arbitrage_and_arbitrage_are_never_whitelisted(self):
        """Explicit regression guard for the exact failure mode the
        module's own docstring warns about."""
        self.assertNotIn("bracket_arbitrage", shadow.TUNABLE_PARAMS)
        self.assertNotIn("arbitrage", shadow.TUNABLE_PARAMS)
        self.assertNotIn("calibrated_conservative", shadow.TUNABLE_PARAMS)
        self.assertNotIn("calibrated_balanced", shadow.TUNABLE_PARAMS)
        self.assertNotIn("calibrated_aggressive", shadow.TUNABLE_PARAMS)

    def test_mismatched_current_value_is_discarded_as_likely_hallucinated(self):
        """THE hallucination guard: the model was handed the real current
        value in its prompt — if it states something different, that's a
        strong signal it misread the data."""
        real_current = shadow.ACTIVE_STRATEGIES["swing"]["entry_max"]
        response = json.dumps([{
            "strategy": "swing", "param": "entry_max",
            "current_value": real_current + 999,  # deliberately wrong
            "suggested_value": 35, "rationale": "x",
        }])
        logged = self._run_with_response(response)
        self.assertEqual(logged, 0)

    def test_out_of_range_suggested_value_is_discarded(self):
        """Every current tunable param is a cents-denominated price
        (0-100c) — a suggestion way outside that range should never reach
        a human's screen as if it were a real, applyable option."""
        real_current = shadow.ACTIVE_STRATEGIES["swing"]["entry_max"]
        response = json.dumps([{
            "strategy": "swing", "param": "entry_max",
            "current_value": real_current, "suggested_value": 99999,
            "rationale": "x",
        }])
        logged = self._run_with_response(response)
        self.assertEqual(logged, 0)

    def test_negative_suggested_value_is_discarded(self):
        real_current = shadow.ACTIVE_STRATEGIES["swing"]["entry_max"]
        response = json.dumps([{
            "strategy": "swing", "param": "entry_max",
            "current_value": real_current, "suggested_value": -10,
            "rationale": "x",
        }])
        logged = self._run_with_response(response)
        self.assertEqual(logged, 0)

    def test_empty_array_response_is_handled_cleanly(self):
        logged = self._run_with_response("[]")
        self.assertEqual(logged, 0)

    def test_markdown_fenced_json_is_parsed_correctly(self):
        real_current = shadow.ACTIVE_STRATEGIES["longshot"]["min_price"]
        payload = json.dumps([{
            "strategy": "longshot", "param": "min_price",
            "current_value": real_current, "suggested_value": 3,
            "rationale": "x",
        }])
        response = f"```json\n{payload}\n```"
        logged = self._run_with_response(response)
        self.assertEqual(logged, 1)

    def test_malformed_json_is_discarded_without_crashing(self):
        logged = self._run_with_response("this is not json at all {{{")
        self.assertEqual(logged, 0)

    def test_a_non_array_json_response_is_discarded(self):
        logged = self._run_with_response(json.dumps({"strategy": "swing"}))
        self.assertEqual(logged, 0)

    def test_a_non_dict_item_in_the_array_is_skipped_not_crashed(self):
        response = json.dumps(["just a string", 123, None])
        logged = self._run_with_response(response)
        self.assertEqual(logged, 0)

    def test_missing_required_fields_are_skipped(self):
        response = json.dumps([{"strategy": "swing", "param": "entry_max"}])  # no values at all
        logged = self._run_with_response(response)
        self.assertEqual(logged, 0)

    def test_records_the_last_run_timestamp_even_with_zero_suggestions(self):
        self._run_with_response("[]")
        with storage.get_conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='last_advisor_run_ts'").fetchone()
        self.assertIsNotNone(row)

    def test_multiple_valid_suggestions_all_get_logged(self):
        swing_current = shadow.ACTIVE_STRATEGIES["swing"]["entry_max"]
        longshot_current = shadow.ACTIVE_STRATEGIES["longshot"]["min_price"]
        response = json.dumps([
            {"strategy": "swing", "param": "entry_max", "current_value": swing_current,
             "suggested_value": 35, "rationale": "a"},
            {"strategy": "longshot", "param": "min_price", "current_value": longshot_current,
             "suggested_value": 4, "rationale": "b"},
        ])
        logged = self._run_with_response(response)
        self.assertEqual(logged, 2)


if __name__ == "__main__":
    unittest.main()
