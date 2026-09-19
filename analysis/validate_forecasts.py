"""Backfill NYC forecasts, then ask whether they have any skill.

The observation archive already reproduces settlement at 98.7%, so we
have a trustworthy target. The question here is whether the forecast --
which is what a model would actually have at decision time -- carries
signal about that target, and how much of it survives to 24 and 48 hours.

Scored against the market's own Brier of 0.1391 at 24h, which is the bar
from analysis/horizon_calibration.py. A forecast feature that cannot beat
a constant is not worth building a model on; one that beats the market
outright would be surprising and should be treated as suspicious until
it survives the harness.
"""
import datetime as dt
import sqlite3
import sys

import forecast_archive
import splits
from config import SETTINGS
from evaluation import stats

STATION = "NYC"


def backfill():
    print("backfilling NYC forecasts from Open-Meteo previous runs...")
    out = forecast_archive.backfill_station(
        STATION, dt.date(2024, 9, 1), dt.date(2026, 9, 18), chunk_days=30)
    print(" ", out)
    for row in forecast_archive.coverage():
        if row["station"] != STATION:
            continue
        first = dt.datetime.fromtimestamp(row["first_at"], dt.timezone.utc).date()
        last = dt.datetime.fromtimestamp(row["last_at"], dt.timezone.utc).date()
        print(f"  lead {row['lead_hours']}h: {row['n']:,} rows, "
              f"{row['wet_hours']:,} wet, {first} .. {last}")


def _series(table, value_col, extra=""):
    conn = sqlite3.connect(SETTINGS.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        return conn.execute(
            f"SELECT valid_at, {value_col} FROM {table} "
            f"WHERE station = ? AND {value_col} IS NOT NULL {extra}",
            (STATION,)).fetchall()
    finally:
        conn.close()


def validate():
    obs = dict(_series("wx_observations", "precip_in"))
    print(f"\nobservations: {len(obs):,} hours")

    markets = [m for m in splits.load("markets", split="train+dev")
               if m.get("measure") == "precipitation_daily"
               and m.get("station_code") == "CLINYC"
               and m.get("result") in ("yes", "no")]
    print(f"markets: {len(markets)}")

    def close_epoch(m):
        return int(dt.datetime.fromisoformat(
            m["close_time"].replace("Z", "+00:00")).timestamp())

    for lead in (24, 48):
        fc = dict(_series("wx_forecasts", "precip_mm",
                          f"AND lead_hours = {lead}"))
        pop = dict(_series("wx_forecasts", "precip_prob_pct",
                           f"AND lead_hours = {lead}"))
        rows = []
        for m in markets:
            c = close_epoch(m)
            lo, hi = c - 86400, c
            # Only use forecast hours that were available before the
            # decision -- the whole point of storing the lead time.
            f_sum = sum(v for t, v in fc.items()
                        if lo <= t < hi and (t - lead * 3600) <= lo)
            p_max = max((v for t, v in pop.items()
                         if lo <= t < hi and (t - lead * 3600) <= lo),
                        default=None)
            o_sum = sum(v for t, v in obs.items() if lo <= t < hi)
            rows.append((f_sum, p_max, o_sum,
                         1.0 if m["result"] == "yes" else 0.0))
        usable = [r for r in rows if r[1] is not None]
        if not usable:
            print(f"\nlead {lead}h: no usable forecast rows")
            continue

        outcomes = [r[3] for r in usable]
        base = sum(outcomes) / len(outcomes)

        # Three forecasts of the same event, scored the same way.
        pop_probs = [min(max(r[1] / 100.0, 0.001), 0.999) for r in usable]
        # Any forecast precipitation at all -> lean yes. Crude on purpose:
        # this measures the raw information content, not a fitted model.
        any_precip = [0.85 if r[0] > 0 else 0.15 for r in usable]
        constant = [base] * len(usable)

        print(f"\nlead {lead}h  ({len(usable)} markets, base rate {base:.3f})")
        print(f"  {'forecast':<28} {'Brier':>8}")
        for name, probs in (("precip probability (raw)", pop_probs),
                            ("any forecast precip", any_precip),
                            ("constant base rate", constant)):
            print(f"  {name:<28} {stats.brier_score(probs, outcomes):>8.4f}")
        print(f"  {'market price (measured)':<28} {'0.1391':>8}"
              f"   <- the bar, at 24h")

        wet_hit = sum(1 for r in usable if r[0] > 0 and r[3] == 1.0)
        wet_total = sum(1 for r in usable if r[0] > 0)
        dry_correct = sum(1 for r in usable if r[0] == 0 and r[3] == 0.0)
        dry_total = sum(1 for r in usable if r[0] == 0)
        print(f"  forecast said rain: {wet_total} days, settled YES "
              f"{wet_hit} ({wet_hit / wet_total:.1%})" if wet_total else "")
        print(f"  forecast said dry:  {dry_total} days, settled NO "
              f"{dry_correct} ({dry_correct / dry_total:.1%})" if dry_total else "")


if __name__ == "__main__":
    if "--skip-backfill" not in sys.argv:
        backfill()
    validate()

