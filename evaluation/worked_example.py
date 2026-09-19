"""
A worked run, end to end, on real TRAIN+DEV data.

The candidate is not invented for the demo. `analysis/horizon_calibration.py`
found the NYC rain market pricing YES above its realized rate at every
horizon -- +4.6 to +10.1 points, largest at 24h -- and that finding was
recorded as a hypothesis rather than an edge: one station, 468
autocorrelated markets, four horizons examined. This is that hypothesis
going through the machinery that decides whether it is real.

The candidate forecasts `market mid - delta`, i.e. it believes the market
is systematically too bullish by `delta` and is otherwise willing to
accept the price. If the bias is real and large enough to clear fees, this
should find it.

Scoped to precipitation_daily on purpose. The archive is 98% temperature
markets, and running every TRAIN market would load millions of candle rows
into a 1 GB droplet. The hypothesis is about rain markets anyway --
testing it on temperature would be testing something else and calling it
the same thing.

Run:  venv/bin/python -m evaluation.worked_example
"""
from __future__ import annotations

import sys

import splits
from evaluation import baselines, execution, harness, registry

DELTA = 0.10          # how much the market is hypothesised to overprice YES
HORIZON_S = 24 * 3600  # where the measured bias was largest


class FadeMarketForecaster(baselines.Forecaster):
    """Market mid, shaded down by a fixed amount.

    Deliberately the simplest expression of the hypothesis. A richer
    candidate would be harder to interpret: if it passed, you would not
    know whether the bias or the extra machinery earned it.
    """

    def __init__(self, delta: float = DELTA):
        self.delta = delta
        self.name = f"fade_yes_bias(delta={delta:.2f})"
        self._market = baselines.MarketForecaster()

    def probability(self, view, terms, as_of):
        p = self._market.probability(view, terms, as_of)
        return None if p is None else max(0.0, min(1.0, p - self.delta))


def main() -> int:
    markets = [m for m in splits.load("markets", split="train+dev")
               if m.get("measure") == "precipitation_daily"]
    print(f"{len(markets)} precipitation_daily markets in TRAIN+DEV")
    if len(markets) < 100:
        print("too few markets to fold; aborting rather than reporting a "
              "number built on a handful of outcomes")
        return 1

    candidate = baselines.ProbabilityTrader(FadeMarketForecaster())
    model = execution.HourlyCandleExecution(participation_rate=0.10)

    print(f"\ntrials before this run: {registry.trials_to_date()}")
    print("-" * 72)

    report = harness.evaluate(
        candidate, markets, model,
        config={"candidate": candidate.name, "delta": DELTA,
                "horizon_s": HORIZON_S, "measure": "precipitation_daily"},
        seed=20260919, n_folds=3, horizons_s=(HORIZON_S,),
        splits_used="train+dev")

    print(report.report())
    print("-" * 72)
    print("per fold:")
    for r in report.fold_results:
        print(f"  {r.summary()}")

    print()
    print("This run has been recorded in eval_trials. Every future deflated")
    print("score is measured against a count that now includes it, whether")
    print("or not the result was interesting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
