"""Does the market price trace days correctly?

Established: for NYC daily rain, the CLI 'precip' field separates outcomes
perfectly -- 0.00 settles NO (268/268), 'T' settles YES (34/34), a number
settles YES (176/176). So "strictly greater than 0 inches" INCLUDES trace.

That matters because a model predicting measurable rain (>= 0.01") is
predicting the wrong event: it would miss every trace day. The question
here is whether the MARKET makes that mistake too.

Read-only.
"""
import sqlite3
import statistics as st

DB = "/root/kalshi_weather_bot/bot_state.db"
conn = sqlite3.connect(DB, timeout=20)
conn.row_factory = sqlite3.Row

markets = [dict(r) for r in conn.execute(
    "SELECT ticker, date(close_time) d, close_time, result, expiration_value v "
    "FROM historical_markets "
    "WHERE measure='precipitation_daily' AND station_code='CLINYC' "
    "AND expiration_value IS NOT NULL"
)]


def bucket(m):
    if m["result"] == "yes" and m["v"] == 0.0:
        return "YES trace"
    if m["result"] == "yes":
        return "YES measurable"
    return "NO"


# Kalshi close_time is ISO; historical_price_points.ts is a unix int.
import datetime as dt


def close_unix(s):
    s = s.replace("Z", "+00:00")
    try:
        return int(dt.datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


rows = []
for m in markets:
    cu = close_unix(m["close_time"])
    if cu is None:
        continue
    # Price 12h before close: late enough to be informed, early enough that
    # the outcome is usually still genuinely uncertain.
    r12 = conn.execute(
        "SELECT yes_price_cents FROM historical_price_points "
        "WHERE ticker=? AND ts <= ? AND yes_price_cents IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (m["ticker"], cu - 12 * 3600)).fetchone()
    rlast = conn.execute(
        "SELECT yes_price_cents FROM historical_price_points "
        "WHERE ticker=? AND ts <= ? AND yes_price_cents IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (m["ticker"], cu)).fetchone()
    rows.append({
        "bucket": bucket(m), "result": m["result"],
        "p12": r12["yes_price_cents"] if r12 else None,
        "plast": rlast["yes_price_cents"] if rlast else None,
    })
conn.close()

print(f"{len(rows)} markets\n")

print("=== PRICE BY OUTCOME ===")
print(f"{'bucket':<16} {'n':>4} {'n_p12':>6} {'mean p12':>9} {'median':>7} {'mean last':>10}")
for b in ("NO", "YES trace", "YES measurable"):
    sel = [r for r in rows if r["bucket"] == b]
    p12 = [r["p12"] for r in sel if r["p12"] is not None]
    pl = [r["plast"] for r in sel if r["plast"] is not None]
    print(f"{b:<16} {len(sel):>4} {len(p12):>6} "
          f"{(st.mean(p12) if p12 else float('nan')):>9.1f} "
          f"{(st.median(p12) if p12 else float('nan')):>7.1f} "
          f"{(st.mean(pl) if pl else float('nan')):>10.1f}")

print()
print("=== MARKET CALIBRATION, 12H BEFORE CLOSE ===")
have = [r for r in rows if r["p12"] is not None]
if have:
    implied = st.mean(r["p12"] for r in have) / 100.0
    realized = sum(1 for r in have if r["result"] == "yes") / len(have)
    realized_measurable = sum(
        1 for r in have if r["bucket"] == "YES measurable") / len(have)
    print(f"  n = {len(have)}")
    print(f"  mean market-implied P(yes) = {implied:.3f}")
    print(f"  realized P(yes)            = {realized:.3f}")
    print(f"  realized P(measurable)     = {realized_measurable:.3f}  "
          f"<- what a >=0.01in model would predict")
    print(f"  market minus realized      = {implied - realized:+.3f}")
    print(f"  trace share of all markets = "
          f"{sum(1 for r in have if r['bucket'] == 'YES trace') / len(have):.3f}")

print()
print("=== BUYING YES ON EVERY TRACE DAY AT THE 12H PRICE ===")
tr = [r["p12"] for r in rows if r["bucket"] == "YES trace" and r["p12"] is not None]
if tr:
    cost = sum(tr)
    payout = 100 * len(tr)
    print(f"  {len(tr)} contracts, mean entry {st.mean(tr):.1f}c, "
          f"gross P&L {payout - cost}c over {len(tr)} trades "
          f"({(payout - cost) / len(tr):.1f}c/trade)")
    print("  (hindsight only -- you cannot know in advance which day is trace)")
