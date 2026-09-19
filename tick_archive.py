"""
Append-only, date-partitioned, gzipped archive of raw WebSocket messages.

Why this exists, with numbers. Once the collector was actually working it
wrote ~770,000 ticks a day, each carrying a ~544-byte `raw_json` blob,
into the same SQLite file the trading bot reads and writes -- about
460 MB/day against 14 GB of free disk. That is four weeks of runway, and
it puts a large, useless write load on a file that also serves live
trading.

Useless because nothing ever reads `raw_json` back. It is write-only: an
archive of the unparsed message, kept so the data can be reprocessed
later when we understand it better. An archive is exactly the thing that
should not live in a hot transactional database.

So the raw message goes here instead -- one gzipped file per UTC day,
append-only, one JSON object per line. The parsed numeric columns stay in
SQLite, where they are small and queryable. Compression on JSON of this
shape runs 8-10x, and the write no longer contends with the bot.

Format, one line per tick:

    {"r": <received_ts>, "m": <the original message, verbatim>}

The message is embedded unparsed rather than re-serialised as a string.
That avoids double-escaping, avoids a parse/serialise round trip on the
hot path, and means the bytes on disk are the bytes the exchange sent.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import time

DEFAULT_DIR = "data/ticks"
DEFAULT_FLUSH_EVERY = 200
DEFAULT_FLUSH_SECONDS = 30.0


def day_key(received_ts: int) -> str:
    return dt.datetime.fromtimestamp(received_ts, dt.timezone.utc).strftime("%Y-%m-%d")


def path_for(received_ts: int, directory: str = DEFAULT_DIR) -> str:
    return os.path.join(directory, f"{day_key(received_ts)}.jsonl.gz")


def _encode(received_ts: int, raw: str) -> str:
    """One archive line.

    Embedding the message verbatim is only safe while it is single-line
    JSON. A message containing a newline would split across two lines and
    break the one-record-per-line contract for everything after it -- which
    is exactly what happened to records moved out of the database by the
    legacy migration, where some stored payloads were pretty-printed.

    So: verbatim when the message is single-line JSON, and json.dumps
    otherwise. The fallback escapes rather than strips, so nothing is
    lost; the record simply carries its message as a string.
    """
    stripped = raw.lstrip()
    if stripped[:1] in ("{", "[") and "\n" not in raw and "\r" not in raw:
        return '{"r":%d,"m":%s}' % (received_ts, raw)
    return json.dumps({"r": received_ts, "m": raw}, separators=(",", ":"))


class TickArchive:
    """Buffered writer with day rollover.

    Buffered because gzip wants a stream: opening and closing a file per
    message would both be slow and produce a file made of 770,000 tiny
    gzip members. Records are flushed every `flush_every` messages or
    `flush_seconds`, whichever comes first, so an unclean shutdown loses
    at most that much.
    """

    def __init__(self, directory: str = DEFAULT_DIR,
                 flush_every: int = DEFAULT_FLUSH_EVERY,
                 flush_seconds: float = DEFAULT_FLUSH_SECONDS):
        self.directory = directory
        self.flush_every = max(1, flush_every)
        self.flush_seconds = flush_seconds
        os.makedirs(directory, exist_ok=True)
        self._buffer: list[str] = []
        self._day: str | None = None
        self._last_flush = time.time()
        self.records_written = 0

    def append(self, received_ts: int, raw: str) -> None:
        day = day_key(received_ts)
        if self._day is not None and day != self._day:
            # Crossing midnight: get the previous day's records onto disk
            # before any of the new day's are buffered, so a file never
            # contains records from two days.
            self.flush()
        self._day = day
        self._buffer.append(_encode(received_ts, raw))
        now = time.time()
        if (len(self._buffer) >= self.flush_every
                or now - self._last_flush >= self.flush_seconds):
            self.flush()

    def flush(self) -> int:
        if not self._buffer or self._day is None:
            self._last_flush = time.time()
            return 0
        path = os.path.join(self.directory, f"{self._day}.jsonl.gz")
        payload = "\n".join(self._buffer) + "\n"
        # Append mode concatenates gzip members, which is valid and read
        # transparently by gzip and by this module's reader.
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write(payload)
        n = len(self._buffer)
        self.records_written += n
        self._buffer.clear()
        self._last_flush = time.time()
        return n

    def close(self) -> None:
        self.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def read_day(day: str, directory: str = DEFAULT_DIR, on_bad_line=None):
    """Yield {'r': received_ts, 'm': message} for one archived day.

    A line that will not parse is skipped rather than aborting the read.
    An archive is worth having because it can be reprocessed years later,
    and one damaged record should not make the other three quarters of a
    million unreadable. Pass `on_bad_line` to count or log them; the
    default is to drop them silently, which is the right behaviour for a
    caller that just wants the data and the wrong one for a caller
    auditing archive health -- hence the hook.
    """
    path = os.path.join(directory, f"{day}.jsonl.gz")
    if not os.path.exists(path):
        return
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError as exc:
                if on_bad_line is not None:
                    on_bad_line(day, lineno, str(exc))


def verify_day(day: str, directory: str = DEFAULT_DIR) -> dict:
    """Count readable and unreadable records in one archived day."""
    bad = []
    good = sum(1 for _ in read_day(
        day, directory, on_bad_line=lambda d, n, e: bad.append((n, e))))
    return {"day": day, "records": good, "bad_lines": len(bad),
            "first_bad": bad[0] if bad else None}


def archived_days(directory: str = DEFAULT_DIR) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(f[:-len(".jsonl.gz")] for f in os.listdir(directory)
                  if f.endswith(".jsonl.gz"))


def stats(directory: str = DEFAULT_DIR) -> dict:
    days = archived_days(directory)
    total = sum(os.path.getsize(os.path.join(directory, f"{d}.jsonl.gz"))
                for d in days)
    return {"days": len(days), "bytes": total,
            "first": days[0] if days else None,
            "last": days[-1] if days else None}
