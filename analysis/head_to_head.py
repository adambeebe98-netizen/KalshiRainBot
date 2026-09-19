"""Model versus price, on identical markets.

The training report compares the model's Brier against 0.1391, the
market's measured score. That comparison is not sound: the two numbers
come from different populations -- every station-day with weather data on
one side, only NYC markets that existed and carried a price on the other.

This scores both forecasters on exactly the same markets, at the same
moment, against the same outcomes. It is the only version of the question
worth answering, and the answer is allowed to be disappointing.
"""
import datetime as dt
import sqlite3
import sys

import splits
import weather_archive
import weatherman
from config import SETTINGS
from evaluation import stats

LEAD_H = 24
HOUR = 3600
DAY = 86400


def ts(iso_date):
    return int(dt.datetime.fromisoformat(iso_date)
               .replace(tzinfo=dt.timezone.utc).timestamp())


def close_epoch(m):
    return int(dt.datetime.fromisoformat(
        m["close_time"].replace("Z", "+00:00")).timestamp())


model = weatherman.LogisticModel.from_json(
    open(f"models/weatherman_lead{LEAD_H}.json").read())
climo = weatherman.fit_climatology(weather_archive.stations_in_use(),
                                   ts(splits.TRAIN_END))

conn = sqlite3.connect(SETTINGS.db_path)
conn.execute("PRAGMA busy_timeout = 30000")

for split_name in ("train", "dev"):
    markets = [m for m in splits.load("markets", split=split_name)
               if m.get("measure") == "precipitation_daily"
               and m.get("result") in ("yes", "no")
               and m.get("station_code")]
    rows = []
    caches = {}
    for m in markets:
        try:
            station = weather_archive.asos_id(m["station_code"])
        except weather_archive.UnknownStationError:
            continue
        if station not in caches:
            caches[station] = weatherman.ArchiveCache.load(station, LEAD_H)
        cache = caches[station]
        c = close_epoch(m)
        lo, hi = c - DAY, c
        month = dt.datetime.fromtimestamp(lo, dt.timezone.utc).month
        default = (sum(1 for v in cache.obs.values() if v > 0)
                   / max(1, len(cache.obs)))
        x = weatherman.features_for(cache, lo, hi,
                                    climo.get((station, month), default), LEAD_H)
        if x is None:
            continue
        # The price 24h before close -- what the model would trade against.
        price = conn.execute(
            "SELECT yes_price_cents FROM historical_price_points "
            "WHERE ticker = ? AND ts <= ? AND yes_price_cents IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (m["ticker"], lo)).fetchone()
        if price is None:
            continue
        rows.append((model.predict(x), price[0] / 100.0,
                     1.0 if m["result"] == "yes" else 0.0))

    if not rows:
        print(f"\n{split_name.upper()}: no comparable markets")
        continue
    model_p = [min(max(r[0], 0.001), 0.999) for r in rows]
    price_p = [min(max(r[1], 0.001), 0.999) for r in rows]
    y = [r[2] for r in rows]
    base = sum(y) / len(y)
    print(f"\n{split_name.upper()}  ({len(rows)} markets, "
          f"{'IN SAMPLE' if split_name == 'train' else 'OUT OF SAMPLE'}, "
          f"base rate {base:.3f})")
    print(f"  {'model':<22} {stats.brier_score(model_p, y):>8.4f}")
    print(f"  {'market price':<22} {stats.brier_score(price_p, y):>8.4f}")
    print(f"  {'constant base rate':<22} "
          f"{stats.brier_score([base] * len(y), y):>8.4f}")
    skill = stats.brier_skill_score(model_p, y, price_p)
    print(f"  model skill vs price: {skill:+.3f}  "
          f"({'better' if skill > 0 else 'worse'} than the price)")

    # Where does the model disagree with the price, and who is right?
    big = [(mp, pp, yy) for mp, pp, yy in zip(model_p, price_p, y)
           if abs(mp - pp) > 0.25]
    if big:
        model_right = sum(1 for mp, pp, yy in big
                          if abs(mp - yy) < abs(pp - yy))
        print(f"  disagreements over 25 points: {len(big)}; "
              f"model closer on {model_right} ({model_right / len(big):.1%})")

conn.close()
