"""
Shared pacing and a disk guard for the long backfills.

TWO PROBLEMS THIS SOLVES.

First, every backfill hard-codes a 0.2-0.25s sleep, chosen when nobody
had measured anything. Kalshi answers these reads in about 10ms and
served 50 requests a second without a single 429, so the jobs have been
spending roughly 95% of their wall-clock asleep. On the full weather
trade backfill that is the difference between 4.3 hours and 0.4.

The fix is not simply a smaller number. 25 requests without a 429 is
not proof of a ceiling, so this ADAPTS: it starts at a conservative
rate, and on any throttle it backs off hard and recovers slowly.
Fast when the exchange is happy, polite the moment it is not.

Second, a backfill that fills the disk does not just fail. The droplet
runs five live collectors, and live data is the only kind that cannot
be re-fetched -- so running out of space costs real, permanent data
loss while the backfill it was serving could have been re-run any time.
The guard aborts the JOB rather than let that happen.
"""
from __future__ import annotations

import re
import shutil
import time


class DiskGuard:
    """Abort a job before it can starve the live collectors.

    Checked periodically rather than per-write: stat on every record
    would dominate a job doing 50 requests a second, and the disk does
    not fall off a cliff between checks.
    """

    def __init__(self, path: str = "/", min_free_gb: float = 2.0,
                 check_every: int = 200):
        self.path = path
        self.min_free = min_free_gb * 1024 ** 3
        self.check_every = check_every
        self._n = 0
        self._last_free = None

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.path).free

    def check(self, force: bool = False) -> None:
        self._n += 1
        if not force and self._n % self.check_every:
            return
        free = self.free_bytes()
        self._last_free = free
        if free < self.min_free:
            raise RuntimeError(
                f"disk guard: only {free/1024**3:.2f} GB free at "
                f"{self.path}, below the {self.min_free/1024**3:.2f} GB "
                f"floor. Aborting so the live collectors keep their "
                f"space -- their data cannot be re-fetched, this job's "
                f"can.")

    @property
    def free_gb(self) -> float:
        free = self._last_free if self._last_free is not None \
            else self.free_bytes()
        return free / 1024 ** 3


class Throttle:
    """Adaptive pacing: fast by default, cautious after a throttle.

    `interval` is the target gap between requests. A 429 multiplies it
    by `backoff` and sleeps out the penalty; a run of clean responses
    eases it back toward the floor. It never goes below `min_interval`
    or above `max_interval`.
    """

    def __init__(self, interval: float = 0.05, min_interval: float = 0.02,
                 max_interval: float = 2.0, backoff: float = 4.0,
                 recover_after: int = 100):
        self.interval = interval
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.backoff = backoff
        self.recover_after = recover_after
        self._clean = 0
        self._last = 0.0
        self.throttles = 0
        self.requests = 0

    def wait(self) -> None:
        gap = time.time() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.time()
        self.requests += 1

    def ok(self) -> None:
        self._clean += 1
        if self._clean >= self.recover_after and \
                self.interval > self.min_interval:
            self.interval = max(self.min_interval, self.interval * 0.8)
            self._clean = 0

    def throttled(self, retry_after: float | None = None) -> None:
        self.throttles += 1
        self._clean = 0
        self.interval = min(self.max_interval, self.interval * self.backoff)
        time.sleep(retry_after if retry_after else self.interval * 2)


def is_throttle(exc: Exception) -> tuple[bool, float | None]:
    """Does this exception mean 'slow down', and for how long?"""
    msg = str(exc)
    if "429" not in msg and not re.search(r"rate.?limit", msg, re.I):
        return False, None
    m = re.search(r"retry[-_ ]after[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)",
                  msg, re.I)
    return True, (float(m.group(1)) if m else None)


def call(fn, *args, throttle: Throttle, guard: DiskGuard | None = None,
         attempts: int = 5, **kwargs):
    """One paced, throttle-aware, disk-guarded request.

    A 404 returns None rather than raising: for these endpoints it means
    'this market has no such data', which is a normal answer and not a
    failure worth retrying.
    """
    delay = 1.0
    for attempt in range(attempts):
        if guard is not None:
            guard.check()
        throttle.wait()
        try:
            out = fn(*args, **kwargs)
            throttle.ok()
            return out
        except Exception as exc:
            hit, retry_after = is_throttle(exc)
            if hit:
                throttle.throttled(retry_after)
                continue
            if "404" in str(exc):
                return None
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return None
