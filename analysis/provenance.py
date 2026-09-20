"""Which files are original, which were rewritten, which are new?

"Are you still using the scraping code I brought in" deserves an answer
from the record rather than from memory. git knows when each file first
appeared and when it was last touched, so classify every Python file:

  ORIGINAL   added before this Claude Code work began and never
             modified since -- still exactly the user's code
  MODIFIED   the user's file, since edited
  NEW        did not exist before

The scraping layer specifically is what was asked about, so that is
called out separately from everything else.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path("/root/kalshi_weather_bot")

# The session's work starts after the original upload commits. Anything
# first committed on or before this date is the user's own code.
HANDOVER = "2026-09-17"


def git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True).stdout.strip()


def first_seen(path: str) -> str:
    return git("log", "--reverse", "--format=%ad", "--date=short",
               "--", path).split("\n")[0]


def last_touched(path: str) -> str:
    return git("log", "-1", "--format=%ad", "--date=short", "--", path)


def commits(path: str) -> int:
    out = git("log", "--oneline", "--", path)
    return len(out.split("\n")) if out else 0


SCRAPERS = [
    "kalshi_client.py", "storage.py", "realtime_kalshi_ws.py",
    "realtime_weather_poller.py", "weather_data.py",
    "historical_backfill.py", "run_backfill.py", "rules_extractor.py",
    "collector.py", "tick_archive.py", "weather_archive.py",
    "forecast_archive.py", "backfill_sports.py", "backfill_truth.py",
    "backfill_espn_core.py", "sports_truth.py", "export_archive.py",
    "bot.py", "fees.py",
]

print("=" * 78)
print("THE SCRAPING / DATA LAYER")
print("=" * 78)
print(f"{'file':<28} {'first seen':<12} {'last edit':<12} "
      f"{'commits':>7}  status")
print("-" * 78)

for name in SCRAPERS:
    p = ROOT / name
    if not p.exists():
        print(f"{name:<28} (gone)")
        continue
    fs, lt, n = first_seen(name), last_touched(name), commits(name)
    if fs > HANDOVER:
        status = "NEW (written here)"
    elif lt > HANDOVER:
        status = "YOURS, since modified"
    else:
        status = "YOURS, untouched"
    print(f"{name:<28} {fs:<12} {lt:<12} {n:>7}  {status}")

print("\n" + "=" * 78)
print("WHOLE REPO")
print("=" * 78)
orig = mod = new = 0
orig_lines = new_lines = 0
for p in sorted(ROOT.glob("*.py")) + sorted(ROOT.glob("evaluation/*.py")) \
        + sorted(ROOT.glob("tests/*.py")):
    rel = str(p.relative_to(ROOT))
    fs, lt = first_seen(rel), last_touched(rel)
    try:
        lines = len(p.read_text(encoding="utf-8", errors="replace")
                    .splitlines())
    except OSError:
        lines = 0
    if not fs:
        continue
    if fs > HANDOVER:
        new += 1
        new_lines += lines
    elif lt > HANDOVER:
        mod += 1
        orig_lines += lines
    else:
        orig += 1
        orig_lines += lines

print(f"  yours, untouched        {orig:>4} files")
print(f"  yours, since modified   {mod:>4} files")
print(f"  new, written here       {new:>4} files  ({new_lines:,} lines)")
print(f"  your surviving code               ({orig_lines:,} lines)")
