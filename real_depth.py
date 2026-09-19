"""How much size is actually resting on these books?

The sweep needs roughly 500 contracts a leg to clear the fee. The archive
could only answer with 10% of hourly VOLUME -- what traded, not what was
available -- which is a stand-in, not a measurement.

The live tick feed carries yes_bid_size_fp and yes_ask_size_fp: the size
actually displayed at the touch. That is the real constraint, and it is
now sitting in the tick archive.
"""
import json
import statistics as st
from collections import defaultdict

import tick_archive

by_ticker_ask = defaultdict(list)
by_ticker_bid = defaultdict(list)
n_rows = 0

for day in tick_archive.archived_days():
    for rec in tick_archive.read_day(day):
        msg = rec.get("m")
        if not isinstance(msg, dict) or msg.get("type") != "ticker":
            continue
        body = msg.get("msg") or {}
        ticker = body.get("market_ticker")
        if not ticker:
            continue
        n_rows += 1
        for field, sink in (("yes_ask_size_fp", by_ticker_ask),
                            ("yes_bid_size_fp", by_ticker_bid)):
            raw = body.get(field)
            if raw is None:
                continue
            try:
                sink[ticker].append(float(raw))
            except (TypeError, ValueError):
                pass

print(f"{n_rows:,} ticker messages across "
      f"{len(tick_archive.archived_days())} archived days")
print(f"{len(by_ticker_ask):,} tickers with a displayed ask size\n")

all_ask = [v for vals in by_ticker_ask.values() for v in vals]
all_bid = [v for vals in by_ticker_bid.values() for v in vals]
if not all_ask:
    print("no size data found")
    raise SystemExit(0)


def describe(name, vals):
    vals = sorted(vals)
    def pct(p):
        return vals[min(len(vals) - 1, int(len(vals) * p))]
    print(f"{name}: n={len(vals):,}  median {st.median(vals):,.0f}  "
          f"p75 {pct(0.75):,.0f}  p90 {pct(0.90):,.0f}  "
          f"p99 {pct(0.99):,.0f}  max {vals[-1]:,.0f}")


describe("displayed ask size", all_ask)
describe("displayed bid size", all_bid)

print("\nThe sweep buys NO on every leg, which lifts the NO ask -- that is")
print("the YES BID side of the book. So the bid sizes above are the")
print("constraint, and they are per-leg.")

print("\n=== WHAT FRACTION OF THE BOOK SUPPORTS A REAL SWEEP? ===")
for threshold in (10, 50, 100, 500, 2000):
    share = sum(1 for v in all_bid if v >= threshold) / len(all_bid)
    print(f"  quotes showing >= {threshold:>5} contracts: {share:>6.2%}")

print("\n=== PER-EVENT: the thinnest leg is what caps the sweep ===")
events = defaultdict(dict)
for ticker, vals in by_ticker_bid.items():
    if "-" not in ticker:
        continue
    event = ticker.rsplit("-", 1)[0]
    events[event][ticker] = st.median(vals)

sized = []
for event, legs in events.items():
    if len(legs) >= 4:
        sized.append((event, len(legs), min(legs.values())))
if sized:
    mins = sorted(s[2] for s in sized)
    print(f"  {len(sized):,} events with 4+ legs quoting")
    print(f"  thinnest leg, median across events: {st.median(mins):,.0f} "
          f"contracts")
    print(f"  p75 {mins[int(len(mins)*0.75)]:,.0f}   "
          f"p90 {mins[int(len(mins)*0.90)]:,.0f}   max {mins[-1]:,.0f}")
    for need in (100, 500, 2000):
        ok = sum(1 for m in mins if m >= need)
        print(f"  events where EVERY leg shows >= {need:>4}: "
              f"{ok:,} ({ok/len(mins):.1%})")
