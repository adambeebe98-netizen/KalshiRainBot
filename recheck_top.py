"""Re-judge the sweep's strongest candidate against the corrected bar.

The 525-candidate sweep ran while the luck threshold was SR 57 -- a
broken value that no strategy could ever clear, so every DSR came back
0.000 and told us nothing. The threshold is now SR 1.150.

Re-running all 525 would add another 525 permanent trials to make a
point about one of them, so this re-evaluates only the candidate that
led on net P&L. It costs one trial, which is the honest price.
"""
import candidates as candidate_lib
import splits
from evaluation import baselines, execution, harness, registry

markets = [m for m in splits.load("markets", split="train+dev")
           if m.get("measure") == "precipitation_daily"
           and m.get("result") in ("yes", "no")]

top = baselines.ProbabilityTrader(
    candidate_lib.MomentumForecaster(lookback_h=12, strength=0.5))

print(f"re-judging {top.name}")
print(registry.trial_banner())
print("-" * 70)

report = harness.evaluate(
    top, markets, execution.HourlyCandleExecution(participation_rate=0.10),
    config={"candidate": top.name, "measure": "precipitation_daily",
            "note": "recheck after luck-threshold fix"},
    seed=20260919, n_folds=3)
print(report.report())
print("-" * 70)
for r in report.fold_results:
    print(f"  {r.summary()}")
