"""
Tests for the Kalshi-to-ESPN fixture join.

The failure mode here is silent. A bad split does not raise -- it
produces a row with a price path, a win-probability path and an outcome
that all look fine and belong to different games. Nothing downstream can
detect that, so it has to be caught here.

The LACHI case is the one that matters and the one the first
implementation got wrong: 'LACHI' splits as LA+CHI or LAC+HI, and both
halves are real NFL teams either way.
"""
from __future__ import annotations

import unittest

import market_join as mj


class ParseTicker(unittest.TestCase):

    def test_parses_a_standard_fixture(self):
        self.assertEqual(mj.parse_ticker("KXNFLGAME-26JAN18HOUNE-NE"),
                         ("2026-01-18", "HOUNE", "NE"))

    def test_two_digit_year_is_this_century(self):
        date, _, _ = mj.parse_ticker("KXNFLGAME-25JUL31LACDET-LAC")
        self.assertEqual(date, "2025-07-31")

    def test_non_fixture_tickers_return_none_rather_than_raising(self):
        # Callers page through mixed series; plenty of tickers are not
        # two-team fixtures at all.
        for ticker in ("KXRT-EVI-69",
                       "KXHIGHNY-26JAN18-B40",
                       "",
                       "garbage",
                       "KXNFLGAME-26XXX18HOUNE-NE"):
            self.assertIsNone(mj.parse_ticker(ticker), ticker)

    def test_impossible_date_returns_none(self):
        self.assertIsNone(mj.parse_ticker("KXNFLGAME-26FEB30HOUNE-NE"))


class SplitTeams(unittest.TestCase):

    def test_unambiguous_split(self):
        self.assertEqual(mj.split_teams("HOUNE", "NE"), ("HOU", "NE"))
        self.assertEqual(mj.split_teams("HOUNE", "HOU"), ("HOU", "NE"))

    def test_the_lachi_case_from_both_sides(self):
        """'LACHI' is LA+CHI or LAC+HI; the yes_team settles it.

        This is the split the naive implementation got wrong, and its
        failures were all Los Angeles fixtures -- a non-random hole.
        """
        self.assertEqual(mj.split_teams("LACHI", "LA"), ("LA", "CHI"))
        self.assertEqual(mj.split_teams("LACHI", "CHI"), ("LA", "CHI"))

    def test_chargers_are_not_confused_with_rams(self):
        self.assertEqual(mj.split_teams("LACDET", "LAC"), ("LAC", "DET"))
        self.assertEqual(mj.split_teams("LACDET", "DET"), ("LAC", "DET"))

    def test_a_yes_team_that_is_not_in_the_blob_gives_none(self):
        self.assertIsNone(mj.split_teams("HOUNE", "BUF"))

    def test_never_returns_an_empty_half(self):
        # blob == yes_team would otherwise split into ('NE', '').
        self.assertIsNone(mj.split_teams("NE", "NE"))

    def test_empty_inputs_are_none(self):
        self.assertIsNone(mj.split_teams("", "NE"))
        self.assertIsNone(mj.split_teams("HOUNE", ""))


class Aliases(unittest.TestCase):
    """Direction matters, and both hand-written guesses were inverted."""

    def test_kalshi_la_maps_to_espn_lar(self):
        self.assertEqual(mj.espn_code("LA"), "LAR")

    def test_chargers_are_left_alone(self):
        self.assertEqual(mj.espn_code("LAC"), "LAC")

    def test_washington_goes_kalshi_was_to_espn_wsh(self):
        # Not the reverse. The first version had WSH->WAS and lost 28
        # fixtures to it.
        self.assertEqual(mj.espn_code("WAS"), "WSH")

    def test_jacksonville_goes_kalshi_jac_to_espn_jax(self):
        self.assertEqual(mj.espn_code("JAC"), "JAX")

    def test_aliases_do_not_point_at_each_other(self):
        """An alias whose target is itself an alias key would chain."""
        for source, target in mj.CODE_ALIASES.items():
            self.assertNotIn(target, mj.CODE_ALIASES,
                             f"{source}->{target} chains into another alias")

    def test_unknown_codes_pass_through_unchanged(self):
        self.assertEqual(mj.espn_code("HOU"), "HOU")


class FixtureFor(unittest.TestCase):

    def test_builds_a_complete_fixture(self):
        f = mj.fixture_for("KXNFLGAME-26JAN18LACHI-LA")
        self.assertEqual(f["date"], "2026-01-18")
        self.assertEqual((f["away"], f["home"]), ("LA", "CHI"))
        self.assertEqual(f["espn_away"], "LAR")
        self.assertFalse(f["yes_is_home"], "LA is the away side here")

    def test_yes_is_home_is_set_for_the_home_contract(self):
        f = mj.fixture_for("KXNFLGAME-26JAN18LACHI-CHI")
        self.assertTrue(f["yes_is_home"])

    def test_non_fixture_returns_none(self):
        self.assertIsNone(mj.fixture_for("KXRT-EVI-69"))


class MatchEvent(unittest.TestCase):

    EVENTS = [
        {"event_id": "1", "competitors": [
            {"abbrev": "HOU", "home_away": "away"},
            {"abbrev": "NE", "home_away": "home"}]},
        {"event_id": "2", "competitors": [
            {"abbrev": "LAR", "home_away": "away"},
            {"abbrev": "CHI", "home_away": "home"}]},
    ]

    def test_matches_through_the_alias(self):
        f = mj.fixture_for("KXNFLGAME-26JAN18LACHI-LA")
        self.assertEqual(mj.match_event(f, self.EVENTS)["event_id"], "2")

    def test_home_and_away_are_not_interchangeable(self):
        """A reversed fixture is a DIFFERENT game, not the same one."""
        reversed_events = [{"event_id": "9", "competitors": [
            {"abbrev": "NE", "home_away": "away"},
            {"abbrev": "HOU", "home_away": "home"}]}]
        f = mj.fixture_for("KXNFLGAME-26JAN18HOUNE-NE")
        self.assertIsNone(mj.match_event(f, reversed_events))

    def test_no_match_returns_none(self):
        f = mj.fixture_for("KXNFLGAME-26JAN18BUFDEN-BUF")
        self.assertIsNone(mj.match_event(f, self.EVENTS))


class MatchEventNear(unittest.TestCase):
    """Kalshi dates in US local time, ESPN in UTC -- an evening game
    lands on the next UTC day, and that cost 112 August fixtures."""

    def _events(self, day):
        return {day: [{"event_id": "X", "competitors": [
            {"abbrev": "NYJ", "home_away": "away"},
            {"abbrev": "GB", "home_away": "home"}]}]}

    def test_finds_a_game_espn_files_on_the_next_day(self):
        f = mj.fixture_for("KXNFLGAME-25AUG09NYJGB-NYJ")
        found = mj.match_event_near(f, self._events("2025-08-10"))
        self.assertIsNotNone(found, "an 8pm ET kickoff is next-day UTC")
        self.assertEqual(found["event_id"], "X")

    def test_finds_a_game_filed_on_the_previous_day(self):
        f = mj.fixture_for("KXNFLGAME-25AUG09NYJGB-NYJ")
        self.assertIsNotNone(
            mj.match_event_near(f, self._events("2025-08-08")))

    def test_the_exact_date_wins_over_a_neighbour(self):
        exact = {"event_id": "EXACT", "competitors": [
            {"abbrev": "NYJ", "home_away": "away"},
            {"abbrev": "GB", "home_away": "home"}]}
        by_date = self._events("2025-08-10")
        by_date["2025-08-09"] = [exact]
        f = mj.fixture_for("KXNFLGAME-25AUG09NYJGB-NYJ")
        self.assertEqual(
            mj.match_event_near(f, by_date)["event_id"], "EXACT")

    def test_does_not_reach_beyond_the_window(self):
        f = mj.fixture_for("KXNFLGAME-25AUG09NYJGB-NYJ")
        self.assertIsNone(
            mj.match_event_near(f, self._events("2025-08-12")))

    def test_still_refuses_a_reversed_fixture_nearby(self):
        """Widening the date must not weaken the fixture check."""
        reversed_ = {"2025-08-10": [{"event_id": "R", "competitors": [
            {"abbrev": "GB", "home_away": "away"},
            {"abbrev": "NYJ", "home_away": "home"}]}]}
        f = mj.fixture_for("KXNFLGAME-25AUG09NYJGB-NYJ")
        self.assertIsNone(mj.match_event_near(f, reversed_))


class MatchEventAt(unittest.TestCase):
    """Baseball breaks date-only matching in two separate ways.

    Date-only matching put 10.9% of MLB markets on the wrong game:
    roughly half a neighbouring day of the same series, half the other
    end of a doubleheader. Both are resolved by start time.
    """

    def _ev(self, eid, iso):
        return {"event_id": eid, "date": iso, "competitors": [
            {"abbrev": "CHC", "home_away": "away"},
            {"abbrev": "PIT", "home_away": "home"}]}

    def _fixture(self):
        return mj.fixture_for("KXMLBGAME-25APR30CHCPIT-PIT")

    def test_picks_the_doubleheader_game_that_matches_the_close(self):
        by_date = {"2025-04-30": [self._ev("G1", "2025-04-30T17:05Z"),
                                  self._ev("G2", "2025-04-30T21:05Z")]}
        close = mj._epoch("2025-04-30T23:55Z")     # after game 2
        self.assertEqual(
            mj.match_event_at(self._fixture(), by_date, close)["event_id"],
            "G2")

    def test_picks_game_one_when_the_market_closed_between_them(self):
        by_date = {"2025-04-30": [self._ev("G1", "2025-04-30T17:05Z"),
                                  self._ev("G2", "2025-04-30T21:05Z")]}
        close = mj._epoch("2025-04-30T20:10Z")     # before game 2 starts
        self.assertEqual(
            mj.match_event_at(self._fixture(), by_date, close)["event_id"],
            "G1")

    def test_does_not_take_yesterdays_game_in_the_same_series(self):
        """Teams play three-day series; the ±1 day window sees them all."""
        by_date = {"2025-04-29": [self._ev("PREV", "2025-04-29T22:40Z")],
                   "2025-04-30": [self._ev("TODAY", "2025-04-30T22:40Z")]}
        close = mj._epoch("2025-05-01T01:50Z")
        self.assertEqual(
            mj.match_event_at(self._fixture(), by_date, close)["event_id"],
            "TODAY")

    def test_refuses_a_game_that_started_after_the_market_closed(self):
        by_date = {"2025-04-30": [self._ev("LATER", "2025-04-30T23:00Z")]}
        close = mj._epoch("2025-04-30T20:00Z")
        self.assertIsNone(
            mj.match_event_at(self._fixture(), by_date, close),
            "a game cannot settle a contract that closed before it began")

    def test_refuses_a_start_too_far_before_the_close(self):
        by_date = {"2025-04-30": [self._ev("STALE", "2025-04-30T00:05Z")]}
        close = mj._epoch("2025-04-30T23:55Z")     # ~24h later
        self.assertIsNone(
            mj.match_event_at(self._fixture(), by_date, close))

    def test_falls_back_to_date_matching_with_no_close_time(self):
        by_date = {"2025-04-30": [self._ev("ONLY", "2025-04-30T22:40Z")]}
        self.assertEqual(
            mj.match_event_at(self._fixture(), by_date, None)["event_id"],
            "ONLY")

    def test_no_candidates_is_none(self):
        self.assertIsNone(mj.match_event_at(self._fixture(), {}, 0))


class CandidateEvents(unittest.TestCase):

    def test_collects_across_the_window_without_duplicates(self):
        ev = {"event_id": "A", "date": "2025-04-30T22:40Z", "competitors": [
            {"abbrev": "CHC", "home_away": "away"},
            {"abbrev": "PIT", "home_away": "home"}]}
        by_date = {"2025-04-29": [ev], "2025-04-30": [ev]}
        f = mj.fixture_for("KXMLBGAME-25APR30CHCPIT-PIT")
        self.assertEqual(len(mj.candidate_events(f, by_date)), 1)


class ProbabilityForYes(unittest.TestCase):

    def test_home_contract_takes_the_probability_as_is(self):
        f = mj.fixture_for("KXNFLGAME-26JAN18HOUNE-NE")   # NE is home
        self.assertAlmostEqual(mj.probability_for_yes(0.73, f), 0.73)

    def test_away_contract_is_flipped(self):
        f = mj.fixture_for("KXNFLGAME-26JAN18HOUNE-HOU")  # HOU is away
        self.assertAlmostEqual(mj.probability_for_yes(0.73, f), 0.27)

    def test_the_two_sides_of_one_game_sum_to_one(self):
        home = mj.fixture_for("KXNFLGAME-26JAN18HOUNE-NE")
        away = mj.fixture_for("KXNFLGAME-26JAN18HOUNE-HOU")
        for p in (0.0, 0.25, 0.5, 0.9, 1.0):
            self.assertAlmostEqual(
                mj.probability_for_yes(p, home)
                + mj.probability_for_yes(p, away), 1.0)


class Prices(unittest.TestCase):
    """Two ways to get a price wrong, both seen in real data."""

    def test_dollar_strings_are_read_as_dollars_not_ints(self):
        # int(float("0.62")) is 0. An early probe did exactly this and
        # reported a 0c price range on every market on the exchange.
        candle = {"price": {"close": "0.6200"}}
        self.assertAlmostEqual(mj.candle_price(candle), 0.62)

    def test_missing_or_malformed_price_is_none_not_zero(self):
        self.assertIsNone(mj.candle_price({}))
        self.assertIsNone(mj.candle_price({"price": None}))
        self.assertIsNone(mj.candle_price({"price": {}}))
        self.assertIsNone(mj.candle_price({"price": {"close": "abc"}}))

    def test_bid_and_ask_blocks_are_reachable(self):
        candle = {"price": {"close": "0.50"}, "yes_bid": {"close": "0.49"},
                  "yes_ask": {"close": "0.51"}}
        self.assertAlmostEqual(mj.candle_price(candle, "yes_bid"), 0.49)
        self.assertAlmostEqual(mj.candle_price(candle, "yes_ask"), 0.51)

    def test_final_price_prefers_the_minute_series(self):
        """The real NYG@DEN case: the hourly tail INVERTS the outcome.

        Hourly's last bucket ended at 23:00 with the market at 0.02;
        the game finished at 23:40 and the minute series closed at 0.99
        on a market that settled YES.
        """
        market = {
            "candles_hourly": [{"price": {"close": "0.0200"}}],
            "candles_minute": [{"price": {"close": "0.9900"}}],
        }
        self.assertAlmostEqual(mj.final_price(market), 0.99)

    def test_falls_back_to_hourly_when_there_are_no_minute_candles(self):
        market = {"candles_hourly": [{"price": {"close": "0.4000"}}],
                  "candles_minute": []}
        self.assertAlmostEqual(mj.final_price(market), 0.40)

    def test_skips_trailing_candles_that_carry_no_price(self):
        market = {"candles_minute": [{"price": {"close": "0.80"}},
                                     {"price": {}},
                                     {"volume": "0"}]}
        self.assertAlmostEqual(mj.final_price(market), 0.80)

    def test_no_candles_at_all_is_none(self):
        self.assertIsNone(mj.final_price({}))
        self.assertIsNone(mj.final_price({"candles_hourly": [],
                                          "candles_minute": []}))


if __name__ == "__main__":
    unittest.main()
