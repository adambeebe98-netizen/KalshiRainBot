"""Can this data support swing trading at all?

Every candidate run so far buys and holds to settlement. A swing trade is
a different animal: it needs the price to MOVE while the market is open,
and it needs to get in and out, paying the spread and a fee at each end.

The question is not whether a clever swing rule exists. It is whether the
raw arithmetic leaves room for one. Three numbers decide it:

1. How far does a market's price actually travel while it is open?
2. What does a round trip cost -- spread plus two fees?
3. How many hours are even tradeable, given 38.6% of archive hours have
   no volume at all?

If typical movement is smaller than the round-trip cost, no rule can
help and the search is over before it starts. If it is several times
larger, swing trading is genuinely untested rather than dead, and the
harness needs an exit path before anything can be said.
"""
import datetime as dt
import sqlite3
import statistics as st
from collections import defaultdict

import fees
import splits
from config import SETTINGS

conn = sqlite3.connect(SETTINGS.db_path)
conn.execute("PRAGMA busy_timeout = 30000")
conn.row_factory = sqlite3.Row

for measure in ("precipitation_daily", "temperature_high"):
    markets = [m for m in splits.load("markets", split="train+dev")
               if m.get("measure") == measure and m.get("result") in ("yes", "no")]
    print(f"\n{'='*70}")
    print(f"{measure}: {len(markets):,} settled markets")

    ranges, spans, costs, tradeable_hours, total_hours = [], [], [], [], []
    round_trips = []
    sampled = 0
    for m in markets[:1500]:
        rows = conn.execute(
            "SELECT ts, yes_price_cents, yes_bid_cents, yes_ask_cents, volume "
            "FROM historical_price_points WHERE ticker = ? ORDER BY ts",
            (m["ticker"],)).fetchall()
        prices = [r["yes_price_cents"] for r in rows
                  if r["yes_price_cents"] is not None]
        if len(prices) < 4:
            continue
        sampled += 1
        total_hours.append(len(rows))
        live = [r for r in rows if (r["volume"] or 0) > 0
                and r["yes_bid_cents"] is not None
                and r["yes_ask_cents"] is not None]
        tradeable_hours.append(len(live))
        ranges.append(max(prices) - min(prices))
        # Largest move actually capturable: best sell after any buy.
        best = 0
        running_min = prices[0]
        for p in prices[1:]:
            best = max(best, p - running_min)
            running_min = min(running_min, p)
        spans.append(best)
        if live:
            spread = st.median(r["yes_ask_cents"] - r["yes_bid_cents"]
                               for r in live)
            mid = st.median((r["yes_ask_cents"] + r["yes_bid_cents"]) / 2
                            for r in live)
            # A round trip at size 10: cross the spread once, pay two fees.
            entry_fee = fees.taker_fee_cents(10, int(mid)) / 10.0
            exit_fee = fees.taker_fee_cents(10, int(mid)) / 10.0
            costs.append(spread)
            round_trips.append(spread + entry_fee + exit_fee)

    if not sampled:
        print("  no usable markets")
        continue

    def show(name, vals, unit="c"):
        vals = sorted(vals)
        print(f"  {name:<34} median {st.median(vals):>6.1f}{unit}  "
              f"p25 {vals[len(vals)//4]:>5.1f}  p75 {vals[3*len(vals)//4]:>5.1f}")

    print(f"  sampled {sampled:,} markets")
    show("price range over life", ranges)
    show("best capturable move (buy->sell)", spans)
    show("median spread", costs)
    show("round-trip cost (spread + 2 fees)", round_trips)
    show("hourly candles per market", total_hours, "")
    show("tradeable hours per market", tradeable_hours, "")

    if round_trips and spans:
        ratio = st.median(spans) / st.median(round_trips)
        live_share = st.median(tradeable_hours) / max(1, st.median(total_hours))
        print(f"\n  best move / round-trip cost = {ratio:.1f}x")
        print(f"  share of hours tradeable    = {live_share:.0%}")
        if ratio < 1.5:
            print("  -> the arithmetic does not leave room. No rule fixes this.")
        elif ratio < 4:
            print("  -> tight. A swing rule would have to be right often to")
            print("     clear the round trip, but it is not arithmetically dead.")
        else:
            print("  -> room exists. Swing trading is untested here, not dead,")
            print("     and the harness needs an exit path to test it.")

conn.close()
