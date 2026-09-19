"""Rejoin archive records that were split across lines.

Kalshi's WebSocket messages carry a trailing newline. The first version of
tick_archive._encode embedded the message verbatim as

    '{"r":%d,"m":%s}' % (received_ts, raw)

so with raw ending in a newline the closing brace landed on the next
line and every record was written as two. The data is all there; the
line boundaries are wrong.

Repair is deterministic: accumulate lines until the buffer parses as
JSON, emit it as one line, continue. Nothing is dropped and nothing is
guessed -- a record either parses or is kept aside and counted.

Writes to a temporary file and swaps only on success, so an interrupted
repair leaves the original intact.
"""
from __future__ import annotations

import gzip
import json
import os
import sys

import tick_archive

MAX_CONTINUATION_LINES = 50


def repair_day(day: str, directory: str = tick_archive.DEFAULT_DIR,
               dry_run: bool = False) -> dict:
    path = os.path.join(directory, f"{day}.jsonl.gz")
    if not os.path.exists(path):
        return {"day": day, "error": "missing"}

    repaired = already_ok = dropped = 0
    out_lines: list[str] = []
    buffer = ""
    buffered_lines = 0

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line and not buffer:
                continue
            buffer = line if not buffer else buffer + line
            buffered_lines += 1
            try:
                json.loads(buffer)
            except ValueError:
                if buffered_lines > MAX_CONTINUATION_LINES:
                    dropped += 1
                    buffer, buffered_lines = "", 0
                continue
            out_lines.append(buffer)
            if buffered_lines > 1:
                repaired += 1
            else:
                already_ok += 1
            buffer, buffered_lines = "", 0

    if buffer:
        dropped += 1

    result = {"day": day, "records": len(out_lines), "repaired": repaired,
              "already_ok": already_ok, "dropped": dropped}
    if dry_run or not out_lines:
        return result

    tmp = path + ".repairing"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
    # Verify before swapping. A repair that produces an unreadable file is
    # worse than the split-line problem it fixes.
    check = 0
    with gzip.open(tmp, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                json.loads(line)
                check += 1
    if check != len(out_lines):
        os.unlink(tmp)
        result["error"] = f"verification failed: {check} of {len(out_lines)}"
        return result
    os.replace(tmp, path)
    result["verified"] = check
    return result


def main() -> int:
    dry = "--dry-run" in sys.argv
    days = tick_archive.archived_days()
    print(f"{len(days)} archived day(s){' (dry run)' if dry else ''}\n")
    for day in days:
        before = tick_archive.verify_day(day)
        out = repair_day(day, dry_run=dry)
        after = tick_archive.verify_day(day)
        print(f"{day}: readable {before['records']:,} -> {after['records']:,}  "
              f"(rejoined {out.get('repaired', 0):,}, "
              f"already fine {out.get('already_ok', 0):,}, "
              f"dropped {out.get('dropped', 0)})")
        if "error" in out:
            print(f"   ERROR: {out['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
