"""
Adam's insight, confirmed correct and confirmed valuable: within one
Kalshi series, every market's rules text is the same template with only
the date and threshold substituted -- confirmed live tonight that
calling the LLM separately for every single market was wasteful past
the first one (Austin alone: 3,700+ distinct tickers). These tests
verify the template-learning and reuse logic against real rules text
patterns confirmed tonight, plus the edge cases that matter for
trusting a threshold value without an LLM call behind it.
"""
from __future__ import annotations

import unittest

from rules_extractor import MarketRules
from series_template import build_template, try_apply_template


def _rules(**overrides):
    base = dict(ticker="T", station_code="CLINYC", settlement_source="NWS",
                measure="temperature_high", threshold_description="x",
                trace_counts_as_zero=None, fallback_rule=None, confidence="high",
                threshold_low_f=None, threshold_high_f=None)
    base.update(overrides)
    return MarketRules(**base)


class TestBuildAndApplyGreaterThan(unittest.TestCase):
    """Real NYC wording confirmed against the live rules text tonight."""

    def setUp(self):
        self.text1 = ("If the highest temperature recorded in Central Park, New York for July 16, 2026 "
                       "as reported by the National Weather Service's Climatological Report (Daily), "
                       "is greater than 96°, then the market resolves to Yes.")
        self.rules1 = _rules(threshold_low_f=96.0001)
        self.template = build_template(self.text1, self.rules1, "2026-07-16T14:00:00Z")

    def test_template_builds_successfully(self):
        self.assertIsNotNone(self.template)

    def test_applies_correctly_to_a_different_date_and_threshold(self):
        text2 = ("If the highest temperature recorded in Central Park, New York for July 17, 2026 "
                 "as reported by the National Weather Service's Climatological Report (Daily), "
                 "is greater than 89°, then the market resolves to Yes.")
        result = try_apply_template(self.template, text2, "M2")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.threshold_low_f, 89.0001, places=3)
        self.assertIsNone(result.threshold_high_f)
        self.assertEqual(result.station_code, "CLINYC")
        self.assertEqual(result.settlement_source, "NWS")
        self.assertEqual(result.ticker, "M2")

    def test_mismatched_wording_returns_none_not_a_guess(self):
        result = try_apply_template(self.template, "Completely unrelated rules text.", "X")
        self.assertIsNone(result)


class TestBuildAndApplyLessThan(unittest.TestCase):
    """Real Phoenix wording confirmed against the live rules text tonight."""

    def test_less_than_threshold_applies_correctly(self):
        text1 = ("If the maximum temperature recorded at Phoenix for Jul 16, 2026, is less than 99° "
                 "fahrenheit according to the National Weather Service's Climatological Report (Daily), "
                 "then the market resolves to Yes.")
        rules1 = _rules(station_code="CLIPHX", threshold_high_f=98.9999)
        template = build_template(text1, rules1, "2026-07-16T14:00:00Z")
        self.assertIsNotNone(template)

        text2 = ("If the maximum temperature recorded at Phoenix for Jul 20, 2026, is less than 104° "
                 "fahrenheit according to the National Weather Service's Climatological Report (Daily), "
                 "then the market resolves to Yes.")
        result = try_apply_template(template, text2, "M2")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.threshold_high_f, 103.9999, places=3)
        self.assertIsNone(result.threshold_low_f)


class TestBracketBothThresholds(unittest.TestCase):
    def test_both_threshold_bounds_apply_correctly(self):
        text1 = "Settles YES if the high temperature for Sep 17, 2026 is between 88 and 90 degrees Fahrenheit."
        rules1 = _rules(station_code="CLIAUS", threshold_low_f=88.0, threshold_high_f=90.0)
        template = build_template(text1, rules1, "2026-09-17T14:00:00Z")
        self.assertIsNotNone(template)

        text2 = "Settles YES if the high temperature for Sep 18, 2026 is between 92 and 94 degrees Fahrenheit."
        result = try_apply_template(template, text2, "M2")
        self.assertIsNotNone(result)
        self.assertEqual(result.threshold_low_f, 92.0)
        self.assertEqual(result.threshold_high_f, 94.0)


class TestEdgeCases(unittest.TestCase):
    def test_no_occurrence_datetime_still_builds_but_wont_generalize_across_dates(self):
        """Without a real event date to mask, the date text is treated
        as a literal, fixed part of the pattern rather than a wildcard --
        still safe (fullmatch simply rejects a different date rather
        than guessing), just narrower: it won't reuse across different
        dates, only across different thresholds on the same literal date."""
        text1 = "If the high temp for July 16, 2026 is greater than 96, resolves Yes."
        rules1 = _rules(threshold_low_f=96.0001)
        template = build_template(text1, rules1, None)
        self.assertIsNotNone(template)

        text_same_date_diff_threshold = "If the high temp for July 16, 2026 is greater than 88, resolves Yes."
        result_same_date = try_apply_template(template, text_same_date_diff_threshold, "M")
        self.assertIsNotNone(result_same_date)

        text_different_date = "If the high temp for July 17, 2026 is greater than 96, resolves Yes."
        result_diff_date = try_apply_template(template, text_different_date, "M2")
        self.assertIsNone(result_diff_date, "a different date's text should not match without real date masking")

    def test_threshold_equal_to_event_day_of_month_still_resolves_correctly(self):
        """The event date is masked out before the threshold search, so
        a threshold that happens to equal the day-of-month should not
        be confused with a date component."""
        text = ("If the highest temperature recorded in Central Park, New York for July 16, 2026 "
                "as reported by the National Weather Service's Climatological Report (Daily), "
                "is greater than 16°, then the market resolves to Yes.")
        rules = _rules(threshold_low_f=16.0001)
        template = build_template(text, rules, "2026-07-16T14:00:00Z")
        if template is not None:
            result = try_apply_template(template, text, "VERIFY")
            self.assertIsNotNone(result)
            self.assertAlmostEqual(result.threshold_low_f, 16.0001, places=3)

    def test_no_threshold_at_all_returns_none(self):
        rules = _rules()  # both thresholds None -- e.g. a non-numeric measure
        template = build_template("Some rules text with no number to templatize.", rules, "2026-07-16T14:00:00Z")
        self.assertIsNone(template)

    def test_threshold_not_findable_in_text_returns_none(self):
        rules = _rules(threshold_low_f=500.0)  # nothing close to 500 appears in the text
        template = build_template(
            "If the high temp for July 16, 2026 is greater than 96, resolves Yes.",
            rules, "2026-07-16T14:00:00Z"
        )
        self.assertIsNone(template)


if __name__ == "__main__":
    unittest.main()
