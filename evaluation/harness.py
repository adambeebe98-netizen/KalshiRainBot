"""
Orchestration and the verdict.

Everything else in this package exists so that this module can say PASS or
FAIL and be believed. The rules:

- A candidate and every baseline see the same folds, the same execution
  model, the same fees and the same trading rule. Only the forecast
  differs.
- A candidate PASSES only if, net of fees and out of sample, it beats
  every baseline AND its deflated Sharpe clears 0.95 AND the block
  bootstrap gate passes. Anything else FAILS, with the reason stated,
  regardless of how good the absolute numbers look.
- There is no "promising". A middle state is how candidates survive to be
  retested until one of the retests passes by luck, and the trial counter
  makes that expensive rather than free.

Two gates rather than one, because they fail differently. The deflated
Sharpe accounts for how many candidates have been looked at, which is the
error that scales with search size. The block bootstrap accounts for the
returns being lumpy, bimodal and serially clustered -- a whole bracket set
resolves at once -- which is the error that makes Sharpe the wrong tool
for this payoff shape. Passing one and failing the other is a fail.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from evaluation import baselines as baselines_mod
from evaluation import folds as folds_mod
from evaluation import objective, pit, registry, stats
from evaluation.execution import ExecutionModel

DEFAULT_DECISION_HORIZONS_S = (24 * 3600,)
DSR_THRESHOLD = 0.95
BOOTSTRAP_THRESHOLD = 0.05      # max tolerated share of resamples at or below zero


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reasons: tuple[str, ...]

    def __str__(self) -> str:
        head = "PASS" if self.passed else "FAIL"
        return head + "".join(f"\n  - {r}" for r in self.reasons)


@dataclass(frozen=True)
class CandidateReport:
    candidate_name: str
    run: registry.RunRecord
    result: objective.Result
    fold_results: tuple[objective.Result, ...]
    baseline_results: dict
    trials_to_date: int
    luck_threshold: float
    dsr_probability: float | None
    bootstrap_p_at_or_below_zero: float | None
    verdict: Verdict
    notes: tuple[str, ...] = field(default=())

    def report(self) -> str:
        lines = [f"candidate: {self.candidate_name}",
                 f"trials to date: {self.trials_to_date} -- a no-skill "
                 f"best-of-{self.trials_to_date} reaches SR "
                 f"{self.luck_threshold:.3f} by luck alone",
                 self.result.summary()]
        for _, res in sorted(self.baseline_results.items()):
            lines.append(f"  baseline {res.summary()}")
        dsr = ("n/a" if self.dsr_probability is None
               else f"{self.dsr_probability:.4f}")
        boot = ("n/a" if self.bootstrap_p_at_or_below_zero is None
                else f"{self.bootstrap_p_at_or_below_zero:.4f}")
        lines.append(f"  deflated Sharpe P(skill) = {dsr} "
                     f"(needs > {DSR_THRESHOLD})")
        lines.append(f"  bootstrap P(mean <= 0) = {boot} "
                     f"(needs < {BOOTSTRAP_THRESHOLD})")
        lines.extend(f"  note: {n}" for n in self.notes)
        lines.append(str(self.verdict))
        lines.append(self.result.assumptions.describe())
        return "\n".join(lines)


def _decision_times(close_time_iso: str, horizons_s) -> list[int]:
    close = pit.iso_to_epoch(close_time_iso)
    if close is None:
        return []
    return [close - h for h in horizons_s]


def _trades_per_day(trades) -> int:
    """Block length for the bootstrap: roughly one market-day of trades.

    Resampling single trades would treat a bracket set that all resolved
    on the same weather as independent evidence, which is exactly the
    dependence the block bootstrap exists to respect.
    """
    if not trades:
        return 1
    days = {}
    for t in trades:
        day = t.fill.filled_at // 86400
        days[day] = days.get(day, 0) + 1
    return max(1, round(sum(days.values()) / len(days)))


def _run_one(trader, fold, execution_model: ExecutionModel, horizons_s,
             db_path: str | None, label: str,
             price_cache=None) -> objective.Result:
    """Run one trader over one fold's test markets."""
    trades = []
    n_decisions = 0
    # Hoisted: the terms view and the labels are the same for every ticker
    # in the fold, and rebuilding them per market opened a fresh SQLite
    # connection on every iteration.
    cache = price_cache or pit.PriceCache(fold.test_tickers, db_path=db_path)
    terms_view = pit.PointInTimeView(
        fold.test_end, sources=["historical_price_points"], db_path=db_path,
        price_cache=cache)
    labels = pit.labels_for(fold.test_tickers, db_path=db_path)
    for ticker in sorted(fold.test_tickers):
        terms = terms_view.market(ticker)
        if terms is None or not terms.close_time:
            continue
        label_row = labels.get(ticker)
        if label_row is None:
            continue                      # unsettled or 'scalar'
        opened = pit.iso_to_epoch(terms.open_time)
        for as_of in _decision_times(terms.close_time, horizons_s):
            if opened is None or as_of < opened:
                continue                  # market did not exist yet
            # Visibility is decided by that comparison, not by re-querying
            # the market -- view.market() would return exactly the same
            # answer at the cost of a database round trip per decision.
            view = pit.PointInTimeView(
                as_of, sources=["historical_price_points"], db_path=db_path,
                price_cache=cache)
            n_decisions += 1
            decision = trader.decide(view, terms, as_of)
            fill = execution_model.execute(decision, view)
            if fill is None:
                continue
            trades.append(objective.settle(fill, label_row.result))
    return objective.Result(trades=tuple(trades),
                            assumptions=execution_model.assumptions(),
                            n_decisions=n_decisions, label=label)


def evaluate(candidate, markets: list[dict], execution_model: ExecutionModel,
             config: dict, seed: int = 0, n_folds: int = 5,
             purge_seconds: int = folds_mod.DEFAULT_PURGE_S,
             embargo_seconds: int = folds_mod.DEFAULT_EMBARGO_S,
             horizons_s=DEFAULT_DECISION_HORIZONS_S,
             db_path: str | None = None,
             splits_used: str = "train+dev",
             bootstrap_resamples: int = 2000) -> CandidateReport:
    """Evaluate one candidate end to end and return a verdict.

    `candidate` is anything with `.name` and
    `.decide(view, terms, as_of) -> Decision` -- the same interface the
    baselines implement, so they are genuinely interchangeable.
    """
    run = registry.open_run(
        config=config, seed=seed, splits_used=splits_used,
        purge_seconds=purge_seconds, embargo_seconds=embargo_seconds,
        execution_assumptions=execution_model.assumptions(), db_path=db_path)

    fold_list = folds_mod.generate(markets, n_folds=n_folds,
                                   purge_seconds=purge_seconds,
                                   embargo_seconds=embargo_seconds)

    notes: list[str] = []
    candidate_folds = []
    baseline_folds: dict[str, list] = {}

    for fold in fold_list:
        # One prefetch per fold, shared by the candidate and every
        # baseline. They all read the same immutable candles, and each
        # still filters them through its own view.
        cache = pit.PriceCache(fold.test_tickers, db_path=db_path)
        candidate_folds.append(_run_one(
            candidate, fold, execution_model, horizons_s, db_path,
            label=f"{candidate.name} fold {fold.index}", price_cache=cache))
        # Baselines are refitted per fold on that fold's TRAINING markets
        # only -- a constant fitted on everything would have seen the test
        # window, which is the whole thing the fold structure prevents.
        for trader in baselines_mod.standard_baselines(
                fold.train_tickers, db_path=db_path):
            # Keyed by kind, not name. The constant baseline is refit per
            # fold so its name carries a different fitted value each time;
            # keying on the name split one baseline into three partial
            # ones, each holding a third of the trades and each a far
            # easier target than the pooled baseline actually is.
            baseline_folds.setdefault(trader.kind, []).append(
                _run_one(trader, fold, execution_model, horizons_s, db_path,
                         label=f"{trader.name} fold {fold.index}",
                         price_cache=cache))

    result = objective.combine(candidate_folds, label=candidate.name)
    baseline_results = {name: objective.combine(runs, label=name)
                        for name, runs in baseline_folds.items()}

    # -- gates ----------------------------------------------------------

    reasons: list[str] = []
    trials = registry.trials_to_date(db_path) + 1   # including this one
    variance = registry.observed_sharpe_variance(db_path)
    threshold = stats.expected_max_sharpe(max(trials, 1), variance)

    if result.n_trades == 0:
        reasons.append("no trades were filled, so there is nothing to judge")
        dsr_p = None
        boot_p = None
    else:
        if result.net_pnl_cents <= 0:
            reasons.append(
                f"net P&L {result.net_pnl_cents / 100:+.2f} USD is not "
                f"positive after fees -- it did not beat paying the price")
        for name, base in baseline_results.items():
            if result.net_pnl_cents <= base.net_pnl_cents:
                reasons.append(
                    f"did not beat baseline {name} "
                    f"({result.net_pnl_cents / 100:+.2f} vs "
                    f"{base.net_pnl_cents / 100:+.2f} USD)")

        try:
            dsr = stats.deflated_sharpe_ratio(
                result.net_returns, n_trials=trials, benchmark_sharpe=threshold)
            dsr_p = dsr.probability
            if dsr_p <= DSR_THRESHOLD:
                reasons.append(
                    f"deflated Sharpe P(skill) {dsr_p:.4f} does not clear "
                    f"{DSR_THRESHOLD} against a luck threshold of "
                    f"{threshold:.3f} at {trials} trials")
        except ValueError as exc:
            dsr_p = None
            reasons.append(f"deflated Sharpe could not be computed: {exc}")

        block = _trades_per_day(result.trades)
        if len(result.trades) < 4:
            boot_p = None
            reasons.append(
                f"only {len(result.trades)} trades -- too few to bootstrap")
        else:
            samples = stats.moving_block_bootstrap(
                [t.net_pnl_cents for t in result.trades],
                block_size=min(block, len(result.trades)),
                n_resamples=bootstrap_resamples, seed=seed)
            boot_p = sum(1 for s in samples if s <= 0) / len(samples)
            if boot_p >= BOOTSTRAP_THRESHOLD:
                reasons.append(
                    f"block bootstrap: {boot_p:.1%} of resamples are at or "
                    f"below zero (needs under {BOOTSTRAP_THRESHOLD:.0%}); "
                    f"block length {block} trades")

    notes.append(
        "the best-heuristic baseline is not implemented, so this candidate "
        "was compared against two baselines, not three")

    verdict = Verdict(passed=not reasons,
                      reasons=tuple(reasons) if reasons
                      else ("beat every baseline net of fees, cleared the "
                            "deflated Sharpe threshold, and survived the "
                            "block bootstrap",))

    registry.record_trial(run.run_id, candidate.name, run.config_hash,
                          net_sharpe=result.net_sharpe(),
                          verdict="PASS" if verdict.passed else "FAIL",
                          db_path=db_path)

    return CandidateReport(
        candidate_name=candidate.name, run=run, result=result,
        fold_results=tuple(candidate_folds), baseline_results=baseline_results,
        trials_to_date=trials, luck_threshold=threshold,
        dsr_probability=dsr_p, bootstrap_p_at_or_below_zero=boot_p,
        verdict=verdict, notes=tuple(notes))
