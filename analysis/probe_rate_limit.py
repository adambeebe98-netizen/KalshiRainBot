"""How fast will Kalshi actually let us read?

Every backfill in this project paces at 0.2-0.25s between requests --
4 to 5 a second -- chosen because Kalshi publishes no read limit this
code can rely on and pacing beat discovering one the hard way. That
was the right call when the jobs were small. The full weather trade
backfill is roughly 59,000 markets, and at 5/s that is over three
hours of pure waiting.

So measure it, carefully. This ramps UP and stops at the first sign of
throttling rather than hammering to find the ceiling: the goal is a
safe sustainable rate, not the maximum before a ban. Any 429 ends the
test immediately.

Read-only endpoints only. Nothing here places or cancels anything.
"""
from __future__ import annotations

import re
import time

from kalshi_client import KalshiClient

kalshi = KalshiClient()

# Cheap, read-only, and representative of what a backfill actually does.
def one_call():
    return kalshi.get_historical_markets(series_ticker="KXRAINNYC", limit=1)


def burst(n: int, interval: float) -> dict:
    """n requests at a fixed interval; report latency and any throttling."""
    latencies, throttled, errors = [], 0, 0
    t_start = time.time()
    for _ in range(n):
        t0 = time.time()
        try:
            one_call()
            latencies.append(time.time() - t0)
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or re.search(r"rate", msg, re.I):
                throttled += 1
                break
            errors += 1
        remaining = interval - (time.time() - t0)
        if remaining > 0:
            time.sleep(remaining)
    elapsed = time.time() - t_start
    lat = sorted(latencies)
    return {
        "sent": len(latencies) + throttled + errors,
        "ok": len(latencies),
        "throttled": throttled,
        "errors": errors,
        "elapsed": elapsed,
        "rate": len(latencies) / elapsed if elapsed else 0,
        "p50": lat[len(lat) // 2] if lat else 0,
        "p90": lat[int(len(lat) * 0.9)] if lat else 0,
    }


print("ramping up, stopping at the first throttle\n")
print(f"{'interval':>9} {'target/s':>9} {'sent':>6} {'ok':>5} {'429':>5} "
      f"{'err':>5} {'actual/s':>9} {'p50':>7} {'p90':>7}")
print("-" * 74)

safe = None
for interval in (0.25, 0.15, 0.10, 0.06, 0.04, 0.02):
    r = burst(25, interval)
    target = 1 / interval
    flag = ""
    if r["throttled"]:
        flag = "  <-- THROTTLED, stopping"
    elif r["errors"]:
        flag = f"  <-- {r['errors']} errors"
    else:
        safe = interval
    print(f"{interval:>9.2f} {target:>9.1f} {r['sent']:>6} {r['ok']:>5} "
          f"{r['throttled']:>5} {r['errors']:>5} {r['rate']:>9.1f} "
          f"{r['p50']:>6.2f}s {r['p90']:>6.2f}s{flag}")
    if r["throttled"] or r["errors"]:
        break
    time.sleep(1.0)

print()
if safe:
    print(f"  fastest interval with no throttling seen: {safe:.2f}s "
          f"({1/safe:.0f} req/s)")
    markets = 59_093
    reqs = markets * 1.3          # listing pages plus a trade page or two
    for label, iv in (("current pacing", 0.20), ("measured safe", safe)):
        hours = reqs * iv / 3600
        print(f"    {label:<16} {iv:.2f}s -> {hours:5.1f} h for the full "
              f"weather trade backfill")
    print("\n  A ceiling not hit in 25 requests is not a ceiling. Any real")
    print("  job should still back off on 429 rather than trust this.")
else:
    print("  throttled immediately; keep the current pacing")
