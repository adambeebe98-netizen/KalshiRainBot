"""A focused sweep around the one thing that showed consistent signal.

The broad sweep found momentum at a 6-hour lookback net-positive at its
MEDIAN exit rule -- not just its best -- which is the only result so far
that held across configurations rather than at a single lucky point.

This maps the neighbourhood: lookbacks either side of 6 hours, strengths
either side of +0.5, and entry thresholds, which the broad sweep never
varied at all. Exit rules are deliberately cut to four that actually
differ. The previous run wasted a third of its candidates on
max_hold_hours settings that never bound at a 3-hour step and produced
byte-identical results.

Larger market sample than before (5x), because the previous answer rested
on 52 trades and the question now is whether the signal is real rather
than whether it exists at one parameter.

This is a focused search, not a fishing expedition, and the distinction
is not stylistic: every candidate here is a permanent trial that raises
the bar for everything after it. 112 aimed at one hypothesis buys more
than 500 aimed at nothing.
"""
import sys
from collections import defaultdict

import candidates as candidate_lib
import splits
import swing
from evaluation import execution, harness, registry

MEASURE = "temperature_high"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
STEP_H = 3

markets = [m for m in splits.load("markets", split="train+dev")
           if m.get("measure") == MEASURE and m.get("result") in ("yes", "no")]
markets = sorted(markets, key=lambda m: m.get("close_time") or "")[-LIMIT:]

forecasters = [candidate_lib.MomentumForecaster(lb, k)
               for lb in (3, 4, 5, 6, 7, 8, 10)
               for k in (0.25, 0.5, 0.75, 1.0)]

# Four exit rules that genuinely differ at a 3-hour step, each paired
# with a distinct entry threshold so the grid explores both.
configs = [
    swing.SwingConfig(edge_threshold=0.05, exit_edge=0.01),
    swing.SwingConfig(edge_threshold=0.08, exit_edge=0.02),
    swing.SwingConfig(edge_threshold=0.08, exit_edge=0.02,
                      take_profit_cents=15, stop_loss_cents=10),
    swing.SwingConfig(edge_threshold=0.12, exit_edge=0.04,
                      take_profit_cents=20, stop_loss_cents=15),
]
population = [swing.SwingTrader(f, c) for f in forecasters for c in configs]

print(f"{MEASURE}: {len(markets):,} markets, {len(population)} candidates")
print(f"({len(forecasters)} forecasters x {len(configs)} exit/entry rules)")
print(registry.trial_banner())
print()

reports = harness.evaluate_many(
    population, markets,
    execution.HourlyCandleExecution(participation_rate=0.10),
    config_of=lambda c: {"candidate": c.name, "measure": MEASURE,
                          "mode": "swing", "focus": "momentum"},
    seed=20260919, n_folds=3, swing=True, step_hours=STEP_H, progress=20)

traded = [r for r in reports if r.result.n_trades > 0]
passed = [r for r in reports if r.verdict.passed]
print(f"\n{'='*74}")
print(f"{len(reports)} evaluated   {len(traded)} traded   {len(passed)} PASSED")

print(f"\n=== NET P&L BY LOOKBACK (across all k and exit rules) ===")
by_lb = defaultdict(list)
for r, c in zip(reports, population):
    by_lb[c.forecaster.lookback_h].append(r.result.net_pnl_cents)
print(f"{'lookback':>9} {'n':>4} {'best':>9} {'median':>9} {'worst':>9} {'>0':>5}")
for lb in sorted(by_lb):
    v = sorted(by_lb[lb])
    pos = sum(1 for x in v if x > 0)
    print(f"{lb:>8}h {len(v):>4} {v[-1]/100:>8.2f} {v[len(v)//2]/100:>8.2f} "
          f"{v[0]/100:>8.2f} {pos:>4}")

print(f"\n=== NET P&L BY STRENGTH ===")
by_k = defaultdict(list)
for r, c in zip(reports, population):
    by_k[c.forecaster.strength].append(r.result.net_pnl_cents)
print(f"{'k':>9} {'n':>4} {'best':>9} {'median':>9} {'>0':>5}")
for k in sorted(by_k):
    v = sorted(by_k[k])
    pos = sum(1 for x in v if x > 0)
    print(f"{k:>+9.2f} {len(v):>4} {v[-1]/100:>8.2f} {v[len(v)//2]/100:>8.2f} "
          f"{pos:>4}")

print(f"\n=== TOP 10 ===")
print(f"{'candidate':<50} {'net':>8} {'trades':>7} {'win%':>6} {'DSR':>7}")
for r in sorted(traded, key=lambda r: -r.result.net_pnl_cents)[:10]:
    dsr = "n/a" if r.dsr_probability is None else f"{r.dsr_probability:.3f}"
    print(f"{r.candidate_name[:49]:<50} {r.result.net_pnl_cents/100:>7.2f} "
          f"{r.result.n_trades:>7} {r.result.win_rate:>5.0%} {dsr:>7}")

print(f"\nA ridge -- neighbouring lookbacks positive together -- is worth")
print("more than any single peak. A lone spike surrounded by losses is")
print("what overfitting looks like.")
print(f"\ntrials now: {registry.trials_to_date():,}")
