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

**Credentials still needing rotation** (flagged, not yet done): GitHub
PAT, Kalshi API key, dashboard password. Live in `.env` on the droplet —
never hardcode them in this repo, including in this file.

## Real-time pipeline status (as of last session)

Both `kalshi-realtime-ws` and `kalshi-realtime-weather` were built,
tested (572 tests passing), and deployed. The weather poller ran clean
the entire time. The WebSocket listener hit and got a real fix for the
`market_lifecycle_v2` flood above, ran clean for a couple minutes after
that fix, but was stopped before fully confirming ticks were landing
correctly in `realtime_ticks` end to end. **That's the actual next
step** — restart both services, let them run for a while, and verify
real rows are accumulating correctly (row counts, spot-check a few
against Kalshi's own displayed prices) before treating this as done.

## Testing

```
python3 -m unittest discover -s tests
```
569+ tests, all passing as of last session. Run the full suite, not
individual files — see the test-isolation gotcha above for why that
distinction actually matters here.

## Data scale for context

Historical backfill: 59,133 markets across 138 weather series (rain +
high/low temp, ~2-year window), fully complete, real settlement values
confirmed against real weather outcomes (all temperature series settle
against The Weather Company, not NWS — mean difference ~0.54°F, not a
material trading concern). This is a one-time, complete dataset —
there's no need to re-run the backfill unless new series appear.
