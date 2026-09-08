# Kalshi Weather Bot

A bot that scans Kalshi weather markets (rain/precipitation, temperature),
figures out the exact station/source/threshold each contract actually
settles on, compares that to real weather data, and trades when there's a
meaningful gap between its estimate and the market price.

**Starts in paper-trading mode by default and stays there until you
explicitly flip it live.** Read the whole README before you do that.

## Why this design

The thing that looked like a free inefficiency — "it was raining outside
but the contract said no" — is a real, structural feature of these
markets: they settle on ONE named station and data provider, and trace
amounts of rain count as zero. That part of your instinct is correct.

But it's not a secret. It's written in every contract's rules. So the bot
isn't built to fade "people online said it was raining" — it's built to
estimate, station by station, whether the *specific* settlement source
will show measurable precipitation, and only trade when that estimate
disagrees with the market price by more than a set margin. Some of those
disagreements will be wrong. That's expected — the point is that they're
wrong less often than the edge you're demanding, over enough trades.

## Setup

1. `pip install -r requirements.txt`
2. Generate an RSA keypair and create an API key in your Kalshi account
   (Settings > API Keys). Kalshi's exact flow and API base URL have moved
   around — confirm both at https://docs.kalshi.com before trusting the
   defaults in `.env.example`.
3. Copy `.env.example` to `.env` and fill in:
   - `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`
   - `ANTHROPIC_API_KEY` (only used to parse rules text into structured fields)
4. Adjust `SERIES_TICKERS` to the actual series you want to scan — pull the
   current list from the Kalshi markets UI, the placeholders here are
   illustrative.
5. `python bot.py --once` to run a single scan cycle in paper mode and
   sanity-check the output before letting it loop continuously.

## What to check before trusting any of this

- **Spot-check `rules_cache.json`** against the real rules PDF for a
  handful of markets. The Anthropic-based extraction is the single point
  of failure for the whole system — if it gets the station or threshold
  wrong, everything downstream is wrong too, confidently.
- **`weather_data.STATION_REFERENCE`** only has a handful of cities. Add
  lat/lon for any station you actually want to trade, or the bot will
  silently fall back to a 50/50 "no data" estimate that will almost never
  clear your edge threshold (which is a safe failure mode, but means it's
  doing nothing for that market).
- **Query `calibration_stats` periodically** once you have real settled
  history. `sum_predicted/n` vs `sum_actual/n` per station tells you
  directly whether the model is over- or under-confident there, and by
  how much — this is the same number the bot itself uses to correct
  future predictions.
- **Run in paper mode for a real stretch — weeks, not hours** — before
  going live. `decisions` and `trades` tables in `bot_state.db` let you
  compute win rate, average edge, and realized vs. modeled probability.
  If the model's stated probabilities don't roughly match outcome
  frequency over time (e.g. things it calls 70% don't happen ~70% of the
  time), the model needs work before it should touch real money.

## Running unattended, day after day

New daily markets don't need any manual step — each scan cycle queries
`status=open` for your configured series tickers, and Kalshi rolls new
daily contracts into the same series automatically. The bot picks them up
on its own.

To actually leave it running with little upkeep, deploy it as a background
service rather than a terminal window:

```
cd deploy/
# edit paths and username in kalshi-weather-bot.service, then:
sudo cp kalshi-weather-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kalshi-weather-bot
sudo systemctl start kalshi-weather-bot
journalctl -u kalshi-weather-bot -f   # watch it run
```

This needs a machine that's on all the time — a small VPS (e.g. a $5-6/mo
Linode/DigitalOcean box) works well; a laptop that sleeps or closes does
not. `Restart=on-failure` means systemd brings it back up if it crashes,
and the bot itself now survives transient API errors (logs and backs off
instead of dying) and remembers its bankroll across restarts by reading
the last snapshot from `bot_state.db`.

"Little to no maintenance" doesn't mean zero — check in periodically:
- `journalctl -u kalshi-weather-bot -f` or `bot.log` for errors
- Query `bot_state.db` (`decisions`, `trades`, `bankroll_snapshots` tables)
  for a rough sense of whether the model's calibration is holding up
- The bot halts itself after 5 consecutive failed cycles in a row rather
  than looping forever silently broken — treat that as a "come look at
  this" signal, not something to auto-restart blindly

## Risk modes

Set `RISK_MODE` in `.env` to `conservative`, `balanced`, or `aggressive` —
it picks sane presets for minimum required edge and position sizing (see
`config.py` for exact numbers). Setting `MIN_EDGE_CENTS`, `MAX_POSITION_PCT`,
or `MAX_DAILY_LOSS_PCT` explicitly always overrides the preset for that one
value, so you can start from a preset and tweak just what you want.

## How it learns

Every settled trade feeds `calibration.py`, which keeps a running average
of predicted-vs-actual outcomes **per station**. If Austin's rain contracts
have historically resolved yes less often than the model predicted — the
"Austin rain is weird" pattern — that shows up automatically as a negative
bias once enough Austin trades have settled, and gets applied to future
Austin predictions.

Two things worth understanding about this, honestly:

- **It's a running average, not a trained model.** You can query
  `calibration_stats` in the database directly and see exactly why a
  station's estimates shifted — there's no hidden layer of weights you
  can't inspect. That's deliberate: with real money on the line, being
  able to explain *why* the bot changed its mind matters more than squeezing
  out extra accuracy from a fancier method.
- **It needs real sample size.** With roughly one rain contract per city
  per day, 20 settled trades (the minimum before any correction kicks in)
  takes about three weeks per station, minimum, and needs the station to
  actually have contracts trading that whole time. Expect the "learning"
  to show up over a season, not days. `settlement.py` is what feeds this —
  it runs every cycle, checking whether previously-opened trades have
  resolved yet.

## Going live

```
python bot.py --live
```

Requires `LIVE_TRADING=true` in `.env` AND typing a confirmation phrase at
the prompt. The daily loss kill switch (`MAX_DAILY_LOSS_PCT`, default 6%)
stops new trades for the rest of the day once tripped — it does not
auto-close open positions.

## Honest limitations

- The probability model is a simple, transparent heuristic (recent
  observation + NWS forecast POP), not a trained model. That's
  intentional — you need a baseline you can audit before adding
  complexity, and there's no real backtest history yet to train on.
- NWS point forecasts are a proxy, not the literal settlement feed, for
  markets that settle via The Weather Company. Where `settlement_source`
  isn't NWS, treat the model's confidence accordingly.
- This is real-money automated trading. Nothing here is a guarantee of
  profitability, and I'm not able to tell you it will make money — only
  that the plumbing and risk controls are sound. Paper-trade it, look at
  the actual numbers, and size any live bankroll as money you can afford
  to lose entirely.
