# Setting up GitHub so future updates are one command

Right now, every update means re-pasting whole files. This fixes that —
after this one-time setup, updating the bot on your server becomes:

```
cd ~/kalshi_weather_bot && git pull && sudo systemctl restart kalshi-weather-bot kalshi-bot-ui
```

That's the whole future workflow. Here's the one-time setup.

## 1. Create a GitHub account (if you don't have one)

Go to github.com, sign up. Free.

## 2. Create a new repository

1. Click the **+** in the top right → **New repository**
2. Name it something like `kalshi-weather-bot`
3. Leave it **Public** — the code itself never contains your API keys or
   private key (those only ever exist in `.env` and a `.pem` file on your
   server, which are deliberately excluded via `.gitignore` and never
   uploaded), so there's nothing sensitive to protect here, and Public
   avoids needing to set up a login token just to download it later.
4. Don't check any of the "initialize with README" boxes
5. Click **Create repository**

## 3. Upload the code

I've packaged the whole project as `kalshi_weather_bot_github.zip` —
download it, then unzip it on your computer (double-click on Mac/Windows).

On the new repo's GitHub page:
1. Click **"uploading an existing file"** (a link on the empty repo page)
2. Drag the *contents* of the unzipped `kalshi_weather_bot` folder into
   the upload box — all the `.py` files, `README.md`, the `web_ui` folder,
   etc. (Drag the files themselves, not the outer folder, so they land at
   the top level of the repo.)
3. Scroll down, click **Commit changes**

## 4. Connect your existing droplet to this repo

Back in your droplet's Console:

```
cd ~/kalshi_weather_bot
git init
git remote add origin https://github.com/YOUR_USERNAME/kalshi-weather-bot.git
git fetch origin
git checkout -B main origin/main
```

Replace `YOUR_USERNAME` with your actual GitHub username. This makes your
server's code match exactly what's in GitHub, without touching `.env`,
`venv/`, the database, or your private key file — those are all listed in
`.gitignore` so git ignores them completely, both now and on every future
pull.

Then restart both services once to make sure everything's still wired up:

```
sudo systemctl restart kalshi-weather-bot kalshi-bot-ui
```

## 5. From now on, updates work like this

When I give you updated code, instead of pasting files:

1. Upload the changed files to your GitHub repo the same way (or I'll tell
   you exactly which files changed)
2. On your droplet:
   ```
   cd ~/kalshi_weather_bot && git pull && sudo systemctl restart kalshi-weather-bot kalshi-bot-ui
   ```

That's it — one line, no re-pasting, no risk of a paste getting cut off
mid-script like happened during initial setup.
