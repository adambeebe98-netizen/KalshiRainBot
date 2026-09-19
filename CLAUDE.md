# KalshiRainBot

Kalshi weather prediction-market trading bot. Python + Flask dashboard,
deployed on a DigitalOcean droplet (`68.183.104.17:8080`), paper-trading
against **production** Kalshi (`KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2`
— confirmed live, this is real market data end to end, not a sandbox).

Live paths on the droplet: `/root/kalshi_weather_bot`, DB at `bot_state.db`,
venv at `venv/`.

## Deploying

There is no CI/CD. The real deployment path is:
```
git push                      # from wherever you're editing
ssh <droplet>
cd /root/kalshi_weather_bot && git pull
systemctl restart <service>
```
`deploy/deploy.sh` is a **stale, one-time bootstrap script** embedding a
very old snapshot of the project — it is NOT how the droplet actually
stays updated. Don't trust it as a reference for current file contents.

### Services (systemd, all on the droplet)
- `kalshi-weather-bot` — the live paper-trading bot (`bot.py`). Always running.
- `kalshi-realtime-ws` — WebSocket tick listener (`realtime_kalshi_ws.py`).
  **Currently stopped and disabled** pending a follow-up verification pass
  — see "Real-time pipeline status" below before assuming it's live.
- `kalshi-realtime-weather` — NWS observation poller (`realtime_weather_poller.py`).
  Also currently stopped/disabled alongside the WS listener.
- `kalshi-backfill` — one-time historical backfill (`run_full_backfill.py`).
  Already run to completion; 59,133 markets across 138 series, fully backfilled.
  No reason to run this again unless the series list changes.

`pip install` on the droplet needs `venv/bin/pip`, not system pip —
Debian's PEP 668 blocks system-wide installs (`--break-system-packages`
works too, but the venv is the actual convention already in use here).

## Hard-won gotchas (read before touching related code)

**Kalshi prices are `"<field>_dollars"` decimal STRINGS, never plain
integer cents.** Confirmed on both REST (`/markets`) and the WebSocket
feed (`yes_bid_dollars`, `yes_ask_dollars`, etc). Always go through
`kalshi_client.market_price_cents(payload, field_name)` rather than
reading a field directly — this bit us on REST first, then again on the
WebSocket payload in the exact same way.

**`bot_state.db` is a SHARED SQLite file** between `bot.py` and any other
script that touches it (backfill, real-time listener, one-off diagnostic
scripts). SQLite's own default `busy_timeout` is 0 — an immediate failure
on any write collision, not a wait-and-retry. `storage.get_conn()` sets
`PRAGMA busy_timeout = 5000` for exactly this reason. This was the
confirmed, sole cause of all 117 failures in the original historical
backfill (`"database is locked"`, nothing else) before the fix — if you
ever see that error again, check whether it's calling `get_conn()`.

**`market_lifecycle_v2` (WebSocket) is exchange-wide, not scoped to the
`market_tickers` you subscribed alongside it.** Confirmed live: a single
subscribe call with our ~450 weather tickers still delivered lifecycle
events for random Bitcoin/election/sports markets across the whole
exchange — makes sense in hindsight, a "new market opened" event can't be
scoped to a ticker that doesn't exist yet. `realtime_kalshi_ws.py` filters
these client-side via `is_tracked_series_ticker()` before storing or
acting on them. Don't remove that filter — a burst of ~25 unrelated
markets opening at once is enough to stall the event loop and disconnect
the WebSocket via a starved keepalive ping (happened on the very first
live run).

**Real weather doesn't update second-by-second.** ASOS stations publish
official observations roughly hourly, with occasional ~5-min "special"
reports during rapid changes. There's no meaningful websocket for this —
`realtime_weather_poller.py` polls NWS every ~2 min instead, which is
already more than fast enough to catch every real observation.

**Historical vs real-time tables are deliberately different shapes.**
`historical_price_points`/`historical_weather_points` are one row per
HOURLY candlestick, deduped via `INSERT OR IGNORE` + a UNIQUE constraint
on `(ticker, ts)`. `realtime_ticks` is the opposite on purpose: append-only,
NO unique constraint, because multiple genuine events can land in the
same second on a live market and collapsing them would be real data loss.
`realtime_weather_obs` IS deduped (`UNIQUE(station_code, ts)`), because
repeated 2-min polls between actual new NWS readings really are just
noise, not new data. Know which regime a new table belongs to before
adding one.

**Series discovery is dynamic, not hardcoded.** `kalshi_client.discover_series_tickers()`
+ `SETTINGS.discovery_keywords` (default: `KXRAIN`, `KXHIGH`, `KXLOW`) is
how the bot, the backfill, and the real-time listener all find "our"
markets, rather than a maintained list of 138 series tickers somewhere.
`realtime_kalshi_ws.py`'s lifecycle filter reuses this same keyword logic.

**Test isolation across the whole suite, not just one file.**
`config.SETTINGS` is a frozen dataclass read once per process. `unittest
discover` imports every test file into ONE process, so only the first
file's `use_temp_db()` call actually takes effect — every test file
ends up sharing the same DB despite each calling it. The established
fix (see `tests/test_storage.py`'s `_clear()` helper) is: every test
class that touches a shared table must clear that table in its own
`setUp()`. Skipping this passes when you run one file directly and
fails only under the full `discover` run — always run the full suite
before considering something done, not just the file you touched.

**Settled P&L is NET of real Kalshi fees, and that's load-bearing.**
`settlement.py` and every close path in `shadow.py` subtract the taker
fee before writing `pnl_cents` (entry fee only for a position held to
settlement; entry + exit for one sold early — swing exits, bracket
offloads). `fee_cents_paid` on the row is the total charged, and
`fees_applied_to_pnl=1` marks a row as already corrected. Before this,
P&L was gross, which didn't just inflate the numbers — it biased the
leaderboard toward whichever strategies place the most ORDERS, since the
fee scales with order count, not edge. `rederive_fees.py` applied the
same correction to the 2,059 rows that settled before the change
($216.79 total; four strategies flipped from apparently profitable to
negative). It's idempotent, but it rebuilds bankroll snapshots, so the
bot must be STOPPED while it runs or the running process's in-memory
bankroll overwrites the corrections on its next cycle.

**Credentials still needing rotation** (flagged, not yet done): GitHub
PAT, Kalshi API key, dashboard password. Live in `.env` on the droplet —
never hardcode them in this repo, including in this file.

## The validation vault (read before ANY modelling work)

`splits.py` defines three fixed, hard-coded ranges on
`historical_markets.close_time`:

```
TRAIN   close_time <  2026-02-19                     25,782 markets
DEV     2026-02-19 <= close_time <  2026-04-19       10,921 markets
VAULT   2026-04-19 <= close_time <  2026-07-20       22,430 markets
```

Markets closing on/after 2026-07-20 (everything the live bot is capturing
now) are in NO split — ask for `Split.FUTURE` explicitly if you want them.

**The rule: no result computed on vault data is valid unless it was the
FIRST time that candidate touched the vault.** A second look is not an
out-of-sample estimate any more — it's a number that was selected for,
and the vault is spent for that idea. Training, tuning, feature
selection, threshold picking, and "let me just eyeball whether it
generalizes" all count as touching it.

Enforcement is in code, not in discipline: `splits.load(...)` returns
TRAIN by default and raises `VaultAccessError` on the vault unless called
with `allow_vault=True` AND a non-empty `reason`, which is appended to
the `vault_access_log` table with the calling file:line and row count.
Check `splits.vault_access_history()` before trusting any out-of-sample
number — if the candidate already appears there, it isn't one.

Read historical data through `splits.load()` rather than querying
`historical_markets` / `historical_price_points` /
`historical_weather_points` directly. A raw query bypasses the vault, and
that's exactly the kind of shortcut that's invisible three months later.
The split boundaries are constants on purpose — if they were config
they'd drift the first time a result disappointed.

## What this project is actually for (2026-09-19)

Not the hand-written strategies in `shadow.py` — those are
instrumentation. The goal is high-quality data collection now, feeding a
learned model later, trained on a GPU machine at home.

The thesis comes from how the project started: a $200 loss on "will it
rain in Austin", where it rained at the house and not at the airport
gauge the contract settles on. So the edge being pursued is
**understanding the instrument and the settlement product better than
the people trading it**, not out-forecasting NOAA, which is unwinnable.

The architecture that follows:

- **Layer 1, the weatherman.** Predicts what a specific gauge reports in
  a specific NWS product. Must be trained on decades of station history
  (IEM), *not* on Kalshi outcomes — there are only 612 historical
  daily-rain markets and **554 of them are New York**. Every other city
  has exactly 3.
- **Layer 2, the trader.** Takes Layer 1's calibrated probability,
  compares to price, sizes net of fees.

### The target definition, verified

Rain markets settle on NWS **CLI products** (`CLIAUS`, `CLINYC`, …) —
the official daily climate report for one designated station. The
threshold, on 552 of 612 daily markets, is "strictly greater than 0
inches of precipitation".

**Trace counts.** `analysis/trace_test.py` joins 478 settled NYC markets
to the CLI archive and the separation is perfect: `0.00` settles NO
(268/268), `T` settles **YES** (34/34), a number settles YES (176/176).

Consequently P(measurable rain, ≥0.01") = **36.9%** while P(contract
settles YES) = **44.0%**. A model trained on measurable rainfall predicts
an event 7 points rarer than the one being paid on, on every market, in
the same direction. Define labels against the settlement product, never
against a physical threshold that looks equivalent.

The market already knows this — trace days price at 82c mean / 97c
median 12h out against 21c for dry days. There is no free money there.

### Structural facts about these markets

- **No price data exists beyond ~36h before close.** The tradeable window
  is ~36 hours, not days. A candidate needing a longer lead time has
  nothing to trade, not merely a worse forecast.
- **The market is sharp.** Brier 0.1391 at 24h against 0.2469 for a
  constant. That is the bar, and it is much higher than the earlier
  shadow-sample analysis suggested (that sample was selected on
  disagreement with the price, which flattered the constant).

## The evaluation harness (`evaluation/`)

Nine modules, built 2026-09-19. **Read `evaluation/DESIGN.md` before
touching any of it** — especially section 12 (where the design pushes
back on its own brief) and section 14 (amendments after `analysis/`).

Its job is not to find edge. It is to **make false edge hard to
manufacture**. Everything downstream is only as trustworthy as this is.

- `stats.py` — deflated Sharpe, block bootstrap, Brier. Note: the
  familiar √(2 ln N) overestimates the luck threshold (5.26 vs a true
  4.86 at a million trials); Bailey's estimator is what the code uses.
- `pit.py` — point-in-time access. There is no accessor that returns a
  future row and none that returns the label. `historical_weather_points`
  has UNKNOWN availability and is **refused** unless you pass
  `allow_unverified=True` with a written reason, which is logged.
- `folds.py` — purged, embargoed walk-forward. Label-period overlap
  removal is unconditional; `purge_seconds=0` does not disable it.
- `execution.py` / `objective.py` — fills at the next candle's **ask**,
  no fill in a zero-volume hour (38.6% of them), size capped at 10% of
  volume. Net-of-fees is the only figure any reporting surface emits.
- `registry.py` — `eval_trials` is **append-only, enforced by SQLite
  triggers**. Deleting rows lowers the multiple-testing bar for every
  future result. Do not try to route around this.
- `baselines.py` — market price and an out-of-sample constant. The
  best-heuristic baseline is **not implemented and raises**; `shadow.py`
  is not pure. Every report says so.
- `harness.py` — PASS requires positive net P&L, beating every baseline,
  DSR > 0.95, and the bootstrap gate. There is no "promising" state.
- `worked_example.py` — a real run on TRAIN+DEV.

**Acceptance tests are the point.** 120 provably-random strategies must
produce zero passes, and an oracle must be caught. If
`tests/test_eval_acceptance.py` ever fails, nothing the harness says can
be believed until it passes again.

### First real result

The "market overprices YES" hypothesis from
`analysis/horizon_calibration.py` was run through the harness and
**failed**: net −$19.49 over 58 trades, DSR 0.0785, 81% of bootstrap
resamples at or below zero. It beat guessing the base rate and lost to
not trading. Treat it as closed unless something new turns up.

## Real-time pipeline status (2026-09-19, verified)

Both services restarted and **confirmed working end to end**. Only
weather tickers are landing — KXRAIN, KXLOWT, KXHIGH, zero crypto — so
the `market_lifecycle_v2` fix is genuinely the code that runs. ~2,200
ticks per 5 minutes across 774 markets. DB is now `journal_mode=wal`.

A single failed tick write used to tear down the whole WebSocket
connection (confirmed twice in 27 minutes). Fixed: the write is caught at
the call site, runs off the event loop via `asyncio.to_thread`, and
retries on lock contention.

### Known data gap: no observed rainfall

`precip_last_hour_mm` and `precip_last_3hr_mm` are **always NULL** — 0 of
302 observations. This is NWS, not a bug: `precipitationLastHour` is
absent from the payload entirely, and the raw METAR fallback does not
help (P-group in 0 of 72 observations at KMDW). The parser is correct.

For a point gauge, **do not substitute Open-Meteo's gridded
precipitation** — a model grid cell is not the airport bucket, and that
distinction is the whole $200 lesson in data form. Use the **Iowa
Environmental Mesonet ASOS archive**, which is the same instrument the
contracts settle on.

## Testing

```
venv/bin/python -m unittest discover -s tests
```
797 tests. One known failure: `test_bot.py`'s
`test_real_environment_returns_none_gracefully_when_not_a_git_checkout`
asserts `get_git_commit()` returns None "when not a git checkout", but
the test file lives inside the repo, so it returns a hash. It is an
environment-dependent test, not a code bug — but it means the suite can
never be green, which is worth fixing so that "tests pass" means
something again.

Run the full suite, not individual files — see the test-isolation gotcha
above for why that distinction actually matters here.

## Data scale for context

Historical backfill: 59,133 markets across 138 weather series (rain +
high/low temp, ~2-year window), fully complete, real settlement values
confirmed against real weather outcomes (all temperature series settle
against The Weather Company, not NWS — mean difference ~0.54°F, not a
material trading concern). This is a one-time, complete dataset —
there's no need to re-run the backfill unless new series appear.
