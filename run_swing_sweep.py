"""Sweep swing strategies -- the first search with an exit path.

Usage:  venv/bin/python -u run_swing_sweep.py [measure] [market_limit]

Runs on temperature markets by default: 29,354 settled instances against
463 for rain, and the better movement-to-cost ratio of the two.
"""
import sys
from collections import defaultdict

import candidates as candidate_lib
import splits
import swing
from evaluation import execution, harness, registry

MEASURE = sys.argv[1] if len(sys.argv) > 1 else "temperature_high"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 4000

markets = [m for m in splits.load("markets", split="train+dev")
           if m.get("measure") == MEASURE and m.get("result") in ("yes", "no")]
if LIMIT and len(markets) > LIMIT:
    # Most recent, so the sample reflects the market as it is now rather
    # than as it was when these series launched.
    markets = sorted(markets, key=lambda m: m.get("close_time") or "")[-LIMIT:]

forecasters = [
    candidate_lib.MomentumForecaster(12, 0.5),
    candidate_lib.MomentumForecaster(6, 0.5),
    candidate_lib.MomentumForecaster(24, 0.5),
    candidate_lib.MomentumForecaster(12, -0.5),
    candidate_lib.MeanReversionForecaster(6, 0.75),
    candidate_lib.MeanReversionForecaster(12, 0.5),
    candidate_lib.PriceLevelForecaster(-0.10),
    candidate_lib.PriceLevelForecaster(0.10),
]
population = swing.build_population(forecasters)

print(f"measure: {MEASURE}")
print(f"markets: {len(markets):,}")
print(f"candidates: {len(population):,} "
      f"({len(forecasters)} forecasters x {len(population)//len(forecasters)} exit rules)")
print(f"trials before: {registry.trials_to_date():,}")
print(registry.trial_banner())
print()

reports = harness.evaluate_many(
    population, markets,
    execution.HourlyCandleExecution(participation_rate=0.10),
    config_of=lambda c: {"candidate": c.name, "measure": MEASURE,
                          "mode": "swing"},
    seed=20260919, n_folds=3, swing=True, step_hours=3, progress=10)

passed = [r for r in reports if r.verdict.passed]
traded = [r for r in reports if r.result.n_trades > 0]
print(f"\n{'='*72}")
print(f"{len(reports)} evaluated   {len(traded)} traded   {len(passed)} PASSED")

print(f"\n=== TOP 12 BY NET P&L ===")
print(f"{'candidate':<46} {'net':>9} {'trades':>7} {'win%':>6} {'DSR':>7} {'v':>6}")
for r in sorted(traded, key=lambda r: -r.result.net_pnl_cents)[:12]:
    dsr = "n/a" if r.dsr_probability is None else f"{r.dsr_probability:.3f}"
    print(f"{r.candidate_name[:45]:<46} {r.result.net_pnl_cents/100:>8.2f} "
          f"{r.result.n_trades:>7} {r.result.win_rate:>5.0%} {dsr:>7} "
          f"{'PASS' if r.verdict.passed else 'FAIL':>6}")

print(f"\n=== BY FORECASTER ===")
by_kind = defaultdict(list)
for r, c in zip(reports, population):
    by_kind[c.forecaster.name].append(r)
print(f"{'forecaster':<40} {'best net':>10} {'median':>9} {'traded':>7}")
for name in sorted(by_kind):
    rs = by_kind[name]
    nets = sorted(r.result.net_pnl_cents for r in rs)
    print(f"{name[:39]:<40} {nets[-1]/100:>9.2f} "
          f"{nets[len(nets)//2]/100:>8.2f} "
          f"{sum(1 for r in rs if r.result.n_trades > 0):>7}")

if passed:
    print(f"\n=== PASSED ===")
    for r in passed:
        print(r.report())
        print("-" * 72)
else:
    print("\nNothing passed. The per-forecaster table is the useful output.")
print(f"\ntrials now: {registry.trials_to_date():,}")
