"""Search a population of candidates, honestly.

Usage:  venv/bin/python -u run_sweep.py [measure] [n_folds]

Reports by FAMILY as well as by candidate, because "which of these 300
variants scored best" is mostly a question about luck, while "did any of
these six ideas survive" is a question about the market.
"""
import sys
from collections import defaultdict

import candidates as candidate_lib
import splits
from evaluation import execution, harness, registry

MEASURE = sys.argv[1] if len(sys.argv) > 1 else "precipitation_daily"
N_FOLDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

markets = [m for m in splits.load("markets", split="train+dev")
           if m.get("measure") == MEASURE and m.get("result") in ("yes", "no")]
population = candidate_lib.build_population()

print(f"measure: {MEASURE}")
print(f"markets: {len(markets):,}")
print(f"candidates: {len(population):,}")
print(f"trials before this sweep: {registry.trials_to_date():,}")
print(f"\nThis sweep adds {len(population):,} permanent trials. Every future")
print("candidate is judged against the resulting luck threshold.\n")

reports = harness.evaluate_many(
    population, markets,
    execution.HourlyCandleExecution(participation_rate=0.10),
    config_of=lambda c: {"candidate": c.name, "measure": MEASURE},
    seed=20260919, n_folds=N_FOLDS, progress=25)

passed = [r for r in reports if r.verdict.passed]
traded = [r for r in reports if r.result.n_trades > 0]
print(f"\n{'='*70}")
print(f"{len(reports):,} evaluated   {len(traded):,} traded at all   "
      f"{len(passed):,} PASSED")
print(f"trials to date now: {registry.trials_to_date():,}")

print(f"\n=== BY FAMILY ===")
print(f"{'family':<18} {'n':>5} {'traded':>7} {'passed':>7} "
      f"{'best net':>10} {'median net':>11}")
by_family = defaultdict(list)
for r, c in zip(reports, population):
    by_family[c.forecaster.kind].append(r)
for kind in sorted(by_family):
    rs = by_family[kind]
    nets = sorted(r.result.net_pnl_cents for r in rs)
    t = sum(1 for r in rs if r.result.n_trades > 0)
    p = sum(1 for r in rs if r.verdict.passed)
    print(f"{kind:<18} {len(rs):>5} {t:>7} {p:>7} "
          f"{nets[-1]/100:>9.2f} {nets[len(nets)//2]/100:>10.2f}")

print(f"\n=== TOP 10 BY NET P&L (net of fees, out of sample) ===")
ranked = sorted([r for r in reports if r.result.n_trades > 0],
                key=lambda r: -r.result.net_pnl_cents)[:10]
print(f"{'candidate':<44} {'net':>8} {'trades':>7} {'DSR':>7} {'verdict':>8}")
for r in ranked:
    dsr = "n/a" if r.dsr_probability is None else f"{r.dsr_probability:.3f}"
    print(f"{r.candidate_name[:43]:<44} {r.result.net_pnl_cents/100:>7.2f} "
          f"{r.result.n_trades:>7} {dsr:>7} "
          f"{'PASS' if r.verdict.passed else 'FAIL':>8}")

if passed:
    print(f"\n=== THE {len(passed)} THAT PASSED ===")
    for r in passed:
        print(r.report())
        print("-" * 70)
else:
    print("\nNothing passed. With a search this size that is the expected")
    print("outcome unless something real is there -- the luck threshold now")
    print("sits where it does precisely so that a lucky winner cannot clear")
    print("it. The per-family table above is the useful output: it says")
    print("which ideas produced net-positive results at all, which is worth")
    print("more than any single candidate's number.")
