"""Train Layer 1 and report honestly.

Trains on station-days inside the TRAIN split, evaluates on DEV, and
never touches the VAULT. Reports against three references so the number
means something: a constant base rate, the raw forecast used directly,
and the market's own measured Brier of 0.1391 at 24h.
"""
import datetime as dt
import json
import sys

import splits
import weather_archive
import weatherman
from evaluation import stats


def ts(iso_date: str) -> int:
    return int(dt.datetime.fromisoformat(iso_date)
               .replace(tzinfo=dt.timezone.utc).timestamp())


LEAD_H = int(sys.argv[1]) if len(sys.argv) > 1 else 24
TRAIN_START, TRAIN_END = ts(splits.TRAIN_START), ts(splits.TRAIN_END)
DEV_START, DEV_END = ts(splits.DEV_START), ts(splits.DEV_END)

stations = [s for s in weather_archive.stations_in_use()]
print(f"lead {LEAD_H}h, {len(stations)} stations")
print(f"TRAIN {splits.TRAIN_START} .. {splits.TRAIN_END}")
print(f"DEV   {splits.DEV_START} .. {splits.DEV_END}")
print(f"VAULT {splits.VAULT_START} .. {splits.VAULT_END}  (untouched)\n")

print("fitting climatology on training observations only...")
climo = weatherman.fit_climatology(stations, TRAIN_END)
print(f"  {len(climo)} station-month cells\n")

print("building datasets...")
xtr, ytr, mtr = weatherman.build_dataset(stations, LEAD_H, TRAIN_START,
                                          TRAIN_END, climo)
xdv, ydv, mdv = weatherman.build_dataset(stations, LEAD_H, DEV_START,
                                          DEV_END, climo)
print(f"  train {len(xtr):,} station-days, wet rate {sum(ytr)/max(1,len(ytr)):.3f}")
print(f"  dev   {len(xdv):,} station-days, wet rate {sum(ydv)/max(1,len(ydv)):.3f}")
if not xtr or not xdv:
    print("not enough data; aborting rather than reporting a number built on it")
    raise SystemExit(1)

print("\nfitting...")
model = weatherman.fit(xtr, ytr)
print(model.describe())

idx = {n: i for i, n in enumerate(weatherman.FEATURE_NAMES)}


def report(name, X, Y):
    preds = [model.predict(x) for x in X]
    base = sum(Y) / len(Y)
    const = [base] * len(Y)
    # The forecast used directly, with no model: any forecast rain at all.
    raw = [0.85 if x[idx["fc_precip_sum_mm"]] > 0 else 0.15 for x in X]
    pop = [min(max(x[idx["fc_pop_max"]], 0.001), 0.999) for x in X]
    print(f"\n{name}  (n={len(Y):,}, wet rate {base:.3f})")
    print(f"  {'model':<26} {stats.brier_score(preds, Y):>8.4f}")
    print(f"  {'raw forecast (any rain)':<26} {stats.brier_score(raw, Y):>8.4f}")
    print(f"  {'forecast probability':<26} {stats.brier_score(pop, Y):>8.4f}")
    print(f"  {'constant base rate':<26} {stats.brier_score(const, Y):>8.4f}")
    # Calibration: a model that says 70% must be right 70% of the time,
    # or Layer 2 sizes every position wrong.
    print(f"  calibration by decile:")
    buckets = {}
    for p, y in zip(preds, Y):
        b = min(9, int(p * 10))
        w, n = buckets.get(b, (0, 0))
        buckets[b] = (w + y, n + 1)
    for b in sorted(buckets):
        w, n = buckets[b]
        if n >= 5:
            print(f"    {b/10:.1f}-{(b+1)/10:.1f}  n={n:>5}  "
                  f"predicted~{(b+0.5)/10:.2f}  actual {w/n:.3f}")
    return stats.brier_score(preds, Y)


report("TRAIN (in sample)", xtr, ytr)
dev_brier = report("DEV (out of sample)", xdv, ydv)

print(f"\n  {'market price at 24h (measured)':<30} 0.1391  <- the bar")
if dev_brier < 0.1391:
    print("\n  DEV Brier is below the market's. Treat that as a hypothesis,")
    print("  not a finding: different sample, no fees, no execution, and")
    print("  the harness has not seen it yet.")

path = f"models/weatherman_lead{LEAD_H}.json"
import os
os.makedirs("models", exist_ok=True)
with open(path, "w") as fh:
    fh.write(model.to_json())
print(f"\nsaved {path}")
