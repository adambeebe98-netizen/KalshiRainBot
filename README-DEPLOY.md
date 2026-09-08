# Deploying the bot — the whole path, step by step

You don't need to understand the code. You need to do three things:
get a small always-on computer (a "server"), get three pieces of
information, and paste one script. That's it.

## 1. Get a server

This needs to run on a machine that's always on — not your laptop or phone.
The cheapest reliable option is a small cloud server ("VPS"). Recommended:
**DigitalOcean** — it has a browser-based terminal, so you never need to
install anything or deal with SSH.

1. Go to digitalocean.com, sign up, add a payment method.
2. Click "Create" → "Droplets."
3. Choose the cheapest plan (around $6/month is plenty for this).
4. Choose **Ubuntu** as the operating system (should be the default).
5. Pick any region close to you.
6. Under authentication, choose "Password" if you don't know what SSH keys
   are — it'll email/show you a root password.
7. Click Create. Wait a minute or two for it to boot.
8. Once it's ready, click on your new droplet, then click the **"Console"**
   button near the top — this opens a black terminal window right in your
   browser. This is where everything below happens.

(Linode and Hetzner work the same way with a similar browser console, if
you prefer one of those instead.)

## 2. Have these three things ready

The script will ask you for these one at a time. Get them ready first so
you're not hunting for them mid-setup.

- **Kalshi API Key ID and private key file**
  In your Kalshi account: Settings → API Keys → Create new key.
  Kalshi will show you a Key ID (copy it somewhere) and download a
  **private key file** to your computer — open that file in Notepad/TextEdit,
  you'll paste its full contents (including the `-----BEGIN...` and
  `-----END...` lines) during setup.
- **An Anthropic API key**
  From console.anthropic.com → API Keys → Create key. This is only used
  to help the bot read Kalshi's contract rules text — it doesn't touch money.
- **A number: how much paper money to start with** (just picks the starting
  bankroll for the simulation — no real money involved until you separately
  decide to go live later).

## 3. Paste and run the script

In the browser console (the black terminal window from step 1):

1. Log in as `root` if it asks (it'll tell you the password on screen or
   emailed to you).
2. Paste the entire contents of `deploy.sh` (the file below) into the
   terminal and press Enter. Terminals can be slow with big pastes — give
   it a few seconds after pasting before pressing Enter if it seems stuck.
3. It will run for a minute or two installing things, then start asking
   you questions on screen, in this order:
   - Kalshi API Key ID → paste it, press Enter
   - Your private key contents → paste the whole thing, press Enter, then
     press **Ctrl-D** (this tells it you're done pasting)
   - Anthropic API key → paste it, press Enter
   - Risk mode → type `conservative`, `balanced`, or `aggressive`, or just
     press Enter for the default
   - Starting paper bankroll → type a number like `500`, or press Enter
     for the default
   - Series tickers → press Enter to accept the default unless you know
     you want something else

When it finishes, it prints a "Done" message. The bot is now running in
**paper mode** — no real money — continuously, in the background, and will
keep running even if you close the browser tab or the server restarts.

## 4. Checking on it later

Reopen the Console on your DigitalOcean droplet any time and run:

```
sudo systemctl status kalshi-weather-bot     # is it running?
tail -f ~/kalshi_weather_bot/bot.log         # watch what it's doing live (Ctrl-C to stop watching)
```

## 5. Going live (real money) — later, deliberately

Don't do this until you've let it run in paper mode for a few weeks and
looked at the results. When you're ready, in the console:

```
sudo systemctl stop kalshi-weather-bot
nano ~/kalshi_weather_bot/.env
```

Change `LIVE_TRADING=false` to `LIVE_TRADING=true` and
`LIVE_TRADING_CONFIRMED=false` to `LIVE_TRADING_CONFIRMED=true`, then
press Ctrl-X, Y, Enter to save. Then:

```
sudo systemctl start kalshi-weather-bot
```

That second confirmed flag exists specifically so going live is a
deliberate edit you make on purpose, not something that happens by
accident.

## If something goes wrong

The most common failure is a typo in a pasted key, or DigitalOcean's
Ubuntu version having slightly different package names. If the script
stops with a red error message, copy that exact message and bring it
back here — I can tell you what it means and what to change, you just
won't need to debug it blind.
