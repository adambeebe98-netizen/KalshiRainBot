"""Tests for evaluation/folds.py.

The headline test is `test_the_leaking_market_is_purged`: a market that
opens before a test window and settles inside it must appear in an
unpurged training set and be absent from a purged one. That single case
is the reason the module exists.
"""
import datetime as dt
import unittest

from evaluation import folds

DAY = 86400
HOUR = 3600


def _market(ticker, open_day, close_day, base=1_700_000_000):
    """A market opening and closing on given day offsets from a base."""
    def iso(t):
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    return {"ticker": ticker,
            "open_time": iso(base + open_day * DAY),
            "close_time": iso(base + close_day * DAY)}


def _population(n_days=200, per_day=3, base=1_700_000_000):
    """A market opening 39h before it closes, several per day -- roughly
    the real shape: median lifetime 39h, brackets sharing a city-day."""
    out = []
    for day in range(n_days):
        for j in range(per_day):
            close = base + day * DAY + 12 * HOUR
            open_ = close - 39 * HOUR
            out.append({
                "ticker": f"M-{day:03d}-{j}",
                "open_time": dt.datetime.fromtimestamp(
                    open_, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "close_time": dt.datetime.fromtimestamp(
                    close, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    return out


class TestToEpoch(unittest.TestCase):
    def test_accepts_int_iso_and_bare_date(self):
        self.assertEqual(folds.to_epoch(1000), 1000)
        self.assertEqual(folds.to_epoch("1970-01-01T00:16:40Z"), 1000)
        self.assertEqual(folds.to_epoch("1970-01-01T00:16:40+00:00"), 1000)

    def test_bare_date_is_utc_midnight_not_local(self):
        # The bug this guards: a naive date read as local time shifts every
        # split boundary by the host's UTC offset.
        self.assertEqual(folds.to_epoch("1970-01-02"), DAY)
        self.assertEqual(folds.to_epoch("2026-04-19"),
                         int(dt.datetime(2026, 4, 19, tzinfo=dt.timezone.utc).timestamp()))

    def test_bad_input_is_none(self):
        for bad in (None, "", "not a date", object()):
            self.assertIsNone(folds.to_epoch(bad))


class TestPurge(unittest.TestCase):
    def test_the_leaking_market_is_purged(self):
        # THE test. A market opening before the test window and settling
        # inside it carries the answer across the boundary.
        #
        # The comparison is against the NAIVE training set -- everything
        # already open when testing starts, which is what a reasonable
        # person would build without thinking about label periods. It is
        # deliberately not "generate with purging disabled": label-period
        # overlap is removed unconditionally, because a mode that emits a
        # knowingly-leaking fold is not a mode this module should have.
        base = 1_700_000_000
        population = _population(base=base)
        population.append(_market("LEAKER", 98, 101, base=base))

        target = None
        for f in folds.generate(population, n_folds=3, embargo_seconds=0):
            if f.test_start <= base + 101 * DAY + 12 * HOUR < f.test_end:
                target = f
                break
        self.assertIsNotNone(target, "no fold covers day 101; fixture is wrong")

        leaker_open = folds.to_epoch(_market("LEAKER", 98, 101, base=base)["open_time"])
        self.assertLess(leaker_open, target.test_start,
                        "fixture: the leaker must already be open when "
                        "testing starts, or it proves nothing")

        self.assertNotIn("LEAKER", target.train_tickers,
                         "a market settling inside the test window must not "
                         "be in that fold's training set")
        self.assertIn("LEAKER", target.purged_tickers,
                      "and the purge must be the reason it is missing")

    def test_purging_is_not_optional(self):
        # Even with every tunable set to zero, a market is excluded from
        # the training set of any fold its label period straddles.
        #
        # Only those folds: a market settling on day 101 SHOULD appear in
        # training for a fold testing day 150. Excluding it there would not
        # be extra safety, it would be throwing away history -- which is
        # what an earlier version of this test wrongly demanded.
        base = 1_700_000_000
        population = _population(base=base)
        leaker = _market("LEAKER", 98, 101, base=base)
        population.append(leaker)
        opened = folds.to_epoch(leaker["open_time"])
        closed = folds.to_epoch(leaker["close_time"])

        straddled = 0
        for f in folds.generate(population, n_folds=3, purge_seconds=0,
                                 embargo_seconds=0):
            if opened < f.test_end and closed > f.test_start:
                straddled += 1
                self.assertNotIn("LEAKER", f.train_tickers)
            elif closed < f.test_start:
                self.assertIn("LEAKER", f.train_tickers,
                              "a market settled well before the test window "
                              "belongs in training")
        # At least one, and two is legitimate: a three-day label period
        # sitting across a fold boundary overlaps both test windows, and
        # must be purged from both.
        self.assertGreaterEqual(straddled, 1,
                                "fixture straddles no fold; it proves nothing")

    def test_no_training_label_period_overlaps_its_test_window(self):
        # The invariant, checked exhaustively rather than by example.
        population = _population()
        by_ticker = {m["ticker"]: folds._label_period(m) for m in population}
        for f in folds.generate(population, n_folds=4):
            for ticker in f.train_tickers:
                opened, closed = by_ticker[ticker]
                overlaps = opened < f.test_end and closed > f.test_start
                self.assertFalse(
                    overlaps,
                    f"fold {f.index}: {ticker} label period "
                    f"[{opened},{closed}] overlaps test "
                    f"[{f.test_start},{f.test_end})")

    def test_purge_removes_the_run_up_to_the_window(self):
        population = _population()
        wide = folds.generate(population, n_folds=3, purge_seconds=10 * DAY,
                               embargo_seconds=0)
        narrow = folds.generate(population, n_folds=3, purge_seconds=0,
                                 embargo_seconds=0)
        self.assertLess(len(wide[0].train_tickers), len(narrow[0].train_tickers))
        self.assertGreater(wide[0].n_purged, narrow[0].n_purged)

    def test_purge_count_is_recorded(self):
        for f in folds.generate(_population(), n_folds=3):
            self.assertGreater(f.n_purged, 0,
                               "with 39h lifetimes some market always straddles")


class TestEmbargo(unittest.TestCase):
    def test_embargo_shrinks_training(self):
        population = _population()
        without = folds.generate(population, n_folds=3, embargo_seconds=0)
        with_ = folds.generate(population, n_folds=3, embargo_seconds=7 * DAY)
        self.assertLess(len(with_[0].train_tickers), len(without[0].train_tickers))
        self.assertGreater(with_[0].n_embargoed, 0)
        self.assertEqual(without[0].n_embargoed, 0)

    def test_embargo_is_recorded_on_the_fold(self):
        f = folds.generate(_population(), n_folds=3, embargo_seconds=3 * DAY)[0]
        self.assertEqual(f.embargo_seconds, 3 * DAY)


class TestWalkForwardShape(unittest.TestCase):
    def test_folds_are_chronological_and_non_overlapping(self):
        fs = folds.generate(_population(), n_folds=4)
        for a, b in zip(fs, fs[1:]):
            self.assertLessEqual(a.test_end, b.test_start)
        for f in fs:
            self.assertLess(f.test_start, f.test_end)

    def test_training_always_precedes_testing(self):
        for f in folds.generate(_population(), n_folds=4):
            self.assertLessEqual(f.train_end, f.test_start)

    def test_training_universe_expands(self):
        fs = folds.generate(_population(), n_folds=4, purge_seconds=0,
                             embargo_seconds=0)
        sizes = [len(f.train_tickers) for f in fs]
        self.assertEqual(sizes, sorted(sizes))

    def test_test_sets_are_disjoint(self):
        seen = set()
        for f in folds.generate(_population(), n_folds=4):
            overlap = seen & set(f.test_tickers)
            self.assertFalse(overlap, f"ticker in two test sets: {overlap}")
            seen |= set(f.test_tickers)

    def test_a_ticker_is_never_in_its_own_folds_train_and_test(self):
        for f in folds.generate(_population(), n_folds=4):
            self.assertFalse(set(f.train_tickers) & set(f.test_tickers))

    def test_thin_folds_are_dropped_not_returned_undersized(self):
        # 20 markets cannot support 5 folds with a 50-market minimum.
        with self.assertRaises(ValueError):
            folds.generate(_population(n_days=7, per_day=3), n_folds=5,
                            min_train_markets=50, min_test_markets=10)

    def test_summary_and_describe_render(self):
        fs = folds.generate(_population(), n_folds=3)
        self.assertIn("fold 0", fs[0].summary())
        text = folds.describe(fs)
        self.assertIn("purge=48h", text)
        self.assertIn("total dropped", text)


class TestVaultIsUnreachable(unittest.TestCase):
    def test_a_fold_intersecting_the_vault_raises(self):
        import splits
        vault_start = folds.to_epoch(splits.VAULT_START)
        # Markets spanning the vault period -- generate must refuse.
        population = []
        start_day = (vault_start - 30 * DAY)
        for i in range(300):
            close = start_day + i * DAY
            population.append({
                "ticker": f"V-{i}",
                "open_time": dt.datetime.fromtimestamp(
                    close - 39 * HOUR, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "close_time": dt.datetime.fromtimestamp(
                    close, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        with self.assertRaises(folds.VaultOverlapError):
            folds.generate(population, n_folds=3)

    def test_generate_from_splits_cannot_request_the_vault(self):
        import splits
        with self.assertRaises(splits.VaultAccessError):
            folds.generate_from_splits(split="vault")


class TestArgumentValidation(unittest.TestCase):
    def test_rejects_bad_fold_counts_and_windows(self):
        pop = _population()
        with self.assertRaises(ValueError):
            folds.generate(pop, n_folds=0)
        with self.assertRaises(ValueError):
            folds.generate(pop, purge_seconds=-1)
        with self.assertRaises(ValueError):
            folds.generate(pop, embargo_seconds=-1)

    def test_rejects_markets_without_usable_times(self):
        with self.assertRaises(ValueError):
            folds.generate([{"ticker": "X", "open_time": None, "close_time": None}])
        with self.assertRaises(ValueError):
            folds.generate([])

    def test_ignores_individual_unusable_rows(self):
        pop = _population()
        pop.append({"ticker": "BAD", "open_time": "garbage", "close_time": None})
        fs = folds.generate(pop, n_folds=3)
        for f in fs:
            self.assertNotIn("BAD", f.train_tickers)
            self.assertNotIn("BAD", f.test_tickers)


if __name__ == "__main__":
    unittest.main()
