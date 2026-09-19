"""What else is on this exchange, and which of it is worth collecting?

Three criteria, in this order, all learned the hard way tonight:

1. DISPLAYED SIZE, not volume. The bracket sweep died because the median
   thinnest leg showed zero contracts. Volume can come from one large
   print a day in a market that is unquotable the rest of the time.
2. INSTANCE COUNT. 612 rain markets could not train anything, and 554 of
   them were one city.
3. SETTLEMENT CLARITY, the one edge this project has established. Not
   measurable, so the rules text is printed for the shortlist.

Two traps, both hit while writing this:

  Field names. The list endpoint returns volume_fp, open_interest_fp and
  yes_bid_size_fp as fixed-point STRINGS, and prices as <field>_dollars.
  market.get("volume") silently returns None, which is how the first
  version concluded the whole exchange had zero volume.

  Pagination. Walking /markets without a series filter never escapes
  KXMVECROSSCATEGORY, which alone has 77,587 open markets -- 80,000 rows
  in and the survey had still not reached a single weather market. So
  this enumerates SERIES first and samples each one.
"""
import collections
import sys

from kalshi_client import KalshiClient

kalshi = KalshiClient()
MARKETS_PER_SERIES = 200
SKIP_PREFIXES = ("KXMVECROSSCATEGORY",)   # 77k markets, zero quoted
RULES: dict[str, tuple[str, str]] = {}


def fp(market, key):
    try:
        return float(market.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def cents(market, key):
    raw = market.get(f"{key}_dollars")
    try:
        return round(float(raw) * 100) if raw is not None else None
    except (TypeError, ValueError):
        return None


print("enumerating series...")
series = []
cursor = None
for _ in range(40):
    resp = kalshi.get_series_list(limit=200, cursor=cursor)
    batch = resp.get("series", []) or resp.get("series_list", [])
    series.extend(batch)
    cursor = resp.get("cursor")
    sys.stdout.write(f"\r  {len(series):,} series")
    sys.stdout.flush()
    if not cursor or not batch:
        break
tickers = sorted({s.get("ticker") for s in series if s.get("ticker")})
tickers = [t for t in tickers if not t.startswith(SKIP_PREFIXES)]
print(f"\n{len(tickers):,} series to sample\n")

stats = collections.defaultdict(lambda: collections.defaultdict(float))
for i, st in enumerate(tickers, 1):
    try:
        resp = kalshi.get_markets(series_ticker=st, status="open",
                                  limit=MARKETS_PER_SERIES)
    except Exception:
        continue
    for m in resp.get("markets", []):
        s = stats[st]
        s["n"] += 1
        s["vol24"] += fp(m, "volume_24h_fp")
        s["oi"] += fp(m, "open_interest_fp")
        bid, ask = cents(m, "yes_bid"), cents(m, "yes_ask")
        depth = min(fp(m, "yes_bid_size_fp"), fp(m, "yes_ask_size_fp"))
        if bid and ask and 0 < bid < ask < 100:
            s["quoted"] += 1
            s["spread"] += ask - bid
            s["depth"] += depth
            if depth >= 100:
                s["deep100"] += 1
        if st not in RULES:
            RULES[st] = (m.get("title", "") or "",
                         m.get("rules_primary", "") or "")
    if i % 50 == 0:
        sys.stdout.write(f"\r  sampled {i}/{len(tickers)} series")
        sys.stdout.flush()
print()

rows = []
for st, s in stats.items():
    n = s["n"]
    if not n:
        continue
    q = s["quoted"] or 1
    rows.append({"series": st, "n": int(n), "vol24": s["vol24"],
                 "oi": s["oi"], "quoted": s["quoted"] / n,
                 "spread": s["spread"] / q, "depth": s["depth"] / q,
                 "deep100": s["deep100"] / n, "vol_per": s["vol24"] / n})

print(f"{len(rows):,} series with open markets\n")
print("=== TOP 25 BY 24H VOLUME ===")
print(f"{'series':<22} {'mkts':>5} {'24h vol':>10} {'vol/mkt':>8} "
      f"{'quoted':>7} {'spread':>7} {'depth':>7} {'>=100':>6}")
for r in sorted(rows, key=lambda r: -r["vol24"])[:25]:
    print(f"{r['series'][:21]:<22} {r['n']:>5} {r['vol24']:>10,.0f} "
          f"{r['vol_per']:>8,.0f} {r['quoted']:>6.0%} {r['spread']:>6.1f}c "
          f"{r['depth']:>7,.0f} {r['deep100']:>5.0%}")

print("\n=== DEEPEST BOOKS (>=20 markets, >=70% quoted) ===")
good = [r for r in rows if r["n"] >= 20 and r["quoted"] >= 0.70]
for r in sorted(good, key=lambda r: -r["depth"])[:20]:
    print(f"{r['series'][:21]:<22} {r['n']:>5} {r['vol_per']:>8,.0f} "
          f"{r['quoted']:>6.0%} {r['spread']:>6.1f}c {r['depth']:>7,.0f} "
          f"{r['deep100']:>5.0%}")

wx = sum(r["vol24"] for r in rows
         if r["series"].startswith(("KXRAIN", "KXHIGH", "KXLOW")))
total = sum(r["vol24"] for r in rows) or 1
print(f"\nweather is {wx/total:.1%} of sampled 24h volume")

print("\n=== SETTLEMENT RULES, DEEPEST NON-WEATHER FAMILIES ===")
shown = 0
for r in sorted(good, key=lambda r: -r["depth"]):
    if r["series"].startswith(("KXRAIN", "KXHIGH", "KXLOW")):
        continue
    title, rules = RULES.get(r["series"], ("", ""))
    print(f"\n{r['series']} -- depth {r['depth']:,.0f}, "
          f"{r['n']} markets, spread {r['spread']:.1f}c")
    print(f"  {title[:90]}")
    print(f"  {rules[:260]}")
    shown += 1
    if shown >= 8:
        break
