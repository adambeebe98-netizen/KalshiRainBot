"""
Covers rules_extractor.py's caching and parsing logic, including a real
gap found by audit: the try/except around Claude's JSON response only
caught json.JSONDecodeError/ValueError (malformed JSON SYNTAX), not
TypeError from constructing MarketRules with a validly-parsed but
wrong-shaped object (an unexpected extra key, or a missing required
field) — that used to propagate straight up uncaught through extract(),
and bot.py's call site had no guard of its own either, so a single odd
LLM response could silently skip evaluating every OTHER market in that
series for the rest of that scan cycle, not just the one bad ticker.

The Anthropic API call itself is mocked throughout.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from rules_extractor import RulesExtractor, MarketRules, CACHE_SCHEMA_VERSION


def fake_response(text: str):
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


VALID_EXTRACTION = {
    "station_code": "KAUS", "settlement_source": "NWS", "measure": "precipitation_daily",
    "threshold_description": "strictly greater than 0 inches",
    "threshold_low_f": None, "threshold_high_f": None,
    "trace_counts_as_zero": True, "fallback_rule": None, "confidence": "high",
}


class RulesExtractorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_cache = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp_cache.close()
        os.unlink(self.tmp_cache.name)  # extractor should handle a non-existent cache file fine
        with patch("anthropic.Anthropic"):
            self.extractor = RulesExtractor(cache_path=self.tmp_cache.name, api_key="test-key")

    def tearDown(self):
        if os.path.exists(self.tmp_cache.name):
            os.unlink(self.tmp_cache.name)

    def _set_response(self, text: str):
        self.extractor._client.messages.create = MagicMock(return_value=fake_response(text))


class TestCaching(RulesExtractorTestCase):
    def test_missing_cache_file_starts_empty_without_crashing(self):
        self.assertEqual(self.extractor._cache, {})

    def test_successful_extraction_is_cached(self):
        self._set_response(json.dumps(VALID_EXTRACTION))
        self.extractor.extract("T1", "some rules text")
        self.assertIn("T1", self.extractor._cache)
        self.assertEqual(self.extractor._cache["T1"]["_schema_version"], CACHE_SCHEMA_VERSION)

    def test_cached_result_does_not_call_the_api_again(self):
        self._set_response(json.dumps(VALID_EXTRACTION))
        self.extractor.extract("T2", "some rules text")
        self.extractor._client.messages.create.reset_mock()
        self.extractor.extract("T2", "some rules text")
        self.extractor._client.messages.create.assert_not_called()

    def test_force_true_bypasses_the_cache(self):
        self._set_response(json.dumps(VALID_EXTRACTION))
        self.extractor.extract("T3", "some rules text")
        self.extractor._client.messages.create.reset_mock()
        self.extractor.extract("T3", "some rules text", force=True)
        self.extractor._client.messages.create.assert_called_once()

    def test_old_schema_version_cache_entry_is_refreshed_not_trusted(self):
        self.extractor._cache["T4"] = {"_schema_version": CACHE_SCHEMA_VERSION - 1, **VALID_EXTRACTION,
                                        "ticker": "T4"}
        self._set_response(json.dumps(VALID_EXTRACTION))
        self.extractor.extract("T4", "some rules text")
        self.extractor._client.messages.create.assert_called_once()

    def test_cache_persists_across_a_new_extractor_instance(self):
        self._set_response(json.dumps(VALID_EXTRACTION))
        self.extractor.extract("T5", "some rules text")
        with patch("anthropic.Anthropic"):
            second_extractor = RulesExtractor(cache_path=self.tmp_cache.name, api_key="test-key")
        self.assertIn("T5", second_extractor._cache)


class TestEmptyRulesText(RulesExtractorTestCase):
    def test_empty_rules_text_returns_low_confidence_without_calling_the_api(self):
        result = self.extractor.extract("T6", "")
        self.assertEqual(result.confidence, "low")
        self.assertEqual(result.measure, "other")
        self.extractor._client.messages.create.assert_not_called()

    def test_whitespace_only_rules_text_is_treated_as_empty(self):
        result = self.extractor.extract("T7", "   \n\t  ")
        self.assertEqual(result.confidence, "low")
        self.extractor._client.messages.create.assert_not_called()


class TestResponseParsing(RulesExtractorTestCase):
    def test_plain_json_no_markdown_fence(self):
        self._set_response(json.dumps(VALID_EXTRACTION))
        result = self.extractor.extract("T8", "rules text")
        self.assertEqual(result.station_code, "KAUS")
        self.assertEqual(result.confidence, "high")

    def test_markdown_fenced_json_with_language_hint(self):
        payload = json.dumps(VALID_EXTRACTION)
        self._set_response(f"```json\n{payload}\n```")
        result = self.extractor.extract("T9", "rules text")
        self.assertEqual(result.station_code, "KAUS")

    def test_markdown_fenced_json_without_language_hint(self):
        payload = json.dumps(VALID_EXTRACTION)
        self._set_response(f"```\n{payload}\n```")
        result = self.extractor.extract("T10", "rules text")
        self.assertEqual(result.station_code, "KAUS")

    def test_malformed_json_falls_back_to_a_safe_low_confidence_result(self):
        self._set_response("this is not valid json at all {{{")
        result = self.extractor.extract("T11", "rules text")
        self.assertEqual(result.confidence, "low")
        self.assertEqual(result.measure, "other")
        self.assertIn("failed", result.threshold_description)

    def test_valid_json_with_an_unexpected_extra_field_falls_back_safely(self):
        """THE regression: this used to raise an uncaught TypeError from
        the MarketRules constructor, not get caught by the JSON-only
        except clause."""
        bad = dict(VALID_EXTRACTION)
        bad["some_field_the_model_made_up"] = "unexpected"
        self._set_response(json.dumps(bad))
        result = self.extractor.extract("T12", "rules text")
        self.assertEqual(result.confidence, "low")
        self.assertEqual(result.measure, "other")

    def test_valid_json_missing_a_required_field_falls_back_safely(self):
        """Another shape of the same regression: a required field (no
        dataclass default) simply absent from the model's response."""
        bad = dict(VALID_EXTRACTION)
        del bad["measure"]  # measure has no default in MarketRules
        self._set_response(json.dumps(bad))
        result = self.extractor.extract("T13", "rules text")
        self.assertEqual(result.confidence, "low")

    def test_valid_json_with_a_duplicate_ticker_key_falls_back_safely(self):
        bad = dict(VALID_EXTRACTION)
        bad["ticker"] = "SOMETHING-ELSE"  # would collide with extract()'s own ticker=ticker kwarg
        self._set_response(json.dumps(bad))
        result = self.extractor.extract("T14", "rules text")
        self.assertEqual(result.confidence, "low")

    def test_empty_response_content_falls_back_safely(self):
        resp = MagicMock()
        resp.content = []
        self.extractor._client.messages.create = MagicMock(return_value=resp)
        result = self.extractor.extract("T15", "rules text")
        self.assertEqual(result.confidence, "low")

    def test_a_failed_extraction_is_still_cached(self):
        """So a persistently-malformed market doesn't get re-tried (and
        re-billed) every single cycle forever."""
        self._set_response("not json")
        self.extractor.extract("T16", "rules text")
        self.assertIn("T16", self.extractor._cache)


class TestMarketRulesDefaults(unittest.TestCase):
    def test_threshold_fields_default_to_none(self):
        rules = MarketRules(ticker="X", station_code="KAUS", settlement_source="NWS",
                             measure="precipitation_daily", threshold_description="x",
                             trace_counts_as_zero=True, fallback_rule=None, confidence="high")
        self.assertIsNone(rules.threshold_low_f)
        self.assertIsNone(rules.threshold_high_f)


if __name__ == "__main__":
    unittest.main()
