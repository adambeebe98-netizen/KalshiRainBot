import storage
import time
import ast
import re
import json
import os
from kalshi_client import KalshiClient
from rules_extractor import RulesExtractor

storage.init_db()
CACHE_FILE = "weather_series_cache.json"
LLM_HISTORY_FILE = "llm_history.jsonl"
COST_PER_CALL = 0.002541  # measured: ~512 input tokens ($3/M) + ~67 output tokens ($15/M), Sonnet 4.6 confirmed pricing

TOTAL_SERIES = 138
FALLBACK_PER_SERIES_ESTIMATE = 3000
BAR_LENGTH = 150

if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE) as f:
        weather_series = json.load(f)
else:
    kalshi = KalshiClient()
    extractor = RulesExtractor()
    all_tickers = set()
    for keyword in ["KXRAIN", "KXHIGH", "KXLOW"]:
        all_tickers.update(kalshi.discover_series_tickers(keyword))

    weather_series = []
    for series_ticker in sorted(all_tickers):
        try:
            markets = kalshi.get_markets(series_ticker=series_ticker, status="open", limit=1).get("markets", [])
            if not markets:
                weather_series.append(series_ticker)
                continue
            rules_text = kalshi.get_market_rules_text(markets[0]["ticker"])
            if not rules_text.strip():
                continue
            rules = extractor.extract(markets[0]["ticker"], rules_text)
            if rules.measure != "other":
                weather_series.append(series_ticker)
        except Exception:
            continue

    with open(CACHE_FILE, "w") as f:
        json.dump(weather_series, f)

started, finished = {}, {}
try:
    with open("backfill_service.log") as f:
        lines = f.readlines()
except FileNotFoundError:
    lines = []
current = None
for line in lines:
    m = re.match(r"--- (\S+) ---", line)
    if m:
        current = m.group(1)
        started[current] = True
    elif current and "'processed':" in line:
        finished[current] = line.strip()
        current = None

completed = [s for s in weather_series if s in finished]
active = [s for s in weather_series if s in started and s not in finished]
upcoming = [s for s in weather_series if s not in started]

with storage.get_conn() as conn:
    total = conn.execute("SELECT COUNT(*) FROM historical_markets").fetchone()[0]
    earliest_ts = conn.execute("SELECT MIN(backfilled_ts) FROM historical_markets").fetchone()[0]
    series_sizes = []
    for s in completed:
        count = conn.execute("SELECT COUNT(*) FROM historical_markets WHERE ticker LIKE ?", (f"{s}-%",)).fetchone()[0]
        series_sizes.append(count)

if series_sizes:
    avg_per_series = sum(series_sizes) / len(series_sizes)
    estimate_basis = f"based on the real average of {len(series_sizes)} completed series"
else:
    avg_per_series = FALLBACK_PER_SERIES_ESTIMATE
    estimate_basis = "initial rough guess -- no series finished yet to measure a real average"

active_series = active[0] if active else None
active_current = 0
if active_series:
    with storage.get_conn() as conn:
        active_current = conn.execute("SELECT COUNT(*) FROM historical_markets WHERE ticker LIKE ?", (f"{active_series}-%",)).fetchone()[0]

not_yet_started = len(weather_series) - len(completed) - len(active)
remaining_for_active = max(0, avg_per_series - active_current) if active else 0
estimated_remaining = (not_yet_started * avg_per_series) + remaining_for_active
total_estimate = total + estimated_remaining

fraction = min(total / total_estimate, 1.0) if total_estimate > 0 else 0
filled = int(fraction * BAR_LENGTH)
bar = "|" * filled + " " * (BAR_LENGTH - filled)
pct = fraction * 100
print(f"[{bar}] {pct:.1f}%  ({total}/{int(total_estimate)} est.)")

now = int(time.time())
if earliest_ts and now > earliest_ts and total > 0:
    elapsed_seconds = now - earliest_ts
    overall_rate_per_sec = total / elapsed_seconds
    if overall_rate_per_sec > 0:
        seconds_left = estimated_remaining / overall_rate_per_sec
        hours_left = seconds_left / 3600
        if hours_left >= 24:
            print(f"Estimated time remaining: ~{hours_left/24:.1f} days  ({estimate_basis})")
        else:
            print(f"Estimated time remaining: ~{hours_left:.1f} hours  ({estimate_basis})")

extractor_now = RulesExtractor()
cache_size_now = len(extractor_now._cache)
active_series_cache_count = sum(1 for k in extractor_now._cache if k.startswith(f"{active_series}-")) if active_series else 0

llm_history = []
if os.path.exists(LLM_HISTORY_FILE):
    with open(LLM_HISTORY_FILE) as f:
        for line in f:
            try:
                llm_history.append(json.loads(line))
            except json.JSONDecodeError:
                continue

WINDOW_SECONDS = 600
candidates = [h for h in llm_history if now - h["ts"] <= WINDOW_SECONDS + 90]
oldest_in_window = min(candidates, key=lambda h: h["ts"]) if candidates else None

# For the per-series rate: only compare against a snapshot where the SAME series was active,
# otherwise the delta is meaningless (comparing one series' count against a different one's).
same_series_candidates = [h for h in candidates if h.get("active_series") == active_series]
oldest_same_series = min(same_series_candidates, key=lambda h: h["ts"]) if same_series_candidates else None

llm_history.append({
    "ts": now, "cache_size": cache_size_now, "markets_total": total,
    "active_series": active_series, "active_series_cache_count": active_series_cache_count,
    "active_series_markets": active_current,
})
llm_history = [h for h in llm_history if now - h["ts"] <= 1200]
with open(LLM_HISTORY_FILE, "w") as f:
    for h in llm_history:
        f.write(json.dumps(h) + "\n")

print()
if oldest_in_window:
    real_window_minutes = (now - oldest_in_window["ts"]) / 60
    calls_in_window = cache_size_now - oldest_in_window["cache_size"]
    markets_in_window = total - oldest_in_window["markets_total"]
    cost_in_window = calls_in_window * COST_PER_CALL

    print(f"=== LLM USAGE (last {real_window_minutes:.1f} min, all series combined) ===")
    print(f"LLM calls: {calls_in_window}  |  Cost this window: ${cost_in_window:.4f}")

    if markets_in_window > 0:
        calls_per_market_now = calls_in_window / markets_in_window
        est_remaining_calls = estimated_remaining * calls_per_market_now
        est_remaining_cost = est_remaining_calls * COST_PER_CALL
        print(f"Estimated remaining LLM calls: {int(est_remaining_calls):,}  |  "
              f"Estimated remaining cost: ${est_remaining_cost:.2f}")
else:
    print("=== LLM USAGE ===")
    print("Building up history for a real rate (needs ~10 min of dashboard refreshes)")

print()
print("=== CACHE INFO (real NEW calls only, not stale cache-hit history) ===")
if active_series and oldest_same_series:
    window_min = (now - oldest_same_series["ts"]) / 60
    new_calls = active_series_cache_count - oldest_same_series["active_series_cache_count"]
    new_markets = active_current - oldest_same_series["active_series_markets"]
    if new_markets > 0:
        real_pct = new_calls / new_markets * 100
        print(f"Current series ({active_series}), last {window_min:.1f} min: "
              f"{new_calls} new LLM calls / {new_markets} new markets  ({real_pct:.1f}% needed a fresh call)")
    else:
        print(f"Current series ({active_series}): not enough new markets yet in this window")
elif active_series:
    print(f"Current series ({active_series}): building up history (needs a few dashboard refreshes)")
print()

print(f"=== PROGRESS: {len(completed)}/{len(weather_series)} series complete ===\n")

# finished dict preserves real chronological completion order (insertion order)
chronological = [s for s in finished if s in completed]

completed_with_work = []
completed_empty = []
for s in chronological:
    try:
        parsed = ast.literal_eval(finished[s])
        is_empty = parsed.get("processed", 0) == 0 and parsed.get("failed", 0) == 0 and parsed.get("skipped_too_old", 0) == 0
    except (ValueError, SyntaxError):
        is_empty = False
    if is_empty:
        completed_empty.append(s)
    else:
        completed_with_work.append(s)

SHOW_LAST = 5
print(f"--- COMPLETED, most recent {min(SHOW_LAST, len(completed_with_work))} of {len(completed_with_work)} ---")
for s in completed_with_work[-SHOW_LAST:]:
    print(f"  {s}: {finished[s]}")

if completed_empty:
    print(f"\n--- COMPLETED (no markets in range, {len(completed_empty)}) ---")
    shown = completed_empty[-10:]
    line = ", ".join(shown)
    if len(completed_empty) > 10:
        line += f", +{len(completed_empty) - 10} more"
    print(f"  {line}")

with storage.get_conn() as conn:
    print("\n--- CURRENTLY ACTIVE ---")
    for s in active:
        count = conn.execute("SELECT COUNT(*) FROM historical_markets WHERE ticker LIKE ?", (f"{s}-%",)).fetchone()[0]
        print(f"  {s}: {count} markets logged so far")

    print(f"\n--- MOST RECENT EXTRACTIONS ---")
    recent5 = conn.execute(
        "SELECT ticker, station_code, threshold_low_f, threshold_high_f, result, settlement_source "
        "FROM historical_markets ORDER BY backfilled_ts DESC LIMIT 5"
    ).fetchall()
    for ticker, station, low, high, result, source in recent5:
        thresh = f"[{low},{high}]" if (low is not None or high is not None) else "-"
        print(f"  {ticker}  station={station}  threshold={thresh}  result={result}  source={source}")

    print(f"\n--- NEXT UP ---")
    for s in upcoming[:5]:
        print(f"  {s}")
    if len(upcoming) > 5:
        print(f"  ... and {len(upcoming) - 5} more queued")

    cutoff = int(time.time()) - 60
    recent_count = conn.execute("SELECT COUNT(*) FROM historical_markets WHERE backfilled_ts >= ?", (cutoff,)).fetchone()[0]

print(f"\n--- LAST 60 SECONDS: {recent_count} markets backfilled ---")
print(f"\n=== RUNNING TOTAL (all series, all time): {total} ===")
