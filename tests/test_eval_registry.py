"""Tests for evaluation/registry.py."""
import os
import sqlite3
import tempfile
import unittest

from evaluation import execution, registry


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE historical_markets (ticker TEXT PRIMARY KEY, x INTEGER);
            CREATE TABLE historical_price_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, ts INTEGER);
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER);
        """)
        conn.executemany("INSERT INTO historical_markets VALUES (?,?)",
                         [(f"M{i}", i) for i in range(5)])
        conn.executemany(
            "INSERT INTO historical_price_points (ticker, ts) VALUES (?,?)",
            [(f"M{i}", i) for i in range(10)])
        conn.commit()
        conn.close()
        registry.init(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def assumptions(self):
        return execution.HourlyCandleExecution().assumptions()

    def a_run(self, config=None, seed=1):
        return registry.open_run(
            config or {"candidate": "x", "alpha": 1},
            seed=seed, splits_used="train+dev",
            purge_seconds=172800, embargo_seconds=86400,
            execution_assumptions=self.assumptions(),
            db_path=self.db,
            snapshot_tables=("historical_markets", "historical_price_points",
                             "market_snapshots"))


class TestConfigHashing(unittest.TestCase):
    def test_key_order_does_not_change_the_hash(self):
        a = registry.config_hash({"a": 1, "b": 2})
        b = registry.config_hash({"b": 2, "a": 1})
        self.assertEqual(a, b)

    def test_different_values_change_the_hash(self):
        self.assertNotEqual(registry.config_hash({"a": 1}),
                            registry.config_hash({"a": 2}))

    def test_nested_structures_hash_stably(self):
        a = {"x": {"p": 1, "q": [1, 2]}, "y": 3}
        b = {"y": 3, "x": {"q": [1, 2], "p": 1}}
        self.assertEqual(registry.config_hash(a), registry.config_hash(b))

    def test_list_order_does_matter(self):
        # Sorting keys is canonicalisation; sorting VALUES would erase a
        # real difference between two configs.
        self.assertNotEqual(registry.config_hash({"x": [1, 2]}),
                            registry.config_hash({"x": [2, 1]}))

    def test_hash_is_hex_sha256(self):
        h = registry.config_hash({"a": 1})
        self.assertEqual(len(h), 64)
        int(h, 16)


class TestTrialCounting(RegistryTestCase):
    def test_counts_accumulate(self):
        run = self.a_run()
        self.assertEqual(registry.trials_to_date(self.db), 0)
        n1 = registry.record_trial(run.run_id, "cand-a", "h1", 0.5, "FAIL",
                                    db_path=self.db)
        n2 = registry.record_trial(run.run_id, "cand-b", "h2", 0.9, "FAIL",
                                    db_path=self.db)
        self.assertEqual((n1, n2), (1, 2))
        self.assertEqual(registry.trials_to_date(self.db), 2)

    def test_discarded_candidates_still_count(self):
        # A candidate you looked at and disliked still consumed a look.
        run = self.a_run()
        registry.record_trial(run.run_id, "rubbish", "h", None, "FAIL",
                               db_path=self.db)
        self.assertEqual(registry.trials_to_date(self.db), 1)

    def test_trials_cannot_be_deleted(self):
        run = self.a_run()
        registry.record_trial(run.run_id, "a", "h", 0.1, "FAIL", db_path=self.db)
        conn = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM eval_trials")
        conn.close()
        self.assertEqual(registry.trials_to_date(self.db), 1)

    def test_trials_cannot_be_rewritten(self):
        run = self.a_run()
        registry.record_trial(run.run_id, "a", "h", 0.1, "FAIL", db_path=self.db)
        conn = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE eval_trials SET net_sharpe = 9.0")
        conn.close()

    def test_history_returns_newest_first(self):
        run = self.a_run()
        for name in ("a", "b", "c"):
            registry.record_trial(run.run_id, name, "h", 0.1, "FAIL",
                                   db_path=self.db)
        names = [r["candidate_name"] for r in registry.history(db_path=self.db)]
        self.assertEqual(names, ["c", "b", "a"])


class TestLuckThreshold(RegistryTestCase):
    def test_threshold_rises_with_trials(self):
        a = registry.luck_threshold(10, 1.0)
        b = registry.luck_threshold(10_000, 1.0)
        self.assertLess(a, b)

    def test_banner_states_the_count_and_the_threshold(self):
        run = self.a_run()
        for i in range(5):
            registry.record_trial(run.run_id, f"c{i}", "h", 0.1 * i, "FAIL",
                                   db_path=self.db)
        banner = registry.trial_banner(db_path=self.db)
        self.assertIn("trials to date: 5", banner)
        self.assertIn("by luck alone", banner)

    def test_sharpe_variance_is_floored_on_thin_history(self):
        self.assertEqual(registry.observed_sharpe_variance(self.db), 0.01)

    def test_sharpe_variance_reflects_search_dispersion(self):
        run = self.a_run()
        for v in (-2.0, -1.0, 0.0, 1.0, 2.0):
            registry.record_trial(run.run_id, "c", "h", v, "FAIL",
                                   db_path=self.db)
        self.assertGreater(registry.observed_sharpe_variance(self.db), 1.0)

    def test_wider_search_raises_the_bar(self):
        narrow = registry.luck_threshold(1000, 0.01)
        wide = registry.luck_threshold(1000, 1.0)
        self.assertLess(narrow, wide)


class TestRunRecords(RegistryTestCase):
    def test_run_is_persisted_and_reloadable(self):
        run = self.a_run()
        again = registry.load_run(run.run_id, self.db)
        self.assertEqual(again.config_hash, run.config_hash)
        self.assertEqual(again.seed, run.seed)
        self.assertEqual(again.purge_seconds, run.purge_seconds)
        self.assertEqual(again.execution_model, "HourlyCandleExecution")

    def test_assumptions_survive_the_round_trip(self):
        run = self.a_run()
        again = registry.load_run(run.run_id, self.db)
        self.assertEqual(again.execution_assumptions["uses_depth"], False)
        self.assertIn("notes", again.execution_assumptions)

    def test_identical_configs_hash_identically_across_runs(self):
        a = self.a_run(config={"k": 1})
        b = self.a_run(config={"k": 1})
        self.assertEqual(a.config_hash, b.config_hash)
        self.assertNotEqual(a.run_id, b.run_id)

    def test_unknown_run_is_none(self):
        self.assertIsNone(registry.load_run("nope", self.db))

    def test_describe_renders(self):
        self.assertIn("run ", self.a_run().describe())


class TestDataBoundaries(RegistryTestCase):
    def test_boundaries_capture_max_id_and_counts(self):
        b = registry.snapshot_boundaries(
            ("historical_markets", "historical_price_points"), self.db)
        self.assertEqual(b["historical_price_points"], 10)   # MAX(id)
        self.assertEqual(b["historical_markets"], 5)         # COUNT(*), no id

    def test_missing_table_records_minus_one(self):
        b = registry.snapshot_boundaries(("does_not_exist",), self.db)
        self.assertEqual(b["does_not_exist"], -1)

    def test_reopen_succeeds_when_data_is_intact(self):
        run = self.a_run()
        self.assertEqual(registry.reopen_run(run.run_id, self.db).run_id,
                         run.run_id)

    def test_growth_does_not_break_reproduction(self):
        # New rows arriving after the run are fine -- the boundary pins
        # them out. Only deletion is fatal.
        run = self.a_run()
        conn = sqlite3.connect(self.db)
        conn.executemany(
            "INSERT INTO historical_price_points (ticker, ts) VALUES (?,?)",
            [("NEW", i) for i in range(5)])
        conn.commit()
        conn.close()
        registry.reopen_run(run.run_id, self.db)

    def test_deletion_makes_reproduction_raise(self):
        run = self.a_run()
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM historical_price_points WHERE id > 3")
        conn.commit()
        conn.close()
        with self.assertRaises(registry.BoundaryError) as ctx:
            registry.reopen_run(run.run_id, self.db)
        self.assertIn("rows were deleted", str(ctx.exception))

    def test_reopening_an_unknown_run_raises(self):
        with self.assertRaises(KeyError):
            registry.reopen_run("nope", self.db)


class TestDeterminism(RegistryTestCase):
    def test_same_config_and_seed_produce_the_same_hash_twice(self):
        cfg = {"alpha": 0.5, "beta": [1, 2, 3], "name": "candidate-1"}
        self.assertEqual(registry.config_hash(cfg), registry.config_hash(cfg))

    def test_canonical_json_is_byte_stable(self):
        cfg = {"b": 2, "a": {"z": 1, "y": 2}}
        self.assertEqual(registry.canonical_json(cfg),
                         registry.canonical_json({"a": {"y": 2, "z": 1}, "b": 2}))


if __name__ == "__main__":
    unittest.main()
