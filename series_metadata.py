"""
Capture what Kalshi STATES about each series, including the settlement
source.

The rules extractor has been asking an LLM to read contract prose and
infer the settlement source. For KXRAINNYC it returned:

    settlement_source: "unclear"    confidence: medium

while /series/KXRAINNYC states it outright:

    settlement_sources: [{"name": "NWS Climatological Report",
                          "url": "https://forecast.weather.gov/...CLI..."}]

That is the CLI product -- the exact instrument behind the Austin loss,
where being right about the rain and wrong about what the contract read
cost $200. The field was published the whole time on an endpoint the
bot had never called, and the LLM was guessing at it and getting it
wrong for the one market family this project exists to understand.

So this fetches it directly. Series metadata barely changes, so one
pass a day is generous and the whole thing is a few dozen requests.

What this does NOT replace: station_code still lives in prose
("recorded at Central Park, New York"), and early_close_condition is a
dense paragraph about data releases and fallback deadlines. An LLM
still earns its place on those. It simply should not be guessing at
fields the API declares.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import time

from kalshi_client import KalshiClient

DEFAULT_DIR = "data/series_meta"

KEEP = ("ticker", "title", "category", "frequency", "settlement_sources",
        "contract_url", "tags", "fee_type", "fee_multiplier",
        "product_metadata", "settlement_timer_seconds")


def collect(kalshi: KalshiClient, series_list: list[str],
            directory: str = DEFAULT_DIR,
            sleep_between: float = 0.25) -> dict:
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(directory, f"{day}.jsonl.gz")
    os.makedirs(directory, exist_ok=True)

    stats = {"fetched": 0, "with_source": 0, "failed": 0}
    rows = []
    for series in series_list:
        try:
            resp = kalshi.get_series(series)
        except Exception as exc:
            stats["failed"] += 1
            rows.append({"series_ticker": series, "observed_ts":
                         int(time.time()),
                         "error": f"{type(exc).__name__}: {str(exc)[:80]}"})
            time.sleep(sleep_between)
            continue

        block = resp.get("series") or resp
        if isinstance(block, list):
            block = block[0] if block else {}
        row = {"series_ticker": series, "observed_ts": int(time.time())}
        for k in KEEP:
            if k in block:
                row[k] = block[k]
        sources = block.get("settlement_sources") or []
        if sources:
            stats["with_source"] += 1
        rows.append(row)
        stats["fetched"] += 1
        time.sleep(sleep_between)

    with gzip.open(path, "at", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":"),
                                default=str) + "\n")
    stats["path"] = path
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--series", action="append")
    p.add_argument("--show", action="store_true",
                   help="print each settlement source as it is found")
    args = p.parse_args()

    import collector
    from config import SETTINGS

    series_list = args.series or sorted(
        set(collector.EXTRA_SERIES) | set(SETTINGS.series_tickers))

    kalshi = KalshiClient()
    print(f"fetching metadata for {len(series_list)} series")
    stats = collect(kalshi, series_list, args.dir)
    print(f"  {stats['fetched']} fetched, {stats['with_source']} declare a "
          f"settlement source, {stats['failed']} failed")
    print(f"  -> {stats['path']}")

    if args.show:
        with gzip.open(stats["path"], "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                srcs = row.get("settlement_sources") or []
                names = ", ".join(s.get("name", "?") for s in srcs) or "-"
                print(f"    {row['series_ticker']:<24} {names[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
