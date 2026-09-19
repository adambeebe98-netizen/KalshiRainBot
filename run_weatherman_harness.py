"""Put Layer 1 through the harness.

Everything up to now has been Brier scores, which say how well the model
forecasts. This asks the only question that pays: after realistic fills,
after fees, against every baseline, and counted against every candidate
ever looked at -- is there anything here?

The answer is allowed to be no. That is what the harness is for.
"""
import datetime as dt
import sys

import splits
import weather_archive
import weatherman
import weatherman_forecaster
from evaluation import baselines, execution, harness, registry

LEAD_H = 24
SOURCES = ("historical_price_points", "wx_observations", "wx_forecasts")


def ts(iso_date):
    return int(dt.datetime.fromisoformat(iso_date)
               .replace(tzinfo=dt.timezone.utc).timestamp())


markets = [m for m in splits.load("markets", split="train+dev")
           if m.get("measure") == "precipitation_daily"
           and m.get("result") in ("yes", "no")]
print(f"{len(markets)} settled daily-rain markets in TRAIN+DEV")

print("fitting climatology on training observations only...")
climo = weatherman.fit_climatology(weather_archive.stations_in_use(),
                                   ts(splits.TRAIN_END))

forecaster = weatherman_forecaster.load(
    f"models/weatherman_lead{LEAD_H}.json", lead_hours=LEAD_H, climatology=climo)
candidate = baselines.ProbabilityTrader(forecaster)

print(f"\ntrials before this run: {registry.trials_to_date()}")
print("-" * 72)

report = harness.evaluate(
    candidate, markets,
    execution.HourlyCandleExecution(participation_rate=0.10),
    config={"candidate": candidate.name, "lead_hours": LEAD_H,
            "measure": "precipitation_daily",
            "model": forecaster.model.to_json()},
    seed=20260919, n_folds=3, horizons_s=(LEAD_H * 3600,),
    splits_used="train+dev", sources=SOURCES)

print(report.report())
print("-" * 72)
print("per fold:")
for r in report.fold_results:
    print(f"  {r.summary()}")
