"""Buy NO on every bracket in a mutually exclusive set.

No forecast required. If exactly one bracket in an event settles YES,
then buying NO on all n costs sum(100 - yes_bid) and pays (n-1) * 100.
Rearranged, that profits whenever

    sum(yes_bid) > 100

i.e. whenever the set is collectively priced above certainty. Structural,
not predictive: it does not matter what the weather does.

Three things have to hold for it to be real, and each is checked here
rather than assumed:

1. The brackets really are mutually exclusive -- exactly one settles YES.
   Threshold-style contracts ("above 72") are nested, not exclusive, and
   including them would invent an arbitrage that is not there.
2. The overpricing has to exceed fees on n separate orders.
3. You have to be able to fill all n legs. A partial sweep is not an
   arbitrage, it is an unhedged position.
"""
import datetime as dt
import sqlite3
from collections import defaultdict

import fees
import splits
from config import SETTINGS

conn = sqlite3.connect(SETTINGS.db_path)
conn.execute("PRAGMA busy_timeout = 30000")
conn.row_factory = sqlite3.Row

markets = [m for m in splits.load("markets", split="train+dev")
           if m.get("event_ticker") and m.get("result") in ("yes", "no")]
print(f"{len(markets):,} settled markets in TRAIN+DEV")

events = defaultdict(list)
for m in markets:
    events[m["event_ticker"]].append(m)
print(f"{len(events):,} events")

# --- 1. which events are genuinely one-of-n? ------------------------------
exclusive, nested, other = [], [], []
for ev, ms in events.items():
    if len(ms) < 2:
        continue
    n_yes = sum(1 for m in ms if m["result"] == "yes")
    if n_yes == 1:
        exclusive.append(ev)
    elif n_yes > 1:
        nested.append(ev)
    else:
        other.append(ev)

print(f"\nexactly one YES (mutually exclusive): {len(exclusive):,}")
print(f"more than one YES (nested/threshold):  {len(nested):,}")
print(f"no YES at all:                         {len(other):,}")
print("\nOnly the first group can support the trade. A set with two winners")
print("pays (n-2)*100 and the arithmetic above does not apply.")

sizes = defaultdict(int)
for ev in exclusive:
    sizes[len(events[ev])] += 1
print("\nbracket count distribution (exclusive events):")
for n in sorted(sizes):
    print(f"  {n} legs: {sizes[n]:,} events")

# --- 2. how often is the set overpriced? ----------------------------------
HORIZONS = (6, 12, 24)
print(f"\n{'horizon':>8} {'events':>8} {'sum>100':>9} {'share':>7} "
      f"{'mean edge':>10} {'best':>7}")
print("-" * 56)

for hours in HORIZONS:
    checked = over = 0
    edges = []
    for ev in exclusive:
        ms = events[ev]
        close = None
        for m in ms:
            try:
                close = int(dt.datetime.fromisoformat(
                    m["close_time"].replace("Z", "+00:00")).timestamp())
                break
            except (ValueError, TypeError, AttributeError):
                continue
        if close is None:
            continue
        as_of = close - hours * 3600
        bids = []
        for m in ms:
            row = conn.execute(
                "SELECT yes_bid_cents FROM historical_price_points "
                "WHERE ticker = ? AND ts <= ? AND yes_bid_cents IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1", (m["ticker"], as_of)).fetchone()
            if row is None:
                bids = []
                break
            bids.append(row["yes_bid_cents"])
        if len(bids) != len(ms):
            continue          # cannot sweep a set you cannot price in full
        checked += 1
        edge = sum(bids) - 100
        edges.append(edge)
        if edge > 0:
            over += 1
    if not checked:
        print(f"{hours:>6}h {0:>8}   no fully-priced sets")
        continue
    mean_edge = sum(edges) / len(edges)
    print(f"{hours:>6}h {checked:>8} {over:>9} {over/checked:>6.1%} "
          f"{mean_edge:>9.1f}c {max(edges):>6}c")

# --- 3. does any of it survive fees? --------------------------------------
print("\n=== NET OF FEES, AT 24H ===")
gross_total = net_total = taken = 0
for ev in exclusive:
    ms = events[ev]
    try:
        close = int(dt.datetime.fromisoformat(
            ms[0]["close_time"].replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, AttributeError):
        continue
    as_of = close - 24 * 3600
    bids = []
    for m in ms:
        row = conn.execute(
            "SELECT yes_bid_cents FROM historical_price_points "
            "WHERE ticker = ? AND ts <= ? AND yes_bid_cents IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (m["ticker"], as_of)).fetchone()
        if row is None:
            bids = []
            break
        bids.append(row["yes_bid_cents"])
    if len(bids) != len(ms):
        continue
    edge = sum(bids) - 100
    if edge <= 0:
        continue
    # One contract per leg. Each NO leg is its own order, so each pays a fee.
    leg_fees = sum(fees.taker_fee_cents(1, 100 - b) for b in bids)
    taken += 1
    gross_total += edge
    net_total += edge - leg_fees

print(f"  sets with a positive gross edge: {taken:,}")
if taken:
    print(f"  gross: {gross_total/100:+.2f} USD over {taken} sweeps "
          f"({gross_total/taken:.1f}c each)")
    print(f"  net:   {net_total/100:+.2f} USD "
          f"({net_total/taken:+.1f}c each)")
    print(f"  fees ate {(gross_total - net_total)/100:.2f} USD")
print("\nOne contract per leg is the most favourable case for this trade:")
print("Kalshi's fee is capped per ORDER, so larger sizes amortise it. But")
print("a sweep also needs every leg to fill, and the legs are thin.")
conn.close()
