"""
Tests for the training-row builder, mostly about look-ahead.

A row that carries information from after its own timestamp trains a
model that appears to beat the market and cannot. It is invisible in
every offline metric -- accuracy, calibration, backtest P&L all look
better, not worse -- so the only cheap place to catch it is here.

The cases below are the specific ways it gets in: taking the nearest
point instead of the last one, interpolating across a gap, and filling
a pregame row with the opening estimate.
"""
from __future__ import annotations

import unittest

import dataset as ds


class Epoch(unittest.TestCase):

    def test_parses_iso_with_z(self):
        self.assertEqual(ds.to_epoch("1970-01-01T00:00:00Z"), 0)

    def test_passes_numbers_through(self):
        self.assertEqual(ds.to_epoch(1700000000), 1700000000)

    def test_bad_values_are_none_not_zero(self):
        for bad in (None, "", "   ", "not a date"):
            self.assertIsNone(ds.to_epoch(bad), repr(bad))


class CurveAsOf(unittest.TestCase):
    """The single most important behaviour in this module."""

    def setUp(self):
        self.curve = ds.ProbabilityCurve([
            (100, {"p_yes": 0.5}),
            (200, {"p_yes": 0.7}),
            (300, {"p_yes": 0.9}),
        ])

    def test_returns_the_last_point_at_or_before(self):
        self.assertEqual(self.curve.as_of(250)["p_yes"], 0.7)

    def test_a_point_exactly_at_the_instant_counts(self):
        # It was known at t, so it is allowed at t.
        self.assertEqual(self.curve.as_of(200)["p_yes"], 0.7)

    def test_never_reaches_forward_even_when_closer(self):
        """t=199 is one second from the 0.7 point and 99 from the 0.5.

        'Nearest' would take 0.7, which did not exist yet. This is the
        mistake that makes a backtest look brilliant.
        """
        self.assertEqual(self.curve.as_of(199)["p_yes"], 0.5)

    def test_before_the_curve_starts_is_none(self):
        self.assertIsNone(self.curve.as_of(99),
                          "pregame has no in-game estimate")

    def test_after_the_curve_ends_holds_the_last_point(self):
        self.assertEqual(self.curve.as_of(10_000)["p_yes"], 0.9)

    def test_empty_curve_is_none_everywhere(self):
        empty = ds.ProbabilityCurve([])
        self.assertIsNone(empty.as_of(0))
        self.assertIsNone(empty.as_of(999))
        self.assertIsNone(empty.span)

    def test_unsorted_input_is_sorted(self):
        curve = ds.ProbabilityCurve([(300, {"p_yes": 0.9}),
                                     (100, {"p_yes": 0.5})])
        self.assertEqual(curve.as_of(150)["p_yes"], 0.5)
        self.assertEqual(curve.span, (100, 300))


class BuildCurve(unittest.TestCase):

    FIXTURE = {"yes_is_home": True, "event_id": "E"}

    def test_joins_points_to_play_wallclocks(self):
        plays = [{"id": "p1", "wallclock": "2026-01-18T20:00:00Z"},
                 {"id": "p2", "wallclock": "2026-01-18T21:00:00Z"}]
        points = [{"playId": "p1", "homeWinPercentage": 0.4},
                  {"playId": "p2", "homeWinPercentage": 0.8}]
        curve = ds.build_curve(plays, points, self.FIXTURE)
        self.assertEqual(len(curve), 2)
        self.assertAlmostEqual(
            curve.as_of(ds.to_epoch("2026-01-18T20:30:00Z"))["p_yes"], 0.4)

    def test_accepts_the_core_api_field_names_too(self):
        plays = [{"id": "p1", "wallclock": "2026-01-18T20:00:00Z"}]
        points = [{"play_id": "p1", "home_win_pct": 0.6,
                   "spread_cover_home": 0.55, "total_over": 0.48}]
        curve = ds.build_curve(plays, points, self.FIXTURE)
        value = curve.as_of(ds.to_epoch("2026-01-18T20:00:00Z"))
        self.assertAlmostEqual(value["p_yes"], 0.6)
        self.assertAlmostEqual(value["spread_cover_home"], 0.55)
        self.assertAlmostEqual(value["total_over"], 0.48)

    def test_points_whose_play_has_no_clock_are_dropped(self):
        # Inventing a time for these is how holes become fiction.
        plays = [{"id": "p1", "wallclock": None},
                 {"id": "p2", "wallclock": "2026-01-18T21:00:00Z"}]
        points = [{"playId": "p1", "homeWinPercentage": 0.4},
                  {"playId": "p2", "homeWinPercentage": 0.8}]
        self.assertEqual(len(ds.build_curve(plays, points, self.FIXTURE)), 1)

    def test_probability_is_flipped_for_an_away_contract(self):
        plays = [{"id": "p1", "wallclock": "2026-01-18T20:00:00Z"}]
        points = [{"playId": "p1", "homeWinPercentage": 0.75}]
        away = {"yes_is_home": False, "event_id": "E"}
        curve = ds.build_curve(plays, points, away)
        self.assertAlmostEqual(
            curve.as_of(ds.to_epoch("2026-01-18T20:00:00Z"))["p_yes"], 0.25)

    def test_no_plays_gives_an_empty_curve_not_a_crash(self):
        points = [{"playId": "p1", "homeWinPercentage": 0.4}]
        self.assertEqual(len(ds.build_curve([], points, self.FIXTURE)), 0)


class CandleRows(unittest.TestCase):

    FIXTURE = {"yes_is_home": True, "event_id": "E1"}

    def _market(self):
        return {
            "ticker": "KXNFLGAME-26JAN18HOUNE-NE",
            "result": "yes",
            "close_time": "2026-01-18T23:40:00Z",
            "candles_hourly": [
                # Pregame: before any play.
                {"end_period_ts": ds.to_epoch("2026-01-18T19:00:00Z"),
                 "price": {"close": "0.6000"},
                 "yes_bid": {"close": "0.5900"},
                 "yes_ask": {"close": "0.6100"},
                 "volume": "1000.00", "open_interest": "500.00"},
                # In game.
                {"end_period_ts": ds.to_epoch("2026-01-18T22:00:00Z"),
                 "price": {"close": "0.8000"},
                 "yes_bid": {"close": "0.7900"},
                 "yes_ask": {"close": "0.8100"},
                 "volume": "2000.00", "open_interest": "900.00"},
            ],
            "candles_minute": [
                {"end_period_ts": ds.to_epoch("2026-01-18T23:39:00Z"),
                 "price": {"close": "0.9900"},
                 "yes_bid": {"close": "0.9800"},
                 "yes_ask": {"close": "1.0000"},
                 "volume": "300.00", "open_interest": "1000.00"},
            ],
        }

    def _curve(self):
        plays = [{"id": "a", "wallclock": "2026-01-18T20:30:00Z"},
                 {"id": "b", "wallclock": "2026-01-18T23:30:00Z"}]
        points = [{"playId": "a", "homeWinPercentage": 0.55},
                  {"playId": "b", "homeWinPercentage": 0.97}]
        return ds.build_curve(plays, points, self.FIXTURE)

    def test_emits_every_candle_from_both_resolutions(self):
        rows = ds.candle_rows(self._market(), self._curve(),
                              self.FIXTURE, "KXNFLGAME")
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["resolution"] for r in rows},
                         {"hourly", "minute"})

    def test_pregame_row_has_no_independent_estimate(self):
        rows = ds.candle_rows(self._market(), self._curve(),
                              self.FIXTURE, "KXNFLGAME")
        pregame = [r for r in rows
                   if r["ts"] == ds.to_epoch("2026-01-18T19:00:00Z")][0]
        self.assertIsNone(pregame["p_independent"],
                          "filling this invents a number nobody had")
        self.assertFalse(pregame["in_game"])

    def test_in_game_row_takes_the_estimate_known_at_that_time(self):
        rows = ds.candle_rows(self._market(), self._curve(),
                              self.FIXTURE, "KXNFLGAME")
        row = [r for r in rows
               if r["ts"] == ds.to_epoch("2026-01-18T22:00:00Z")][0]
        # 0.55 was known at 20:30. 0.97 arrives at 23:30 and must not
        # leak backwards into a 22:00 row.
        self.assertAlmostEqual(row["p_independent"], 0.55)

    def test_prices_are_dollars_and_spread_is_derived(self):
        rows = ds.candle_rows(self._market(), self._curve(),
                              self.FIXTURE, "KXNFLGAME")
        row = [r for r in rows
               if r["ts"] == ds.to_epoch("2026-01-18T22:00:00Z")][0]
        self.assertAlmostEqual(row["price"], 0.80)
        self.assertAlmostEqual(row["spread"], 0.02)
        self.assertAlmostEqual(row["volume"], 2000.0)

    def test_seconds_to_close_counts_down(self):
        rows = ds.candle_rows(self._market(), self._curve(),
                              self.FIXTURE, "KXNFLGAME")
        by_ts = {r["ts"]: r for r in rows}
        early = by_ts[ds.to_epoch("2026-01-18T19:00:00Z")]
        late = by_ts[ds.to_epoch("2026-01-18T23:39:00Z")]
        self.assertGreater(early["seconds_to_close"],
                           late["seconds_to_close"])
        self.assertEqual(late["seconds_to_close"], 60)

    def test_label_is_the_settled_result(self):
        rows = ds.candle_rows(self._market(), self._curve(),
                              self.FIXTURE, "KXNFLGAME")
        self.assertTrue(all(r["label"] == 1 for r in rows))

    def test_an_unsettled_market_has_a_null_label(self):
        market = self._market()
        market["result"] = None
        rows = ds.candle_rows(market, self._curve(), self.FIXTURE,
                              "KXNFLGAME")
        self.assertTrue(all(r["label"] is None for r in rows))

    def test_include_pregame_false_drops_rows_with_no_estimate(self):
        rows = ds.candle_rows(self._market(), self._curve(), self.FIXTURE,
                              "KXNFLGAME", include_pregame=False)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["in_game"] for r in rows))

    def test_soccer_style_market_with_no_curve_still_yields_rows(self):
        rows = ds.candle_rows(self._market(), ds.ProbabilityCurve([]),
                              self.FIXTURE, "KXEPLGAME")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["p_independent"] is None for r in rows))
        self.assertTrue(all(not r["in_game"] for r in rows))


class BuildState(unittest.TestCase):
    """Score and clock, the ground truth spread/total markets need.

    ESPN's own spread and total probabilities cannot serve these: it
    quotes one whole-number line (7.0) while Kalshi lists a ladder of
    half-points (6.5, 7.5, ...), so they are different contracts. Across
    294 games the lines coincided in 44. Score and clock answer every
    line at once.
    """

    HOME = {"yes_is_home": True, "event_id": "E"}
    AWAY = {"yes_is_home": False, "event_id": "E"}

    def _plays(self):
        return [
            {"id": "a", "wallclock": "2026-01-18T20:30:00Z",
             "away_score": 0, "home_score": 7, "period": 1,
             "clock_seconds": 600},
            {"id": "b", "wallclock": "2026-01-18T22:00:00Z",
             "away_score": 10, "home_score": 7, "period": 3,
             "clock_seconds": 200},
        ]

    def test_margin_is_oriented_to_the_home_contract(self):
        s = ds.build_state(self._plays(), self.HOME)
        v = s.as_of(ds.to_epoch("2026-01-18T20:45:00Z"))
        self.assertEqual(v["margin_for_yes"], 7)
        self.assertEqual(v["total_score"], 7)

    def test_margin_flips_for_the_away_contract(self):
        s = ds.build_state(self._plays(), self.AWAY)
        v = s.as_of(ds.to_epoch("2026-01-18T22:30:00Z"))
        self.assertEqual(v["margin_for_yes"], 3, "away leads 10-7")

    def test_total_score_is_side_independent(self):
        for fixture in (self.HOME, self.AWAY):
            s = ds.build_state(self._plays(), fixture)
            v = s.as_of(ds.to_epoch("2026-01-18T22:30:00Z"))
            self.assertEqual(v["total_score"], 17)

    def test_margin_is_none_when_there_is_no_side(self):
        """A total belongs to the game, not a team."""
        s = ds.build_state(self._plays(), {"yes_is_home": None})
        v = s.as_of(ds.to_epoch("2026-01-18T22:30:00Z"))
        self.assertIsNone(v["margin_for_yes"])
        self.assertEqual(v["total_score"], 17)

    def test_reads_the_mlb_field_spelling_too(self):
        plays = [{"id": "a", "wallclock": "2026-01-18T20:30:00Z",
                  "awayScore": 2, "homeScore": 5,
                  "period": {"number": 4}}]
        s = ds.build_state(plays, self.HOME)
        v = s.as_of(ds.to_epoch("2026-01-18T21:00:00Z"))
        self.assertEqual(v["margin_for_yes"], 3)
        self.assertEqual(v["period"], 4)

    def test_state_never_reaches_forward(self):
        s = ds.build_state(self._plays(), self.HOME)
        # One second before the 10-7 play; must still read 0-7.
        v = s.as_of(ds.to_epoch("2026-01-18T21:59:59Z"))
        self.assertEqual(v["away_score"], 0)

    def test_plays_without_a_clock_are_skipped(self):
        plays = [{"id": "a", "wallclock": None,
                  "away_score": 0, "home_score": 7}]
        self.assertEqual(len(ds.build_state(plays, self.HOME)), 0)


class RowsCarryState(unittest.TestCase):

    FIXTURE = {"yes_is_home": True, "event_id": "E1",
               "kind": "spread", "line": 3.5}

    def test_state_columns_are_populated_and_point_in_time(self):
        market = {
            "ticker": "KXNFLSPREAD-25AUG21NENYG-NE3",
            "result": "yes",
            "close_time": "2026-01-18T23:40:00Z",
            "candles_hourly": [
                {"end_period_ts": ds.to_epoch("2026-01-18T19:00:00Z"),
                 "price": {"close": "0.5000"}},
                {"end_period_ts": ds.to_epoch("2026-01-18T22:30:00Z"),
                 "price": {"close": "0.7000"}},
            ],
        }
        plays = [{"id": "a", "wallclock": "2026-01-18T21:00:00Z",
                  "away_score": 3, "home_score": 14, "period": 2}]
        state = ds.build_state(plays, self.FIXTURE)
        rows = ds.candle_rows(market, ds.ProbabilityCurve([]), self.FIXTURE,
                              "KXNFLSPREAD", state=state)
        by_ts = {r["ts"]: r for r in rows}

        pre = by_ts[ds.to_epoch("2026-01-18T19:00:00Z")]
        self.assertIsNone(pre["margin_for_yes"], "no play had happened")
        self.assertFalse(pre["in_game"])

        live = by_ts[ds.to_epoch("2026-01-18T22:30:00Z")]
        self.assertEqual(live["margin_for_yes"], 11)
        self.assertEqual(live["total_score"], 17)
        self.assertTrue(live["in_game"])
        self.assertAlmostEqual(live["market_line"], 3.5)
        self.assertEqual(live["market_kind"], "spread")

    def test_espn_spread_probability_is_not_called_p_independent(self):
        """It answers a different line, so it must not be mistaken for
        this market's estimate."""
        market = {"ticker": "T", "result": "yes",
                  "close_time": "2026-01-18T23:40:00Z",
                  "candles_hourly": [
                      {"end_period_ts": ds.to_epoch("2026-01-18T22:00:00Z"),
                       "price": {"close": "0.70"}}]}
        plays = [{"id": "a", "wallclock": "2026-01-18T20:00:00Z"}]
        points = [{"playId": "a", "home_win_pct": 0.8,
                   "spread_cover_home": 0.62}]
        curve = ds.build_curve(plays, points, self.FIXTURE)
        rows = ds.candle_rows(market, curve, self.FIXTURE, "KXNFLSPREAD")
        self.assertAlmostEqual(rows[0]["espn_spread_cover_home"], 0.62)
        self.assertAlmostEqual(rows[0]["p_independent"], 0.8,
                               msg="p_independent stays the moneyline")


class Edge(unittest.TestCase):

    def test_positive_when_the_independent_source_is_higher(self):
        self.assertAlmostEqual(
            ds.edge({"p_independent": 0.70, "price": 0.60}), 0.10)

    def test_none_when_either_side_is_missing(self):
        self.assertIsNone(ds.edge({"p_independent": None, "price": 0.6}))
        self.assertIsNone(ds.edge({"p_independent": 0.6, "price": None}))


if __name__ == "__main__":
    unittest.main()
