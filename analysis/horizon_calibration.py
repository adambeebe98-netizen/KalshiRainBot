"""Is the NYC rain market calibrated, and at what forecast horizon?

The 12h result (implied 50.8% vs realized 44.0%) is suspect because by
then the weather has usually already happened -- median prices of 7c and
97c describe a resolved market, not a forecasting problem. This walks the
horizon back to where genuine uncertainty remains.

Deliberately NOT a strategy search. It reports calibration and the market's
own Brier score per horizon, which is the baseline any future model has to
beat. Mining these buckets for a trading rule would be exactly the
multiple-testing trap that makes backtests lie -- 6 horizons x 5 buckets is
30 comparisons, and the best of 30 noise draws looks good by construction.

Read-only.
"""
import datetime as dt
import sqlite3
import statistics as st

DB = "/root/kalshi_weather_bot/bot_state.db"
HORIZONS_H = [6, 12, 24, 36, 48, 72, 96]

conn = sqlite3.connect(DB, timeout=20)
conn.row_factory = sqlite3.Row
markets = [dict(r) for r in conn.execute(
    "SELECT ticker, close_time, result, expiration_value v "
    "FROM historical_markets "
    "WHERE measure='precipitation_daily' AND station_code='CLINYC' "
    "AND expiration_value IS NOT NULL"
)]


def close_unix(s):
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def price_at(ticker, cutoff):
    r = conn.execute(
        "SELECT yes_price_cents FROM historical_price_points "
        "WHERE ticker=? AND ts <= ? AND yes_price_cents IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (ticker, cutoff)).fetchone()
    return r["yes_price_cents"] if r else None


print(f"{len(markets)} NYC daily-rain markets with a settled value\n")
print(f"{'horizon':>8} {'n':>5} {'cover':>6} {'implied':>8} {'realized':>9} "
      f"{'diff':>7} {'SE':>6} {'z':>6} {'Brier':>7}")
print("-" * 70)

per_horizon = {}
for h in HORIZONS_H:
    pairs = []
    for m in markets:
        cu = close_unix(m["close_time"])
        if cu is None:
            continue
        p = price_at(m["ticker"], cu - h * 3600)
        if p is None:
            continue
        pairs.append((p / 100.0, 1.0 if m["result"] == "yes" else 0.0))
    if len(pairs) < 30:
        print(f"{h:>6}h {len(pairs):>5}  too few")
        continue
    implied = st.mean(p for p, _ in pairs)
    realized = st.mean(o for _, o in pairs)
    diffs = [p - o for p, o in pairs]
    se = st.stdev(diffs) / (len(diffs) ** 0.5)
    brier = st.mean((p - o) ** 2 for p, o in pairs)
    per_horizon[h] = pairs
    print(f"{h:>6}h {len(pairs):>5} {len(pairs)/len(markets):>6.2f} "
          f"{implied:>8.3f} {realized:>9.3f} {implied-realized:>+7.3f} "
          f"{se:>6.3f} {(implied-realized)/se:>6.2f} {brier:>7.4f}")

print()
print("NOTE ON THE z COLUMN: it assumes independent markets. Consecutive")
print("days of NYC rain are strongly autocorrelated, so the effective")
print("sample is materially smaller than n and these z values are")
print("optimistic. Treat 2-3 as suggestive, not conclusive.")

# Calibration curve at the longest horizon with decent coverage.
best_h = max((h for h in per_horizon if len(per_horizon[h]) >= 200), default=None)
if best_h:
    print(f"\n=== CALIBRATION CURVE AT {best_h}H ===")
    pairs = per_horizon[best_h]
    edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.01]
    print(f"{'price band':>12} {'n':>5} {'mean price':>11} {'realized':>9} {'diff':>7}")
    for lo, hi in zip(edges, edges[1:]):
        sel = [(p, o) for p, o in pairs if lo <= p < hi]
        if not sel:
            continue
        mp = st.mean(p for p, _ in sel)
        mo = st.mean(o for _, o in sel)
        print(f"{lo:.1f}-{hi:.1f}".rjust(12) + f" {len(sel):>5} "
              f"{mp:>11.3f} {mo:>9.3f} {mp-mo:>+7.3f}")

print("\n=== BASELINES TO BEAT (at each horizon) ===")
print("Any Layer 1 model must beat the market's Brier score above, and must")
print("also beat a constant predictor. Constant = realized base rate, which")
print("is only knowable in hindsight, so it is a floor rather than a rival.")
for h, pairs in per_horizon.items():
    base = st.mean(o for _, o in pairs)
    const_brier = st.mean((base - o) ** 2 for _, o in pairs)
    mkt_brier = st.mean((p - o) ** 2 for p, o in pairs)
    verdict = "market better" if mkt_brier < const_brier else "CONSTANT BETTER"
    print(f"  {h:>3}h  market {mkt_brier:.4f}  constant {const_brier:.4f}  -> {verdict}")

conn.close()
