"""
Tests for the sports ground-truth poller.

Two things here are easy to get silently wrong, and both were wrong in
the first draft:

  * A finished game rewrote its whole boxscore every 90 seconds for the
    two-hour grace window -- ~80 identical copies per game.
  * The revision flag was computed after the digest had already been
    stored, so it would have read True on the very first write and
    labelled every new boxscore a revision.

Silently wrong is the operative phrase: neither shows up as an error,
and both corrupt the one signal the file exists to produce. So the
dedup and the revision flag are tested directly rather than eyeballed.
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest

import sports_truth


def _play(pid: str, text: str, seq: str = "1") -> dict:
    return {"id": pid, "sequenceNumber": seq, "type": {"text": "Single"},
            "text": text, "scoreValue": 0, "awayScore": 0, "homeScore": 0,
            "period": {"number": 1}, "clock": {"displayValue": "T1"},
            "scoringPlay": False, "wallclock": "2026-09-19T12:00:00Z",
            "participants": []}


def _summary_payload(plays, completed=True, box=None, odds_detail="BUF -5.5"):
    return {
        "plays": plays,
        "winprobability": [{"homeWinPercentage": 0.61, "tiePercentage": 0.0,
                            "playId": "p9"}],
        "pickcenter": [{"provider": {"name": "DraftKings"},
                        "details": odds_detail, "overUnder": 54.5,
                        "spread": -5.5,
                        "homeTeamOdds": {"moneyLine": -245},
                        "awayTeamOdds": {"moneyLine": 200}}],
        "boxscore": box if box is not None else {"teams": ["a", "b"]},
        "header": {"competitions": [
            {"status": {"type": {"completed": completed}}}]},
    }


class SummaryDedup(unittest.TestCase):
    """The same game polled twice must not write the same rows twice."""

    def setUp(self):
        self.payload = _summary_payload([_play("1", "Single to left")])
        self._real_get = sports_truth._get
        sports_truth._get = lambda path, **kw: self.payload

    def tearDown(self):
        sports_truth._get = self._real_get

    def test_first_poll_writes_everything(self):
        seen, gstate = {}, {}
        batch = sports_truth.summary("mlb", "E1", seen, gstate)
        self.assertEqual(len(batch["play"]), 1)
        self.assertEqual(len(batch["winprob"]), 1)
        self.assertEqual(len(batch["odds"]), 1)
        self.assertEqual(len(batch["boxscore"]), 1)

    def test_second_identical_poll_writes_nothing(self):
        seen, gstate = {}, {}
        sports_truth.summary("mlb", "E1", seen, gstate)
        batch = sports_truth.summary("mlb", "E1", seen, gstate)
        self.assertEqual(batch["play"], [])
        self.assertEqual(batch["winprob"], [],
                         "win probability rewritten unchanged")
        self.assertEqual(batch["odds"], [], "odds line rewritten unchanged")
        self.assertEqual(batch["boxscore"], [],
                         "boxscore rewritten unchanged -- this is the ~80"
                         " copies per game bug")

    def test_eighty_polls_of_a_finished_game_write_once(self):
        """The grace window is 2h at 90s, so this is the real cadence."""
        seen, gstate = {}, {}
        writes = 0
        for _ in range(80):
            batch = sports_truth.summary("mlb", "E1", seen, gstate)
            writes += sum(len(v) for v in batch.values())
        self.assertEqual(writes, 4, "one play + winprob + odds + boxscore")


class Revisions(unittest.TestCase):
    """A changed record is the signal; a new record is not a revision."""

    def setUp(self):
        self._real_get = sports_truth._get

    def tearDown(self):
        sports_truth._get = self._real_get

    def test_first_boxscore_is_not_flagged_as_a_revision(self):
        sports_truth._get = lambda path, **kw: _summary_payload(
            [_play("1", "Single")])
        gstate = {}
        batch = sports_truth.summary("mlb", "E1", {}, gstate)
        self.assertFalse(batch["boxscore"][0]["revision"],
                         "a first write is not a revision")

    def test_changed_boxscore_is_flagged_as_a_revision(self):
        payload = _summary_payload([_play("1", "Single")])
        sports_truth._get = lambda path, **kw: payload
        seen, gstate = {}, {}
        sports_truth.summary("mlb", "E1", seen, gstate)

        # The official scorer changes a hit to an error after the game.
        payload["boxscore"] = {"teams": ["a", "b"], "hits": 4}
        batch = sports_truth.summary("mlb", "E1", seen, gstate)
        self.assertEqual(len(batch["boxscore"]), 1)
        self.assertTrue(batch["boxscore"][0]["revision"],
                        "a boxscore that changed after the fact IS the"
                        " event this poller exists to catch")

    def test_changed_play_text_is_flagged_as_a_revision(self):
        payload = _summary_payload([_play("1", "Single to left")])
        sports_truth._get = lambda path, **kw: payload
        seen, gstate = {}, {}
        sports_truth.summary("mlb", "E1", seen, gstate)

        payload["plays"] = [_play("1", "Reached on error")]
        batch = sports_truth.summary("mlb", "E1", seen, gstate)
        self.assertEqual(len(batch["play"]), 1)
        self.assertTrue(batch["play"][0]["revision"])
        self.assertIsNotNone(batch["play"][0]["previous_digest"])

    def test_a_moving_odds_line_is_written_again(self):
        payload = _summary_payload([_play("1", "Single")])
        sports_truth._get = lambda path, **kw: payload
        seen, gstate = {}, {}
        sports_truth.summary("mlb", "E1", seen, gstate)

        payload["pickcenter"][0]["details"] = "BUF -7.5"
        batch = sports_truth.summary("mlb", "E1", seen, gstate)
        self.assertEqual(len(batch["odds"]), 1,
                         "a line that moves is the signal")


class StatePersistence(unittest.TestCase):
    """Without this, a restart rewrites every play of every live game.

    Measured over one day with several restarts: 16,420 plays had more
    than one stored version and 15,640 of those differed in NOTHING.
    That is pure duplication, and it also makes any later count of
    "how often does the official record change" meaningless.
    """

    def setUp(self):
        self.payload = _summary_payload([_play("1", "Single to left")])
        self._real_get = sports_truth._get
        sports_truth._get = lambda path, **kw: self.payload

    def tearDown(self):
        sports_truth._get = self._real_get

    def test_state_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            state = {"plays_seen": {"mlb:E1": {"p1": "abc"}},
                     "game_state": {"mlb:E1": {"boxscore": "def"}},
                     "board_seen": {"mlb:E1": "ghi"},
                     "finished_at": {"E1": 1700000000}}
            sports_truth.save_state(state, d)
            back = sports_truth.load_state(d)
            self.assertEqual(back["plays_seen"], state["plays_seen"])
            self.assertEqual(back["game_state"], state["game_state"])
            self.assertEqual(back["finished_at"], {"E1": 1700000000})

    def test_a_restart_does_not_rewrite_the_same_plays(self):
        with tempfile.TemporaryDirectory() as d:
            first = sports_truth.load_state(d)
            batch = sports_truth.summary(
                "mlb", "E1", first.setdefault("plays_seen", {}).setdefault(
                    "mlb:E1", {}),
                first.setdefault("game_state", {}).setdefault("mlb:E1", {}))
            self.assertEqual(len(batch["play"]), 1)
            sports_truth.save_state(first, d)

            # Service restarts: fresh process, state read from disk.
            second = sports_truth.load_state(d)
            batch2 = sports_truth.summary(
                "mlb", "E1", second["plays_seen"]["mlb:E1"],
                second["game_state"]["mlb:E1"])
            self.assertEqual(batch2["play"], [],
                             "a restart re-recorded plays it already had")

    def test_missing_state_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sports_truth.load_state(d), {})

    def test_corrupt_state_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, sports_truth.STATE_FILE), "w") as fh:
                fh.write("{not json")
            self.assertEqual(sports_truth.load_state(d), {})

    def test_prune_keeps_the_most_recent_games(self):
        state = {
            "plays_seen": {f"mlb:E{i}": {} for i in range(10)},
            "game_state": {f"mlb:E{i}": {} for i in range(10)},
            "finished_at": {f"E{i}": 1000 + i for i in range(10)},
        }
        sports_truth.prune_state(state, keep_events=4)
        self.assertEqual(len(state["plays_seen"]), 4)
        # Oldest finish times go first.
        self.assertIn("mlb:E9", state["plays_seen"])
        self.assertNotIn("mlb:E0", state["plays_seen"])


class WallclockNotInDigest(unittest.TestCase):
    """ESPN revises wallclock constantly and it is not the record.

    612 of 778 flagged revisions in one day differed ONLY in wallclock.
    Counting those makes the flag 8-to-1 noise.
    """

    def setUp(self):
        self._real_get = sports_truth._get

    def tearDown(self):
        sports_truth._get = self._real_get

    def test_a_changed_wallclock_alone_is_not_a_revision(self):
        payload = _summary_payload([_play("1", "Single to left")])
        payload["plays"][0]["wallclock"] = "2026-09-19T18:10:24Z"
        sports_truth._get = lambda path, **kw: payload
        seen, gstate = {}, {}
        sports_truth.summary("mlb", "E1", seen, gstate)

        payload["plays"][0]["wallclock"] = "2026-09-19T18:11:48Z"
        batch = sports_truth.summary("mlb", "E1", seen, gstate)
        self.assertEqual(batch["play"], [],
                         "a nudged timestamp is not the record changing")

    def test_a_changed_type_still_is_a_revision(self):
        payload = _summary_payload([_play("1", "Shot")])
        sports_truth._get = lambda path, **kw: payload
        seen, gstate = {}, {}
        sports_truth.summary("mlb", "E1", seen, gstate)

        # The real NHL case: 'Missed' reclassified as 'Shot'.
        payload["plays"][0]["type"] = {"text": "Missed"}
        payload["plays"][0]["wallclock"] = "2026-09-19T19:00:00Z"
        batch = sports_truth.summary("mlb", "E1", seen, gstate)
        self.assertEqual(len(batch["play"]), 1)
        self.assertTrue(batch["play"][0]["revision"])


class Writing(unittest.TestCase):

    def test_write_round_trips_as_gzipped_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            n = sports_truth.write("mlb", "play",
                                   [{"kind": "play", "a": 1},
                                    {"kind": "play", "a": 2}], d)
            self.assertEqual(n, 2)
            path = sports_truth._path("mlb", "play", d)
            self.assertTrue(os.path.exists(path))
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual([r["a"] for r in rows], [1, 2])

    def test_write_appends_rather_than_truncating(self):
        with tempfile.TemporaryDirectory() as d:
            sports_truth.write("mlb", "play", [{"a": 1}], d)
            sports_truth.write("mlb", "play", [{"a": 2}], d)
            with gzip.open(sports_truth._path("mlb", "play", d), "rt") as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
            self.assertEqual(len(rows), 2, "a second write clobbered the"
                                           " first -- the archive is"
                                           " append-only by design")

    def test_empty_write_creates_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sports_truth.write("mlb", "play", [], d), 0)
            self.assertFalse(os.path.exists(sports_truth._path("mlb", "play", d)))


class GracePolicy(unittest.TestCase):

    def test_live_games_are_always_polled(self):
        self.assertTrue(sports_truth._is_interesting(
            {"event_id": "E1", "state": "in", "completed": False}, {}))

    def test_finished_games_stay_in_scope_for_the_grace_window(self):
        finished: dict = {}
        row = {"event_id": "E1", "state": "post", "completed": True}
        self.assertTrue(sports_truth._is_interesting(row, finished),
                        "a scorer can change a ruling after the whistle")
        # Age it out.
        finished["E1"] -= sports_truth.FINISHED_GRACE_SECONDS + 1
        self.assertFalse(sports_truth._is_interesting(row, finished))


if __name__ == "__main__":
    unittest.main()
