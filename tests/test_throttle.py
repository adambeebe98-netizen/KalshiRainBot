"""
Tests for the shared throttle and disk guard.

Both exist to protect something that cannot be recovered: the disk
guard protects live collection from a backfill filling the disk, and
the throttle protects API access from a job that has just been told it
can go twenty times faster.

The cases below are the ones where getting it wrong is silent -- a
guard that never fires, a backoff that does not back off, a recovery
that undoes the backoff immediately.
"""
from __future__ import annotations

import collections
import unittest
from unittest import mock

import throttle as th

# shutil.disk_usage returns a NAMED tuple and the guard reads `.free`
# from it. Mocking it with a plain tuple type-checks fine and fails at
# runtime on the attribute -- which is exactly what the first version of
# these tests did, five times over.
Usage = collections.namedtuple("Usage", "total used free")
GB = 1024 ** 3


def usage(free_gb: float) -> Usage:
    return Usage(total=100 * GB, used=(100 - free_gb) * GB,
                 free=int(free_gb * GB))


class DiskGuardTests(unittest.TestCase):

    def test_passes_when_there_is_room(self):
        g = th.DiskGuard(min_free_gb=1.0, check_every=1)
        with mock.patch.object(th.shutil, "disk_usage",
                               return_value=usage(50)):
            g.check()      # no raise

    def test_aborts_below_the_floor(self):
        g = th.DiskGuard(min_free_gb=2.0, check_every=1)
        with mock.patch.object(th.shutil, "disk_usage",
                               return_value=usage(1)):
            with self.assertRaises(RuntimeError) as ctx:
                g.check()
        # The message has to say WHY, or whoever finds the dead job at
        # 3am will assume the backfill was the thing that mattered.
        self.assertIn("live collectors", str(ctx.exception))

    def test_only_checks_periodically(self):
        g = th.DiskGuard(min_free_gb=99999.0, check_every=10)
        with mock.patch.object(th.shutil, "disk_usage",
                               return_value=usage(0.001)) as du:
            for _ in range(9):
                g.check()
            self.assertEqual(du.call_count, 0, "stat'd too eagerly")
            with self.assertRaises(RuntimeError):
                g.check()

    def test_force_checks_immediately(self):
        g = th.DiskGuard(min_free_gb=99999.0, check_every=1000)
        with mock.patch.object(th.shutil, "disk_usage",
                               return_value=usage(0.001)):
            with self.assertRaises(RuntimeError):
                g.check(force=True)


class ThrottleTests(unittest.TestCase):

    def test_a_429_widens_the_interval(self):
        t = th.Throttle(interval=0.05, backoff=4.0)
        with mock.patch.object(th.time, "sleep"):
            t.throttled()
        self.assertAlmostEqual(t.interval, 0.20)
        self.assertEqual(t.throttles, 1)

    def test_backoff_is_capped(self):
        t = th.Throttle(interval=1.0, max_interval=2.0, backoff=10.0)
        with mock.patch.object(th.time, "sleep"):
            t.throttled()
        self.assertEqual(t.interval, 2.0)

    def test_recovery_needs_a_run_of_clean_calls(self):
        t = th.Throttle(interval=0.20, min_interval=0.02,
                        recover_after=5)
        for _ in range(4):
            t.ok()
        self.assertEqual(t.interval, 0.20, "eased off too early")
        t.ok()
        self.assertLess(t.interval, 0.20)

    def test_recovery_never_goes_below_the_floor(self):
        t = th.Throttle(interval=0.02, min_interval=0.02, recover_after=1)
        for _ in range(50):
            t.ok()
        self.assertEqual(t.interval, 0.02)

    def test_retry_after_is_honoured(self):
        t = th.Throttle(interval=0.05)
        with mock.patch.object(th.time, "sleep") as slept:
            t.throttled(retry_after=7.5)
        slept.assert_called_once_with(7.5)


class IsThrottleTests(unittest.TestCase):

    def test_detects_a_429(self):
        hit, after = th.is_throttle(Exception("Kalshi API error 429 on GET"))
        self.assertTrue(hit)
        self.assertIsNone(after)

    def test_reads_retry_after_when_present(self):
        hit, after = th.is_throttle(
            Exception('429 {"retry_after": 12}'))
        self.assertTrue(hit)
        self.assertEqual(after, 12.0)

    def test_a_404_is_not_a_throttle(self):
        hit, _ = th.is_throttle(Exception("Kalshi API error 404"))
        self.assertFalse(hit)


class CallTests(unittest.TestCase):

    def test_returns_the_value(self):
        t = th.Throttle(interval=0.0)
        self.assertEqual(th.call(lambda: 42, throttle=t), 42)

    def test_404_becomes_none_without_retrying(self):
        t = th.Throttle(interval=0.0)
        calls = []

        def boom():
            calls.append(1)
            raise Exception("Kalshi API error 404")

        self.assertIsNone(th.call(boom, throttle=t))
        self.assertEqual(len(calls), 1, "retried a 404")

    def test_a_429_is_retried_after_backing_off(self):
        t = th.Throttle(interval=0.0)
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise Exception("Kalshi API error 429")
            return "ok"

        with mock.patch.object(th.time, "sleep"):
            self.assertEqual(th.call(flaky, throttle=t), "ok")
        self.assertEqual(t.throttles, 1)

    def test_the_disk_guard_stops_the_job(self):
        t = th.Throttle(interval=0.0)
        g = th.DiskGuard(min_free_gb=99999.0, check_every=1)
        with mock.patch.object(th.shutil, "disk_usage",
                               return_value=usage(0.001)):
            with self.assertRaises(RuntimeError):
                th.call(lambda: "never", throttle=t, guard=g)


if __name__ == "__main__":
    unittest.main()

